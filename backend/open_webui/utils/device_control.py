"""Device delegation through independently verified Systems sessions; no Web signing key."""
HEADER = 'X-BuildStudio-Device-Capability'
BROKER = 'http://127.0.0.1:8743'


def explicit_session_header(value):
    if not isinstance(value, str):
        return False
    parts = value.split(' ')
    return (len(parts) == 2 and parts[0].lower() == 'bearer'
            and bool(parts[1]) and not any(c.isspace() for c in parts[1])
            and not parts[1].startswith('sk-'))


def session_from_request(request):
    header = getattr(request, 'headers', {}).get('authorization')
    if header:
        return header.split(' ')[1] if explicit_session_header(header) else ''
    return getattr(request, 'cookies', {}).get('token', '')


async def capability(user, scope, *, session_token='', chat='', job='', digest=''):
    if (getattr(user, 'role', None) != 'admin' or not isinstance(session_token, str)
            or not session_token.startswith('bs1_') or len(session_token) > 200):
        return ''
    import httpx
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        try:
            response = await client.post(BROKER + '/v1/session',
                json={'scope': scope, 'chat': chat, 'job': job, 'digest': digest},
                headers={'Authorization': 'Bearer ' + session_token})
        except httpx.HTTPError:
            raise ValueError('Device authorization unavailable') from None
    if response.status_code in (401, 403):
        return ''
    if response.status_code != 200:
        raise ValueError('Device authorization unavailable')
    result = response.json()
    if (not isinstance(result, dict) or result.get('owner') != str(getattr(user, 'id', ''))
            or not isinstance(result.get('capability'), str) or len(result['capability']) > 4096):
        raise ValueError('Device authorization mismatch')
    return result['capability']


async def control_request(user, body, *, session_token='', approve=False):
    import httpx
    from fastapi import HTTPException
    try:
        proof = await capability(user, 'approve' if approve else 'console', session_token=session_token,
                           job=body.get('job', '') if approve else '',
                           digest=body.get('digest', '') if approve else '')
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, 'Device management is unavailable.') from None
    if not proof:
        raise HTTPException(403, 'Device management is available only to the registered owner.')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=115) as client:
        try:
            r = await client.post(BROKER + '/v1/control', json=body,
                                  headers={'Authorization': 'Bearer ' + proof})
        except httpx.HTTPError:
            raise HTTPException(503, 'Device management is unavailable.') from None
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.json().get('error', 'Device request failed'))
    return r.json()
