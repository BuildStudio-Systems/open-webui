import test from 'node:test';
import assert from 'node:assert/strict';
import {prepareShareChat} from '../src/lib/utils/prepare-share-chat.mjs';
const fid='00000000-0000-4000-8000-000000000001',cid='00000000-0000-4000-8000-000000000002';
function fixture(overrides={}) {
 const calls=[],drafts=new Map();let valid=true;
 const opts={file:new File(['%PDF-fixture'],'Private report.pdf',{type:'application/pdf'}),token:'fixture',userId:'owner',current:()=>valid,storage:{setItem:(k,v)=>drafts.set(k,v)},prompt:'Summarize this report.',modelIds:['There-Agent 3.8'],request:async(path,o)=>{
  calls.push({path,...o});if(overrides[path])return overrides[path](o,()=>{valid=false;});
  if(path==='/api/v1/auths/')return {id:'owner',role:'user',permissions:{chat:{file_upload:true}}};
  if(path.startsWith('/api/v1/files/?'))return {id:fid,meta:{collection_name:'file-'+fid}};
  if(path.endsWith('/process/status'))return {status:'completed'};
  if(path==='/api/v1/chats/new')return {id:cid};
  if(o.method==='DELETE')return {status:true};throw Error(path);
 }};
 return {opts,calls,drafts};
}
test('imports to a separate private chat with draft prompt, never calls completions or public sharing',async()=>{
 const x=fixture();assert.deepEqual(await prepareShareChat(x.opts),{fileId:fid,chatId:cid});
 assert.equal(x.calls.filter(c=>c.path==='/api/v1/auths/').length,2);
 const chat=JSON.parse(x.calls.find(c=>c.path==='/api/v1/chats/new').body);assert.equal(chat.folder_id,null);assert.deepEqual(chat.chat.messages,[]);assert.equal(chat.chat.files[0].id,fid);
 assert.deepEqual([...x.drafts.keys()],['chat-input-'+cid]);const draft=JSON.parse(x.drafts.get('chat-input-'+cid));assert.equal(draft.files[0].id,fid);assert.equal(draft.webSearchEnabled,false);
 assert.equal(x.calls.some(c=>/completions|\/share|tools/.test(c.path)),false);
});
test('a different signed-in user is rejected before upload',async()=>{const x=fixture({'/api/v1/auths/':()=>({id:'other',role:'admin'})});await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.length,1);});
test('file permission denial is honored',async()=>{const x=fixture({'/api/v1/auths/':()=>({id:'owner',role:'user',permissions:{chat:{file_upload:false}}})});await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.length,1);});
test('cancel during upload deletes only the newly created file and creates no chat',async()=>{const x=fixture({'/api/v1/files/?process=true':(_,cancel)=>{cancel();return {id:fid};}});await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.at(-1).path,'/api/v1/files/'+fid);assert.equal(x.calls.at(-1).method,'DELETE');assert.equal(x.calls.some(c=>c.path==='/api/v1/chats/new'),false);});
test('processing failure cleans up the new file and leaves previous drafts untouched',async()=>{const x=fixture({['/api/v1/files/'+fid+'/process/status']:()=>({status:'failed'})});x.drafts.set('chat-input-existing','preserved');await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.at(-1).method,'DELETE');assert.deepEqual([...x.drafts],[['chat-input-existing','preserved']]);});
test('identity is checked again after processing',async()=>{let count=0;const x=fixture({'/api/v1/auths/':()=>({id:++count===1?'owner':'other',role:'admin'})});await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.at(-1).method,'DELETE');});
test('unknown chat commit keeps its private file and does not blindly retry',async()=>{const x=fixture({'/api/v1/chats/new':()=>{throw Error('lost response');}});await assert.rejects(prepareShareChat(x.opts));assert.equal(x.calls.filter(c=>c.path==='/api/v1/chats/new').length,1);assert.equal(x.calls.some(c=>c.method==='DELETE'),false);assert.equal(x.drafts.size,0);});
