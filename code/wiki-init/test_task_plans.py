"""Plans are computed and rechecked through the public task CLI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent


class TaskPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wiki-plan-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        shutil.copytree(HERE/'templates/tree/.memory/tools',self.root/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
        (self.root/'.memory/wiki').mkdir();(self.root/'code').mkdir();self.seq=0
        (self.root/'code/events.md').write_text('E1: Synthetic reviewed task events.\n')

    def cli(self,*args,ok=True):
        result=subprocess.run([sys.executable,'.memory/tools/structural.py','tasks',*args],cwd=self.root,text=True,capture_output=True)
        if ok:self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result

    def event(self,key,op='create',**values):
        self.seq+=1
        if op=='create':
            values={'title':key,'status':'open','owner':'Ari','due':'2026-12-03','priority':1,
                    'release_required':True,'dependencies':[],**values}
        event={'seq':self.seq,'id':key,'op':op,'values':values,'reason':'Reviewed synthetic event',
               'source':{'path':'code/events.md','quote':'E1: Synthetic reviewed task events.'}}
        (self.root/'event.json').write_text(json.dumps(event));self.cli('apply','event.json')

    def test_new_mixed_inventory_has_distinct_groups_and_actual_blockers(self):
        self.event('READY');self.event('WAIT',dependencies=['READY'])
        self.event('MANUAL',status='blocked');self.event('ONGOING',status='in_progress',dependencies=['READY'])
        self.event('UNKNOWN',status=None);self.event('CLOSED',status='done');self.event('NO_OWNER',owner=None)
        plan=json.loads(self.cli('plan').stdout)
        self.assertEqual({k:[t['id'] for t in v] for k,v in plan['groups'].items()},
                         {'ready':['READY'],'blocked':['MANUAL','WAIT'],'uncertain':['NO_OWNER','UNKNOWN'],
                          'ongoing':['ONGOING'],'closed':['CLOSED']})
        waiting=next(t for t in plan['groups']['blocked'] if t['id']=='WAIT')
        self.assertEqual(waiting['blocked_by'],['READY'])
        self.assertEqual(waiting['owner'],'Ari')
        self.assertFalse(plan['inventory_complete'])
        self.assertIn('ongoing_with_unfinished_prerequisites',plan['groups']['ongoing'][0]['warnings'])

    def test_unknown_status_with_known_blocker_is_never_assumed_open(self):
        self.event('PREREQ');self.event('UNKNOWN',status=None,dependencies=['PREREQ'])
        plan=json.loads(self.cli('plan').stdout)
        self.assertEqual([t['id'] for t in plan['groups']['uncertain']],['UNKNOWN'])
        self.assertEqual(plan['groups']['uncertain'][0]['blocked_by'],['PREREQ'])

    def test_reopened_direct_prerequisite_invalidates_saved_plan_and_owner_is_preserved(self):
        self.event('CREDENTIALS',status='done');self.event('QA',status='done',dependencies=['CREDENTIALS'])
        self.event('SHIP',dependencies=['QA','CREDENTIALS'],owner='Nora')
        old=json.loads(self.cli('plan').stdout);self.assertEqual([t['id'] for t in old['groups']['ready']],['SHIP'])
        (self.root/'plan.json').write_text(json.dumps(old));self.cli('verify-plan','plan.json')
        self.event('CREDENTIALS','update',status='open')
        self.assertNotEqual(self.cli('verify-plan','plan.json',ok=False).returncode,0)
        new=json.loads(self.cli('plan').stdout)
        ship=new['groups']['blocked'][0]
        self.assertEqual(ship['id'],'SHIP');self.assertEqual(ship['blocked_by'],['CREDENTIALS'])
        self.assertEqual(ship['dependencies'],['QA','CREDENTIALS']);self.assertEqual(ship['owner'],'Nora')
        self.assertTrue(ship['release_required'])

    def test_saved_plan_rejects_omitted_obligation_invented_ready_and_coerced_type(self):
        self.event('A');self.event('B',dependencies=['A'])
        plan=json.loads(self.cli('plan').stdout)
        mutations=[]
        removed=json.loads(json.dumps(plan));removed['groups']['blocked']=[];mutations.append(removed)
        invented=json.loads(json.dumps(plan));invented['groups']['ready'].append(invented['groups']['blocked'].pop());mutations.append(invented)
        coerced=json.loads(json.dumps(plan));coerced['schema']=True;mutations.append(coerced)
        for altered in mutations:
            (self.root/'plan.json').write_text(json.dumps(altered))
            self.assertNotEqual(self.cli('verify-plan','plan.json',ok=False).returncode,0)

    def test_legacy_missing_inventory_is_not_a_complete_empty_plan(self):
        plan=json.loads(self.cli('plan').stdout)
        self.assertFalse(plan['inventory_complete']);self.assertIn('untracked_work_may_exist',plan['warnings'])
        self.assertTrue(all(not group for group in plan['groups'].values()))

    def test_readiness_inventory_cannot_adopt_a_selected_continuation(self):
        self.event('READY')
        plan = json.loads(self.cli('plan').stdout)
        self.assertFalse(plan['selects_next'])
        self.assertEqual(plan['continuation_source'], '.memory/STATE.md#Next')
        self.assertEqual(plan['groups']['ready'][0]['id'], 'READY')
        human = self.cli('plan', '--markdown').stdout
        self.assertIn('Readiness does not select the next action', human)

    def test_human_plan_is_generated_from_same_state_and_altered_next_step_fails(self):
        self.event('GATE');self.event('SHIP',dependencies=['GATE'],owner=None)
        rendered=self.cli('plan','--markdown').stdout
        self.assertIn('## Blocked work',rendered)
        self.assertIn('Resolve prerequisites: GATE',rendered)
        self.assertIn('Unknown',rendered)
        (self.root/'plan.md').write_text(rendered);self.cli('verify-plan','plan.md')
        (self.root/'plan.md').write_text(rendered.replace('Resolve prerequisites: GATE','Start now'))
        self.assertNotEqual(self.cli('verify-plan','plan.md',ok=False).returncode,0)

    def test_advisory_links_do_not_block_and_priority_then_due_order_is_explicit(self):
        self.event('ADVISORY',status='open',priority=3)
        self.event('NEW',priority=0,due=None);self.event('SOON',priority=1,due='2026-11-01')
        self.event('LATER',priority=1,due='2026-12-01')
        self.event('NEW','link',target='ADVISORY',kind='soft')
        plan=json.loads(self.cli('plan').stdout)
        self.assertEqual([t['id'] for t in plan['groups']['ready']],['NEW','SOON','LATER','ADVISORY'])
        self.assertEqual(plan['groups']['ready'][0]['advisory_dependencies'],['ADVISORY'])
        self.assertIsNone(plan['groups']['ready'][0]['due'])

    def test_markdown_rejects_source_changes_even_when_recorded_quote_survives(self):
        self.event('NEW')
        (self.root/'plan.md').write_text(self.cli('plan','--markdown').stdout)
        self.cli('verify-plan','plan.md')
        with (self.root/'code/events.md').open('a') as source:
            source.write('E2: Newly recorded context outside the quoted excerpt.\n')
        self.assertNotEqual(self.cli('verify-plan','plan.md',ok=False).returncode,0)

    def test_human_plan_shows_manual_explanation_and_all_known_blocker_ids(self):
        self.event('GATE')
        self.event('MANUAL',status='blocked')
        self.event('ONGOING',status='in_progress',dependencies=['GATE'])
        self.event('UNKNOWN',status=None,dependencies=['GATE'])
        rendered=self.cli('plan','--markdown').stdout
        manual=next(line for line in rendered.splitlines() if '| MANUAL:' in line)
        self.assertIn('Reviewed synthetic event',manual)
        self.assertIn('code/events.md',manual)
        self.assertIn('E1: Synthetic reviewed task events.',manual)
        for key in ('ONGOING','UNKNOWN'):
            line=next(line for line in rendered.splitlines() if '| '+key+':' in line)
            self.assertIn('GATE',line)

    def test_unknown_prerequisite_owner_and_sources_are_visible_after_freshness_change(self):
        self.event('GATE',status=None);self.event('SHIP',dependencies=['GATE'],owner=None)
        saved=self.cli('plan').stdout;(self.root/'plan.json').write_text(saved)
        self.cli('verify-plan','plan.json')
        (self.root/'code/events.md').write_text('Source revised after capture.\n')
        self.assertNotEqual(self.cli('verify-plan','plan.json',ok=False).returncode,0)
        plan=json.loads(self.cli('plan').stdout)
        self.assertIn('source_excerpt_review_required',plan['warnings'])
        ship=next(t for t in plan['groups']['uncertain'] if t['id']=='SHIP')
        self.assertEqual(ship['unknown_dependencies'],['GATE']);self.assertIsNone(ship['owner'])
        self.assertIn('owner',ship['unknown_fields'])


if __name__=='__main__':unittest.main()
