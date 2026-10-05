import test from 'node:test';
import assert from 'node:assert/strict';
import {PrivateShareTransfer} from '../src/lib/utils/private-share-transfer.ts';
const pdf=()=>new Blob(['%PDF-synthetic'],{type:'application/pdf'});
function ok(init){return Response.json({file_id:'fixture',name:decodeURIComponent(init.headers['X-File-Name']),url:'https://buildstudio-share.com'});}
test('lost response retries identical bytes and key without regenerating; success blocks extra sends',async()=>{
 let exports=0;const posts=[];const state=new PrivateShareTransfer();
 const generate=async()=>{exports++;return pdf();};
 const request=async(url,init)=>{posts.push(init);assert.equal(url,'/api/v1/integrations/share/files');if(posts.length===1)throw Error('network');return ok(init);};
 await assert.rejects(state.send('bs1_fixture',generate,request));
 await state.send('bs1_fixture',generate,request);await state.send('bs1_fixture',generate,request);
 assert.equal(exports,1);assert.equal(posts.length,2);assert.equal(posts[0].body,posts[1].body);assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);assert(state.saved);
 assert.equal(posts[0].redirect,'error');assert.equal(posts[0].cache,'no-store');
});
test('navigation during PDF generation prevents transmission',async()=>{
 let resolve;const pending=new Promise(r=>resolve=r);const state=new PrivateShareTransfer();let calls=0;
 const sending=state.send('bs1_fixture',()=>pending,async()=>{calls++;return Response.json({});});
 state.dispose();resolve(pdf());await sending;assert.equal(calls,0);assert.equal(state.prepared,false);
});
test('parallel clicks do not start another export',async()=>{
 let resolve;const pending=new Promise(r=>resolve=r);let exports=0,calls=0;const state=new PrivateShareTransfer();
 const generate=()=>{exports++;return pending;};const request=async(_,i)=>{calls++;return ok(i);};
 const a=state.send('bs1_fixture',generate,request);await state.send('bs1_fixture',generate,request);resolve(pdf());await a;assert.equal(exports,1);assert.equal(calls,1);
});
test('malformed and oversized exports never send',async()=>{
 for(const p of [new Blob(['not pdf']),new Blob(['%PDF-',new Uint8Array(32*1024*1024)])]) {
  const state=new PrivateShareTransfer();let calls=0;await assert.rejects(state.send('bs1_fixture',async()=>p,async()=>{calls++;return Response.json({});}));assert.equal(calls,0);
 }
});
test('untrusted receipt cannot mark saved or replace pending file',async()=>{
 const state=new PrivateShareTransfer();await assert.rejects(state.send('bs1_fixture',async()=>pdf(),async(_,i)=>Response.json({...JSON.parse(await ok(i).text()),url:'https://evil.example'})));assert.equal(state.saved,false);assert.equal(state.prepared,true);
});
test('native token rejected before exporting',async()=>{
 const state=new PrivateShareTransfer();let count=0;await assert.rejects(state.send('local',async()=>{count++;return pdf();}));assert.equal(count,0);
});
