"""Convert authorized engine chunks into THERE's existing chat citation format."""

import asyncio
import math
from itertools import islice
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
    normalized_queries = []
    for query in islice(queries or [], 3):
        if not isinstance(query, str) or not query.strip():
            continue
        query = query[:2000]
        if query not in normalized_queries:
            normalized_queries.append(query)
    # One already-authorized workspace, at most three independent searches.
    # Preserve query order for stable score ties; never cache user results/ACLs.
    tasks = [asyncio.create_task(WeKnoraClient().search(binding.engine_id, query, limit=limit))
             for query in normalized_queries]
    try:
        envelopes = await asyncio.gather(*tasks)
    finally:
        # gather propagates the original error, but does not cancel siblings.
        # Do not leave work running after a failed/cancelled chat request.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    records, seen = [], set()
    for envelope in envelopes:
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
