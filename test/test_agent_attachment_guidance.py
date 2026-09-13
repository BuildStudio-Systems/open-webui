import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

source = Path(__file__).parents[1] / 'backend/open_webui/utils/agent_file_delivery.py'
spec = importlib.util.spec_from_file_location('attachment_guidance', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class AttachmentGuidanceTests(unittest.TestCase):
    def test_admin_both_stream_modes_preserve_history_and_parameters(self):
        for stream in [False, True]:
            messages = [{'role':'system','content':'Original instructions'}, {'role':'user','content':'Generate a document'}]
            payload = {'messages':messages, 'stream':stream, 'model':'There-Agent 3.8'}
            result = module.with_agent_attachment_guidance(payload, 'http://127.0.0.1:8642/v1', SimpleNamespace(id='admin-1',role='admin'))
            self.assertEqual(result['messages'][0]['content'], module.AGENT_ATTACHMENT_GUIDANCE)
            self.assertEqual(result['messages'][1:], messages)
            self.assertEqual(len(payload['messages']), 2)
            self.assertEqual(result['stream'], stream)
            self.assertEqual(result['model'], payload['model'])
            self.assertIs(module.with_agent_attachment_guidance(result, 'http://127.0.0.1:8642/v1', SimpleNamespace(id='admin-1',role='admin')), result)

    def test_other_roles_and_invalid_owners_unchanged(self):
        payload = {'messages':[]}
        for role, owner in [('user','u1'),('pending','u2'),('admin','bad owner'),('admin','')]:
            self.assertIs(module.with_agent_attachment_guidance(payload, 'http://127.0.0.1:8642/v1', SimpleNamespace(role=role,id=owner)),payload)

    def test_other_providers_unchanged(self):
        payload = {'messages':[]}
        for url in ['http://127.0.0.1:8000/v1','https://external.example/v1','http://localhost:8642/v1/extra','http://user@localhost:8642/v1']:
            self.assertIs(module.with_agent_attachment_guidance(payload,url,SimpleNamespace(role='admin',id='a1')),payload)

    def test_missing_or_invalid_history_unchanged(self):
        for payload in [{},{'messages':None},{'messages':'not a list'}]:
            self.assertIs(module.with_agent_attachment_guidance(payload,'http://127.0.0.1:8642/v1',SimpleNamespace(role='admin',id='a1')),payload)

if __name__ == '__main__':
    unittest.main()
