import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

@pytest.fixture
def endpoint(monkeypatch):
    base=Path(__file__).parents[1]/'backend/open_webui'
    pkg=ModuleType('open_webui');pkg.__path__=[str(base)]
    studio=SimpleNamespace(ENABLED=True, APP='there', check=AsyncMock(return_value={'role':'admin'}))
    pkg.studio_identity=studio
    monkeypatch.setitem(sys.modules,'open_webui',pkg)
    monkeypatch.setitem(sys.modules,'open_webui.studio_identity',studio)
    spec=importlib.util.spec_from_file_location('private_handoff_test',base/'routers/share_handoff.py')
    h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
    async def auth(request: Request):
        await studio.check(h.browser_token(request))
        return SimpleNamespace(role='admin')
    app=FastAPI();app.include_router(h.create_router(auth))
    monkeypatch.setenv('THERE_SHARE_HANDOFF','1')
    headers={'Origin':'https://buildstudio-there.com','Authorization':'Bearer bs1_fixture',
             'Content-Type':'application/pdf','X-File-Name':'report.pdf','Idempotency-Key':'private-stable-fixture'}
    with TestClient(app) as client:yield h,studio,client,headers

@pytest.mark.parametrize('field,value,status',[
 ('Origin','https://evil.example',403),('Origin','null',403),('Authorization','',401),
 ('Authorization','Bearer native-token',401),('Content-Type','text/plain',415),
 ('Content-Encoding','gzip',415),('X-File-Name','%FF',422)])
def test_untrusted_requests_never_transfer(endpoint,field,value,status):
    h,studio,c,headers=endpoint
    with patch.object(h,'transfer') as send:
        response=c.post('/api/v1/integrations/share/files',headers={**headers,field:value},content=b'%PDF-fixture')
        assert response.status_code==status;send.assert_not_called()

def test_ambient_cookie_cannot_authorize(endpoint):
    h,studio,c,headers=endpoint
    c.cookies.set('token','bs1_fixture')
    assert c.get('/api/v1/integrations/share/status').status_code==401

def test_disable_and_wrong_app(endpoint,monkeypatch):
    h,studio,c,headers=endpoint
    for flag,app in [('0','there'),('1','monitor')]:
        monkeypatch.setenv('THERE_SHARE_HANDOFF',flag);studio.APP=app
        assert c.get('/api/v1/integrations/share/status',headers=headers).json()=={'enabled':False}
        assert c.post('/api/v1/integrations/share/files',headers=headers,content=b'%PDF-test').status_code==503

def test_invalid_size_or_magic_never_transfer(endpoint,monkeypatch):
    h,studio,c,headers=endpoint
    with patch.object(h,'transfer') as send:
        assert c.post('/api/v1/integrations/share/files',headers=headers,content=b'bad').status_code==422
        monkeypatch.setattr(h,'MAX_FILE',4)
        assert c.post('/api/v1/integrations/share/files',headers=headers,content=b'%PDF-test').status_code==413
        send.assert_not_called()

def test_revocation_after_read(endpoint):
    h,studio,c,headers=endpoint
    studio.check.side_effect=[{},HTTPException(401,'revoked')]
    with patch.object(h,'transfer') as send:
        assert c.post('/api/v1/integrations/share/files',headers=headers,content=b'%PDF-test').status_code==401
        assert studio.check.call_args.kwargs=={'fresh':True};send.assert_not_called()

def test_stable_retry_and_fresh_check(endpoint):
    h,studio,c,headers=endpoint;receipt={'file_id':'a','name':'report.pdf','url':'https://buildstudio-share.com'}
    with patch.object(h,'transfer',return_value=receipt) as send:
        for _ in range(2):assert c.post('/api/v1/integrations/share/files',headers=headers,content=b'%PDF-test').json()==receipt
        assert send.call_args_list[0]==send.call_args_list[1]
        assert send.call_args.args==('bs1_fixture','private-stable-fixture','report.pdf',b'%PDF-test')
    assert sum(x.kwargs.get('fresh') is True for x in studio.check.call_args_list)==2

@pytest.mark.parametrize('error,status',[('HandoffError',409),('OutcomeUnconfirmed',503)])
def test_error_sanitization_and_release(endpoint,error,status):
    h,*_=endpoint
    with patch.object(h,'handoff_pdf',side_effect=getattr(h,error)('private credential')):
        with pytest.raises(HTTPException) as exc:h.transfer('token','key','name',b'%PDF-test')
        assert exc.value.status_code==status and 'private credential' not in exc.value.detail
    assert h.workers.acquire(blocking=False);h.workers.release()

def test_capacity_does_not_release_other_worker(endpoint):
    h,*_=endpoint;h.workers.acquire();h.workers.acquire()
    try:
        with patch.object(h,'handoff_pdf') as send:
            with pytest.raises(HTTPException) as exc:h.transfer('token','key','name',b'%PDF-test')
            assert exc.value.status_code==429;send.assert_not_called()
        assert not h.workers.acquire(blocking=False)
    finally:h.workers.release();h.workers.release()

def test_route_is_registered_before_spa():
    s=(Path(__file__).parents[1]/'backend/open_webui/main.py').read_text('utf-8')
    assert s.index('app.include_router(private_share_handoff_router)')<s.index("if os.path.exists(FRONTEND_BUILD_DIR):")

@pytest.mark.parametrize('role,permission,allowed',[('admin',False,True),('user',True,True),('user',False,False),('pending',True,False)])
def test_native_export_permissions(endpoint,monkeypatch,role,permission,allowed):
    import asyncio
    from starlette.requests import Request
    h,*_=endpoint
    fake_config=SimpleNamespace(Config=SimpleNamespace(get=AsyncMock(return_value={})))
    monkeypatch.setitem(sys.modules,'open_webui.models.config',fake_config)
    monkeypatch.setitem(sys.modules,'open_webui.utils.access_control',SimpleNamespace(get_permissions=AsyncMock(return_value={'chat':{'export':permission}})))
    monkeypatch.setitem(sys.modules,'open_webui.there_studio',SimpleNamespace(user_by_token=AsyncMock(return_value=SimpleNamespace(id='fixture',role=role))))
    req=Request({'type':'http','headers':[(b'authorization',b'Bearer bs1_fixture')]})
    if allowed:assert asyncio.run(h.require_export_user(req)).role==role
    else:
        with pytest.raises(HTTPException) as exc:asyncio.run(h.require_export_user(req))
        assert exc.value.status_code==403
