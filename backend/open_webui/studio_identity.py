"""Backend-only Systems identity client. No local authentication fallback."""
import asyncio
import hashlib
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid

from fastapi import HTTPException

ENABLED = bool(os.environ.get('STUDIO_IDENTITY_KEY'))
APP = os.environ.get('STUDIO_IDENTITY_APP', '')
URL = 'https://buildstudio-systems.com/api/v1/identity/'
_cache = {}
log = logging.getLogger(__name__)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Identity headers and payloads belong only to the configured endpoint.
        return None


_opener = urllib.request.build_opener(_RejectRedirects())


def _failure_kind(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ConnectionResetError):
        return 'connection_reset'
    if isinstance(reason, ConnectionRefusedError):
        return 'connection_refused'
    if isinstance(reason, (json.JSONDecodeError, UnicodeDecodeError)):
        return 'invalid_response'
    return 'transport_or_response'


def _log_unavailable(action, request_id, started, kind, upstream_status=None):
    # Only fixed labels, a generated ID and numeric fields. Never interpolate an
    # exception, request, response body, source address, identity or credential.
    safe_action = action if action in {'login', 'check', 'logout', 'password'} else 'unknown'
    log.warning(
        'studio_identity_unavailable action=%s request_id=%s kind=%s elapsed_ms=%d upstream_status=%s',
        safe_action, request_id, kind, max(0, round((time.monotonic() - started) * 1000)),
        upstream_status if upstream_status is not None else '-',
    )

def _request(action, body, source='unknown'):
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    request = urllib.request.Request(URL+action, data=json.dumps(body).encode(), headers={'Content-Type':'application/json','X-Studio-Client':APP,'X-Studio-Key':os.environ.get('STUDIO_IDENTITY_KEY',''),'X-Studio-Source':source[:160],'X-Studio-Request-ID':request_id}, method='POST')
    try:
        with _opener.open(request, timeout=3) as response:
            return json.loads(response.read(16384))
    except urllib.error.HTTPError as error:
        status = error.code if error.code in (401,403,409,422,429) else 503
        error.close()
        if status == 503:
            _log_unavailable(action, request_id, started, 'upstream_http', error.code)
        messages={401:'Your credentials or session are no longer valid.',403:'This account does not have access to this system.',409:'This account needs review in Systems management.',422:'Check your username and password. New passwords require 12 characters.',429:'Too many sign-in attempts. Try again in 15 minutes.'}
        raise HTTPException(status, messages.get(status,'Staff authentication is temporarily unavailable.'), headers={'X-Studio-Request-ID': request_id} if status == 503 else None) from None
    except Exception as error:
        _log_unavailable(action, request_id, started, _failure_kind(error))
        raise HTTPException(503,'Staff authentication is temporarily unavailable.', headers={'X-Studio-Request-ID': request_id}) from None

async def call(action, body, source='unknown'):
    return await asyncio.to_thread(_request, action, body, source)

async def check(token):
    if not token or not token.startswith('bs1_') or len(token)>200:
        raise HTTPException(401,'Please sign in again.')
    key=hashlib.sha256(token.encode()).hexdigest()
    now=time.monotonic()
    cached=_cache.get(key)
    if cached and cached[0]>now: return cached[1]
    try: result=await call('check',{'token':token})
    except Exception:
        _cache.pop(key,None)
        raise
    if len(_cache)>4096: _cache.clear()
    _cache[key]=(now+3,result)
    return result

async def logout(token):
    if token:
        _cache.pop(hashlib.sha256(token.encode()).hexdigest(),None)
        await call('logout',{'token':token})
