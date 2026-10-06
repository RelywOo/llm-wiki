"""Shared WikiInit production delivery tests. Real Git guards; disposable projects.

WIKIINIT_SOURCE selects the shared distribution. WIKIINIT_PREVIOUS_DISTRIBUTION
optionally supplies the backed-up older release for a genuine upgrade replay.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE=Path(os.environ.get('WIKIINIT_SOURCE',Path(__file__).resolve().parent))
PREVIOUS=Path(os.environ.get('WIKIINIT_PREVIOUS_DISTRIBUTION',HERE))
FILES=['checkpoint_inventory.py','checkpoint_background.py','checkpoint_watchdog.py','checkpoint_package.py','consult_nudge.py']
SYSTEMD=(sys.platform=='linux' and shutil.which('systemctl') and subprocess.run(['systemctl','--user','show-environment'],capture_output=True).returncode==0)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wiki-distribution-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        self.units=set();self.addCleanup(self.cleanup_units)
        self.run_cmd('git','init','-q')
        self.run_cmd('git','config','user.name','Distribution test')
        self.run_cmd('git','config','user.email','test@example.invalid')

    def run_cmd(self,*args,expected=0,env=None):
        if len(args) > 1 and Path(args[1]).name in ("install.py", "update.py"):
            args = (*args, "--portable")
        r=subprocess.run(args,cwd=self.root,capture_output=True,text=True,env=env)
        self.assertEqual(r.returncode,expected,r.stdout+r.stderr)
        return r.stdout+r.stderr

    def unit(self,root=None):
        root=root or self.root
        name='jev-checkpoint-watchdog-'+hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
        self.units.add(name);return name

    def cleanup_units(self):
        for name in self.units:
            directory=Path.home()/'.config/systemd/user'
            owned=[directory/(name+'.'+suffix) for suffix in ('timer','service')]
            if any(p.exists() and p.read_text().startswith('# Managed by JEV checkpoint watchdog') for p in owned):
                subprocess.run(['systemctl','--user','disable','--now',name+'.timer'],capture_output=True)
                subprocess.run(['systemctl','--user','stop',name+'.service'],capture_output=True)
                for p in owned:
                    if p.exists() and p.read_text().startswith('# Managed by JEV checkpoint watchdog'):p.unlink()
        if self.units:subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)

    def install(self,*extra,source=HERE,expected=0,env=None):
        if '--watchdog' in extra:self.unit()
        return self.run_cmd(sys.executable,str(source/'install.py'),'delivery','--codex',*extra,expected=expected,env=env)

    def update(self,*extra,expected=0,env=None):
        if '--watchdog' in extra:self.unit()
        return self.run_cmd(sys.executable,str(HERE/'update.py'),*extra,expected=expected,env=env)

    def knowledge(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes() for p in (self.root/'.memory').rglob('*')
                if p.is_file() and ('.memory/wiki/' in p.as_posix() or p.name in ('STATE.md','checkpoint-cursors.json','inventory.json'))}

    def delivered(self,legacy=False):
        for name in FILES:self.assertEqual((self.root/'.memory/tools'/name).read_bytes(),(HERE/'templates/tree/.memory/tools'/name).read_bytes())
        self.assertTrue((self.root/'.memory/checkpoint-background.md').is_file())
        commands=self.root/('.claude/commands' if legacy else '.memory/commands')
        self.assertIn('CODEX BACKGROUND MODE',(commands/'wiki-checkpoint.md').read_text())
        self.assertIn('checkpoint_watchdog.py',(commands/'wiki-resume.md').read_text())
        self.assertIn('CODEX BACKGROUND MODE',(self.root/'.agents/skills/wiki-checkpoint/SKILL.md').read_text())
        self.run_cmd(sys.executable,'.memory/tools/dual_sync.py','--check')
        self.run_cmd(sys.executable,'.memory/tools/install_hooks.py','--check')

    def test_fresh_foreground_delivery_and_all_stage_preflight(self):
        output=self.install();self.delivered()
        self.assertIn('watchdog not activated',output)
        self.assertFalse((self.root/'.git/wiki-background-monitor/registration.json').exists())
        self.assertFalse((self.root/'.memory/inventory.json').exists())
        self.run_cmd(sys.executable,'-c',"import sys; sys.path.insert(0,'.memory/tools'); import checkpoint_inventory as i; p={'scenario':'Test','records':[{'ref':'R1','role':'user','text':('A complete finding. '*850).strip()}],'prior_memory':[]}; list(i.requests(p,i.split(p['records']),'explicit_full','kind'));\ntry: i.preflight(p)\nexcept ValueError as e: assert 'no API calls made' in str(e)\nelse: raise AssertionError('late-stage oversize was accepted')")
        head=self.run_cmd('git','rev-parse','HEAD');self.update();self.assertEqual(head,self.run_cmd('git','rev-parse','HEAD'))

    def test_upgrade_preserves_knowledge_cursors_backend_custom_skills_and_inbox(self):
        self.install(source=PREVIOUS)
        if PREVIOUS==HERE:
            for name in ('checkpoint_background.py','checkpoint_watchdog.py'):(self.root/'.memory/tools'/name).unlink()
        (self.root/'.memory/checkpoint-cursors.json').write_text('{"schema":1,"threads":{}}\n')
        (self.root/'.memory/inventory.json').write_text('{"backend":"jev-combined"}\n')
        custom=self.root/'.agents/skills/my-custom/SKILL.md';custom.parent.mkdir(parents=True);custom.write_text('User-owned skill.\n')
        helper=self.root/'.memory/tools/my_helper.py';helper.write_text('# user-owned\n')
        inbox=self.root/'.git/wiki-background-monitor/state.json';inbox.parent.mkdir(parents=True);inbox.write_text('{"schema":1,"events":{},"observations":{}}\n')
        self.run_cmd('git','add','.memory/checkpoint-cursors.json','.memory/inventory.json','.memory/tools','.agents/skills/my-custom')
        self.run_cmd('git','commit','-qm','Add preserved project data')
        before=self.knowledge();old_inbox=inbox.read_bytes();cfg=(self.root/'.memory/runtimes.json').read_bytes()
        self.update();self.delivered()
        self.assertEqual(before,self.knowledge());self.assertEqual(inbox.read_bytes(),old_inbox)
        self.assertEqual((self.root/'.memory/runtimes.json').read_bytes(),cfg)
        self.assertEqual(custom.read_text(),'User-owned skill.\n');self.assertEqual(helper.read_text(),'# user-owned\n')
        head=self.run_cmd('git','rev-parse','HEAD');self.update();self.assertEqual(head,self.run_cmd('git','rev-parse','HEAD'))

    def test_legacy_layout_upgrade(self):
        script="import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import install as i; r=Path.cwd(); i.install_tree(r,'delivery'); i.merge_settings(r); i.claude_md(r,'delivery'); i.install_codex(r,'delivery'); i.run_dual_sync(r)"
        self.run_cmd(sys.executable,'-c',script,str(PREVIOUS))
        self.run_cmd('git','add','.');self.run_cmd('git','commit','-qm','Legacy installation')
        self.run_cmd(sys.executable,'.memory/tools/install_hooks.py')
        before=self.knowledge();self.update();self.delivered(legacy=True)
        self.assertEqual(before,self.knowledge());self.assertFalse((self.root/'.memory/runtimes.json').exists())
        head=self.run_cmd('git','rev-parse','HEAD');self.update();self.assertEqual(head,self.run_cmd('git','rev-parse','HEAD'))

    def test_dirty_new_tools_and_guide_refused(self):
        self.install()
        for relative in ['.memory/tools/checkpoint_background.py','.memory/tools/checkpoint_watchdog.py','.memory/checkpoint-background.md']:
            p=self.root/relative;original=p.read_bytes();p.write_bytes(original+b'\nLocal unsaved change.\n')
            head=self.run_cmd('git','rev-parse','HEAD');before=self.knowledge()
            self.assertIn('uncommitted',self.update(expected=3))
            self.assertEqual(p.read_bytes(),original+b'\nLocal unsaved change.\n');self.assertEqual(before,self.knowledge())
            self.assertEqual(head,self.run_cmd('git','rev-parse','HEAD'));p.write_bytes(original)

    def test_watchdog_requires_explicit_codex_selection(self):
        output=self.run_cmd(sys.executable,str(HERE/'install.py'),'delivery','--watchdog',expected=3)
        self.assertIn('requires an enabled Codex',output)
        self.assertFalse((self.root/'.memory').exists())

    def test_staged_unrelated_work_is_not_adopted_by_install_or_update(self):
        p=self.root/'user.txt';p.write_text('User work.\n');self.run_cmd('git','add','user.txt')
        self.assertIn('staged',self.install(expected=3));self.assertFalse((self.root/'.memory').exists())
        self.run_cmd('git','commit','-qm','Preserve user work');self.install()
        p.write_text('More user work.\n');self.run_cmd('git','add','user.txt')
        index=self.run_cmd('git','ls-files','--stage');self.assertIn('staged',self.update(expected=3))
        self.assertEqual(index,self.run_cmd('git','ls-files','--stage'))

    def test_pending_checkpoint_and_transaction_block_update(self):
        self.install()
        for relative in ['.git/wiki-background/active','.git/wiki-checkpoint-txn']:
            p=self.root/relative;p.mkdir(parents=True)
            self.assertIn('unfinished checkpoint',self.update(expected=3));p.rmdir()

    def broken_systemd(self,allow_probe=False):
        bin_dir=Path(self.tmp.name)/'bin';bin_dir.mkdir(exist_ok=True)
        p=bin_dir/'systemctl';p.write_text('#!/bin/sh\n'+('if [ "$2" = "show-environment" ]; then exit 0; fi\n' if allow_probe else '')+'exit 1\n');p.chmod(0o755)
        return {**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH']}

    def test_unavailable_requested_watchdog_refuses_before_writes(self):
        output=self.install('--watchdog',expected=3,env=self.broken_systemd())
        self.assertIn('before project writes',output);self.assertFalse((self.root/'.memory').exists())
        self.install(env=self.broken_systemd());self.delivered()

    @unittest.skipUnless(SYSTEMD,'Linux user systemd required')
    def test_fresh_watchdog_activation(self):
        self.install('--watchdog');self.delivered()
        output=self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','health');self.assertIn('true',output)
        before=self.knowledge();self.update();self.assertEqual(before,self.knowledge())

    @unittest.skipUnless(SYSTEMD,'Linux user systemd required')
    def test_watchdog_refresh_preserves_disabled_state_and_inbox(self):
        self.install()
        p=self.root/'.memory/tools/checkpoint_watchdog.py';p.write_text(p.read_text()+'\n# Older runtime fixture.\n')
        self.run_cmd('git','add',str(p.relative_to(self.root)));self.run_cmd('git','commit','-qm','Older watchdog fixture')
        self.unit();self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','install')
        reg=self.root/'.git/wiki-background-monitor/registration.json';old=json.loads(reg.read_text())
        self.run_cmd('systemctl','--user','disable','--now',self.unit()+'.timer')
        inbox=self.root/'.git/wiki-background-monitor/state.json';saved=inbox.read_bytes()
        self.update();new=json.loads(reg.read_text());self.assertNotEqual(old['runtime'],new['runtime'])
        self.assertNotEqual(subprocess.run(['systemctl','--user','is-enabled','--quiet',self.unit()+'.timer']).returncode,0)
        self.assertNotEqual(subprocess.run(['systemctl','--user','is-active','--quiet',self.unit()+'.timer']).returncode,0)
        self.assertEqual(inbox.read_bytes(),saved)
        self.assertIn('timer is not active',self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','health',expected=2))
        self.update('--watchdog');self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','health')

    @unittest.skipUnless(SYSTEMD,'Linux user systemd required')
    def test_watchdog_relocation_replaces_only_owned_units(self):
        self.install('--watchdog');old=self.unit()
        # Keep a valid portable Git hook path so the test isolates watchdog relocation.
        self.run_cmd('git','config','core.hooksPath','.git/wiki-memory-hooks/hooks')
        moved=Path(self.tmp.name)/'moved project';self.root.rename(moved);self.root=moved;new=self.unit()
        self.update();self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','health')
        self.assertFalse((Path.home()/'.config/systemd/user'/f'{old}.timer').exists())
        self.assertTrue((Path.home()/'.config/systemd/user'/f'{new}.timer').exists())

    @unittest.skipUnless(SYSTEMD,'Linux user systemd required')
    def test_foreign_timer_refused_before_project_writes(self):
        p=Path.home()/'.config/systemd/user'/(self.unit()+'.service')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# User-owned foreign service\n')
        self.addCleanup(p.unlink,missing_ok=True)
        output=self.install('--watchdog',expected=3)
        self.assertIn('foreign',output);self.assertFalse((self.root/'.memory').exists())
        self.assertEqual(p.read_text(),'# User-owned foreign service\n')

    @unittest.skipUnless(SYSTEMD,'Linux user systemd required')
    def test_post_commit_activation_failure_is_explicit_and_recoverable(self):
        output=self.install('--watchdog',expected=6,env=self.broken_systemd(allow_probe=True))
        self.assertIn('NOT ready',output);self.assertTrue((self.root/'.memory/wiki').exists())
        head=self.run_cmd('git','rev-parse','HEAD');before=self.knowledge()
        self.update('--watchdog');self.run_cmd(sys.executable,'.memory/tools/checkpoint_watchdog.py','health')
        self.assertEqual(head,self.run_cmd('git','rev-parse','HEAD'));self.assertEqual(before,self.knowledge())


if __name__=='__main__':unittest.main(verbosity=2)
