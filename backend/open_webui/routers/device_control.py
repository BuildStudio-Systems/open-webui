from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from open_webui.utils.auth import get_admin_user
from open_webui.utils.device_control import control_request, explicit_session_header, session_from_request

router = APIRouter()


class Approval(BaseModel):
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


@router.get('/devices')
async def devices(request: Request, user=Depends(get_admin_user)):
    return JSONResponse(await control_request(user, {'action': 'list'}, session_token=session_from_request(request)), headers={'Cache-Control': 'no-store'})


@router.get('/jobs')
async def jobs(request: Request, user=Depends(get_admin_user)):
    return JSONResponse(await control_request(user, {'action': 'jobs'}, session_token=session_from_request(request)), headers={'Cache-Control': 'no-store'})


@router.get('/jobs/{job_id}')
async def job(job_id: str, request: Request, user=Depends(get_admin_user)):
    return JSONResponse(await control_request(user, {'action': 'job', 'job': job_id}, session_token=session_from_request(request)), headers={'Cache-Control': 'no-store'})


@router.post('/jobs/{job_id}/approve')
async def approve(job_id: str, body: Approval, request: Request, user=Depends(get_admin_user)):
    # Bearer only: cross-site forms and ambient cookies cannot approve an operation.
    if not explicit_session_header(request.headers.get('authorization')):
        raise HTTPException(403, 'Explicit authenticated approval required.')
    return await control_request(user, {'action': 'approve', 'job': job_id, 'digest': body.digest}, session_token=session_from_request(request), approve=True)


@router.post('/jobs/{job_id}/cancel')
async def cancel(job_id: str, request: Request, user=Depends(get_admin_user)):
    if not explicit_session_header(request.headers.get('authorization')):
        raise HTTPException(403, 'Explicit authenticated action required.')
    return await control_request(user, {'action': 'cancel', 'job': job_id}, session_token=session_from_request(request))


@router.get('/console', response_class=HTMLResponse)
async def console():
    # Public shell only. All inventory, command text and results require the owner API.
    return HTMLResponse(CONSOLE, headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
        'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})


CONSOLE = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>THERE · Devices</title>
<style>html:lang(en) body{font-family:Inter,Arial,sans-serif}html:lang(ja) body{font-family:'Noto Sans JP','Yu Gothic',sans-serif}html:lang(zh-CN) body{font-family:'Noto Sans SC','Microsoft YaHei',sans-serif}body{font:16px system-ui,sans-serif;background:#f7f7f8;color:#202124;margin:0}main{max-width:980px;margin:auto;padding:28px}header{display:flex;align-items:center;justify-content:space-between;gap:12px}select,button{font:inherit;padding:9px 14px;border:1px solid #ddd;border-radius:9px;background:white;cursor:pointer}article{padding:20px;background:white;border:1px solid #e4e4e7;border-radius:14px;margin:18px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:14px;border-radius:8px}h1{font-size:26px}button:disabled{opacity:.5;cursor:default}.muted{color:#666}.error{color:#b42318}.actions{display:flex;gap:12px;flex-wrap:wrap}a{color:inherit}</style></head><body><main><header><h1 id="title"></h1><select id="language"><option value="en">🇬🇧 English</option><option value="ja">🇯🇵 日本語</option><option value="zh">🇨🇳 中文</option></select></header><p id="intro" class="muted"></p><p><a href="/">← THERE</a></p><button id="reload"></button><p id="error" class="error" role="alert"></p><h2 id="devicesTitle"></h2><section id="devices"></section><h2 id="jobsTitle"></h2><section id="jobs"></section></main>
<script>
const words={en:{title:'Device management',intro:'Review the target and complete command before execution. Timeouts may have partial effects; inspect before retrying.',reload:'Refresh',devices:'Registered devices',jobs:'Operations',approve:'Approve and execute',cancel:'Cancel',none:'No operations yet. Ask There Agent to inspect a device or prepare an operation.',login:'Sign in to THERE with the registered owner account first.',prompt:'Execute this exact command on ',enroll:'Enrollment required',details:'Review command and result'},ja:{title:'デバイス管理',intro:'実行前に対象とコマンド全文をご確認ください。タイムアウト時も一部実行済みの場合があります。再試行前に状態を確認してください。',reload:'更新',devices:'登録済みデバイス',jobs:'操作',approve:'承認して実行',cancel:'取消',none:'操作はまだありません。There Agentに状態確認や操作の準備を依頼してください。',login:'登録済み所有者のアカウントでTHEREにログインしてください。',prompt:'次の対象で表示中のコマンドを実行しますか：',enroll:'接続設定が必要',details:'コマンドと結果を確認'},zh:{title:'设备管理',intro:'执行前请核对目标设备和完整命令。超时可能已产生部分效果，重试前请先检查状态。',reload:'刷新',devices:'已登记设备',jobs:'操作记录',approve:'确认并执行',cancel:'取消',none:'暂无操作。请让 There Agent 检查设备或准备操作方案。',login:'请先使用已登记的所有者账号登录 THERE。',prompt:'确定在以下设备上执行当前显示的完整命令：',enroll:'等待接入',details:'查看命令与结果'}};
let lang=localStorage.getItem('there-device-language')||'en';if(!words[lang])lang='en';const $=id=>document.getElementById(id);const text=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n};
async function api(path,body){const token=localStorage.getItem('token');if(!token)throw Error(words[lang].login);const r=await fetch('/api/v1/device-control/'+path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});const v=await r.json();if(!r.ok)throw Error(typeof v.detail==='string'?v.detail:'Request failed');return v}
function labels(){document.documentElement.lang=lang==='zh'?'zh-CN':lang;const w=words[lang];$('title').textContent=w.title;$('intro').textContent=w.intro;$('reload').textContent=w.reload;$('devicesTitle').textContent=w.devices;$('jobsTitle').textContent=w.jobs;$('language').value=lang}
let generation=0;
async function renderDetails(card,job,epoch){
  const detail=await api('jobs/'+encodeURIComponent(job.id));
  if(epoch!==generation||!card.isConnected)return;
  card.replaceChildren(text('h3',detail.device+' · '+detail.state),text('p',detail.description),text('pre',detail.script));
  if(detail.result)card.append(text('pre',JSON.stringify(detail.result,null,2)));
  if(detail.state!=='pending')return;
  const actions=document.createElement('div');actions.className='actions';
  for(const action of ['approve','cancel']){
    const b=text('button',words[lang][action]);
    b.onclick=async()=>{
      if(action==='approve'&&!confirm(words[lang].prompt+detail.device+'?'))return;
      for(const button of actions.children)button.disabled=true;
      try{await api('jobs/'+detail.id+'/'+action,action==='approve'?{digest:detail.digest}:{});await refresh()}
      catch(e){$('error').textContent=e.message;for(const button of actions.children)button.disabled=false}
    };
    actions.append(b);
  }
  card.append(actions);
}
async function refresh(){
  const epoch=++generation;labels();$('error').textContent='';
  try{
    const [d,j]=await Promise.all([api('devices'),api('jobs')]);
    if(epoch!==generation)return;
    $('devices').replaceChildren();
    for(const v of d.devices){const c=text('article',v.name+' · '+v.id+' · '+(v.adapter==='pending'?words[lang].enroll:v.adapter));if(v.reason)c.append(text('p',v.reason));$('devices').append(c)}
    $('jobs').replaceChildren();
    if(!j.jobs.length)$('jobs').append(text('p',words[lang].none));
    for(const v of j.jobs){
      const c=document.createElement('article');c.append(text('h3',v.device+' · '+v.state),text('p',v.description));
      const expand=text('button',words[lang].details);
      expand.onclick=async()=>{expand.disabled=true;try{await renderDetails(c,v,epoch)}catch(e){$('error').textContent=e.message;expand.disabled=false}};
      c.append(expand);$('jobs').append(c);
    }
  }catch(e){if(epoch===generation)$('error').textContent=e.message}
}
$('language').onchange=()=>{lang=$('language').value;localStorage.setItem('there-device-language',lang);refresh()};$('reload').onclick=refresh;refresh();
</script></body></html>'''
