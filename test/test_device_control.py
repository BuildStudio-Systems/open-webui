"""Web delegates using the human session; it cannot create device authority."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import httpx

spec=importlib.util.spec_from_file_location('device_control_client',Path(__file__).parents[1]/'backend/open_webui/utils/device_control.py')
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)


class DelegationTests(IsolatedAsyncioTestCase):
    async def test_session_goes_only_to_fixed_local_broker(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'owner':'owner','capability':'bounded-proof'})
        actual=httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kw:actual(transport=httpx.MockTransport(handler),**kw)):
            value=await client.capability(SimpleNamespace(id='owner',role='admin'),'agent',session_token='bs1_'+'a'*32,chat='fixture')
        self.assertEqual(value,'bounded-proof')
        self.assertEqual(str(seen[0].url),'http://127.0.0.1:8743/v1/session')
        self.assertNotIn(b'"owner"',seen[0].content)
        self.assertEqual(seen[0].headers['Authorization'],'Bearer bs1_'+'a'*32)

    async def test_nonowner_response_mismatch_and_unavailable_fail_closed(self):
        actual=httpx.AsyncClient
        for status,body,expected in ((403,{},''),(200,{'owner':'different','capability':'proof'},None),
                                     (503,{},None),(200,[],None)):
            def handler(request):return httpx.Response(status,json=body)
            with patch.object(httpx,'AsyncClient',side_effect=lambda **kw:actual(transport=httpx.MockTransport(handler),**kw)):
                if expected is None:
                    with self.assertRaises(ValueError):await client.capability(SimpleNamespace(id='owner',role='admin'),'console',session_token='bs1_'+'a'*32)
                else:self.assertEqual(await client.capability(SimpleNamespace(id='owner',role='admin'),'console',session_token='bs1_'+'a'*32),expected)

    async def test_redirect_does_not_forward_session(self):
        seen=[];actual=httpx.AsyncClient
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307,headers={'Location':'https://untrusted.invalid/capture'})
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kw:actual(transport=httpx.MockTransport(handler),**kw)):
            with self.assertRaises(ValueError):await client.capability(SimpleNamespace(id='owner',role='admin'),'console',session_token='bs1_'+'a'*32)
        self.assertEqual(seen,['http://127.0.0.1:8743/v1/session'])

    async def test_explicit_bad_header_never_falls_back_to_owner_cookie(self):
        for header in ('Bearer sk-machine','Basic x','Bearer  double','Bearer token\n'):
            request=SimpleNamespace(headers={'authorization':header},cookies={'token':'bs1_cookie'})
            self.assertEqual(client.session_from_request(request),'')
        self.assertEqual(client.session_from_request(SimpleNamespace(headers={},cookies={'token':'bs1_cookie'})),'bs1_cookie')
