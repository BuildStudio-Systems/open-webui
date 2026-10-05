"""Read-through personal Q&A. Never maintain a second, stale copy of chat data."""

from contextlib import asynccontextmanager
from sqlalchemy import select
from sqlalchemy.orm import load_only
from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrant
from open_webui.models.chats import Chat
from open_webui.models.chat_messages import ChatMessage
from open_webui.there_integration.personal_search import is_device_receipt
from open_webui.there_integration.personal_context import retrieval_terms, history_source


@asynccontextmanager
async def personal_session(db=None):
    # An explicit transaction must not be replaced when the application's
    # optional DATABASE_ENABLE_SESSION_SHARING setting is disabled.
    if db is not None:
        yield db
    else:
        async with get_async_db_context() as session:
            yield session


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(block['text'] for block in value
                         if isinstance(block, dict) and block.get('type') == 'text'
                         and isinstance(block.get('text'), str))
    return ''


def pairs(messages, query=''):
    """Only explicit, completed assistant/user parent pairs; no tool output."""
    needle = query.casefold().strip()
    result = []
    for message_id, answer in messages.items():
        if not isinstance(answer, dict) or answer.get('role') != 'assistant':
            continue
        if answer.get('done') is not True or answer.get('error'):
            continue
        parent_id = answer.get('parentId')
        if not isinstance(parent_id, str):
            continue
        question = messages.get(parent_id)
        if not isinstance(question, dict) or question.get('role') != 'user':
            continue
        q, a = text_content(question.get('content')), text_content(answer.get('content'))
        if not q.strip() or not a.strip() or (needle and needle not in (q + '\n' + a).casefold()):
            continue
        result.append(dict(message_id=message_id, question=q[:16000], answer=a[:32000],
                           truncated=len(q) > 16000 or len(a) > 32000))
    return result


async def conversation_page(db, owner_id, *, query='', page=1, limit=20):
    # Ownership is applied in SQL, before reading any message content. Pages refer
    # to conversations, not matches; an empty page can still have a next page.
    chats = list((await db.scalars(select(Chat).where(Chat.user_id == owner_id)
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
        .offset((page - 1) * limit).limit(limit + 1))).all())
    items = []
    for chat in chats[:limit]:
        if (chat.meta or {}).get('internal') or chat.timer_at or chat.user_id != owner_id:
            continue
        payload = chat.chat if isinstance(chat.chat, dict) else {}
        history = payload.get('history')
        if isinstance(history, dict) and isinstance(history.get('messages'), dict):
            # The committed JSON is authoritative during the existing two-step
            # delete/update flow. Do not resurrect stale normalized rows.
            messages = history['messages']
        elif isinstance(payload.get('messages'), list):
            messages = {}
            previous = None
            for index, message in enumerate(payload['messages']):
                if not isinstance(message, dict):
                    continue
                mid = str(message.get('id') or index)
                messages[mid] = {**message, 'parentId': message.get('parentId', previous)}
                previous = mid
        else:
            rows = (await db.scalars(select(ChatMessage).where(
                ChatMessage.chat_id == chat.id, ChatMessage.user_id == owner_id))).all()
            messages = {row.id: dict(role=row.role, parentId=row.parent_id,
                content=row.content, done=row.done, error=row.error) for row in rows}
        for pair in pairs(messages, query):
            items.append(dict(chat_id=chat.id, title=chat.title, updated_at=chat.updated_at, **pair))
    return dict(items=items, page=page, has_more=len(chats) > limit,
                scope='personal', scanned_conversations=min(len(chats), limit))


async def search_history(db, owner_id, *, query='', terms=None, exclude_id='', page=1, limit=20,
                         exclude_device_receipts=False):
    if db.get_bind().dialect.name == 'postgresql':
        from open_webui.there_integration.personal_search import search_pairs
        return await search_pairs(db, owner_id, query=query, terms=terms,
                                  exclude_id=exclude_id, page=page, limit=limit,
                                  exclude_device_receipts=exclude_device_receipts)
    # SQLite is used only for development/testing. Match the PostgreSQL contract
    # against all owned conversations, using the same canonical extraction.
    items = []
    cursor = 1
    while True:
        batch = await conversation_page(db, owner_id, query=query, page=cursor, limit=100)
        for item in batch['items']:
            if item['chat_id'] != exclude_id and not (
                exclude_device_receipts and is_device_receipt(item['answer'])
            ):
                items.append(item)
        if not batch['has_more']:
            break
        cursor += 1
    if terms:
        ranked = [(sum(t in (item['question'] + '\n' + item['answer']).casefold() for t in terms), item)
                  for item in items]
        items = [item for score, item in sorted(ranked,
            key=lambda pair: (pair[0], pair[1]['updated_at'], pair[1]['chat_id']), reverse=True)
            if score >= min(2, len(terms))]
    offset = (page - 1) * limit
    return dict(items=items[offset:offset + limit], page=page,
                has_more=len(items) > offset + limit, scope='personal',
                search_scope='all_history', pagination='question_answer')


async def history_page(db, owner_id, *, query='', page=1, limit=20):
    if query.strip():
        return await search_history(db, owner_id, query=query, page=page, limit=limit)
    return await conversation_page(db, owner_id, page=page, limit=limit)


def query_terms(query):
    return retrieval_terms(query)


async def personal_sources(user, chat_id, query, *, db=None):
    """Only the requester owns the destination AND the source. Admin is not special."""
    terms = query_terms(query)
    if not chat_id or not terms:
        return []
    async with personal_session(db) as session:
        # The destination gate needs metadata, not its potentially large JSON
        # transcript. Do not transfer/decode that transcript before retrieval.
        chat = await session.scalar(select(Chat).options(load_only(
            Chat.id, Chat.share_id, Chat.meta, Chat.timer_at, raiseload=True,
        )).where(Chat.id == chat_id, Chat.user_id == user.id,
            ~select(AccessGrant.id).where(
                AccessGrant.resource_type == 'chat', AccessGrant.resource_id == chat_id,
            ).exists()))
        if not chat or chat.share_id or (chat.meta or {}).get('internal') or chat.timer_at:
            return []
        page = await search_history(session, user.id, terms=terms, exclude_id=chat_id, limit=3,
                                    exclude_device_receipts=True)
        return [history_source(item, terms) for item in page['items']]
