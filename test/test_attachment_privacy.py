"""Private conversations must not be recovered through attachment APIs or RAG."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_there_api import modules, harness

MARKER = 'SYNTHETIC_PRIVATE_RECEIPT'


@pytest.fixture(autouse=True)
def register_tables(modules, monkeypatch):
    from open_webui.routers import files
    from open_webui.models import messages, shared_chats
    from open_webui import config
    from open_webui.models import chats
    monkeypatch.setenv('THERE_ENABLE_CHAT_SHARING', 'false')
    monkeypatch.setattr(config, 'ENABLE_ADMIN_CHAT_ACCESS', False)
    monkeypatch.setattr(chats, 'ENABLE_ADMIN_CHAT_ACCESS', False)


async def setup(h, tmp_path):
    from open_webui.routers import files as api
    from open_webui.models.files import File
    from open_webui.models.chats import Chat, ChatFile
    from open_webui.models.access_grants import AccessGrants
    h.app.include_router(api.router, prefix='/api/v1/files')
    path=tmp_path/'private.png';path.write_bytes(MARKER.encode())
    async with h.sessions() as db:
        db.add(File(id='private',user_id='alice',filename='private.png',path=str(path),data={'content':MARKER,'status':'completed'},meta={'content_type':'image/png'},created_at=1,updated_at=1))
        db.add(Chat(id='private-chat',user_id='alice',title='private',chat={'history':{'currentId':'m','messages':{'m':{'id':'m','role':'assistant','content':MARKER}}}},meta={},created_at=1,updated_at=1,share_id='old-share'))
        await db.flush()
        db.add(ChatFile(id='link',chat_id='private-chat',message_id='m',file_id='private',user_id='alice',created_at=1,updated_at=1))
        await db.commit()
        await AccessGrants.set_access_grants('shared_chat','private-chat',[{'principal_type':'user','principal_id':'*','permission':'read'}],db=db)
    return api


@pytest.mark.parametrize('suffix', ['', '/data/content', '/content', '/content/private.png', '/process/status'])
def test_file_routes_reject_old_shares_and_unconditional_admin(modules,monkeypatch,tmp_path,suffix):
    async def run():
        async with harness(modules,monkeypatch,tmp_path) as h:
            await setup(h,tmp_path)
            for user in ['bob','admin']:
                r=await h.client.get('/api/v1/files/private'+suffix,headers={'X-Test-User':user})
                assert r.status_code==404,r.text
                assert MARKER not in r.text
            r=await h.client.get('/api/v1/files/private'+suffix,headers={'X-Test-User':'alice'})
            assert r.status_code==200,r.text
            if not suffix:
                assert 'path' not in r.json()  # Storage paths are server-internal.
    asyncio.run(run())


def test_private_files_not_listed_for_other_admin(modules,monkeypatch,tmp_path):
    async def run():
        async with harness(modules,monkeypatch,tmp_path) as h:
            await setup(h,tmp_path)
            r=await h.client.get('/api/v1/files/',headers={'X-Test-User':'admin'})
            assert r.status_code==200,r.text
            assert r.json()['items']==[]
    asyncio.run(run())


def test_old_share_cannot_feed_chat_attachment_or_image_or_vector_search(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui.retrieval.utils import get_sources_from_items, filter_accessible_collections
        from open_webui.utils.files import get_image_base64_from_file_id
        async with harness(modules,monkeypatch,tmp_path) as h:
            await setup(h,tmp_path)
            for name,role in [('bob','user'),('admin','admin'),('alice','user')]:
                user=SimpleNamespace(id=name,role=role)
                sources=await get_sources_from_items(None,[{'type':'chat','id':'private-chat'}],[],None,1,None,1,0,0,False,user=user)
                assert (MARKER in str(sources)) == (name=='alice')
                image=await get_image_base64_from_file_id('private',user)
                assert bool(image)==(name=='alice')
                collections=await filter_accessible_collections({'file-private'},user)
                assert collections==({'file-private'} if name=='alice' else set())
    asyncio.run(run())


def test_explicit_knowledge_grant_remains_read_only(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui.models.knowledge import Knowledge, KnowledgeFile
        from open_webui.models.access_grants import AccessGrants
        from open_webui.utils.access_control.files import has_access_to_file
        async with harness(modules,monkeypatch,tmp_path) as h:
            await setup(h,tmp_path)
            async with h.sessions() as db:
                db.add(Knowledge(id='kb',user_id='alice',name='shared',description='',meta={},created_at=1,updated_at=1))
                await db.flush()
                db.add(KnowledgeFile(id='kf',knowledge_id='kb',file_id='private',user_id='alice',created_at=1,updated_at=1))
                await db.commit()
                await AccessGrants.set_access_grants('knowledge','kb',[{'principal_type':'user','principal_id':'bob','permission':'read'}],db=db)
                user=SimpleNamespace(id='bob',role='user')
                assert await has_access_to_file('private','read',user,db=db)
                assert not await has_access_to_file('private','write',user,db=db)
            r=await h.client.get('/api/v1/files/private/data/content',headers={'X-Test-User':'bob'})
            assert r.status_code==200 and MARKER in r.text
    asyncio.run(run())


def test_admin_cannot_modify_or_delete_private_attachment(modules,monkeypatch,tmp_path):
    async def run():
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup(h,tmp_path)
            process=AsyncMock();monkeypatch.setattr(api,'process_file',process)
            for method,path,body in [('post','/data/content/update',{'content':'overwritten'}),('post','/rename',{'filename':'changed'}),('delete','',None)]:
                r=await h.client.request(method,'/api/v1/files/private'+path,json=body,headers={'X-Test-User':'admin'})
                assert r.status_code==404,r.text
            process.assert_not_awaited()
            r=await h.client.get('/api/v1/files/private/data/content',headers={'X-Test-User':'alice'})
            assert MARKER in r.text
    asyncio.run(run())


def test_html_and_builtin_file_tools_follow_private_policy(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui.tools.builtin import _has_read_access_to_file
        from open_webui.utils.access_control.files import get_accessible_folder_files
        from open_webui.models.files import Files
        from open_webui.models.users import UserModel
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup(h,tmp_path)
            monkeypatch.setattr(api.Users,'get_user_by_id',AsyncMock(return_value=SimpleNamespace(id='alice',role='admin')))
            file=await Files.get_file_by_id('private')
            for name,role in [('admin','admin'),('bob','user'),('alice','user')]:
                user=UserModel(id=name,role=role,name=name,email=name+'@example.test',created_at=1,updated_at=1,last_active_at=1)
                assert await _has_read_access_to_file(file,user.model_dump()) == (name=='alice')
                entries=await get_accessible_folder_files([{'type':'file','id':'private'}],user)
                assert bool(entries)==(name=='alice')
                r=await h.client.get('/api/v1/files/private/content/html',headers={'X-Test-User':name})
                assert r.status_code==(200 if name=='alice' else 404),r.text
    asyncio.run(run())


def test_global_delete_and_processing_reject_without_side_effects(modules,monkeypatch,tmp_path):
    async def run():
        from fastapi import HTTPException
        from open_webui.routers import retrieval
        async with harness(modules,monkeypatch,tmp_path) as h:
            api=await setup(h,tmp_path)
            delete=AsyncMock();monkeypatch.setattr(api.Files,'delete_all_files',delete)
            with pytest.raises(HTTPException) as error:
                await api.delete_all_files(None,user=SimpleNamespace(id='admin',role='admin'),db=None)
            assert error.value.status_code==403
            delete.assert_not_awaited()
            monkeypatch.setattr(retrieval,'get_retrieval_config',AsyncMock(return_value={}))
            with pytest.raises(HTTPException):
                await retrieval.process_file(None,retrieval.ProcessFileForm(file_id='private',content='overwrite'),user=SimpleNamespace(id='admin',role='admin'),db=None)
            file=await api.Files.get_file_by_id('private')
            assert file.data['content']==MARKER
    asyncio.run(run())


def test_explicit_sharing_and_admin_opt_in_compatibility(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui import config
        from open_webui.utils.chat_privacy import can_bypass_private_content_access
        from open_webui.utils.access_control.files import has_access_to_file
        async with harness(modules,monkeypatch,tmp_path) as h:
            await setup(h,tmp_path)
            user=SimpleNamespace(id='bob',role='user')
            assert not await has_access_to_file('private','read',user)
            monkeypatch.setenv('THERE_ENABLE_CHAT_SHARING','true')
            assert await has_access_to_file('private','read',user)
            assert not await has_access_to_file('private','write',user)
            for bypass,private,expected in [(False,False,False),(True,False,False),(False,True,False),(True,True,True)]:
                monkeypatch.setattr(config,'BYPASS_ADMIN_ACCESS_CONTROL',bypass)
                monkeypatch.setattr(config,'ENABLE_ADMIN_CHAT_ACCESS',private)
                assert can_bypass_private_content_access('admin') is expected
                assert not can_bypass_private_content_access('user')
    asyncio.run(run())
