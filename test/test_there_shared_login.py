import base64
import concurrent.futures
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient

import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from fastapi import FastAPI


def load_module(name):
    path = Path(__file__).parents[1] / 'backend/open_webui' / (name + '.py')
    spec = importlib.util.spec_from_file_location('there_sso_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


studio = load_module('studio_identity')
package = ModuleType('open_webui')
package.studio_identity = studio
with patch.dict(sys.modules, {'open_webui': package}):
    sso = load_module('there_sso')
    native = load_module('there_studio')
package.there_sso = sso
app = FastAPI()
app.include_router(sso.router)
app.add_middleware(native.StudioMiddleware)
main = SimpleNamespace(app=app)



def owner():
    return {"id": "1" * 32, "external_id": "11111111-1111-4111-8111-111111111111",
            "login": "synthetic@build.test", "login_alias": "synthetic@build.test", "display_name": "Synthetic Owner",
            "role": "admin", "super_admin": True, "expires_at": int(time.time()) + 200,
            "token": "bs1_" + "t" * 64}


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sso.sqlite3"
        self.store = sso.TransactionStore(self.path)

    def test_restart_preserves_binding_without_raw_state_or_cookie(self):
        state, cookie, challenge = self.store.create()
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT * FROM sso_transactions").fetchone()
        self.assertEqual(sso.hashed(state), row[0])
        self.assertEqual(sso.hashed(cookie), row[1])
        self.assertNotIn(state, row)
        self.assertNotIn(cookie, row)
        self.assertIsNone(sso.TransactionStore(self.path).consume("x" * 43, cookie))
        self.assertIsNone(self.store.consume(state, "y" * 43))
        verifier = sso.TransactionStore(self.path).consume(state, cookie)
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="))
        self.assertIsNone(self.store.consume(state, cookie))
        if os.name != "nt":
            self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_concurrent_consumption_has_one_winner(self):
        state, cookie, _ = self.store.create()
        stores = [sso.TransactionStore(self.path) for _ in range(8)]
        with concurrent.futures.ThreadPoolExecutor(8) as executor:
            values = list(executor.map(lambda item: item.consume(state, cookie), stores))
        self.assertEqual(1, sum(value is not None for value in values))

    def test_expiry_limit_and_replaced_browser_transaction(self):
        limited = sso.TransactionStore(self.path, maximum=1)
        state, cookie, _ = limited.create()
        with self.assertRaises(OverflowError):
            limited.create()
        new_state, new_cookie, _ = limited.create(cookie)
        self.assertIsNone(limited.consume(state, cookie))
        with patch.object(sso.time, "time", return_value=time.time() + 301):
            self.assertIsNone(limited.consume(new_state, new_cookie))
            limited.create()

    def test_bound_on_new_pending_transactions_from_one_source(self):
        for _ in range(30):
            self.store.create(source="synthetic-source")
        with self.assertRaises(OverflowError):
            self.store.create(source="synthetic-source")
        self.store.create(source="other-source")

    def test_pending_and_inflight_cancellation_survive_restart(self):
        state, cookie, _ = self.store.create()
        self.store.cancel(cookie)
        self.assertIsNone(sso.TransactionStore(self.path).consume(state, cookie))
        state, cookie, _ = self.store.create()
        self.assertIsNotNone(self.store.consume(state, cookie))
        sso.TransactionStore(self.path).cancel(cookie)
        self.assertFalse(sso.TransactionStore(self.path).complete(state, cookie))

    def test_completion_requires_consumption_and_is_once_and_unexpired(self):
        state, cookie, _ = self.store.create()
        self.assertFalse(self.store.complete(state, cookie))
        self.store.consume(state, cookie)
        self.assertFalse(self.store.complete(state, "x" * 43))
        self.assertTrue(sso.TransactionStore(self.path).complete(state, cookie))
        self.assertFalse(self.store.complete(state, cookie))
        state, cookie, _ = self.store.create()
        self.store.consume(state, cookie)
        with patch.object(sso.time, "time", return_value=time.time() + 301):
            self.assertFalse(self.store.complete(state, cookie))

    def test_original_six_column_schema_upgrades_without_losing_pending_binding(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        state, cookie, verifier = "s" * 43, "c" * 43, "v" * 64
        with closing(sqlite3.connect(old)) as db:
            db.execute("CREATE TABLE sso_transactions (state_hash TEXT PRIMARY KEY, cookie_hash TEXT NOT NULL, "
                       "verifier TEXT NOT NULL, expires_at INTEGER NOT NULL, source_hash TEXT NOT NULL, created_at INTEGER NOT NULL)")
            db.execute("INSERT INTO sso_transactions VALUES(?,?,?,?,?,?)",
                       (sso.hashed(state), sso.hashed(cookie), verifier, int(time.time()) + 200, "", int(time.time())))
            db.commit()
        upgraded = sso.TransactionStore(old)
        self.assertEqual(verifier, upgraded.consume(state, cookie))
        upgraded.cancel(cookie)
        self.assertFalse(upgraded.complete(state, cookie))

    def test_callback_access_log_never_contains_query_or_code(self):
        for target in ("/api/auth/sso/callback", "/api/auth/sso/callback/",
                       "/api/auth/sso/callback/../callback", "/api/auth/sso/./callback", "/api/auth/sso/callback/%2e%2e/callback",
                       "/api/auth/sso/callback//", "/api/auth/sso/%63allback/",
                       "/api/auth/sso%2Fcallback/", "//api//auth//sso//callback///",
                       "/%61pi/%61uth/%73so/%63allback//", "/API/auth/sso/callback/extra",
                       "/api;x/auth;y/sso;z/callback;sid/",
                       "https://buildstudio-there.com//api//auth//sso//callback/"):
            with self.subTest(target=target):
                record = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1,
                                           '%s - "%s %s HTTP/%s" %d',
                                           ("fixture-client", "GET", target + "?code=private-code&state=private-state", "1.1", 307), None)
                sso.CallbackLogFilter().filter(record)
                self.assertNotIn("private", record.getMessage())
                self.assertEqual("/api/auth/sso/callback", record.args[2])

        record = logging.LogRecord("uvicorn.access", logging.INFO, "fixture", 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("fixture-client", "GET", "/api/health?public=value", "1.1", 200), None)
        sso.CallbackLogFilter().filter(record)
        self.assertEqual("/api/health?public=value", record.args[2])


class SsoAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = sso.TransactionStore(Path(self.tmp.name) / "sso.sqlite3")
        self.exchange = AsyncMock(return_value=owner())
        self.check = AsyncMock(return_value=owner())
        self.cleanup = AsyncMock()
        for item in (patch.object(sso, "ENABLED", True), patch.object(studio, "ENABLED", True),
                     patch.dict(sys.modules, {'open_webui': package, 'open_webui.there_studio': native}),
                     patch.object(native, 'resolve_identity', AsyncMock()),
                     patch.object(sso, "_store", self.store), patch.object(studio, "call", self.exchange),
                     patch.object(studio, "check", self.check), patch.object(studio, "logout", self.cleanup)):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app, base_url=sso.ORIGIN, follow_redirects=False)
        self.addCleanup(self.client.close)

    def start(self):
        response = self.client.get("/api/auth/sso/start")
        self.assertEqual(303, response.status_code)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(sso.ISSUER + "/sso/", response.headers["location"].split("?", 1)[0])
        self.assertEqual(["there"], query["client_id"])
        self.assertEqual([sso.CALLBACK], query["redirect_uri"])
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertNotIn("code_verifier", query)
        cookie = response.headers["set-cookie"]
        for item in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=300", "Path=/"):
            self.assertIn(item, cookie)
        return query["state"][0], query

    def callback(self, original_state, **changes):
        params = {"state": original_state, "iss": sso.ISSUER, "code": "bsa1_" + "c" * 43}
        params.update(changes)
        return self.client.get("/api/auth/sso/callback", params=params)

    def test_enabled_start_callback_and_no_replay(self):
        self.assertEqual({"enabled": True}, self.client.get("/api/auth/sso/config").json())
        state, query = self.start()
        response = self.callback(state)
        self.assertEqual("/auth?sso=complete", response.headers["location"])
        self.assertIn("SameSite=strict", response.headers.get_list("set-cookie")[-1])
        self.assertIn(sso.MAIN_COOKIE, response.headers.get_list("set-cookie")[-1])
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("no-referrer", response.headers["referrer-policy"])
        call, body = self.exchange.call_args.args
        self.assertEqual("sso/exchange", call)
        self.assertEqual(sso.CALLBACK, body["redirect_uri"])
        self.assertEqual(query["code_challenge"][0], base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).decode().rstrip("="))
        self.assertEqual("/auth?sso=invalid_request", self.callback(state).headers["location"])
        self.assertEqual(1, self.exchange.await_count)
        self.assertNotIn("bs1_", response.headers["location"])
        self.assertEqual(2, self.check.await_count)
        self.check.assert_awaited_with(owner()["token"], fresh=True)
        self.cleanup.assert_not_awaited()

    def test_disabled_and_central_disabled_do_not_create_transactions(self):
        for flag in (patch.object(sso, "ENABLED", False), patch.object(studio, "ENABLED", False)):
            with flag:
                self.assertEqual({"enabled": False}, self.client.get("/api/auth/sso/config").json())
                self.assertEqual("/auth?sso=disabled", self.client.get("/api/auth/sso/start").headers["location"])
        self.exchange.assert_not_awaited()

    def test_existing_live_session_stays_on_monitor(self):
        self.client.cookies.set(sso.MAIN_COOKIE, "bs1_" + "z" * 64)
        self.check.side_effect = None
        self.check.return_value = owner()
        response = self.client.get("/api/auth/sso/start")
        self.assertEqual("/auth?sso=complete", response.headers["location"])
        self.exchange.assert_not_awaited()

    def test_foreign_start_or_external_return_url_is_refused(self):
        for kwargs in ({"headers": {"sec-fetch-site": "cross-site"}}, {"params": {"redirect_uri": "https://evil.test/"}}):
            response = self.client.get("/api/auth/sso/start", **kwargs)
            self.assertEqual("/auth?sso=invalid_request", response.headers["location"])
        self.exchange.assert_not_awaited()

    def test_state_cookie_and_issuer_are_required(self):
        for changes in ({"state": "x" * 43}, {"iss": "https://evil.test"}, {"code": "bad"}, {"code": "bsa1_" + "c" * 44}, {"extra": "https://evil.test"}):
            state, _ = self.start()
            self.assertIn(self.callback(state, **changes).headers["location"], ("/auth?sso=invalid_request", "/auth?sso=expired"))
        state, _ = self.start()
        self.client.cookies.clear()
        self.assertEqual("/auth?sso=invalid_request", self.callback(state).headers["location"])
        self.exchange.assert_not_awaited()

    def test_duplicate_fields_and_code_error_mix_refused(self):
        state, _ = self.start()
        params = [("state", state), ("state", state), ("iss", sso.ISSUER), ("code", "bsa1_" + "c" * 43)]
        response = self.client.get("/api/auth/sso/callback", params=params)
        self.assertEqual("/auth?sso=invalid_request", response.headers["location"])
        state, _ = self.start()
        self.assertEqual("/auth?sso=invalid_request", self.callback(state, error="login_required").headers["location"])
        self.exchange.assert_not_awaited()

    def test_error_callback_consumes_and_never_exchanges(self):
        state, _ = self.start()
        response = self.client.get("/api/auth/sso/callback", params={"state": state, "iss": sso.ISSUER, "error": "login_required"})
        self.assertEqual("/auth?sso=login_required", response.headers["location"])
        self.exchange.assert_not_awaited()
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM sso_transactions WHERE consumed_at IS NOT NULL").fetchone()[0])

    def test_failures_are_fixed_clean_and_not_retried(self):
        for status in (401, 403, 409, 422, 429, 503):
            state, _ = self.start()
            self.exchange.side_effect = HTTPException(status, "private upstream detail")
            response = self.callback(state)
            self.assertEqual("/auth?sso=access_denied" if status == 403 else "/auth?sso=unavailable", response.headers["location"])
            self.assertNotIn("private", response.text + str(response.headers))
            self.assertFalse(any(sso.MAIN_COOKIE in value for value in response.headers.get_list("set-cookie")))

    def test_regular_admin_malformed_or_expired_identity_never_sets_session(self):
        for changes in ({"super_admin": False}, {"super_admin": "true"}, {"role": "user"},
                        {"external_id": "same-email-is-not-a-link"}, {"expires_at": int(time.time()) - 1},
                        {"expires_at": True}, {"token": "bad"}):
            state, _ = self.start()
            self.exchange.return_value = {**owner(), **changes}
            response = self.callback(state)
            self.assertEqual("/auth?sso=unavailable", response.headers["location"])
            self.assertFalse(any(sso.MAIN_COOKIE in value for value in response.headers.get_list("set-cookie")))

    def test_upstream_exchange_uses_real_http_with_server_held_pkce(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                received.append((handler.path, json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))))
                content = json.dumps(owner()).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(content)))
                handler.end_headers()
                handler.wfile.write(content)

            def log_message(handler, *_args):
                pass

        endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=endpoint.serve_forever, daemon=True)
        worker.start()
        try:
            state, query = self.start()
            with patch.object(studio, "call", new=lambda action, body: __import__("asyncio").to_thread(studio._request, action, body)), patch.object(studio, "URL", f"http://127.0.0.1:{endpoint.server_port}/"):
                response = self.callback(state)
            self.assertEqual("/auth?sso=complete", response.headers["location"])
            self.assertEqual(1, len(received))
            self.assertEqual("/sso/exchange", received[0][0])
            self.assertNotEqual(query["code_challenge"][0], received[0][1]["code_verifier"])
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            worker.join(2)

    def test_pending_logout_without_formal_session_cancels_before_exchange(self):
        state, _ = self.start()
        cookie = self.client.cookies.get(sso.COOKIE)
        response = self.client.post("/api/v1/auths/signout", headers={"Origin": sso.ORIGIN})
        self.assertEqual(200, response.status_code)
        self.assertTrue(any(value.startswith(sso.COOKIE + '=') and 'Max-Age=0' in value
                            for value in response.headers.get_list("set-cookie")))
        callback = self.client.get("/api/auth/sso/callback", params={"state": state, "iss": sso.ISSUER,
                                  "code": "bsa1_" + "c" * 43}, headers={"Cookie": sso.COOKIE + '=' + cookie})
        self.assertEqual("/auth?sso=expired", callback.headers["location"])
        self.exchange.assert_not_awaited()
        self.cleanup.assert_not_awaited()

    def test_logout_cancellation_during_exchange_revokes_new_child(self):
        state, _ = self.start()
        cookie = self.client.cookies.get(sso.COOKIE)
        async def exchange(*_args):
            sso.TransactionStore(self.store.path).cancel(cookie)
            return owner()
        self.exchange.side_effect = exchange
        response = self.callback(state)
        self.assertEqual("/auth?sso=unavailable", response.headers["location"])
        self.assertFalse(any(sso.MAIN_COOKIE in value for value in response.headers.get_list("set-cookie")))
        self.cleanup.assert_awaited_once_with(owner()["token"])

    def test_final_fresh_check_rejects_revocation_or_changed_identity_and_cleans_child(self):
        for value in (HTTPException(401, "revoked"), HTTPException(503, "offline"),
                      {**owner(), "id": "2" * 32}, {**owner(), "external_id": "22222222-2222-4222-8222-222222222222"},
                      {**owner(), "super_admin": False}, {**owner(), "role": "user"},
                      {**owner(), "expires_at": int(time.time()) - 1}):
            with self.subTest(value=type(value).__name__):
                self.cleanup.reset_mock()
                self.check.side_effect = value if isinstance(value, Exception) else None
                self.check.return_value = value
                state, _ = self.start()
                response = self.callback(state)
                self.assertEqual("/auth?sso=unavailable", response.headers["location"])
                self.assertFalse(any(sso.MAIN_COOKIE in item for item in response.headers.get_list("set-cookie")))
                self.cleanup.assert_awaited_once_with(owner()["token"])

    def test_final_live_expiry_caps_session_cookie(self):
        self.check.return_value = {**owner(), "expires_at": int(time.time()) + 30}
        state, _ = self.start()
        response = self.callback(state)
        self.assertEqual("/auth?sso=complete", response.headers["location"])
        self.assertTrue(any("Max-Age=30" in item or "Max-Age=29" in item for item in response.headers.get_list("set-cookie")))

    def test_pending_logout_cancellation_failure_is_honest_and_still_clears_cookies(self):
        self.start()
        with patch.object(self.store, "cancel", side_effect=OSError("unavailable")):
            response = self.client.post("/api/v1/auths/signout", headers={"Origin": sso.ORIGIN})
        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": False, "shared_login_cancelled": False, "session_revoked": True}, response.json())
        self.assertTrue(any('Max-Age=0' in value for value in response.headers.get_list("set-cookie")))
        self.cleanup.assert_not_awaited()

    def test_logout_foreign_origin_or_existing_session_bad_csrf_do_not_cancel(self):
        state, _ = self.start()
        cookie = self.client.cookies.get(sso.COOKIE)
        self.assertEqual(403, self.client.post("/api/v1/auths/signout", headers={"Origin": "https://foreign.test"}).status_code)
        self.assertIsNotNone(self.store.consume(state, cookie))
        self.cleanup.assert_not_awaited()


    def test_conflicting_native_identity_denies_and_revokes_child(self):
        state, _ = self.start()
        with patch.object(native, 'resolve_identity', AsyncMock(side_effect=HTTPException(409, 'private conflict'))):
            response = self.callback(state)
        self.assertEqual('/auth?sso=unavailable', response.headers['location'])
        self.cleanup.assert_awaited_once_with(owner()['token'])
        self.assertNotIn('private', response.text + str(response.headers))

    def test_revocation_while_native_binding_is_awaited_denies_cookie(self):
        state, _ = self.start()
        self.check.side_effect = [owner(), HTTPException(401, 'revoked during mapping')]
        response = self.callback(state)
        self.assertEqual('/auth?sso=unavailable', response.headers['location'])
        self.cleanup.assert_awaited_once_with(owner()['token'])
        self.assertFalse(any(sso.MAIN_COOKIE in item for item in response.headers.get_list('set-cookie')))

    def test_missing_login_alias_is_rejected_before_native_mapping(self):
        state, _ = self.start()
        identity = owner()
        del identity['login_alias']
        self.exchange.return_value = identity
        response = self.callback(state)
        self.assertEqual('/auth?sso=unavailable', response.headers['location'])
        native.resolve_identity.assert_not_awaited()

    def test_logout_outage_clears_browser_but_reports_unconfirmed_revocation(self):
        self.client.cookies.set(sso.MAIN_COOKIE, owner()['token'])
        self.cleanup.side_effect = HTTPException(503, 'private outage')
        response = self.client.post('/api/v1/auths/signout', headers={'Origin': sso.ORIGIN})
        self.assertEqual(503, response.status_code)
        self.assertEqual({'status': False, 'shared_login_cancelled': True, 'session_revoked': False}, response.json())
        self.assertTrue(any('Max-Age=0' in value for value in response.headers.get_list('set-cookie')))


class FreshIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_check_bypasses_read_cache_and_propagates_revocation(self):
        token = "bs1_" + "r" * 64
        studio._cache.clear()
        self.addCleanup(studio._cache.clear)
        with patch.object(studio, "call", new=AsyncMock(return_value=owner())) as request:
            await studio.check(token)
            await studio.check(token)
            self.assertEqual(1, request.await_count)
            request.side_effect = HTTPException(401, "revoked")
            with self.assertRaises(HTTPException):
                await studio.check(token, fresh=True)
            self.assertEqual(2, request.await_count)
            self.assertEqual({}, studio._cache)


if __name__ == "__main__":
    unittest.main()
