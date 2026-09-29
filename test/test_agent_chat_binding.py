"""Standalone behavior tests; no app import, database, or credentials required."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock
from unittest.mock import patch
import sys
import tempfile
import json


BACKEND = Path(__file__).parents[1] / 'backend/open_webui'
sys.path.insert(0, str(BACKEND.parent))
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
    'x-buildstudio-device-capability': 'forged-capability',
    'Authorization': 'Bearer synthetic-test-key',
    'X-Other': 'preserved',
}


class BindingTests(IsolatedAsyncioTestCase):
    async def test_device_proof_uses_owner_allowlist_after_saved_chat_check(self):
        from open_webui.utils import device_control
        with patch.object(device_control, 'capability', new=AsyncMock(return_value='broker-proof')) as issuer:
            result = await binding.bind_agent_request_headers(SPOOFED, URL, ADMIN, {'chat_id':CHAT}, AsyncMock(return_value=True), session_token='bs1_synthetic_owner_session')
            self.assertEqual(result[device_control.HEADER], 'broker-proof')
            issuer.assert_awaited_once_with(ADMIN, 'agent', chat=CHAT, session_token='bs1_synthetic_owner_session')
            self.assertNotIn('bs1_synthetic_owner_session', result.values())
            issuer.reset_mock()
            with self.assertRaises(binding.AgentChatBindingError):
                await binding.bind_agent_request_headers(SPOOFED, URL, ADMIN, {'chat_id':CHAT}, AsyncMock(return_value=False), session_token='bs1_synthetic_owner_session')
            issuer.assert_not_awaited()

    async def test_optional_broken_device_configuration_keeps_chat_without_device_access(self):
        with patch('open_webui.utils.device_control.capability', side_effect=ValueError('synthetic invalid config')):
            result = await binding.bind_agent_request_headers(SPOOFED, URL, ADMIN, {'chat_id':CHAT}, AsyncMock(return_value=True))
        self.assertEqual(result[binding.AGENT_CHAT_HEADER], CHAT)
        self.assertFalse(any(k.lower() == 'x-buildstudio-device-capability' for k in result))

    async def test_auxiliary_tasks_never_enter_agent_even_without_saved_chat(self):
        for task in ('title_generation', 'TASKS.TITLE_GENERATION', 'query_generation',
                     'tags_generation', 'follow_up_generation', 'future_task'):
            for chat_id in (CHAT, None, 'temporary:socket'):
                with self.subTest(task=task, chat_id=chat_id):
                    ownership = AsyncMock(return_value=True)
                    with self.assertRaises(binding.AgentChatBindingError):
                        await binding.bind_agent_request_headers(
                            SPOOFED, URL, ADMIN,
                            {'task': task, 'chat_id': chat_id}, ownership,
                        )
                    ownership.assert_not_awaited()

    async def test_auxiliary_guard_does_not_affect_inference_provider(self):
        ownership = AsyncMock()
        result = await binding.bind_agent_request_headers(
            SPOOFED, 'http://127.0.0.1:8000/v1', ADMIN,
            {'task': 'title_generation', 'chat_id': CHAT}, ownership,
        )
        self.assertIs(result, SPOOFED)
        ownership.assert_not_awaited()

    async def test_regular_chat_with_empty_task_keeps_binding(self):
        for task in (None, ''):
            ownership = AsyncMock(return_value=True)
            result = await binding.bind_agent_request_headers(
                SPOOFED, URL, ADMIN, {'task': task, 'chat_id': CHAT}, ownership,
            )
            self.assertEqual(result[binding.AGENT_CHAT_HEADER], CHAT)
            ownership.assert_awaited_once_with(CHAT, ADMIN.id)

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


class TaskRoutingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.urls = ['http://127.0.0.1:8000/v1', URL, 'https://provider.example/v1']
        self.models = {
            'inference': {'owned_by': 'openai', 'urlIdx': 0},
            'agent': {'owned_by': 'openai', 'urlIdx': 1},
            'agent-preset': {'owned_by': 'openai', 'preset': True,
                             'info': {'base_model_id': 'agent'}},
            'external': {'owned_by': 'openai', 'urlIdx': 2},
        }

    def test_agent_and_presets_resolve_to_registered_inference(self):
        for model in ('agent', 'agent-preset'):
            self.assertEqual(binding.task_inference_fallback(model, self.models, self.urls), 'inference')
        self.assertEqual(self.models['agent-preset']['info']['base_model_id'], 'agent')

    def test_explicit_non_agent_models_are_preserved(self):
        for model in ('inference', 'external', 'missing'):
            self.assertEqual(binding.task_inference_fallback(model, self.models, self.urls), model)

    def test_no_arbitrary_fallback_for_ambiguous_or_missing_inference(self):
        del self.models['inference']
        self.assertEqual(binding.task_inference_fallback('agent', self.models, self.urls), 'agent')
        for name in ('inference-a', 'inference-b'):
            self.models[name] = {'owned_by': 'openai', 'urlIdx': 0}
        self.assertEqual(binding.task_inference_fallback('agent', self.models, self.urls), 'agent')

    def test_presets_do_not_duplicate_one_base_inference_candidate(self):
        self.models['named-inference'] = {'owned_by': 'openai', 'preset': True,
                                         'info': {'base_model_id': 'inference'}}
        self.assertEqual(binding.task_inference_fallback('agent', self.models, self.urls), 'inference')

    def test_lookalike_endpoints_and_pipe_models_never_selected(self):
        for url in ('http://external.example:8000/v1', 'http://user@localhost:8000/v1',
                    'http://localhost:8000/v1?other=1', 'http://localhost:8000/else',
                    'http://localhost:invalid/v1'):
            with self.subTest(url=url):
                self.assertEqual(binding.task_inference_fallback('agent', self.models, [url, URL]), 'agent')
        self.models['inference']['pipe'] = {'type': 'pipe'}
        self.assertEqual(binding.task_inference_fallback('agent', self.models, self.urls), 'agent')

    def test_bad_indexes_cycles_and_missing_config_do_not_guess(self):
        for index in (-1, True, '1', 999):
            self.models['agent']['urlIdx'] = index
            self.assertEqual(binding.task_inference_fallback('agent', self.models, self.urls), 'agent')
        self.models['agent']['info'] = {'base_model_id': 'agent-preset'}
        self.assertEqual(binding.task_inference_fallback('agent-preset', self.models, self.urls), 'agent-preset')
        self.assertEqual(binding.task_inference_fallback('agent', self.models, None), 'agent')

    async def test_missing_fallback_is_still_rejected_at_final_agent_boundary(self):
        del self.models['inference']
        chosen = binding.task_inference_fallback('agent', self.models, self.urls)
        ownership = AsyncMock()
        with self.assertRaises(binding.AgentChatBindingError):
            await binding.bind_agent_request_headers(
                {}, self.urls[self.models[chosen]['urlIdx']], ADMIN,
                {'task': 'title_generation', 'chat_id': CHAT}, ownership,
            )
        ownership.assert_not_awaited()

    async def test_actual_task_config_resolver_routes_agent_and_keeps_parameters(self):
        # Execute the actual resolver bodies with only the configuration I/O
        # injected; do not import the application's database/startup side effects.
        namespace = {'task_inference_fallback': binding.task_inference_fallback}
        for path, name in (('utils/task.py', 'get_task_model_id'),
                           ('routers/tasks.py', 'get_task_model_generation_config')):
            tree = ast.parse((BACKEND / path).read_text(encoding='utf-8'))
            function = next(node for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name == name)
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<task resolver>', 'exec'), namespace)
        for configured, expected in ((None, 'inference'), ('agent-preset', 'inference'),
                                     ('external', 'external')):
            with self.subTest(configured=configured):
                get_many = AsyncMock(return_value={
                    'task.model.external': configured,
                    'task.model.params': {'temperature': 0, 'max_tokens': 80, 'unused': None},
                    'openai.api_base_urls': self.urls,
                })
                namespace['Config'] = SimpleNamespace(get_many=get_many)
                selected, params = await namespace['get_task_model_generation_config']('agent-preset', self.models)
                self.assertEqual(selected, expected)
                self.assertEqual(params, {'temperature': 0, 'max_tokens': 80})
                self.assertIn('openai.api_base_urls', get_many.await_args.args)


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

    async def test_actual_router_rejects_auxiliary_agent_before_upstream(self):
        with self.assertRaises(self.error_type) as caught:
            await self.handler(
                self.request, URL, config={},
                metadata={'task': 'TASKS.TITLE_GENERATION', 'chat_id': CHAT}, user=ADMIN,
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.ownership.assert_not_awaited()

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
