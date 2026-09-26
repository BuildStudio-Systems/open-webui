from __future__ import annotations

import re
from collections.abc import AsyncIterator

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from open_webui.models.chats import Chats
from open_webui.routers.openai import get_openai_runtime_config
from open_webui.utils.agent_file_delivery import (
    AGENT_CHAT_HEADER,
    AgentChatBindingError,
    file_owner_headers,
    is_local_hermes_url,
    require_agent_chat_owner,
)
from open_webui.utils.auth import get_admin_user

router = APIRouter()
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_DOWNLOAD_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=10)
# Same verification as httpx's per-client default, built once instead of per download.
_TLS_CONTEXT = httpx.create_ssl_context()
_DOWNLOAD_CLOSE_TIMEOUT_SECONDS = 5.0


async def _close_download_resource(resource) -> None:
    with anyio.move_on_after(_DOWNLOAD_CLOSE_TIMEOUT_SECONDS, shield=True):
        await resource.aclose()


async def _hermes_connection() -> tuple[str, dict[str, str]]:
    """Resolve Hermes from Open WebUI's current persisted connections."""
    _, urls, keys, _ = await get_openai_runtime_config()
    matches = [index for index, url in enumerate(urls) if is_local_hermes_url(url)]
    if len(matches) != 1:
        raise HTTPException(status_code=503, detail="Agent file delivery is not configured")

    index = matches[0]
    base_url = urls[index] if index < len(urls) else ""
    api_key = keys[index] if index < len(keys) else ""
    if not base_url or not api_key:
        raise HTTPException(status_code=503, detail="Agent file delivery is not configured")
    return base_url.rstrip('/'), {"Authorization": f"Bearer {api_key}"}


def _upstream_error(response: httpx.Response) -> HTTPException:
    if response.status_code == 404:
        return HTTPException(status_code=404, detail="File not found or link expired")
    if response.status_code in {401, 403}:
        return HTTPException(status_code=502, detail="Agent file authorization failed")
    return HTTPException(status_code=502, detail="Agent file download failed")


def _download_headers(response: httpx.Response, *, include_content_length: bool = True) -> dict[str, str]:
    headers = {
        "cache-control": "private, no-store",
        "content-security-policy": "sandbox",
        "content-disposition": "attachment",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
    }
    for name in (
        "accept-ranges",
        "content-disposition",
        "content-range",
        "etag",
        "last-modified",
    ):
        if value := response.headers.get(name):
            headers[name] = value
    if include_content_length and (content_length := response.headers.get("content-length")):
        headers["content-length"] = content_length
    return headers


@router.get("/{artifact_id}/{filename}")
async def download_agent_file(
    artifact_id: str,
    filename: str,
    request: Request,
    _user=Depends(get_admin_user),
):
    del filename  # Display-only; Hermes owns the trusted filename metadata.
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise HTTPException(status_code=404, detail="File not found")

    base_url, headers = await _hermes_connection()
    owner_headers = file_owner_headers(_user)
    if not owner_headers:
        raise HTTPException(status_code=404, detail="File not found")
    headers.update(owner_headers)
    headers["Accept-Encoding"] = "identity"
    for name in ("range", "if-range", "if-none-match", "if-modified-since"):
        if value := request.headers.get(name):
            headers[name] = value

    client = httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, limits=_DOWNLOAD_LIMITS, verify=_TLS_CONTEXT)
    try:
        upstream_request = client.build_request("GET", f"{base_url}/files/{artifact_id}", headers=headers)
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        await _close_download_resource(client)
        raise HTTPException(status_code=504, detail="Agent file download timed out") from exc
    except httpx.RequestError as exc:
        await _close_download_resource(client)
        raise HTTPException(status_code=502, detail="Agent file download failed") from exc
    except BaseException:
        await _close_download_resource(client)
        raise

    async def close() -> None:
        try:
            await _close_download_resource(response)
        finally:
            await _close_download_resource(client)

    # This header comes from the Agent's stored artifact record, never the
    # browser. Check before reading bytes or publishing response headers. Legacy
    # links without a binding keep their existing owner/TTL policy.
    bound_chat_id = response.headers.get(AGENT_CHAT_HEADER)
    if bound_chat_id is not None:
        try:
            await require_agent_chat_owner(bound_chat_id, _user.id, Chats.is_chat_owner)
        except AgentChatBindingError:
            await close()
            raise HTTPException(status_code=404, detail="File not found or link expired") from None
        except BaseException:
            await close()
            raise

    if response.status_code == 416:
        response_headers = _download_headers(response, include_content_length=False)
        await close()
        return Response(status_code=416, headers=response_headers)
    if response.is_error:
        error = _upstream_error(response)
        await close()
        raise error

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(1024 * 1024):
                yield chunk
        finally:
            await close()

    return StreamingResponse(
        stream(),
        status_code=response.status_code,
        # Keeping this route octet-stream also prevents the global response
        # compressor from corrupting byte ranges for text-like artifacts.
        media_type="application/octet-stream",
        headers=_download_headers(response),
    )
