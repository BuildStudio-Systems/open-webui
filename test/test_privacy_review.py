"""Failure, audit, legacy cache and web-fetch boundaries with synthetic inputs."""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from test_there_api import modules, harness

SECRET='SYNTHETIC_SECRET_49fd'
FAILURE=RuntimeError('10.12.34.56 internal.host https://example.test/?token='+SECRET)


@pytest.fixture(autouse=True)
def register(modules,monkeypatch):
    from open_webui.routers import files
    from open_webui.models import messages
    from open_webui import config
    monkeypatch.setattr(config,'ENABLE_ADMIN_CHAT_ACCESS',False)


def test_builtin_web_errors_never_echo_upstream_data(modules,monkeypatch,caplog):
    async def run():
        from open_webui.tools import builtin
        monkeypatch.setattr(builtin.Config,'get',AsyncMock(return_value=3))
        monkeypatch.setattr(builtin,'_search_web',AsyncMock(side_effect=FAILURE))
        monkeypatch.setattr(builtin,'get_content_from_url',AsyncMock(side_effect=FAILURE))
        caplog.set_level(logging.DEBUG)
        outputs=[await builtin.search_web('public query',__request__=object()),await builtin.fetch_url('https://example.test',__request__=object())]
        assert all('error' in x for x in outputs)
        assert SECRET not in str(outputs)+caplog.text
        assert '10.12.34.56' not in str(outputs)+caplog.text
    asyncio.run(run())


def test_web_processing_errors_use_fixed_messages(modules,monkeypatch,caplog):
    async def run():
        from open_webui.routers import retrieval
        monkeypatch.setattr(retrieval,'get_retrieval_config',AsyncMock(return_value=SimpleNamespace()))
        monkeypatch.setattr(retrieval,'get_content_from_url',AsyncMock(side_effect=FAILURE))
        monkeypatch.setattr(retrieval,'_fetch_url',AsyncMock(side_effect=FAILURE))
        caplog.set_level(logging.DEBUG)
        for handler in [retrieval.process_url,retrieval.process_web]:
            with pytest.raises(HTTPException) as error:
                await handler(None,retrieval.ProcessUrlForm(url='https://example.test/?q='+SECRET),user=SimpleNamespace(id='alice',role='user'))
            assert SECRET not in str(error.value.detail)+caplog.text
            assert error.value.status_code==400
    asyncio.run(run())


def test_file_failure_persistence_and_events_do_not_save_upstream_error(modules,monkeypatch,tmp_path,caplog):
    async def run():
        from open_webui.models.files import File,Files
        from open_webui.routers import retrieval
        async with harness(modules,monkeypatch,tmp_path) as h:
            async with h.sessions() as db:
                db.add(File(id='f',user_id='alice',filename='private.txt',path=None,data={'content':SECRET},meta={},created_at=1,updated_at=1));await db.commit()
            monkeypatch.setattr(retrieval,'get_retrieval_config',AsyncMock(return_value=SimpleNamespace(BYPASS_EMBEDDING_AND_RETRIEVAL=False)))
            monkeypatch.setattr(retrieval,'run_in_threadpool',AsyncMock(side_effect=FAILURE))
            emitted=AsyncMock();monkeypatch.setattr(retrieval,'publish_event',emitted)
            caplog.set_level(logging.DEBUG,logger=retrieval.__name__)
            async with h.sessions() as db:
                with pytest.raises(HTTPException) as error:
                    await retrieval.process_file(None,retrieval.ProcessFileForm(file_id='f'),user=SimpleNamespace(id='alice',role='user'),db=db)
            f=await Files.get_file_by_id('f')
            assert f.data['status']=='failed'
            assert SECRET not in f.data['error']+str(error.value.detail)+str(emitted.call_args_list)+caplog.text
            assert SECRET==f.data['content']  # The owner's actual document is preserved.
    asyncio.run(run())


@pytest.mark.parametrize('level',['METADATA','REQUEST','REQUEST_RESPONSE'])
def test_private_audit_never_buffers_bodies_or_query_strings(modules,monkeypatch,level):
    async def run():
        from open_webui.utils import audit as audit_module
        from open_webui.utils.audit import AuditLoggingMiddleware,AuditLevel
        monkeypatch.setattr(audit_module,'AUDIT_LOG_LEVEL',level)
        monkeypatch.delenv('THERE_ENABLE_CONTENT_LOGGING',raising=False)
        entries=[];seen=[]
        async def app(scope,receive,send):
            seen.append(await receive())
            await send({'type':'http.response.start','status':201,'headers':[]})
            await send({'type':'http.response.body','body':SECRET.encode()})
        audit=AuditLoggingMiddleware(app,audit_level=AuditLevel(level))
        monkeypatch.setattr(audit,'_get_authenticated_user',AsyncMock(return_value=None))
        monkeypatch.setattr(audit.audit_logger,'write',entries.append)
        capture=AsyncMock();monkeypatch.setattr(audit,'_capture_request',capture)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=audit),base_url='https://there.test') as client:
            response=await client.post('/api/v1/chat/completions?token='+SECRET,content=SECRET,headers={'Authorization':'Bearer '+SECRET})
        assert response.status_code==201 and response.text==SECRET
        assert seen[0]['body']==SECRET.encode()
        capture.assert_not_awaited()
        assert len(entries)==1 and entries[0].response_status_code==201
        assert entries[0].request_uri=='/api/v1/chat/completions'
        assert entries[0].request_object is None and entries[0].response_object is None
        assert SECRET not in repr(entries)
    asyncio.run(run())


def test_cache_requires_registered_file_owner(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui.utils.private_cache import private_cache_response
        from open_webui.models.files import File
        async with harness(modules,monkeypatch,tmp_path) as h:
            root=tmp_path/'cache';root.mkdir();p=root/'receipt.txt';p.write_text(SECRET)
            unknown=root/'legacy.json';unknown.write_text(SECRET)
            async with h.sessions() as db:
                db.add(File(id='cached',user_id='alice',filename='receipt.txt',path=str(p.resolve()),data={},meta={},created_at=1,updated_at=1));await db.commit()
            for name,role in [('bob','user'),('admin','admin')]:
                with pytest.raises(HTTPException) as error:
                    await private_cache_response(root,'receipt.txt',SimpleNamespace(id=name,role=role))
                assert error.value.status_code==404
            owner=SimpleNamespace(id='alice',role='user')
            r=await private_cache_response(root,'receipt.txt',owner)
            assert r.headers['cache-control']=='private, no-store'
            assert str(r.path)==str(p.resolve())
            for path in ['legacy.json','../there-api.sqlite','/etc/passwd','missing.txt']:
                with pytest.raises(HTTPException):await private_cache_response(root,path,owner)
            try:(root/'alias.txt').symlink_to(p)
            except OSError:pass  # Windows hosts without symlink privilege; Linux lane exercises this.
            else:
                with pytest.raises(HTTPException):await private_cache_response(root,'alias.txt',owner)
    asyncio.run(run())


@pytest.mark.parametrize('address',['127.0.0.1','10.1.2.3','172.20.1.1','192.168.1.1','169.254.169.254','100.64.0.1','::1','fe80::1','fc00::1','::ffff:127.0.0.1'])
def test_public_fetch_rejects_private_connection_addresses(modules,monkeypatch,address):
    from open_webui.retrieval.web import utils
    monkeypatch.setattr(utils,'ENABLE_LOCAL_WEB_FETCH',False)
    with pytest.raises(ValueError):utils._assert_addresses_allowed([address])


def test_public_fetch_allows_public_destination(modules,monkeypatch):
    from open_webui.retrieval.web import utils
    monkeypatch.setattr(utils,'ENABLE_LOCAL_WEB_FETCH',False)
    monkeypatch.setattr(utils,'WEB_FETCH_FILTER_LIST',[])
    utils._assert_addresses_allowed(['1.1.1.1','2606:4700:4700::1111'])


def test_bulk_reset_cannot_bypass_private_file_policy(modules,monkeypatch):
    async def run():
        from open_webui.routers import retrieval
        reset=AsyncMock();monkeypatch.setattr(retrieval.ASYNC_VECTOR_DB_CLIENT,'reset',reset)
        owner=SimpleNamespace(id='admin',role='admin')
        for handler in [retrieval.reset_vector_db,retrieval.reset_upload_dir]:
            with pytest.raises(HTTPException) as error:await handler(None,user=owner)
            assert error.value.status_code==403
        reset.assert_not_awaited()
    asyncio.run(run())


def test_batch_processing_uses_trusted_content_and_rejects_other_admin(modules,monkeypatch,tmp_path):
    async def run():
        from open_webui.routers import retrieval
        from open_webui.models.files import File,Files
        async with harness(modules,monkeypatch,tmp_path) as h:
            async with h.sessions() as db:
                db.add(File(id='f',user_id='alice',filename='trusted.txt',path=None,data={'content':SECRET},meta={},created_at=1,updated_at=1));await db.commit()
            file=await Files.get_file_by_id('f')
            forged=file.model_copy(update={'user_id':'admin','filename':'forged','data':{'content':'FORGED'}})
            validate=AsyncMock();monkeypatch.setattr(retrieval,'_validate_collection_access',validate)
            monkeypatch.setattr(retrieval,'get_retrieval_config',AsyncMock(return_value=SimpleNamespace()))
            process=AsyncMock(return_value=True);monkeypatch.setattr(retrieval,'run_in_threadpool',process)
            monkeypatch.setattr(retrieval,'publish_event',AsyncMock())
            form=retrieval.BatchProcessFilesForm(files=[forged],collection_name='owned-kb')
            result=await retrieval.process_files_batch(None,form,user=SimpleNamespace(id='admin',role='admin'))
            assert result.errors and not result.results
            process.assert_not_awaited()
            with pytest.raises(HTTPException):
                await retrieval.process_files_batch(None,form.model_copy(update={'collection_name':''}),user=SimpleNamespace(id='alice',role='user'))
            result=await retrieval.process_files_batch(None,form,user=SimpleNamespace(id='alice',role='user'))
            assert result.results[0].status=='completed'
            docs=process.call_args.args[2]
            assert docs[0].page_content==SECRET and docs[0].metadata['created_by']=='alice'
            assert docs[0].metadata['name']=='trusted.txt'
            assert (await Files.get_file_by_id('f')).data['content']==SECRET
    asyncio.run(run())
