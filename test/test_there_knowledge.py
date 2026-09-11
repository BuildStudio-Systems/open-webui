"""Advanced knowledge routes: API contracts, ACL ordering and input boundaries.

Load the real route and shared form/response/operation-completion helpers without
starting Open WebUI's application database or model dependencies. Database intent
creation and authentication/ACL services are injected at their service boundary.
"""

import ast
import importlib.util
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException, Response
import httpx
from pydantic import BaseModel, ConfigDict
import pytest

from open_webui.there_integration.weknora import WeKnoraError

PREFIX = "/api/v1/there/knowledge/local-kb"
CHUNK = PREFIX + "/documents/doc-1/chunks/chunk-1"
INTENT = "12345678-1234-1234-1234-123456789abc"
WRITE_REQUESTS = [
    ("POST", PREFIX + "/documents/doc-1/reparse", None, "reparse_document"),
    ("PUT", CHUNK, {"content": "Changed", "expected_revision": 3}, "update_chunk"),
    ("POST", CHUNK + "/revert", {"version": 1, "expected_revision": 3}, "revert_chunk"),
    ("POST", PREFIX + "/faq", {"question": "Why?", "answers": ["Because."]}, "create_faq_entry"),
    ("PUT", PREFIX + "/faq/100000001", {"question": "Why?", "answers": ["Because."]}, "update_faq_entry"),
    ("DELETE", PREFIX + "/faq/100000001", None, "delete_faq_entries"),
    ("PUT", PREFIX + "/wiki/page?slug=concept/rag", {"content": "Changed", "expected_version": 3}, "update_wiki_page"),
    ("POST", PREFIX + "/wiki/revert", {"slug": "concept/rag", "version": 1}, "revert_wiki_page"),
    ("POST", PREFIX + "/wiki/pages", {"slug": "concept/rag", "title": "RAG", "content": "Body"}, "create_wiki_page"),
    ("DELETE", PREFIX + "/wiki/page?slug=concept/rag", None, "delete_wiki_page"),
]
READ_REQUESTS = [
    PREFIX + "/documents/doc-1/chunks",
    CHUNK + "/revisions",
    PREFIX + "/faq",
    PREFIX + "/faq/100000001",
    PREFIX + "/wiki/pages",
    PREFIX + "/wiki/page?slug=concept/rag",
    PREFIX + "/wiki/revisions?slug=concept/rag",
    PREFIX + "/wiki/graph",
    PREFIX + "/wiki/search?q=RAG",
    PREFIX + "/wiki/index",
]


@pytest.fixture
def fixture(monkeypatch):
    backend = Path(__file__).resolve().parents[1] / "backend"
    shared_path = backend / "open_webui/routers/there.py"
    selected = {"Form", "no_store", "engine_data", "engine_items", "read_engine", "write_engine"}
    tree = ast.parse(shared_path.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in selected
    ]
    shared = ModuleType("open_webui.routers.there")
    shared.__dict__.update(
        {
            "BaseModel": BaseModel,
            "ConfigDict": ConfigDict,
            "Response": Response,
            "HTTPException": HTTPException,
            "WeKnoraError": WeKnoraError,
            "time": time,
        }
    )
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(shared_path), "exec"), shared.__dict__)

    async def authentication():
        raise HTTPException(401, "Authentication required")

    async def database():
        yield None

    shared.get_verified_user = authentication
    shared.get_async_session = database
    shared.require_workspace = AsyncMock()
    shared.get_binding = AsyncMock()
    shared.begin_operation = AsyncMock()
    monkeypatch.setitem(sys.modules, "open_webui.routers.there", shared)
    name = "_there_knowledge_routes_test"
    spec = importlib.util.spec_from_file_location(name, backend / "open_webui/routers/there_knowledge.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    user = SimpleNamespace(id="verified-user", role="user")
    db = SimpleNamespace(commit=AsyncMock())
    operation = SimpleNamespace(id="operation-1", state="pending", error_code=None, updated_at=0)
    module.require_workspace = AsyncMock()
    module.get_binding = AsyncMock(
        return_value=(SimpleNamespace(engine_id="engine-kb"), SimpleNamespace(id="local-kb"))
    )
    module.begin_operation = AsyncMock(return_value=operation)
    engine = SimpleNamespace(
        **{
            name: AsyncMock(return_value={"success": True, "data": []})
            for name in (
                "list_chunks",
                "list_chunk_revisions",
                "reparse_document",
                "update_chunk",
                "revert_chunk",
                "list_faq_entries",
                "create_faq_entry",
                "get_faq_entry",
                "update_faq_entry",
                "delete_faq_entries",
                "list_wiki_pages",
                "get_wiki_page",
                "update_wiki_page",
                "list_wiki_revisions",
                "revert_wiki_page",
                "get_wiki_graph",
                "create_wiki_page",
                "delete_wiki_page",
                "search_wiki",
                "get_wiki_index",
            )
        }
    )
    module.WeKnoraClient = lambda: engine
    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1/there")

    async def authenticated_user():
        return user

    async def session():
        yield db

    app.dependency_overrides[authentication] = authenticated_user
    app.dependency_overrides[database] = session
    return SimpleNamespace(
        module=module, app=app, user=user, db=db, engine=engine, operation=operation, authentication=authentication
    )


async def request(fixture, method, path, *, json=None, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url="http://test") as client:
        return await client.request(method, path, json=json, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", READ_REQUESTS)
async def test_every_read_route_requires_authenticated_resource_access(fixture, path):
    fixture.module.get_binding.side_effect = HTTPException(404, "Not accessible")
    response = await request(fixture, "GET", path)
    assert response.status_code == 404
    fixture.module.get_binding.assert_awaited_once_with("local-kb", fixture.user, "read", db=fixture.db)
    assert all(method.await_count == 0 for method in vars(fixture.engine).values())
    fixture.module.begin_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body,engine_method", WRITE_REQUESTS)
async def test_every_write_requires_workspace_then_write_acl(fixture, method, path, body, engine_method):
    fixture.module.require_workspace.side_effect = HTTPException(403, "No workspace permission")
    response = await request(fixture, method, path, json=body)
    assert response.status_code == 403
    fixture.module.get_binding.assert_not_awaited()
    fixture.module.begin_operation.assert_not_awaited()
    getattr(fixture.engine, engine_method).assert_not_awaited()
    fixture.module.require_workspace.side_effect = None
    fixture.module.get_binding.side_effect = HTTPException(404, "Not accessible")
    response = await request(fixture, method, path, json=body)
    assert response.status_code == 404
    fixture.module.get_binding.assert_awaited_once_with("local-kb", fixture.user, "write", db=fixture.db)
    fixture.module.begin_operation.assert_not_awaited()
    getattr(fixture.engine, engine_method).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body,engine_method", WRITE_REQUESTS)
async def test_every_write_records_intent_and_propagates_idempotency_key(fixture, method, path, body, engine_method):
    response = await request(fixture, method, path, json=body, headers={"Idempotency-Key": INTENT})
    assert response.status_code in (200, 201)
    assert response.json()["operation_id"] == "operation-1"
    assert fixture.module.begin_operation.await_args.args[4] == INTENT
    assert getattr(fixture.engine, engine_method).await_args.args[0] == "engine-kb"
    assert fixture.operation.state == "succeeded"
    fixture.db.commit.assert_awaited_once()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_mutation_authorizes_before_intent_and_engine_io(fixture):
    events = []

    async def permission(*args):
        events.append("permission")

    async def binding(*args, **kwargs):
        events.append("write-acl")
        return SimpleNamespace(engine_id="engine-kb"), None

    async def intent(*args):
        events.append("durable-intent")
        return fixture.operation

    async def mutate(*args, **kwargs):
        events.append("engine")
        return {"success": True, "data": {"id": "doc-1"}}

    fixture.module.require_workspace.side_effect = permission
    fixture.module.get_binding.side_effect = binding
    fixture.module.begin_operation.side_effect = intent
    fixture.engine.reparse_document.side_effect = mutate
    response = await request(fixture, "POST", PREFIX + "/documents/doc-1/reparse")
    assert response.status_code == 200
    assert events == ["permission", "write-acl", "durable-intent", "engine"]


@pytest.mark.asyncio
async def test_duplicate_intent_stops_engine_submission(fixture):
    fixture.module.begin_operation.side_effect = HTTPException(409, {"operation_id": "previous", "state": "unknown"})
    response = await request(fixture, "POST", PREFIX + "/documents/doc-1/reparse", headers={"Idempotency-Key": INTENT})
    assert response.status_code == 409
    fixture.engine.reparse_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthenticated_caller_cannot_reach_engine_or_acl(fixture):
    fixture.app.dependency_overrides.pop(fixture.authentication)
    response = await request(fixture, "GET", PREFIX + "/faq")
    assert response.status_code == 401
    fixture.module.get_binding.assert_not_awaited()
    fixture.engine.list_faq_entries.assert_not_awaited()


@pytest.mark.asyncio
async def test_faq_ids_are_exact_strings_and_nested_pagination_is_preserved(fixture):
    entry = {
        "id": 9223372036854775807,
        "tag_id": 9007199254740993,
        "standard_question": "Why?",
        "answers": ["Because."],
    }
    fixture.engine.list_faq_entries.return_value = {
        "success": True,
        "data": {"data": [entry], "total": 21, "page": 1, "page_size": 20},
    }
    response = await request(fixture, "GET", PREFIX + "/faq")
    data = response.json()
    assert response.status_code == 200
    assert data["items"][0]["id"] == "9223372036854775807"
    assert data["items"][0]["tag_id"] == "9007199254740993"
    assert data["total"] == 21
    assert data["has_more"] is True


@pytest.mark.asyncio
async def test_faq_int64_path_is_parsed_without_float_rounding(fixture):
    fixture.engine.get_faq_entry.return_value = {"data": {"id": 9223372036854775807}}
    response = await request(fixture, "GET", PREFIX + "/faq/9223372036854775807")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == "9223372036854775807"
    fixture.engine.get_faq_entry.assert_awaited_once_with("engine-kb", 9223372036854775807)


@pytest.mark.asyncio
async def test_chunk_edit_preserves_indentation_and_expected_revision(fixture):
    content = "    code()\n\n"
    response = await request(fixture, "PUT", CHUNK, json={"content": content, "expected_revision": 3})
    assert response.status_code == 200
    fixture.engine.update_chunk.assert_awaited_once_with(
        "engine-kb", "doc-1", "chunk-1", content=content, expected_revision=3
    )


@pytest.mark.asyncio
async def test_chunk_revisions_preserve_preview_and_revert_maps_version(fixture):
    revision = {"revision": 0, "content": "Previous body", "edited_at": "2026-09-09T00:00:00Z"}
    fixture.engine.list_chunk_revisions.return_value = {"success": True, "data": [revision]}
    response = await request(fixture, "GET", CHUNK + "/revisions")
    assert response.json()["items"] == [revision]
    response = await request(fixture, "POST", CHUNK + "/revert", json={"version": 0, "expected_revision": 3})
    assert response.status_code == 200
    fixture.engine.revert_chunk.assert_awaited_once_with(
        "engine-kb", "doc-1", "chunk-1", revision=0, expected_revision=3
    )


@pytest.mark.asyncio
async def test_engine_failure_uses_real_shared_operation_completion_rules(fixture):
    fixture.engine.reparse_document.side_effect = WeKnoraError(
        "WEKNORA_TIMEOUT", 504, "Processing result is uncertain."
    )
    response = await request(fixture, "POST", PREFIX + "/documents/doc-1/reparse")
    assert response.status_code == 504
    assert response.json()["detail"]["state"] == "unknown"
    assert fixture.operation.state == "unknown"
    assert fixture.operation.error_code == "WEKNORA_TIMEOUT"
    fixture.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_wiki_is_reported_as_disabled_not_as_empty_success(fixture):
    fixture.engine.list_wiki_pages.side_effect = WeKnoraError("WEKNORA_WIKI_DISABLED", 409, "Wiki is not enabled.")
    response = await request(fixture, "GET", PREFIX + "/wiki/pages")
    assert response.status_code == 409
    assert response.json()["detail"] == "Wiki is not enabled."


@pytest.mark.asyncio
async def test_wiki_typed_responses_and_revision_guards(fixture):
    fixture.engine.list_wiki_pages.return_value = {"pages": [{"slug": "concept/rag"}], "total": 1}
    response = await request(fixture, "GET", PREFIX + "/wiki/pages")
    assert response.json()["items"] == [{"slug": "concept/rag"}]
    fixture.engine.list_wiki_revisions.return_value = {"revisions": [{"version": 1}], "current_version": 2, "total": 1}
    response = await request(fixture, "GET", PREFIX + "/wiki/revisions?slug=concept/rag")
    assert response.json()["current_version"] == 2
    assert response.json()["items"] == [{"version": 1}]
    response = await request(
        fixture, "PUT", PREFIX + "/wiki/page?slug=concept/rag", json={"content": "New body", "expected_version": 2}
    )
    assert response.status_code == 200
    fixture.engine.update_wiki_page.assert_awaited_once_with(
        "engine-kb", "concept/rag", content="New body", expected_version=2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", CHUNK, {"content": "x"}),
        ("PUT", CHUNK, {"content": " ", "expected_revision": 1}),
        ("PUT", CHUNK, {"content": "x", "expected_revision": True}),
        ("PUT", CHUNK, {"content": "x", "expected_revision": 1, "tenant_id": "spoof"}),
        ("POST", CHUNK + "/revert", {"version": -1, "expected_revision": 3}),
        ("POST", PREFIX + "/faq", {"question": "Why?", "answers": []}),
        ("POST", PREFIX + "/faq", {"question": "Why?", "answers": [" "]}),
        ("POST", PREFIX + "/faq", {"question": "Why?", "answers": ["x"], "knowledge_base_id": "other"}),
        ("GET", PREFIX + "/faq/9223372036854775808", None),
        ("GET", PREFIX + "/faq/0", None),
        ("GET", PREFIX + "/faq?pages=1&page_size=101", None),
        ("GET", PREFIX + "/documents/doc-1/chunks?page=0", None),
        ("GET", PREFIX + "/wiki/page?slug=../private", None),
        ("PUT", PREFIX + "/wiki/page?slug=concept/rag", {"content": "x", "expected_version": 0}),
        ("POST", PREFIX + "/wiki/revert", {"slug": "concept/../private", "version": 1}),
        ("POST", PREFIX + "/wiki/pages", {"slug": "../private", "title": "RAG", "content": "Body"}),
        ("POST", PREFIX + "/wiki/pages", {"slug": "rag", "title": " ", "content": "Body"}),
        ("POST", PREFIX + "/wiki/pages", {"slug": "rag", "title": "RAG", "content": "Body", "tenant_id": "spoof"}),
        ("POST", PREFIX + "/wiki/pages", {"slug": "rag", "title": "RAG", "content": "Body", "status": "public"}),
        ("DELETE", PREFIX + "/wiki/page?slug=../private", None),
        ("GET", PREFIX + "/wiki/search?q=%20", None),
        ("GET", PREFIX + "/wiki/search?q=RAG&limit=51", None),
    ],
)
async def test_bad_inputs_cannot_create_an_intent_or_reach_engine(fixture, method, path, body):
    response = await request(fixture, method, path, json=body)
    assert response.status_code == 422
    fixture.module.begin_operation.assert_not_awaited()
    assert all(method.await_count == 0 for method in vars(fixture.engine).values())


@pytest.mark.asyncio
async def test_wiki_create_preserves_content_and_defaults_to_draft(fixture):
    content = "    code()\n\n"
    response = await request(fixture, "POST", PREFIX + "/wiki/pages", json={
        "slug": "concept/rag", "title": "RAG", "content": content,
    })
    assert response.status_code == 201
    fixture.engine.create_wiki_page.assert_awaited_once_with(
        "engine-kb", slug="concept/rag", title="RAG", content=content,
        page_type="concept", status="draft",
    )


@pytest.mark.asyncio
async def test_wiki_search_and_index_preserve_pinned_envelopes(fixture):
    fixture.engine.search_wiki.return_value = {"pages": [{"slug": "concept/rag", "title": "RAG"}]}
    response = await request(fixture, "GET", PREFIX + "/wiki/search?q=RAG&limit=5")
    assert response.json()["items"] == [{"slug": "concept/rag", "title": "RAG"}]
    fixture.engine.search_wiki.assert_awaited_once_with("engine-kb", "RAG", limit=5)
    fixture.engine.get_wiki_index.return_value = {"content": "Index"}
    response = await request(fixture, "GET", PREFIX + "/wiki/index")
    assert response.json() == {"data": {"content": "Index"}}
    assert response.headers["cache-control"] == "no-store"
