from __future__ import annotations

import os
from types import SimpleNamespace

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

        async def aiter_bytes(self, _chunk_size):
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
    request = SimpleNamespace(headers={"range": "bytes=2-5"})

    response = await agent_files_module.download_agent_file(
        "a" * 32, "clip.mp4", request, _user=SimpleNamespace(id="user-1")
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert captured["url"] == "http://127.0.0.1:8642/v1/files/" + "a" * 32
    assert captured["request_headers"]["range"] == "bytes=2-5"
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert body == b"2345"
    assert captured["response_closed"] is True
    assert captured["client_closed"] is True
