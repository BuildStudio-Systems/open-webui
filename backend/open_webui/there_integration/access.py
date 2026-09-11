"""Authorization shared by the management API and both chat retrieval paths."""

from fastapi import HTTPException
from sqlalchemy import select
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.knowledge import Knowledge, Knowledges
from open_webui.models.there import ThereKnowledge
from open_webui.utils.access_control import has_permission


async def require_workspace(user, permission, db=None):
    if user.role != 'admin' and not await has_permission(
        user.id, f'workspace.{permission}', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(403, '当前账号没有此工作区权限。')


async def get_binding(resource_id, user, permission='read', *, db=None, ready=True):
    async with get_async_db_context(db) as session:
        row = (await session.execute(
            select(ThereKnowledge, Knowledge).join(Knowledge, Knowledge.id == ThereKnowledge.id)
            .where(ThereKnowledge.id == resource_id)
        )).first()
        if row is None:
            raise HTTPException(404, '知识库不存在或不可访问。')
        binding, knowledge = row
        if not (
            knowledge.user_id == user.id
            or (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
            or await AccessGrants.has_access(
                user_id=user.id, resource_type='knowledge', resource_id=resource_id,
                permission=permission, db=session,
            )
        ):
            raise HTTPException(404, '知识库不存在或不可访问。')
        if ready and (binding.state != 'ready' or not binding.engine_id):
            raise HTTPException(409, '知识库尚未就绪，请检查操作记录；不要重复导入。')
        return binding, knowledge


async def is_managed(resource_id, *, db=None):
    async with get_async_db_context(db) as session:
        return await session.get(ThereKnowledge, resource_id) is not None
