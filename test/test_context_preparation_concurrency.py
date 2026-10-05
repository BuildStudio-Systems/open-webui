"""Exercise the actual chat payload context stage without importing the app."""
import ast
import asyncio
import logging
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'backend/open_webui/utils/middleware.py'


def load_stage(files, history, source=None):
    tree = ast.parse(source or SOURCE.read_text('utf-8'))
    process = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_chat_payload')
    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    start = next(i for i, n in enumerate(process.body) if assigns(n, 'file_context_enabled'))
    end = next(i for i in range(start + 1, len(process.body)) if assigns(process.body[i], 'system_message'))
    wrapper = ast.parse('async def stage(request, form_data, extra_params, user, metadata, model, prompt, sources):\n    return form_data, sources').body[0]
    wrapper.body = process.body[start:end] + wrapper.body
    helpers = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'chat_completion_context_handler']
    spec = importlib.util.spec_from_file_location('context_chat_ids', SOURCE.with_name('chat_id.py'))
    chat_ids = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chat_ids)
    namespace = dict(asyncio=asyncio, log=logging.getLogger(__name__),
                     chat_completion_files_handler=files, is_saved_chat_id=chat_ids.is_saved_chat_id)
    module = ast.Module(body=helpers + [wrapper], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    personal = ModuleType('open_webui.there_integration.personal')
    personal.personal_sources = history
    return namespace['stage'], personal


class ContextPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def run_stage(self, files, history, *, metadata=None, enabled=True, prompt='synthetic query'):
        stage, personal = load_stage(files, history)
        owner = SimpleNamespace(id='alice')
        body = {'messages': [{'role': 'user', 'content': prompt}]}
        with patch.dict(sys.modules, {personal.__name__: personal}):
            return await stage(None, body, {}, owner,
                metadata if metadata is not None else {'session_id': 'session', 'chat_id': 'owned'},
                {'info': {'meta': {'capabilities': {'file_context': enabled}}}}, prompt, ['existing'])

    async def test_both_start_before_either_finishes_and_order_is_stable(self):
        file_started, history_started = asyncio.Event(), asyncio.Event()
        async def files(request, body, extra, user):
            file_started.set()
            await asyncio.wait_for(history_started.wait(), .5)
            return {**body, 'prepared': True}, {'sources': ['file']}
        async def history(user, chat_id, query):
            self.assertEqual((user.id, chat_id, query), ('alice', 'owned', 'synthetic query'))
            history_started.set()
            await asyncio.wait_for(file_started.wait(), .5)
            return ['history']
        body, sources = await self.run_stage(files, history)
        self.assertTrue(body.get('prepared'))
        self.assertEqual(sources, ['existing', 'file', 'history'])

    async def test_failure_of_either_source_keeps_the_other(self):
        for failed in ('file', 'history'):
            with self.subTest(failed=failed):
                async def files(request, body, extra, user):
                    if failed == 'file':
                        raise RuntimeError('private fixture content must not be logged')
                    return body, {'sources': ['file']}
                async def history(*args):
                    if failed == 'history':
                        raise RuntimeError('private fixture content must not be logged')
                    return ['history']
                with self.assertLogs(__name__, level='WARNING') as messages:
                    _, sources = await self.run_stage(files, history)
                self.assertEqual(sources, ['existing', 'history' if failed == 'file' else 'file'])
                self.assertNotIn('private fixture', '\n'.join(messages.output))

    async def test_disconnect_cancels_and_joins_both_sources(self):
        entered = [asyncio.Event(), asyncio.Event()]
        closed = [asyncio.Event(), asyncio.Event()]
        async def wait(index):
            entered[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                # Async DB/HTTP finalizers must survive cancellation cleanup.
                await asyncio.sleep(.01 * (index + 1))
                closed[index].set()
        async def files(*args):
            await wait(0)
        async def history(*args):
            await wait(1)
        task = asyncio.create_task(self.run_stage(files, history))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), .5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(event.is_set() for event in closed))

    async def test_noninteractive_temporary_and_empty_prompts_skip_history(self):
        for metadata, prompt in (({}, 'query'), ({'session_id': 's', 'chat_id': 'local:temp'}, 'query'),
                                 ({'session_id': 's', 'chat_id': 'temporary:temp'}, 'query'),
                                 ({'session_id': 's', 'chat_id': 'channel:temp'}, 'query'),
                                 ({'session_id': 's', 'chat_id': 'owned'}, '')):
            async def files(request, body, extra, user):
                return body, {'sources': ['file']}
            async def history(*args):
                self.fail('History must not be read for this destination')
            _, sources = await self.run_stage(files, history, metadata=metadata, prompt=prompt)
            self.assertEqual(sources, ['existing', 'file'])

    async def test_disabled_file_context_preserves_history(self):
        async def files(*args):
            self.fail('File context disabled')
        async def history(*args):
            return ['history']
        _, sources = await self.run_stage(files, history, enabled=False)
        self.assertEqual(sources, ['existing', 'history'])


if __name__ == '__main__':
    unittest.main()
