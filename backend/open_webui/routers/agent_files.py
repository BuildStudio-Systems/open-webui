from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from open_webui.routers.openai import get_openai_runtime_config
from open_webui.utils.agent_file_delivery import file_owner_headers, is_local_hermes_url
from open_webui.utils.auth import get_verified_user

router = APIRouter()
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


async def _hermes_connection() -> tuple[str, dict[str, str]]:
    """Resolve Hermes from Open WebUI's current persisted connections."""
    _, urls, keys, _ = await get_openai_runtime_config()
    index = next((i for i, url in enumerate(urls) if is_local_hermes_url(url)), None)
    if index is None:
        raise HTTPException(status_code=503, detail="Agent file delivery is not configured")

    base_url = urls[index] if index < len(urls) else ""
    api_key = keys[index] if index < len(keys) else ""
    if not base_url or not api_key:
        raise HTTPException(status_code=503, detail="Agent file delivery is not configured")
    return base_url, {"Authorization": f"Bearer {api_key}"}


def _upstream_error(response: httpx.Response) -> HTTPException:
    if response.status_code == 404:
        return HTTPException(status_code=404, detail="File not found or link expired")
    if response.status_code in {401, 403}:
        return HTTPException(status_code=502, detail="Agent file authorization failed")
    return HTTPException(status_code=502, detail="Agent file download failed")


@router.get("/{artifact_id}/{filename}")
async def download_agent_file(
    artifact_id: str,
    filename: str,
    request: Request,
    _user=Depends(get_verified_user),
):
    del filename  # Display-only; Hermes owns the trusted filename metadata.
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise HTTPException(status_code=404, detail="File not found")

    base_url, headers = await _hermes_connection()
    headers.update(file_owner_headers(_user))
    for name in ("range", "if-none-match", "if-modified-since"):
        if value := request.headers.get(name):
            headers[name] = value

    client = httpx.AsyncClient(timeout=None)
    upstream_request = client.build_request("GET", f"{base_url}/files/{artifact_id}", headers=headers)
    response = await client.send(upstream_request, stream=True)
    if response.is_error:
        await response.aread()
        error = _upstream_error(response)
        await response.aclose()
        await client.aclose()
        raise error

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    forwarded_headers = {
        name: value
        for name in (
            "accept-ranges",
            "cache-control",
            "content-disposition",
            "content-length",
            "content-range",
            "etag",
            "last-modified",
        )
        if (value := response.headers.get(name))
    }
    return StreamingResponse(
        stream(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=forwarded_headers,
    )
