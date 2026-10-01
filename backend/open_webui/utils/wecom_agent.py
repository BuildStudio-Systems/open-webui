"""Private WeCom-to-Agent entry, with central revocation and private chat output.

Only the root-configured bridge may submit a message. Conversation replies may
be returned to WeCom; tool output and sensitive details remain owner-private.
"""
import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid

import httpx
from fastapi import HTTPException

_admission = asyncio.Lock()
RESET = {'新对话', '开始新对话', '/new', '/reset'}
CHAT_PROMPT = ('You are There, talking directly with your owner in a private WeCom chat. '
    'Reply naturally in the language used by the owner. Use the supplied recent conversation '
    'for follow-up questions. Do not call tools for greetings or questions answerable directly. '
    'Never include credentials, internal network addresses, topology, machine names or private '
    'file paths in your reply. Device actions still require the existing authorization policy.')


def conversation_id(config, event_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'buildstudio-wecom:'+config['binding']+':'+event_id))


def public_reply(content, tool_used, config, chat_id):
    # Tool lifecycle events are produced by the trusted gateway, not model text.
    # Withhold the entire tool-derived result; regex alone cannot scrub topology.
    sensitive = tool_used or any(isinstance(v,str) and len(v)>=16 and v in content
        for k,v in config.items() if k.endswith('_key') or k in {'owner','binding'})
    patterns = [r'\b(?:\d{1,3}\.){3}\d{1,3}\b', r'(?i)\b(?:[a-f0-9]{0,4}:){2,}[a-f0-9:]+',
        r'(?i)\b[\w.-]+\.(?:local|lan|internal)\b', r'(?i)\b(?:password|passwd|secret|api[_ -]?key|token)\s*[:=]\s*\S+',
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----',r'(?i)\b(?:ssh-rsa|ssh-ed25519)\s+',
        r'(?i)(?:[a-z]:[\\/]|/(?:etc|home|root|var|opt|proc|run|mnt)/)',
        r'(?i)\b(?:gateway-(?:jp|hk)|router-main|switch-main|buildstudio-aiserver)\b']
    sensitive = sensitive or any(re.search(p,content) for p in patterns)
    if sensitive:
        return '这次处理涉及工具结果或受保护的信息，详情已保存到你的私有会话：\nhttps://buildstudio-there.com/c/'+chat_id
    # Keep below both proactive and stream limits, without splitting UTF-8.
    if len(content.encode('utf-8'))>3300:
        return content.encode('utf-8')[:3100].decode('utf-8','ignore')+'\n\n回复较长，完整内容：https://buildstudio-there.com/c/'+chat_id
    return content


async def recent_context(chats, config, previous):
    pairs=[];budget=12000;seen=set()
    for _ in range(8):
        if not previous or previous in seen:
            break
        if not isinstance(previous,str) or not re.fullmatch(r'[a-f0-9]{64}',previous):
            raise HTTPException(409,'Invalid conversation reference.')
        seen.add(previous)
        item=await chats.get_chat_by_id(conversation_id(config,previous))
        if item is None:
            break
        if item.user_id!=config['owner'] or item.meta.get('wecom_event')!=previous or item.meta.get('channel')!='wecom':
            raise HTTPException(409,'Conversation ownership mismatch.')
        messages=list(item.chat.get('history',{}).get('messages',{}).values())
        user=next((m for m in messages if m.get('role')=='user'),None)
        reply=next((m.get('wecom_reply') for m in messages if m.get('role')=='assistant' and m.get('done')),None)
        # Never import raw tool results, old private-only outputs or other chats.
        if user and isinstance(reply,str) and reply:
            size=len(user['content'])+len(reply)
            if size>budget:break
            pairs.append([{'role':'user','content':user['content']},{'role':'assistant','content':reply}]);budget-=size
        previous=item.meta.get('wecom_previous')
    return [message for pair in reversed(pairs) for message in pair]


async def read_agent_stream(response):
    response.raise_for_status()
    text=[];size=0;tool_used=False;finished=False;event='message';data=[]
    async for line in response.aiter_lines():
        if len(line)>500000:raise ValueError('oversized_event')
        if line.startswith('event:'):event=line[6:].strip()
        elif line.startswith('data:'):data.append(line[5:].lstrip())
        elif not line:
            raw='\n'.join(data);data=[]
            if event=='hermes.tool.progress':tool_used=True
            elif raw and raw!='[DONE]':
                value=json.loads(raw)
                if value.get('error'):raise ValueError('agent_error')
                for choice in value.get('choices',[]):
                    delta=choice.get('delta') or {}
                    if delta.get('tool_calls'):tool_used=True
                    content=delta.get('content')
                    if isinstance(content,str):
                        size+=len(content)
                        if size>120000:raise ValueError('response_too_large')
                        text.append(content)
                    reason=choice.get('finish_reason')
                    if reason is not None:
                        if reason!='stop':raise ValueError('incomplete_agent')
                        finished=True
            event='message'
    if not finished or not ''.join(text).strip():raise ValueError('incomplete_stream')
    return ''.join(text),tool_used


def authenticate(request, config):
    supplied = request.headers.get('authorization', '')
    expected = config.get('bridge_key', '')
    if (request.client is None or request.client.host not in {'127.0.0.1', '::1'}
            or not isinstance(expected,str) or len(expected) != 64
            or not supplied.isascii()
            or not hmac.compare_digest(supplied, 'Bearer '+expected)):
        raise HTTPException(403, 'Channel authentication required.')


def validate(body):
    if (not isinstance(body,dict) or not {'event_id','text'} <= set(body) or set(body)-{'event_id','text','previous_event_id'}
            or not isinstance(body['event_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['event_id'])
            or not isinstance(body['text'],str) or not 1 <= len(body['text'].strip()) <= 4000
            or (body.get('previous_event_id') is not None and (not isinstance(body['previous_event_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['previous_event_id'])))):
        raise HTTPException(400, 'Invalid channel message.')
    return body['event_id'],body['text'].strip()


async def handle(request):
    config_file = os.getenv('THERE_WECOM_CONFIG_FILE', '')
    if not config_file:
        raise HTTPException(503, 'Channel is not configured.')
    try:
        config = json.loads(Path(config_file).read_text())
    except (OSError,ValueError):
        raise HTTPException(503, 'Channel is not configured.') from None
    authenticate(request,config)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20000:
            raise HTTPException(413, 'Message too large.')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid channel message.') from None
    event_id,text = validate(body)
    if _admission.locked():
        raise HTTPException(429, 'Agent channel is busy.')
    async with _admission:
        return await execute(config,event_id,text,body.get('previous_event_id'))


async def execute(config,event_id,text,previous=None):
    from open_webui.models.chats import Chats,ChatForm
    from open_webui.models.users import Users
    chat_id = conversation_id(config,event_id)
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=6) as client:
        try:
            response = await client.post('http://127.0.0.1:8743/v1/channel/session',
                json={'binding':config['binding'],'chat':chat_id},
                headers={'Authorization':'Bearer '+config['broker_key']})
            if response.status_code != 200:
                raise HTTPException(403 if response.status_code==403 else 503,'Channel authorization unavailable.')
            grant = response.json()
        except httpx.HTTPError:
            raise HTTPException(503,'Channel authorization unavailable.') from None
    owner = config['owner']
    if grant.get('owner') != owner or not isinstance(grant.get('capability'),str):
        raise HTTPException(403,'Channel authorization denied.')
    user = await Users.get_user_by_id(owner)
    if not user or user.role != 'admin':
        raise HTTPException(403,'Channel owner unavailable.')
    existing = await Chats.get_chat_by_id(chat_id)
    if existing is not None:
        if existing.user_id != owner or existing.meta.get('wecom_event') != event_id:
            raise HTTPException(409,'Channel execution conflict.')
        # A crash after execution started is never retried implicitly. The saved
        # conversation and Broker jobs provide the authenticated recovery record.
        messages=existing.chat.get('history',{}).get('messages',{}).values()
        reply=next((m.get('wecom_reply') for m in messages if m.get('role')=='assistant' and m.get('done')),None)
        return {'chat_id':chat_id,'state':'completed' if reply else 'recorded','duplicate':True,'reply':reply or '这条请求已经登记，结果尚未确认，请不要重复执行。'}
    history=await recent_context(Chats,config,previous)
    user_message_id = str(uuid.uuid4())
    assistant_id = str(uuid.uuid4())
    now = int(time.time())
    user_message = {'id':user_message_id,'parentId':None,'childrenIds':[assistant_id],
                    'role':'user','content':text,'timestamp':now,'models':['there-agent']}
    pending_message = {'id':assistant_id,'parentId':user_message_id,'childrenIds':[],
                       'role':'assistant','content':'There Agent 正在处理企业微信请求。',
                       'timestamp':now,'model':'there-agent','done':False}
    chat = {'title':'WeCom · '+text[:45],'models':['there-agent'],
            'history':{'messages':{user_message_id:user_message,assistant_id:pending_message},'currentId':assistant_id},
            'messages':[user_message,pending_message]}
    inserted = await Chats.insert_new_chat(chat_id,owner,ChatForm(chat=chat),internal_meta={'wecom_event':event_id,'channel':'wecom','wecom_previous':previous})
    if inserted is None:
        raise HTTPException(503,'Private chat storage unavailable.')
    state = 'completed'
    content = '处理结果尚未确认。请查看设备操作记录；不要直接重复执行。'
    tool_used=False
    try:
        if text in RESET:
            content='已开始新对话。你可以直接在这里和我聊天。'
        else:
          async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(290,read=290)) as client:
            async with client.stream('POST','http://127.0.0.1:8642/v1/chat/completions',
                headers={'Authorization':'Bearer '+config['agent_key'],
                         'X-BuildStudio-Chat-Id':chat_id,'X-BuildStudio-User-Id':owner,
                         'X-BuildStudio-Device-Capability':grant['capability'],
                         'Idempotency-Key':'wecom-'+event_id},
                json={'model':'there-agent','stream':True,'messages':[{'role':'system','content':CHAT_PROMPT},*history,{'role':'user','content':text}]}) as response:
                content,tool_used=await read_agent_stream(response)
    except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError):
        state = 'unconfirmed'
    reply=public_reply(content,tool_used,config,chat_id)
    saved = await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id,assistant_id,
        {**pending_message,'content':content,'done':True,'wecom_reply':reply})
    if saved is None:
        state = 'unconfirmed'
        reply='结果保存失败，请先检查 There 会话，不要重复执行设备操作。'
    return {'chat_id':chat_id,'state':state,'duplicate':False,'reply':reply}
