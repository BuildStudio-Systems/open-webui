from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from open_webui.utils.auth import get_verified_user

router = APIRouter()
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _hermes_connection() -> tuple[str, dict[str, str]]:
    """Resolve the Hermes entry from the OpenAI-compatible connection env."""
    raw_urls = os.getenv("OPENAI_API_BASE_URLS") or os.getenv("OPENAI_API_BASE_URL", "http://127.0.0.1:8642/v1")
    raw_keys = os.getenv("OPENAI_API_KEYS") or os.getenv("OPENAI_API_KEY", "")
    urls = [value.strip().rstrip("/") for value in raw_urls.split(";")]
    keys = [value.strip() for value in raw_keys.split(";")]

    index = next(
        (i for i, url in enumerate(urls) if ":8642" in url),
        0,
    )
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

    base_url, headers = _hermes_connection()
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
