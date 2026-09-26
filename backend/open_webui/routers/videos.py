from __future__ import annotations

import os
from collections.abc import AsyncIterator

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from open_webui.utils.auth import get_verified_user


router = APIRouter()
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_DOWNLOAD_CLOSE_TIMEOUT_SECONDS = 5.0


async def _close_download_resource(resource) -> None:
    # A disconnected browser may cancel the surrounding AnyIO scope. Give each
    # resource its own bounded cleanup opportunity, without hanging the request.
    with anyio.move_on_after(_DOWNLOAD_CLOSE_TIMEOUT_SECONDS, shield=True):
        await resource.aclose()


class _VideoStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect while sending response headers can occur before the
            # body generator starts and before Starlette runs background work.
            if self.background is not None:
                await self.background()


class CreateVideoForm(BaseModel):
    prompt: str = Field(min_length=1, max_length=12_000)
    size: str = "864x480"
    seconds: int = Field(default=5, ge=2, le=15)
    seed: int | None = None
    quality: str = "turbo"
    model: str = "minimax-h3"


def _coordinator() -> tuple[str, dict[str, str]]:
    base_url = os.getenv(
        "VIDEO_GENERATION_API_BASE_URL", "http://127.0.0.1:8890"
    ).rstrip("/")
    api_key = os.getenv("VIDEO_GENERATION_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Video generation is not configured")
    return base_url, {"Authorization": f"Bearer {api_key}"}


def _user_headers(user) -> dict[str, str]:
    _, headers = _coordinator()
    return {**headers, "X-OpenWebUI-User-Id": user.id}


def _upstream_error(response: httpx.Response) -> HTTPException:
    detail = "Video generation service request failed"
    try:
        payload = response.json()
        detail = payload.get("detail") or payload.get("error", {}).get("message") or detail
    except (ValueError, AttributeError):
        if response.text.strip():
            detail = response.text.strip()[:1000]
    return HTTPException(status_code=response.status_code, detail=detail)


@router.post("/jobs", status_code=202)
async def create_video(form: CreateVideoForm, user=Depends(get_verified_user)):
    base_url, _ = _coordinator()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/v1/videos",
            headers=_user_headers(user),
            json=form.model_dump(exclude_none=True),
        )
    if response.is_error:
        raise _upstream_error(response)
    return response.json()


@router.get("/jobs/{job_id}")
async def get_video_job(job_id: str, user=Depends(get_verified_user)):
    base_url, _ = _coordinator()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{base_url}/v1/videos/{job_id}", headers=_user_headers(user)
        )
    if response.is_error:
        raise _upstream_error(response)
    return response.json()


@router.get("/jobs/{job_id}/content")
async def get_video_content(job_id: str, request: Request, user=Depends(get_verified_user)):
    base_url, _ = _coordinator()
    upstream_headers = {**_user_headers(user), "Accept-Encoding": "identity"}
    for name in ("range", "if-range", "if-none-match", "if-modified-since"):
        if value := request.headers.get(name):
            upstream_headers[name] = value
    client = httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, trust_env=False, follow_redirects=False)
    try:
        upstream_request = client.build_request(
            "GET", f"{base_url}/v1/videos/{job_id}/content", headers=upstream_headers
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        await _close_download_resource(client)
        raise HTTPException(status_code=504, detail="Video download timed out") from exc
    except httpx.RequestError as exc:
        await _close_download_resource(client)
        raise HTTPException(status_code=502, detail="Video download failed") from exc
    except BaseException:
        await _close_download_resource(client)
        raise

    closed = False

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        try:
            await _close_download_resource(response)
        finally:
            try:
                await _close_download_resource(client)
            finally:
                closed = True

    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": response.headers.get(
            "content-disposition", f'inline; filename="{job_id}.mp4"'
        ),
        "X-Content-Type-Options": "nosniff",
    }
    for name in ("accept-ranges", "content-range", "etag", "last-modified"):
        if value := response.headers.get(name):
            headers[name] = value
    if response.status_code in {304, 416}:
        await close()
        return Response(status_code=response.status_code, headers=headers)
    if response.status_code not in {200, 206}:
        # Do not buffer an unbounded upstream error body or follow a redirect.
        await close()
        raise HTTPException(
            status_code=response.status_code if response.is_error else 502,
            detail="Video download failed",
        )
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        await close()
        raise HTTPException(status_code=502, detail="Unexpected video content encoding")

    async def stream() -> AsyncIterator[bytes]:
        try:
            # Keep byte-range payloads unchanged, including multipart ranges.
            async for chunk in response.aiter_raw(64 * 1024):
                yield chunk
        finally:
            await close()

    if content_length := response.headers.get("content-length"):
        headers["Content-Length"] = content_length
    return _VideoStreamingResponse(
        stream(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "video/mp4"),
        headers=headers,
        background=BackgroundTask(close),
    )
