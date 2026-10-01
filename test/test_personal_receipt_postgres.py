"""Opt-in PostgreSQL regression; only transaction-local temporary fixture tables."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import unittest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SPEC = importlib.util.spec_from_file_location('personal_search_under_test',
    Path(__file__).resolve().parents[1] / 'backend/open_webui/there_integration/personal_search.py')
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


@unittest.skipUnless(os.environ.get('THERE_TEST_POSTGRES_URL'), 'requires explicit PostgreSQL test connection')
class PostgresReceiptTests(unittest.TestCase):
    def test_full_text_match_but_bounded_unicode_transfer(self):
        async def run():
            engine = create_async_engine(os.environ['THERE_TEST_POSTGRES_URL'])
            try:
                async with engine.connect() as db:
                    transaction = await db.begin()
                    try:
                        await db.execute(text('SET LOCAL search_path = pg_temp, pg_catalog'))
                        await db.execute(text('CREATE TEMP TABLE chat (id text, user_id text, title text, updated_at bigint, chat jsonb, meta jsonb, timer_at bigint) ON COMMIT DROP'))
                        await db.execute(text('CREATE TEMP TABLE chat_message (id text, user_id text, chat_id text, role text, parent_id text, content jsonb, done boolean, error jsonb) ON COMMIT DROP'))
                        for i, extra in enumerate(('', ' TARGET NEEDLE')):
                            payload = {'history': {'messages': {
                                'q': {'role': 'user', 'content': '質問😀' * 5333 + '問' + extra},
                                'a': {'role': 'assistant', 'parentId': 'q', 'done': True,
                                      'content': '回答😀' * 10666 + '回答' + extra}}}}
                            await db.execute(text("INSERT INTO chat VALUES (:id,'alice','fixture',:i,CAST(:p AS jsonb),'{}',NULL)"),
                                             dict(id=str(i), i=i, p=json.dumps(payload)))
                        class CapturingDB:
                            async def execute(self, *args, **kwargs):
                                result = await db.execute(*args, **kwargs)
                                rows = result.mappings().all()
                                for row in rows:
                                    self_test.assertLessEqual(len(row['question']), 16000)
                                    self_test.assertLessEqual(len(row['answer']), 32000)
                                    self_test.assertNotIn('search_body', row)
                                class Replay:
                                    def mappings(self): return self
                                    def all(self): return rows
                                return Replay()
                        self_test = self
                        for criteria in ({'terms': ['target', 'needle']}, {'query': 'TARGET NEEDLE'}):
                            result = await search.search_pairs(CapturingDB(), 'alice', **criteria)
                            self.assertEqual([r['chat_id'] for r in result['items']], ['1'])
                            item = result['items'][0]
                            self.assertTrue(item['truncated'])
                            self.assertEqual(len(item['question']), 16000)
                            self.assertEqual(len(item['answer']), 32000)
                        exact = await search.search_pairs(CapturingDB(), 'alice', query='回答', limit=1, page=2)
                        self.assertEqual(exact['items'][0]['chat_id'], '0')
                        self.assertFalse(exact['items'][0]['truncated'])
                    finally:
                        await transaction.rollback()
            finally:
                await engine.dispose()
        asyncio.run(run())

    def test_rank_filter_manual_search_ownership_and_pagination(self):
        async def run():
            engine = create_async_engine(os.environ['THERE_TEST_POSTGRES_URL'])
            try:
                async with engine.connect() as db:
                    transaction = await db.begin()
                    try:
                        await db.execute(text('SET LOCAL search_path = pg_temp, pg_catalog'))
                        await db.execute(text('CREATE TEMP TABLE chat (id text, user_id text, title text, updated_at bigint, chat jsonb, meta jsonb, timer_at bigint) ON COMMIT DROP'))
                        await db.execute(text('CREATE TEMP TABLE chat_message (id text, user_id text, chat_id text, role text, parent_id text, content jsonb, done boolean, error jsonb) ON COMMIT DROP'))
                        answers = ['Useful device ai status instructions.'] + [
                            prefix + 'json\n{}\n```' for prefix in search.DEVICE_RECEIPT_PREFIXES]
                        for owner in ('alice', 'bob'):
                            for i, answer in enumerate(answers):
                                payload = {'history': {'messages': {
                                    'q': {'role': 'user', 'content': 'device ai status'},
                                    'a': {'role': 'assistant', 'parentId': 'q', 'content': answer, 'done': True}}}}
                                await db.execute(text('INSERT INTO chat VALUES (:id,:owner,\'fixture\',:updated,CAST(:payload AS jsonb),\'{}\',NULL)'),
                                    dict(id=f'{owner}-{i}', owner=owner, updated=i, payload=json.dumps(payload)))
                        normal = await search.search_pairs(db, 'alice', terms=['device', 'status'], limit=3)
                        self.assertEqual([x['chat_id'] for x in normal['items']], ['alice-3', 'alice-2', 'alice-1'])
                        self.assertTrue(normal['has_more'])
                        auto = await search.search_pairs(db, 'alice', terms=['device', 'status'], limit=3, exclude_device_receipts=True)
                        self.assertEqual([x['chat_id'] for x in auto['items']], ['alice-0'])
                        self.assertFalse(auto['has_more'])
                        # Non-ranked path and filtering before pagination use the same rule.
                        auto = await search.search_pairs(db, 'alice', query='device ai', limit=1, exclude_device_receipts=True)
                        self.assertEqual([x['chat_id'] for x in auto['items']], ['alice-0'])
                        self.assertFalse(auto['has_more'])
                        manual = await search.search_pairs(db, 'alice', query='device ai', limit=10)
                        self.assertEqual(len(manual['items']), 4)
                        self.assertFalse((await search.search_pairs(db, 'alice', query="%' OR 1=1 --"))['items'])
                        self.assertFalse((await search.search_pairs(db, 'alice', terms=['device'], exclude_id='alice-0', exclude_device_receipts=True))['items'])
                    finally:
                        await transaction.rollback()
            finally:
                await engine.dispose()
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
