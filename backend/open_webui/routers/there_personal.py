"""Separate self-service and audited administrative historical-Q&A access."""

import time
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from open_webui.config import ENABLE_ADMIN_CHAT_ACCESS
from open_webui.internal.db import get_async_session
from open_webui.models.there import ThereOperation
from open_webui.there_integration.personal import history_page
from open_webui.utils.auth import get_verified_user


async def private_response(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Authorization, Cookie'


router = APIRouter(dependencies=[Depends(private_response)])


async def bounded_history(db, owner_id, **kwargs):
    try:
        return await history_page(db, owner_id, **kwargs)
    except TimeoutError:
        raise HTTPException(503, '历史检索超时，请稍后重试或缩小关键词范围。') from None


@router.get('/personal/history')
async def own_history(query: str = Query('', max_length=500),
                      page: int = Query(1, ge=1, le=100000),
                      user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    # No target user parameter or administrator bypass on the personal endpoint.
    return await bounded_history(db, user.id, query=query, page=page)


@router.get('/admin/personal-history/{owner_id}')
async def admin_history(owner_id: str, request: Request,
                        query: str = Query('', max_length=500),
                        page: int = Query(1, ge=1, le=100000),
                        user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    if user.role != 'admin' or not ENABLE_ADMIN_CHAT_ACCESS:
        raise HTTPException(403, '没有管理员历史记录调取权限。')
    state_token = getattr(getattr(request.state, 'token', None), 'credentials', '') or ''
    if (request.headers.get('Authorization', '').lower().startswith('bearer sk-')
            or request.headers.get('x-api-key') or state_token.startswith('sk-')
            or request.cookies.get('token', '').startswith('sk-')):
        raise HTTPException(403, '请使用管理员交互式登录。')
    if not owner_id or len(owner_id) > 128:
        raise HTTPException(422, '用户标识无效。')
    # Fail closed: commit a metadata-only access audit BEFORE returning records.
    # Do not put prompts, answers, search text, credentials or tokens in logs.
    now = int(time.time())
    audit_id = str(uuid4())
    db.add(ThereOperation(id=audit_id, user_id=user.id, request_id=audit_id,
        resource_id=owner_id, action='admin.personal_history.read', state='requested',
        created_at=now, updated_at=now))
    await db.commit()
    result = await bounded_history(db, owner_id, query=query, page=page)
    return {**result, 'scope': 'admin', 'owner_id': owner_id, 'audit_id': audit_id}
