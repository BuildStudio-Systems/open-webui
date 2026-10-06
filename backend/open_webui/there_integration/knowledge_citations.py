"""Project authorized WeKnora search hits into Open WebUI citation sources.

Only the THERE-owned, read-only MCP connection is eligible. The parser is
deliberately fail-closed: arbitrary MCP tools and malformed search envelopes
never become trusted-looking citation cards.
"""

from __future__ import annotations

import json
import math
import re
from html import escape as html_escape
from html import unescape as html_unescape
from itertools import islice
from typing import Any

WEKNORA_MCP_SERVER_ID = 'buildstudio-weknora'
WEKNORA_SEARCH_TOOL_NAME = 'hybrid_search'
MAX_CITATION_ROWS = 20
MAX_CITATION_SCAN_ROWS = 200
MAX_CITATION_CONTENT_CHARS = 20_000
MAX_CITATION_ID_CHARS = 512
MAX_CITATION_QUERY_CHARS = 2_000
MAX_CITATION_RESULT_CHARS = 2_000_000

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def escape_rag_source_attribute(value: Any) -> str:
    """Canonicalize and escape one value for a quoted RAG source attribute."""

    return html_escape(html_unescape(str(value)), quote=True)


def serialize_rag_source_opening_tag(
    source_index: int,
    *,
    resource_type: str | None = None,
) -> str:
    """Serialize a RAG marker from server-generated values only."""

    if type(source_index) is not int or source_index < 1:
        raise ValueError('source_index must be a positive integer')
    attributes = [f'id="{escape_rag_source_attribute(source_index)}"']
    if resource_type == 'knowledge':
        attributes.append(f'resource-type="{escape_rag_source_attribute(resource_type)}"')
    return '<source ' + ' '.join(attributes) + '>'


def _mapping_fits_result_limit(value: dict[str, Any]) -> bool:
    size = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
        for fragment in encoder.iterencode(value):
            size += len(fragment)
            if size > MAX_CITATION_RESULT_CHARS:
                return False
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False
    return True


def _decode_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        if len(value) > MAX_CITATION_RESULT_CHARS:
            return None
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError, TypeError):
            return None
        return value if isinstance(value, dict) else None
    if isinstance(value, dict) and _mapping_fits_result_limit(value):
        return value
    return None


def _validated_row(row: Any) -> tuple[str, str, str, str, int | float] | None:
    if not isinstance(row, dict):
        return None
    values = (
        row.get('content'),
        row.get('knowledge_id'),
        row.get('id'),
        row.get('knowledge_base_id'),
    )
    score = row.get('score')
    if not all(
        isinstance(value, str) and value.strip() and len(value) <= MAX_CITATION_ID_CHARS for value in values[1:]
    ):
        return None
    if not isinstance(values[0], str) or not values[0].strip():
        return None
    if type(score) not in (int, float):
        return None
    try:
        finite_score = math.isfinite(score)
    except (OverflowError, TypeError):
        return None
    if not finite_score:
        return None
    content, knowledge_id, chunk_id, knowledge_base_id = values
    return (
        content,
        knowledge_id.strip(),
        chunk_id.strip(),
        knowledge_base_id.strip(),
        score,
    )


def _validated_tool_params(tool_params: Any) -> tuple[str, str] | None:
    if not isinstance(tool_params, dict):
        return None
    requested_kb_id = tool_params.get('kb_id')
    query = tool_params.get('query')
    if not isinstance(requested_kb_id, str) or not isinstance(query, str):
        return None
    requested_kb_id = requested_kb_id.strip()
    query = query.strip()
    if not _UUID_RE.fullmatch(requested_kb_id) or not query or len(query) > MAX_CITATION_QUERY_CHARS:
        return None
    return requested_kb_id, query


def _validated_result_rows(tool_result: Any) -> list[Any] | None:
    envelope = _decode_result(tool_result)
    if envelope is None or envelope.get('success') is not True:
        return None
    rows = envelope.get('data')
    return rows if isinstance(rows, list) else None


def citation_sources_from_weknora_mcp_result(
    *,
    server_id: str,
    tool_name: str,
    tool_params: dict[str, Any] | None,
    tool_result: Any,
) -> list[dict[str, Any]]:
    """Return standard Open WebUI sources for one authorized hybrid search.

    ``server_id`` and the unprefixed MCP ``tool_name`` must come from the
    server-side tool registry, never from model-generated arguments.

    Compatibility is intentionally strict: ``kb_id`` must be a UUID. Existing
    human-readable aliases may still be accepted by the upstream tool, but do
    not produce citation sources until its authenticated result contract
    exposes a server-verified resolved knowledge-base ID.
    """

    if server_id != WEKNORA_MCP_SERVER_ID or tool_name != WEKNORA_SEARCH_TOOL_NAME:
        return []

    validated_params = _validated_tool_params(tool_params)
    if validated_params is None:
        return []
    requested_kb_id, _ = validated_params

    rows = _validated_result_rows(tool_result)
    if rows is None:
        return []

    grouped: dict[str, dict[str, Any]] = {}
    seen_chunks: set[tuple[str, str]] = set()
    for row in islice(rows, MAX_CITATION_SCAN_ROWS):
        validated = _validated_row(row)
        if validated is None:
            continue
        content, knowledge_id, chunk_id, knowledge_base_id, score = validated
        if knowledge_base_id.lower() != requested_kb_id.lower():
            continue

        dedupe_key = (knowledge_id, chunk_id)
        if dedupe_key in seen_chunks:
            continue
        seen_chunks.add(dedupe_key)

        title = row.get('knowledge_title') or row.get('knowledge_filename') or 'BuildStudio Knowledge'
        if not isinstance(title, str) or not title.strip():
            title = 'BuildStudio Knowledge'
        title = title.strip()[:500]
        source_id = f'weknora:{knowledge_id}'
        source = grouped.setdefault(
            source_id,
            {
                'source': {
                    'id': source_id,
                    'name': title,
                    'type': 'knowledge',
                    'engine': 'weknora',
                },
                'document': [],
                'metadata': [],
            },
        )
        source['document'].append(content[:MAX_CITATION_CONTENT_CHARS])
        source['metadata'].append(
            {
                'source': source_id,
                'name': title,
                'knowledge_id': knowledge_id,
                'knowledge_base_id': knowledge_base_id,
                'chunk_id': chunk_id,
                'engine': 'weknora',
                # Hybrid-search scores are not guaranteed to be normalized
                # (RRF is one possible path). Preserve the value as diagnostic
                # metadata without presenting it as a 0-100% relevance score.
                'retrieval_score': score,
            }
        )
        if len(seen_chunks) >= MAX_CITATION_ROWS:
            break

    return list(grouped.values())
