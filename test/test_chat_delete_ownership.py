"""Model-level ownership regression coverage for single-chat deletion."""

import asyncio

import pytest

from test_there_api import harness, modules


@pytest.fixture(autouse=True)
def register_chat_delete_tables(modules):
    """Register every table used by the deletion path before create_all."""
    from open_webui.models.automations import AutomationRun
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.chats import Chat
    from open_webui.models.shared_chats import SharedChat

    return Chat, ChatMessage, AutomationRun, SharedChat


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
