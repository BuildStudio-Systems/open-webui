"""Offline image delivery tests: real helpers, extracted router, synthetic I/O."""
import ast
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import aiohttp

UTILS = Path(__file__).resolve().parents[1] / 'backend/open_webui/utils'
spec = importlib.util.spec_from_file_location('image_prefetch', UTILS / 'images/prefetch.py')
prefetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefetch)


def extracted(path, name, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace[name]


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_bounded_concurrency(self):
        entered, finished, output = [], [], []
        release = asyncio.Event()

        async def load(i):
            entered.append(i)
            if i == 0:
                await release.wait()
            finished.append(i)
            return i

        async with prefetch.prefetch_images(range(10), load) as results:
            first = asyncio.create_task(anext(results))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(entered, [0, 1])
            self.assertEqual(finished, [1])
            release.set()
            output.append(await first)
            async for value in results:
                output.append(value)
        self.assertEqual(output, list(range(10)))

    async def test_empty(self):
        async def unexpected(_):
            self.fail('No reads for an empty result')
        async with prefetch.prefetch_images([], unexpected) as results:
            self.assertEqual([v async for v in results], [])

    async def test_single(self):
        async def load(value):
            return value
        async with prefetch.prefetch_images([b'unchanged'], load) as results:
            self.assertEqual([v async for v in results], [b'unchanged'])

    async def test_consumer_failure_or_break_drains_reads(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                drained = asyncio.Event()
                entered = []
                async def load(i):
                    entered.append(i)
                    if i:
                        try:
                            await asyncio.Future()
                        finally:
                            drained.set()
                    return i
                try:
                    async with prefetch.prefetch_images(range(9), load) as results:
                        async for _ in results:
                            if fail:
                                raise ValueError('upload failed')
                            break
                except ValueError:
                    self.assertTrue(fail)
                self.assertTrue(drained.is_set())
                self.assertEqual(entered, [0, 1])

    async def test_read_failure_drains_sibling(self):
        drained = asyncio.Event()
        async def load(i):
            if i == 0:
                await asyncio.sleep(0)
                raise ValueError('read failed')
            try:
                await asyncio.Future()
            finally:
                drained.set()
        with self.assertRaisesRegex(ValueError, 'read failed'):
            async with prefetch.prefetch_images(range(5), load) as results:
                async for _ in results:
                    self.fail('Failed image cannot be published')
        self.assertTrue(drained.is_set())

    async def test_request_cancellation_drains_all_reads(self):
        entered, drained = [], []
        async def load(i):
            entered.append(i)
            try:
                await asyncio.Future()
            finally:
                drained.append(i)
        async def request():
            async with prefetch.prefetch_images(range(9), load) as results:
                async for _ in results:
                    pass
        task = asyncio.create_task(request())
        while len(entered) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(drained), [0, 1])

    async def test_concurrent_requests_do_not_share_results(self):
        async def request(owner):
            async def load(i):
                await asyncio.sleep(0)
                return owner, i
            async with prefetch.prefetch_images(range(3), load) as results:
                return [v async for v in results]
        self.assertEqual(await asyncio.gather(request('A'), request('B')),
                         [[('A', i) for i in range(3)], [('B', i) for i in range(3)]])


class ComfyEventsTests(unittest.IsolatedAsyncioTestCase):
    def helper(self, history_calls):
        async def queue(*args):
            return {'prompt_id': 'ours'}
        async def history(*args):
            history_calls.append(args)
            return {'ours': {'outputs': {'1': {'images': [{'filename': 'a.png', 'subfolder': '', 'type': 'output'}]}}}}
        return extracted(UTILS / 'images/comfyui.py', '_ws_get_images', {
            'aiohttp': aiohttp, 'JSONCodec': json, 'queue_prompt': queue,
            'get_history': history, 'get_image_url': lambda *a: 'synthetic-url',
        })

    async def test_failure_and_interruption_fail_immediately_without_history(self):
        for kind in ('execution_error', 'execution_interrupted'):
            with self.subTest(kind=kind):
                calls = []
                async def messages():
                    yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
                        'type': kind, 'data': {'prompt_id': 'ours', 'exception_message': 'private/path'}}))
                    self.fail('Waited after terminal failure')
                with self.assertRaisesRegex(RuntimeError, '^ComfyUI image generation failed or was interrupted$'):
                    await self.helper(calls)(messages(), {}, 'client', 'base', 'key')
                self.assertEqual(calls, [])

    async def test_foreign_failure_and_preview_do_not_end_own_generation(self):
        calls = []
        async def messages():
            yield SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b'preview')
            for message in [
                {'type': 'execution_error', 'data': {'prompt_id': 'other'}},
                {'type': 'executing', 'data': {'prompt_id': 'ours', 'node': '1'}},
                {'type': 'executing', 'data': {'prompt_id': 'ours', 'node': None}},
            ]:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))
        result = await self.helper(calls)(messages(), {'1': {'class_type': 'SaveImage'}}, 'client', 'base', 'key')
        self.assertEqual(result, {'data': [{'url': 'synthetic-url'}]})
        self.assertEqual(len(calls), 1)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_url_and_base64_results_keep_order_and_generation_parameters(self):
        config = SimpleNamespace(IMAGE_SIZE='512x512', IMAGE_GENERATION_ENGINE='openai',
            IMAGES_OPENAI_API_KEY='synthetic-key', IMAGES_OPENAI_API_BASE_URL='https://synthetic.test',
            IMAGES_OPENAI_API_VERSION='', IMAGE_GENERATION_MODEL='same-model', IMAGES_OPENAI_API_PARAMS={})
        reads, writes, submissions = [], [], []
        owner = SimpleNamespace(id='alice')
        async def get_config(): return config
        async def get_model(_): return 'same-model'
        async def load(value, headers=None):
            reads.append((value, headers))
            await asyncio.sleep(0)
            return value.encode(), 'image/png'
        async def upload(request, body, mime, metadata, user):
            self.assertIs(user, owner)
            self.assertEqual(metadata['chat_id'], 'owned')
            writes.append(body)
            return None, {'id': body.decode()}
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def json(self, **kwargs):
                return {'data': [{'url': 'https://synthetic.test/1.png'}, {'b64_json': 'synthetic-base64'}]}
        class Session:
            def post(self, **kwargs):
                submissions.append(kwargs)
                return Response()
        async def get_session(): return Session()
        namespace = dict(get_image_config=get_config, get_image_model=get_model, get_image_data=load,
                         upload_image=upload, get_session=get_session, prefetch_images=prefetch.prefetch_images,
                         ENABLE_FORWARD_USER_INFO_HEADERS=False, re=__import__('re'),
                         IMAGE_URL_RESPONSE_MODELS_REGEX_PATTERN='^url-model$', AIOHTTP_CLIENT_SESSION_SSL=True)
        router = extracted(UTILS.parent / 'routers/images.py', 'image_generations', namespace)
        result = await router(None, SimpleNamespace(size=None, prompt='same prompt', n=2),
                              metadata={'chat_id': 'owned'}, user=owner)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]['json'], {'model': 'same-model', 'prompt': 'same prompt',
                                               'n': 2, 'size': '512x512', 'response_format': 'b64_json'})
        self.assertEqual(reads, [('https://synthetic.test/1.png', {'Authorization': 'Bearer synthetic-key'}),
                                ('synthetic-base64', None)])
        self.assertEqual(result, [{'id': body.decode()} for body in writes])
        self.assertEqual(writes, [b'https://synthetic.test/1.png', b'synthetic-base64'])

    async def test_comfy_generation_and_edit_preserve_order_owner_and_metadata(self):
        for edit in (False, True):
            with self.subTest(edit=edit):
                config = SimpleNamespace(
                    IMAGE_SIZE='512x512', IMAGE_GENERATION_ENGINE='comfyui',
                    IMAGE_EDIT_ENGINE='comfyui', ENABLE_IMAGE_EDIT=True,
                    IMAGE_EDIT_SIZE='512x512', IMAGE_EDIT_MODEL='same-model',
                    IMAGE_STEPS=None, COMFYUI_WORKFLOW='{}', COMFYUI_WORKFLOW_NODES=[],
                    COMFYUI_BASE_URL='http://synthetic.test', COMFYUI_API_KEY='test-key',
                    IMAGES_EDIT_COMFYUI_BASE_URL='http://synthetic.test',
                    IMAGES_EDIT_COMFYUI_API_KEY='test-key',
                    IMAGES_EDIT_COMFYUI_WORKFLOW='{}', IMAGES_EDIT_COMFYUI_WORKFLOW_NODES=[],
                )
                writes, reads = [], []
                owner = SimpleNamespace(id='synthetic-owner')
                async def get_config(): return config
                async def get_model(request): return 'same-model'
                async def generate(*args): return {'data': [{'url': str(i)} for i in range(4)]}
                async def upload_input(*args): return {'name': 'input.png'}
                async def load(url, headers, trusted_base_url):
                    reads.append((url, headers, trusted_base_url))
                    await asyncio.sleep(0)
                    return url.encode(), 'image/png'
                async def upload(request, body, mime, metadata, user):
                    self.assertIs(user, owner)
                    self.assertEqual(metadata['chat_id'], 'owned-chat')
                    writes.append(body)
                    return None, {'id': body.decode()}
                class Form(SimpleNamespace):
                    def model_dump(self, **kw): return vars(self)
                namespace = dict(
                    get_image_config=get_config, get_image_model=get_model,
                    ComfyUICreateImageForm=Form, ComfyUIWorkflow=Form,
                    ComfyUIEditImageForm=Form, comfyui_edit_image=generate,
                    comfyui_upload_image=upload_input,
                    get_image_file_item=lambda _: ('image', ('input.png', b'test', 'image/png')),
                    Depends=lambda _: None, get_verified_user=lambda: None,
                    comfyui_create_image=generate, get_image_data=load,
                    upload_image=upload, prefetch_images=prefetch.prefetch_images,
                    uuid=__import__('uuid'), log=SimpleNamespace(debug=lambda *a: None),
                )
                router = extracted(UTILS.parent / 'routers/images.py', 'image_edits' if edit else 'image_generations', namespace)
                result = await router(None, Form(size=None, prompt='test', n=4, steps=None, negative_prompt=None,
                                                model=None, image='data:image/png;base64,dGVzdA=='),
                                      metadata={'chat_id': 'owned-chat'}, user=owner)
                self.assertEqual(writes, [b'0', b'1', b'2', b'3'])
                self.assertEqual(result, [{'id': str(i)} for i in range(4)])
                self.assertTrue(all(headers == {'Authorization': 'Bearer test-key'} and
                                    base == 'http://synthetic.test' for _, headers, base in reads))


if __name__ == '__main__':
    unittest.main()
