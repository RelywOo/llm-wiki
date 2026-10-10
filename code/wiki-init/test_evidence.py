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

    def test_publish_extracts_receipts_without_model_copying_and_verifies_final_file(self):
        manifest={'answer':{'region':'eu-west-1','explanation':'Production scope.'},
                  'sources':[{'path':'.memory/research/events.md','start_line':1,'end_line':1,'event_id':'E001'}]}
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        result=json.loads(self.cli('evidence','publish','manifest.json','deliverables/answer.json').stdout)
        self.assertTrue(result['artifact_verified'])
        artifact=json.loads((self.root/'deliverables/answer.json').read_text())
        self.assertEqual(artifact['answer'],manifest['answer'])
        receipt=artifact['evidence'][0]['receipt']
        self.assertEqual(receipt['quote'],'E001: Production region is eu-west-1.\n')
        self.assertEqual(receipt['event_id'],'E001')
        self.check(receipt)
        checked=json.loads(self.cli('evidence','verify','deliverables/answer.json').stdout)
        self.assertTrue(checked['artifact_verified'])
        self.assertEqual(checked['semantic_support'],'not_checked')

    def test_publish_never_overwrites_existing_artifacts_or_repository_files(self):
        manifest={'answer':{'region':'eu-west-1'},'sources':[
            {'path':'.memory/research/events.md','start_line':1,'end_line':1}]}
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        original=self.source.read_bytes()
        for output in ('.memory/research/events.md','manifest.json','../outside.json',str(self.root/'absolute.json')):
            with self.subTest(output=output):
                result=self.cli('evidence','publish','manifest.json',output,ok=False)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.source.read_bytes(),original)
        self.cli('evidence','publish','manifest.json','deliverables/answer.json')
        before=(self.root/'deliverables/answer.json').read_bytes()
        manifest['answer']['region']='ap-south-1'
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        self.assertNotEqual(self.cli('evidence','publish','manifest.json','deliverables/answer.json',ok=False).returncode,0)
        self.assertEqual((self.root/'deliverables/answer.json').read_bytes(),before)

    def publish_manifest(self, sources=None, answer=None, output='deliverables/answer.json', ok=True):
        manifest={'answer':answer or {'region':'eu-west-1'},'sources':sources or [
            {'path':'.memory/research/events.md','start_line':1,'end_line':1,'event_id':'E001'}]}
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        return self.cli('evidence','publish','manifest.json',output,ok=ok)

    def test_final_artifact_detects_answer_receipt_and_source_changes(self):
        self.publish_manifest()
        target=self.root/'deliverables/answer.json';original=target.read_text()
        for field in ('answer','quote','hash','extra','semantic'):
            artifact=json.loads(original)
            if field=='answer': artifact['answer']['region']='ap-south-1'
            elif field=='quote': artifact['evidence'][0]['receipt']['quote']='Invented text'
            elif field=='hash': artifact['evidence'][0]['receipt']['source_sha256']='0'*64
            elif field=='extra': artifact['evidence'][0]['receipt']['meaning']='Production allowed'
            else: artifact['semantic_support']='verified'
            target.write_text(json.dumps(artifact))
            with self.subTest(field=field):
                self.assertNotEqual(self.cli('evidence','verify','deliverables/answer.json',ok=False).returncode,0)
        target.write_text(original)
        self.source.write_text(self.source.read_text()+'E003: New source revision.\n')
        result=self.cli('evidence','verify','deliverables/answer.json',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('source_changed',result.stdout)

    def test_publish_rejects_model_supplied_receipt_fields_and_wrong_events(self):
        base={'path':'.memory/research/events.md','start_line':1,'end_line':1,'event_id':'E001'}
        for change in ({'quote':'fake'},{'source_sha256':'0'*64},{'meaning':'approved'},
                       {'event_id':'E999'},{'current':1},{'current':True},{'start_line':True}):
            with self.subTest(change=change):
                self.assertNotEqual(self.publish_manifest([dict(base,**change)],ok=False).returncode,0)
                self.assertFalse((self.root/'deliverables/answer.json').exists())
        self.publish_manifest()

    def test_multi_source_current_decision_is_rechecked_after_supersession(self):
        path='.memory/wiki/decisions/0001-rule.md'
        (self.root/path).write_text('---\nid: 0001\ndate: 2026-10-10\n---\n# Rule\n## Decision\nProduction region is eu-west-1.\n')
        sources=[{'path':path,'start_line':7,'end_line':7,'current':True},
                 {'path':'.memory/research/events.md','start_line':2,'end_line':2,'event_id':'E002'}]
        self.publish_manifest(sources,{'region':'eu-west-1','explanation':'Лаборатория отдельно.'})
        self.cli('evidence','verify','deliverables/answer.json')
        (self.root/'.memory/wiki/decisions/0002-rule.md').write_text(
            '---\nid: 0002\ndate: 2026-10-11\nsupersedes: 0001\n---\n# Rule\n## Decision\nProduction region is us-east-1.\n')
        result=self.cli('evidence','verify','deliverables/answer.json',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('current_decision_not_established: superseded',result.stdout)
        self.assertNotEqual(self.publish_manifest(sources,output='deliverables/second.json',ok=False).returncode,0)
        self.assertFalse((self.root/'deliverables/second.json').exists())

    def test_publish_rejects_symlink_outputs_and_competing_publications(self):
        other=self.root/'other';other.mkdir()
        (self.root/'deliverables').symlink_to(other,target_is_directory=True)
        self.assertNotEqual(self.publish_manifest(ok=False).returncode,0)
        self.assertEqual(list(other.iterdir()),[])
        (self.root/'deliverables').unlink();(self.root/'deliverables').mkdir()
        (self.root/'deliverables/answer.json').symlink_to(self.source)
        original=self.source.read_bytes()
        self.assertNotEqual(self.publish_manifest(ok=False).returncode,0)
        self.assertEqual(self.source.read_bytes(),original)
        (self.root/'deliverables/answer.json').unlink()
        processes=[subprocess.Popen([sys.executable,'.memory/tools/structural.py','evidence','publish',
                    'manifest.json','deliverables/answer.json'],cwd=self.root,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,text=True) for _ in range(4)]
        for process in processes: process.communicate()
        self.assertEqual(sum(p.returncode==0 for p in processes),1)
        self.cli('evidence','verify','deliverables/answer.json')
        self.assertEqual([p.name for p in (self.root/'deliverables').iterdir()],['answer.json'])

    def test_publish_derives_an_unambiguous_event_label_when_selection_omits_it(self):
        self.publish_manifest([{'path':'.memory/research/events.md','start_line':1,'end_line':1}])
        artifact=json.loads((self.root/'deliverables/answer.json').read_text())
        self.assertEqual(artifact['evidence'][0]['receipt'].get('event_id'),'E001')
        self.cli('evidence','verify','deliverables/answer.json')
        # A range containing two different labels must not invent a single event identity.
        self.publish_manifest([{'path':'.memory/research/events.md','start_line':1,'end_line':2}],
                              output='deliverables/multiple.json')
        multiple=json.loads((self.root/'deliverables/multiple.json').read_text())
        self.assertNotIn('event_id',multiple['evidence'][0]['receipt'])
        self.publish_manifest([{'path':'.memory/research/events.md','start_line':1,'end_line':2,'event_id':'E002'}],
                              output='deliverables/explicit.json')
        explicit=json.loads((self.root/'deliverables/explicit.json').read_text())
        self.assertEqual(explicit['evidence'][0]['receipt']['event_id'],'E002')

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
