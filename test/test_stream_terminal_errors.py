"""Execute the actual stream error gate without loading application state."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).parents[1] / 'backend/open_webui/utils/middleware.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
HELPER = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == '_chat_completion_stream_error')
# Extract the real error/persistence/event branch, not a copied implementation.
ERROR_GATE = next(n for n in ast.walk(TREE) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == 'error'
                  and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                          and c.func.id == '_safe_streaming_provider_error' for c in ast.walk(n)))


class TerminalErrorTests(unittest.IsolatedAsyncioTestCase):
    async def run_gate(self, data, save=True, db_fails=False):
        chats = SimpleNamespace(upsert_message_to_chat_by_id_and_message_id=AsyncMock())
        if db_fails:
            chats.upsert_message_to_chat_by_id_and_message_id.side_effect = RuntimeError('synthetic')
        events = AsyncMock()
        ns = {'Any': Any, 'Chats': chats, 'event_emitter': events,
              'save_to_chat': save, 'metadata': {'chat_id': 'synthetic-chat', 'message_id': 'synthetic-answer'},
              '_safe_streaming_provider_error': lambda error: 'Safe connection error'}
        setup = ast.parse('error = _chat_completion_stream_error(data)')
        wrapper = ast.parse('async def execute(data):\n pass').body[0]
        wrapper.body = setup.body + [ERROR_GATE]
        module = ast.fix_missing_locations(ast.Module(body=[HELPER, wrapper], type_ignores=[]))
        exec(compile(module, str(SOURCE), 'exec'), ns)
        await ns['execute'](data)
        return chats.upsert_message_to_chat_by_id_and_message_id, events

    async def test_errors_with_choices_are_saved_and_emitted(self):
        for reason in ['length', 'error', 'content_filter']:
            with self.subTest(reason=reason):
                save, emit = await self.run_gate({'choices': [{'delta': {}, 'finish_reason': reason}]})
                save.assert_awaited_once_with('synthetic-chat', 'synthetic-answer', {'error': {'content': 'Safe connection error'}})
                emit.assert_awaited_once_with({'type': 'chat:completion', 'data': {'error': 'Safe connection error'}})

    async def test_raw_provider_error_is_never_forwarded(self):
        for choices in [[], [{'finish_reason': 'stop', 'delta': {'content': 'retained text'}}]]:
            with self.subTest(choices=choices):
                data = {'error': {'message': 'synthetic-secret'}, 'choices': choices}
                save, emit = await self.run_gate(data)
                self.assertNotIn('synthetic-secret', str(emit.await_args))
                save.assert_awaited_once()
                self.assertEqual(data['choices'], choices)

    async def test_normal_chunks_tool_calls_and_stop_are_not_errors(self):
        for reason in [None, 'stop', 'tool_calls', 'function_call']:
            with self.subTest(reason=reason):
                save, emit = await self.run_gate({'choices': [{'delta': {}, 'finish_reason': reason}]})
                save.assert_not_awaited()
                emit.assert_not_awaited()

    async def test_temporary_chat_does_not_write(self):
        save, emit = await self.run_gate({'error': 'synthetic'}, save=False)
        save.assert_not_awaited()
        emit.assert_awaited_once()

    async def test_failed_persistence_still_notifies_client(self):
        _, emit = await self.run_gate({'error': 'synthetic'}, db_fails=True)
        emit.assert_awaited_once()

    def test_gate_is_before_empty_choices_guard(self):
        branches = [n for n in ast.walk(TREE) if isinstance(n, ast.If) and ast.unparse(n.test) == 'not choices']
        self.assertTrue(any(0 < n.lineno - ERROR_GATE.lineno < 35 for n in branches))


if __name__ == '__main__':
    unittest.main()
