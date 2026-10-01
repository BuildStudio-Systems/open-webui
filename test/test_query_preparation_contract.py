"""Exercise real handlers with isolated model/retrieval/config dependencies."""
import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1] / 'backend/open_webui'


def handler(relative, name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    node.args.defaults = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), relative, 'exec'), namespace)
    return namespace[name]


class QueryPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(get=AsyncMock(return_value=True), get_many=AsyncMock(return_value={}))
        self.request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(
            MODELS={}, RERANKING_FUNCTION=None)))
        self.namespace = dict(Config=self.config, json=json, log=logging.getLogger('query-test'))
        self.generate = handler('routers/tasks.py', 'generate_queries', self.namespace)
        self.body = {'model': 'fixture', 'messages': [{'role': 'user', 'content': '请查找服务器备份'}],
                     'metadata': {'files': [{'id': 'attachment', 'type': 'file'}]}}
        self.sources = [{'source': {'id': 'attachment'}, 'document': ['actual supplied text'], 'metadata': []}]
        self.retrieve = AsyncMock(return_value=self.sources)
        self.events = AsyncMock()

    def files_handler(self, generator):
        namespace = dict(Config=self.config, generate_queries=generator,
                         get_last_user_message=lambda messages: messages[-1]['content'],
                         get_sources_from_items=self.retrieve, JSONCodec=json, log=logging.getLogger('query-test'))
        return handler('utils/middleware.py', 'chat_completion_files_handler', namespace)

    async def test_cache_envelope_nonempty_and_empty_without_model(self):
        for queries in (['服务器备份', 'バックアップ', 'backup'], []):
            with self.subTest(queries=queries):
                self.request.state.cached_queries = queries
                result = await self.generate(self.request, {'type': 'retrieval'}, object())
                self.assertEqual(json.loads(result['choices'][0]['message']['content']), {'queries': queries})

    async def test_cached_web_queries_reach_file_retriever(self):
        self.request.state.cached_queries = ['specific backup query']
        _, result = await self.files_handler(self.generate)(self.request, self.body, {'__event_emitter__': self.events}, object())
        self.assertEqual(self.retrieve.call_args.kwargs['queries'], ['specific backup query'])
        self.assertEqual(result['sources'], self.sources)

    async def test_global_full_context_skips_model_and_keeps_source(self):
        self.config.get_many.return_value = {'rag.full_context': True}
        model = AsyncMock(side_effect=AssertionError('unnecessary model call'))
        _, result = await self.files_handler(model)(self.request, self.body, {'__event_emitter__': self.events}, object())
        model.assert_not_awaited()
        self.assertTrue(self.retrieve.call_args.kwargs['full_context'])
        self.assertEqual(result['sources'], self.sources)
        self.config.get_many.assert_awaited_once()

    async def test_all_explicit_full_context_skips_model(self):
        self.body['metadata']['files'][0]['context'] = 'full'
        model = AsyncMock()
        await self.files_handler(model)(self.request, self.body, {'__event_emitter__': self.events}, object())
        model.assert_not_awaited()
        self.assertTrue(self.retrieve.call_args.kwargs['full_context'])

    async def test_mixed_context_still_generates_queries(self):
        self.body['metadata']['files'].append({'id': 'full', 'type': 'file', 'context': 'full'})
        model = AsyncMock(return_value={'choices': [{'message': {'content': '{"queries":["specific query"]}'}}]})
        await self.files_handler(model)(self.request, self.body, {'__event_emitter__': self.events}, object())
        model.assert_awaited_once()
        self.assertFalse(self.retrieve.call_args.kwargs['full_context'])
        self.assertEqual(self.retrieve.call_args.kwargs['queries'], ['specific query'])

    async def test_disabled_retrieval_does_not_use_cache(self):
        class Disabled(Exception):
            def __init__(self, **kwargs): pass
        self.namespace.update(HTTPException=Disabled, status=SimpleNamespace(HTTP_400_BAD_REQUEST=400),
                              ERROR_MESSAGES=SimpleNamespace(FEATURE_DISABLED=lambda _: 'disabled'))
        self.config.get.return_value = False
        self.request.state.cached_queries = ['cached']
        with self.assertRaises(Disabled):
            await self.generate(self.request, {'type': 'retrieval'}, object())

    async def test_no_files_avoids_config_and_model(self):
        self.body['metadata']['files'] = []
        model = AsyncMock()
        await self.files_handler(model)(self.request, self.body, {'__event_emitter__': self.events}, object())
        self.config.get_many.assert_not_awaited()
        model.assert_not_awaited()
        self.retrieve.assert_not_awaited()

    async def test_requests_do_not_share_cached_queries(self):
        self.request.state.cached_queries = ['cached']
        other = SimpleNamespace(state=SimpleNamespace(), app=self.request.app)
        # No model registered: a new request must reach model validation.
        class Missing(Exception):
            def __init__(self, **kwargs): pass
        self.namespace.update(HTTPException=Missing, status=SimpleNamespace(HTTP_404_NOT_FOUND=404),
                              ERROR_MESSAGES=SimpleNamespace(MODEL_NOT_FOUND=lambda: 'missing'))
        with self.assertRaises(Missing):
            await self.generate(other, {'type': 'retrieval', 'model': 'fixture'}, object())


if __name__ == '__main__':
    unittest.main()
