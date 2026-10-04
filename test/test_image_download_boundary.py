"""Actual download helper with synthetic local HTTP servers and no external I/O."""
import ast
import asyncio
import base64
import ipaddress
import logging
from pathlib import Path
import socket
import unittest
from types import SimpleNamespace
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

BACKEND = Path(__file__).resolve().parents[1] / 'backend/open_webui'


def extract(path, names, namespace):
    tree = ast.parse(path.read_text('utf-8'))
    nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
    assert len(nodes) == len(names)
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(path), 'exec'), namespace)


class DownloadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hits = []
        self.validated = []
        self.safe_options = []
        self.runners = []
        self.shared = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        self.log = logging.getLogger('synthetic.image.boundary')
        async def image(request):
            self.hits.append(dict(request.headers))
            return web.Response(body=b'synthetic-image', content_type='image/png')
        self.destination = await self.serve(image)
        async def redirect(request):
            raise web.HTTPFound(self.destination)
        self.redirect = await self.serve(redirect)
        async def get_session(): return self.shared
        def safe_session(**kwargs):
            self.safe_options.append(kwargs)
            return aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        self.ns = dict(asyncio=asyncio, base64=base64, urlparse=urlparse, log=self.log,
                       validate_url=self.validated.append, get_session=get_session,
                       get_ssrf_safe_session=safe_session, AIOHTTP_CLIENT_SESSION_SSL=True)
        extract(BACKEND/'routers/images.py', {'_is_same_origin', 'get_image_data'}, self.ns)
        self.download = self.ns['get_image_data']

    async def serve(self, handler):
        app = web.Application(); app.router.add_get('/image', handler)
        runner = web.AppRunner(app); await runner.setup(); self.runners.append(runner)
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        return 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/image'

    async def asyncTearDown(self):
        await self.shared.close()
        for runner in self.runners: await runner.cleanup()

    async def test_trusted_backend_preserves_auth_and_private_service_support(self):
        result = await self.download(self.destination, {'Authorization':'Bearer synthetic'}, self.destination)
        self.assertEqual(result, (b'synthetic-image', 'image/png'))
        self.assertEqual(self.hits[0].get('Authorization'), 'Bearer synthetic')
        self.assertEqual(self.validated, [])
        self.assertEqual(self.safe_options, [])

    async def test_public_result_strips_all_backend_and_user_headers(self):
        result = await self.download(self.destination, {'Authorization':'Bearer synthetic', 'X-User-Email':'private@example.com', 'Cookie':'secret=value'}, 'https://provider.example')
        self.assertEqual(result, (b'synthetic-image', 'image/png'))
        for key in ['Authorization','X-User-Email','Cookie']: self.assertNotIn(key, self.hits[0])
        self.assertEqual(self.validated, [self.destination])
        self.assertEqual(self.safe_options, [{'trust_env':False, 'store_cookies':False}])

    async def test_redirects_never_reach_destination_for_trusted_or_public_result(self):
        for trusted in [self.redirect, 'https://provider.example']:
            with self.subTest(trusted=trusted):
                self.assertEqual(await self.download(self.redirect, {'Authorization':'Bearer synthetic'}, trusted), (None,None))
        self.assertEqual(self.hits, [])

    async def test_rejected_url_never_connects_and_does_not_log_signed_url(self):
        def reject(url): raise ValueError('private URL ?signature=synthetic-sensitive')
        self.ns['validate_url'] = reject
        with self.assertLogs(self.log, level='WARNING') as logs:
            self.assertEqual(await self.download(self.destination+'?signature=synthetic-sensitive'), (None,None))
        self.assertNotIn('synthetic-sensitive', '\n'.join(logs.output))
        self.assertEqual(self.safe_options, [])
        self.assertEqual(self.hits, [])

    async def test_malformed_trusted_url_does_not_bypass_validation(self):
        for bad in [self.destination+'\\suffix', self.destination+'\t', 'http://127.0.0.1:bad/image']:
            self.assertEqual(await self.download(bad, {'Authorization':'secret'}, self.destination), (None,None))
        self.assertEqual(self.hits, [])

    async def test_origin_comparison_rejects_userinfo_suffix_and_port_changes(self):
        same=self.ns['_is_same_origin']
        self.assertTrue(same('https://provider.example:443/image','https://provider.example/api'))
        for bad in ['https://provider.example.evil/image','https://provider.example@evil.example/image',
                    'https://user:pass@provider.example/image','http://provider.example/image',
                    'https://provider.example:444/image','https:///image']:
            self.assertFalse(same(bad,'https://provider.example'))

    async def test_connect_time_dns_rebinding_is_blocked(self):
        # Real production connector, fake DNS reply: initial URL validation
        # accepts the hostname; the connection-time answer becomes loopback.
        def check_addresses(addresses):
            if any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise ValueError('Blocked connection address')
        ns={'aiohttp':aiohttp, '_assert_host_allowed':lambda host:None, '_assert_addresses_allowed':check_addresses}
        extract(BACKEND/'retrieval/web/utils.py', {'_SSRFSafeConnector'}, ns)
        class Resolver(aiohttp.abc.AbstractResolver):
            async def resolve(self,host,port=0,family=socket.AF_INET):
                return [{'hostname':host,'host':'127.0.0.1','port':port,'family':socket.AF_INET,'proto':0,'flags':0}]
            async def close(self): pass
        def safe_session(**kwargs):
            self.assertEqual(kwargs, {'trust_env':False,'store_cookies':False})
            return aiohttp.ClientSession(connector=ns['_SSRFSafeConnector'](resolver=Resolver()), trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        self.ns['get_ssrf_safe_session']=safe_session
        url=self.destination.replace('127.0.0.1','synthetic-public.example')
        self.assertEqual(await self.download(url), (None,None))
        self.assertEqual(self.validated, [url])
        self.assertEqual(self.hits, [])

    async def test_base64_stays_off_network(self):
        self.assertEqual(await self.download('data:image/png;base64,'+base64.b64encode(b'synthetic').decode()), (b'synthetic','image/png'))
        self.assertEqual(self.hits, [])
        self.assertEqual(self.validated, [])

    async def test_edit_input_download_uses_same_proxy_cookie_and_redirect_boundary(self):
        tree=ast.parse((BACKEND/'routers/images.py').read_text('utf-8'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='load_url_image')
        self.ns['request']=SimpleNamespace(base_url='https://there.example/')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'images.py','exec'),self.ns)
        load=self.ns['load_url_image']
        with self.assertRaisesRegex(ValueError,'redirect'):
            await load(self.redirect)
        self.assertEqual(self.hits, [])
        self.assertEqual(self.safe_options,[{'trust_env':False,'store_cookies':False}])
        self.assertEqual(await load(self.destination), 'data:image/png;base64,'+base64.b64encode(b'synthetic-image').decode())

if __name__ == '__main__': unittest.main()
