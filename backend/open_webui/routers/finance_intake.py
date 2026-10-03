"""Exact private tool route; browser session cookies grant no authority here."""
import json
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from open_webui.utils.finance_intake import native_invoke, vault
from open_webui.utils.finance_intake_store import IntakeError

router = APIRouter()
PRIVATE_DEADLINE = 50


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate JSON field')
        result[key]=value
    return result


def reject_constant(value): raise ValueError('non-finite JSON value')


@router.post('/tool')
async def tool(request: Request):
    headers = {'Cache-Control':'no-store','Referrer-Policy':'no-referrer'}
    # Public reverse proxies keep the canonical domain Host. The nonce remains
    # mandatory even for direct loopback clients; no ambient cookies or device key.
    if (not request.client or request.client.host not in {'127.0.0.1','::1'}
            or request.headers.get('host')!='127.0.0.1:3000'):
        return JSONResponse({'error':'private_tool_route'},status_code=403,headers=headers)
    auth = request.headers.get('authorization','')
    if not auth.startswith('Bearer ') or request.headers.get('transfer-encoding'):
        return JSONResponse({'error':'business_context_required'},status_code=403,headers=headers)
    try:
        vault.get(auth[7:])
        length=request.headers.get('content-length','')
        if not length.isascii() or not length.isdigit() or not 0<int(length)<=60000:
            raise IntakeError('invalid_content_length',413)
        async with asyncio.timeout(PRIVATE_DEADLINE):
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>60000: raise IntakeError('payload_too_large',413)
            if len(raw)!=int(length): raise IntakeError('invalid_content_length',422)
            try: body=json.loads(raw,object_pairs_hook=unique_object,parse_constant=reject_constant)
            except (ValueError,UnicodeError): raise IntakeError('invalid_business_json',422) from None
            result=await native_invoke(auth[7:],body)
        return JSONResponse(result,headers=headers)
    except IntakeError as error:
        return JSONResponse({'status':'unconfirmed' if error.code=='finance_result_unconfirmed' else 'rejected',
            'error':error.code,'claims':[]},status_code=error.status,headers=headers)
    except Exception:
        return JSONResponse({'status':'unconfirmed','error':'finance_result_unconfirmed','claims':[]},status_code=503,headers=headers)
