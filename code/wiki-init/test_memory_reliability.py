"""Behavioral regression tests through the installed public command surface."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class MemoryReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-reliability-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(HERE / 'templates/tree/.memory/tools', self.root / '.memory/tools')
        self.wiki = self.root / '.memory/wiki'
        (self.wiki / 'decisions').mkdir(parents=True)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def decision(self, did, text, supersedes=None):
        path = f'.memory/wiki/decisions/{did}-rule.md'
        self.write(path, f'---\nid: {did}\ndate: 2026-10-10\n' +
                   (f'supersedes: {supersedes}\n' if supersedes else '') +
                   f'---\n# Rule\n\n## Decision\n{text}\n')
        return path

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, '.memory/tools/structural.py', *args],
                                cwd=self.root, capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_old_section_identifies_current_successor(self):
        old = self.decision('0008', 'Server and offline: 512 KiB.')
        self.decision('0017', 'Server: 2048 KiB. Offline remains 512 KiB.', '0008')
        self.decision('0033', 'Offline: 256 KiB. Server remains 2048 KiB.', '0017')
        output = self.cli('section', old, 'Decision').stdout
        self.assertIn('SUPERSEDED', output)
        self.assertIn('0033', output)

    def test_partial_replacement_retains_earlier_scopes_from_any_entry(self):
        self.decision('0001', 'Retention: EU 30 days; US 90 days.')
        self.decision('0002', 'Change only EU retention to 7 days.', '0001')
        for entry in ('0001', '0002'):
            packet = json.loads(self.cli('decision', entry).stdout)
            self.assertEqual(packet['current'], '0002')
            self.assertEqual([x['id'] for x in packet['lineage']], ['0001', '0002'])
            self.assertIn('US 90 days', packet['lineage'][0]['text'])

    def test_ambiguous_and_invalid_graphs_refuse_current_answer(self):
        self.decision('0001', 'Original.')
        self.decision('0002', 'Alternative A.', '0001')
        self.decision('0003', 'Alternative B.', '0001')
        output = self.cli('decision', '0001', ok=False)
        self.assertNotEqual(output.returncode, 0)
        self.assertIn('ambiguous', output.stderr)
        self.decision('0001', 'Cycle.', '0002')
        self.assertNotEqual(self.cli('decision', '0001', ok=False).returncode, 0)

    def test_search_history_remains_visible_and_marked(self):
        self.decision('0001', 'Legacy secret quasar buffer 16.')
        self.decision('0002', 'Buffer 64.', '0001')
        output = self.cli('search', 'quasar').stdout
        self.assertIn('SUPERSEDED', output)
        self.assertIn('0002', output)

    def test_new_unrelated_decision_and_missing_id(self):
        self.decision('0001', 'TLS minimum 1.3.')
        self.decision('0002', 'Independent timeout 20 seconds.')
        packet = json.loads(self.cli('decision', '0001').stdout)
        self.assertEqual(packet['status'], 'current')
        self.assertEqual(len(packet['lineage']), 1)
        self.assertNotEqual(self.cli('decision', '9999', ok=False).returncode, 0)

    def test_resolver_refreshes_after_new_decision_without_stale_cache(self):
        self.decision('0001', 'Timeout is 10 seconds.')
        self.assertEqual(json.loads(self.cli('decision', '0001').stdout)['current'], '0001')
        self.decision('0002', 'Timeout is 25 seconds.', '0001')
        self.assertEqual(json.loads(self.cli('decision', '0001').stdout)['current'], '0002')
        self.assertEqual(json.loads(self.cli('decision', '0002').stdout)['current'], '0002')


if __name__ == '__main__':
    unittest.main()
