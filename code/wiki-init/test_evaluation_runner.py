"""Exercise runner orchestration without claiming any model-quality result."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EvidenceRunnerTest(unittest.TestCase):
    def test_failed_session_does_not_replace_next_arms_claim_prompt(self):
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='wiki-runner-') as temporary:
            root = Path(temporary)
            runtime = root/'failed_runtime.py'
            runtime.write_text(
                'from pathlib import Path\n'
                'def session(workspace, output, prompt, **kwargs):\n'
                '    output.mkdir(parents=True)\n'
                '    (output/"prompt.txt").write_text(prompt)\n'
                '    return {"completed": False}\n')
            result = subprocess.run(
                [sys.executable, str(repo/'code/reliability-evaluation/run_evidence.py'),
                 '--runtime', str(runtime), '--baseline', str(repo),
                 '--output', str(root/'results')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            rows = json.loads((root/'results/summary.json').read_text())
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(not row['passed'] for row in rows))
            for case in ('scope', 'date', 'reason'):
                prompts = [(root/f'results/{case}-{arm}/session/prompt.txt').read_text()
                           for arm in ('baseline', 'new')]
                self.assertEqual(prompts[0], prompts[1])
                self.assertIn('A:', prompts[0])


if __name__ == '__main__':
    unittest.main()
