"""Keep There's own login UI and data IDs while consulting Systems for staff access."""
import asyncio
import json
import os
import re
from http.cookies import SimpleCookie

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from open_webui import studio_identity as studio

ORIGIN='https://buildstudio-there.com'

def token_from(request):
    authorization=request.headers.get('authorization','')
    if authorization.lower().startswith('bearer '): return authorization[7:]
    return request.cookies.get('token','') or request.headers.get('x-api-key','')

async def resolve_identity(identity):
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.auths import Auth
    from open_webui.models.users import Users
    from sqlalchemy.exc import IntegrityError
    user=await Users.get_user_by_id(identity['external_id'])
    if not user:
        if await Users.get_user_by_email(identity['login_alias']):
            raise HTTPException(409,'This login belongs to another identity. Review it in Systems management.')
        try:
            async with get_async_db_context() as db:
                db.add(Auth(id=identity['external_id'],email=identity['login_alias'],password='!systems-managed!',active=False))
                user=await Users.insert_new_user(identity['external_id'],identity['display_name'] or identity['login'],identity['login_alias'],role=identity['role'],db=db)
        except IntegrityError:
            user=await Users.get_user_by_id(identity['external_id'])
            if not user: raise HTTPException(409,'An account mapping conflict needs administrator review.') from None
    if user.role!=identity['role']:
        user=await Users.update_user_role_by_id(user.id,identity['role'])
    return user.model_copy(update={'role':identity['role'],'name':identity['display_name'] or user.name})

async def user_by_token(token):
    return await resolve_identity(await studio.check(token))

async def response_for(identity,token):
    from open_webui.models.config import Config
    from open_webui.utils.access_control import get_permissions
    user=await resolve_identity(identity)
    result={'token':token,'token_type':'Bearer','expires_at':identity['expires_at'],'id':user.id,'name':user.name,'email':user.email,'role':user.role,'profile_image_url':user.profile_image_url,'permissions':await get_permissions(user.id,await Config.get('user.permissions'))}
    response=JSONResponse(result,headers={'Cache-Control':'no-store'})
    response.set_cookie('token',token,max_age=28800,path='/',secure=True,httponly=True,samesite='strict')
    return response

class StudioMiddleware:
    def __init__(self,app): self.app=app

    async def protected_http(self,scope,receive,send,token):
        state={'started':False,'finished':False}
        async def tracked(message):
            if message['type']=='http.response.start': state['started']=True
            if message['type']=='http.response.body' and not message.get('more_body',False): state['finished']=True
            await send(message)
        task=asyncio.create_task(self.app(scope,receive,tracked))
        async def watch():
            while not task.done():
                await asyncio.sleep(3)
                try: await studio.check(token)
                except HTTPException:
                    if task.done(): return
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                    try:
                        if not state['started']:
                            await JSONResponse({'detail':'Your session has ended.'},status_code=401)(scope,receive,send)
                        elif not state['finished']:
                            await send({'type':'http.response.body','body':b'','more_body':False})
                    except Exception:
                        # A response with a fixed Content-Length closes as an incomplete transfer.
                        pass
                    return
        watcher=asyncio.create_task(watch())
        try:
            done,_=await asyncio.wait([task,watcher],return_when=asyncio.FIRST_COMPLETED)
            if task in done and not task.cancelled(): await task
            else: await watcher
        finally:
            for pending in (task,watcher):
                if not pending.done(): pending.cancel()
            await asyncio.gather(task,watcher,return_exceptions=True)

    async def __call__(self,scope,receive,send):
        if not studio.ENABLED or scope['type']!='http':
            return await self.app(scope,receive,send)
        request=Request(scope,receive)
        path=scope['path'].rstrip('/')
        method=scope['method']
        try:
            if method=='GET' and path in ('/admin/users','/admin/users/create'):
                return await RedirectResponse('https://buildstudio-systems.com/admin/',status_code=303)(scope,receive,send)
            # Close alternative human identity issuance and native staff-management writes.
            blocked=path in ('/api/v1/auths/add','/api/v1/auths/signup','/api/v1/auths/ldap','/api/v1/auths/api_key') or bool(re.fullmatch(r'/api/v1/users/[^/]+/update',path)) or (method=='DELETE' and bool(re.fullmatch(r'/api/v1/users/[^/]+',path))) or path.endswith('/token/exchange') or path.startswith('/api/v1/scim')
            if blocked and method not in ('GET','HEAD','OPTIONS'):
                raise HTTPException(403,'Staff accounts are managed at https://buildstudio-systems.com/admin/')
            if path in ('/api/v1/auths/signin','/api/v1/auths/signout','/api/v1/auths/update/password') and method=='POST':
                if request.headers.get('origin')!=ORIGIN: raise HTTPException(403,'Sign in from the There website.')
                if path.endswith('/signout'):
                    token=token_from(request)
                    if token.startswith('bs1_'): await studio.logout(token)
                    response=JSONResponse({'status':True});response.delete_cookie('token',path='/',secure=True,httponly=True,samesite='strict')
                    return await response(scope,receive,send)
                body=b''
                async for chunk in request.stream():
                    body+=chunk
                    if len(body)>4096: raise HTTPException(413,'Request too large.')
                try: data=json.loads(body)
                except (ValueError,TypeError): raise HTTPException(422,'Invalid login request.') from None
                if not isinstance(data,dict): raise HTTPException(422,'Invalid login request.')
                if path.endswith('/signin'):
                    identity=await studio.call('login',{'login':data.get('email',''),'password':data.get('password','')},request.client.host if request.client else 'unknown')
                    response=await response_for(identity,identity['token'])
                else:
                    await studio.call('password',{'token':token_from(request),'current_password':data.get('password',''),'password':data.get('new_password','')})
                    response=JSONResponse(True);response.delete_cookie('token',path='/',secure=True,httponly=True,samesite='strict')
                return await response(scope,receive,send)
        except HTTPException as error:
            return await JSONResponse({'detail':error.detail},status_code=error.status_code,headers={'Cache-Control':'no-store'})(scope,receive,send)
        token=token_from(request)
        if token.startswith('bs1_') and path.startswith(('/api/','/openai/','/ollama/')):
            return await self.protected_http(scope,receive,send,token)
        return await self.app(scope,receive,send)
