"""Standalone behavior tests; no app import, database, or credentials required."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock


BACKEND = Path(__file__).parents[1] / 'backend/open_webui'
spec = importlib.util.spec_from_file_location(
    'agent_chat_binding', BACKEND / 'utils/agent_file_delivery.py'
)
binding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binding)
CHAT = 'c66d52ba-ae53-44c5-8ed0-56b19992cab0'
URL = 'http://127.0.0.1:8642/v1'
ADMIN = SimpleNamespace(id='admin-a', role='admin')
SPOOFED = {
    'x-buildstudio-user-id': 'another-owner',
    'X-BUILDSTUDIO-CHAT-ID': 'another-chat',
    'x-Hermes-Session-ID': 'private-session',
    'X-HERMES-SESSION-KEY': 'private-key',
    'Authorization': 'Bearer synthetic-test-key',
    'X-Other': 'preserved',
}


class BindingTests(IsolatedAsyncioTestCase):
    async def test_owned_saved_chat_uses_authenticated_principal_and_keeps_input(self):
        ownership = AsyncMock(return_value=True)
        metadata = {'chat_id': CHAT, 'user_id': 'spoofed-owner', 'session_id': 'untrusted'}
        original = dict(SPOOFED)
        result = await binding.bind_agent_request_headers(SPOOFED, URL, ADMIN, metadata, ownership)
        ownership.assert_awaited_once_with(CHAT, ADMIN.id)
        self.assertEqual(result, {
            'Authorization': SPOOFED['Authorization'], 'X-Other': 'preserved',
            binding.FILE_OWNER_HEADER: ADMIN.id, binding.AGENT_CHAT_HEADER: CHAT,
        })
        self.assertEqual(SPOOFED, original)
        self.assertEqual(metadata['user_id'], 'spoofed-owner')

    async def test_foreign_missing_or_deleted_saved_chat_denied(self):
        for found in (False, None):
            with self.subTest(found=found):
                ownership = AsyncMock(return_value=found)
                with self.assertRaises(binding.AgentChatBindingError):
                    await binding.bind_agent_request_headers({}, URL, ADMIN, {'chat_id': CHAT}, ownership)
                ownership.assert_awaited_once_with(CHAT, ADMIN.id)

    async def test_every_request_rechecks_ownership(self):
        ownership = AsyncMock(side_effect=[True, False])
        await binding.bind_agent_request_headers({}, URL, ADMIN, {'chat_id': CHAT}, ownership)
        with self.assertRaises(binding.AgentChatBindingError):
            await binding.bind_agent_request_headers({}, URL, ADMIN, {'chat_id': CHAT}, ownership)
        self.assertEqual(ownership.await_count, 2)

    async def test_database_failure_does_not_fall_back_to_legacy_session(self):
        ownership = AsyncMock(side_effect=RuntimeError('synthetic database unavailable'))
        with self.assertRaises(RuntimeError):
            await binding.bind_agent_request_headers({}, URL, ADMIN, {'chat_id': CHAT}, ownership)

    async def test_invalid_and_noncanonical_saved_ids_rejected_before_database(self):
        for chat_id in (CHAT.upper(), CHAT.replace('-', ''), f'{{{CHAT}}}', f' {CHAT}',
                        'not-a-chat', 1, [], {}, 'x' * 1000):
            with self.subTest(chat_id=repr(chat_id)):
                ownership = AsyncMock(return_value=True)
                with self.assertRaises(binding.AgentChatBindingError):
                    await binding.bind_agent_request_headers({}, URL, ADMIN, {'chat_id': chat_id}, ownership)
                ownership.assert_not_awaited()

    async def test_no_saved_chat_omits_binding_and_always_removes_continuation_headers(self):
        for metadata in (None, {}, {'chat_id': ''}, {'chat_id': None},
                         {'chat_id': 'temporary:socket'}, {'chat_id': 'local:socket'},
                         {'chat_id': 'channel:123'}):
            with self.subTest(metadata=metadata):
                ownership = AsyncMock()
                result = await binding.bind_agent_request_headers(SPOOFED, URL, ADMIN, metadata, ownership)
                self.assertEqual(result, {
                    'Authorization': SPOOFED['Authorization'], 'X-Other': 'preserved',
                    binding.FILE_OWNER_HEADER: ADMIN.id,
                })
                ownership.assert_not_awaited()

    async def test_non_admin_and_no_user_cannot_forward_any_reserved_header(self):
        for user in (None, SimpleNamespace(id='customer', role='user'),
                     SimpleNamespace(id='pending', role='pending'),
                     SimpleNamespace(id='invalid owner', role='admin')):
            with self.subTest(user=user):
                ownership = AsyncMock()
                result = await binding.bind_agent_request_headers(SPOOFED, URL, user, {'chat_id': CHAT}, ownership)
                self.assertEqual(result, {'Authorization': SPOOFED['Authorization'], 'X-Other': 'preserved'})
                ownership.assert_not_awaited()

    async def test_other_providers_and_lookalike_urls_are_untouched(self):
        for url in ('https://provider.example/v1', 'http://localhost:8000/v1',
                    'http://localhost:8642/v1/other', 'http://user@localhost:8642/v1',
                    'http://localhost:8642/v1?target=remote'):
            with self.subTest(url=url):
                ownership = AsyncMock()
                result = await binding.bind_agent_request_headers(SPOOFED, url, ADMIN, {'chat_id': CHAT}, ownership)
                self.assertIs(result, SPOOFED)
                ownership.assert_not_awaited()

    async def test_invalid_metadata_fails_closed(self):
        for metadata in ('not a mapping', [], 3):
            with self.subTest(metadata=metadata):
                with self.assertRaises(binding.AgentChatBindingError):
                    await binding.bind_agent_request_headers({}, URL, ADMIN, metadata, AsyncMock())


class RouterHeaderTests(IsolatedAsyncioTestCase):
    """Execute the actual router function with only its I/O dependencies injected."""

    def setUp(self):
        tree = ast.parse((BACKEND / 'routers/openai.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == 'get_headers_and_cookies')
        # Preserve function behavior without importing the entire Web application.
        self.ownership = AsyncMock(return_value=True)
        self.expand = AsyncMock(side_effect=lambda headers, *args, **kwargs: dict(headers))

        class HTTPException(Exception):
            def __init__(self, status_code, detail):
                self.status_code, self.detail = status_code, detail

        self.error_type = HTTPException
        namespace = {
            'Request': object, 'UserModel': object, 'ENABLE_FORWARD_USER_INFO_HEADERS': False,
            'get_custom_headers': self.expand, 'HTTPException': HTTPException,
            'AgentChatBindingError': binding.AgentChatBindingError,
            'bind_agent_request_headers': binding.bind_agent_request_headers,
            'Chats': SimpleNamespace(is_chat_owner=self.ownership),
        }
        module = ast.Module(body=[function], type_ignores=[])
        exec(compile(module, '<actual OpenAI header handler>', 'exec'), namespace)
        self.handler = namespace['get_headers_and_cookies']
        self.request = SimpleNamespace(cookies={})

    async def test_actual_router_binds_after_custom_expansion_and_preserves_bearer(self):
        headers, cookies = await self.handler(
            self.request, URL, key='synthetic-service-key',
            config={'headers': {k: v for k, v in SPOOFED.items() if k != 'Authorization'}},
            metadata={'chat_id': CHAT, 'user_id': 'spoofed'}, user=ADMIN,
        )
        self.assertEqual(headers[binding.FILE_OWNER_HEADER], ADMIN.id)
        self.assertEqual(headers[binding.AGENT_CHAT_HEADER], CHAT)
        self.assertEqual(headers['Authorization'], 'Bearer synthetic-service-key')
        self.assertFalse(any(key.lower().startswith('x-hermes-session-') for key in headers))
        self.assertEqual(cookies, {})
        self.expand.assert_awaited_once()
        self.ownership.assert_awaited_once_with(CHAT, ADMIN.id)

    async def test_actual_router_denies_foreign_chat_with_generic_403(self):
        self.ownership.return_value = False
        with self.assertRaises(self.error_type) as caught:
            await self.handler(self.request, URL, config={}, metadata={'chat_id': CHAT}, user=ADMIN)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, 'Agent chat is unavailable')

    async def test_actual_router_does_not_change_external_session_headers(self):
        headers, _ = await self.handler(
            self.request, 'https://provider.example/v1', config={'headers': SPOOFED},
            metadata={'chat_id': CHAT}, user=ADMIN,
        )
        for key, value in SPOOFED.items():
            self.assertEqual(headers[key], value)
        self.ownership.assert_not_awaited()


if __name__ == '__main__':
    main()
