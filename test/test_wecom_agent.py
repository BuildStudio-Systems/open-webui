import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from fastapi import HTTPException

spec=importlib.util.spec_from_file_location('wecom_agent_under_test',Path(__file__).parents[1]/'backend/open_webui/utils/wecom_agent.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class WeComAgentTests(unittest.IsolatedAsyncioTestCase):
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
        completion=httpx.Response(200,json={'choices':[{'message':{'content':'private 192.168.1.1 credential-canary'}}]},request=httpx.Request('POST','http://test'))
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.side_effect=[grant,completion]
        models={'open_webui.models.chats':SimpleNamespace(Chats=chats,ChatForm=lambda **kw:kw),'open_webui.models.users':SimpleNamespace(Users=users)}
        with patch.dict(sys.modules,models),patch.object(module.httpx,'AsyncClient',return_value=client):
            result=await module.execute(self.config,'d'*64,'Check device')
            self.assertEqual(result['state'],'completed')
            self.assertNotIn('private',str(result));self.assertNotIn('canary',str(result))
            self.assertIn('credential-canary',saved[0][2]['content'])
            headers=client.post.call_args.kwargs['headers']
            self.assertEqual(headers['X-BuildStudio-Device-Capability'],'test-proof')
            chats.get_chat_by_id.return_value=SimpleNamespace(user_id='owner',meta={'wecom_event':'d'*64})
            client.post.side_effect=[grant]
            repeated=await module.execute(self.config,'d'*64,'Changed retry text')
            self.assertTrue(repeated['duplicate'])
            self.assertEqual(client.post.call_count,3)

    async def test_central_revocation_stops_before_chat_or_agent(self):
        chats=SimpleNamespace(get_chat_by_id=AsyncMock())
        client=AsyncMock();client.__aenter__.return_value=client
        client.post.return_value=httpx.Response(403,json={},request=httpx.Request('POST','http://test'))
        models={'open_webui.models.chats':SimpleNamespace(Chats=chats,ChatForm=lambda **kw:kw),'open_webui.models.users':SimpleNamespace(Users=object())}
        with patch.dict(sys.modules,models),patch.object(module.httpx,'AsyncClient',return_value=client),self.assertRaises(HTTPException):
            await module.execute(self.config,'d'*64,'Check device')
        chats.get_chat_by_id.assert_not_awaited()


if __name__=='__main__':unittest.main()
