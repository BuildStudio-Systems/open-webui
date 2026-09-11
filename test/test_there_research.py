from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from open_webui.there_integration import research


CROSSREF = {"message": {"items": [{
    "DOI": "10.1234/graph.1", "title": ["A Unified Graph Paper"],
    "author": [{"given": "Alice", "family": "Example"}],
    "published": {"date-parts": [[2025, 2, 1]]}, "type": "journal-article",
    "URL": "https://doi.org/10.1234/graph.1", "is-referenced-by-count": 17,
}]}}
DATACITE = {"data": [{"id": "10.1234/graph.1", "attributes": {
    "doi": "10.1234/graph.1", "titles": [{"title": "A Unified Graph Paper"}],
    "creators": [{"name": "Alice Example"}], "publicationYear": 2025,
    "types": {"resourceTypeGeneral": "JournalArticle"},
    "url": "https://doi.org/10.1234/graph.1",
}}]}


class Transport:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        payload = self.routes[urlsplit(request.full_url).hostname]
        if isinstance(payload, BaseException):
            raise payload
        return io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def run_search(transport, *, sources=("crossref", "datacite"), q="graph learning", **kwargs):
    return asyncio.run(research.search(q, sources=sources, opener=transport, environment={}, **kwargs))


def test_normalizes_and_deduplicates_papers_with_provider_provenance():
    transport = Transport({"api.crossref.org": CROSSREF, "api.datacite.org": DATACITE})
    result = run_search(transport)
    assert result["total"] == 1
    assert result["partial"] is False
    assert result["successful_source_count"] == 2
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["title"] == "A Unified Graph Paper"
    assert item["authors"] == ["Alice Example"]
    assert item["year"] == 2025
    assert item["url"] == "https://doi.org/10.1234/graph.1"
    assert item["doi"] == "10.1234/graph.1"
    assert set(item["sources"]) == {"crossref", "datacite"}
    assert {entry["source"] for entry in item["provenance"]} == {"crossref", "datacite"}
    assert item["source"] in item["sources"]


def test_failed_provider_does_not_discard_successful_results_or_leak_details():
    sensitive = "https://user:password@example.invalid?api_key=private"
    transport = Transport({"api.crossref.org": CROSSREF, "api.datacite.org": URLError(sensitive)})
    result = run_search(transport)
    assert result["partial"] is True
    assert result["all_sources_failed"] is False
    assert len(result["items"]) == 1
    assert {item["source"]: item["status"] for item in result["sources"]} == {"crossref": "ok", "datacite": "network_error"}
    assert sensitive not in json.dumps(result)


def test_throttled_and_invalid_sources_are_reported_as_partial():
    error = HTTPError("https://api.crossref.org/works", 429, "token=private", {}, None)
    transport = Transport({"api.crossref.org": error, "api.datacite.org": b"not json"})
    result = run_search(transport)
    assert result["items"] == []
    assert result["all_sources_failed"] is True
    assert result["partial"] is True
    assert [item["status"] for item in result["sources"]] == ["rate_limited", "invalid_response"]
    assert "private" not in json.dumps(result)


def test_query_is_encoded_as_data_not_url_or_cli_parameters():
    query = 'graph & rows=999 --source private; $(command) "unicode 中文"'
    transport = Transport({"api.crossref.org": CROSSREF})
    run_search(transport, sources=["crossref"], q=query, limit=3)
    request, timeout = transport.requests[0]
    parsed = urlsplit(request.full_url)
    parameters = parse_qs(parsed.query)
    assert parsed.hostname == "api.crossref.org"
    assert parameters["query.bibliographic"] == [query]
    assert parameters["rows"] == ["3"]
    assert timeout <= 20


@pytest.mark.parametrize("kwargs", [
    {"q": ""}, {"q": "x" * 2001}, {"q": None}, {"limit": 0}, {"limit": 51},
    {"limit": True}, {"timeout": 0}, {"timeout": 31}, {"timeout": True},
    {"sources": ["file:///etc/passwd"]}, {"sources": []}, {"sources": "crossref"},
    {"sources": [None]}, {"sources": ["crossref"] * 11}, {"sources": 7},
])
def test_bounds_are_enforced_before_transport_use(kwargs):
    transport = Transport({})
    with pytest.raises(ValueError):
        run_search(transport, **kwargs)
    assert transport.requests == []


def test_all_default_providers_stay_available():
    details = research.providers()
    assert {entry["name"] for entry in details if entry["default"]} == {
        "arxiv", "openreview", "crossref", "openalex", "europe-pmc", "datacite", "doaj", "openaire",
    }
    assert {entry["name"] for entry in details if not entry["default"]} == {"semantic-scholar", "dblp"}


def test_production_deadline_kills_blocked_provider_workers_without_network(monkeypatch):
    # Exercise the real process lifecycle with an unresponsive DNS-like worker.
    monkeypatch.setattr(research._academic, "_provider_worker_command", lambda source: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    result = asyncio.run(research.search("deadline test", timeout=1, sources=["datacite"]))
    assert time.monotonic() - started < 4
    assert result["all_sources_failed"] is True
    assert result["sources"][0]["status"] == "timeout"


def test_search_does_not_block_async_event_loop():
    class SlowTransport(Transport):
        def __call__(self, request, timeout):
            time.sleep(0.05)
            return super().__call__(request, timeout)

    async def scenario():
        task = asyncio.create_task(research.search("graph", opener=SlowTransport({"api.datacite.org": DATACITE}), sources=["datacite"]))
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.005)
            ticks += 1
        return await task, ticks

    result, ticks = asyncio.run(scenario())
    assert result["items"]
    assert ticks >= 3
