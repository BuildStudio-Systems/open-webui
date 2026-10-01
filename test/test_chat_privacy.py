"""Private deployment policy, using real chat snapshots/grants and HTTP routes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import Request
from test_there_api import modules, harness


@pytest.mark.parametrize('value,expected', [(None,False), ('false',False), ('',False), ('1',False), ('TRUE',True)])
def test_sharing_requires_explicit_operator_opt_in(monkeypatch,value,expected):
    from open_webui.utils.chat_privacy import chat_sharing_enabled
    if value is None:monkeypatch.delenv('THERE_ENABLE_CHAT_SHARING',raising=False)
    else:monkeypatch.setenv('THERE_ENABLE_CHAT_SHARING',value)
    assert chat_sharing_enabled() is expected


@pytest.fixture(autouse=True)
def register_chat_tables(modules):
    # Register router models and their FK targets before harness.create_all.
    from open_webui.routers import chats
    from open_webui.models import messages, shared_chats


async def setup_api(h,monkeypatch):
    from open_webui.routers import chats as api
    from open_webui.models.chats import Chat
    from open_webui.models.shared_chats import SharedChat
    from open_webui.models.access_grants import AccessGrants
    monkeypatch.setattr(api,'ENABLE_ADMIN_CHAT_ACCESS',False)
    monkeypatch.setattr(api,'publish_event',AsyncMock())
    h.permissions['chat']={'share':True,'import':True}
    h.permissions['sharing']={'public_chats':True,'open_chats':True}
    async def optional_user(request:Request):
        who=request.headers.get('X-Test-User')
        return SimpleNamespace(id=who,role='admin' if who=='admin' else 'user') if who else None
    h.app.dependency_overrides[api.get_optional_verified_user]=optional_user
    h.app.include_router(api.router,prefix='/api/v1/chats')
    payload={'title':'private fixture','history':{'currentId':'m','messages':{'m':{'id':'m','role':'assistant','content':'SYNTHETIC_PRIVATE_NETWORK 10.77.0.8','done':True}}}}
    async with h.sessions() as db:
        db.add(Chat(id='source',user_id='alice',chat=payload,title='private fixture',meta={},created_at=1,updated_at=1,share_id='snapshot'))
        await db.flush()
        db.add(SharedChat(id='snapshot',chat_id='source',user_id='alice',chat=payload,title='private fixture',created_at=1,updated_at=1))
        await db.commit()
        await AccessGrants.set_access_grants('shared_chat','source',[{'principal_type':'anyone','permission':'read'}],db=db)
    return api


def test_old_public_snapshot_is_owner_only_and_cannot_be_cloned(modules,monkeypatch,tmp_path):
    async def run():
        monkeypatch.delenv('THERE_ENABLE_CHAT_SHARING',raising=False)
        from open_webui.models import chats as models
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup_api(h,monkeypatch)
            monkeypatch.setattr(api,'ENABLE_ADMIN_CHAT_ACCESS',True)
            imports=AsyncMock();monkeypatch.setattr(api.Chats,'import_chats',imports)
            for who in (None,'bob','admin'):
                headers={'X-Test-User':who} if who else {}
                response=await h.client.get('/api/v1/chats/share/snapshot',headers=headers)
                assert response.status_code==401
                assert 'SYNTHETIC_PRIVATE_NETWORK' not in response.text
                if who:
                    clone=await h.client.post('/api/v1/chats/snapshot/clone/shared',headers=headers)
                    assert clone.status_code==401,clone.text
                    assert 'SYNTHETIC_PRIVATE_NETWORK' not in clone.text
            imports.assert_not_awaited()
            own=await h.client.get('/api/v1/chats/share/snapshot',headers={'X-Test-User':'alice'})
            assert own.status_code==200 and 'SYNTHETIC_PRIVATE_NETWORK' in own.text
            # The original chat's grants cannot bypass the snapshot policy.
            monkeypatch.setattr(models,'ENABLE_ADMIN_CHAT_ACCESS',False)
            async with h.sessions() as db:
                assert await models.Chats.get_chat_by_id_for_user('source',SimpleNamespace(id='bob',role='user'),db=db) is None
                assert await models.Chats.get_chat_by_id_for_user('source',SimpleNamespace(id='alice',role='user'),db=db) is not None
    asyncio.run(run())


def test_publication_denied_but_revocation_and_private_chat_survive(modules,monkeypatch,tmp_path):
    async def run():
        monkeypatch.setenv('THERE_ENABLE_CHAT_SHARING','false')
        from open_webui.models.shared_chats import SharedChats
        from open_webui.models.access_grants import AccessGrants
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup_api(h,monkeypatch)
            for who in ('alice','admin'):
                headers={'X-Test-User':who}
                r=await h.client.post('/api/v1/chats/source/share',headers=headers)
                assert r.status_code==403
                r=await h.client.post('/api/v1/chats/shared/source/access/update',headers=headers,json={'access_grants':[{'principal_type':'anyone','permission':'read'}]})
                assert r.status_code==403
            r=await h.client.post('/api/v1/chats/shared/source/access/update',headers={'X-Test-User':'alice'},json={'access_grants':[]})
            assert r.status_code==200,r.text
            async with h.sessions() as db:
                assert not await AccessGrants.get_grants_by_resource('shared_chat','source',db=db)
                assert await SharedChats.get_by_id('snapshot',db=db) is not None
                assert await api.Chats.get_chat_by_id_and_user_id('source','alice',db=db) is not None
            # Unsharing still works; it does not delete the original conversation.
            r=await h.client.delete('/api/v1/chats/source/share',headers={'X-Test-User':'alice'})
            assert r.status_code==200,r.text
            async with h.sessions() as db:
                assert await SharedChats.get_by_id('snapshot',db=db) is None
                assert await api.Chats.get_chat_by_id_and_user_id('source','alice',db=db) is not None
    asyncio.run(run())


def test_opt_in_keeps_read_and_clone_permissions_identical(modules,monkeypatch,tmp_path):
    async def run():
        monkeypatch.setenv('THERE_ENABLE_CHAT_SHARING','true')
        from open_webui.models.access_grants import AccessGrants
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup_api(h,monkeypatch)
            async with h.sessions() as db:
                await AccessGrants.set_access_grants('shared_chat','source',[],db=db)
            for route,method in [('/share/snapshot','get'),('/snapshot/clone/shared','post')]:
                r=await getattr(h.client,method)('/api/v1/chats'+route,headers={'X-Test-User':'admin'})
                assert r.status_code==401,r.text
            # Explicit grant can restore legitimate opt-in sharing, without admin bypass.
            async with h.sessions() as db:
                await AccessGrants.set_access_grants('shared_chat','source',[{'principal_type':'user','principal_id':'bob','permission':'read'}],db=db)
            r=await h.client.get('/api/v1/chats/share/snapshot',headers={'X-Test-User':'bob'})
            assert r.status_code==200,r.text
            clone=await h.client.post('/api/v1/chats/snapshot/clone/shared',headers={'X-Test-User':'bob'})
            assert clone.status_code==200,clone.text
            assert 'SYNTHETIC_PRIVATE_NETWORK' in clone.text
            async with h.sessions() as db:
                assert await api.Chats.is_chat_owner(clone.json()['id'],'bob',db=db)
    asyncio.run(run())
