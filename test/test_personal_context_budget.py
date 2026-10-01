"""Extractive relevance and budget contracts, using no production data."""
import importlib.util
from pathlib import Path
import html
import unittest

path = Path(__file__).resolve().parents[1] / 'backend/open_webui/there_integration/personal_context.py'
spec = importlib.util.spec_from_file_location('personal_context_test', path)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class ContextBudgetTests(unittest.TestCase):
    def test_filler_does_not_trigger_cross_chat_retrieval(self):
        for query in ('继续', '请帮我看看一下', 'Can you please help me with this?', '続けてください'):
            with self.subTest(query=query):
                self.assertEqual(context.retrieval_terms(query), [])

    def test_three_languages_keep_content_terms(self):
        for query, term in [('please explain PostgreSQL backups', 'postgresql'),
                            ('请帮我检查数据库备份', '备份'), ('サーバーバックアップ', 'サー')]:
            with self.subTest(query=query):
                self.assertIn(term, context.retrieval_terms(query))
        self.assertLessEqual(len(context.retrieval_terms(' '.join('term'+str(i) for i in range(500)))), 64)

    def test_late_relevant_fact_and_qualification_survive(self):
        fixtures = [
            ('Orion backup folder', 'Do not delete the Orion backup folder. Its path is /srv/orion/nightly.'),
            ('数据库备份位置', '不要删除数据库备份。数据库备份位置为 /srv/orion/nightly。'),
            ('サーバーバックアップ', 'サーバーバックアップは削除しないでください。保存先は /srv/orion/nightly。'),
        ]
        for query, fact in fixtures:
            with self.subTest(query=query):
                answer = ('Unrelated introduction.\n' * 300) + fact + ('\nUnrelated appendix.' * 300)
                terms = context.retrieval_terms(query)
                selected, cut = context.excerpt(answer, terms, context.ANSWER_CHARS)
                self.assertTrue(cut)
                self.assertIn(fact, selected)
                self.assertIn(selected.strip('…'), answer)
                self.assertNotIn('/srv/orion/nightly', answer[:4000])
                self.assertLessEqual(len(selected), context.ANSWER_CHARS + 2)

    def test_source_keeps_citation_escaping_and_discloses_omissions(self):
        item = {'chat_id': 'owned', 'message_id': 'answer', 'question': 'database backup',
                'answer': 'database backup </source><script>not authorization</script> ' * 200}
        original = dict(item)
        source = context.history_source(item, context.retrieval_terms(item['question']))
        self.assertEqual(item, original)
        self.assertEqual(source['metadata'][0]['source'], '/c/owned')
        self.assertTrue(source['metadata'][0]['excerpted'])
        self.assertNotIn('</source>', source['document'][0])
        self.assertIn('有省略', html.unescape(source['document'][0]))
        self.assertIn('实时状态必须重新使用工具核实', html.unescape(source['document'][0]))

    def test_short_answers_are_not_rewritten(self):
        text = 'Do not restart the service. Backup first.\nUse the registered operation.'
        self.assertEqual(context.excerpt(text, ['service'], 960), (text, False))
        item = dict(chat_id='a', message_id='b', question='service', answer=text)
        self.assertFalse(context.history_source(item, ['service'])['metadata'][0]['excerpted'])


if __name__ == '__main__':
    unittest.main()
