"""Convert authorized engine chunks into THERE's existing chat citation format."""

import math
from fastapi import HTTPException
from open_webui.there_integration.access import get_binding
from open_webui.there_integration.weknora import WeKnoraClient


def authorized_rows(rows, engine_id):
    """Defense in depth against mis-scoped or malformed upstream search results."""
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get('knowledge_base_id') != engine_id:
            continue
        if not isinstance(row.get('content'), str):
            continue
        score = row.get('score', 0)
        if type(score) not in (int, float):
            continue
        try:
            if not math.isfinite(score):
                continue
        except OverflowError:
            continue
        if not isinstance(row.get('knowledge_id'), str) or not isinstance(row.get('id'), str):
            continue
        result.append(row)
    return result


async def retrieve(resource_id, queries, count, user):
    if user is None:
        raise HTTPException(401, '请登录后检索知识库。')
    binding, knowledge = await get_binding(resource_id, user)
    limit = min(max(int(count or 5), 1), 20)
    records, seen = [], set()
    for query in list(queries or [])[:3]:
        if not isinstance(query, str) or not query.strip():
            continue
        envelope = await WeKnoraClient().search(binding.engine_id, query[:2000], limit=limit)
        data = envelope.get('data') or []
        if isinstance(data, dict):
            data = data.get('results') or data.get('items') or []
        for row in authorized_rows(data, binding.engine_id):
            key = (row.get('knowledge_id'), row.get('id'), row.get('content'))
            if key not in seen:
                seen.add(key)
                records.append(row)
    records = sorted(records, key=lambda row: float(row.get('score') or 0), reverse=True)[:limit]
    return {
        'documents': [[row.get('content', '') for row in records]],
        'metadatas': [[{
            'source': row.get('knowledge_title') or row.get('title') or knowledge.name,
            'name': row.get('knowledge_title') or row.get('title') or knowledge.name,
            'knowledge_id': resource_id,
            'document_id': row.get('knowledge_id'),
            'file_id': f"there-{resource_id}-{row.get('knowledge_id', '')}",
            'chunk_id': row.get('id'),
            'url': f'/workspace/there?knowledge={resource_id}',
            'engine': 'weknora',
        } for row in records]],
        'distances': [[row.get('score', 0) for row in records]],
    }
