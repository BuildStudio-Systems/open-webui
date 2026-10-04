"""ASGI response boundaries; no production identity or database needed."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def middleware(monkeypatch):
    package = ModuleType('open_webui')
    package.studio_identity = SimpleNamespace(ENABLED=True)
    monkeypatch.setitem(sys.modules, 'open_webui', package)
    spec = importlib.util.spec_from_file_location(
        'private_response_cache_under_test',
        Path(__file__).parents[1] / 'backend/open_webui/there_studio.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, package.studio_identity


def scope(path, kind='http'):
    return {'type': kind, 'path': path, 'method': 'GET', 'headers': [],
            'query_string': b'', 'scheme': 'https', 'server': ('test', 443)}


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('path', ['/api', '/api/v1/chats/', '/api/v1/images/config',
                                 '/api/v1/files/example/content', '/openai/v1/models',
                                 '/ollama/api/chat', '/cache/image/example.png'])
@pytest.mark.parametrize('status', [200, 206, 307, 304, 401, 403, 404, 500, 503])
def test_dynamic_responses_override_cacheable_headers(middleware, enabled, path, status):
    module, studio = middleware
    studio.ENABLED = enabled
    async def exercise():
        messages = []
        async def app(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': status, 'headers': [
                (b'cache-control', b'public, max-age=600'),
                (b'cache-control', b's-maxage=600'), (b'etag', b'"fixture"'),
                (b'content-range', b'bytes 0-2/3')]})
            await send({'type': 'http.response.body', 'body': b'abc'})
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(message): messages.append(message)
        await module.StudioMiddleware(app)(scope(path), receive, send)
        start = messages[0]
        assert start['status'] == status
        assert [v for k, v in start['headers'] if k == b'cache-control'] == [b'no-store']
        assert dict(start['headers'])[b'content-range'] == b'bytes 0-2/3'
        assert messages[1]['body'] == b'abc'
    asyncio.run(exercise())


def test_stream_is_delivered_before_producer_finishes(middleware):
    module, _ = middleware
    async def exercise():
        messages = []
        delivered = asyncio.Event()
        async def app(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'data: first\n\n', 'more_body': True})
            await asyncio.wait_for(delivered.wait(), 1)
            await send({'type': 'http.response.body', 'body': b'data: done\n\n', 'more_body': False})
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(message):
            messages.append(message)
            if message.get('more_body'): delivered.set()
        await module.StudioMiddleware(app)(scope('/api/chat/completions'), receive, send)
        assert len(messages) == 3
        assert dict(messages[0]['headers'])[b'cache-control'] == b'no-store'
        assert messages[-1]['more_body'] is False
    asyncio.run(exercise())


def test_websocket_is_passed_through(middleware):
    module, _ = middleware
    async def exercise():
        messages = []
        async def app(_scope, _receive, send):
            await send({'type': 'websocket.accept'})
            await send({'type': 'websocket.close', 'code': 1000})
        async def receive(): return {'type': 'websocket.connect'}
        async def send(message): messages.append(message)
        await module.StudioMiddleware(app)(scope('/api/ws', 'websocket'), receive, send)
        assert messages == [{'type': 'websocket.accept'}, {'type': 'websocket.close', 'code': 1000}]
    asyncio.run(exercise())
