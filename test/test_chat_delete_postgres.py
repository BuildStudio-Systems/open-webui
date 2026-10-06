"""Opt-in cross-connection lock tests using a private synthetic PostgreSQL schema.

THERE_CHAT_DELETE_TEST_PG_URL must point at an isolated socket cluster, never
the running THERE database. Each scenario creates and drops its own schema.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_there_api import modules


PG_URL = os.environ.get('THERE_CHAT_DELETE_TEST_PG_URL')
pytestmark = pytest.mark.skipif(not PG_URL, reason='requires explicit isolated PostgreSQL test URL')

if PG_URL and sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def postgres_harness(modules, monkeypatch):
    from open_webui.internal import db as db_module
    from open_webui.models.automations import AutomationRun
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.chats import Chat
    from open_webui.models.groups import Group, GroupMember
    from open_webui.models.shared_chats import SharedChat
    from open_webui.models.users import User

    url = make_url(PG_URL)
    socket = str(url.query.get('host', ''))
    if url.database not in {'there_chat_delete_synthetic', 'there_expense_synthetic'} or not socket.startswith(
        ('/tmp/there-chat-delete-test-', '/tmp/there-expense-test-')
    ):
        raise RuntimeError('Chat deletion tests require an isolated synthetic socket cluster')
    if url.drivername != 'postgresql+psycopg':
        raise RuntimeError('Chat deletion lock tests require the async psycopg PostgreSQL driver')

    schema = 'chat_delete_' + uuid4().hex
    engine = create_async_engine(url, isolation_level='READ COMMITTED',
                                 execution_options={'schema_translate_map': {None: schema}},
                                 connect_args={'options': '-c statement_timeout=10000 -c lock_timeout=7000'})
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            await connection.run_sync(
                modules.db.Base.metadata.create_all,
                tables=[User.__table__, Chat.__table__, ChatMessage.__table__,
                        SharedChat.__table__, AutomationRun.__table__,
                        Group.__table__, GroupMember.__table__],
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(db_module, 'AsyncSessionLocal', sessions)
        monkeypatch.setattr(db_module, 'DATABASE_ENABLE_SESSION_SHARING', True)
        async with sessions() as session:
            for owner in ('alice', 'bob'):
                session.add(User(id=owner, name=owner, email=f'{owner}@test.invalid', role='user'))
            session.add(Group(id='group-one', user_id='bob', name='Synthetic group',
                              description='', created_at=1, updated_at=1))
            session.add(GroupMember(id='alice-membership', group_id='group-one',
                                    user_id='alice', created_at=1, updated_at=1))
            for chat_id, owner, folder in (
                ('target', 'alice', 'folder-one'),
                ('other-folder', 'alice', 'folder-two'),
                ('other-owner', 'bob', 'folder-one'),
            ):
                session.add(Chat(id=chat_id, user_id=owner, title=chat_id, chat={}, meta={},
                                 folder_id=folder, created_at=1, updated_at=1))
            await session.flush()
            for chat_id in ('target', 'other-folder', 'other-owner'):
                owner = 'bob' if chat_id == 'other-owner' else 'alice'
                session.add(ChatMessage(id=f'{chat_id}-message', chat_id=chat_id, user_id=owner,
                                        role='user', content='Synthetic', created_at=1, updated_at=1))
                session.add(SharedChat(id=f'{chat_id}-share', chat_id=chat_id, user_id=owner,
                                       title=chat_id, chat={}, created_at=1, updated_at=1))
                session.add(AutomationRun(id=f'{chat_id}-run', automation_id='fixture',
                                          chat_id=chat_id, status='success', created_at=1))
            await session.commit()
        yield sessions, engine
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def wait_until_blocked(engine, waiter_pid, blocker_pid):
    """Prove a real database wait; never infer ordering from a short sleep."""
    async def observe():
        async with engine.connect() as connection:
            while True:
                blocked = await connection.scalar(
                    text('SELECT :blocker = ANY(pg_blocking_pids(:waiter))'),
                    {'blocker': blocker_pid, 'waiter': waiter_pid},
                )
                if blocked:
                    return
                await asyncio.sleep(0.01)

    await asyncio.wait_for(observe(), 5)


def pause_before_commit(session, monkeypatch, reached, resume):
    commit = session.commit

    async def paused_commit():
        reached.set()
        await resume.wait()
        await commit()

    monkeypatch.setattr(session, 'commit', paused_commit)


def pause_after_lock(session, monkeypatch, reached, resume, *, scalar):
    method = session.scalar if scalar else session.execute

    async def paused_lock(statement, *args, **kwargs):
        result = await method(statement, *args, **kwargs)
        if getattr(statement, '_for_update_arg', None) is not None and not reached.is_set():
            reached.set()
            await resume.wait()
        return result

    monkeypatch.setattr(session, 'scalar' if scalar else 'execute', paused_lock)


async def perform_delete(kind, session):
    from open_webui.models.chats import Chats

    if kind == 'single':
        return await Chats.delete_chat_by_id_and_user_id('target', 'alice', db=session)
    if kind == 'admin':
        return await Chats.delete_chat_by_id('target', db=session)
    if kind == 'folder':
        return await Chats.delete_chats_by_user_id_and_folder_id('alice', 'folder-one', db=session)
    return await Chats.delete_chats_by_user_id('alice', db=session)


@pytest.mark.parametrize('kind', ['single', 'admin', 'folder', 'all'])
@pytest.mark.parametrize('first', ['run', 'delete'])
def test_automation_run_and_chat_delete_wait_in_both_orders(modules, monkeypatch, kind, first):
    async def scenario():
        from open_webui.models.automations import AutomationRun, AutomationRuns
        from open_webui.models.chat_messages import ChatMessage
        from open_webui.models.chats import Chat
        from open_webui.models.shared_chats import SharedChat

        async with postgres_harness(modules, monkeypatch) as (sessions, engine):
            async with sessions() as run_session, sessions() as delete_session:
                run_pid = await run_session.scalar(text('SELECT pg_backend_pid()'))
                delete_pid = await delete_session.scalar(text('SELECT pg_backend_pid()'))
                reached, resume = asyncio.Event(), asyncio.Event()
                tasks = []
                try:
                    if first == 'run':
                        pause_before_commit(run_session, monkeypatch, reached, resume)
                        run_task = asyncio.create_task(AutomationRuns.insert(
                            'fixture', 'success', chat_id='target', db=run_session))
                        tasks.append(run_task)
                        await asyncio.wait_for(reached.wait(), 5)
                        delete_task = asyncio.create_task(perform_delete(kind, delete_session))
                        tasks.append(delete_task)
                        await wait_until_blocked(engine, delete_pid, run_pid)
                    else:
                        pause_after_lock(delete_session, monkeypatch, reached, resume,
                                         scalar=kind in {'single', 'admin'})
                        delete_task = asyncio.create_task(perform_delete(kind, delete_session))
                        tasks.append(delete_task)
                        await asyncio.wait_for(reached.wait(), 5)
                        run_task = asyncio.create_task(AutomationRuns.insert(
                            'fixture', 'success', chat_id='target', db=run_session))
                        tasks.append(run_task)
                        await wait_until_blocked(engine, run_pid, delete_pid)
                    resume.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), 5)
                    assert delete_task.result() is True
                    inserted = run_task.result()
                    if first == 'delete':
                        assert inserted.chat_id is None
                finally:
                    resume.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            async with sessions() as session:
                assert await session.get(Chat, 'target') is None
                assert await session.get(ChatMessage, 'target-message') is None
                assert await session.get(SharedChat, 'target-share') is None
                assert (await session.get(AutomationRun, 'target-run')).chat_id is None
                assert (await session.get(AutomationRun, inserted.id)).chat_id is None
                assert await session.get(Chat, 'other-owner') is not None
                assert (await session.get(AutomationRun, 'other-owner-run')).chat_id == 'other-owner'
                if kind != 'all':
                    assert await session.get(Chat, 'other-folder') is not None
                    assert await session.get(SharedChat, 'other-folder-share') is not None

    asyncio.run(scenario())


@pytest.mark.parametrize('creation', ['new', 'import'])
@pytest.mark.parametrize('first', ['create', 'delete'])
def test_account_delete_and_late_chat_creation_share_the_owner_lock(
    modules, monkeypatch, creation, first
):
    async def scenario():
        from open_webui.models.chats import Chat, Chats, ChatForm, ChatImportForm
        from open_webui.models.groups import GroupMember
        from open_webui.models.users import User, Users

        async with postgres_harness(modules, monkeypatch) as (sessions, engine):
            async with sessions() as create_session, sessions() as delete_session:
                create_pid = await create_session.scalar(text('SELECT pg_backend_pid()'))
                delete_pid = await delete_session.scalar(text('SELECT pg_backend_pid()'))
                reached, resume = asyncio.Event(), asyncio.Event()

                async def create_chat():
                    if creation == 'new':
                        return await Chats.insert_new_chat('late-chat', 'alice', ChatForm(chat={}),
                                                           db=create_session)
                    return await Chats.import_chats('alice', [ChatImportForm(chat={})], db=create_session)

                tasks = []
                try:
                    if first == 'create':
                        pause_before_commit(create_session, monkeypatch, reached, resume)
                        create_task = asyncio.create_task(create_chat())
                        tasks.append(create_task)
                        await asyncio.wait_for(reached.wait(), 5)
                        delete_task = asyncio.create_task(Users.delete_user_by_id('alice', db=delete_session))
                        tasks.append(delete_task)
                        await wait_until_blocked(engine, delete_pid, create_pid)
                    else:
                        pause_after_lock(delete_session, monkeypatch, reached, resume, scalar=True)
                        delete_task = asyncio.create_task(Users.delete_user_by_id('alice', db=delete_session))
                        tasks.append(delete_task)
                        await asyncio.wait_for(reached.wait(), 5)
                        create_task = asyncio.create_task(create_chat())
                        tasks.append(create_task)
                        await wait_until_blocked(engine, create_pid, delete_pid)
                    resume.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), 5)
                    assert delete_task.result() is True
                    if first == 'delete':
                        assert create_task.result() == ([] if creation == 'import' else None)
                    else:
                        assert create_task.result()
                finally:
                    resume.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            async with sessions() as session:
                assert await session.get(User, 'alice') is None
                assert not (await session.execute(select(Chat.id).where(Chat.user_id == 'alice'))).all()
                assert await session.get(GroupMember, 'alice-membership') is None
                assert await session.get(User, 'bob') is not None
                assert await session.get(Chat, 'other-owner') is not None

    asyncio.run(scenario())
