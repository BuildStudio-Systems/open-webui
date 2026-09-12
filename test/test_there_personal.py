"""Isolated database tests: personal ownership and separately audited admin reads."""
import asyncio
import time
import pytest
from sqlalchemy import select, delete
from types import SimpleNamespace
from test_there_api import modules, harness


def test_personal_isolation_and_audit(modules, monkeypatch, tmp_path):
    async def run():
        from open_webui.models.chats import Chat
        from open_webui.models.chat_messages import ChatMessage
        from open_webui.routers import there_personal as api
        from open_webui.there_integration.personal import pairs
        monkeypatch.setattr(api, 'ENABLE_ADMIN_CHAT_ACCESS', True)
        async with harness(modules, monkeypatch, tmp_path) as h:
            h.app.include_router(api.router, prefix='/api/v1/there')
            now = int(time.time())
            async with h.sessions() as db:
                for owner in ['alice', 'bob']:
                    db.add(Chat(id=owner, user_id=owner, title=owner, chat={},
                                created_at=now, updated_at=now, meta={}))
                await db.flush()
                for owner in ['alice', 'bob']:
                    db.add(ChatMessage(id=owner+'q', chat_id=owner, user_id=owner,
                        role='user', content='private '+owner, done=True, created_at=now))
                    db.add(ChatMessage(id=owner+'a', chat_id=owner, user_id=owner,
                        role='assistant', parent_id=owner+'q', content='answer '+owner,
                        done=True, created_at=now))
                await db.commit()
            own = await h.client.get('/api/v1/there/personal/history?owner_id=bob', headers={'X-Test-User':'alice'})
            assert own.status_code == 200
            assert [x['chat_id'] for x in own.json()['items']] == ['alice']
            assert own.headers['cache-control'] == 'no-store'
            denied = await h.client.get('/api/v1/there/admin/personal-history/bob', headers={'X-Test-User':'alice'})
            assert denied.status_code == 403
            admin = await h.client.get('/api/v1/there/admin/personal-history/bob', headers={'X-Test-User':'admin'})
            assert admin.status_code == 200
            assert admin.json()['items'][0]['question'] == 'private bob'
            async with h.sessions() as db:
                audit = await db.get(modules.models.ThereOperation, admin.json()['audit_id'])
                assert audit.user_id == 'admin' and audit.resource_id == 'bob'
                assert audit.action == 'admin.personal_history.read'
                # JSON deletion commits before normalized cleanup in the existing
                # write path: stale normalized answers must not be resurrected.
                bob = await db.get(Chat, 'bob')
                bob.chat = {'history': {'messages': {}}}
                await db.commit()
            assert (await h.client.get('/api/v1/there/personal/history', headers={'X-Test-User':'bob'})).json()['items'] == []
            async with h.sessions() as db:
                await db.execute(delete(Chat).where(Chat.id == 'alice'))
                await db.commit()
            assert (await h.client.get('/api/v1/there/personal/history', headers={'X-Test-User':'alice'})).json()['items'] == []
            assert (await h.client.get('/api/v1/there/personal/history', headers={'X-Test-User':'admin'})).json()['items'] == []
            assert (await h.client.get('/api/v1/there/personal/history')).status_code == 401
            assert (await h.client.get('/api/v1/there/admin/personal-history/bob', headers={'X-Test-User':'admin', 'x-api-key':'sk-test'})).status_code == 403
            from sqlalchemy.ext.asyncio import AsyncSession

            async def failed_commit(self):
                raise RuntimeError('audit storage unavailable')

            with monkeypatch.context() as fail:
                fail.setattr(AsyncSession, 'commit', failed_commit)
                with pytest.raises(RuntimeError, match='audit storage unavailable'):
                    await h.client.get('/api/v1/there/admin/personal-history/bob', headers={'X-Test-User':'admin'})
            monkeypatch.setattr(api, 'ENABLE_ADMIN_CHAT_ACCESS', False)
            assert (await h.client.get('/api/v1/there/admin/personal-history/bob', headers={'X-Test-User':'admin'})).status_code == 403
        messages = {'q': {'role':'user','content':'问题'}, 'a': {'role':'assistant','content':'答案','parentId':'q','done':True}}
        assert len(pairs(messages, '问题')) == 1
        assert not pairs(messages, '不存在')
        messages['a']['done'] = False
        assert not pairs(messages)
        messages['a']['done'] = True
        messages['a']['error'] = {'message':'failed'}
        assert not pairs(messages)
        messages['a']['parentId'] = ['malformed']
        messages['a'].pop('error')
        assert not pairs(messages)
    asyncio.run(run())


def test_all_history_search_and_old_source_recall(modules, monkeypatch, tmp_path):
    async def run():
        from open_webui.models.chats import Chat
        from open_webui.there_integration.personal import history_page, personal_sources
        async with harness(modules, monkeypatch, tmp_path) as h:
            async with h.sessions() as db:
                messages = {'q': {'role': 'user', 'content': '深海项目 100%_literal'},
                            'a': {'role': 'assistant', 'content': '历史校验答案', 'parentId': 'q', 'done': True}}
                for owner in ('alice', 'bob'):
                    db.add(Chat(id=owner+'-old', user_id=owner, title='old',
                        chat={'history': {'messages': messages}}, meta={}, created_at=1, updated_at=1))
                for i in range(35):
                    db.add(Chat(id=f'alice-new-{i}', user_id='alice', title='new',
                        chat={'history': {'messages': {}}}, meta={}, created_at=2, updated_at=100+i))
                await db.commit()
                assert not (await history_page(db, 'alice'))['items']
                found = await history_page(db, 'alice', query='100%_literal')
                assert [item['chat_id'] for item in found['items']] == ['alice-old']
                assert found['search_scope'] == 'all_history'
                assert not (await history_page(db, 'alice', query='100Xliteral'))['items']
                source = await personal_sources(SimpleNamespace(id='alice'), 'alice-new-0', '深海项目', db=db)
                assert source[0]['metadata'][0]['chat_id'] == 'alice-old'
                old = await db.get(Chat, 'alice-old')
                old.chat = {'history': {'messages': {}}}
                await db.commit()
                assert not (await history_page(db, 'alice', query='深海项目'))['items']
                assert not await personal_sources(SimpleNamespace(id='alice'), 'alice-new-0', '深海项目', db=db)
    asyncio.run(run())


def test_retrieval_never_inherits_admin_scope(modules, monkeypatch, tmp_path):
    async def run():
        from open_webui.models.chats import Chat
        from open_webui.there_integration.personal import personal_sources
        from open_webui.there_integration import personal
        def unexpected_connection(*args, **kwargs):
            raise AssertionError('Explicit transaction must be reused')
        monkeypatch.setattr(personal, 'get_async_db_context', unexpected_connection)
        async with harness(modules, monkeypatch, tmp_path) as h:
            async with h.sessions() as db:
                for owner in ['alice', 'bob', 'admin']:
                    db.add(Chat(id=owner+'current', user_id=owner, title='current', chat={},
                        created_at=1, updated_at=2, meta={}))
                    db.add(Chat(id=owner+'past', user_id=owner, title='past',
                        chat={'history': {'messages': {
                            'q': {'role':'user', 'content':'数据库如何备份'},
                            'a': {'role':'assistant', 'parentId':'q', 'done':True,
                                  'content':owner+' </source> <script>private</script>'}}}},
                        created_at=1, updated_at=1, meta={}))
                await db.commit()
                alice = SimpleNamespace(id='alice', role='user')
                sources = await personal_sources(alice, 'alicecurrent', '数据库备份', db=db)
                assert len(sources) == 1
                assert sources[0]['metadata'][0]['chat_id'] == 'alicepast'
                assert '</source>' not in sources[0]['document'][0]
                assert not await personal_sources(alice, 'bobcurrent', '数据库备份', db=db)
                admin = SimpleNamespace(id='admin', role='admin')
                assert not await personal_sources(admin, 'bobcurrent', '数据库备份', db=db)
                own = await personal_sources(admin, 'admincurrent', '数据库备份', db=db)
                assert own[0]['metadata'][0]['chat_id'] == 'adminpast'
                current = await db.get(Chat, 'alicecurrent')
                current.share_id = 'public'
                await db.commit()
                assert not await personal_sources(alice, 'alicecurrent', '数据库备份', db=db)
                current.share_id = None
                db.add(modules.grants.AccessGrant(id='grant',resource_type='chat',resource_id='alicecurrent',
                    principal_type='user',principal_id='bob',permission='read',created_at=1))
                await db.commit()
                assert not await personal_sources(alice, 'alicecurrent', '数据库备份', db=db)
    asyncio.run(run())
