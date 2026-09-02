from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from open_webui.utils.auth import get_verified_user


router = APIRouter()


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
async def get_video_content(job_id: str, user=Depends(get_verified_user)):
    base_url, _ = _coordinator()
    client = httpx.AsyncClient(timeout=None)
    request = client.build_request(
        "GET",
        f"{base_url}/v1/videos/{job_id}/content",
        headers=_user_headers(user),
    )
    response = await client.send(request, stream=True)
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

    headers = {
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": response.headers.get(
            "content-disposition", f'inline; filename="{job_id}.mp4"'
        ),
    }
    if content_length := response.headers.get("content-length"):
        headers["Content-Length"] = content_length
    return StreamingResponse(
        stream(),
        media_type=response.headers.get("content-type", "video/mp4"),
        headers=headers,
    )
