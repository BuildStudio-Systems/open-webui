"""THERE's native workspace API; no browser/LLM can choose engine credentials."""

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.groups import Groups
from open_webui.models.knowledge import Knowledge
from open_webui.models.skills import Skill
from open_webui.models.there import ThereKnowledge, ThereOperation, TherePaper, ThereSkillOrigin
from open_webui.there_integration import VERSION, catalog, research
from open_webui.there_integration.access import get_binding, require_workspace
from open_webui.there_integration.retrieval import authorized_rows
from open_webui.there_integration.weknora import WeKnoraClient, WeKnoraError, default_embedding_model_id
from open_webui.utils.auth import get_verified_user


async def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(dependencies=[Depends(no_store)])


class Form(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class KnowledgeForm(Form):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default='', max_length=2000)
    base_type: Literal['document', 'faq'] = 'document'


class ManualForm(Form):
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=200000)


class SearchForm(Form):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class ActivateForm(Form):
    digest: str = Field(pattern=r'^sha256-[a-f0-9]{64}$')
    reviewed: bool


class PaperForm(Form):
    title: str = Field(min_length=1, max_length=1024)
    url: str = Field(min_length=1, max_length=2048)
    authors: list[str] = Field(default_factory=list, max_length=100)
    year: str | int | None = None
    source: str = Field(default='', max_length=128)
    abstract: str = Field(default='', max_length=50000)

    @field_validator('url')
    @classmethod
    def safe_link(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('An HTTP(S) citation URL without credentials is required')
        return value


def item(binding, knowledge):
    return dict(id=knowledge.id, name=knowledge.name, description=knowledge.description,
                created_at=knowledge.created_at, state=binding.state,
                base_type=(knowledge.meta or {}).get('there', {}).get('base_type', 'document'))


def engine_data(envelope):
    return envelope.get('data') if isinstance(envelope, dict) else None


def engine_items(envelope):
    data = engine_data(envelope)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('items', 'data', 'results', 'knowledge', 'chunks'):
            if isinstance(data.get(key), list):
                return data[key]
    return []


async def read_engine(awaitable):
    try:
        return await awaitable
    except WeKnoraError as error:
        raise HTTPException(error.status_code, error.message) from None


async def begin_operation(db, user, action, resource_id, request_id):
    try:
        request_id = str(UUID(request_id)) if request_id else str(uuid4())
    except ValueError:
        raise HTTPException(422, 'Idempotency-Key 必须是 UUID。') from None
    existing = (await db.execute(select(ThereOperation).where(
        ThereOperation.user_id == user.id, ThereOperation.request_id == request_id
    ))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, {'message': '此请求已有操作记录，请检查结果后再操作。',
                                  'operation_id': existing.id, 'state': existing.state})
    now = int(time.time())
    operation = ThereOperation(id=str(uuid4()), request_id=request_id, user_id=user.id,
        resource_id=resource_id, action=action, state='pending', created_at=now, updated_at=now)
    db.add(operation)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, '此请求已提交。') from None
    return operation


async def write_engine(db, operation, awaitable):
    """Record intent before I/O. Timeouts are NOT safe to automatically replay."""
    try:
        response = await awaitable
    except WeKnoraError as error:
        operation.state = 'unknown' if error.status_code >= 500 else 'failed'
        operation.error_code = error.code[:64]
        operation.updated_at = int(time.time())
        await db.commit()
        raise HTTPException(error.status_code, {'message': error.message,
            'operation_id': operation.id, 'state': operation.state}) from None
    operation.state = 'succeeded'
    operation.updated_at = int(time.time())
    # The caller commits this together with the canonical mapping change.
    return response


@router.get('/status')
async def status(user=Depends(get_verified_user)):
    modules = []
    try:
        await WeKnoraClient().list_bases()
        modules.append(dict(id='knowledge', name='知识引擎', state='ready',
                            detail='文档入库、状态与混合检索；Wiki / GraphRAG 尚待独立模型验收'))
    except (WeKnoraError, ValueError, OSError):
        modules.append(dict(id='knowledge', name='知识引擎', state='unavailable', detail='内部连接或授权未就绪'))
    try:
        result = await asyncio.to_thread(catalog.search, '', 1)
        modules.append(dict(id='skills', name='技能目录', state='ready',
                            detail=f"{result.get('total', 0)} 项；审阅后导入个人原生技能"))
    except (ValueError, OSError, RuntimeError):
        modules.append(dict(id='skills', name='技能目录', state='unavailable', detail='固定版本目录不可用'))
    modules.append(dict(id='research', name='论文检索', state='configured',
                        detail='使用项目内置八源适配器；每次查询返回各源实时状态'))
    return {'version': VERSION, 'modules': modules}


@router.get('/knowledge')
async def list_knowledge(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    query = select(Knowledge).join(ThereKnowledge, ThereKnowledge.id == Knowledge.id)
    if not (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL):
        groups = {g.id for g in await Groups.get_groups_by_member_id(user.id, db=db)}
        query = AccessGrants.has_permission_filter(db=db, query=query, DocumentModel=Knowledge,
            filter={'user_id': user.id, 'group_ids': groups}, resource_type='knowledge', permission='read')
    knowledge = (await db.execute(query.order_by(Knowledge.created_at.desc()).limit(200))).scalars().all()
    bindings = {row.id: row for row in (await db.execute(
        select(ThereKnowledge).where(ThereKnowledge.id.in_([k.id for k in knowledge]))
    )).scalars()}
    return {'items': [item(bindings[k.id], k) for k in knowledge]}


@router.post('/knowledge', status_code=201)
async def create_knowledge(form: KnowledgeForm, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session), idempotency_key: str | None = Header(None)):
    await require_workspace(user, 'knowledge', db)
    try:
        embedding_model_id = default_embedding_model_id()
    except WeKnoraError as error:
        raise HTTPException(error.status_code, error.message) from None
    resource_id, now = str(uuid4()), int(time.time())
    operation = await begin_operation(db, user, 'knowledge.create', resource_id, idempotency_key)
    knowledge = Knowledge(id=resource_id, user_id=user.id, name=form.name, description=form.description,
        meta={'source': 'external', 'external': {'connection_id': 'there-managed'},
              'there': {'engine': 'weknora', 'base_type': form.base_type}}, created_at=now, updated_at=now)
    binding = ThereKnowledge(id=resource_id, state='provisioning', created_at=now, updated_at=now)
    db.add(knowledge)
    await db.flush()
    db.add(binding)
    await db.commit()
    try:
        result = await write_engine(db, operation, WeKnoraClient().create_base(
            name=form.name, description=f'{form.description}\n[there:{resource_id}]', base_type=form.base_type,
            embedding_model_id=embedding_model_id))
        data = engine_data(result)
        if (not isinstance(data, dict) or not isinstance(data.get('id'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', data['id'])):
            operation.state = 'unknown'
            operation.error_code = 'INVALID_ENGINE_RESPONSE'
            raise HTTPException(502, '知识库创建结果需要管理员核对。')
        binding.engine_id, binding.state = data['id'], 'ready'
    except HTTPException:
        binding.state = 'unknown' if operation.state in ('pending', 'unknown') else 'failed'
        await db.commit()
        raise
    await db.commit()
    return {**item(binding, knowledge), 'operation_id': operation.id}


@router.delete('/knowledge/{id}')
async def delete_knowledge(id: str, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session), idempotency_key: str | None = Header(None)):
    await require_workspace(user, 'knowledge', db)
    binding, knowledge = await get_binding(id, user, 'write', db=db)
    operation = await begin_operation(db, user, 'knowledge.delete', id, idempotency_key)
    await write_engine(db, operation, WeKnoraClient().delete_base(binding.engine_id))
    await AccessGrants.revoke_all_access('knowledge', id, db=db)
    await db.delete(binding)
    await db.flush()
    await db.delete(knowledge)
    await db.commit()
    return {'deleted': True, 'operation_id': operation.id}


@router.get('/knowledge/{id}/documents')
async def documents(id: str, page: int = Query(1, ge=1), user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session)):
    binding, _ = await get_binding(id, user, db=db)
    result = await read_engine(WeKnoraClient().list_documents(binding.engine_id, page=page, page_size=50))
    return {'items': engine_items(result), 'page': page, 'total': result.get('total')}


@router.post('/knowledge/{id}/documents/manual', status_code=201)
async def manual(id: str, form: ManualForm, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session), idempotency_key: str | None = Header(None)):
    await require_workspace(user, 'knowledge', db)
    binding, _ = await get_binding(id, user, 'write', db=db)
    operation = await begin_operation(db, user, 'document.create', id, idempotency_key)
    result = await write_engine(db, operation, WeKnoraClient().create_manual_document(
        binding.engine_id, title=form.title, content=form.content))
    await db.commit()
    return {'data': engine_data(result), 'operation_id': operation.id}


@router.post('/knowledge/{id}/documents/file', status_code=201)
async def upload(id: str, file: UploadFile = File(...), user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session), idempotency_key: str | None = Header(None)):
    await require_workspace(user, 'knowledge', db)
    binding, _ = await get_binding(id, user, 'write', db=db)
    content = await file.read(20 * 1024 * 1024 + 1)
    await file.close()
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, '文件需非空且不超过 20 MiB。')
    operation = await begin_operation(db, user, 'document.upload', id, idempotency_key)
    result = await write_engine(db, operation, WeKnoraClient().upload_document(binding.engine_id,
        filename=(file.filename or 'document').replace('\\', '/').rsplit('/', 1)[-1],
        content=content, content_type=file.content_type or 'application/octet-stream'))
    await db.commit()
    return {'data': engine_data(result), 'operation_id': operation.id}


@router.delete('/knowledge/{id}/documents/{document_id}')
async def delete_document(id: str, document_id: str, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session), idempotency_key: str | None = Header(None)):
    await require_workspace(user, 'knowledge', db)
    binding, _ = await get_binding(id, user, 'write', db=db)
    operation = await begin_operation(db, user, 'document.delete', id, idempotency_key)
    await write_engine(db, operation, WeKnoraClient().delete_document(binding.engine_id, document_id))
    await db.commit()
    return {'deleted': True, 'operation_id': operation.id}


@router.post('/knowledge/{id}/search')
async def search(id: str, form: SearchForm, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session)):
    binding, _ = await get_binding(id, user, db=db)
    result = await read_engine(WeKnoraClient().search(binding.engine_id, form.query, limit=form.limit))
    return {'items': authorized_rows(engine_items(result), binding.engine_id)}


@router.get('/catalog')
async def search_catalog(q: str = Query('', max_length=256), limit: int = Query(30, ge=1, le=100),
    user=Depends(get_verified_user)):
    try:
        return await asyncio.to_thread(catalog.search, q, limit)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(503, '技能目录当前不可用。') from None


@router.get('/catalog/{id:path}')
async def get_skill(id: str, user=Depends(get_verified_user)):
    try:
        return await asyncio.to_thread(catalog.get_skill, id)
    except (ValueError, OSError, RuntimeError, KeyError):
        raise HTTPException(404, '技能不存在或固定版本目录不可用。') from None


@router.post('/catalog/{id:path}/activate')
async def activate_skill(id: str, form: ActivateForm, user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session)):
    await require_workspace(user, 'skills', db)
    if not form.reviewed:
        raise HTTPException(422, '请先阅读并确认技能正文。')
    source = await get_skill(id, user)
    if source['digest'] != form.digest:
        raise HTTPException(409, '技能版本已变化，请重新阅读。')
    skill_id = 'aas-' + hashlib.sha256(f'{user.id}\0{id}'.encode()).hexdigest()[:32]
    existing = await db.get(Skill, skill_id)
    if existing:
        if existing.user_id != user.id:
            raise HTTPException(409, '技能 ID 冲突。')
        return {'skill_id': existing.id, 'name': existing.name, 'existing': True}
    now = int(time.time())
    name = f"{source['name']} · {hashlib.sha256(user.id.encode()).hexdigest()[:8]}"
    db.add(Skill(id=skill_id, user_id=user.id, name=name,
        description=source.get('description', ''), content=source['content'],
        meta={'tags': ['aas', 'reviewed']}, is_active=True, updated_at=now, created_at=now))
    try:
        await db.flush()
        db.add(ThereSkillOrigin(skill_id=skill_id, catalog_id=id, digest=source['digest'],
            version=source['version'], reviewed_by=user.id, reviewed_at=now))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, '技能已存在，请刷新个人技能列表。') from None
    return {'skill_id': skill_id, 'name': name}


@router.get('/research')
async def search_research(q: str = Query(min_length=1, max_length=500), limit: int = Query(10, ge=1, le=30),
    user=Depends(get_verified_user)):
    try:
        return await research.search(q, limit=limit)
    except (ValueError, RuntimeError, OSError, TimeoutError):
        raise HTTPException(503, '论文数据源暂不可用，请稍后重试。') from None


@router.get('/papers')
async def list_papers(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(select(TherePaper).where(TherePaper.user_id == user.id)
        .order_by(TherePaper.created_at.desc()).limit(200))).scalars()
    return {'items': [{'id': p.id, 'title': p.title, 'url': p.url, **p.data} for p in rows]}


@router.post('/papers', status_code=201)
async def save_paper(form: PaperForm, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    await require_workspace(user, 'knowledge', db)
    existing = (await db.execute(select(TherePaper).where(
        TherePaper.user_id == user.id, TherePaper.url == form.url))).scalar_one_or_none()
    if existing:
        return {'id': existing.id, 'existing': True}
    paper = TherePaper(id=str(uuid4()), user_id=user.id, title=form.title, url=form.url,
        data=form.model_dump(exclude={'title', 'url'}), created_at=int(time.time()))
    db.add(paper)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, '该论文已收藏。') from None
    return {'id': paper.id}


@router.get('/operations')
async def operations(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(select(ThereOperation).where(ThereOperation.user_id == user.id)
        .order_by(ThereOperation.created_at.desc()).limit(100))).scalars()
    return {'items': [dict(id=o.id, resource_id=o.resource_id, action=o.action, state=o.state,
        error_code=o.error_code, created_at=o.created_at, updated_at=o.updated_at) for o in rows]}
