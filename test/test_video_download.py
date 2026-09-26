"""Isolated video proxy tests; real ASGI FileResponse, no services or credentials."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
import httpx
import pytest
from starlette_compress import CompressMiddleware
from starlette.requests import ClientDisconnect


@pytest.fixture
def videos(monkeypatch):
    auth = ModuleType("open_webui.utils.auth")

    async def verified(request: Request):
        if not request.headers.get("x-test-user"):
            raise HTTPException(401, "Login required")
        return SimpleNamespace(id=request.headers["x-test-user"])

    auth.get_verified_user = verified
    path = Path(__file__).resolve().parents[1] / "backend/open_webui/routers/videos.py"
    spec = importlib.util.spec_from_file_location("isolated_there_videos", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, "open_webui.utils.auth", auth)
        spec.loader.exec_module(module)
    monkeypatch.setenv("VIDEO_GENERATION_API_BASE_URL", "http://coordinator.test")
    monkeypatch.setenv("VIDEO_GENERATION_API_KEY", "synthetic-video-test-key")
    return module


@pytest.fixture
def proxy(videos, monkeypatch, tmp_path):
    payload = bytes(range(256)) * 4096
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(payload)
    upstream = FastAPI()
    state = SimpleNamespace(requests=[], bytes_sent=0, clients=[])

    @upstream.get("/v1/videos/{job_id}/content")
    async def content(job_id: str, request: Request):
        state.requests.append(dict(request.headers))
        assert request.headers["authorization"] == "Bearer synthetic-video-test-key"
        if request.headers["x-openwebui-user-id"] != "owner":
            raise HTTPException(404, "Not found")
        return FileResponse(clip, media_type="video/mp4", filename="clip.mp4")

    async def counted_upstream(scope, receive, send):
        async def counted(message):
            if message["type"] == "http.response.body":
                state.bytes_sent += len(message.get("body", b""))
            await send(message)
        await upstream(scope, receive, counted)

    real_client = httpx.AsyncClient

    def upstream_client(**kwargs):
        assert kwargs["timeout"] == videos._DOWNLOAD_TIMEOUT
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["verify"] is videos._TLS_CONTEXT_NO_ENV  # Built once, not per download.
        client = real_client(transport=httpx.ASGITransport(app=counted_upstream), **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(videos.httpx, "AsyncClient", upstream_client)
    app = FastAPI()
    app.include_router(videos.router)
    app.add_middleware(CompressMiddleware)
    return SimpleNamespace(
        client=lambda: real_client(transport=httpx.ASGITransport(app=app), base_url="http://web.test"),
        state=state, payload=payload,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("range_value,expected", [
    ("bytes=2-5", bytes(range(2, 6))),
    ("bytes=-4", bytes(range(252, 256))),
])
async def test_real_file_response_range_avoids_full_download(proxy, range_value, expected):
    async with proxy.client() as client:
        response = await client.get("/jobs/clip/content", headers={"x-test-user": "owner", "range": range_value})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-length"] == "4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"].endswith(f"/{len(proxy.payload)}")
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-encoding" not in response.headers
    assert proxy.state.bytes_sent == 4
    assert proxy.state.requests[0]["range"] == range_value
    assert proxy.state.requests[0]["accept-encoding"] == "identity"
    assert all(client.is_closed for client in proxy.state.clients)


@pytest.mark.asyncio
async def test_if_range_preserved_and_stale_validator_returns_full_file(proxy):
    async with proxy.client() as client:
        initial = await client.get("/jobs/clip/content", headers={"x-test-user": "owner", "range": "bytes=0-3"})
        for validator, expected_status in [(initial.headers["etag"], 206), ('"stale"', 200)]:
            response = await client.get("/jobs/clip/content", headers={
                "x-test-user": "owner", "range": "bytes=4-7", "if-range": validator,
            })
            assert response.status_code == expected_status
            assert response.content == (proxy.payload[4:8] if expected_status == 206 else proxy.payload)
            assert proxy.state.requests[-1]["if-range"] == validator


@pytest.mark.asyncio
async def test_multipart_ranges_keep_boundary_and_exact_length(proxy):
    async with proxy.client() as client:
        response = await client.get("/jobs/clip/content", headers={
            "x-test-user": "owner", "range": "bytes=0-3,20-23",
        })
    assert response.status_code == 206
    assert response.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)
    assert proxy.payload[0:4] in response.content and proxy.payload[20:24] in response.content
    assert proxy.state.bytes_sent == len(response.content) < 1000


@pytest.mark.asyncio
async def test_unsatisfiable_range_preserves_416_and_closes_upstream(proxy):
    async with proxy.client() as client:
        response = await client.get("/jobs/clip/content", headers={
            "x-test-user": "owner", "range": f"bytes={len(proxy.payload) + 10}-",
        })
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(proxy.payload)}"
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert all(client.is_closed for client in proxy.state.clients)


@pytest.mark.asyncio
async def test_anonymous_and_foreign_owners_cannot_download_or_spoof_owner(proxy):
    async with proxy.client() as client:
        anonymous = await client.get("/jobs/clip/content")
        assert anonymous.status_code == 401
        assert not proxy.state.requests
        foreign = await client.get("/jobs/clip/content", headers={
            "x-test-user": "other", "x-openwebui-user-id": "owner", "range": "bytes=0-3",
        })
    assert foreign.status_code == 404
    assert proxy.state.requests[0]["x-openwebui-user-id"] == "other"
    assert all(client.is_closed for client in proxy.state.clients)


def fake_upstream(videos, monkeypatch, *, error=None, status=200, headers=None):
    state = SimpleNamespace(client_closed=False, stream_closed=False, consumed=False)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            state.consumed = True
            yield b"a" * (64 * 1024)
            yield b"b" * (64 * 1024)

        async def aclose(self):
            await anyio.sleep(0)
            state.stream_closed = True

    class Client:
        def __init__(self, **kwargs):
            pass

        def build_request(self, method, url, headers):
            return httpx.Request(method, url, headers=headers)

        async def send(self, request, stream):
            if error:
                raise error
            return httpx.Response(status, headers=headers, stream=Stream())

        async def aclose(self):
            await anyio.sleep(0)
            state.client_closed = True

    monkeypatch.setattr(videos.httpx, "AsyncClient", Client)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (httpx.ConnectTimeout("synthetic timeout"), 504),
    (httpx.ConnectError("synthetic connect failure"), 502),
])
async def test_send_failures_close_client(videos, monkeypatch, error, expected):
    state = fake_upstream(videos, monkeypatch, error=error)
    with pytest.raises(HTTPException) as raised:
        await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    assert raised.value.status_code == expected
    assert state.client_closed


@pytest.mark.asyncio
async def test_cancelled_send_closes_client(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    assert state.client_closed


@pytest.mark.asyncio
async def test_partial_stream_close_releases_response_and_client(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch)
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    assert len(await anext(response.body_iterator)) == 64 * 1024
    await response.body_iterator.aclose()
    assert state.stream_closed and state.client_closed


@pytest.mark.asyncio
async def test_background_cleanup_releases_unstarted_stream(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch)
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    await response.background()
    assert state.stream_closed and state.client_closed and not state.consumed


@pytest.mark.asyncio
async def test_disconnect_before_first_body_chunk_releases_upstream(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch)
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))

    async def disconnected_send(message):
        raise OSError("synthetic disconnected browser")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, disconnected_send)
    assert state.stream_closed and state.client_closed and not state.consumed


@pytest.mark.asyncio
async def test_anyio_disconnect_cancellation_cannot_interrupt_resource_cleanup(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch)
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))

    with anyio.CancelScope() as cancel_scope:
        async def disconnected_send(message):
            cancel_scope.cancel()
            await anyio.sleep(0)

        async def receive():
            return {"type": "http.disconnect"}

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, disconnected_send)

    assert state.stream_closed and state.client_closed and not state.consumed


@pytest.mark.asyncio
async def test_response_close_timeout_still_attempts_client_close(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch)
    original = videos.httpx.AsyncClient

    class SlowResponseCloseClient(original):
        async def send(self, request, stream):
            response = await super().send(request, stream)

            async def stalled_close():
                await anyio.sleep_forever()

            response.aclose = stalled_close
            return response

    monkeypatch.setattr(videos.httpx, "AsyncClient", SlowResponseCloseClient)
    monkeypatch.setattr(videos, "_DOWNLOAD_CLOSE_TIMEOUT_SECONDS", .01)
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    await asyncio.wait_for(response.background(), .5)
    assert state.client_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers,expected", [
    (503, {}, 503),
    (302, {"location": "https://untrusted.invalid"}, 502),
    (200, {"content-encoding": "gzip"}, 502),
])
async def test_errors_redirects_and_compression_close_without_buffering(videos, monkeypatch, status, headers, expected):
    state = fake_upstream(videos, monkeypatch, status=status, headers=headers)
    with pytest.raises(HTTPException) as raised:
        await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    assert raised.value.status_code == expected
    assert state.stream_closed and state.client_closed
    assert not state.consumed


@pytest.mark.asyncio
async def test_conditional_304_closes_without_consuming_body(videos, monkeypatch):
    state = fake_upstream(videos, monkeypatch, status=304, headers={"etag": '"version"'})
    response = await videos.get_video_content("clip", SimpleNamespace(headers={}), SimpleNamespace(id="owner"))
    assert response.status_code == 304 and response.body == b""
    assert response.headers["etag"] == '"version"'
    assert state.stream_closed and state.client_closed and not state.consumed
