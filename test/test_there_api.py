"""THERE API integration tests with real SQLite persistence and access grants.

Run in an isolated pytest process with PYTHONPATH=frontend/backend. Production
credentials, databases and engine endpoints are never used by these tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest


@pytest.fixture(scope="module")
def modules(tmp_path_factory):
    environment = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("there-api-import")
    for key, value in {
        "DATA_DIR": str(root / "data"), "STATIC_DIR": str(root / "static"),
        "FRONTEND_BUILD_DIR": str(root / "no-build"), "DATABASE_URL": f"sqlite:///{root / 'import.sqlite'}",
        "ENABLE_DB_MIGRATIONS": "false", "OFFLINE_MODE": "true",
        "WEBUI_SECRET_KEY": "there-api-isolated-test-secret",
    }.items():
        environment.setenv(key, value)
    from open_webui.internal import db
    from open_webui.models import access_grants, knowledge, skills, there, users
    from open_webui.routers import there as api
    from open_webui.there_integration import access, retrieval
    from open_webui.retrieval import external
    yield SimpleNamespace(db=db, grants=access_grants, knowledge=knowledge, skills=skills,
        models=there, users=users, api=api, access=access, retrieval=retrieval, external=external)
    environment.undo()


class Engine:
    def __init__(self, error_type):
        self.error_type = error_type
        self.calls = []
        self.create_error = None
        self.create_result = None
        self.search_rows = []

    async def create_base(self, **kwargs):
        self.calls.append(("create_base", kwargs))
        if self.create_error:
            raise self.create_error
        return self.create_result if self.create_result is not None else {"data": {"id": "engine-" + str(uuid4())}}

    async def list_bases(self):
        return {"data": []}

    async def list_documents(self, engine_id, **kwargs):
        self.calls.append(("list_documents", engine_id))
        return {"data": [{"id": "document-1", "knowledge_base_id": engine_id, "title": "Private text"}], "total": 1}

    async def search(self, engine_id, query, **kwargs):
        self.calls.append(("search", engine_id, query))
        return {"data": self.search_rows or [{"id": "chunk-1", "knowledge_id": "document-1", "knowledge_base_id": engine_id, "content": "Private answer", "score": 0.8}]}

    async def create_manual_document(self, engine_id, **kwargs):
        self.calls.append(("manual", engine_id, kwargs))
        return {"data": {"id": "document-1", "knowledge_base_id": engine_id}}

    async def delete_base(self, engine_id):
        self.calls.append(("delete_base", engine_id))
        return {"success": True}

    async def delete_document(self, engine_id, document_id):
        self.calls.append(("delete_document", engine_id, document_id))
        return {"success": True}


@asynccontextmanager
async def harness(modules, monkeypatch, tmp_path):
    from fastapi import FastAPI, HTTPException, Request
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'there-api.sqlite'}")

    @event.listens_for(database.sync_engine, 'connect')
    def foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    async with database.begin() as connection:
        await connection.run_sync(modules.db.Base.metadata.create_all)
    sessions = async_sessionmaker(database, expire_on_commit=False)
    monkeypatch.setattr(modules.db, 'AsyncSessionLocal', sessions)

    @asynccontextmanager
    async def db_context(existing=None):
        if existing is not None:
            yield existing
        else:
            async with sessions() as session:
                yield session

    async def session_dependency():
        async with sessions() as session:
            yield session

    async def user_dependency(request: Request):
        identity = request.headers.get('X-Test-User')
        if not identity:
            raise HTTPException(401, 'Test authentication required')
        return SimpleNamespace(id=identity, role='admin' if identity == 'admin' else 'user')

    user_dependency.__annotations__['request'] = Request

    permissions = {'workspace': {'knowledge': True, 'skills': True}}

    async def config_get(key, default=None):
        return permissions if key == 'user.permissions' else default

    engine = Engine(modules.api.WeKnoraError)
    monkeypatch.setattr(modules.api, 'WeKnoraClient', lambda: engine)
    monkeypatch.setattr(modules.retrieval, 'WeKnoraClient', lambda: engine)
    monkeypatch.setattr(modules.access, 'get_async_db_context', db_context)
    monkeypatch.setattr(modules.access.Config, 'get', config_get)
    monkeypatch.setattr(modules.api, 'BYPASS_ADMIN_ACCESS_CONTROL', False)
    monkeypatch.setattr(modules.access, 'BYPASS_ADMIN_ACCESS_CONTROL', False)
    app = FastAPI()
    app.include_router(modules.api.router, prefix='/api/v1/there')
    app.dependency_overrides[modules.api.get_async_session] = session_dependency
    app.dependency_overrides[modules.api.get_verified_user] = user_dependency
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url='http://there.test') as client:
            yield SimpleNamespace(client=client, sessions=sessions, engine=engine, permissions=permissions,
                app=app, db_context=db_context)
    finally:
        await database.dispose()


def headers(user='alice', request_id=None):
    result = {'X-Test-User': user}
    if request_id:
        result['Idempotency-Key'] = request_id
    return result


async def create(context, user='alice', request_id=None):
    response = await context.client.post('/api/v1/there/knowledge', json={'name': 'Private notes', 'description': 'Only the owner'}, headers=headers(user, request_id))
    assert response.status_code == 201, response.text
    return response.json()


def test_knowledge_is_private_and_cross_user_requests_never_call_engine(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            calls = len(context.engine.calls)
            for suffix, method in [('documents', 'get'), ('search', 'post')]:
                args = {'json': {'query': 'private'}} if method == 'post' else {}
                response = await getattr(context.client, method)(f"/api/v1/there/knowledge/{resource['id']}/{suffix}", headers=headers('bob'), **args)
                assert response.status_code == 404
            response = await context.client.delete(f"/api/v1/there/knowledge/{resource['id']}", headers=headers('bob'))
            assert response.status_code == 404
            assert len(context.engine.calls) == calls
            for user, count in [('alice', 1), ('bob', 0), ('admin', 0)]:
                response = await context.client.get('/api/v1/there/knowledge', headers=headers(user))
                assert response.status_code == 200, response.text
                assert len(response.json()['items']) == count
            async with context.sessions() as session:
                from sqlalchemy import select
                assert not (await session.execute(select(modules.grants.AccessGrant))).scalars().all()
            assert response.headers['Cache-Control'] == 'no-store'
    asyncio.run(scenario())


@pytest.mark.parametrize('current,legacy,expected', [
    (None, None, 'buildstudio-weknora-bge-m3'),
    ('configured-model', None, 'configured-model'),
    (None, 'legacy-model', 'legacy-model'),
    ('same-model', 'same-model', 'same-model'),
    ('configured-model', '', 'configured-model'),
    ('', 'legacy-model', 'legacy-model'),
    ('', '', 'buildstudio-weknora-bge-m3'),
])
def test_create_knowledge_sends_configured_embedding_model_to_engine(
    modules, monkeypatch, tmp_path, current, legacy, expected,
):
    from open_webui.there_integration.weknora import WeKnoraClient

    for key, value in [('THERE_WEKNORA_EMBEDDING_MODEL_ID', current), ('THERE_WEKNORA_EMBEDDING_MODEL', legacy)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    requests = []

    def dispatch(request):
        requests.append(request)
        return httpx.Response(201, json={'data': {'id': 'configured-base'}})

    client = WeKnoraClient(transport=httpx.MockTransport(dispatch))
    monkeypatch.setattr(client, '_read_key', lambda: 'isolated-test-api-key')

    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            monkeypatch.setattr(modules.api, 'WeKnoraClient', lambda: client)
            resource = await create(context)
            assert resource['state'] == 'ready'
            assert len(requests) == 1
            assert requests[0].method == 'POST'
            assert requests[0].url.path == '/api/v1/knowledge-bases'
            assert json.loads(requests[0].content)['embedding_model_id'] == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('current,legacy', [
    ('configured-model', 'conflicting-legacy-model'),
    ('invalid/model', ''),
    ('', 'invalid model'),
])
def test_model_configuration_error_precedes_create_side_effects(modules, monkeypatch, tmp_path, current, legacy):
    monkeypatch.setenv('THERE_WEKNORA_EMBEDDING_MODEL_ID', current)
    monkeypatch.setenv('THERE_WEKNORA_EMBEDDING_MODEL', legacy)

    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            request_id = str(uuid4())
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(request_id=request_id), json={'name': 'Configured library'})
            assert response.status_code == 503, response.text
            if current and legacy:
                assert 'THERE_WEKNORA_EMBEDDING_MODEL_ID conflicts' in response.json()['detail']
            assert context.engine.calls == []
            async with context.sessions() as session:
                from sqlalchemy import select
                for model in (modules.knowledge.Knowledge, modules.models.ThereKnowledge, modules.models.ThereOperation):
                    assert not (await session.execute(select(model))).scalars().all()
            monkeypatch.setenv('THERE_WEKNORA_EMBEDDING_MODEL_ID', 'fixed-model')
            monkeypatch.delenv('THERE_WEKNORA_EMBEDDING_MODEL', raising=False)
            await create(context, request_id=request_id)
            assert len(context.engine.calls) == 1
    asyncio.run(scenario())


def test_read_sharing_does_not_grant_writes(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            async with context.sessions() as session:
                session.add(modules.grants.AccessGrant(id=str(uuid4()), resource_type='knowledge', resource_id=resource['id'], principal_type='user', principal_id='bob', permission='read', created_at=int(time.time())))
                await session.commit()
            response = await context.client.get(f"/api/v1/there/knowledge/{resource['id']}/documents", headers=headers('bob'))
            assert response.status_code == 200
            response = await context.client.get('/api/v1/there/knowledge', headers=headers('bob'))
            assert len(response.json()['items']) == 1
            calls = len(context.engine.calls)
            response = await context.client.post(f"/api/v1/there/knowledge/{resource['id']}/documents/manual", headers=headers('bob'), json={'title': 'Illegal', 'content': 'Write'})
            assert response.status_code == 404
            assert len(context.engine.calls) == calls
    asyncio.run(scenario())


def test_owner_delete_removes_binding_and_grants_but_preserves_journal(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            async with context.sessions() as session:
                session.add(modules.grants.AccessGrant(id=str(uuid4()), resource_type='knowledge', resource_id=resource['id'], principal_type='user', principal_id='bob', permission='read', created_at=int(time.time())))
                await session.commit()
            response = await context.client.delete(f"/api/v1/there/knowledge/{resource['id']}", headers=headers(request_id=str(uuid4())))
            assert response.status_code == 200, response.text
            async with context.sessions() as session:
                from sqlalchemy import select
                assert await session.get(modules.models.ThereKnowledge, resource['id']) is None
                assert await session.get(modules.knowledge.Knowledge, resource['id']) is None
                assert not (await session.execute(select(modules.grants.AccessGrant))).scalars().all()
                operations = (await session.execute(select(modules.models.ThereOperation))).scalars().all()
                assert len(operations) == 2
                assert {operation.state for operation in operations} == {'succeeded'}
    asyncio.run(scenario())


def test_auth_and_workspace_permissions_are_required(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            for route in ('status', 'knowledge', 'catalog', 'research?q=topic', 'papers', 'operations'):
                response = await context.client.get('/api/v1/there/' + route)
                assert response.status_code == 401
            context.permissions['workspace']['knowledge'] = False
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(), json={'name': 'Unauthorized'})
            assert response.status_code == 403
            assert context.engine.calls == []
    asyncio.run(scenario())


def test_idempotency_duplicate_is_journaled_once_and_operation_private(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            request_id = str(uuid4())
            resource = await create(context, request_id=request_id)
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(request_id=request_id), json={'name': 'duplicate'})
            assert response.status_code == 409
            assert response.json()['detail']['operation_id'] == resource['operation_id']
            assert len(context.engine.calls) == 1
            for user, count in [('alice', 1), ('bob', 0)]:
                response = await context.client.get('/api/v1/there/operations', headers=headers(user))
                assert len(response.json()['items']) == count
            await create(context, user='bob', request_id=request_id)
            assert len(context.engine.calls) == 2
    asyncio.run(scenario())


def test_concurrent_duplicate_create_has_only_one_engine_side_effect(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            request_id = str(uuid4())
            responses = await asyncio.gather(*[
                context.client.post('/api/v1/there/knowledge', headers=headers(request_id=request_id), json={'name': 'Concurrent'})
                for _ in range(2)
            ])
            assert sorted(response.status_code for response in responses) == [201, 409]
            assert len(context.engine.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('failure_kind', ['timeout', 'rejected', 'malformed'])
def test_uncertain_engine_create_is_not_replayed_or_searchable(modules, monkeypatch, tmp_path, failure_kind):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            expected_state = 'failed' if failure_kind == 'rejected' else 'unknown'
            if failure_kind == 'malformed':
                context.engine.create_result = {'data': {}}
            else:
                context.engine.create_error = modules.api.WeKnoraError('TEST_FAILURE', 422 if failure_kind == 'rejected' else 504)
            request_id = str(uuid4())
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(request_id=request_id), json={'name': 'Uncertain'})
            assert response.status_code == (422 if failure_kind == 'rejected' else 502 if failure_kind == 'malformed' else 504)
            listing = (await context.client.get('/api/v1/there/knowledge', headers=headers())).json()['items']
            assert listing[0]['state'] == expected_state
            journal = (await context.client.get('/api/v1/there/operations', headers=headers())).json()['items']
            assert journal[0]['state'] == expected_state
            response = await context.client.post(f"/api/v1/there/knowledge/{listing[0]['id']}/search", headers=headers(), json={'query': 'test'})
            assert response.status_code == 409
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(request_id=request_id), json={'name': 'Uncertain'})
            assert response.status_code == 409
            assert len(context.engine.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid_id', [None, True, 123, {}, [], '', '../outside', 'bad/id', 'bad id', 'x' * 129])
def test_malformed_engine_ids_do_not_become_canonical_bindings(modules, monkeypatch, tmp_path, invalid_id):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            context.engine.create_result = {'data': {'id': invalid_id}}
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(request_id=str(uuid4())), json={'name': 'Invalid upstream ID'})
            assert response.status_code == 502, response.text
            async with context.sessions() as session:
                from sqlalchemy import select
                bindings = (await session.execute(select(modules.models.ThereKnowledge))).scalars().all()
                operations = (await session.execute(select(modules.models.ThereOperation))).scalars().all()
                assert len(bindings) == len(operations) == 1
                assert bindings[0].engine_id is None
                assert bindings[0].state == operations[0].state == 'unknown'
                assert operations[0].error_code == 'INVALID_ENGINE_RESPONSE'
    asyncio.run(scenario())


def test_skill_review_digest_is_revalidated_and_import_is_private(modules, monkeypatch, tmp_path):
    source = {'id': 'test-skill', 'name': 'Test skill', 'description': 'Source', 'content': 'Reviewed body', 'version': '16.9.1', 'digest': 'sha256-' + hashlib.sha256(b'Reviewed body').hexdigest()}
    monkeypatch.setattr(modules.api.catalog, 'get_skill', lambda skill_id: dict(source))
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            for reviewed, digest, expected in [(False, source['digest'], 422), (True, 'sha256-' + '0' * 64, 409)]:
                response = await context.client.post('/api/v1/there/catalog/test-skill/activate', headers=headers(), json={'reviewed': reviewed, 'digest': digest})
                assert response.status_code == expected
            first = await context.client.post('/api/v1/there/catalog/test-skill/activate', headers=headers(), json={'reviewed': True, 'digest': source['digest']})
            assert first.status_code == 200, first.text
            duplicate = await context.client.post('/api/v1/there/catalog/test-skill/activate', headers=headers(), json={'reviewed': True, 'digest': source['digest']})
            assert duplicate.json()['existing'] is True
            bob = await context.client.post('/api/v1/there/catalog/test-skill/activate', headers=headers('bob'), json={'reviewed': True, 'digest': source['digest']})
            assert bob.json()['skill_id'] != first.json()['skill_id']
            async with context.sessions() as session:
                from sqlalchemy import select
                skill = await session.get(modules.skills.Skill, first.json()['skill_id'])
                origin = await session.get(modules.models.ThereSkillOrigin, skill.id)
                assert skill.user_id == 'alice'
                assert skill.content == source['content']
                assert origin.digest == source['digest'] and origin.reviewed_by == 'alice'
                assert not (await session.execute(select(modules.grants.AccessGrant))).scalars().all()
    asyncio.run(scenario())


def test_concurrent_skill_import_handles_unique_constraint_race(modules, monkeypatch, tmp_path):
    source = {'id': 'race-skill', 'name': 'Race skill', 'description': '', 'content': 'Body', 'version': '16.9.1', 'digest': 'sha256-' + hashlib.sha256(b'Body').hexdigest()}
    monkeypatch.setattr(modules.api.catalog, 'get_skill', lambda skill_id: dict(source))
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            responses = await asyncio.gather(*[
                context.client.post('/api/v1/there/catalog/race-skill/activate', headers=headers(), json={'reviewed': True, 'digest': source['digest']})
                for _ in range(2)
            ])
            assert all(response.status_code in (200, 409) for response in responses)
            assert any(response.status_code == 200 for response in responses)
            async with context.sessions() as session:
                from sqlalchemy import select
                assert len((await session.execute(select(modules.skills.Skill))).scalars().all()) == 1
                assert len((await session.execute(select(modules.models.ThereSkillOrigin))).scalars().all()) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('route_id', ['python/testing', 'python%2Ftesting'])
def test_nested_catalog_ids_round_trip_through_real_get_and_activate_routes(modules, monkeypatch, tmp_path, route_id):
    source = {'id': 'python/testing', 'name': 'Python testing', 'description': '', 'content': 'Nested skill', 'version': '16.9.1', 'digest': 'sha256-' + hashlib.sha256(b'Nested skill').hexdigest()}
    requested = []

    def get_skill(skill_id):
        requested.append(skill_id)
        assert skill_id == source['id']
        return dict(source)

    monkeypatch.setattr(modules.api.catalog, 'get_skill', get_skill)
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            response = await context.client.get('/api/v1/there/catalog/' + route_id, headers=headers())
            assert response.status_code == 200, response.text
            assert response.json()['id'] == source['id']
            response = await context.client.post('/api/v1/there/catalog/' + route_id + '/activate', headers=headers(), json={'reviewed': True, 'digest': source['digest']})
            assert response.status_code == 200, response.text
            async with context.sessions() as session:
                origin = await session.get(modules.models.ThereSkillOrigin, response.json()['skill_id'])
                assert origin.catalog_id == source['id']
            assert requested == [source['id'], source['id']]
    asyncio.run(scenario())


def test_paper_saves_are_private_and_deduplicated_per_user(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            paper = {'title': 'A paper', 'url': 'https://doi.org/10.1234/a', 'authors': ['Alice'], 'year': 2025, 'source': 'crossref'}
            first = await context.client.post('/api/v1/there/papers', headers=headers(), json=paper)
            duplicate = await context.client.post('/api/v1/there/papers', headers=headers(), json=paper)
            assert first.status_code == duplicate.status_code == 201
            assert first.json()['id'] == duplicate.json()['id']
            assert (await context.client.get('/api/v1/there/papers', headers=headers('bob'))).json()['items'] == []
            assert len((await context.client.get('/api/v1/there/papers', headers=headers())).json()['items']) == 1
            bob = await context.client.post('/api/v1/there/papers', headers=headers('bob'), json=paper)
            assert bob.json()['id'] != first.json()['id']
            for unsafe in ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.com/paper']:
                response = await context.client.post('/api/v1/there/papers', headers=headers(), json={**paper, 'url': unsafe})
                assert response.status_code == 422
    asyncio.run(scenario())


def test_unknown_operations_and_untrusted_engine_fields_are_not_accepted(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            response = await context.client.post('/api/v1/there/operations/delete-all', headers=headers(), json={})
            assert response.status_code in (404, 405)
            response = await context.client.post('/api/v1/there/knowledge', headers=headers(), json={'name': 'Injected', 'engine_id': 'someone-else', 'api_key': 'private'})
            assert response.status_code == 422
            assert not context.engine.calls
    asyncio.run(scenario())


def test_chat_external_hook_uses_authoritative_binding_and_citations(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            async with context.sessions() as session:
                binding = await session.get(modules.models.ThereKnowledge, resource['id'])
                engine_id = binding.engine_id
            context.engine.search_rows = [
                {'id': 'safe', 'knowledge_id': 'document-1', 'knowledge_base_id': engine_id, 'content': 'Owner content', 'knowledge_title': 'Owner document', 'score': 0.9},
                {'id': 'foreign', 'knowledge_id': 'document-2', 'knowledge_base_id': 'foreign-base', 'content': 'NEVER LEAK', 'score': 1.0},
            ]
            # Client-editable metadata deliberately points elsewhere; binding wins.
            knowledge = SimpleNamespace(id=resource['id'], name='Visible', meta={'source': 'external', 'external': {'connection_id': 'attacker-url'}})
            user = SimpleNamespace(id='alice', role='user')
            for queries in [['single chat query'], ['first tool query', 'second tool query']]:
                result = await modules.external.retrieve_external_knowledge(None, knowledge, queries, 5, user=user)
                assert result['documents'] == [['Owner content']]
                metadata = result['metadatas'][0][0]
                assert metadata['knowledge_id'] == resource['id']
                assert metadata['document_id'] == 'document-1'
                assert metadata['source'] == 'Owner document'
                assert metadata['engine'] == 'weknora'
            calls = len(context.engine.calls)
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as failure:
                await modules.external.retrieve_external_knowledge(None, knowledge, ['private'], 5, user=SimpleNamespace(id='bob', role='user'))
            assert failure.value.status_code == 404
            assert len(context.engine.calls) == calls
    asyncio.run(scenario())


def test_workspace_search_drops_foreign_knowledge_base_content(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            async with context.sessions() as session:
                engine_id = (await session.get(modules.models.ThereKnowledge, resource['id'])).engine_id
            context.engine.search_rows = [
                {'id': 'safe', 'knowledge_id': 'document-1', 'knowledge_base_id': engine_id, 'content': 'Owner content', 'score': 0.9},
                {'id': 'foreign', 'knowledge_id': 'document-2', 'knowledge_base_id': 'foreign-base', 'content': 'NEVER LEAK', 'score': 1.0},
            ]
            response = await context.client.post(f"/api/v1/there/knowledge/{resource['id']}/search", headers=headers(), json={'query': 'search'})
            assert response.status_code == 200
            assert [row['content'] for row in response.json()['items']] == ['Owner content']
    asyncio.run(scenario())


def test_malformed_search_rows_are_dropped_in_workspace_and_chat(modules, monkeypatch, tmp_path):
    async def scenario():
        async with harness(modules, monkeypatch, tmp_path) as context:
            resource = await create(context)
            async with context.sessions() as session:
                engine_id = (await session.get(modules.models.ThereKnowledge, resource['id'])).engine_id
            valid = {'id': 'safe', 'knowledge_id': 'document-1', 'knowledge_base_id': engine_id, 'content': 'Valid content', 'score': 0.9}
            malformed = [None, False, 'not a record', [], {},
                {**valid, 'content': None}, {**valid, 'content': {'bad': 'object'}},
                {**valid, 'score': '0.7'}, {**valid, 'score': True},
                {**valid, 'score': float('nan')}, {**valid, 'score': float('inf')},
                {**valid, 'knowledge_id': []}, {**valid, 'id': {}},
            ]
            context.engine.search_rows = [*malformed, valid]
            response = await context.client.post(f"/api/v1/there/knowledge/{resource['id']}/search", headers=headers(), json={'query': 'search'})
            assert response.status_code == 200, response.text
            assert response.json()['items'] == [valid]
            knowledge = SimpleNamespace(id=resource['id'], name='Visible', meta={'source': 'external'})
            result = await modules.external.retrieve_external_knowledge(None, knowledge, ['query'], 5, user=SimpleNamespace(id='alice', role='user'))
            assert result['documents'] == [['Valid content']]
            assert result['distances'] == [[0.9]]
    asyncio.run(scenario())


def test_oversized_integer_score_is_rejected_without_float_overflow(modules):
    row = {'id': 'chunk', 'knowledge_id': 'doc', 'knowledge_base_id': 'engine', 'content': 'Body', 'score': 10 ** 1000}
    assert modules.retrieval.authorized_rows([row], 'engine') == []


def test_additive_migration_preserves_legacy_rows_and_enforces_constraints(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError

    path = Path(__file__).resolve().parents[1] / 'backend/open_webui/migrations/versions/e909a0010001_add_there_integration.py'
    specification = importlib.util.spec_from_file_location('there_test_migration', path)
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    database = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    try:
        with database.begin() as connection:
            connection.execute(text('PRAGMA foreign_keys=ON'))
            connection.execute(text('CREATE TABLE knowledge (id TEXT PRIMARY KEY, name TEXT)'))
            connection.execute(text('CREATE TABLE skill (id TEXT PRIMARY KEY, content TEXT)'))
            connection.execute(text("INSERT INTO knowledge VALUES ('legacy', 'preserve me')"))
            connection.execute(text("INSERT INTO skill VALUES ('existing', 'existing skill')"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) >= {'knowledge', 'skill', 'there_knowledge', 'there_operation', 'there_skill_origin', 'there_paper'}
            assert connection.execute(text("SELECT name FROM knowledge WHERE id='legacy'")).scalar() == 'preserve me'
            assert connection.execute(text("SELECT content FROM skill WHERE id='existing'")).scalar() == 'existing skill'
            with pytest.raises(IntegrityError):
                connection.execute(text("INSERT INTO there_knowledge VALUES ('missing', 'engine', 'ready', 0, 0)"))
            with pytest.raises(RuntimeError, match='reconciled'):
                migration.downgrade()
    finally:
        database.dispose()
