"""Owner-only expense intake. Authority stays in Web, outside model arguments."""
import hashlib
import asyncio
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import httpx

from open_webui.utils.finance_intake_store import (IntakeError, IntakeStore, canonical,
                                                 canonical_uuid, request_key)

HEADER = 'X-BuildStudio-Business-Context'
PRIVATE_URL = 'http://127.0.0.1:3000/api/v1/finance-intake/tool'
FINANCE = 'https://buildstudio-demo.com/api/integrations/there/expense-drafts'
IDENTITY = 'https://buildstudio-systems.com/api/v1/identity/'
CREATE = 'finance.expense.draft.create'
CONTEXT = 'finance.expense.draft.context'
IDENTITY_DEADLINE = 5
FINANCE_DEADLINE = 15
_AMOUNT = re.compile(r'(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,2})?\Z')
_SESSION = re.compile(r'bs1_[A-Za-z0-9_-]{16,196}\Z')


@dataclass(frozen=True)
class ToolContext:
    owner: str
    intake_id: str
    session: str = field(repr=False)
    deadline: float
    current_text: str = field(repr=False)


class ContextVault:
    """Process-local, bounded authority. Restart requires a new real login request."""
    def __init__(self, clock=time.monotonic, capacity=512):
        self.clock, self.capacity = clock, capacity
        self.rows, self.lock = {}, threading.Lock()

    def issue(self, owner, intake, session, expires_at, current_text):
        remaining = min(300, expires_at - int(time.time()))
        if remaining <= 0: raise IntakeError('source_session_expired', 403)
        with self.lock:
            now = self.clock()
            self.rows = {k: v for k, v in self.rows.items() if v.deadline > now}
            if len(self.rows) >= self.capacity: raise IntakeError('business_context_busy', 429)
            nonce = secrets.token_urlsafe(32)
            self.rows[hashlib.sha256(nonce.encode()).hexdigest()] = ToolContext(
                owner, intake, session, now + remaining, current_text)
        return nonce

    def get(self, nonce):
        if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', nonce):
            raise IntakeError('business_context_denied', 403)
        with self.lock:
            row = self.rows.get(hashlib.sha256(nonce.encode()).hexdigest())
            if not row or row.deadline <= self.clock():
                raise IntakeError('business_context_expired', 403)
            return row


class FinanceClient:
    def __init__(self, central_call, transport=None):
        self.central = central_call
        self.transport = transport

    async def central_request(self, action, body):
        try:
            async with asyncio.timeout(IDENTITY_DEADLINE): return await self.central(action,body)
        except TimeoutError: raise IntakeError('source_identity_unavailable',503) from None

    async def fresh_source(self, session, owner):
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            raise IntakeError('source_session_required', 403)
        identity = await self.central_request('check', {'token': session})
        if (not isinstance(identity, dict) or identity.get('external_id') != owner
                or identity.get('role') != 'admin'
                or type(identity.get('expires_at')) is not int
                or identity['expires_at'] <= int(time.time())):
            raise IntakeError('source_identity_denied', 403)
        return identity

    async def call(self, session, capability, scope, suffix, body):
        grant = await self.central_request('coordination/finance/expense-delegate', {
            'session_token': session, 'capability': capability, 'scope': scope})
        token = grant.get('token') if isinstance(grant, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r'bsc1_[A-Za-z0-9_-]{43}', token):
            raise IntakeError('delegation_unavailable', 503)
        try:
            async with asyncio.timeout(FINANCE_DEADLINE), httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=15, transport=self.transport) as client:
                async with client.stream('POST', FINANCE + suffix,
                        content=canonical(body), headers={
                            'Content-Type': 'application/json', 'X-Studio-Delegation': token}) as response:
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 60000: raise IntakeError('finance_response_invalid', 503)
                    if 300 <= response.status_code < 400:
                        raise IntakeError('finance_redirect_rejected', 503)
                    try: value = json.loads(raw)
                    except (ValueError, UnicodeError): raise IntakeError('finance_response_invalid', 503) from None
                    if not isinstance(value, dict): raise IntakeError('finance_response_invalid', 503)
                    if response.status_code == 422 and value.get('status') == 'needs_input':
                        return {'status': 'needs_input', 'fields': _fields(value.get('fields')), 'claims': []}
                    if response.status_code != 200:
                        raise IntakeError('finance_request_rejected', response.status_code if response.status_code in (403,409,429) else 503)
                    return value
        except (httpx.HTTPError,TimeoutError):
            raise IntakeError('finance_result_unconfirmed', 503) from None


def _fields(value):
    if not isinstance(value, list): return ['required_fields']
    return [x for x in value[:40] if isinstance(x, str)
            and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', x)] or ['required_fields']


async def native_identity(action,body):
    """Private typed calls use cancellable async transport, never the login cache."""
    if action not in {'check','coordination/finance/expense-delegate'}:
        raise IntakeError('business_action_denied',403)
    if os.environ.get('STUDIO_IDENTITY_APP')!='there' or not os.environ.get('STUDIO_IDENTITY_KEY'):
        raise IntakeError('source_identity_unavailable',503)
    try:
        async with asyncio.timeout(IDENTITY_DEADLINE),httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=5) as client:
            async with client.stream('POST',IDENTITY+action,content=canonical(body),headers={
                    'Content-Type':'application/json','X-Studio-Client':'there',
                    'X-Studio-Key':os.environ['STUDIO_IDENTITY_KEY']}) as response:
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>16384: raise IntakeError('source_identity_unavailable',503)
                if response.status_code!=(201 if action=='coordination/finance/expense-delegate' else 200):
                    raise IntakeError('source_identity_denied' if response.status_code in (401,403) else 'source_identity_unavailable',
                                      403 if response.status_code in (401,403) else 503)
                value=json.loads(raw)
                if not isinstance(value,dict): raise ValueError
                return value
    except IntakeError: raise
    except (httpx.HTTPError,TimeoutError,ValueError,UnicodeError):
        raise IntakeError('source_identity_unavailable',503) from None


def explicit_currencies(text):
    return [code for code,pattern in {
        'JPY':r'日元|円|(?<![A-Za-z])JPY(?![A-Za-z])|\byen\b',
        'CNY':r'人民币|人民幣|(?<![A-Za-z])(?:CNY|RMB)(?![A-Za-z])',
        'USD':r'美元|(?<![A-Za-z])USD(?![A-Za-z])|\bUS\s*dollars?\b',
        'EUR':r'欧元|歐元|(?<![A-Za-z])EUR(?![A-Za-z])',
        'HKD':r'港元|港币|(?<![A-Za-z])HKD(?![A-Za-z])',
        'TWD':r'台币|臺幣|(?<![A-Za-z])TWD(?![A-Za-z])',
        'KRW':r'韩元|韓元|(?<![A-Za-z])KRW(?![A-Za-z])',
        'SGD':r'新加坡元|(?<![A-Za-z])SGD(?![A-Za-z])'}.items() if re.search(pattern,text,re.I)]


def expense_list_residue(text):
    """Bounded positive grammar for direct expense lists or paid/spent statements."""
    return bool(re.fullmatch(r'(?:[\s,，、;；:：。.!！/|&]+|'
        r'(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])|'
        r'今天|今日|本日|本人|我已|我|私は|已支付|支付了|付了|花了|买了|购买了|支払いました|支払った|払いました|払った|使った|購入しました|買いました|'
        r'金额|金額|费用|費用|开销|支出|経費|登记|记录|記錄|录入|登録|記録|和|及|と|请|お願い|'
        r'日元|円|人民币|人民幣|美元|欧元|歐元|港元|港币|台币|臺幣|韩元|韓元|新加坡元|'
        r'\b(?:today|I|my|paid|spent|bought|amount|cost|expense|expenses|register|record|save|please|and|JPY|CNY|RMB|USD|EUR|HKD|TWD|KRW|SGD|yen)\b)*',text,re.I))


def direct_expense_message(text,definitions=None):
    """Positive permission is parsed from immutable text, before model fields.

    First phase: date/purpose/amount lists, optionally prefixed by 'I paid'.
    Custom fields use a separate semicolon label=value suffix. Unsupported
    prose, questions and third-person/approximate statements have no authority.
    """
    if not isinstance(text,str) or not text.strip() or len(text)>40000: return None
    if re.search(r'\b(?:will|plan|plans|planning|planned|intend|intends|intending|unpaid|yesterday)\b|\bnext\s+(?:day|week|month|year)\b|haven[’\']t|hasn[’\']t|not\s+(?:yet\s+)?(?:paid|spent)|明後日|来週|来月|翌日|明后天|明後天|后天|後天|下周|下週|下个月|下個月|下月|上个月|上個月|上月|先月',text,re.I): return None
    if re.search(r'[?？“「『>]|```|翻译|翻譯|翻訳|translate|总结|總結|要約|summari[sz]e|引用|quote|预算|預算|budget|价格|價格|price\b|贵吗|多少钱|いくら|how much|朋友|同事|他说|他說|她说|he said|she said|大概|约莫|大约|大約|approximately|approx|about\b|maybe|假设|假設|suppose|imaginary|do not|don[’\']t|不要|先别|先別|暂不|暫不|登録しない|記録しない|refund|退款|返金|取消|撤销|cancel|昨天|昨日|前天|一昨日|上周|先週|last\s|tomorrow|明天|明日',text,re.I): return None
    # Commas inside decimal grouping stay within a money token.
    text=text.strip().rstrip('。.!！')
    registration=re.match(r'^(?:登记开销|记录开销|登记支出|経費登録|経費を登録|record expenses|register expenses)\s*[:：]?\s*',text,re.I)
    explicit_registration=registration is not None
    if registration:text=text[registration.end():]
    fragments=re.split(r'[，、;；\n]|,(?![0-9])',text)
    expenses=[]
    labels=None if definitions is None else {d.get('label') or d.get('key') for d in definitions if isinstance(d,dict)}
    currency=r'(?:日元|円|人民币|人民幣|美元|欧元|歐元|港元|港币|台币|臺幣|韩元|韓元|新加坡元|JPY|CNY|RMB|USD|EUR|HKD|TWD|KRW|SGD|yen)'
    for fragment in fragments:
        segment=fragment.strip()
        if not segment: return None
        custom=re.fullmatch(r'([^0-9=:：]{1,60})\s*[=：:]\s*(.{1,2000})',segment)
        if custom and expenses:
            if labels is not None and custom[1].strip() not in labels: return None
            continue
        segment=re.sub(r'^(?:今天|今日|本日|today\b|[0-9]{4}-[0-9]{2}-[0-9]{2})\s*','',segment,flags=re.I)
        segment=re.sub(r'^(?:JPY|CNY|RMB|USD|EUR|HKD|TWD|KRW|SGD)\s+','',segment,flags=re.I)
        already_paid=bool(re.match(r'^(?:本人|我|私は|I\b)',segment,re.I) and re.search(r'已支付|支付了|花了|付了|\bpaid\b|\bspent\b|支払いました',segment,re.I))
        segment=re.sub(r'^(?:本人|我|私は|I\b)\s*(?:已支付|支付了|花了|付了|paid\b|spent\b|支払いました)?\s*','',segment,flags=re.I)
        matched=re.fullmatch(r'(?P<purpose>[^0-9,:：=;；。.!！?？]{1,60}?)\s*(?:(?:金额|金額|费用|費用|花了|amount|cost)\s*[:：=]?\s*)?'
            r'(?P<amount>(?:0|[1-9][0-9]{0,13})(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?P<currency>'+currency+r')?\s*',segment,re.I)
        if not matched: return None
        purpose=matched['purpose'].strip()
        if (not purpose or re.search(r'支付|付了|paid\b|spent\b|问|問|告诉|告訴|解释|解釋|what\b|how\b|why\b|can\b|could\b|tell\b|explain\b',purpose,re.I)
                or purpose.lower() in {'金额','金額','amount','cost','JPY'.lower(),'CNY'.lower(),'USD'.lower()}): return None
        familiar=purpose.lower() in {
            '午餐','午饭','晚餐','早餐','咖啡','电车','交通','出租车','公交','购物','办公用品','住宿','酒店','餐饮','餐费','打车','停车费','火车','机票','书籍','笔记本','手机话费','快递费','油费',
            '昼食','夕食','朝食','電車','交通費','タクシー','バス','買い物','コーヒー','宿泊','ホテル','書籍','文具','駐車場','燃料',
            'lunch','dinner','breakfast','coffee','train','transport','transportation','taxi','bus','shopping','office supplies','hotel','lodging','meal','meals','parking','fuel','books'}
        if not (familiar or explicit_registration or already_paid): return None
        if Decimal(matched['amount'].replace(',',''))<=0: return None
        expenses.append({'purpose':purpose,'amount':matched['amount'],'fragment':fragment})
    return expenses if 1<=len(expenses)<=20 else None


def expense_candidate(text):
    # Routing is server-derived. No normal question, translation or greeting
    # receives a nonce; Finance context supplies exact custom-field definitions.
    return direct_expense_message(text) is not None


def normalize_items(value, context, created_at, current_text):
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise IntakeError('invalid_item_count', 422)
    try:
        today = datetime.fromtimestamp(created_at, timezone.utc).astimezone(ZoneInfo(context['timezone'])).date().isoformat()
    except (KeyError, ValueError, TypeError): raise IntakeError('binding_timezone_invalid', 503) from None
    missing, result, used_spans = [], [], []
    parsed=direct_expense_message(current_text,context.get('required_fields',[]))
    if parsed is None: return {'status':'needs_input','fields':['registration_intent'],'claims':[]}
    if len(parsed)!=len(value): return {'status':'needs_input','fields':['complete_expense_batch'],'claims':[]}
    if re.search(r'(?:不要|不必|别|別|勿|无需|無需|暂不|暫不|先不)(?:再|先|帮我|幫我)?\s*(?:登记|登記|记录|記錄|录入|記入)|do not|don[’\']t|not (?:record|register)|(?:示例|例如|比如)|\bexample\b|登録しない|記録しない|登録しないで|記録しないで',current_text,re.I):
        return {'status':'needs_input','fields':['registration_intent'],'claims':[]}
    if re.search(r'翻译|翻譯|訳して|翻訳|translate|总结|總結|要約|summari[sz]e|引用|quoted?\b|预算|預算|budget|价格|價格|price\b|多少钱|多少錢|いくら|how much|他说|他說|她说|她說|he said|she said|假设|假設|imaginary|suppose|计划|計画|我要|will spend|[“「『]|^\s*>|```',current_text,re.I):
        return {'status':'needs_input','fields':['registration_intent'],'claims':[]}
    if re.search(r'(?<![0-9])[−-]\s*[0-9]|退款|退费|退費|返金|refund|撤销|撤銷|取消登记|cancel|取り消',current_text,re.I):
        return {'status':'needs_input','fields':['expense_intent'],'claims':[]}
    source_currencies=explicit_currencies(current_text)
    source_dates=list(dict.fromkeys(re.findall(r'(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])',current_text)))
    relative_date=bool(re.search(r'昨天|昨日|前天|一昨日|上周|上週|上月|先週|先月|yesterday|tomorrow|明天|明日|\blast\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\d{1,2}[/月]\d{1,2}',current_text,re.I))
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or set(raw) - {'source_quote','title','expense_date','currency','gross_amount','memo','category','tax','extra'}:
            raise IntakeError('invalid_expense_fields', 422)
        quote = raw.get('source_quote')
        if not isinstance(quote,str) or not quote or len(quote)>2000 or quote not in current_text:
            missing.append(f'items.{index}.source_quote')
            quote = ''
        start = current_text.find(quote) if quote else -1
        # Repeated identical phrases have distinct literal spans. A second tool
        # line cannot repeatedly name the first expense to mint extra drafts.
        while start>=0 and any(start<end and start+len(quote)>begin for begin,end in used_spans):
            start=current_text.find(quote,start+1)
        if start<0: missing.append(f'items.{index}.source_quote')
        else: used_spans.append((start,start+len(quote)))
        item = {k: raw.get(k) for k in ('title','expense_date','currency','gross_amount','memo','category','tax')}
        item['line_id'], item['extra'] = index, raw.get('extra', {})
        for key, maximum, required in (('title',120,True),('memo',500,False),('category',60,False)):
            v = item[key]
            if required and (v is None or v == ''): missing.append(f'items.{index}.{key}')
            elif v is not None and (not isinstance(v, str) or len(v)>maximum):
                raise IntakeError('invalid_expense_text', 422)
            elif v is not None and v and v not in quote:
                missing.append(f'items.{index}.{key}')
        if item['title']!=parsed[index-1]['purpose']:
            missing.append(f'items.{index}.title')
        quote_dates=list(dict.fromkeys(re.findall(r'(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])',quote)))
        evidenced_dates=quote_dates or source_dates
        if item['expense_date'] in (None, 'today'):
            item['expense_date']=evidenced_dates[0] if len(evidenced_dates)==1 else today
        try:
            if date.fromisoformat(item['expense_date']).isoformat() != item['expense_date']: raise ValueError
            if date.fromisoformat(item['expense_date'])>date.fromisoformat(today): raise ValueError
        except (ValueError, TypeError): missing.append(f'items.{index}.expense_date')
        if item['expense_date']!=today and item['expense_date'] not in current_text:
            missing.append(f'items.{index}.expense_date')
        if relative_date or len(evidenced_dates)>1 or (evidenced_dates and item['expense_date']!=evidenced_dates[0]):
            missing.append(f'items.{index}.expense_date')
        quote_currencies=explicit_currencies(quote)
        evidenced_currencies=quote_currencies or source_currencies
        expected_currency=evidenced_currencies[0] if len(evidenced_currencies)==1 else context.get('default_currency')
        if item['currency'] is None: item['currency']=expected_currency
        if not isinstance(item['currency'], str) or not re.fullmatch(r'[A-Z]{3}', item['currency']):
            missing.append(f'items.{index}.currency')
        if len(evidenced_currencies)>1 or item['currency']!=expected_currency:
            missing.append(f'items.{index}.currency')
        amount = item['gross_amount']
        if not isinstance(amount, str) or not _AMOUNT.fullmatch(amount) or not any(c in '123456789' for c in amount):
            missing.append(f'items.{index}.gross_amount')
        else:
            monetary_text=re.sub(r'(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])|(?<![0-9])[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?![0-9])',' ',quote)
            monetary_text=re.sub(r'[0-9]+\s*(?:杯|個|个|件|次|人|张|枚|份|本|冊|辆|台|items?\b|tickets?\b)',' ',monetary_text)
            monetary_text=re.sub(r'(?:第\s*|\b(?:no\.?|number|shop|store)\s*)[0-9]+|[0-9]+\s*(?:号|號)',' ',monetary_text,flags=re.I)
            monetary_text=re.sub(r'^\s*[0-9]+[.)、]\s*',' ',monetary_text)
            matches=list(re.finditer(r'(?<![0-9.])[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?(?![0-9.])',monetary_text))
            numbers=[x.group() for x in matches]
            def money_evidence(match):
                before,after=monetary_text[:match.start()],monetary_text[match.end():]
                # A bare count/identifier is not a price. Accept explicit adjacent
                # currency/amount labels, or a trailing expense amount in the
                # configured currency (e.g. "lunch 1200").
                currency=r'(?:日元|円|人民币|人民幣|美元|欧元|歐元|港元|港币|台币|臺幣|韩元|韓元|新加坡元|JPY|CNY|RMB|USD|EUR|HKD|TWD|KRW|SGD|yen|US\s*dollars?)'
                marked=(re.search(currency+r'\s*$',before,re.I) or re.match(r'\s*'+currency+r'(?![A-Za-z])',after,re.I)
                        or re.search(r'(?:金额|金額|费用|費用|支出|花费|花費|amount|cost|paid|spent|金額)\s*[:：=]?\s*$',before,re.I))
                trailing=(re.fullmatch(r'\s*[。.!！]?\s*',after) and item['title'] and item['title'] in before
                          and not re.search(r'第\s*$|(?:no\.?|number|shop|store|line|item)\s*$',before,re.I))
                return bool(marked or trailing)
            money_matches=[x for x in matches if money_evidence(x)]
            if len(money_matches)!=1 or Decimal(money_matches[0].group().replace(',',''))!=Decimal(amount):
                missing.append(f'items.{index}.gross_amount')
            if re.search(r'\d\s*(?:或|或者|or|~|至|到)\s*\d',quote,re.I):
                missing.append(f'items.{index}.gross_amount')
            if len(numbers)>1 and re.search(r'单价|單價|単価|unit price|[×*]|合计|合計|total',quote,re.I):
                missing.append(f'items.{index}.gross_amount')
            if re.search(r'[0-9]+\s*(?:杯|個|个|件|份|本|冊|辆|台|items?\b|tickets?\b)',quote,re.I):
                missing.append(f'items.{index}.gross_amount')
        if not evidenced_currencies and re.search(r'[¥￥$]|(?<![日人港欧歐])元',quote):
            missing.append(f'items.{index}.currency')
        tax = item['tax']
        if tax is not None:
            # First release does not interpret deductibility or rates. Native
            # Finance can fill explicit tax information after reviewing a draft.
            missing.append(f'items.{index}.tax_review')
        extra = item['extra']
        if not isinstance(extra,dict) or len(extra)>40: raise IntakeError('invalid_custom_fields',422)
        requirements=context.get('required_fields',[])
        if not isinstance(requirements,list) or len(requirements)>40: raise IntakeError('finance_context_invalid',503)
        definitions={}
        for definition in requirements:
            if (not isinstance(definition,dict) or not isinstance(definition.get('key'),str)
                    or definition.get('type') not in {'TEXT','TEXTAREA','NUMBER','DATE','SELECT','CHECKBOX'}):
                raise IntakeError('finance_context_invalid',503)
            definitions[definition['key']]=definition
        for k, v in extra.items():
            if not isinstance(k,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',k): raise IntakeError('invalid_custom_fields',422)
            if k not in definitions: raise IntakeError('unknown_custom_field',422)
            if v is not None and type(v) is not bool and not (isinstance(v,str) and len(v)<=2000):
                raise IntakeError('invalid_custom_fields',422)
            # Model-picked options/checkboxes are not user statements. The first
            # release accepts only literal textual values in this expense quote.
            if type(v) is bool or definitions[k]['type']=='CHECKBOX':
                missing.append('manual_custom_fields')
            elif isinstance(v,str) and v and v not in quote:
                missing.append(f'items.{index}.extra.{k}')
            kind=definitions[k]['type']
            if v is not None and kind!='CHECKBOX':
                valid=isinstance(v,str)
                if kind=='TEXT': valid=valid and len(v)<=200
                elif kind=='NUMBER': valid=valid and bool(re.fullmatch(r'-?(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,4})?',v))
                elif kind=='DATE':
                    try: valid=valid and date.fromisoformat(v).isoformat()==v
                    except (ValueError,TypeError): valid=False
                elif kind=='SELECT':
                    options=definitions[k].get('options')
                    if not isinstance(options,list) or any(not isinstance(x,str) for x in options): raise IntakeError('finance_context_invalid',503)
                    valid=valid and v in options
                if not valid: missing.append(f'items.{index}.extra.{k}')
        for required in requirements:
            key=required['key']; v=extra.get(key)
            if required.get('required') is True and (v is None or v=='' or v==[]):
                missing.append('manual_custom_fields' if required['type']=='CHECKBOX' else f'items.{index}.extra.{key}')
        canonical(item)
        result.append(item)
    uncovered=current_text
    for begin,end in sorted(used_spans,reverse=True): uncovered=uncovered[:begin]+' '+uncovered[end:]
    if not expense_list_residue(uncovered): missing.append('complete_expense_batch')
    if missing: return {'status':'needs_input','fields':missing,'claims':[]}
    return result


class FinanceIntake:
    def __init__(self, store, client, vault, validate_chat):
        self.store, self.client, self.vault, self.validate_chat = store, client, vault, validate_chat

    async def invoke(self, nonce, body):
        proof = self.vault.get(nonce)
        if not isinstance(body,dict) or set(body)-{'action','items'} or body.get('action') not in {'context','create','receipt'}:
            raise IntakeError('business_action_denied',403)
        row = await self.store.get(proof.intake_id, proof.owner)
        if not await self.validate_chat(row): raise IntakeError('message_authority_revoked',403)
        await self.client.fresh_source(proof.session, proof.owner)
        base = {'intake_id':row['id'],'request_key':request_key(row)}
        if body['action'] == 'receipt':
            if 'items' in body: raise IntakeError('receipt_has_no_model_payload',422)
            if not row.get('payload_json'): return {'status':'not_recorded','claims':[]}
            payload = json.loads(row['payload_json'])
        else:
            context = await self.client.call(proof.session, CONTEXT, base, '/context', base)
            if context.get('status') == 'needs_input': return {'status':'needs_input','fields':['company_binding'],'claims':[]}
            if (context.get('status') != 'ready' or type(context.get('company_id')) is not int
                    or not 0<context['company_id']<2**63 or type(context.get('binding_revision')) is not int
                    or context['binding_revision']<=0): raise IntakeError('finance_context_invalid',503)
            canonical_uuid(context.get('binding_id'))
            if body['action']=='context':
                if 'items' in body: raise IntakeError('context_has_no_model_payload',422)
                return {k:context[k] for k in ('status','company_id','company_name','default_currency','timezone','required_fields') if k in context} | {
                    'today':datetime.fromtimestamp(row['created_at'],timezone.utc).astimezone(ZoneInfo(context['timezone'])).date().isoformat()}
            items = normalize_items(body.get('items'), context, row['created_at'], proof.current_text)
            if isinstance(items,dict):
                if row.get('payload_sha256'): raise IntakeError('immutable_batch_conflict')
                return items
            payload = base | {'binding_id':context['binding_id'],'binding_revision':context['binding_revision'],'items':items}
            await self.store.bind_payload(row['id'],proof.owner,payload)
        scope = base | {'binding_id':payload['binding_id'],'binding_revision':payload['binding_revision'],
            'company_id':context['company_id'] if body['action']!='receipt' else await self._receipt_company(proof,base,payload),
            'payload_sha256':hashlib.sha256(canonical(payload)).hexdigest(),'item_count':len(payload['items'])}
        # Recheck after database/CAS waits. Finance independently checks before native commit.
        self.vault.get(nonce)
        if not await self.validate_chat(row): raise IntakeError('message_authority_revoked',403)
        await self.client.fresh_source(proof.session,proof.owner)
        result = await self.client.call(proof.session, CREATE, scope,
            '/receipt' if body['action']=='receipt' else '',payload)
        if result.get('status')=='needs_input': return result
        if (result.get('status')!='completed' or result.get('intake_id')!=row['id']
                or result.get('request_key')!=base['request_key'] or not isinstance(result.get('claims'),list)
                or len(result['claims'])!=len(payload['items']) or type(result.get('company_id')) is not int
                or result['company_id']!=scope['company_id'] or not isinstance(result.get('company_name'),str)
                or not result['company_name'] or len(result['company_name'])>200): raise IntakeError('finance_receipt_invalid',503)
        clean = []
        for index, claim in enumerate(result['claims'],1):
            if (not isinstance(claim,dict) or type(claim.get('id')) is not int or claim['id']<=0
                    or type(claim.get('line_id')) is not int or claim['line_id']!=index
                    or claim.get('status') not in {'DRAFT','SUBMITTED','REVIEWED','APPROVED','REJECTED','PAID','DELETED'}):
                raise IntakeError('finance_receipt_invalid',503)
            fields=('title','expense_date','currency','gross_amount')
            if claim['status']=='DELETED':
                if any(claim.get(k) is not None for k in fields): raise IntakeError('finance_receipt_invalid',503)
            else:
                try:
                    if (not isinstance(claim.get('title'),str) or len(claim['title'])>120
                            or not isinstance(claim.get('currency'),str) or not re.fullmatch(r'[A-Z]{3}',claim['currency'])
                            or not isinstance(claim.get('gross_amount'),str) or not _AMOUNT.fullmatch(claim['gross_amount'])
                            or date.fromisoformat(claim['expense_date']).isoformat()!=claim['expense_date']):
                        raise ValueError
                except (ValueError,TypeError,KeyError): raise IntakeError('finance_receipt_invalid',503) from None
            clean.append({k:claim.get(k) for k in ('id','line_id','status',*fields)})
        receipt = base | {'status':'completed','company_id':scope['company_id'],'company_name':result['company_name'],
            'duplicate':result.get('duplicate') is True,'claims':clean}
        await self.store.save_receipt(row['id'],proof.owner,receipt)
        return receipt

    async def _receipt_company(self, proof, base, payload):
        current = await self.client.call(proof.session,CONTEXT,base,'/context',base)
        if (current.get('status')!='ready' or current.get('binding_id')!=payload['binding_id']
                or current.get('binding_revision')!=payload['binding_revision']
                or type(current.get('company_id')) is not int or not 0<current['company_id']<2**63):
            raise IntakeError('finance_binding_changed',409)
        return current['company_id']


vault = ContextVault()


def native_store():
    from open_webui.internal.db import get_async_db_context
    return IntakeStore(get_async_db_context)


async def settings():
    # A process-local raw-session vault needs deterministic loopback routing.
    # Cross-worker support must use a separate private authority service.
    if os.environ.get('UVICORN_WORKERS','1')!='1': return None
    from open_webui.models.config import Config
    if await Config.get('finance_intake.enabled',False) is not True: return None
    owners = await Config.get('finance_intake.owners',[])
    return set(owners) if isinstance(owners,list) and all(isinstance(x,str) for x in owners) else None


async def reserve_message(request, user, metadata, is_new_chat):
    """Business failure never changes ordinary chat editing permissions."""
    owners = await settings()
    from open_webui.utils.device_control import session_from_request
    session = session_from_request(request)
    message = metadata.get('user_message')
    if (not owners or user.id not in owners or user.role!='admin' or not _SESSION.fullmatch(session)
            or metadata.get('internal') or metadata.get('automation_id') or metadata.get('task')
            or not isinstance(message,dict) or message.get('role')!='user'
            or (not is_new_chat and not re.fullmatch(r'[a-f0-9-]{36}',metadata.get('chat_id','')))):
        return None
    if not expense_candidate(message.get('content')): return None
    client = FinanceClient(native_identity)
    try:
        await client.fresh_source(session,user.id)
        row = await native_store().reserve(user.id,message.get('id'),message.get('content'),
                                          None if is_new_chat else metadata.get('chat_id'))
        metadata['_finance_intake_id'] = row['id']
        return row
    except Exception:
        metadata['_finance_intake_id'] = None
        return None


async def validate_native_chat(row):
    from open_webui.models.chats import Chats
    if not await Chats.is_chat_owner(row['chat_id'],row['owner_id']): return False
    message = await Chats.get_message_by_id_and_message_id(row['chat_id'],row['message_id'])
    return bool(isinstance(message,dict) and message.get('role')=='user'
        and isinstance(message.get('content'),str)
        and hashlib.sha256(message['content'].encode()).hexdigest()==row['text_sha256'])


async def context_header(user,metadata,session):
    ident = metadata.get('_finance_intake_id') if isinstance(metadata,dict) else None
    if not ident or user is None or user.role!='admin': return ''
    owners = await settings()
    if not owners or user.id not in owners: return ''
    row = await native_store().get(ident,user.id)
    message=metadata.get('user_message')
    if (row['chat_id']!=metadata.get('chat_id') or not isinstance(message,dict)
            or message.get('id')!=row['message_id'] or message.get('role')!='user'
            or not isinstance(message.get('content'),str)
            or hashlib.sha256(message['content'].encode()).hexdigest()!=row['text_sha256']
            or not await validate_native_chat(row)): return ''
    identity = await FinanceClient(native_identity).fresh_source(session,user.id)
    return vault.issue(user.id,row['id'],session,identity['expires_at'],metadata['user_message']['content'])


async def native_invoke(nonce,body):
    owners = await settings()
    proof = vault.get(nonce)
    if not owners or proof.owner not in owners: raise IntakeError('business_intake_disabled',403)
    return await FinanceIntake(native_store(),FinanceClient(native_identity),vault,validate_native_chat).invoke(nonce,body)
