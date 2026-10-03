"""Immutable expense-message authority. No user sessions or raw chat text at rest."""
from contextlib import asynccontextmanager
import hashlib
import json
import time
import uuid

from sqlalchemy import (Table, MetaData, Column, Text, BigInteger, UniqueConstraint,
                        select, update, text)

metadata = MetaData()
intakes = Table('there_finance_intake', metadata,
    Column('id', Text, primary_key=True), Column('owner_id', Text, nullable=False),
    Column('message_id', Text, nullable=False), Column('chat_id', Text, nullable=False),
    Column('text_sha256', Text, nullable=False), Column('created_at', BigInteger, nullable=False),
    Column('payload_sha256', Text), Column('payload_json', Text), Column('receipt_json', Text),
    UniqueConstraint('owner_id', 'message_id', name='uq_there_finance_message'))


class IntakeError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value):
    def valid(v):
        if v is None or type(v) in (bool, int): return
        if isinstance(v, str):
            v.encode('utf-8', errors='strict')
            return
        if isinstance(v, list):
            for x in v: valid(x)
            return
        if isinstance(v, dict):
            if any(not isinstance(k, str) or not k.isascii() for k in v):
                raise IntakeError('invalid_object_key', 422)
            for x in v.values(): valid(x)
            return
        raise IntakeError('invalid_numeric_type', 422)
    try:
        valid(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except UnicodeError:
        raise IntakeError('invalid_unicode', 422) from None


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value: raise ValueError
    except (ValueError, AttributeError):
        raise IntakeError('invalid_identity', 422) from None
    return value


def request_key(row):
    return 'there-expense:' + hashlib.sha256(canonical([
        'there-finance-v1', row['owner_id'], row['chat_id'], row['message_id'], row['id']
    ])).hexdigest()


class IntakeStore:
    def __init__(self, session_provider):
        self.sessions = session_provider

    @asynccontextmanager
    async def transaction(self, lock_key):
        async with self.sessions() as db:
            async with db.begin():
                if db.bind.dialect.name == 'postgresql':
                    await db.execute(text("SET LOCAL lock_timeout='3s'"))
                    key = int.from_bytes(hashlib.sha256(lock_key.encode()).digest()[:8], 'big', signed=True)
                    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                yield db

    async def reserve(self, owner, message, content, chat_id=None):
        canonical_uuid(owner); canonical_uuid(message)
        if chat_id is not None: canonical_uuid(chat_id)
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 40000:
            raise IntakeError('invalid_current_user_message', 422)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        async with self.transaction(owner + ':' + message) as db:
            row = (await db.execute(select(intakes).where(
                intakes.c.owner_id == owner, intakes.c.message_id == message))).mappings().first()
            if row:
                if row['text_sha256'] != digest or (chat_id and row['chat_id'] != chat_id):
                    raise IntakeError('immutable_message_conflict')
                return dict(row)
            value = dict(id=str(uuid.uuid4()), owner_id=owner, message_id=message,
                chat_id=chat_id or str(uuid.uuid4()), text_sha256=digest, created_at=int(time.time()))
            await db.execute(intakes.insert().values(**value))
            return value

    async def get(self, ident, owner):
        async with self.sessions() as db:
            row = (await db.execute(select(intakes).where(
                intakes.c.id == ident, intakes.c.owner_id == owner))).mappings().first()
        if not row: raise IntakeError('intake_unavailable', 403)
        return dict(row)

    async def bind_payload(self, ident, owner, body):
        data = canonical(body)
        if len(data) > 60000: raise IntakeError('payload_too_large', 413)
        digest = hashlib.sha256(data).hexdigest()
        async with self.transaction(ident) as db:
            row = (await db.execute(select(intakes).where(intakes.c.id == ident,
                intakes.c.owner_id == owner).with_for_update())).mappings().first()
            if not row: raise IntakeError('intake_unavailable', 403)
            if row['payload_sha256'] and row['payload_sha256'] != digest:
                raise IntakeError('immutable_batch_conflict')
            if not row['payload_sha256']:
                await db.execute(update(intakes).where(intakes.c.id == ident).values(
                    payload_sha256=digest, payload_json=data.decode()))
        return digest

    async def save_receipt(self, ident, owner, receipt):
        if receipt.get('status') != 'completed': return
        data = canonical(receipt)
        if len(data) > 60000: raise IntakeError('receipt_too_large', 503)
        async with self.transaction(ident) as db:
            await db.execute(update(intakes).where(intakes.c.id == ident,
                intakes.c.owner_id == owner).values(receipt_json=data.decode()))
