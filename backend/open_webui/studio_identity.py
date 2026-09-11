"""Backend-only Systems identity client. No local authentication fallback."""
import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from fastapi import HTTPException

ENABLED = bool(os.environ.get('STUDIO_IDENTITY_KEY'))
APP = os.environ.get('STUDIO_IDENTITY_APP', '')
URL = 'https://buildstudio-systems.com/api/v1/identity/'
_cache = {}

def _request(action, body, source='unknown'):
    request = urllib.request.Request(URL+action, data=json.dumps(body).encode(), headers={'Content-Type':'application/json','X-Studio-Client':APP,'X-Studio-Key':os.environ.get('STUDIO_IDENTITY_KEY',''),'X-Studio-Source':source[:160]}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read(16384))
    except urllib.error.HTTPError as error:
        status = error.code if error.code in (401,403,409,422,429) else 503
        messages={401:'Your credentials or session are no longer valid.',403:'This account does not have access to this system.',409:'This account needs review in Systems management.',422:'Check your username and password. New passwords require 12 characters.',429:'Too many sign-in attempts. Try again in 15 minutes.'}
        raise HTTPException(status, messages.get(status,'Staff authentication is temporarily unavailable.')) from None
    except Exception:
        raise HTTPException(503,'Staff authentication is temporarily unavailable.') from None

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
