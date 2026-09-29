"""Owner-only, short-lived device capabilities; SSH credentials never enter Web."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import time
import uuid

CONFIG = Path('/etc/buildstudio-there/device-control-web.json')
HEADER = 'X-BuildStudio-Device-Capability'


def explicit_session_header(value):
    if not isinstance(value, str):
        return False
    parts = value.split(' ')
    return (len(parts) == 2 and parts[0].lower() == 'bearer'
            and bool(parts[1]) and not any(c.isspace() for c in parts[1])
            and not parts[1].startswith('sk-'))


def capability(user, scope, *, chat='', job='', digest='', config_path=None):
    if getattr(user, 'role', None) != 'admin':
        return ''
    try:
        config = json.loads((config_path or CONFIG).read_text())
    except FileNotFoundError:
        return ''  # Optional installation; ordinary chat remains available.
    if str(getattr(user, 'id', '')) not in config.get('owners', []):
        return ''
    key = Path(config['signing_key_file']).read_bytes().strip()
    if len(key) < 32 or scope not in {'agent', 'console', 'approve'}:
        raise ValueError('Device control configuration is invalid')
    now = int(time.time())
    claims = {'aud': 'there-device-control-v1', 'owner': str(user.id), 'scope': scope,
              'jti': uuid.uuid4().hex,
              'chat': chat, 'iat': now, 'exp': now + (900 if scope == 'agent' else 60),
              'job': job, 'digest': digest}
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    return body + '.' + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


async def control_request(user, body, *, approve=False):
    import httpx
    from fastapi import HTTPException
    proof = capability(user, 'approve' if approve else 'console',
                       job=body.get('job', '') if approve else '',
                       digest=body.get('digest', '') if approve else '')
    if not proof:
        raise HTTPException(403, 'Device management is available only to the registered owner.')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=115) as client:
        try:
            r = await client.post('http://127.0.0.1:8743/v1/control', json=body,
                                  headers={'Authorization': 'Bearer ' + proof})
        except httpx.HTTPError:
            raise HTTPException(503, 'Device management is unavailable.') from None
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.json().get('error', 'Device request failed'))
    return r.json()
