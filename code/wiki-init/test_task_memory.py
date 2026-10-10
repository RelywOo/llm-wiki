"""Task capture and recovery contracts through the public CLI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class TaskMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wiki-tasks-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(HERE/'templates/tree/.memory/tools', self.root/'.memory/tools')
        (self.root/'.memory/wiki').mkdir()
        (self.root/'.memory/research').mkdir()
        (self.root/'.memory/research/events.md').write_text('E001: Release task is required.\nE002: Owner changes to Mei.\n')

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable,'.memory/tools/structural.py',*args],cwd=self.root,text=True,capture_output=True)
        if ok: self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return p

    def event(self, seq=1, task='T01', op='create', values=None):
        if values is None:
            values={'title':'Migration tests','status':'open','owner':'Alex','due':'2026-11-12',
                    'priority':1,'release_required':True,'dependencies':[]}
        return {'seq':seq,'id':task,'op':op,'values':values,'reason':'Recorded project event',
                'source':{'path':'.memory/research/events.md','quote':'E001: Release task is required.'}}

    def apply(self, event, ok=True):
        (self.root/'event.json').write_text(json.dumps(event))
        return self.cli('tasks','apply','event.json',ok=ok)

    def test_missing_required_field_is_rejected_before_capture(self):
        event=self.event();del event['values']['release_required']
        result=self.apply(event,ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('release_required', result.stderr)
        self.assertFalse((self.root/'.memory/wiki/task-ledger.md').exists())

    def test_explicit_unknown_never_becomes_false(self):
        event=self.event();event['values']['release_required']=None
        self.apply(event)
        task=json.loads(self.cli('tasks','show','T01').stdout)['tasks']['T01']
        self.assertIsNone(task['values']['release_required'])
        self.assertEqual(task['unknown_fields'],['release_required'])
        self.assertFalse(task['complete'])
        self.assertNotEqual(self.cli('tasks','check','--complete',ok=False).returncode,0)

    def test_new_owner_update_preserves_all_unchanged_fields_and_sources(self):
        self.apply(self.event())
        event=self.event(2,op='update',values={'owner':'Mei'})
        event['source']['quote']='E002: Owner changes to Mei.'
        self.apply(event)
        output=json.loads(self.cli('tasks','show').stdout)
        task=output['tasks']['T01']
        self.assertTrue(task['values']['release_required'])
        self.assertEqual(task['values']['owner'],'Mei')
        self.assertEqual(task['field_events']['release_required']['seq'],1)
        self.assertEqual(task['field_events']['owner']['seq'],2)

    def test_field_types_and_duplicate_ids_are_not_coerced(self):
        event=self.event();event['values']['release_required']='false'
        self.assertNotEqual(self.apply(event,ok=False).returncode,0)
        self.apply(self.event())
        self.assertNotEqual(self.apply(self.event(2),ok=False).returncode,0)
        self.assertEqual(json.loads(self.cli('tasks','show').stdout)['revision'],1)

    def test_all_required_fields_survive_navigation_compaction(self):
        for i in range(1,25):
            event=self.event(i,task=f'T{i:02d}')
            event['values']['release_required']=i<=17
            event['values']['status']='done' if i<=2 else 'open'
            self.apply(event)
        (self.root/'.memory/wiki/index.md').write_text('# Index\n(compacted)\n')
        tasks=json.loads(self.cli('tasks','show').stdout)['tasks']
        obligations=[k for k,t in tasks.items() if t['values']['release_required'] is True
                     and t['values']['status'] not in ('done','cancelled')]
        self.assertEqual(len(obligations),15)
        self.assertEqual(len(tasks),24)
        self.assertTrue(all(t['complete'] for t in tasks.values()))

    def test_lock_and_stale_sequence_leave_ledger_unchanged(self):
        self.apply(self.event())
        ledger=self.root/'.memory/wiki/task-ledger.md'
        before=ledger.read_bytes()
        self.assertNotEqual(self.apply(self.event(),ok=False).returncode,0)
        lock=self.root/'.memory/wiki/.task-ledger.lock';lock.touch()
        self.assertNotEqual(self.apply(self.event(2,op='update',values={'owner':'Mei'}),ok=False).returncode,0)
        self.assertEqual(ledger.read_bytes(),before)

    def test_missing_ledger_is_not_a_complete_empty_inventory(self):
        output=json.loads(self.cli('tasks','show').stdout)
        self.assertFalse(output['tracked'])
        self.assertFalse(output['complete'])

    def test_staged_history_rewrite_is_rejected_by_real_commit_validator(self):
        def git(*args):
            subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True)
        git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        self.apply(self.event())
        git('add','.');git('commit','-qm','Capture task')
        ledger=self.root/'.memory/wiki/task-ledger.md'
        ledger.write_text(ledger.read_text().replace('"release_required": true','"release_required": false'))
        git('add','.memory/wiki/task-ledger.md')
        result=subprocess.run([sys.executable,'.memory/tools/pre_commit_guard.py'],cwd=self.root,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('append-only',result.stdout+result.stderr)

    def test_bulk_dependency_replacement_cannot_silently_drop_direct_edge(self):
        self.apply(self.event(1,task='T24'))
        middle=self.event(2,task='T14');middle['values']['dependencies']=['T24']
        self.apply(middle)
        launch=self.event(3,task='T18');launch['values']['dependencies']=['T14','T24']
        self.apply(launch)
        result=self.apply(self.event(4,task='T18',op='update',values={'dependencies':['T14']}),ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('link',result.stderr)

    def test_remove_one_edge_preserves_other_direct_and_indirect_constraints(self):
        events=[]
        for seq,key in enumerate(('T24','T23','T14','T18'),1):
            e=self.event(seq,task=key)
            if key=='T14': e['values'].update(status='done',dependencies=['T24'])
            if key=='T18': e['values']['dependencies']=['T14','T23','T24']
            events.append(e)
        self.apply(events)
        self.apply(self.event(5,task='T18',op='unlink',values={'target':'T23','kind':'hard'}))
        view=json.loads(self.cli('tasks','show').stdout)
        launch=view['tasks']['T18']
        self.assertEqual(launch['values']['dependencies'],['T14','T24'])
        self.assertEqual(launch['readiness']['blocked_by'],['T24'])
        self.assertNotIn('T18',view['ready_now'])
        self.apply(self.event(6,task='T24',op='update',values={'status':'done'}))
        self.assertIn('T18',json.loads(self.cli('tasks','show').stdout)['ready_now'])
        self.apply(self.event(7,task='T24',op='update',values={'status':'open'}))
        self.assertNotIn('T18',json.loads(self.cli('tasks','show').stdout)['ready_now'])

    def test_new_advisory_link_does_not_block_but_hard_link_does(self):
        self.apply([self.event(1,task='BUILD'),self.event(2,task='REVIEW')])
        self.apply(self.event(3,task='BUILD',op='link',values={'target':'REVIEW','kind':'soft'}))
        self.assertIn('BUILD',json.loads(self.cli('tasks','show').stdout)['ready_now'])
        self.apply(self.event(4,task='BUILD',op='link',values={'target':'REVIEW','kind':'hard'}))
        self.assertNotIn('BUILD',json.loads(self.cli('tasks','show').stdout)['ready_now'])

    def test_cycles_unknown_targets_and_missing_removals_are_atomic_failures(self):
        self.apply([self.event(1,task='A'),self.event(2,task='B')])
        self.apply(self.event(3,task='A',op='link',values={'target':'B','kind':'hard'}))
        before=(self.root/'.memory/wiki/task-ledger.md').read_bytes()
        for key,target,op in [('B','A','link'),('A','MISSING','link'),('B','A','unlink'),('A','A','link')]:
            with self.subTest(key=key,target=target,op=op):
                self.assertNotEqual(self.apply(self.event(4,task=key,op=op,values={'target':target,'kind':'hard'}),ok=False).returncode,0)
                self.assertEqual((self.root/'.memory/wiki/task-ledger.md').read_bytes(),before)

    def test_readiness_status_truth_table_includes_unknown_and_closed(self):
        self.apply([self.event(1,task='A'),self.event(2,task='B')])
        self.apply(self.event(3,task='A',op='link',values={'target':'B','kind':'hard'}))
        cases=[('open','open','blocked'),('open','done','ready'),('open','cancelled','blocked'),
               ('open',None,'unknown'),('done','done','not_open'),('in_progress','done','not_open'),
               ('blocked','done','not_open'),(None,'done','unknown')]
        seq=4
        for own,dependency,expected in cases:
            with self.subTest(own=own,dependency=dependency):
                self.apply([self.event(seq,task='A',op='update',values={'status':own}),
                            self.event(seq+1,task='B',op='update',values={'status':dependency})])
                seq+=2
                view=json.loads(self.cli('tasks','show','A').stdout)
                self.assertEqual(view['tasks']['A']['readiness']['state'],expected)

    def test_current_snapshot_has_watermark_and_rejects_stale_publication(self):
        self.apply(self.event())
        current=self.cli('tasks','current').stdout
        (self.root/'snapshot.json').write_text(current)
        self.cli('tasks','verify','snapshot.json')
        self.apply(self.event(2,op='update',values={'owner':'Mei'}))
        self.assertNotEqual(self.cli('tasks','verify','snapshot.json',ok=False).returncode,0)
        new=json.loads(self.cli('tasks','current').stdout)
        self.assertEqual(new['revision'],2)
        self.assertEqual(new['tasks']['T01']['owner'],'Mei')

    def test_current_and_history_agree_without_replaying_prose(self):
        self.apply(self.event())
        self.apply(self.event(2,op='update',values={'owner':'Mei','due':'2026-12-01'}))
        before=json.loads(self.cli('tasks','at','1','T01').stdout)
        now=json.loads(self.cli('tasks','current','T01').stdout)
        detailed=json.loads(self.cli('tasks','show','T01').stdout)
        self.assertEqual(before['mode'],'historical')
        self.assertEqual(before['tasks']['T01']['owner'],'Alex')
        self.assertEqual(now['tasks']['T01']['owner'],'Mei')
        for key,value in detailed['tasks']['T01']['values'].items():
            self.assertEqual(now['tasks']['T01'][key],value)
        self.assertEqual(len(json.loads(self.cli('tasks','history','T01').stdout)['events']),2)
        self.assertNotEqual(self.cli('tasks','at','99',ok=False).returncode,0)
        (self.root/'old.json').write_text(json.dumps(before))
        self.assertNotEqual(self.cli('tasks','verify','old.json',ok=False).returncode,0)

    def test_current_view_size_does_not_grow_with_replaced_field_history(self):
        events=[self.event()]
        for seq in range(2,42):
            events.append(self.event(seq,op='update',values={'owner':f'Owner-{seq}'}))
        self.apply(events)
        compact=self.cli('tasks','current').stdout
        history=self.cli('tasks','history').stdout
        self.assertLess(len(compact),len(history)//3)
        self.assertEqual(json.loads(compact)['tasks']['T01']['owner'],'Owner-41')

    def test_snapshot_verification_rejects_type_coercion_and_invented_values(self):
        self.apply(self.event())
        snapshot=json.loads(self.cli('tasks','current').stdout)
        snapshot['tasks']['T01']['release_required']=1
        (self.root/'snapshot.json').write_text(json.dumps(snapshot))
        self.assertNotEqual(self.cli('tasks','verify','snapshot.json',ok=False).returncode,0)

    def test_unknown_status_is_uncertain_not_an_open_obligation(self):
        event=self.event();event['values']['status']=None
        self.apply(event)
        view=json.loads(self.cli('tasks','current').stdout)
        self.assertEqual(view['open_release_obligations'],[])
        self.assertEqual(view['uncertain_obligations'],['T01'])

    def test_installed_resume_and_update_preserve_task_knowledge(self):
        self.root=self.root/'installed';self.root.mkdir()
        def run(*args):
            result=subprocess.run(args,cwd=self.root,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return result.stdout
        run('git','init','-q');run('git','config','user.name','Test')
        run('git','config','user.email','test@example.invalid')
        run(sys.executable,str(HERE/'install.py'),'task-test','--codex','--portable')
        (self.root/'.memory/research').mkdir(exist_ok=True)
        (self.root/'.memory/research/events.md').write_text('E001: Release task is required.\n')
        self.apply(self.event())
        output=self.cli('resume').stdout
        self.assertIn('open release obligations 1',output)
        self.assertIn('STATE idle does not cancel obligations',output)
        run('git','add','.');run('git','commit','-qm','Capture task')
        ledger=self.root/'.memory/wiki/task-ledger.md';before=ledger.read_bytes()
        run(sys.executable,str(HERE/'update.py'),'--portable')
        self.assertEqual(before,ledger.read_bytes())
        self.assertEqual(json.loads(self.cli('tasks','current').stdout)['open_release_obligations'],['T01'])

    def test_capture_rejects_a_quote_that_the_existing_source_does_not_contain(self):
        event=self.event();event['source']['quote']='Invented approval that is not in the source.'
        result=self.apply(event,ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('quote',result.stderr)

    def test_commit_checks_staged_source_not_unstaged_repair(self):
        def git(*args): subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True)
        git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        git('add','.');git('commit','-qm','Initial tools and sources')
        self.apply(self.event())
        source=self.root/'.memory/research/events.md';original=source.read_text()
        source.write_text('Nothing here supports the claimed quote.\n')
        git('add','.memory')
        source.write_text(original)
        result=subprocess.run([sys.executable,'.memory/tools/pre_commit_guard.py'],cwd=self.root,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('quote mismatch',result.stdout+result.stderr)

    def test_filed_excerpt_survives_source_change_with_visible_warning(self):
        def git(*args): subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True)
        git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        self.apply(self.event());git('add','.');git('commit','-qm','Capture sourced task')
        source=self.root/'.memory/research/events.md';source.write_text('Revised source content.\n')
        git('add','.memory/research/events.md')
        result=subprocess.run([sys.executable,'.memory/tools/pre_commit_guard.py'],cwd=self.root,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        view=json.loads(self.cli('tasks','current').stdout)
        self.assertEqual(view['tasks']['T01']['owner'],'Alex')
        self.assertEqual(view['sources']['1']['current_file']['status'],'historical_excerpt')
        source.unlink()
        view=json.loads(self.cli('tasks','current').stdout)
        self.assertEqual(view['sources']['1']['current_file']['status'],'source_unavailable')

    def test_duplicate_json_fields_are_not_silently_collapsed(self):
        text=json.dumps(self.event()).replace('"release_required": true','"release_required": true, "release_required": false')
        (self.root/'event.json').write_text(text)
        result=self.cli('tasks','apply','event.json',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('duplicate JSON key',result.stderr)

    def test_checkpoint_accepts_filed_excerpt_after_mid_session_source_change(self):
        self.root=self.root/'installed';self.root.mkdir()
        def run(*args):
            result=subprocess.run(args,cwd=self.root,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run('git','init','-q');run('git','config','user.name','Test')
        run('git','config','user.email','test@example.invalid')
        run(sys.executable,str(HERE/'install.py'),'historical-source','--codex','--portable')
        source=self.root/'.memory/research/events.md'
        source.parent.mkdir(exist_ok=True)
        source.write_text('E001: Release task is required.\n')
        self.apply(self.event())
        run('git','add','.');run('git','commit','-qm','File task between checkpoints')
        source.write_text('Revised source content.\n')
        run('git','add','.');run('git','commit','-qm','Revise mutable source')
        self.cli('guard')
        # A new, unfiled event still needs a quote in the current source.
        source.write_text('E001: Release task is required.\n')
        self.apply(self.event(2,op='update',values={'owner':'Mei'}))
        source.write_text('Revised source content.\n')
        result=self.cli('guard',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('quote mismatch: event 2',result.stdout+result.stderr)


if __name__=='__main__': unittest.main()
