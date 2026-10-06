"""Model-level ownership regression coverage for single-chat deletion."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_there_api import harness, modules


@pytest.fixture(autouse=True)
def register_chat_delete_tables(modules):
    """Register every table used by the deletion path before create_all."""
    from open_webui.models.automations import AutomationRun
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.chats import Chat
    from open_webui.models.groups import Group, GroupMember
    from open_webui.models.shared_chats import SharedChat

    return Chat, ChatMessage, AutomationRun, SharedChat, Group, GroupMember


def test_delete_chat_by_id_and_user_id_gates_every_side_effect(modules, monkeypatch, tmp_path):
    async def scenario():
        from open_webui.models.automations import AutomationRun
        from open_webui.models.chat_messages import ChatMessage
        from open_webui.models.chats import Chat, Chats
        from open_webui.models.shared_chats import SharedChat

        async with harness(modules, monkeypatch, tmp_path) as context:
            chat_id = 'owned-chat'
            message_id = 'owned-chat-message'
            run_id = 'owned-chat-run'
            share_id = 'owned-chat-share'
            payload = {'history': {'currentId': 'message', 'messages': {}}}

            async with context.sessions() as session:
                session.add(
                    Chat(
                        id=chat_id,
                        user_id='alice',
                        title='Private chat',
                        chat=payload,
                        share_id=share_id,
                        meta={},
                        created_at=1,
                        updated_at=1,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        ChatMessage(
                            id=message_id,
                            chat_id=chat_id,
                            user_id='alice',
                            role='user',
                            content='private message',
                            created_at=1,
                            updated_at=1,
                        ),
                        AutomationRun(
                            id=run_id,
                            automation_id='automation',
                            chat_id=chat_id,
                            status='success',
                            created_at=1,
                        ),
                        SharedChat(
                            id=share_id,
                            chat_id=chat_id,
                            user_id='alice',
                            title='Private chat',
                            chat=payload,
                            created_at=1,
                            updated_at=1,
                        ),
                    ]
                )
                await session.commit()

            assert await Chats.delete_chat_by_id_and_user_id(chat_id, 'bob') is False

            async with context.sessions() as session:
                assert await session.get(Chat, chat_id) is not None
                assert await session.get(ChatMessage, message_id) is not None
                assert (await session.get(AutomationRun, run_id)).chat_id == chat_id
                assert await session.get(SharedChat, share_id) is not None

            assert await Chats.delete_chat_by_id_and_user_id(chat_id, 'alice') is True

            async with context.sessions() as session:
                assert await session.get(Chat, chat_id) is None
                assert await session.get(ChatMessage, message_id) is None
                assert (await session.get(AutomationRun, run_id)).chat_id is None
                assert await session.get(SharedChat, share_id) is None

    asyncio.run(scenario())


@pytest.mark.parametrize('session_sharing', [False, True])
def test_account_delete_keeps_cleanup_and_owner_delete_in_one_transaction(
    modules, monkeypatch, tmp_path, session_sharing
):
    async def scenario():
        from open_webui.internal import db as db_module
        from open_webui.models.automations import AutomationRun
        from open_webui.models.chats import Chat, Chats, ChatForm, ChatImportForm
        from open_webui.models.groups import Group, GroupMember
        from open_webui.models.users import User, Users

        async with harness(modules, monkeypatch, tmp_path) as context:
            monkeypatch.setattr(db_module, 'DATABASE_ENABLE_SESSION_SHARING', session_sharing)
            async with context.sessions() as session:
                session.add(User(id='alice', email='alice@test.invalid', name='Alice', role='user'))
                session.add(Group(id='group-one', user_id='owner', name='Group', description='',
                                  created_at=1, updated_at=1))
                session.add(GroupMember(id='member-one', group_id='group-one', user_id='alice'))
                session.add(Chat(id='account-chat', user_id='alice', title='Fixture', chat={}, meta={}))
                session.add(AutomationRun(id='account-run', automation_id='fixture',
                                          chat_id='account-chat', status='success', created_at=1))
                await session.commit()

            cleanup = Chats._delete_chats_by_user_id_in_session
            commits = []

            async def inspect_cleanup(user_id, session):
                await cleanup(user_id, session)
                assert session.in_transaction()
                assert await session.get(User, user_id) is not None
                commit = session.commit

                async def counted_commit():
                    commits.append(True)
                    await commit()

                monkeypatch.setattr(session, 'commit', counted_commit)

            monkeypatch.setattr(Chats, '_delete_chats_by_user_id_in_session', inspect_cleanup)
            assert await Users.delete_user_by_id('alice') is True
            assert len(commits) == 1
            async with context.sessions() as session:
                assert await session.get(User, 'alice') is None
                assert await session.get(Chat, 'account-chat') is None
                assert (await session.get(AutomationRun, 'account-run')).chat_id is None
                assert await session.get(GroupMember, 'member-one') is None

            assert await Chats.insert_new_chat('late-chat', 'alice', ChatForm(chat={})) is None
            assert await Chats.import_chats('alice', [ChatImportForm(chat={})]) == []
            async with context.sessions() as session:
                assert await session.get(Chat, 'late-chat') is None

    asyncio.run(scenario())


def test_account_delete_rolls_back_chat_cleanup_when_user_delete_fails(
    modules, monkeypatch, tmp_path
):
    async def scenario():
        from sqlalchemy.sql.dml import Delete
        from open_webui.models.automations import AutomationRun
        from open_webui.models.chats import Chat, Chats
        from open_webui.models.groups import Group, GroupMember
        from open_webui.models.users import User, Users

        async with harness(modules, monkeypatch, tmp_path) as context:
            async with context.sessions() as session:
                session.add(User(id='alice', email='alice@test.invalid', name='Alice', role='user'))
                session.add(Group(id='group-one', user_id='owner', name='Group', description='',
                                  created_at=1, updated_at=1))
                session.add(GroupMember(id='member-one', group_id='group-one', user_id='alice'))
                session.add(Chat(id='retained-chat', user_id='alice', title='Fixture', chat={}, meta={}))
                session.add(AutomationRun(id='retained-run', automation_id='fixture',
                                          chat_id='retained-chat', status='success', created_at=1))
                await session.commit()

            cleanup = Chats._delete_chats_by_user_id_in_session

            async def fail_owner_delete(user_id, session):
                await cleanup(user_id, session)
                execute = session.execute

                async def failing_execute(statement, *args, **kwargs):
                    if isinstance(statement, Delete) and statement.table.name == 'user':
                        raise RuntimeError('Synthetic account delete failure')
                    return await execute(statement, *args, **kwargs)

                monkeypatch.setattr(session, 'execute', failing_execute)

            monkeypatch.setattr(Chats, '_delete_chats_by_user_id_in_session', fail_owner_delete)
            assert await Users.delete_user_by_id('alice') is False
            async with context.sessions() as session:
                assert await session.get(User, 'alice') is not None
                assert await session.get(Chat, 'retained-chat') is not None
                assert (await session.get(AutomationRun, 'retained-run')).chat_id == 'retained-chat'
                assert await session.get(GroupMember, 'member-one') is not None

    asyncio.run(scenario())


def test_chat_creation_and_import_accept_a_live_owner(modules, monkeypatch, tmp_path):
    async def scenario():
        from open_webui.models.chats import Chats, ChatForm, ChatImportForm
        from open_webui.models.users import User

        async with harness(modules, monkeypatch, tmp_path) as context:
            async with context.sessions() as session:
                session.add(User(id='alice', email='alice@test.invalid', name='Alice', role='user'))
                await session.commit()
            assert (await Chats.insert_new_chat('live-chat', 'alice', ChatForm(chat={}))).id == 'live-chat'
            imported = await Chats.import_chats('alice', [ChatImportForm(chat={})])
            assert len(imported) == 1 and imported[0].user_id == 'alice'

    asyncio.run(scenario())


def test_folder_delete_reports_failure_when_chat_cleanup_fails(modules, monkeypatch):
    # Importing the full router registers optional channel tables in the shared
    # SQLAlchemy metadata. Remove only those additions afterwards so later
    # isolated harnesses do not inherit unrelated foreign-key dependencies.
    metadata = modules.db.Base.metadata
    tables_before = set(metadata.tables)

    async def scenario():
        from fastapi import HTTPException
        from open_webui.routers import folders as api

        folder = SimpleNamespace(id='folder-one', user_id='alice')
        monkeypatch.setattr(api, 'check_folders_permission', AsyncMock())
        monkeypatch.setattr(api.Folders, 'get_folder_by_id_and_user_id', AsyncMock(return_value=folder))
        monkeypatch.setattr(api.Folders, 'get_folder_ids_by_id_and_user_id_in_subtree',
                            AsyncMock(return_value=['folder-one']))
        monkeypatch.setattr(api.Folders, 'delete_folder_by_id_and_user_id',
                            AsyncMock(return_value=['folder-one']))
        monkeypatch.setattr(api.Folders, 'get_folders_by_parent_id_and_user_id', AsyncMock(return_value=[]))
        monkeypatch.setattr(api.Chats, 'count_chats_by_folder_ids_and_user_id', AsyncMock(return_value=0))
        monkeypatch.setattr(api.Chats, 'delete_chats_by_user_id_and_folder_id', AsyncMock(return_value=False))
        event = AsyncMock()
        monkeypatch.setattr(api, 'publish_event', event)
        with pytest.raises(HTTPException) as error:
            await api.delete_folder_by_id(SimpleNamespace(), 'folder-one', True,
                                          SimpleNamespace(id='alice', role='user'), None)
        assert error.value.status_code == 400
        event.assert_not_awaited()

    try:
        asyncio.run(scenario())
    finally:
        for name in set(metadata.tables) - tables_before:
            metadata.remove(metadata.tables[name])


def test_delete_chat_rolls_back_shared_session_after_mid_transaction_error(
    modules, monkeypatch, tmp_path
):
    async def scenario():
        from open_webui.models.automations import AutomationRun
        from open_webui.models.chats import Chat, Chats
        from open_webui.internal import db as db_module

        async with harness(modules, monkeypatch, tmp_path) as context:
            chat_id = 'rollback-chat'
            run_id = 'rollback-run'

            async with context.sessions() as session:
                session.add(
                    Chat(
                        id=chat_id,
                        user_id='alice',
                        title='Private chat',
                        chat={'history': {'currentId': None, 'messages': {}}},
                        meta={},
                        created_at=1,
                        updated_at=1,
                    )
                )
                session.add(
                    AutomationRun(
                        id=run_id,
                        automation_id='automation',
                        chat_id=chat_id,
                        status='success',
                        created_at=1,
                    )
                )
                await session.commit()

            async with context.sessions() as session:
                monkeypatch.setattr(db_module, 'DATABASE_ENABLE_SESSION_SHARING', True)
                original_execute = session.execute
                calls = 0

                async def fail_second_execute(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError('synthetic delete failure')
                    return await original_execute(*args, **kwargs)

                monkeypatch.setattr(session, 'execute', fail_second_execute)
                assert (
                    await Chats.delete_chat_by_id_and_user_id(
                        chat_id, 'alice', db=session
                    )
                    is False
                )
                assert session.in_transaction() is False

            async with context.sessions() as session:
                assert await session.get(Chat, chat_id) is not None
                assert (await session.get(AutomationRun, run_id)).chat_id == chat_id

    asyncio.run(scenario())


def test_automation_run_insert_does_not_link_a_missing_chat(
    modules, monkeypatch, tmp_path
):
    async def scenario():
        from open_webui.models.automations import AutomationRuns
        from open_webui.models.chats import Chat
        from open_webui.internal import db as db_module

        async with harness(modules, monkeypatch, tmp_path) as context:
            monkeypatch.setattr(db_module, 'DATABASE_ENABLE_SESSION_SHARING', True)
            async with context.sessions() as session:
                session.add(
                    Chat(
                        id='existing-chat',
                        user_id='alice',
                        title='Private chat',
                        chat={'history': {'currentId': None, 'messages': {}}},
                        meta={},
                        created_at=1,
                        updated_at=1,
                    )
                )
                await session.commit()

            async with context.sessions() as session:
                session.add(
                    Chat(
                        id='pending-chat',
                        user_id='alice',
                        title='Pending private chat',
                        chat={'history': {'currentId': None, 'messages': {}}},
                        meta={},
                        created_at=1,
                        updated_at=1,
                    )
                )
                pending = await AutomationRuns.insert(
                    'automation', 'success', chat_id='pending-chat', db=session
                )

            existing = await AutomationRuns.insert('automation', 'success', chat_id='existing-chat')
            missing = await AutomationRuns.insert(
                'automation', 'success', chat_id='missing-chat'
            )
            channel = await AutomationRuns.insert(
                'automation', 'success', chat_id='channel:alerts'
            )

            assert existing.chat_id == 'existing-chat'
            assert pending.chat_id == 'pending-chat'
            assert missing.chat_id is None
            assert channel.chat_id == 'channel:alerts'

    asyncio.run(scenario())


def test_all_chat_delete_entry_points_cleanup_locked_dependencies(
    modules, monkeypatch, tmp_path
):
    async def scenario():
        from open_webui.models.automations import AutomationRun
        from open_webui.models.chat_messages import ChatMessage
        from open_webui.models.chats import Chat, Chats
        from open_webui.models.shared_chats import SharedChat

        async with harness(modules, monkeypatch, tmp_path) as context:
            payload = {'history': {'currentId': None, 'messages': {}}}
            chat_specs = (
                ('admin-chat', 'folder-admin'),
                ('folder-chat', 'folder-one'),
                ('user-chat', 'folder-two'),
            )
            async with context.sessions() as session:
                for chat_id, folder_id in chat_specs:
                    share_id = f'{chat_id}-share'
                    session.add(
                        Chat(
                            id=chat_id,
                            user_id='alice',
                            title=chat_id,
                            chat=payload,
                            share_id=share_id,
                            folder_id=folder_id,
                            meta={},
                            created_at=1,
                            updated_at=1,
                        )
                    )
                    await session.flush()
                    session.add_all(
                        [
                            ChatMessage(
                                id=f'{chat_id}-message',
                                chat_id=chat_id,
                                user_id='alice',
                                role='user',
                                content='private message',
                                created_at=1,
                                updated_at=1,
                            ),
                            AutomationRun(
                                id=f'{chat_id}-run',
                                automation_id='automation',
                                chat_id=chat_id,
                                status='success',
                                created_at=1,
                            ),
                            SharedChat(
                                id=share_id,
                                chat_id=chat_id,
                                user_id='alice',
                                title=chat_id,
                                chat=payload,
                                created_at=1,
                                updated_at=1,
                            ),
                        ]
                    )
                # This deliberately models a share row whose creator matches
                # the bulk-deleted user while its still-live parent Chat does
                # not.  The bulk path must scope snapshots to its locked Chat
                # IDs instead of using a broad SharedChat.user_id predicate.
                session.add(
                    Chat(
                        id='survivor-chat',
                        user_id='bob',
                        title='survivor-chat',
                        chat=payload,
                        share_id='survivor-chat-share',
                        folder_id='folder-three',
                        meta={},
                        created_at=1,
                        updated_at=1,
                    )
                )
                await session.flush()
                session.add(
                    SharedChat(
                        id='survivor-chat-share',
                        chat_id='survivor-chat',
                        user_id='alice',
                        title='survivor-chat',
                        chat=payload,
                        created_at=1,
                        updated_at=1,
                    )
                )
                await session.commit()

            assert await Chats.delete_chat_by_id('admin-chat') is True
            assert (
                await Chats.delete_chats_by_user_id_and_folder_id(
                    'alice', 'folder-one'
                )
                is True
            )

            async with context.sessions() as session:
                assert await session.get(Chat, 'admin-chat') is None
                assert await session.get(Chat, 'folder-chat') is None
                assert await session.get(Chat, 'user-chat') is not None
                assert (await session.get(AutomationRun, 'admin-chat-run')).chat_id is None
                assert (await session.get(AutomationRun, 'folder-chat-run')).chat_id is None
                assert (await session.get(AutomationRun, 'user-chat-run')).chat_id == 'user-chat'

            assert await Chats.delete_chats_by_user_id('alice') is True

            async with context.sessions() as session:
                for chat_id, _ in chat_specs:
                    assert await session.get(Chat, chat_id) is None
                    assert await session.get(ChatMessage, f'{chat_id}-message') is None
                    assert await session.get(SharedChat, f'{chat_id}-share') is None
                    assert (await session.get(AutomationRun, f'{chat_id}-run')).chat_id is None
                assert await session.get(Chat, 'survivor-chat') is not None
                assert await session.get(SharedChat, 'survivor-chat-share') is not None

    asyncio.run(scenario())
