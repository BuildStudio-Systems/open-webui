from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def open_webui_modules(tmp_path_factory):
    # Open WebUI validates its runtime environment while importing. Keep this
    # test isolated from a developer or production database.
    data_dir = tmp_path_factory.mktemp("open-webui-no-store")
    os.environ["DATA_DIR"] = str(data_dir)
    # Keep the fixture value away from 44 characters: Open WebUI treats an
    # exactly-44-character value as an already encoded Fernet key.
    os.environ["WEBUI_SECRET_KEY"] = "local-open-webui-test-secret"

    from open_webui.retrieval.web import utils as web_utils
    from open_webui.routers import retrieval
    from open_webui.utils import middleware

    return retrieval, web_utils, middleware


def _request(*, no_store: bool):
    from starlette.requests import Request

    headers = []
    if no_store:
        headers.append((b"x-buildstudio-no-store", b"true"))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/completions",
            "query_string": b"",
            "headers": headers,
            "scheme": "https",
            "server": ("there.test", 443),
            "client": ("127.0.0.1", 1),
        }
    )


def test_request_scoped_no_store_skips_vector_save_but_keeps_search_and_page_content(open_webui_modules, monkeypatch):
    retrieval, _, _ = open_webui_modules
    config = SimpleNamespace(
        ENABLE_WEB_SEARCH=True,
        USER_PERMISSIONS={},
        WEB_SEARCH_ENGINE="test",
        WEB_SEARCH_CONCURRENT_REQUESTS=0,
        BYPASS_WEB_SEARCH_WEB_LOADER=True,
        BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=False,
        ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER=False,
        TEXT_SPLITTER="",
        CHUNK_SIZE=1000,
        CHUNK_OVERLAP=100,
        CHUNK_MIN_SIZE_TARGET=0,
        TOP_K=3,
    )
    searched_queries: list[str] = []
    vector_saves: list[object] = []

    async def fake_config():
        return config

    async def fake_search(_request, _engine, query, _user):
        searched_queries.append(query)
        return [
            retrieval.SearchResult(
                link="https://example.com/article",
                title="Article",
                snippet="Fetched page text",
            )
        ]

    async def fake_run_in_threadpool(function, *_args, **_kwargs):
        vector_saves.append(function)
        return True

    monkeypatch.setattr(retrieval, "get_retrieval_config", fake_config)
    monkeypatch.setattr(retrieval, "search_web", fake_search)
    monkeypatch.setattr(retrieval, "run_in_threadpool", fake_run_in_threadpool)
    user = SimpleNamespace(id="mini-user", role="admin")

    no_store_result = asyncio.run(
        retrieval.process_web_search(
            _request(no_store=True),
            retrieval.SearchForm(queries=["request scoped search"]),
            user=user,
        )
    )

    assert searched_queries == ["request scoped search"]
    assert vector_saves == []
    assert no_store_result["collection_name"] is None
    assert no_store_result["docs"] == [
        {
            "content": "Fetched page text",
            "metadata": {
                "source": "https://example.com/article",
                "title": "Article",
                "snippet": "Fetched page text",
                "link": "https://example.com/article",
                "start_index": 0,
            },
        }
    ]

    # The full page loader path must honor the same request-scoped contract.
    config.BYPASS_WEB_SEARCH_WEB_LOADER = False

    async def fake_loader_config():
        return {
            "web_loader_ssl_verification": True,
            "web_loader_concurrent_requests": 2,
            "web_search_trust_env": False,
        }

    class FakeLoader:
        async def aload(self):
            return [
                retrieval.Document(
                    page_content="Fully loaded page text",
                    metadata={"source": "https://example.com/article"},
                )
            ]

    monkeypatch.setattr(retrieval, "get_loader_config", fake_loader_config)
    monkeypatch.setattr(retrieval, "get_web_loader", lambda *_args, **_kwargs: FakeLoader())
    full_loader_result = asyncio.run(
        retrieval.process_web_search(
            _request(no_store=True),
            retrieval.SearchForm(queries=["full page search"]),
            user=user,
        )
    )
    assert searched_queries[-1] == "full page search"
    assert vector_saves == []
    assert full_loader_result["docs"] == [
        {
            "content": "Fully loaded page text",
            "metadata": {
                "source": "https://example.com/article",
                "start_index": 0,
            },
        }
    ]

    # An ordinary browser request has no header and keeps the global vector
    # retrieval behaviour, proving this is not a process-wide switch.
    browser_result = asyncio.run(
        retrieval.process_web_search(
            _request(no_store=False),
            retrieval.SearchForm(queries=["browser search"]),
            user=user,
        )
    )
    assert searched_queries[-1] == "browser search"
    assert vector_saves == [retrieval.save_docs_to_vector_db]
    assert browser_result["collection_names"][0].startswith("web-search-mini-user-")
    assert "docs" not in browser_result

    # The administrator's existing global bypass remains full-context when no
    # request header is present; only the Mini Program contract is bounded.
    config.BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL = True
    global_bypass_result = asyncio.run(
        retrieval.process_web_search(
            _request(no_store=False),
            retrieval.SearchForm(queries=["global bypass search"]),
            user=user,
        )
    )
    assert vector_saves == [retrieval.save_docs_to_vector_db]
    assert global_bypass_result["docs"][0]["content"] == "Fully loaded page text"
    assert "start_index" not in global_bypass_result["docs"][0]["metadata"]


def test_request_scoped_no_store_bounds_long_pages_and_prioritizes_relevant_chunks(open_webui_modules, monkeypatch):
    retrieval, _, _ = open_webui_modules
    config = SimpleNamespace(
        ENABLE_WEB_SEARCH=True,
        USER_PERMISSIONS={},
        WEB_SEARCH_ENGINE="test",
        WEB_SEARCH_CONCURRENT_REQUESTS=0,
        BYPASS_WEB_SEARCH_WEB_LOADER=False,
        BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=False,
        ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER=False,
        TEXT_SPLITTER="",
        CHUNK_SIZE=2048,
        CHUNK_OVERLAP=0,
        CHUNK_MIN_SIZE_TARGET=0,
        TOP_K=100,
    )
    vector_saves: list[object] = []

    async def fake_config():
        return config

    async def fake_search(_request, _engine, _query, _user):
        return [
            retrieval.SearchResult(
                link="https://general.example/guide",
                title="General guide",
                snippet="General information",
            ),
            retrieval.SearchResult(
                link="https://research.example/paper",
                title="Battery paper",
                snippet="Relevant research",
            ),
        ]

    async def fake_loader_config():
        return {
            "web_loader_ssl_verification": True,
            "web_loader_concurrent_requests": 2,
            "web_search_trust_env": False,
        }

    class FakeLoader:
        async def aload(self):
            return [
                retrieval.Document(
                    page_content=("orchard irrigation schedule and soil maintenance. " * 2400),
                    metadata={
                        "source": "https://general.example/guide",
                        "title": "General guide",
                    },
                ),
                retrieval.Document(
                    page_content=("本论文研究锆电池催化剂的效率与寿命。" * 500),
                    metadata={
                        "source": "https://research.example/paper",
                        "title": "Battery paper",
                        "citation_tag": "keep-me",
                    },
                ),
            ]

    async def fake_run_in_threadpool(function, *_args, **_kwargs):
        vector_saves.append(function)
        return True

    monkeypatch.setattr(retrieval, "get_retrieval_config", fake_config)
    monkeypatch.setattr(retrieval, "search_web", fake_search)
    monkeypatch.setattr(retrieval, "get_loader_config", fake_loader_config)
    monkeypatch.setattr(retrieval, "get_web_loader", lambda *_args, **_kwargs: FakeLoader())
    monkeypatch.setattr(retrieval, "run_in_threadpool", fake_run_in_threadpool)

    result = asyncio.run(
        retrieval.process_web_search(
            _request(no_store=True),
            retrieval.SearchForm(queries=["锆电池催化剂效率"]),
            user=SimpleNamespace(id="mini-user", role="admin"),
        )
    )

    returned_docs = result["docs"]
    assert vector_saves == []
    assert len(returned_docs) <= retrieval.NO_STORE_WEB_SEARCH_MAX_CHUNKS
    assert sum(len(doc["content"]) for doc in returned_docs) <= (retrieval.NO_STORE_WEB_SEARCH_MAX_CHARACTERS)
    assert len(returned_docs) < retrieval.NO_STORE_WEB_SEARCH_MAX_CHUNKS
    assert any(doc["metadata"].get("content_truncated") for doc in returned_docs)
    assert result["loaded_count"] == len(returned_docs)
    assert "锆电池催化剂" in returned_docs[0]["content"]
    assert returned_docs[0]["metadata"]["source"] == ("https://research.example/paper")
    assert returned_docs[0]["metadata"]["title"] == "Battery paper"
    assert returned_docs[0]["metadata"]["citation_tag"] == "keep-me"
    assert isinstance(returned_docs[0]["metadata"]["start_index"], int)


def test_web_loader_failure_logs_never_include_signed_url_or_exception_text(open_webui_modules, monkeypatch, caplog):
    _, web_utils, _ = open_webui_modules
    sensitive_url = (
        "https://user:password@example.com/private/path"
        "?access_token=top-secret&signature=also-secret#private-fragment"
    )
    loader = object.__new__(web_utils.SafeWebBaseLoader)
    loader.web_paths = (sensitive_url,)
    loader.bs_kwargs = {}
    loader.bs_get_text_kwargs = {}

    def fail_with_sensitive_exception(*_args, **_kwargs):
        raise RuntimeError(f"network failure for {sensitive_url}")

    monkeypatch.setattr(loader, "_scrape", fail_with_sensitive_exception)
    with caplog.at_level(logging.DEBUG, logger=web_utils.__name__):
        assert list(loader.lazy_load()) == []

        monkeypatch.setattr(web_utils, "safe_validate_urls", lambda _urls: [])
        with pytest.raises(ValueError):
            web_utils.get_web_loader([sensitive_url])

    rendered_logs = caplog.text
    assert "Web content loading failed; continuing" in rendered_logs
    assert "count=1" in rendered_logs
    for secret in (
        sensitive_url,
        "user:password",
        "/private/path",
        "access_token",
        "top-secret",
        "signature",
        "also-secret",
        "private-fragment",
        "network failure for",
    ):
        assert secret not in rendered_logs


def test_web_loader_fail_fast_paths_reraise_original_error_not_name_error(open_webui_modules, monkeypatch):
    _, web_utils, _ = open_webui_modules
    expected = RuntimeError("loader failed")

    tavily_sync = object.__new__(web_utils.SafeTavilyLoader)
    tavily_sync.web_paths = ["https://example.com/article"]
    tavily_sync.continue_on_failure = False

    def fail_sync(*_args, **_kwargs):
        raise expected

    monkeypatch.setattr(tavily_sync, "_safe_process_url_sync", fail_sync)
    with pytest.raises(RuntimeError) as caught:
        list(tavily_sync.lazy_load())
    assert caught.value is expected

    tavily_async = object.__new__(web_utils.SafeTavilyLoader)
    tavily_async.web_paths = ["https://example.com/article"]
    tavily_async.continue_on_failure = False

    async def fail_async(*_args, **_kwargs):
        raise expected

    monkeypatch.setattr(tavily_async, "_safe_process_url", fail_async)

    async def consume_tavily():
        return [document async for document in tavily_async.alazy_load()]

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(consume_tavily())
    assert caught.value is expected

    microsoft = object.__new__(web_utils.SafeMicrosoftWebIQLoader)
    microsoft.continue_on_failure = False

    async def fail_threadpool(*_args, **_kwargs):
        raise expected

    monkeypatch.setattr(web_utils, "run_in_threadpool", fail_threadpool)

    async def consume_microsoft():
        return [document async for document in microsoft.alazy_load()]

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(consume_microsoft())
    assert caught.value is expected


def test_web_search_middleware_does_not_log_provider_exception_or_signed_url(open_webui_modules, monkeypatch, caplog):
    _, _, middleware = open_webui_modules
    from fastapi import HTTPException

    sensitive_url = "https://example.com/search?access_token=never-log-this"
    emitted: list[dict] = []

    async def emit(event):
        emitted.append(event)

    async def fail_query_generation(*_args, **_kwargs):
        raise RuntimeError(f"provider failed for {sensitive_url}")

    async def fail_search(*_args, **_kwargs):
        raise HTTPException(status_code=400, detail=sensitive_url)

    monkeypatch.setattr(middleware, "generate_queries", fail_query_generation)
    monkeypatch.setattr(middleware, "process_web_search", fail_search)
    form_data = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "find a paper"}],
    }
    extra_params = {"__event_emitter__": emit, "__chat_id__": None}

    with caplog.at_level(logging.DEBUG, logger=middleware.__name__):
        result = asyncio.run(
            middleware.chat_web_search_handler(
                _request(no_store=True),
                form_data,
                extra_params,
                SimpleNamespace(id="mini-user", role="admin"),
            )
        )

    assert result is form_data
    assert emitted[-1]["data"]["description"] == ("An error occurred while searching the web")
    assert sensitive_url not in caplog.text
    assert "never-log-this" not in caplog.text
    assert "provider failed for" not in caplog.text


def test_tool_citation_failure_does_not_log_signed_url(open_webui_modules, caplog):
    _, _, middleware = open_webui_modules
    sensitive_url = "https://example.com/page?access_token=never-log-this"

    class BrokenSearchResult(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError(f"malformed tool result from {sensitive_url}")

    with caplog.at_level(logging.DEBUG, logger=middleware.__name__):
        sources = middleware.get_citation_source_from_tool_result(
            tool_name="search_web",
            tool_params={},
            tool_result=[BrokenSearchResult()],
        )

    assert sources[0]["source"]["name"] == "search_web"
    assert "Tool result citation parsing failed" in caplog.text
    assert sensitive_url not in caplog.text
    assert "access_token" not in caplog.text
    assert "never-log-this" not in caplog.text
