"""Admin-only, read-only access to consented WeChat Mini Program chats."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from loguru import logger

from open_webui.constants import ERROR_MESSAGES
from open_webui.there_integration.miniprogram_chats import (
    MiniProgramChatStore,
    MiniProgramChatStoreError,
)
from open_webui.utils.audit import (
    AuditLevel,
    AuditLogEntry,
    AuditLogger,
    wechat_admin_audit_is_ready,
)
from open_webui.utils.auth import get_admin_user

FEATURE_ENV = "ENABLE_ADMIN_WECHAT_CHAT_ACCESS"
audit_logger = AuditLogger(logger)
NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}
ADMIN_WECHAT_PATH_PREFIX = "/api/v1/there/admin/wechat"


class AdminWeChatNoStoreMiddleware:
    """Prevent every response on the sensitive admin prefix from being cached.

    A route dependency cannot decorate responses produced before the endpoint
    runs, including authentication failures and FastAPI request validation
    errors.  This pure-ASGI wrapper observes those responses as well while
    leaving every other application route untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
            path == ADMIN_WECHAT_PATH_PREFIX or path.startswith(f"{ADMIN_WECHAT_PATH_PREFIX}/")
        ):
            return await self.app(scope, receive, send)

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {b"cache-control", b"pragma"}
                ]
                headers.extend(
                    (name.lower().encode("ascii"), value.encode("ascii")) for name, value in NO_STORE_HEADERS.items()
                )
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_no_store)


def _no_store(response: Response) -> None:
    response.headers.update(NO_STORE_HEADERS)


router = APIRouter(dependencies=[Depends(_no_store)])


def _feature_enabled() -> bool:
    return os.getenv(FEATURE_ENV, "false").strip().lower() == "true"


def _http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=NO_STORE_HEADERS,
    )


def _require_feature(request: Request, user, *, action: str) -> None:
    if not _feature_enabled():
        _write_audit(
            request,
            user,
            action=action,
            target_id=None,
            result_count=0,
            response_status=status.HTTP_403_FORBIDDEN,
        )
        raise _http_error(status.HTTP_403_FORBIDDEN, ERROR_MESSAGES.ACCESS_PROHIBITED)

    if not wechat_admin_audit_is_ready():
        # There is deliberately no audit write here: reaching this branch means
        # the durable file sink cannot be trusted.  Refuse before opening the
        # Mini Program database and leave a generic application-log diagnostic.
        logger.error("WeChat administrator chat access refused: audit file sink is unavailable")
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Administrator audit trail is temporarily unavailable",
        )


def _write_audit(
    request: Request,
    user,
    *,
    action: str,
    target_id: str | None,
    result_count: int,
    response_status: int,
) -> None:
    """Write metadata only; never pass titles, messages, sources, or query values."""
    try:
        audit_logger.write(
            AuditLogEntry(
                id=str(uuid4()),
                user={"id": str(user.id), "role": str(user.role)},
                audit_level=AuditLevel.METADATA.value,
                verb="READ",
                request_uri="/api/v1/there/admin/wechat",
                response_status_code=response_status,
                source_ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_object=None,
                response_object=None,
            ),
            extra={
                "action": action,
                "target_id": target_id,
                "result_count": result_count,
            },
        )
    except Exception:
        # The audit-file handler is configured with catch=False specifically so
        # a late write failure reaches this boundary.  Never return conversation
        # data unless its access record was durably accepted by the sink.
        logger.exception("WeChat administrator chat access refused: audit write failed")
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Administrator audit trail is temporarily unavailable",
        ) from None


def _store_error(request: Request, user, action: str, target_id: str | None) -> HTTPException:
    _write_audit(
        request,
        user,
        action=action,
        target_id=target_id,
        result_count=0,
        response_status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    return _http_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Mini Program chat records are temporarily unavailable",
    )


@router.get("/chats")
async def list_wechat_chats(
    request: Request,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=50, ge=1, le=100),
    user=Depends(get_admin_user),
):
    _require_feature(request, user, action="wechat_chat.list.denied")
    try:
        result = await asyncio.to_thread(
            MiniProgramChatStore().list_chats,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        _write_audit(
            request,
            user,
            action="wechat_chat.list.invalid",
            target_id=None,
            result_count=0,
            response_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        raise _http_error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except MiniProgramChatStoreError:
        raise _store_error(request, user, "wechat_chat.list", None) from None
    _write_audit(
        request,
        user,
        action="wechat_chat.list",
        target_id=None,
        result_count=len(result["items"]),
        response_status=status.HTTP_200_OK,
    )
    return result


@router.get("/chats/{chat_id}")
async def get_wechat_chat(
    request: Request,
    chat_id: UUID,
    user=Depends(get_admin_user),
):
    _require_feature(request, user, action="wechat_chat.view.denied")
    target_id = str(chat_id)
    try:
        result = await asyncio.to_thread(MiniProgramChatStore().get_chat, target_id)
    except MiniProgramChatStoreError:
        raise _store_error(request, user, "wechat_chat.view", target_id) from None
    if result is None:
        _write_audit(
            request,
            user,
            action="wechat_chat.view",
            target_id=target_id,
            result_count=0,
            response_status=status.HTTP_404_NOT_FOUND,
        )
        raise _http_error(status.HTTP_404_NOT_FOUND, "Chat record not found")
    _write_audit(
        request,
        user,
        action="wechat_chat.view",
        target_id=target_id,
        result_count=1,
        response_status=status.HTTP_200_OK,
    )
    return result


@router.get("/chats/{chat_id}/messages")
async def list_wechat_chat_messages(
    request: Request,
    chat_id: UUID,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=100),
    user=Depends(get_admin_user),
):
    _require_feature(request, user, action="wechat_chat.messages.denied")
    target_id = str(chat_id)
    try:
        result = await asyncio.to_thread(
            MiniProgramChatStore().list_messages,
            target_id,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        _write_audit(
            request,
            user,
            action="wechat_chat.messages.invalid",
            target_id=target_id,
            result_count=0,
            response_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        raise _http_error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except MiniProgramChatStoreError:
        raise _store_error(request, user, "wechat_chat.messages", target_id) from None
    if result is None:
        _write_audit(
            request,
            user,
            action="wechat_chat.messages",
            target_id=target_id,
            result_count=0,
            response_status=status.HTTP_404_NOT_FOUND,
        )
        raise _http_error(status.HTTP_404_NOT_FOUND, "Chat record not found")
    _write_audit(
        request,
        user,
        action="wechat_chat.messages",
        target_id=target_id,
        result_count=len(result["items"]),
        response_status=status.HTTP_200_OK,
    )
    return result
