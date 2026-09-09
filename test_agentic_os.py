import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentic_os import bootstrap, context, doctor, plan, safe_path


class AgenticOSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_bootstrap_preserves_existing_user_context(self):
        first = bootstrap(self.root)
        profile = self.root/'context/user.md'
        profile.write_text('My edited preferences', encoding='utf-8')
        second = bootstrap(self.root)
        self.assertGreater(first['created'], 30)
        self.assertEqual(second['created'], 0)
        self.assertEqual(profile.read_text(encoding='utf-8'), 'My edited preferences')
        self.assertFalse(second['task_database_replaced'])

    def test_project_context_does_not_load_other_brand(self):
        bootstrap(self.root)
        packet = context('wdr', root=self.root)
        paths = [d['path'] for d in packet['documents']]
        self.assertIn('shared/brand-context/wdr.md', paths)
        self.assertNotIn('shared/brand-context/lumipaw.md', paths)
        self.assertEqual(packet['knowledge']['status'], 'query_required')
        self.assertTrue(all(len(d['sha256']) == 64 for d in packet['documents']))

    def test_traversal_and_unknown_projects_rejected(self):
        with self.assertRaises(ValueError): safe_path(self.root, '../escape')
        with self.assertRaises(ValueError): context('../private', root=self.root)
        with self.assertRaises(ValueError): plan('nope', 'task', root=self.root)

    def test_plan_treats_command_like_text_as_data(self):
        bootstrap(self.root)
        task = 'echo $(Remove-Item F:\\*) ; arbitrary text'
        result = plan('nexen', task, 'phases', self.root)
        saved = json.loads(Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['task'], task)
        self.assertEqual(saved['status'], 'planned')
        self.assertTrue(all(x['status'] == 'pending' for x in saved['phases']))

    def test_files_do_not_claim_running_hooks_or_remote_access(self):
        bootstrap(self.root)
        report = doctor(self.root, live=False)
        checks = {x['name']:x for x in report['checks']}
        self.assertEqual(checks['identity']['status'], 'passed')
        self.assertEqual(checks['native_session_hook']['status'], 'needs_attention')
        self.assertEqual(checks['remote_access']['status'], 'needs_attention')
        self.assertFalse(report['ready'])
        self.assertIsNone(report['completion_percentage'])

    def test_wrong_receipt_type_does_not_break_readiness(self):
        bootstrap(self.root)
        receipt = self.root / 'bad-receipt.json'
        receipt.write_text('[1]', encoding='utf-8')
        def isolated_path(value):
            if str(value).startswith('H:/NEXEN/state/'):
                return receipt
            return Path(value)
        with patch('agentic_os.Path', side_effect=isolated_path):
            report = doctor(self.root, live=False)
        checks = {x['name']: x for x in report['checks']}
        self.assertEqual(checks['claude_mem']['status'], 'needs_attention')
        self.assertEqual(checks['memsearch']['status'], 'needs_attention')

if __name__ == '__main__': unittest.main()
