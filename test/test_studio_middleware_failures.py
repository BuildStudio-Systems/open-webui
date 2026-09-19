"""Offline ASGI checks: outages fail closed without masquerading as revoked sessions."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.fixture
def middleware(monkeypatch):
    studio = SimpleNamespace(ENABLED=True)
    package = ModuleType('open_webui')
    package.studio_identity = studio
    monkeypatch.setitem(sys.modules, 'open_webui', package)
    path = Path(__file__).parents[1] / 'backend/open_webui/there_studio.py'
    spec = importlib.util.spec_from_file_location('studio_middleware_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sleep = asyncio.sleep
    async def next_turn(_):
        await sleep(0)
    monkeypatch.setattr(module.asyncio, 'sleep', next_turn)
    return module, studio


def scope(path='/api/v1/chats/', method='GET'):
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'path': path, 'method': method, 'scheme': 'https', 'query_string': b'',
            'headers': [(b'origin', b'https://buildstudio-there.com')],
            'client': ('192.0.2.10', 10000), 'server': ('test', 443)}


@pytest.mark.parametrize('status', [401, 403, 503])
def test_watchdog_preserves_status_and_cancels_pending_work(middleware, status):
    module, studio = middleware
    request_id = 'a' * 32
    error = HTTPException(status, 'Authentication temporarily unavailable.' if status == 503 else 'Access denied.', headers={'X-Studio-Request-ID': request_id})
    async def exercise():
        messages = []
        stopped = asyncio.Event()
        async def app(*_):
            try: await asyncio.Event().wait()
            finally: stopped.set()
        async def check(_): raise error
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message): messages.append(message)
        studio.check = check
        await asyncio.wait_for(module.StudioMiddleware(app).protected_http(scope(), receive, send, 'bs1_fixture'), 1)
        assert stopped.is_set()
        start = messages[0]
        assert start['status'] == status
        assert dict(start['headers'])[b'cache-control'] == b'no-store'
        assert dict(start['headers'])[b'x-studio-request-id'] == request_id.encode()
        assert json.loads(messages[1]['body']) == {'detail': error.detail}
    asyncio.run(exercise())


def test_already_started_stream_is_stopped_without_second_response(middleware):
    module, studio = middleware
    async def exercise():
        messages = []
        started = asyncio.Event()
        async def app(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            started.set()
            await asyncio.Event().wait()
        async def check(_):
            await started.wait()
            raise HTTPException(503, 'Unavailable')
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(message): messages.append(message)
        studio.check = check
        await asyncio.wait_for(module.StudioMiddleware(app).protected_http(scope(), receive, send, 'bs1_fixture'), 1)
        assert len([m for m in messages if m['type'] == 'http.response.start']) == 1
        assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
    asyncio.run(exercise())


def test_signin_503_keeps_safe_diagnostic_header(middleware):
    module, studio = middleware
    async def exercise():
        messages = []
        async def app(*_): raise AssertionError('Sign-in must use central identity')
        async def call(*_): raise HTTPException(503, 'Unavailable', headers={'X-Studio-Request-ID': 'b'*32, 'Set-Cookie': 'PRIVATE'})
        async def receive(): return {'type': 'http.request', 'body': b'{"email":"fixture","password":"PRIVATE"}', 'more_body': False}
        async def send(message): messages.append(message)
        studio.call = call
        await module.StudioMiddleware(app)(scope('/api/v1/auths/signin', 'POST'), receive, send)
        assert messages[0]['status'] == 503
        assert dict(messages[0]['headers'])[b'x-studio-request-id'] == b'b'*32
        assert b'set-cookie' not in dict(messages[0]['headers'])
        assert 'PRIVATE' not in repr(messages)
    asyncio.run(exercise())


@pytest.mark.parametrize('request_id', ['PRIVATE', 'a'*32+'\r\nInjected: yes', None])
def test_error_response_drops_untrusted_ids(middleware, request_id):
    module, _ = middleware
    response = module.identity_error_response(HTTPException(503, 'Unavailable', headers={'X-Studio-Request-ID': request_id}))
    assert 'x-studio-request-id' not in response.headers
