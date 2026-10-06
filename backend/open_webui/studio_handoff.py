"""Owner-scoped PDF handoff for native application backends (stdlib only).

Never use this in a browser. Supply the application's existing Systems key and
the current user's application session, not a site password or administrator
cookie. Preserve request_key across transport failures and application restarts.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid

SOURCES = frozenset({'there','monitor','ark','order','slot','finance'})
SERVICES = SOURCES | {'systems','share'}
SYSTEMS = 'https://buildstudio-systems.com/api/v1/identity/coordination/delegate'
SHARE = 'https://buildstudio-share.com/api/integrations/artifacts'


# Systems stamps a grant at most 120 s ahead on its database clock; this host keeps its own clock,
# so the far-future bound allows 10 s of skew (Claude 2026-10-06; was 125, Share accepts up to 130).
MAX_GRANT_AHEAD=130


class HandoffError(Exception):
    """Fixed, non-sensitive errors: no upstream body or credential in messages.
    ``reason`` is a stable, non-sensitive code the calling app may show or map
    (``share_rejected`` by default; ``share_link_required`` when Systems answered
    that the user has no linked Share account yet; ``share_quota`` on 413)."""
    def __init__(self,message='',reason='share_rejected'):
        super().__init__(message);self.reason=reason


class OutcomeUnconfirmed(HandoffError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(source,service_key,session_token,target,*,transport=None):
    """Read a fixed health endpoint using the caller's current two-system scope."""
    transport=transport or request
    if source not in SERVICES-{'systems'} or target not in SERVICES or source==target:
        raise HandoffError('A registered source and different target are required.')
    if not isinstance(service_key,str) or len(service_key)<32 or not isinstance(session_token,str) or not re.fullmatch(r'bs1_[A-Za-z0-9_-]{16,196}',session_token):
        raise HandoffError('Application credentials and a current session are required.')
    status,result=transport('https://buildstudio-systems.com/api/v1/identity/coordination/invoke',
        json.dumps({'session_token':session_token,'target':target,'capability':'system.probe'}).encode(),
        {'Content-Type':'application/json','X-Studio-Client':source,'X-Studio-Key':service_key})
    if status!=200:raise HandoffError('This connection could not be checked.')
    if not isinstance(result,dict) or result.get('source')!=source or result.get('target')!=target or result.get('capability')!='system.probe':
        raise HandoffError('Invalid connection response.')
    connection=result.get('connection')
    if not isinstance(connection,dict) or connection.get('state') not in {'healthy','reachable','unknown','unavailable'}:
        raise HandoffError('Invalid connection response.')
    return {k:connection.get(k) for k in ['state','reason','latency_ms','checked_at','cached']}


def request(url,body,headers):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(urllib.request.Request(url,data=body,headers=headers,method='POST'),timeout=25) as response:
            if response.headers.get('Content-Encoding','identity')!='identity':
                raise OutcomeUnconfirmed('The receiver response could not be verified.')
            data=response.read(16385)
            if len(data)>16384:raise OutcomeUnconfirmed('The receiver response could not be verified.')
            return response.status,json.loads(data)
    except urllib.error.HTTPError as error:
        return error.code,error_detail(error)
    except (OSError,ValueError,UnicodeError):
        raise OutcomeUnconfirmed('The handoff result is unconfirmed; retain the request key.') from None


def error_detail(error):
    """A rejected call's small JSON body, read only for its stable ``detail.code``;
    its text is never surfaced. Anything unreadable counts as no detail."""
    try:
        if error.headers.get('Content-Encoding','identity')!='identity':return None
        data=error.read(16385)
        if len(data)>16384:return None
        detail=json.loads(data)
        return detail if isinstance(detail,dict) else None
    except (OSError,ValueError,UnicodeError,AttributeError):
        return None


def handoff_pdf(source,service_key,session_token,request_key,name,pdf,*,transport=request):
    if source not in SOURCES or not isinstance(service_key,str) or len(service_key)<32:
        raise HandoffError('A registered source application is required.')
    if not isinstance(session_token,str) or not re.fullmatch(r'bs1_[A-Za-z0-9_-]{16,196}',session_token):
        raise HandoffError('A current application session is required.')
    if not isinstance(request_key,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{15,127}',request_key):
        raise HandoffError('A stable request key is required.')
    if not isinstance(name,str) or not 1<=len(name)<=240 or name!=name.strip() or any(ord(c)<32 or ord(c)==127 or c in '/\\' for c in name):
        raise HandoffError('A valid file name is required.')
    if not isinstance(pdf,bytes) or not pdf.startswith(b'%PDF-') or not 0<len(pdf)<=32*1024*1024:
        raise HandoffError('A PDF of at most 32 MB is required.')
    checksum=hashlib.sha256(pdf).hexdigest()
    body={'target':'share','capability':'artifact.store.private','session_token':session_token,
          'scope':{'request_key':request_key,'name':name,'mime_type':'application/pdf','size':len(pdf),'sha256':checksum}}
    status,grant=transport(SYSTEMS,json.dumps(body,separators=(',',':')).encode(),
        {'Content-Type':'application/json','X-Studio-Client':source,'X-Studio-Key':service_key})
    if status==409 and isinstance(grant,dict) and isinstance(grant.get('detail'),dict) and grant['detail'].get('code')=='recipient_unlinked':
        raise HandoffError('Sign in to Share once with this account so it can receive files.',reason='share_link_required')
    if status!=201:raise HandoffError('Systems did not authorize this file handoff.')
    if (not isinstance(grant,dict) or not isinstance(grant.get('token'),str)
            or not re.fullmatch(r'bsc1_[A-Za-z0-9_-]{43}',grant['token'])
            or type(grant.get('expires_at')) is not int or not time.time()<grant['expires_at']<=time.time()+MAX_GRANT_AHEAD):
        raise HandoffError('Invalid handoff authorization.')
    headers={'Content-Type':'application/pdf','Origin':'https://buildstudio-share.com',
             'X-Studio-Delegation':grant['token']}
    try:
        status,result=transport(SHARE,pdf,headers)
    except OutcomeUnconfirmed:
        status,result=503,None
    if status not in (200,201,401,403,409,410,413,422,429):
        # Read-only reconciliation is safe after a lost COMMIT response. A 404
        # cannot prove no in-flight commit, so never automatically re-upload.
        status,result=transport(SHARE+'/receipt',b'{}',{**headers,'Content-Type':'application/json'})
        if status!=200:raise OutcomeUnconfirmed('The handoff result is unconfirmed; retry later with the same request key.')
    elif status==413:
        raise HandoffError('Share declined the file: it exceeds the size limit or your remaining quota.',reason='share_quota')
    elif status not in (200,201):
        raise HandoffError('Share rejected this handoff; check authorization, quota and request key.')
    file=result.get('file') if isinstance(result,dict) else None
    try:
        if not isinstance(file,dict) or str(uuid.UUID(file['id']))!=file['id'] or file['name']!=name or result['checksumSha256']!=checksum:
            raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        raise OutcomeUnconfirmed('The file receipt could not be verified.') from None
    return {'file_id':file['id'],'name':name,'sha256':checksum,'url':'https://buildstudio-share.com'}
