"""There shared sign-in: fixed issuer, stable native identity, revocable parent."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import posixpath
import re
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from open_webui import studio_identity as studio

ISSUER = "https://buildstudio-systems.com"
ORIGIN = "https://buildstudio-there.com"
CALLBACK = ORIGIN + "/api/auth/sso/callback"
COOKIE = "__Host-bs_there_sso"
MAIN_COOKIE = "token"
ENABLED = os.environ.get("THERE_SSO_ENABLED") == "1"
TTL = 300
OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
CODE = re.compile(r"^bsa1_[A-Za-z0-9_-]{43}$")
APP_TOKEN = re.compile(r"^bs1_[A-Za-z0-9_-]{16,196}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
router = APIRouter()
_store = None
_store_lock = threading.Lock()


class CallbackLogFilter(logging.Filter):
    """Uvicorn logs its request target even when reverse-proxy logs are off."""
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            target = args[2]
            # Preserve origin-form leading // instead of treating it as a
            # network URL authority; match the proxies' decoded/merged URI.
            raw_path = urlsplit(target).path if re.match(r"https?://", target, re.I) else target.split("?", 1)[0].split("#", 1)[0]
            path = posixpath.normpath(re.sub(r"/+", "/", re.sub(r";[^/]*", "", unquote(raw_path)))).rstrip("/").lower()
            if path == "/api/auth/sso/callback" or path.startswith("/api/auth/sso/callback/"):
                record.args = args[:2] + ("/api/auth/sso/callback",) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(CallbackLogFilter())


def enabled() -> bool:
    return ENABLED and studio.ENABLED


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class TransactionStore:
    """A bounded durable one-use state/cookie/PKCE binding, shared by workers."""

    def __init__(self, path: str | Path, maximum: int = 4000):
        self.path = Path(path)
        self.maximum = maximum
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ValueError("invalid transaction storage")
        else:
            os.close(descriptor)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS sso_transactions ("
                       "state_hash TEXT PRIMARY KEY, cookie_hash TEXT NOT NULL, "
                       "verifier TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                       "source_hash TEXT NOT NULL, created_at INTEGER NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(sso_transactions)")}
            for name, definition in (("consumed_at", "INTEGER"), ("completed_at", "INTEGER"),
                                     ("cancelled", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute("ALTER TABLE sso_transactions ADD COLUMN " + name + " " + definition)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, previous_cookie: str = "", source: str = "") -> tuple[str, str, str]:
        state, cookie, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sso_transactions WHERE expires_at<=?", (now,))
            if OPAQUE.fullmatch(previous_cookie):
                db.execute("DELETE FROM sso_transactions WHERE cookie_hash=?", (hashed(previous_cookie),))
            if db.execute("SELECT count(*) FROM sso_transactions").fetchone()[0] >= self.maximum:
                raise OverflowError("transaction limit")
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""
            if source_hash and db.execute("SELECT count(*) FROM sso_transactions WHERE source_hash=? AND created_at>?",
                                          (source_hash, now - 60)).fetchone()[0] >= 30:
                raise OverflowError("source transaction limit")
            db.execute("INSERT INTO sso_transactions(state_hash,cookie_hash,verifier,expires_at,source_hash,created_at) VALUES(?,?,?,?,?,?)",
                       (hashed(state), hashed(cookie), verifier, now + TTL, source_hash, now))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        return state, cookie, challenge

    def consume(self, state: str, cookie: str) -> str | None:
        if not OPAQUE.fullmatch(state) or not OPAQUE.fullmatch(cookie):
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = db.execute("SELECT verifier FROM sso_transactions WHERE state_hash=? AND cookie_hash=? "
                             "AND consumed_at IS NULL AND cancelled=0 AND expires_at>?",
                             (hashed(state), hashed(cookie), now)).fetchone()
            if row is None:
                return None
            # Keep the bounded receipt until expiry so logout can cancel an
            # exchange already running in another process. Never store a token.
            db.execute("UPDATE sso_transactions SET consumed_at=? WHERE state_hash=?", (now, hashed(state)))
            return row[0]

    def cancel(self, cookie: str | None) -> None:
        if not OPAQUE.fullmatch(cookie or ""):
            return
        with self.connect() as db:
            db.execute("UPDATE sso_transactions SET cancelled=1 WHERE cookie_hash=?", (hashed(cookie),))

    def complete(self, state: str, cookie: str) -> bool:
        if not OPAQUE.fullmatch(state) or not OPAQUE.fullmatch(cookie):
            return False
        with self.connect() as db:
            now = int(time.time())
            changed = db.execute("UPDATE sso_transactions SET completed_at=? WHERE state_hash=? AND cookie_hash=? "
                                 "AND consumed_at IS NOT NULL AND completed_at IS NULL AND cancelled=0 AND expires_at>?",
                                 (now, hashed(state), hashed(cookie), now))
            return changed.rowcount == 1


def store() -> TransactionStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TransactionStore(os.environ.get("THERE_SSO_DB", os.path.expanduser("~/.local/state/buildstudio-there/sso.sqlite3")))
    return _store


def result(error: str | None = None):
    response = RedirectResponse("/auth?sso=" + error if error else "/auth?sso=complete", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def owner_principal(value: object, *, require_token: bool = True) -> dict:
    now = int(time.time())
    if not isinstance(value, dict):
        raise ValueError("invalid identity")
    expires = value.get("expires_at")
    if (value.get("super_admin") is not True or value.get("role") != "admin" or
        not isinstance(value.get("external_id"), str) or not UUID.fullmatch(value["external_id"]) or
        not isinstance(value.get("id"), str) or not (re.fullmatch(r"[0-9a-f]{32}", value["id"]) or UUID.fullmatch(value["id"])) or
        (require_token and (not isinstance(value.get("token"), str) or not APP_TOKEN.fullmatch(value["token"]))) or
        not isinstance(value.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9@._+-]{3,160}", value["login"]) or
        not isinstance(value.get("login_alias"), str) or not re.fullmatch(r"[A-Za-z0-9@._+-]{3,160}", value["login_alias"]) or
        not isinstance(value.get("display_name"), str) or len(value["display_name"]) > 120 or
        type(expires) is not int or not now < expires <= now + 28860):
        raise ValueError("invalid owner identity")
    return value


@router.get("/api/auth/sso/config")
def config():
    return JSONResponse({"enabled": enabled()}, headers={"Cache-Control": "no-store"})


@router.get("/api/auth/sso/start")
async def start(request: Request):
    if not enabled():
        return result("disabled")
    # A foreign embedding cannot replace this browser's outstanding transaction.
    if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "none"} or request.query_params:
        return result("invalid_request")
    current = request.cookies.get(MAIN_COOKIE)
    if current:
        try:
            await asyncio.wait_for(studio.check(current, fresh=True), timeout=4)
            return result()
        except HTTPException as error:
            if error.status_code not in {401, 403}:
                return result("unavailable")
        except Exception:
            return result("unavailable")
    try:
        state, cookie, challenge = await asyncio.to_thread(store().create, request.cookies.get(COOKIE, ""),
                                                         request.client.host if request.client else "unknown")
    except Exception:
        return result("unavailable")
    query = urlencode({"client_id": "there", "redirect_uri": CALLBACK, "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
    response = RedirectResponse(ISSUER + "/sso/?" + query, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.set_cookie(COOKIE, cookie, max_age=TTL, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/api/auth/sso/callback")
async def callback(request: Request):
    if not enabled():
        return result("disabled")
    pairs = list(request.query_params.multi_items())
    values = dict(pairs)
    expected = {"state", "iss", "code"} if "code" in values else {"state", "iss", "error"}
    if len(pairs) != len(values) or set(values) != expected or values.get("iss") != ISSUER:
        return result("invalid_request")
    state, cookie = values.get("state", ""), request.cookies.get(COOKIE, "")
    if not OPAQUE.fullmatch(state) or not OPAQUE.fullmatch(cookie):
        return result("invalid_request")
    if "code" in values and not CODE.fullmatch(values["code"]):
        return result("invalid_request")
    if "error" in values and values["error"] not in {"login_required", "access_denied", "temporarily_unavailable", "invalid_request"}:
        return result("invalid_request")
    try:
        verifier = await asyncio.to_thread(store().consume, state, cookie)
    except Exception:
        return result("unavailable")
    if verifier is None:
        return result("expired")
    if "error" in values:
        return result({"temporarily_unavailable": "unavailable"}.get(values["error"], values["error"]))
    identity = None
    try:
        # No replay on timeout: the transaction is consumed before exchanging.
        identity = await asyncio.wait_for(studio.call("sso/exchange", {
            "code": values["code"], "redirect_uri": CALLBACK, "code_verifier": verifier,
        }), timeout=4)
        owner_principal(identity)
        live = owner_principal(await asyncio.wait_for(studio.check(identity["token"], fresh=True), timeout=4),
                               require_token=False)
        if live["id"] != identity["id"] or live["external_id"] != identity["external_id"]:
            raise ValueError("identity changed")
        from open_webui.there_studio import resolve_identity
        await resolve_identity(live)
        # Identity binding may await storage; check central expiry/revocation again.
        confirmed = owner_principal(await asyncio.wait_for(studio.check(identity["token"], fresh=True), timeout=4), require_token=False)
        if confirmed["id"] != live["id"] or confirmed["external_id"] != live["external_id"]:
            raise ValueError("identity changed")
        if not await asyncio.to_thread(store().complete, state, cookie):
            raise ValueError("shared sign-in was cancelled")
        expiry = min(identity["expires_at"], live["expires_at"], confirmed["expires_at"])
        if expiry <= int(time.time()):
            raise ValueError("identity expired")
    except Exception as error:
        token = identity.get("token") if isinstance(identity, dict) else None
        if isinstance(token, str) and APP_TOKEN.fullmatch(token):
            try:
                await asyncio.wait_for(studio.logout(token), timeout=4)
            except Exception:
                logging.getLogger(__name__).warning("Shared sign-in cleanup could not be confirmed.")
        return result("access_denied" if isinstance(error, HTTPException) and error.status_code == 403 else "unavailable")
    response = result()
    response.set_cookie(MAIN_COOKIE, identity["token"], max_age=expiry - int(time.time()),
                        path="/", secure=True, httponly=True, samesite="strict")
    return response
