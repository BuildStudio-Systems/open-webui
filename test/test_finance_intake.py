"""Synthetic authority/HTTP/DB tests. No application boot, credentials or live data."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
import httpx

ROOT=Path(__file__).parents[2]
sys.path.insert(0,str(ROOT/'frontend/backend'))
sys.path.insert(0,str(ROOT/'agent'))
from open_webui.utils.finance_intake_store import IntakeStore,IntakeError,canonical,request_key,metadata
from open_webui.utils.finance_intake import (ContextVault,FinanceClient,FinanceIntake,normalize_items,
                                            CREATE,CONTEXT,FINANCE,reserve_message,context_header)
from gateway.there_chat_scope import parse_there_chat_scope,CHAT_HEADER,OWNER_HEADER,BUSINESS_HEADER
from gateway.there_business_context import business_context_scope,current_business_context,BusinessEvidence

OWNER='11111111-1111-4111-8111-111111111111'
MESSAGE='22222222-2222-4222-8222-222222222222'
CHAT='33333333-3333-4333-8333-333333333333'
BINDING='44444444-4444-4444-8444-444444444444'
SESSION='bs1_'+'s'*43
TEXT='今天午餐1200日元，电车460日元'
def items():
    return [{'source_quote':'午餐1200日元','title':'午餐','gross_amount':'1200'},
            {'source_quote':'电车460日元','title':'电车','gross_amount':'460'}]
def context():
    return {'status':'ready','binding_id':BINDING,'binding_revision':1,'company_id':7,'company_name':'Synthetic Company',
            'default_currency':'JPY','timezone':'Asia/Tokyo','required_fields':[]}


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch=tempfile.TemporaryDirectory()
        pg=os.environ.get('THERE_FINANCE_TEST_PG_URL')
        if pg and ('there_expense_synthetic' not in pg or 'host=%2Ftmp%2Fthere-expense-test-' not in pg):
            raise RuntimeError('Test PG must be an explicitly isolated synthetic socket cluster')
        self.engine=create_async_engine(pg or 'sqlite+aiosqlite:///'+str(Path(self.scratch.name)/'test.db'))
        async with self.engine.begin() as c: await c.run_sync(metadata.create_all)
        self.factory=async_sessionmaker(self.engine)
        self.store=IntakeStore(self.factory)
        self.row=await self.store.reserve(OWNER,MESSAGE,TEXT)
        self.vault=ContextVault()
        self.nonce=self.vault.issue(OWNER,self.row['id'],SESSION,int(time.time())+1000,TEXT)
        self.calls=[]; self.grants=[]; self.claims={}; self.denied=False; self.fail_after_commit=False
        async def central(action,body):
            self.calls.append((action,body))
            if self.denied: raise IntakeError('source_identity_denied',403)
            if action=='check': return {'external_id':OWNER,'role':'admin','expires_at':int(time.time())+900}
            self.grants.append(body)
            return {'token':'bsc1_'+'g'*43}
        async def finance(request):
            self.assertEqual(request.headers['X-Studio-Delegation'],'bsc1_'+'g'*43)
            self.assertNotIn('cookie',request.headers)
            body=json.loads(request.content)
            if request.url.path.endswith('/context'): return httpx.Response(200,json=context())
            key=body['request_key']; duplicate=key in self.claims
            if request.url.path.endswith('/receipt') and not duplicate:
                return httpx.Response(409,json={'code':'NOT_RECORDED'})
            if not duplicate: self.claims[key]=[{'id':100+n,'status':'DRAFT','line_id':n,
                **{k:item[k] for k in ('title','expense_date','currency','gross_amount')}} for n,item in enumerate(body['items'],1)]
            if self.fail_after_commit:
                self.fail_after_commit=False
                raise httpx.ReadError('synthetic response lost',request=request)
            return httpx.Response(200,json={'status':'completed','intake_id':body['intake_id'],
                'request_key':key,'duplicate':duplicate,'company_id':7,'company_name':'Synthetic Company','claims':self.claims[key]})
        self.client=FinanceClient(central,httpx.MockTransport(finance))
        self.valid_chat=AsyncMock(return_value=True)
        self.app=FinanceIntake(self.store,self.client,self.vault,self.valid_chat)

    async def asyncTearDown(self):
        async with self.engine.begin() as c: await c.run_sync(metadata.drop_all)
        await self.engine.dispose(); self.scratch.cleanup()

    async def test_two_drafts_and_exact_idempotent_retry(self):
        a=await self.app.invoke(self.nonce,{'action':'create','items':items()})
        b=await self.app.invoke(self.nonce,{'action':'create','items':items()})
        self.assertEqual(len(self.claims),1); self.assertEqual(a['claims'],b['claims'])
        self.assertFalse(a['duplicate']); self.assertTrue(b['duplicate'])
        self.assertEqual([c['gross_amount'] for c in a['claims']],['1200','460'])
        self.assertEqual(a['company_id'],7)
        stored=await self.store.get(self.row['id'],OWNER)
        self.assertNotIn(SESSION,json.dumps(stored)); self.assertNotIn(TEXT,json.dumps(stored))
        payload=json.loads(stored['payload_json'])
        self.assertTrue(all(x['tax'] is None for x in payload['items']))
        self.assertNotIn('source_quote',payload['items'][0])
        self.assertEqual(self.grants[-1]['scope']['payload_sha256'],hashlib.sha256(canonical(payload)).hexdigest())

    async def test_first_new_chat_lost_response_reserves_original_chat(self):
        retry=await self.store.reserve(OWNER,MESSAGE,TEXT)
        self.assertEqual(retry['id'],self.row['id']); self.assertEqual(retry['chat_id'],self.row['chat_id'])
        self.assertEqual(request_key(retry),request_key(self.row))

    async def test_same_message_edit_and_foreign_chat_conflict(self):
        for content,chat in [(TEXT+' edited',None),(TEXT,CHAT)]:
            with self.assertRaisesRegex(IntakeError,'immutable_message_conflict'):
                await self.store.reserve(OWNER,MESSAGE,content,chat)

    async def test_changed_batch_conflicts_before_second_finance_write(self):
        await self.app.invoke(self.nonce,{'action':'create','items':items()})
        altered=items()[:1]
        with self.assertRaisesRegex(IntakeError,'immutable_batch_conflict'):
            await self.app.invoke(self.nonce,{'action':'create','items':altered})
        self.assertEqual(len(self.claims),1)

    async def test_actual_lost_commit_response_recovers_with_read_only_receipt(self):
        self.fail_after_commit=True
        with self.assertRaisesRegex(IntakeError,'finance_result_unconfirmed'):
            await self.app.invoke(self.nonce,{'action':'create','items':items()})
        receipt=await self.app.invoke(self.nonce,{'action':'receipt'})
        self.assertEqual(len(receipt['claims']),2); self.assertEqual(len(self.claims),1)
        self.assertTrue(receipt['duplicate'])

    async def test_restart_discards_nonce_but_retains_request_identity(self):
        await self.app.invoke(self.nonce,{'action':'create','items':items()})
        restarted=ContextVault()
        with self.assertRaises(IntakeError): restarted.get(self.nonce)
        nonce=restarted.issue(OWNER,self.row['id'],SESSION,int(time.time())+300,TEXT)
        app=FinanceIntake(IntakeStore(self.factory),self.client,restarted,self.valid_chat)
        result=await app.invoke(nonce,{'action':'receipt'})
        self.assertEqual(len(result['claims']),2); self.assertEqual(len(self.claims),1)

    async def test_missing_amount_zero_writes_and_no_payload_frozen(self):
        value=items(); del value[0]['gross_amount']
        result=await self.app.invoke(self.nonce,{'action':'create','items':value})
        self.assertEqual(result['status'],'needs_input'); self.assertEqual(self.claims,{})
        self.assertFalse((await self.store.get(self.row['id'],OWNER))['payload_sha256'])

    async def test_owner_scope_cannot_be_model_overwritten(self):
        for key in ('owner','company_id','session','request_key','target','grant','intake_id'):
            with self.assertRaises(IntakeError): await self.app.invoke(self.nonce,{'action':'create','items':items(),key:OWNER})
        self.assertFalse(self.claims)

    async def test_legacy_session_bounds_always_require_fresh_owner_identity(self):
        for token in ('bs1_'+'a'*16,'bs1_'+'z'*196):
            self.assertEqual((await self.client.fresh_source(token,OWNER))['external_id'],OWNER)
            self.assertEqual(self.calls[-1],('check',{'token':token}))
        before=len(self.calls)
        for token in ('bs1_'+'a'*15,'bs1_'+'a'*197,'bs1_'+'a '*10):
            with self.assertRaises(IntakeError): await self.client.fresh_source(token,OWNER)
        self.assertEqual(len(self.calls),before)
        self.denied=True
        with self.assertRaises(IntakeError): await self.client.fresh_source('bs1_'+'a'*16,OWNER)

    async def test_parent_revoke_and_message_edit_fail_closed(self):
        self.denied=True
        with self.assertRaises(IntakeError): await self.app.invoke(self.nonce,{'action':'create','items':items()})
        self.denied=False; self.valid_chat.return_value=False
        with self.assertRaises(IntakeError): await self.app.invoke(self.nonce,{'action':'create','items':items()})
        self.assertFalse(self.claims)

    async def test_revoke_during_payload_lock_wait_checked_again(self):
        original=self.store.bind_payload
        async def blocked(*args):
            result=await original(*args); self.denied=True; return result
        self.store.bind_payload=blocked
        with self.assertRaises(IntakeError): await self.app.invoke(self.nonce,{'action':'create','items':items()})
        self.assertFalse(self.claims)

    async def test_native_receipt_subject_and_ids_verified(self):
        async def wrong(request):
            if request.url.path.endswith('/context'): return httpx.Response(200,json=context())
            return httpx.Response(200,json={'status':'completed','intake_id':MESSAGE,'request_key':'wrong','claims':[]})
        self.client.transport=httpx.MockTransport(wrong)
        with self.assertRaisesRegex(IntakeError,'finance_receipt_invalid'):
            await self.app.invoke(self.nonce,{'action':'create','items':items()})

    async def test_private_actual_route_uses_nonce_not_cookies_or_public_host(self):
        from fastapi import FastAPI
        from open_webui.routers import finance_intake as route
        app=FastAPI();app.include_router(route.router,prefix='/api/v1/finance-intake')
        with patch.object(route,'native_invoke',side_effect=self.app.invoke),patch.object(route,'vault',self.vault):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',50000)),base_url='http://127.0.0.1:3000') as client:
                self.assertEqual((await client.post('/api/v1/finance-intake/tool',cookies={'token':SESSION},json={'action':'create','items':items()})).status_code,403)
                self.assertEqual((await client.post('/api/v1/finance-intake/tool',headers={'Host':'buildstudio-there.com','Authorization':'Bearer '+self.nonce},json={'action':'context'})).status_code,403)
                result=await client.post('/api/v1/finance-intake/tool',headers={'Authorization':'Bearer '+self.nonce},json={'action':'create','items':items()})
                self.assertEqual(result.status_code,200);self.assertEqual(len(result.json()['claims']),2)
                self.assertEqual(result.headers['cache-control'],'no-store')

    async def test_private_route_rejects_unknown_nonce_before_body_and_slow_body_bounded(self):
        from starlette.requests import Request
        from open_webui.routers import finance_intake as route
        scope={'type':'http','method':'POST','path':'/tool','client':('127.0.0.1',123),
            'headers':[(b'host',b'127.0.0.1:3000'),(b'authorization',b'Bearer '+self.nonce.encode()),(b'content-length',b'20')]}
        calls=[]
        async def slow():
            calls.append('read');await asyncio.sleep(.1)
            return {'type':'http.request','body':b'{}','more_body':False}
        with patch.object(route,'vault',self.vault),patch.object(route,'PRIVATE_DEADLINE',.02):
            bad=dict(scope,headers=[(k,b'Bearer '+b'z'*43) if k==b'authorization' else (k,v) for k,v in scope['headers']])
            self.assertEqual((await route.tool(Request(bad,slow))).status_code,403)
            self.assertEqual(calls,[])
            result=await route.tool(Request(scope,slow))
            self.assertEqual(result.status_code,503)
            self.assertIn(b'finance_result_unconfirmed',result.body)

    async def test_private_route_strict_json_rejects_duplicates_and_nonfinite(self):
        from fastapi import FastAPI
        from open_webui.routers import finance_intake as route
        app=FastAPI();app.include_router(route.router,prefix='/api/v1/finance-intake')
        with patch.object(route,'vault',self.vault),patch.object(route,'native_invoke',side_effect=self.app.invoke) as invoke:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://127.0.0.1:3000') as client:
                for raw in ('{"action":"context","action":"create"}','{"action":"create","items":NaN}'):
                    result=await client.post('/api/v1/finance-intake/tool',content=raw,headers={'Authorization':'Bearer '+self.nonce})
                    self.assertEqual(result.status_code,422)
                invoke.assert_not_called()

    async def test_finance_trickle_response_has_absolute_deadline_and_closes(self):
        from open_webui.utils import finance_intake as intake
        class Trickle(httpx.AsyncByteStream):
            closed=False
            async def __aiter__(stream):
                for _ in range(20):
                    await asyncio.sleep(.01);yield b' '
            async def aclose(stream): stream.closed=True
        stream=Trickle()
        self.client.transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=stream))
        with patch.object(intake,'FINANCE_DEADLINE',.035):
            with self.assertRaisesRegex(IntakeError,'finance_result_unconfirmed'):
                await self.client.call(SESSION,CONTEXT,{},'/context',{})
        self.assertTrue(stream.closed);self.assertFalse(self.claims)

    async def test_native_source_authority_is_real_session_current_message_not_api_key_task_or_cached_login(self):
        from open_webui.utils import finance_intake as intake
        user=SimpleNamespace(id=OWNER,role='admin')
        request=SimpleNamespace(headers={},cookies={'token':SESSION})
        identity=AsyncMock(return_value={'external_id':OWNER,'role':'admin','expires_at':int(time.time())+900})
        meta={'chat_id':self.row['chat_id'],'user_message':{'id':MESSAGE,'role':'user','content':TEXT}}
        with patch.object(intake,'settings',AsyncMock(return_value={OWNER})),patch.object(intake,'native_store',return_value=self.store),patch.object(intake,'native_identity',identity),patch.object(intake,'validate_native_chat',self.valid_chat):
            row=await reserve_message(request,user,meta,True)
            self.assertEqual(row['id'],self.row['id'])
            nonce=await context_header(user,meta,SESSION)
            self.assertEqual(intake.vault.get(nonce).intake_id,row['id'])
            self.assertTrue(all(call.args[0]=='check' for call in identity.call_args_list))
            count=identity.call_count
            self.assertIsNone(await reserve_message(request,user,meta|{'user_message':{'id':MESSAGE,'role':'user','content':'Hello, explain database indexes'}},True))
            self.assertEqual(identity.call_count,count)
            for key,value in (('internal',True),('automation_id','task'),('task','title_generation')):
                self.assertIsNone(await reserve_message(request,user,meta|{key:value},True))
            self.assertIsNone(await reserve_message(SimpleNamespace(headers={'authorization':'Bearer sk-machine'},cookies={'token':SESSION}),user,meta,True))
            self.assertIsNone(await reserve_message(request,SimpleNamespace(id=OWNER,role='user'),meta,True))
            self.assertEqual(await context_header(user,meta|{'user_message':{'id':MESSAGE,'role':'user','content':'history edited'}},SESSION),'')
            self.valid_chat.return_value=False
            self.assertEqual(await context_header(user,meta,SESSION),'')

    async def test_native_central_transport_exact_host_no_redirect_and_cancellable_deadline(self):
        from open_webui.utils import finance_intake as intake
        original=httpx.AsyncClient
        seen=[]
        async def central(request):
            seen.append(request)
            self.assertEqual(str(request.url),intake.IDENTITY+'check')
            self.assertEqual(request.headers['X-Studio-Client'],'there')
            self.assertEqual(request.headers['X-Studio-Key'],'synthetic-key')
            return httpx.Response(303,headers={'location':'https://unexpected.invalid'})
        with patch.dict(os.environ,{'STUDIO_IDENTITY_APP':'there','STUDIO_IDENTITY_KEY':'synthetic-key'}),patch.object(intake.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(central),**kw)):
            with self.assertRaisesRegex(IntakeError,'source_identity_unavailable'): await intake.native_identity('check',{'token':SESSION})
        self.assertEqual(len(seen),1)
        for status,accepted in ((201,True),(200,False),(202,False)):
            with patch.dict(os.environ,{'STUDIO_IDENTITY_APP':'there','STUDIO_IDENTITY_KEY':'synthetic-key'}),patch.object(intake.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(status,json={'token':'bsc1_'+'g'*43})),**kw)):
                if accepted: self.assertIn('token',await intake.native_identity('coordination/finance/expense-delegate',{}))
                else:
                    with self.assertRaises(IntakeError): await intake.native_identity('coordination/finance/expense-delegate',{})
        async def blocked(request): await asyncio.sleep(.2);return httpx.Response(200,json={})
        with patch.dict(os.environ,{'STUDIO_IDENTITY_APP':'there','STUDIO_IDENTITY_KEY':'synthetic-key'}),patch.object(intake,'IDENTITY_DEADLINE',.02),patch.object(intake.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(blocked),**kw)):
            with self.assertRaisesRegex(IntakeError,'source_identity_unavailable'): await intake.native_identity('check',{'token':SESSION})

    async def test_native_current_values_and_deleted_records_not_payload_mirrors(self):
        await self.app.invoke(self.nonce,{'action':'create','items':items()})
        claims=next(iter(self.claims.values()))
        claims[0]['gross_amount']='999.00'
        claims[1].update(status='DELETED',title=None,expense_date=None,currency=None,gross_amount=None)
        result=await self.app.invoke(self.nonce,{'action':'receipt'})
        self.assertEqual(result['claims'][0]['gross_amount'],'999.00')
        self.assertEqual(result['claims'][1]['status'],'DELETED');self.assertIsNone(result['claims'][1]['gross_amount'])

    async def test_pg_concurrent_reserve_and_payload_cas(self):
        if self.engine.dialect.name!='postgresql': self.skipTest('Real PG cross-connection lock test')
        rows=await asyncio.gather(*(self.store.reserve(OWNER,MESSAGE,TEXT) for _ in range(12)))
        self.assertEqual(len({x['id'] for x in rows}),1)
        outcomes=await asyncio.gather(*(self.store.bind_payload(self.row['id'],OWNER,{'batch':i%2}) for i in range(12)),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,str) for x in outcomes),6)
        self.assertTrue(all(isinstance(x,(str,IntakeError)) for x in outcomes))

    async def test_actual_additive_migration_table_and_preserved_rollback_receipts(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        path=ROOT/'frontend/backend/open_webui/migrations/versions/e909a0010002_add_finance_message_intake.py'
        spec=importlib.util.spec_from_file_location('expense_migration',path)
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        self.assertEqual(migration.down_revision,'e909a0010001')
        async with self.engine.begin() as db:
            await db.run_sync(metadata.drop_all)
            def upgrade(connection):
                with patch.object(migration,'op',Operations(MigrationContext.configure(connection))): migration.upgrade()
            await db.run_sync(upgrade)
        migrated=await self.store.reserve(OWNER,MESSAGE,TEXT)
        self.assertEqual(migrated['text_sha256'],hashlib.sha256(TEXT.encode()).hexdigest())
        with self.assertRaisesRegex(RuntimeError,'Keep additive intake'): migration.downgrade()
        self.assertEqual((await self.store.get(migrated['id'],OWNER))['id'],migrated['id'])


class ShapeTests(unittest.TestCase):
    def test_canonical_shared_vector(self):
        body={'binding_id':'11111111-1111-4111-8111-111111111111','binding_revision':1,
            'intake_id':'22222222-2222-4222-8222-222222222222','request_key':'there-expense:'+'a'*64,
            'items':[{'line_id':1,'title':'午餐 / Lunch','expense_date':'2026-10-03','currency':'JPY',
                'gross_amount':'1200.00','memo':None,'category':None,'tax':None,'extra':{}}]}
        self.assertEqual(hashlib.sha256(canonical(body)).hexdigest(),'9067cb065eb65ff08d88e0245c2b53ddbacc43f57b9b5bc51c94583e8d390d67')

    def test_invalid_numeric_unicode_and_custom_keys(self):
        for body in ({'x':1.0},{'x':float('nan')},{'é':1},{'x':'\ud800'}):
            with self.assertRaises(IntakeError): canonical(body)

    def test_original_today_is_bound_to_timezone_and_cross_midnight_retry(self):
        epoch=int(datetime(2026,10,2,16,0,tzinfo=timezone.utc).timestamp())
        for _ in range(2):
            result=normalize_items(items(),context(),epoch,TEXT)
            self.assertEqual(result[0]['expense_date'],'2026-10-03')

    def test_source_amount_currency_quotes_and_tax_are_not_guessed(self):
        samples=[({'gross_amount':'1300'},TEXT),({'currency':'CNY'},TEXT),
            ({'source_quote':'from history'},TEXT),({'tax':{'rate':'10'}},TEXT),
            ({'category':'Meals'},TEXT),({'expense_date':'2026-10-01'},TEXT)]
        for change,source in samples:
            values=items();values[0].update(change)
            self.assertEqual(normalize_items(values,context(),int(time.time()),source)['status'],'needs_input')

    def test_ambiguous_date_amount_yuan_and_example_zero_authority(self):
        for source in ('昨天午餐1200日元','午餐1200或1300日元','午餐1200元','例如午餐1200日元'):
            value=[{'source_quote':source,'title':'午餐','gross_amount':'1200'}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')

    def test_extra_required_and_slot_budget(self):
        c=context();c['required_fields']=[{'key':'cost_center','required':True,'type':'TEXT'}]
        self.assertIn('items.1.extra.cost_center',normalize_items(items(),c,int(time.time()),TEXT)['fields'])
        for value in ([],items()*11):
            with self.assertRaises(IntakeError): normalize_items(value,context(),int(time.time()),TEXT)

    def test_dates_times_quantities_and_ambiguous_totals_are_not_money(self):
        for source,amount in [('2026-10-03 买午餐，金额未说','10'),
                ('12:30 买午餐，金额未说','30'),('2个午餐，金额未说','2'),
                ('午餐单价600日元×2份，合计不确定','1200')]:
            value=[{'source_quote':source,'title':'午餐','gross_amount':amount}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')

    def test_counts_identifiers_negation_and_invented_custom_field_zero_authority(self):
        c=context();c['required_fields']=[{'key':'cost_center','required':False,'type':'SELECT','options':['工程部'],'label':'部门'},
            {'key':'approved','required':False,'type':'CHECKBOX','label':'勾选确认'}]
        for source,title,amount,extra in [('办公买了2本笔记本','笔记本','2',{}),('午餐第10号店','午餐','10',{}),
                ('先别登记，午餐1200日元','午餐','1200',{}),('午餐1200日元','午餐','1200',{'cost_center':'工程部'}),
                ('lunch 1200 JPY','lunch','1200',{'approved':True})]:
            value=[{'source_quote':source,'title':title,'gross_amount':amount,'extra':extra}]
            self.assertEqual(normalize_items(value,c,int(time.time()),source)['status'],'needs_input')
        allowed=[{'source_quote':'午餐1200日元;部门=工程部','title':'午餐','gross_amount':'1200','extra':{'cost_center':'工程部'}}]
        self.assertIsInstance(normalize_items(allowed,c,int(time.time()),allowed[0]['source_quote']),list)

    def test_positive_expense_lists_only_and_complete_batch_before_freeze(self):
        for source,title in [('请翻译这句：午餐1200日元','午餐'),('Translate this sentence: lunch1200JPY','lunch'),
                ('预算午餐1200日元','午餐'),('午餐价格1200日元吗','午餐'),('Can you help lunch 1200 JPY','lunch')]:
            value=[{'source_quote':source,'title':title,'gross_amount':'1200'}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')
        self.assertIn('complete_expense_batch',normalize_items(items()[:1],context(),int(time.time()),TEXT)['fields'])
        for source,title in [('今天午餐1200','午餐'),('Today lunch 1200','lunch'),('今日昼食1200','昼食'),('我午餐花了1200日元','午餐'),('I paid lunch 1200 JPY','lunch')]:
            value=[{'source_quote':source,'title':title,'gross_amount':'1200'}]
            self.assertIsInstance(normalize_items(value,context(),int(time.time()),source),list)

    def test_native_custom_types_validated_before_batch_freeze(self):
        c=context();c['required_fields']=[{'key':'check','type':'CHECKBOX','required':True,'label':'确认'}]
        self.assertIn('manual_custom_fields',normalize_items(items(),c,int(time.time()),TEXT)['fields'])
        c['required_fields']=[{'key':'dept','type':'SELECT','required':False,'options':['工程部'],'label':'部门'}]
        value=[{'source_quote':'午餐1200日元;部门=销售部','title':'午餐','gross_amount':'1200','extra':{'dept':'销售部'}}]
        self.assertEqual(normalize_items(value,c,int(time.time()),value[0]['source_quote'])['status'],'needs_input')
        for extra in ({'dept':['工程部']},{'unknown':'工程部'}):
            value[0]['extra']=extra
            with self.assertRaises(IntakeError): normalize_items(value,c,int(time.time()),value[0]['source_quote'])

    def test_model_description_fields_cannot_consume_intent_and_no_plain_chat_authority(self):
        from open_webui.utils.finance_intake import expense_candidate
        for source in ('午餐1200日元贵吗？','朋友支付了午餐1200日元','午餐1200日元大概',"Don't record lunch 1200 JPY",'登録しない昼食1200円'):
            for field in ('title','memo','category'):
                value=[{'source_quote':source,'title':'午餐','gross_amount':'1200',field:source}]
                self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')
            self.assertFalse(expense_candidate(source))
        for plain in ('Hello','帮我解释数据库索引','Translate lunch1200JPY','What is JPY 1200?'):
            self.assertFalse(expense_candidate(plain))
        for plain in ('房间号1200','版本42','Room 1200','version42'):
            self.assertFalse(expense_candidate(plain))
        for source in (TEXT,'Today lunch 1200','今日昼食1200'):
            self.assertTrue(expense_candidate(source))
        for source,title in [('登记开销: 版权费1200日元','版权费'),('Record expenses: licensing 1200 JPY','licensing'),
                ('経費登録: ライセンス1200円','ライセンス'),('I paid licensing 1200 JPY','licensing')]:
            self.assertTrue(expense_candidate(source))
            self.assertIsInstance(normalize_items([{'source_quote':source,'title':title,'gross_amount':'1200'}],
                context(),int(time.time()),source),list)
        source='今日昼食1200円、電車460円'
        self.assertIsInstance(normalize_items([
            {'source_quote':'今日昼食1200円','title':'昼食','gross_amount':'1200'},
            {'source_quote':'電車460円','title':'電車','gross_amount':'460'}],context(),int(time.time()),source),list)

    def test_future_unpaid_and_unsupported_relative_dates_are_not_occurred_expenses(self):
        from open_webui.utils.finance_intake import direct_expense_message,expense_candidate
        for source in ('I will pay lunch 1200 JPY','I plan lunch 1200 JPY','I have not paid lunch 1200 JPY',
                '明後日の昼食1200円','来週の昼食1200円','下周午餐1200日元','上个月午餐1200日元'):
            self.assertIsNone(direct_expense_message(source));self.assertFalse(expense_candidate(source))
            value=[{'source_quote':source,'title':source,'memo':source,'gross_amount':'1200'}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')
        self.assertIsNotNone(direct_expense_message(TEXT))
        source='2026-10-20午餐1200日元'
        self.assertEqual(normalize_items([{'source_quote':source,'title':'午餐','gross_amount':'1200'}],context(),
            int(datetime(2026,10,3,tzinfo=timezone.utc).timestamp()),source)['status'],'needs_input')

    def test_currency_is_only_explicit_or_configured_default_and_dates_not_overridden(self):
        source='咖啡300';value=[{'source_quote':source,'title':'咖啡','gross_amount':'300','currency':'USD'}]
        self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')
        value[0]['currency']=None
        self.assertEqual(normalize_items(value,context(),int(time.time()),'USD '+source)[0]['currency'],'USD')
        dated='2026-09-30午餐1200日元';value=[{'source_quote':dated,'title':'午餐','gross_amount':'1200'}]
        self.assertEqual(normalize_items(value,context(),int(time.time()),dated)[0]['expense_date'],'2026-09-30')
        value[0]['expense_date']='2026-10-03'
        self.assertEqual(normalize_items(value,context(),int(time.time()),dated)['status'],'needs_input')
        for prefix in ('前天','上周','先週','last Tuesday '):
            source=prefix+'午餐1200日元';value=[{'source_quote':source,'title':'午餐','gross_amount':'1200'}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')

    def test_negative_refunds_and_multiple_money_amounts_not_new_expenses(self):
        for source in ('-500 JPY退款','500 JPY返金','refund lunch 500 JPY','午餐1200日元，晚餐2000日元'):
            value=[{'source_quote':source,'title':'午餐' if '午餐' in source else '500','gross_amount':'1200' if '1200' in source else '500'}]
            self.assertEqual(normalize_items(value,context(),int(time.time()),source)['status'],'needs_input')

    def test_duplicate_source_span_not_second_expense(self):
        self.assertEqual(normalize_items([items()[0],items()[0]],context(),int(time.time()),TEXT)['status'],'needs_input')

    def test_vault_expiry_budget_no_secret_repr(self):
        clock=[1.0];vault=ContextVault(lambda:clock[0],capacity=1)
        nonce=vault.issue(OWNER,MESSAGE,SESSION,int(time.time())+30,TEXT)
        self.assertNotIn(SESSION,repr(vault.get(nonce)));self.assertNotIn(TEXT,repr(vault.get(nonce)))
        with self.assertRaises(IntakeError): vault.issue(OWNER,MESSAGE,SESSION,int(time.time())+30,TEXT)
        clock[0]=100
        with self.assertRaises(IntakeError): vault.get(nonce)

    def test_agent_scope_header_not_identity_or_history(self):
        headers={CHAT_HEADER:CHAT,OWNER_HEADER:OWNER,BUSINESS_HEADER:'a'*43}
        scope=parse_there_chat_scope(headers,authenticated_key_configured=True)
        self.assertEqual(scope.business_context,'a'*43);self.assertNotIn('a'*43,repr(scope))
        for bad in ({BUSINESS_HEADER:'a'*43},headers|{BUSINESS_HEADER:SESSION}):
            with self.assertRaises(ValueError): parse_there_chat_scope(bad,authenticated_key_configured=True)

    def test_business_context_thread_scope_finally_and_three_language_receipts(self):
        self.assertEqual(current_business_context(),'')
        with business_context_scope('a'*43): self.assertEqual(current_business_context(),'a'*43)
        self.assertEqual(current_business_context(),'')
        for message,phrase in [('lunch1200JPY','complete message'),('今天午餐1200日元','完整消息'),('今日昼食1200円','全文')]:
            evidence=BusinessEvidence();evidence.record({'status':'needs_input','fields':['gross_amount'],'claims':[]})
            result=evidence.apply({'final_response':'I posted everything'},message)['final_response']
            self.assertIn(phrase,result);self.assertNotIn('I posted everything',result)

    def test_business_receipts_only_current_native_values_and_no_routing_codes(self):
        receipt={'status':'completed','company_name':'Native Company','request_key':'there-expense:'+'a'*64,'intake_id':MESSAGE,
            'claims':[{'id':7,'line_id':1,'status':'DRAFT','title':'Lunch','expense_date':'2026-10-03','currency':'JPY','gross_amount':'999.00'},
                      {'id':8,'line_id':2,'status':'DELETED','title':None,'expense_date':None,'currency':None,'gross_amount':None}]}
        for message,draft,deleted in [('lunch1200JPY','Draft','Deleted'),('今天午餐1200日元','未提交草稿','已删除'),('今日昼食1200円','下書き','削除済み')]:
            evidence=BusinessEvidence();evidence.record(receipt)
            rendered=evidence.apply({'final_response':'all paid'},message)['final_response']
            for text in ('Native Company','Lunch','999.00 JPY','2026-10-03','#7',draft,deleted): self.assertIn(text,rendered)
            for hidden in ('request_key','intake_id',MESSAGE,'a'*64,'DRAFT','DELETED','all paid'): self.assertNotIn(hidden,rendered)
        evidence=BusinessEvidence();evidence.record({'status':'needs_input','fields':['items.1.gross_amount','items.1.extra.cost_center'],'claims':[]})
        rendered=evidence.apply({'final_response':''},'lunch1200JPY')['final_response']
        self.assertIn('Amount',rendered);self.assertNotIn('gross_amount',rendered);self.assertNotIn('cost_center',rendered)

    def test_bare_amount_no_tool_receipt_never_claims_recorded_and_greetings_unchanged(self):
        for source,word in [('今天午餐1200','未确认'),('Today lunch 1200','unconfirmed'),('今日昼食1200','未確認')]:
            result=BusinessEvidence().apply({'final_response':'已登记，已支付'},source)['final_response']
            self.assertIn(word,result);self.assertNotIn('已支付',result)
        self.assertEqual(BusinessEvidence().apply({'final_response':'Hello!'},'hello')['final_response'],'Hello!')
        self.assertEqual(BusinessEvidence().apply({'final_response':'Lunch 1200 yen'},'请翻译这句：午餐1200日元')['final_response'],'Lunch 1200 yen')
        evidence=BusinessEvidence();evidence.record({'status':'needs_input','fields':['manual_custom_fields'],'claims':[]})
        self.assertIn('manual entry',evidence.apply({'final_response':''},'lunch1200')['final_response'])


if __name__=='__main__': unittest.main()
