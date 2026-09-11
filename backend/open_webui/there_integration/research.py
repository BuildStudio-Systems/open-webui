"""Asynchronous THERE facade over its first-party scholarly provider adapters.

The bundled ``_academic`` module is the unchanged implementation from
deploy/native/hermes-skills/academic-search/scripts/search_academic.py. Its
normalization, identifier-based deduplication, provider provenance, safe errors
and killable per-provider deadline workers are retained here. No request-time
script is loaded from a user's home and no catalog code is executed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from . import _academic

MAX_CONCURRENT_SEARCHES = 4
_search_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SEARCHES)


def providers() -> list[dict[str, Any]]:
    """Return public provider capabilities; never return credential state."""
    return [{"name": name, **_academic.SOURCE_DETAILS[name]} for name in _academic.ALL_SOURCES]


def _search_sync(
    q: str,
    limit: int,
    timeout: int,
    sources: Sequence[str] | None,
    opener: Callable[..., Any] | None,
    environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    started = time.monotonic()
    if not _search_slots.acquire(timeout=min(1, timeout)):
        selected = _academic._normalize_sources(sources)
        return {
            "query": q, "items": [], "total": 0, "partial": True,
            "all_sources_failed": True, "successful_source_count": 0,
            "sources": [{"source": name, "status": "busy", "returned": 0, "retryable": True} for name in selected],
        }
    try:
        # Include slot admission in the bounded wall-clock budget. The provider
        # implementation gives all workers one shared deadline, not N timeouts.
        remaining = max(1, int(timeout - (time.monotonic() - started)))
        result = _academic.search_academic(
            q,
            sources=sources,
            max_results=limit,
            per_source=min(limit, 20),
            timeout=remaining,
            opener=opener,
            environment=environment,
        )
    finally:
        _search_slots.release()
    items = []
    for record in result["results"]:
        source_names = record.get("sources") or []
        items.append({**record, "source": source_names[0] if source_names else "unknown"})
    return {
        "query": result["query"], "items": items, "total": result["filtered_count"],
        "sources": result["source_status"], "partial": result["partial"],
        "all_sources_failed": result["all_sources_failed"],
        "successful_source_count": result["successful_source_count"],
        "deduplicated_count": result["deduplicated_count"],
        "provider_schema_version": result["schema_version"],
    }


async def search(
    q: str,
    limit: int = 20,
    *,
    timeout: int = 20,
    sources: Sequence[str] | None = None,
    opener: Callable[..., Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Search up to ten allowlisted scholarly APIs without blocking the event loop.

    ``opener`` and ``environment`` are server/test dependency injections, never
    HTTP request parameters. In production leave ``opener`` unset so isolated
    workers can be terminated even when an upstream DNS lookup hangs.
    """
    if not isinstance(q, str) or not q.strip() or len(q) > 2_000:
        raise ValueError("Research query must contain between 1 and 2000 characters.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Research limit must be between 1 and 50.")
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError("Research timeout must be between 1 and 30 seconds.")
    if sources is not None and (
        not isinstance(sources, (list, tuple))
        or len(sources) > len(_academic.ALL_SOURCES)
        or any(not isinstance(source, str) for source in sources)
    ):
        raise ValueError("Research sources must be a list of provider names.")
    selected = _academic._normalize_sources(sources)
    return await asyncio.to_thread(_search_sync, q, limit, timeout, selected, opener, environment)
