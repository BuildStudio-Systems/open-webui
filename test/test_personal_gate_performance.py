"""Real SQLite/ORM destination gate with synthetic large transcripts."""
import ast
from contextlib import asynccontextmanager
import html
from pathlib import Path
from types import SimpleNamespace
import unittest

from sqlalchemy import JSON, Column, Integer, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, load_only
from sqlalchemy.types import TypeDecorator

Base = declarative_base()


class TranscriptJSON(TypeDecorator):
    impl = JSON
    cache_ok = True
    reads = 0

    def process_result_value(self, value, dialect):
        type(self).reads += 1
        return value


class Chat(Base):
    __tablename__ = 'chat'
    id = Column(String, primary_key=True)
    user_id = Column(String)
    share_id = Column(String)
    meta = Column(JSON)
    timer_at = Column(Integer)
    chat = Column(TranscriptJSON)


class AccessGrant(Base):
    __tablename__ = 'access_grant'
    id = Column(String, primary_key=True)
    resource_type = Column(String)
    resource_id = Column(String)


class PersonalGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.searches = []
        async with self.sessions() as session:
            session.add(Chat(id='owned', user_id='alice', meta={},
                             chat={'content': 'x' * (4 * 1024 * 1024)}))
            await session.commit()
        TranscriptJSON.reads = 0
        @asynccontextmanager
        async def personal_session(db):
            yield db
        async def search_history(db, owner, **kwargs):
            self.searches.append((owner, kwargs))
            return {'items': [{'chat_id': 'past', 'message_id': 'answer',
                               'question': 'test question', 'answer': 'test answer'}]}
        path = Path(__file__).resolve().parents[1] / 'backend/open_webui/there_integration/personal.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'personal_sources']
        namespace = dict(Chat=Chat, AccessGrant=AccessGrant, select=select, load_only=load_only,
                         personal_session=personal_session, query_terms=lambda q: ['test'] if q else [],
                         search_history=search_history, html=html)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
        self.sources = namespace['personal_sources']

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_destination_transcript_not_decoded_and_retrieval_unchanged(self):
        # Old full-entity gate necessarily decodes this 4MiB synthetic value.
        async with self.sessions() as session:
            row = await session.scalar(select(Chat).where(Chat.id == 'owned', Chat.user_id == 'alice'))
            self.assertEqual(len(row.chat['content']), 4 * 1024 * 1024)
        self.assertEqual(TranscriptJSON.reads, 1)
        TranscriptJSON.reads = 0
        async with self.sessions() as session:
            result = await self.sources(SimpleNamespace(id='alice'), 'owned', 'test', db=session)
        self.assertEqual(TranscriptJSON.reads, 0)
        self.assertEqual(result[0]['metadata'][0]['chat_id'], 'past')
        self.assertEqual(self.searches, [('alice', {'terms': ['test'], 'exclude_id': 'owned', 'limit': 3})])

    async def test_all_destination_denials_still_precede_retrieval(self):
        for case in ('foreign', 'admin_foreign', 'missing', 'shared', 'internal', 'timer', 'grant', 'empty'):
            with self.subTest(case=case):
                async with self.sessions() as session:
                    row = await session.get(Chat, 'owned')
                    row.share_id = 'public' if case == 'shared' else None
                    row.meta = {'internal': True} if case == 'internal' else {}
                    row.timer_at = 1 if case == 'timer' else None
                    if case == 'grant':
                        session.add(AccessGrant(id='grant', resource_type='chat', resource_id='owned'))
                    await session.commit()
                TranscriptJSON.reads = 0
                async with self.sessions() as session:
                    owner = 'admin' if case == 'admin_foreign' else 'bob' if case == 'foreign' else 'alice'
                    result = await self.sources(SimpleNamespace(id=owner, role='admin' if owner == 'admin' else 'user'),
                        'missing' if case == 'missing' else 'owned', '' if case == 'empty' else 'test', db=session)
                self.assertEqual(result, [])
                self.assertEqual(TranscriptJSON.reads, 0)
                self.assertEqual(self.searches, [])


if __name__ == '__main__':
    unittest.main()
