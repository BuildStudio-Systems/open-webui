"""Regression tests for payload-free WeChat administrator access auditing."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse


@pytest.fixture(scope='module')
def audit_module(tmp_path_factory):
    environment = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp('audit-metadata-only')
    for key, value in {
        'DATA_DIR': str(root / 'data'),
        'STATIC_DIR': str(root / 'static'),
        'FRONTEND_BUILD_DIR': str(root / 'no-build'),
        'DATABASE_URL': f"sqlite:///{root / 'openwebui.sqlite'}",
        'ENABLE_DB_MIGRATIONS': 'false',
        'OFFLINE_MODE': 'true',
        'WEBUI_SECRET_KEY': 'audit-metadata-only-isolated-test-secret',
    }.items():
        environment.setenv(key, value)

    from open_webui.utils import audit

    yield SimpleNamespace(module=audit)
    environment.undo()


async def _exercise(
    audit_module,
    monkeypatch,
    *,
    method: str,
    path: str,
    request_body: str = '',
    response_body: str,
    audit_get_requests: bool = True,
    excluded_paths: list[str] | None = None,
    included_paths: list[str] | None = None,
    headers: dict[str, str] | None = None,
    trusted_no_store_token: str | None = None,
):
    audit = audit_module.module

    async def endpoint(scope, receive, send):
        request = Request(scope, receive=receive)
        received = (await request.body()).decode('utf-8')
        response = JSONResponse(
            {'received': received, 'response': response_body},
            status_code=200,
        )
        await response(scope, receive, send)

    middleware = audit.AuditLoggingMiddleware(
        endpoint,
        audit_level=audit.AuditLevel.REQUEST_RESPONSE,
        audit_get_requests=audit_get_requests,
        excluded_paths=excluded_paths,
        included_paths=included_paths,
    )
    if trusted_no_store_token is not None:
        middleware._no_store_token_hash = hashlib.sha256(
            trusted_no_store_token.encode('utf-8')
        ).hexdigest()
    entries = []

    async def no_resolved_user(_request):
        return None

    monkeypatch.setattr(audit, 'AUDIT_LOG_LEVEL', 'REQUEST_RESPONSE')
    monkeypatch.setattr(middleware, '_get_authenticated_user', no_resolved_user)
    monkeypatch.setattr(
        middleware.audit_logger,
        'write',
        lambda entry, **_kwargs: entries.append(entry),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware),
        base_url='http://there.test',
    ) as client:
        response = await client.request(
            method,
            path,
            content=request_body,
            headers={
                'Authorization': 'Bearer synthetic-admin-token',
                **(headers or {}),
            },
        )

    assert response.status_code == 200
    assert len(entries) == 1
    return entries[0]


def test_wechat_admin_route_forces_metadata_only_and_removes_query(audit_module, monkeypatch):
    private_filter = 'private-filter-must-not-be-audited'
    private_response = 'private-chat-and-source-must-not-be-audited'

    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='GET',
            path=('/api/v1/there/admin/wechat/chats' f'?query={private_filter}&cursor=private-cursor'),
            response_body=private_response,
            audit_get_requests=False,
            included_paths=['ordinary-only'],
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.METADATA.value
    assert entry.request_uri == '/api/v1/there/admin/wechat/chats'
    assert entry.request_object is None
    assert entry.response_object is None
    assert entry.response_status_code == 200
    rendered = repr(entry)
    assert private_filter not in rendered
    assert private_response not in rendered
    assert 'private-cursor' not in rendered


def test_request_scoped_no_store_never_copies_chat_payloads_into_audit(
    audit_module,
    monkeypatch,
):
    private_question = 'private Mini Program question'
    private_answer = 'private Mini Program answer'
    private_query = 'private query parameter'

    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='POST',
            path=f'/api/v1/chat/completions?trace={private_query}',
            request_body=private_question,
            response_body=private_answer,
            included_paths=['ordinary-only'],
            headers={'X-BuildStudio-No-Store': 'true'},
            trusted_no_store_token='synthetic-admin-token',
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.METADATA.value
    assert entry.request_uri == '/api/v1/chat/completions'
    assert entry.request_object is None
    assert entry.response_object is None
    assert entry.response_status_code == 200
    rendered = repr(entry)
    assert private_question not in rendered
    assert private_answer not in rendered
    assert private_query not in rendered


def test_wechat_admin_audit_readiness_requires_enabled_durable_file_sink(
    audit_module,
    monkeypatch,
):
    audit = audit_module.module
    from open_webui.utils import logger as logger_module

    monkeypatch.setattr(logger_module, 'audit_file_sink_is_ready', lambda: True)

    monkeypatch.setattr(audit, 'AUDIT_LOG_LEVEL', 'NONE')
    monkeypatch.setattr(audit, 'ENABLE_AUDIT_LOGS_FILE', True)
    assert not audit.wechat_admin_audit_is_ready()

    monkeypatch.setattr(audit, 'AUDIT_LOG_LEVEL', 'NOT_A_LEVEL')
    assert not audit.wechat_admin_audit_is_ready()

    monkeypatch.setattr(audit, 'AUDIT_LOG_LEVEL', 'METADATA')
    monkeypatch.setattr(audit, 'ENABLE_AUDIT_LOGS_FILE', False)
    assert not audit.wechat_admin_audit_is_ready()

    monkeypatch.setattr(audit, 'ENABLE_AUDIT_LOGS_FILE', True)
    monkeypatch.setattr(logger_module, 'audit_file_sink_is_ready', lambda: False)
    assert not audit.wechat_admin_audit_is_ready()

    monkeypatch.setattr(logger_module, 'audit_file_sink_is_ready', lambda: True)
    assert audit.wechat_admin_audit_is_ready()


def test_audit_file_sink_readiness_checks_active_handler_and_writable_path(
    tmp_path,
    monkeypatch,
):
    from open_webui.utils import logger as logger_module

    audit_file = tmp_path / 'audit.log'
    audit_file.write_bytes(b'')
    monkeypatch.setattr(logger_module, 'AUDIT_LOGS_FILE_PATH', str(audit_file))

    monkeypatch.setattr(logger_module, '_AUDIT_FILE_SINK_ID', None)
    assert not logger_module.audit_file_sink_is_ready()

    monkeypatch.setattr(logger_module, '_AUDIT_FILE_SINK_ID', 123)
    assert logger_module.audit_file_sink_is_ready()

    class UnwritablePath:
        def is_file(self):
            return True

        def open(self, _mode):
            raise PermissionError('read-only audit filesystem')

    monkeypatch.setattr(logger_module, 'Path', lambda _path: UnwritablePath())
    assert not logger_module.audit_file_sink_is_ready()


def test_start_logger_configures_audit_file_errors_to_propagate(
    audit_module,
    tmp_path,
    monkeypatch,
):
    from open_webui.utils import logger as logger_module

    add_calls = []

    class FakeLoguruLogger:
        def remove(self):
            return None

        def add(self, *args, **kwargs):
            add_calls.append((args, kwargs))
            return len(add_calls)

        def info(self, _message):
            return None

    class FakeStdlibLogger:
        handlers = []

        def setLevel(self, _level):
            return None

    monkeypatch.setattr(logger_module, 'logger', FakeLoguruLogger())
    monkeypatch.setattr(logger_module, 'LOG_FORMAT', 'text')
    monkeypatch.setattr(logger_module, 'AUDIT_LOG_LEVEL', 'METADATA')
    monkeypatch.setattr(logger_module, 'ENABLE_AUDIT_LOGS_FILE', True)
    monkeypatch.setattr(logger_module, 'AUDIT_LOGS_FILE_PATH', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(logger_module, '_AUDIT_FILE_SINK_ID', None)
    monkeypatch.setattr(
        logger_module,
        'logging',
        SimpleNamespace(
            basicConfig=lambda **_kwargs: None,
            getLogger=lambda _name=None: FakeStdlibLogger(),
        ),
    )

    logger_module.start_logger()

    assert len(add_calls) == 2
    assert add_calls[1][1]['catch'] is False
    assert logger_module._AUDIT_FILE_SINK_ID == 2


def test_ordinary_route_keeps_request_response_auditing(audit_module, monkeypatch):
    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='GET',
            path='/api/v1/ordinary?filter=ordinary-filter',
            response_body='ordinary-response-body',
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.REQUEST_RESPONSE.value
    assert entry.request_uri == 'http://there.test/api/v1/ordinary?filter=ordinary-filter'
    assert entry.request_object == ''
    assert 'ordinary-response-body' in entry.response_object
    assert entry.response_status_code == 200


def test_forged_no_store_header_does_not_downgrade_an_ordinary_route(
    audit_module,
    monkeypatch,
):
    private_request = 'ordinary request must remain auditable'
    private_response = 'ordinary response must remain auditable'

    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='POST',
            path='/api/v1/ordinary',
            request_body=private_request,
            response_body=private_response,
            headers={'X-BuildStudio-No-Store': 'true'},
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.REQUEST_RESPONSE.value
    assert entry.request_object == private_request
    assert private_response in entry.response_object


def test_chat_completion_without_no_store_marker_keeps_normal_auditing(
    audit_module,
    monkeypatch,
):
    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='POST',
            path='/api/v1/chat/completions',
            request_body='ordinary browser chat',
            response_body='ordinary browser response',
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.REQUEST_RESPONSE.value
    assert entry.request_object == 'ordinary browser chat'
    assert 'ordinary browser response' in entry.response_object


def test_forged_no_store_header_does_not_bypass_always_log_endpoint(
    audit_module,
    monkeypatch,
):
    entry = asyncio.run(
        _exercise(
            audit_module,
            monkeypatch,
            method='POST',
            path='/api/v1/auths/signin',
            request_body='signin payload',
            response_body='signin response',
            audit_get_requests=False,
            included_paths=['ordinary-only'],
            headers={'X-BuildStudio-No-Store': 'true'},
        )
    )

    assert entry.audit_level == audit_module.module.AuditLevel.REQUEST_RESPONSE.value
    assert entry.request_object == 'signin payload'
    assert 'signin response' in entry.response_object


@pytest.mark.parametrize(
    ('trusted_hash', 'authorization'),
    [
        (None, 'Bearer synthetic-admin-token'),
        (hashlib.sha256(b'another-token').hexdigest(), 'Bearer synthetic-admin-token'),
        (hashlib.sha256(b'synthetic-admin-token').hexdigest(), None),
        (hashlib.sha256(b'synthetic-admin-token').hexdigest(), 'Basic synthetic-admin-token'),
    ],
)
def test_untrusted_no_store_chat_request_fails_closed_before_the_endpoint(
    audit_module,
    monkeypatch,
    trusted_hash,
    authorization,
):
    audit = audit_module.module
    endpoint_called = False
    entries = []

    async def endpoint(scope, receive, send):
        nonlocal endpoint_called
        endpoint_called = True
        response = JSONResponse({'private': 'must not be returned'})
        await response(scope, receive, send)

    middleware = audit.AuditLoggingMiddleware(
        endpoint,
        audit_level=audit.AuditLevel.REQUEST_RESPONSE,
    )
    middleware._no_store_token_hash = trusted_hash
    monkeypatch.setattr(
        middleware.audit_logger,
        'write',
        lambda entry, **_kwargs: entries.append(entry),
    )

    async def exercise():
        headers = {'X-BuildStudio-No-Store': 'true'}
        if authorization is not None:
            headers['Authorization'] = authorization
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=middleware),
            base_url='http://there.test',
        ) as client:
            return await client.post(
                '/api/v1/chat/completions',
                content='private question',
                headers=headers,
            )

    response = asyncio.run(exercise())

    assert response.status_code == 503
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert response.json() == {
        'detail': 'Trusted no-store request configuration is unavailable'
    }
    assert not endpoint_called
    assert entries == []


def test_metadata_only_prefix_requires_a_path_boundary(audit_module):
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'scheme': 'https',
            'path': '/api/v1/there/admin/wechatty',
            'raw_path': b'/api/v1/there/admin/wechatty',
            'query_string': b'',
            'headers': [],
            'server': ('there.test', 443),
            'client': ('127.0.0.1', 1),
        }
    )

    middleware = audit_module.module.AuditLoggingMiddleware(
        lambda *_args: None,
        audit_level=audit_module.module.AuditLevel.METADATA,
    )

    assert not middleware._requires_metadata_only_audit(request)


def test_wechat_admin_audit_accepts_state_backed_api_key_marker(audit_module, monkeypatch):
    audit = audit_module.module
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'scheme': 'https',
            'path': '/api/v1/there/admin/wechat/chats',
            'raw_path': b'/api/v1/there/admin/wechat/chats',
            'query_string': b'',
            'headers': [],
            'server': ('there.test', 443),
            'client': ('127.0.0.1', 1),
            'state': {'token': object()},
        }
    )
    middleware = audit.AuditLoggingMiddleware(
        lambda *_args: None,
        audit_level=audit.AuditLevel.METADATA,
        audit_get_requests=False,
        included_paths=['ordinary-only'],
    )
    monkeypatch.setattr(audit, 'AUDIT_LOG_LEVEL', 'METADATA')

    assert not middleware._should_skip_auditing(request)
