"""Evidence receipts verify location and freshness, not semantic entailment."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wiki-evidence-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        shutil.copytree(HERE/'templates/tree/.memory/tools',self.root/'.memory/tools')
        (self.root/'.memory/wiki/decisions').mkdir(parents=True)
        self.source=self.root/'.memory/research/events.md';self.source.parent.mkdir()
        self.source.write_text('E001: Production region is eu-west-1.\nE002: ap-south-1 is lab-only.\n')

    def cli(self,*args,ok=True):
        p=subprocess.run([sys.executable,'.memory/tools/structural.py',*args],cwd=self.root,text=True,capture_output=True)
        if ok:self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return p

    def receipt(self,start=1,end=1):
        return json.loads(self.cli('evidence','quote','.memory/research/events.md',str(start),str(end)).stdout)

    def check(self,receipt,*options,ok=True):
        (self.root/'receipt.json').write_text(json.dumps(receipt))
        return self.cli('evidence','check','receipt.json',*options,ok=ok)

    def test_exact_quote_is_extracted_and_fabricated_wording_rejected(self):
        receipt=self.receipt()
        self.assertEqual(receipt['quote'],'E001: Production region is eu-west-1.\n')
        self.check(receipt)
        receipt['quote']='E001: Production region is ap-south-1.'
        self.assertNotEqual(self.check(receipt,ok=False).returncode,0)

    def test_wrong_existing_file_range_and_event_id_are_rejected(self):
        receipt=self.receipt()
        other=self.source.parent/'neighbor.md';other.write_text('E009: Unrelated evidence.\n')
        wrong=dict(receipt,path='.memory/research/neighbor.md')
        self.assertNotEqual(self.check(wrong,ok=False).returncode,0)
        self.assertNotEqual(self.check(dict(receipt,end_line=2),ok=False).returncode,0)
        self.assertNotEqual(self.check(dict(receipt,event_id='E099'),ok=False).returncode,0)
        self.check(dict(receipt,event_id='E001'))

    def test_source_edit_invalidates_receipt_even_when_quote_survives(self):
        receipt=self.receipt()
        self.source.write_text(self.source.read_text()+'E003: New information.\n')
        result=self.check(receipt,ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('source_changed',result.stdout)
        self.check(self.receipt())

    def test_literal_quote_does_not_certify_false_scope_claim(self):
        receipt=self.receipt(2,2)
        receipt['claim']='ap-south-1 is allowed in production.'
        output=json.loads(self.check(receipt).stdout)['results'][0]
        self.assertTrue(output['location_and_freshness_verified'])
        self.assertEqual(output['semantic_support'],'not_checked')
        self.assertNotEqual(self.check(receipt,'--current',ok=False).returncode,0)

    def test_old_decision_is_valid_history_but_not_current_evidence(self):
        for did,body in [('0001','Server limit 512 KiB.'),('0002','Server limit 2048 KiB.')]:
            path=self.root/f'.memory/wiki/decisions/{did}-rule.md'
            path.write_text(f'---\nid: {did}\ndate: 2026-10-10\n'+
                            ('supersedes: 0001\n' if did=='0002' else '')+f'---\n# Rule\n## Decision\n{body}\n')
        old=json.loads(self.cli('evidence','quote','.memory/wiki/decisions/0001-rule.md','7','7').stdout)
        self.check(old)
        self.assertNotEqual(self.check(old,'--current',ok=False).returncode,0)
        new=json.loads(self.cli('evidence','quote','.memory/wiki/decisions/0002-rule.md','8','8').stdout)
        self.check(new,'--current')

    def test_new_unicode_multiline_quote_and_escape_attempts(self):
        self.source.write_bytes('E010: Причина — миграционные тесты.\r\nСрок: 12 ноября.\r\n'.encode())
        receipt=self.receipt(1,2)
        self.assertIn('миграционные',receipt['quote'])
        self.check(receipt)
        self.assertNotEqual(self.cli('evidence','quote','../outside.md','1','1',ok=False).returncode,0)
        self.assertNotEqual(self.cli('evidence','quote','.memory/research/events.md','0','1',ok=False).returncode,0)
        self.assertNotEqual(self.check(dict(receipt,start_line=True),ok=False).returncode,0)


if __name__=='__main__':unittest.main()
