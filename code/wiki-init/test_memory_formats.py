"""Candidate shape contract through the package's format interface."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tested_formats',
    HERE / 'templates/tree/.memory/tools/memory_formats.py')
formats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(formats)


class CandidateFormatTest(unittest.TestCase):
    def test_frozen_format_results_and_read_only_behavior(self):
        contract = json.loads((HERE / 'fixtures/memory-format-contract.json').read_text())
        for i, case in enumerate(contract['cases']):
            with self.subTest(case=i), tempfile.TemporaryDirectory(prefix='wiki-formats-') as tmp:
                root = Path(tmp)
                (root / '.memory/wiki/decisions').mkdir(parents=True)
                if case['tracker']:
                    (root / '.memory/tracker.md').write_text('Existing legacy profile.')
                before = {str(p.relative_to(root)): p.read_bytes()
                          for p in root.rglob('*') if p.is_file()}
                outputs = {p: text.encode() for p, text in case['outputs'].items()}
                self.assertEqual(formats.check_candidate(root, outputs), case['errors'])
                self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes()
                                          for p in root.rglob('*') if p.is_file()})

    def test_proposed_decision_checks_share_the_existing_inventory(self):
        with tempfile.TemporaryDirectory(prefix='wiki-formats-decisions-') as tmp:
            root = Path(tmp)
            path = root / '.memory/wiki/decisions/0001-existing.md'
            path.parent.mkdir(parents=True)
            path.write_text('Existing immutable decision.')
            outputs = {'.memory/STATE.md': b'# STATE\n## Open\n- Unresolved.\n',
                       '.memory/wiki/index.md': '# Index\n## Decisions\n- [0001](decisions/0001-existing.md) — Existing rule.\n'.encode()}
            self.assertEqual(formats.check_candidate(root, outputs), [])
            outputs['.memory/wiki/decisions/0001-new.md'] = b'Conflicting decision.'
            self.assertIn('duplicate decision id: 0001', formats.check_candidate(root, outputs))
            del outputs['.memory/wiki/decisions/0001-new.md']
            outputs['.memory/wiki/decisions/bad_name.md'] = b'Invalid filename.'
            self.assertIn('invalid decision filename: .memory/wiki/decisions/bad_name.md',
                          formats.check_candidate(root, outputs))


if __name__ == '__main__':
    unittest.main()
