"""Owner-filtered PostgreSQL read-through search over completed historical Q&A.

No search replica or persistent copy: canonical JSON deletion takes effect in
the same database snapshot. All search values are bound parameters.
"""
import asyncio
from sqlalchemy import text


# These identify the deterministic Agent receipt envelope, not ordinary Q&A
# that discusses devices. Keep full/manual history search unchanged.
DEVICE_RECEIPT_PREFIXES = tuple(intro + '\n\n```' for intro in (
    'Actual device-tool receipts for this turn follow. Any requested result not listed remains unverified. Registered recipe counts are not counts of tested operations.',
    '以下为本轮设备工具的实际回执；未列出的请求结果尚未确认。登记的操作数量不代表全部操作均已测试。',
    '今回のデバイスツール実行記録です。以下にない要求の結果は未確認です。',
))


def is_device_receipt(answer):
    return isinstance(answer, str) and answer.startswith(DEVICE_RECEIPT_PREFIXES)


# Filter ownership before expanding messages. An explicitly present empty
# history is authoritative; legacy/normalized fallback must not revive it.
PAIR_CTE = """
WITH owned AS MATERIALIZED (
 SELECT id AS chat_id, title, updated_at, CAST(chat AS jsonb) AS payload
 FROM chat
 WHERE user_id = :owner_id AND (:exclude_id = '' OR id <> :exclude_id)
 AND (timer_at IS NULL OR timer_at = 0)
 AND COALESCE(CAST(meta AS jsonb)->'internal', 'null'::jsonb)
     IN ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '{}'::jsonb, '[]'::jsonb)
), classified AS (
 SELECT *, COALESCE(jsonb_typeof(payload#>'{history,messages}') = 'object', false) AS canonical,
           COALESCE(jsonb_typeof(payload->'messages') = 'array', false) AS legacy
 FROM owned
), legacy_rows AS (
 SELECT c.chat_id, e.ordinality,
        COALESCE(NULLIF(e.value->>'id', ''), (e.ordinality - 1)::text) AS mid, e.value
 FROM classified c CROSS JOIN LATERAL jsonb_array_elements(
   CASE WHEN NOT c.canonical AND c.legacy THEN c.payload->'messages' ELSE '[]'::jsonb END
 ) WITH ORDINALITY AS e(value, ordinality)
 WHERE jsonb_typeof(e.value) = 'object'
), legacy_parents AS (
 SELECT *, lag(mid) OVER (PARTITION BY chat_id ORDER BY ordinality) AS previous
 FROM legacy_rows
), message_rows AS (
 SELECT c.chat_id, e.key AS mid, e.value AS message, 0::bigint AS ordinal
 FROM classified c CROSS JOIN LATERAL jsonb_each(
   CASE WHEN c.canonical THEN c.payload#>'{history,messages}' ELSE '{}'::jsonb END
 ) e
 UNION ALL
 SELECT chat_id, mid, CASE WHEN value ? 'parentId' THEN value
   ELSE value || jsonb_build_object('parentId', previous) END, ordinality
 FROM legacy_parents
 UNION ALL
 SELECT c.chat_id, m.id, jsonb_build_object('role', m.role, 'parentId', m.parent_id,
   'content', m.content, 'done', m.done, 'error', m.error), 0::bigint
 FROM classified c JOIN chat_message m ON m.chat_id = c.chat_id AND m.user_id = :owner_id
 WHERE NOT c.canonical AND NOT c.legacy
), messages AS (
 SELECT DISTINCT ON (chat_id, mid) chat_id, mid, message
 FROM message_rows ORDER BY chat_id, mid, ordinal DESC
), content AS (
 SELECT chat_id, mid, message,
 CASE jsonb_typeof(message->'content')
 WHEN 'string' THEN message->>'content'
 WHEN 'array' THEN (
   SELECT string_agg(b.value->>'text', E'\\n' ORDER BY b.ordinality)
   FROM jsonb_array_elements(message->'content') WITH ORDINALITY b(value, ordinality)
   WHERE b.value->>'type' = 'text' AND jsonb_typeof(b.value->'text') = 'string'
 ) ELSE '' END AS body
 FROM messages
), pairs AS (
 SELECT c.chat_id, c.title, c.updated_at, a.mid AS message_id,
        q.body AS question, a.body AS answer
 FROM classified c JOIN content a ON a.chat_id = c.chat_id
 JOIN content q ON q.chat_id = a.chat_id AND q.mid = a.message->>'parentId'
 WHERE a.message->>'role' = 'assistant' AND a.message->'done' = 'true'::jsonb
 AND jsonb_typeof(a.message->'parentId') = 'string' AND q.message->>'role' = 'user'
 AND COALESCE(a.message->'error', 'null'::jsonb)
     IN ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '{}'::jsonb, '[]'::jsonb)
 AND length(btrim(q.body, E' \\t\\r\\n')) > 0 AND length(btrim(a.body, E' \\t\\r\\n')) > 0
)
"""


async def search_pairs(db, owner_id, *, query='', terms=None, exclude_id='', page=1, limit=20,
                       exclude_device_receipts=False):
    """Search all owned history, then paginate/rank matches; never cap by age."""
    params = dict(owner_id=owner_id, exclude_id=exclude_id, offset=(page - 1) * limit,
                  row_limit=limit + 1, needle=query.strip().lower())
    cte, relation = PAIR_CTE, 'pairs'
    if exclude_device_receipts:
        # Filter before ranking/pagination so receipts cannot crowd useful Q&A
        # out of the three automatic context slots. All values remain bound.
        predicates = []
        for i, prefix in enumerate(DEVICE_RECEIPT_PREFIXES):
            params[f'receipt_prefix_{i}'] = prefix
            predicates.append(f'strpos(answer, :receipt_prefix_{i}) <> 1')
        cte += ', eligible AS (SELECT * FROM pairs WHERE ' + ' AND '.join(predicates) + ')'
        relation = 'eligible'
    if terms:
        terms = terms[:64]
        # Fold each body once, rather than once per keyword (up to 64 times).
        # Materialization prevents PostgreSQL from inlining this expression
        # back into every CASE. Ranking still sees the complete original text.
        cte += (", searchable AS MATERIALIZED (SELECT *, "
                "lower(question || E'\\n' || answer) AS search_body FROM " + relation + ")")
        relation = 'searchable'
        # Only parameter names are generated, never SQL from user input.
        score = ' + '.join(
            f"CASE WHEN strpos(search_body, :term_{i}) > 0 THEN 1 ELSE 0 END"
            for i in range(len(terms)))
        params.update({f'term_{i}': term.lower() for i, term in enumerate(terms)})
        params['threshold'] = min(2, len(terms))
        cte += f", ranked AS (SELECT *, ({score}) AS score FROM {relation})"
        relation = 'ranked'
        predicate = 'score >= :threshold'
        ordering = 'score DESC, updated_at DESC, chat_id DESC, message_id'
    else:
        predicate = "strpos(lower(question || E'\\n' || answer), :needle) > 0"
        ordering = 'updated_at DESC, chat_id DESC, message_id'
    # The database query has a bounded wall-clock budget and result size. A
    # timeout is not represented as a successful empty result.
    async with asyncio.timeout(5):
        # Keep the existing public result limits, but apply them before network
        # transfer. Neither full answers nor the folded ranking body leave DB.
        projection = (" SELECT chat_id, title, updated_at, message_id, "
                      "left(question, 16000) AS question, left(answer, 32000) AS answer, "
                      "(length(question) > 16000 OR length(answer) > 32000) AS truncated "
                      f"FROM {relation} WHERE {predicate}")
        rows = (await db.execute(text(cte + projection +
            f' ORDER BY {ordering} OFFSET :offset LIMIT :row_limit'), params)).mappings().all()
    items = []
    for row in rows[:limit]:
        q, a = row['question'], row['answer']
        items.append(dict(chat_id=row['chat_id'], message_id=row['message_id'],
            title=row['title'], updated_at=row['updated_at'], question=q[:16000], answer=a[:32000],
            truncated=bool(row['truncated'])))
    return dict(items=items, page=page, has_more=len(rows) > limit,
                scope='personal', search_scope='all_history', pagination='question_answer')
