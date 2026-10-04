"""Exercise the actual middleware function with bounded synthetic I/O."""
import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/open_webui/utils/middleware.py'


class QueryDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, generate, retrieve, *, full=False):
        tree = ast.parse(SOURCE.read_text('utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'chat_completion_files_handler')
        node.decorator_list = []
        events = []
        deadlines = []

        async def bounded(awaitable, *, timeout):
            deadlines.append(timeout)
            # Keep the real cancellation behavior without an eight-second test.
            return await asyncio.wait_for(awaitable, timeout=.03)

        async def config(*keys):
            return {'rag.full_context': full, 'rag.top_k': 3}

        async def emit(event):
            events.append(event)

        ns = {'asyncio': SimpleNamespace(wait_for=bounded), 'Config': SimpleNamespace(get_many=config),
              'generate_queries': generate, 'get_sources_from_items': retrieve, 'JSONCodec': json,
              'get_last_user_message': lambda messages: messages[-1]['content'], 'log': logging.getLogger(__name__)}
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), ns)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(RERANKING_FUNCTION=None)))
        user = SimpleNamespace(id='synthetic-owner')
        body = {'model': 'synthetic-agent', 'messages': [{'role': 'user', 'content': 'Original question'}],
                'metadata': {'files': [{'id': 'private-fixture', 'type': 'collection'}], 'chat_id': 'synthetic-chat'}}
        return ns['chat_completion_files_handler'](request, body, {'__event_emitter__': emit}, user), events, deadlines, user

    async def test_timeout_cancels_rewriter_and_retrieves_original_with_same_owner(self):
        cancelled = asyncio.Event()
        observed = []

        async def generate(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def retrieve(**kwargs):
            self.assertTrue(cancelled.is_set())
            observed.append(kwargs)
            return []

        call, events, deadlines, owner = self.handler(generate, retrieve)
        _, result = await call
        self.assertEqual(deadlines, [8.0])
        self.assertEqual(observed[0]['queries'], ['Original question'])
        self.assertIs(observed[0]['user'], owner)
        self.assertEqual(observed[0]['items'], [{'id': 'private-fixture', 'type': 'collection'}])
        self.assertEqual(result, {'sources': []})

    async def test_successful_rewriter_keeps_generated_queries_and_sources(self):
        observed = []
        source = {'document': ['Synthetic fact'], 'metadata': [{'source': 'fixture'}], 'source': {'id': 'fixture'}}

        async def generate(*args):
            return {'choices': [{'message': {'content': '{"queries":["rewritten query"]}'}}]}

        async def retrieve(**kwargs):
            observed.append(kwargs)
            return [source]

        call, events, deadlines, owner = self.handler(generate, retrieve)
        _, result = await call
        self.assertEqual(observed[0]['queries'], ['rewritten query'])
        self.assertEqual(result, {'sources': [source]})
        self.assertEqual(events[-1]['data']['count'], 1)

    async def test_parent_cancel_propagates_without_retrieving(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def generate(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def retrieve(**kwargs):
            self.fail('Disconnected request must not continue retrieval')

        call, _, _, _ = self.handler(generate, retrieve)
        task = asyncio.create_task(call)
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_full_context_skips_auxiliary_request(self):
        async def generate(*args):
            self.fail('Full-context requests do not need query rewriting')

        async def retrieve(**kwargs):
            self.assertTrue(kwargs['full_context'])
            self.assertEqual(kwargs['queries'], ['Original question'])
            return []

        call, _, deadlines, _ = self.handler(generate, retrieve, full=True)
        await call
        self.assertEqual(deadlines, [])


if __name__ == '__main__':
    unittest.main()
