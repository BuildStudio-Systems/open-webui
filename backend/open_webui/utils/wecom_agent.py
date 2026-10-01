"""Private WeCom-to-Agent entry, with central revocation and private chat output.

Only the root-configured bridge may submit a message. Detailed results are saved
as an ordinary owner-private There chat; the caller receives IDs/status only.
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


def authenticate(request, config):
    supplied = request.headers.get('authorization', '')
    expected = config.get('bridge_key', '')
    if (request.client is None or request.client.host not in {'127.0.0.1', '::1'}
            or not isinstance(expected,str) or len(expected) != 64
            or not supplied.isascii()
            or not hmac.compare_digest(supplied, 'Bearer '+expected)):
        raise HTTPException(403, 'Channel authentication required.')


def validate(body):
    if (not isinstance(body,dict) or set(body) != {'event_id','text'}
            or not isinstance(body['event_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['event_id'])
            or not isinstance(body['text'],str) or not 1 <= len(body['text'].strip()) <= 4000):
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
        return await execute(config,event_id,text)


async def execute(config,event_id,text):
    from open_webui.models.chats import Chats,ChatForm
    from open_webui.models.users import Users
    chat_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'buildstudio-wecom:'+config['binding']+':'+event_id))
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
        return {'chat_id':chat_id,'state':'recorded','duplicate':True}
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
    inserted = await Chats.insert_new_chat(chat_id,owner,ChatForm(chat=chat),internal_meta={'wecom_event':event_id,'channel':'wecom'})
    if inserted is None:
        raise HTTPException(503,'Private chat storage unavailable.')
    state = 'completed'
    content = '处理结果尚未确认。请查看设备操作记录；不要直接重复执行。'
    try:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=290) as client:
            response = await client.post('http://127.0.0.1:8642/v1/chat/completions',
                headers={'Authorization':'Bearer '+config['agent_key'],
                         'X-BuildStudio-Chat-Id':chat_id,'X-BuildStudio-User-Id':owner,
                         'X-BuildStudio-Device-Capability':grant['capability'],
                         'Idempotency-Key':'wecom-'+event_id},
                json={'model':'there-agent','stream':False,'messages':[{'role':'user','content':text}]})
            response.raise_for_status()
            value = response.json()['choices'][0]['message']['content']
            if not isinstance(value,str) or not value.strip():
                raise ValueError('empty_response')
            content = value[:120000]
    except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError):
        state = 'unconfirmed'
    saved = await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id,assistant_id,
        {**pending_message,'content':content,'done':True})
    if saved is None:
        state = 'unconfirmed'
    # Never return the model output, tool evidence or request text to Tencent.
    return {'chat_id':chat_id,'state':state,'duplicate':False}
