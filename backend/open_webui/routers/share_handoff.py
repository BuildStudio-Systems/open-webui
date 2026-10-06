"""Explicit, owner-scoped PDF handoff; never sends monitoring data automatically."""
import asyncio
import os
import threading
from urllib.parse import unquote_to_bytes

from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui import studio_identity as studio
from open_webui.studio_handoff import handoff_pdf, HandoffError, OutcomeUnconfirmed

MAX_FILE = 32 * 1024 * 1024
# A cancelled request cannot release the network worker's admission early.
workers = threading.BoundedSemaphore(2)


def available():
    return (os.getenv('THERE_SHARE_HANDOFF') == '1'
            and studio.ENABLED and studio.APP == 'there')


def transfer(token, request_key, name, payload):
    if not workers.acquire(blocking=False):
        raise HTTPException(429, 'share_busy')
    try:
        return handoff_pdf('there', os.getenv('STUDIO_IDENTITY_KEY', ''),
                           token, request_key, name, payload)
    except OutcomeUnconfirmed:
        raise HTTPException(503, 'share_unconfirmed') from None
    except HandoffError as error:
        # Stable code only (share_rejected / share_link_required / share_quota); never the upstream text.
        raise HTTPException(409, error.reason) from None
    finally:
        workers.release()


def create_router(require_export_user):
    router = APIRouter(prefix='/api/v1/integrations/share')
    uploads = 0

    @router.get('/status')
    async def status(user=Depends(require_export_user)):
        return {'enabled': available()}

    @router.post('/files', status_code=201)
    async def upload(request: Request, user=Depends(require_export_user)):
        nonlocal uploads
        # Ambient cookies alone never authorize this operation. The native bearer
        # header plus exact Origin provides CSRF protection for the browser flow.
        if request.headers.get('origin') != 'https://buildstudio-there.com':
            raise HTTPException(403, 'invalid_origin')
        token = browser_token(request)
        if not available():
            raise HTTPException(503, 'share_disabled')
        if request.headers.get('content-type', '').split(';', 1)[0] != 'application/pdf':
            raise HTTPException(415, 'pdf_required')
        if request.headers.get('content-encoding', 'identity') != 'identity':
            raise HTTPException(415, 'pdf_required')
        if uploads >= 2:
            raise HTTPException(429, 'share_busy')
        uploads += 1
        try:
            async def read():
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_FILE:
                        raise HTTPException(413, 'pdf_too_large')
                    body.extend(chunk)
                return bytes(body)
            try:
                payload = await asyncio.wait_for(read(), 30)
            except asyncio.TimeoutError:
                raise HTTPException(408, 'upload_timeout') from None
            if not payload.startswith(b'%PDF-'):
                raise HTTPException(422, 'pdf_required')
            try:
                name = unquote_to_bytes(request.headers.get('x-file-name', '')).decode('utf-8')
            except UnicodeError:
                raise HTTPException(422, 'invalid_file_name') from None
            # Recheck after a potentially slow body upload, before delegating.
            await studio.check(token, fresh=True)
            await require_export_user(request)
            return await asyncio.get_running_loop().run_in_executor(None, transfer, token,
                request.headers.get('idempotency-key', ''), name, payload)
        finally:
            uploads -= 1

    return router


def browser_token(request):
    authorization = request.headers.get('authorization', '')
    if not authorization.startswith('Bearer bs1_') or len(authorization) > 207:
        raise HTTPException(401, 'central_session_required')
    return authorization[7:]


async def require_export_user(request: Request):
    # Native authentication and export permission are independent of Share access.
    from open_webui.models.config import Config
    from open_webui.utils.access_control import get_permissions
    from open_webui.there_studio import user_by_token
    token = browser_token(request)
    user = await user_by_token(token)
    if user.role not in ('admin', 'user'):
        raise HTTPException(403, 'export_not_allowed')
    permissions = await get_permissions(user.id, await Config.get('user.permissions'))
    if user.role != 'admin' and not (permissions or {}).get('chat', {}).get('export', True):
        raise HTTPException(403, 'export_not_allowed')
    return user


router = create_router(require_export_user)
