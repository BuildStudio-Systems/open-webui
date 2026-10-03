import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch

import httpx
from fastapi import HTTPException

spec=importlib.util.spec_from_file_location('wecom_agent_under_test',Path(__file__).parents[1]/'backend/open_webui/utils/wecom_agent.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class WeComAgentTests(unittest.IsolatedAsyncioTestCase):
    def test_monitor_node_is_fixed_allowlist(self):
        body={'event_id':'d'*64,'text':'review'}
        self.assertEqual(module.validate({**body,'monitor_node':'monitoring'}),('d'*64,'review'))
        for value in ['unknown','router-main',[],None]:
            with self.assertRaises(HTTPException):module.validate({**body,'monitor_node':value})
    def setUp(self):
        self.config={'bridge_key':'a'*64,'owner':'owner','binding':'b'*64,'broker_key':'c'*64,'agent_key':'test-agent'}

    def test_authentication_and_body_identity_are_not_user_controlled(self):
        for host,token in [('192.0.2.1','Bearer '+'a'*64),('127.0.0.1','Bearer '+'z'*64),('127.0.0.1','密')]:
            with self.assertRaises(HTTPException):
                module.authenticate(SimpleNamespace(client=SimpleNamespace(host=host),headers={'authorization':token}),self.config)
        module.authenticate(SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),headers={'authorization':'Bearer '+'a'*64}),self.config)
        for body in [{'event_id':'a'*64,'text':'hello','owner':'other'},{'event_id':'../../x','text':'hello'},{'event_id':'a'*64,'text':' '*20}]:
            with self.assertRaises(HTTPException):module.validate(body)

    async def test_result_remains_private_and_duplicate_does_not_execute_twice(self):
        saved=[]
        chats=SimpleNamespace(get_chat_by_id=AsyncMock(return_value=None),insert_new_chat=AsyncMock(return_value=True),upsert_message_to_chat_by_id_and_message_id=AsyncMock(side_effect=lambda *a:saved.append(a) or True))
        users=SimpleNamespace(get_user_by_id=AsyncMock(return_value=SimpleNamespace(role='admin')))
        grant=httpx.Response(200,json={'owner':'owner','capability':'test-proof'},request=httpx.Request('POST','http://test'))
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.return_value=grant
        response=self.stream_response('private 192.168.1.1 credential-canary',tool=True)
        context=AsyncMock();context.__aenter__.return_value=response
        client.stream=MagicMock(return_value=context)
        models={'open_webui.models.chats':SimpleNamespace(Chats=chats,ChatForm=lambda **kw:kw),'open_webui.models.users':SimpleNamespace(Users=users)}
        with patch.dict(sys.modules,models),patch.object(module.httpx,'AsyncClient',return_value=client):
            result=await module.execute(self.config,'d'*64,'Check device')
            self.assertEqual(result['state'],'completed')
            self.assertNotIn('private',str(result));self.assertNotIn('canary',str(result))
            self.assertIn('credential-canary',saved[0][2]['content'])
            headers=client.stream.call_args.kwargs['headers']
            self.assertEqual(headers['X-BuildStudio-Device-Capability'],'test-proof')
            chats.get_chat_by_id.return_value=SimpleNamespace(user_id='owner',meta={'wecom_event':'d'*64},chat={'history':{'messages':{'a':saved[0][2]}}})
            repeated=await module.execute(self.config,'d'*64,'Changed retry text')
            self.assertTrue(repeated['duplicate'])
            self.assertEqual(client.post.call_count,2)
            self.assertEqual(client.stream.call_count,1)
            self.assertEqual(repeated['reply'],result['reply'])

    async def test_heartbeat_stream_has_total_deadline_and_saves_unconfirmed(self):
        saved=[]
        chats=SimpleNamespace(get_chat_by_id=AsyncMock(return_value=None),insert_new_chat=AsyncMock(return_value=True),upsert_message_to_chat_by_id_and_message_id=AsyncMock(side_effect=lambda *a:saved.append(a) or True))
        users=SimpleNamespace(get_user_by_id=AsyncMock(return_value=SimpleNamespace(role='admin')))
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.return_value=httpx.Response(200,json={'owner':'owner','capability':'test-proof'},request=httpx.Request('POST','http://test'))
        closed=asyncio.Event()
        async def heartbeats():
            try:
                while True:
                    yield ': heartbeat'
                    await asyncio.sleep(0.002)
            finally:closed.set()
        response=SimpleNamespace(raise_for_status=lambda:None,aiter_lines=heartbeats)
        context=AsyncMock();context.__aenter__.return_value=response;client.stream=MagicMock(return_value=context)
        models={'open_webui.models.chats':SimpleNamespace(Chats=chats,ChatForm=lambda **kw:kw),'open_webui.models.users':SimpleNamespace(Users=users)}
        with patch.dict(sys.modules,models),patch.object(module.httpx,'AsyncClient',return_value=client),patch.object(module,'AGENT_TOTAL_TIMEOUT',0.02):
            result=await asyncio.wait_for(module.execute(self.config,'a'*64,'Read metadata'),1)
        self.assertEqual(result['state'],'unconfirmed')
        self.assertTrue(closed.is_set())
        self.assertTrue(saved[0][2]['done'])
        self.assertEqual(client.stream.call_count,1)
        self.assertIn('尚未确认',result['reply'])

    @staticmethod
    def stream_response(content,tool=False,finish='stop'):
        async def lines():
            if tool:
                for line in ['event: hermes.tool.progress','data: {"status":"running","tool":"there_devices"}','']:yield line
            for value in [{'choices':[{'delta':{'content':content},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':finish}]}]:
                yield 'data: '+json.dumps(value);yield ''
            yield 'data: [DONE]';yield ''
        return SimpleNamespace(raise_for_status=lambda:None,aiter_lines=lines)

    async def test_stream_and_output_privacy(self):
        content,tools=await module.read_agent_stream(self.stream_response('Hello!'))
        self.assertFalse(tools)
        self.assertEqual(module.public_reply(content,tools,self.config,'chat'),'Hello!')
        for secret in ['internal 10.0.0.4','[device](http://host.lan)','password=hidden','SSH /root/private','a'*64,
                       'fe80::1','2404:1a8:7f01:a::3','2001:4860:4860:0:0:0:0:8888','C:\\Users\\x','on backendserver','user buildstudio-monitoring']:
            self.assertNotIn(secret,module.public_reply(secret,False,self.config,'chat'))
        self.assertNotIn('raw tool result',module.public_reply('raw tool result',True,self.config,'chat'))
        # Review 2026-10-03: clock times, ratios and public links are ordinary chat, not topology.
        for plain in ['任务在 02:34:20 完成','比例大约是 1:2:3','官网是 https://buildstudio-systems.com/ 欢迎访问','现在是 14:05']:
            self.assertEqual(module.public_reply(plain,False,self.config,'chat'),plain)
        with self.assertRaises(ValueError):await module.read_agent_stream(self.stream_response('partial',finish='error'))

    async def test_history_uses_only_bound_public_replies_in_order(self):
        def row(event,previous,user,reply):
            return SimpleNamespace(user_id='owner',meta={'wecom_event':event,'channel':'wecom','wecom_previous':previous},chat={'history':{'messages':{'u':{'role':'user','content':user},'a':{'role':'assistant','content':'NEVER-IMPORT-PRIVATE','wecom_reply':reply,'done':True}}}})
        chats=SimpleNamespace(get_chat_by_id=AsyncMock(side_effect=[row('e'*64,'d'*64,'second','answer2'),row('d'*64,None,'first','answer1')]))
        history=await module.recent_context(chats,self.config,'e'*64)
        self.assertEqual([m['content'] for m in history],['first','answer1','second','answer2'])
        self.assertNotIn('NEVER-IMPORT',str(history))
        bad=row('e'*64,None,'x','y');bad.user_id='other'
        chats.get_chat_by_id=AsyncMock(return_value=bad)
        with self.assertRaises(HTTPException):await module.recent_context(chats,self.config,'e'*64)
        self.assertEqual(await module.recent_context(chats,self.config,None),[])

    async def test_central_revocation_stops_before_chat_or_agent(self):
        chats=SimpleNamespace(get_chat_by_id=AsyncMock())
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.return_value=httpx.Response(403,json={},request=httpx.Request('POST','http://test'))
        models={'open_webui.models.chats':SimpleNamespace(Chats=chats,ChatForm=lambda **kw:kw),'open_webui.models.users':SimpleNamespace(Users=object())}
        with patch.dict(sys.modules,models),patch.object(module.httpx,'AsyncClient',return_value=client),self.assertRaises(HTTPException):
            await module.execute(self.config,'d'*64,'Check device')
        chats.get_chat_by_id.assert_not_awaited()


if __name__=='__main__':unittest.main()
