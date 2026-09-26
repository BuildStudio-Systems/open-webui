from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest


@pytest.fixture(scope="module")
def agent_files_module(tmp_path_factory):
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("open-webui-agent-files"))
    os.environ["WEBUI_SECRET_KEY"] = "local-agent-file-test-secret"

    from open_webui.routers import agent_files

    return agent_files


@pytest.mark.asyncio
async def test_hermes_connection_selects_agent_port(agent_files_module, monkeypatch):
    async def runtime_config():
        return (
            True,
            ["http://127.0.0.1:8000/v1", "http://127.0.0.1:8642/v1"],
            ["model-key", "agent-key"],
            {},
        )

    monkeypatch.setattr(agent_files_module, "get_openai_runtime_config", runtime_config)

    base_url, headers = await agent_files_module._hermes_connection()

    assert base_url == "http://127.0.0.1:8642/v1"
    assert headers == {"Authorization": "Bearer agent-key"}


@pytest.mark.asyncio
async def test_hermes_connection_rejects_ambiguous_matches(agent_files_module, monkeypatch):
    async def runtime_config():
        return (
            True,
            ["http://127.0.0.1:8642/v1", "http://localhost:8642/v1"],
            ["agent-key-1", "agent-key-2"],
            {},
        )

    monkeypatch.setattr(agent_files_module, "get_openai_runtime_config", runtime_config)

    with pytest.raises(agent_files_module.HTTPException) as error:
        await agent_files_module._hermes_connection()

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_download_proxy_forwards_range_and_response_headers(agent_files_module, monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 206
        is_error = False
        headers = {
            "content-type": "video/mp4",
            "content-length": "4",
            "content-range": "bytes 2-5/10",
            "accept-ranges": "bytes",
            "content-disposition": 'attachment; filename="clip.mp4"',
        }

        async def aiter_raw(self, _chunk_size):
            yield b"2345"

        async def aclose(self):
            captured["response_closed"] = True

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, method, url, headers):
            captured.update(method=method, url=url, request_headers=headers)
            return object()

        async def send(self, _request, stream):
            assert stream is True
            return FakeResponse()

        async def aclose(self):
            captured["client_closed"] = True

    async def hermes_connection():
        return "http://127.0.0.1:8642/v1", {"Authorization": "Bearer secret"}

    monkeypatch.setattr(agent_files_module, "_hermes_connection", hermes_connection)
    monkeypatch.setattr(agent_files_module.httpx, "AsyncClient", FakeClient)
    request = SimpleNamespace(headers={"range": "bytes=2-5", "if-range": '"clip-v1"'})

    response = await agent_files_module.download_agent_file(
        "a" * 32,
        "clip.mp4",
        request,
        _user=SimpleNamespace(id="user-1", role="admin"),
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert captured["url"] == "http://127.0.0.1:8642/v1/files/" + "a" * 32
    assert captured["request_headers"]["range"] == "bytes=2-5"
    assert captured["request_headers"]["if-range"] == '"clip-v1"'
    assert captured["request_headers"]["Accept-Encoding"] == "identity"
    assert captured["request_headers"]["X-BuildStudio-User-Id"] == "user-1"
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/octet-stream"
    assert body == b"2345"
    assert captured["response_closed"] is True
    assert captured["client_closed"] is True


def test_owner_headers_accept_only_bounded_identifiers(agent_files_module):
    helper = agent_files_module.file_owner_headers

    assert helper(SimpleNamespace(id="user-1", role="admin")) == {"X-BuildStudio-User-Id": "user-1"}
    assert helper(SimpleNamespace(id="user-1", role="user")) == {}
    assert helper(SimpleNamespace(id="bad owner", role="admin")) == {}
    assert helper(SimpleNamespace(id="x" * 129, role="admin")) == {}


def test_local_hermes_url_is_strict(agent_files_module):
    helper = agent_files_module.is_local_hermes_url

    assert helper("http://127.0.0.1:8642/v1") is True
    assert helper("https://[::1]:8642/v1/") is True
    assert helper("ftp://127.0.0.1:8642/v1") is False
    assert helper("http://user@127.0.0.1:8642/v1") is False
    assert helper("http://127.0.0.1:8642/other") is False
    assert helper("http://127.0.0.1:8642/v1?target=elsewhere") is False


@pytest.mark.asyncio
async def test_download_proxy_preserves_416_and_closes_resources(agent_files_module, monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 416
        is_error = True
        headers = {
            "content-length": "99",
            "content-range": "bytes */10",
            "accept-ranges": "bytes",
        }

        async def aclose(self):
            captured["response_closed"] = True

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, _method, _url, _headers=None, **_kwargs):
            return object()

        async def send(self, _request, stream):
            assert stream is True
            return FakeResponse()

        async def aclose(self):
            captured["client_closed"] = True

    async def hermes_connection():
        return "http://127.0.0.1:8642/v1", {"Authorization": "Bearer secret"}

    monkeypatch.setattr(agent_files_module, "_hermes_connection", hermes_connection)
    monkeypatch.setattr(agent_files_module.httpx, "AsyncClient", FakeClient)

    response = await agent_files_module.download_agent_file(
        "b" * 32,
        "clip.mp4",
        SimpleNamespace(headers={"range": "bytes=99-"}),
        _user=SimpleNamespace(id="admin-1", role="admin"),
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.headers["content-length"] == "0"
    assert captured == {"response_closed": True, "client_closed": True}


@pytest.mark.asyncio
async def test_download_proxy_closes_client_on_connect_timeout(agent_files_module, monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, _method, _url, _headers=None, **_kwargs):
            return object()

        async def send(self, _request, stream):
            assert stream is True
            raise agent_files_module.httpx.ConnectTimeout("timeout")

        async def aclose(self):
            captured["client_closed"] = True

    async def hermes_connection():
        return "http://127.0.0.1:8642/v1", {"Authorization": "Bearer secret"}

    monkeypatch.setattr(agent_files_module, "_hermes_connection", hermes_connection)
    monkeypatch.setattr(agent_files_module.httpx, "AsyncClient", FakeClient)

    with pytest.raises(agent_files_module.HTTPException) as error:
        await agent_files_module.download_agent_file(
            "c" * 32,
            "clip.mp4",
            SimpleNamespace(headers={}),
            _user=SimpleNamespace(id="admin-1", role="admin"),
        )

    assert error.value.status_code == 504
    assert captured == {"client_closed": True}


@pytest.mark.asyncio
async def test_openai_requests_bind_local_hermes_to_verified_user(
    agent_files_module,
):
    from open_webui.routers import openai

    request = SimpleNamespace(cookies={})
    admin = SimpleNamespace(id="user-1", role="admin", name="Admin", email="admin@example.test")
    customer = SimpleNamespace(id="user-2", role="user", name="Customer", email="customer@example.test")
    local_headers, _ = await openai.get_headers_and_cookies(
        request,
        "http://127.0.0.1:8642/v1",
        config={
            "auth_type": "none",
            "headers": {"x-buildstudio-user-id": "spoofed-user"},
        },
        user=admin,
    )
    remote_headers, _ = await openai.get_headers_and_cookies(
        request,
        "https://api.example.com/v1",
        config={"auth_type": "none"},
        user=admin,
    )
    customer_headers, _ = await openai.get_headers_and_cookies(
        request,
        "http://127.0.0.1:8642/v1",
        config={
            "auth_type": "none",
            "headers": {"X-BuildStudio-User-Id": "spoofed-user"},
        },
        user=customer,
    )

    assert local_headers["X-BuildStudio-User-Id"] == "user-1"
    assert "X-BuildStudio-User-Id" not in remote_headers
    assert "X-BuildStudio-User-Id" not in customer_headers


@pytest.fixture
def bound_download(agent_files_module, monkeypatch):
    """Synthetic response only: no network, user data, or physical artifact."""
    state = SimpleNamespace(
        response_closed=False, client_closed=False, body_read=False,
        chat_id="c66d52ba-ae53-44c5-8ed0-56b19992cab0", status_code=200,
        headers={}, response_close_wait=None,
    )

    class FakeResponse:
        @property
        def status_code(self):
            return state.status_code

        @property
        def is_error(self):
            return state.status_code >= 400

        @property
        def headers(self):
            return httpx.Headers(state.headers)

        async def aiter_raw(self, _chunk_size):
            state.body_read = True
            yield b"synthetic artifact"

        async def aclose(self):
            if state.response_close_wait is not None:
                await state.response_close_wait()
            await anyio.sleep(0)
            state.response_closed = True

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, method, url, headers):
            state.request_headers = headers
            return object()

        async def send(self, request, stream):
            assert stream is True
            return FakeResponse()

        async def aclose(self):
            await anyio.sleep(0)
            state.client_closed = True

    async def connection():
        return "http://127.0.0.1:8642/v1", {"Authorization": "Bearer synthetic-key"}

    state.ownership = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_files_module, "_hermes_connection", connection)
    monkeypatch.setattr(agent_files_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(agent_files_module.Chats, "is_chat_owner", state.ownership)
    state.headers = {agent_files_module.AGENT_CHAT_HEADER: state.chat_id}

    async def download():
        return await agent_files_module.download_agent_file(
            "a" * 32, "report.txt",
            SimpleNamespace(headers={"X-BuildStudio-Chat-Id": "browser-spoofed-chat"}),
            _user=SimpleNamespace(id="admin-a", role="admin"),
        )

    state.download = download
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 206])
async def test_bound_download_checks_owner_before_read_and_hides_internal_header(
    bound_download, status_code,
):
    state = bound_download
    state.status_code = status_code
    response = await state.download()
    state.ownership.assert_awaited_once_with(state.chat_id, "admin-a")
    assert state.body_read is False
    assert "x-buildstudio-chat-id" not in response.headers
    assert "X-BuildStudio-Chat-Id" not in state.request_headers
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"synthetic artifact"
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 206, 304, 416])
async def test_deleted_or_foreign_chat_denies_bound_download_and_closes_before_body(
    agent_files_module, bound_download, status_code,
):
    state = bound_download
    state.status_code = status_code
    state.ownership.return_value = False
    with pytest.raises(agent_files_module.HTTPException) as caught:
        await state.download()
    assert caught.value.status_code == 404
    assert not state.body_read
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["", "not-a-uuid", "C66D52BA-AE53-44C5-8ED0-56B19992CAB0", "temporary:socket"])
async def test_invalid_artifact_chat_binding_is_not_legacy(
    agent_files_module, bound_download, chat_id,
):
    state = bound_download
    state.headers[agent_files_module.AGENT_CHAT_HEADER] = chat_id
    with pytest.raises(agent_files_module.HTTPException) as caught:
        await state.download()
    assert caught.value.status_code == 404
    state.ownership.assert_not_awaited()
    assert not state.body_read
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
async def test_legacy_without_artifact_chat_header_keeps_owner_ttl_compatibility(bound_download):
    state = bound_download
    state.headers = {}
    response = await state.download()
    state.ownership.assert_not_awaited()
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"synthetic artifact"
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
async def test_download_binding_database_exception_releases_both_resources(bound_download):
    state = bound_download
    state.ownership.side_effect = RuntimeError("synthetic database unavailable")
    with pytest.raises(RuntimeError):
        await state.download()
    assert not state.body_read
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
async def test_cancel_during_download_binding_shields_async_resource_cleanup(bound_download):
    state = bound_download
    with anyio.CancelScope() as scope:
        async def cancel_lookup(*_args):
            scope.cancel()
            await anyio.sleep(0)

        state.ownership.side_effect = cancel_lookup
        await state.download()
    assert not state.body_read
    assert state.client_closed and state.response_closed


@pytest.mark.asyncio
async def test_close_timeout_still_attempts_client_close(
    agent_files_module, bound_download, monkeypatch,
):
    state = bound_download
    state.ownership.return_value = False
    state.response_close_wait = anyio.sleep_forever
    original_scope = anyio.move_on_after
    cleanup_budgets = []

    def independent_resource_scope(delay, *, shield):
        cleanup_budgets.append((delay, shield))
        # Cancel the blocked response at its first checkpoint. Keep the second
        # resource's real budget, so scheduler jitter cannot falsely fail its
        # separate cleanup opportunity.
        return original_scope(0 if len(cleanup_budgets) == 1 else delay, shield=shield)

    monkeypatch.setattr(agent_files_module.anyio, "move_on_after", independent_resource_scope)
    with pytest.raises(agent_files_module.HTTPException) as caught:
        await state.download()
    assert caught.value.status_code == 404
    assert not state.body_read
    assert state.client_closed
    assert cleanup_budgets == [(5.0, True), (5.0, True)]
