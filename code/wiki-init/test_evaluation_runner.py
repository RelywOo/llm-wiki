"""Exercise runner orchestration without claiming any model-quality result."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EvidenceRunnerTest(unittest.TestCase):
    def test_delivery_replay_rejects_a_different_event_log_before_model_calls(self):
        repo=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='wiki-replay-') as temporary:
            root=Path(temporary);replay=root/'replay'
            (replay/'.memory/research').mkdir(parents=True)
            (replay/'.memory/research/events.md').write_text('E001: Different history.\n')
            (replay/'.memory/tools').mkdir()
            runtime=root/'runtime.py'
            runtime.write_text('def session(*args, **kwargs):\n    raise RuntimeError("MODEL_WAS_CALLED")\n')
            result=subprocess.run([sys.executable,str(repo/'code/reliability-evaluation/run_evidence_delivery.py'),
                '--baseline',str(repo),'--runtime',str(runtime),'--replay-workspace',str(replay),
                '--output',str(root/'results')],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('replay event source does not match the fixed current-case oracle',result.stdout+result.stderr)
            self.assertNotIn('MODEL_WAS_CALLED',result.stdout+result.stderr)
            self.assertFalse((root/'results').exists())

    def test_numeric_verdicts_cannot_pass_as_boolean_answers(self):
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='wiki-runner-types-') as temporary:
            root = Path(temporary)
            runtime = root/'numeric_runtime.py'
            runtime.write_text(
                'import json, subprocess, sys\n'
                'def session(workspace, output, prompt, **kwargs):\n'
                '    receipts=[]\n'
                '    lines=(workspace/".memory/research/events.md").read_text().splitlines()\n'
                '    for number, line in enumerate(lines, 1):\n'
                '        text=subprocess.check_output([sys.executable, ".memory/tools/structural.py",\n'
                '            "evidence", "quote", ".memory/research/events.md", str(number),\n'
                '            str(number), "--event", line.split(":")[0]], cwd=workspace, text=True)\n'
                '        receipts.append(json.loads(text))\n'
                '    (workspace/"deliverables/answer.json").write_text(json.dumps(\n'
                '        {"claims":{"A":0,"B":1},"evidence":receipts}))\n'
                '    return {"completed": True}\n')
            result = subprocess.run(
                [sys.executable, str(repo/'code/reliability-evaluation/run_evidence.py'),
                 '--runtime', str(runtime), '--baseline', str(repo),
                 '--output', str(root/'results')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            rows = json.loads((root/'results/summary.json').read_text())
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(not row['schema_valid'] for row in rows))
            self.assertTrue(all(not row['passed'] for row in rows))

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
