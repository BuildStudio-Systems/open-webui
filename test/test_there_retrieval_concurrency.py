"""Query scheduling tests with the real adapter and injected ACL/engine boundary."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from fastapi import HTTPException
import pytest


@pytest.fixture
def retrieval(monkeypatch):
    access = ModuleType("open_webui.there_integration.access")
    state = SimpleNamespace(permissions=[], queries=[], active=0, peak=0)

    async def binding(resource_id, user):
        state.permissions.append((resource_id, user.id))
        if user.id != "owner":
            raise HTTPException(404, "Not found")
        return SimpleNamespace(engine_id="kb-owner"), SimpleNamespace(name="Library")

    access.get_binding = binding
    path = Path(__file__).resolve().parents[1] / "backend/open_webui/there_integration/retrieval.py"
    spec = importlib.util.spec_from_file_location("isolated_there_retrieval", path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, "open_webui.there_integration.access", access)
        spec.loader.exec_module(module)
    return module, state


def row(query, *, kb="kb-owner", score=0.9):
    return {"id": query, "knowledge_id": "doc", "knowledge_base_id": kb, "content": query, "score": score}


@pytest.mark.asyncio
async def test_three_searches_overlap_but_preserve_query_order_and_scope(retrieval, monkeypatch):
    module, state = retrieval
    all_started = asyncio.Event()

    class Engine:
        async def search(self, kb, query, limit):
            assert state.permissions == [("local", "owner")]
            assert kb == "kb-owner" and limit == 20
            state.queries.append(query)
            state.active += 1
            state.peak = max(state.peak, state.active)
            if state.active == 3:
                all_started.set()
            try:
                await asyncio.wait_for(all_started.wait(), timeout=1)
                # Reverse completion order must not change stable score ties.
                await asyncio.sleep({"first": .02, "second": .01, "third": 0}[query])
                return {"data": [row(query), row("foreign", kb="other"), row("bad", score=float("nan"))]}
            finally:
                state.active -= 1

    monkeypatch.setattr(module, "WeKnoraClient", Engine)
    result = await module.retrieve("local", ["first", "second", "third", "never"], 100, SimpleNamespace(id="owner"))
    assert state.peak == 3 and state.active == 0
    assert state.queries == ["first", "second", "third"]
    assert result["documents"] == [["first", "second", "third"]]
    assert all(item["knowledge_id"] == "local" for item in result["metadatas"][0])


@pytest.mark.asyncio
async def test_duplicate_truncated_queries_only_dispatched_once(retrieval, monkeypatch):
    module, state = retrieval

    class Engine:
        async def search(self, kb, query, limit):
            state.queries.append(query)
            return {"data": {"results": [row("same")]}}

    monkeypatch.setattr(module, "WeKnoraClient", Engine)
    result = await module.retrieve("local", ["x" * 2000 + "a", "x" * 2000 + "b", "x" * 2000], 5, SimpleNamespace(id="owner"))
    assert state.queries == ["x" * 2000]
    assert result["documents"] == [["same"]]


@pytest.mark.asyncio
async def test_blank_queries_are_skipped_and_input_consumption_is_bounded(retrieval, monkeypatch):
    module, state = retrieval

    def queries():
        yield ""
        yield None
        yield "  "
        raise AssertionError("Must not consume past the original three-query limit")

    monkeypatch.setattr(module, "WeKnoraClient", lambda: pytest.fail("No valid query"))
    result = await module.retrieve("local", queries(), 5, SimpleNamespace(id="owner"))
    assert result["documents"] == [[]]


@pytest.mark.asyncio
async def test_authorization_is_not_cached_and_precedes_all_engine_work(retrieval, monkeypatch):
    module, state = retrieval
    monkeypatch.setattr(module, "WeKnoraClient", lambda: pytest.fail("Unauthorized engine request"))
    for user, status in [(None, 401), (SimpleNamespace(id="other"), 404)]:
        with pytest.raises(HTTPException) as error:
            await module.retrieve("local", ["query"], 5, user)
        assert error.value.status_code == status
    assert state.permissions == [("local", "other")]


@pytest.mark.asyncio
async def test_failure_preserves_original_error_and_cancels_pending_siblings(retrieval, monkeypatch):
    module, state = retrieval
    error = RuntimeError("synthetic engine failure")
    all_started = asyncio.Event()
    cancelled = []

    class Engine:
        async def search(self, kb, query, limit):
            state.queries.append(query)
            if len(state.queries) == 3:
                all_started.set()
            await all_started.wait()
            if query == "fail":
                raise error
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(query)

    monkeypatch.setattr(module, "WeKnoraClient", Engine)
    with pytest.raises(RuntimeError) as raised:
        await asyncio.wait_for(module.retrieve("local", ["wait1", "fail", "wait2"], 5, SimpleNamespace(id="owner")), 1)
    assert raised.value is error
    assert sorted(cancelled) == ["wait1", "wait2"]


@pytest.mark.asyncio
async def test_parent_cancellation_waits_for_all_search_cleanup(retrieval, monkeypatch):
    module, state = retrieval
    all_started = asyncio.Event()
    finished = []

    class Engine:
        async def search(self, kb, query, limit):
            state.queries.append(query)
            if len(state.queries) == 3:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(query)

    monkeypatch.setattr(module, "WeKnoraClient", Engine)
    task = asyncio.create_task(module.retrieve("local", ["a", "b", "c"], 5, SimpleNamespace(id="owner")))
    await asyncio.wait_for(all_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(finished) == ["a", "b", "c"]
