"""Exercise the identity transport against local HTTP servers with fake secrets."""
import json
import importlib.util
from pathlib import Path
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi import HTTPException

spec = importlib.util.spec_from_file_location(
    'there_identity_transport_under_test',
    Path(__file__).parents[1] / 'backend/open_webui/studio_identity.py',
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


@contextmanager
def endpoint(status=200, location=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.read_request()

        def do_GET(self):
            self.read_request()

        def read_request(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            received.append({'method': self.command,
                             'key_present': bool(self.headers.get('X-Studio-Key'))})
            body = json.dumps({'ok': True}).encode()
            self.send_response(status)
            if location:
                self.send_header('Location', location)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': 0.01}, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class IdentityTransportTests(unittest.TestCase):
    def request(self, url):
        with patch.object(studio, 'URL', url), patch.dict(
            studio.os.environ, {'STUDIO_IDENTITY_KEY': 'synthetic-test-key'}
        ):
            return studio._request('check', {'token': 'synthetic-session'})

    def test_direct_success(self):
        with endpoint() as (url, received):
            self.assertEqual(self.request(url), {'ok': True})
            self.assertEqual(received, [{'method': 'POST', 'key_present': True}])

    def test_redirect_never_receives_application_key(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), endpoint() as (destination, forwarded):
                with endpoint(status, destination) as (url, received):
                    with self.assertRaises(HTTPException) as failure:
                        self.request(url)
                    self.assertEqual(failure.exception.status_code, 503)
                    self.assertEqual(len(received), 1)
                    self.assertEqual(forwarded, [])

    def test_upstream_authentication_errors_keep_their_status(self):
        for status in (401, 403, 409, 422, 429):
            with self.subTest(status=status), endpoint(status) as (url, _):
                with self.assertRaises(HTTPException) as failure:
                    self.request(url)
                self.assertEqual(failure.exception.status_code, status)

    def test_upstream_failure_is_unavailable(self):
        with endpoint(500) as (url, _):
            with self.assertRaises(HTTPException) as failure:
                self.request(url)
            self.assertEqual(failure.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
