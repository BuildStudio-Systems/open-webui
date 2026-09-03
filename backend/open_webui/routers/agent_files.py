from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from open_webui.routers.openai import get_openai_runtime_config
from open_webui.utils.agent_file_delivery import file_owner_headers, is_local_hermes_url
from open_webui.utils.auth import get_admin_user

router = APIRouter()
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_DOWNLOAD_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=10)


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

    client = httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, limits=_DOWNLOAD_LIMITS)
    try:
        upstream_request = client.build_request("GET", f"{base_url}/files/{artifact_id}", headers=headers)
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise HTTPException(status_code=504, detail="Agent file download timed out") from exc
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Agent file download failed") from exc

    if response.status_code == 416:
        response_headers = _download_headers(response, include_content_length=False)
        try:
            await response.aclose()
        finally:
            await client.aclose()
        return Response(status_code=416, headers=response_headers)
    if response.is_error:
        error = _upstream_error(response)
        try:
            await response.aclose()
        finally:
            await client.aclose()
        raise error

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(1024 * 1024):
                yield chunk
        finally:
            try:
                await response.aclose()
            finally:
                await client.aclose()

    return StreamingResponse(
        stream(),
        status_code=response.status_code,
        # Keeping this route octet-stream also prevents the global response
        # compressor from corrupting byte ranges for text-like artifacts.
        media_type="application/octet-stream",
        headers=_download_headers(response),
    )
