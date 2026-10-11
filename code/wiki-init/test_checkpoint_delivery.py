"""Verify shared checkpoint package delivery using disposable repositories."""
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
TREE = HERE / 'templates/tree'
FILES = ['.memory/tools/checkpoint_inventory.py', '.memory/checkpoint-inventory.md', '.memory/tools/checkpoint_collect.py', '.memory/tools/checkpoint_package.py', '.memory/tools/checkpoint_txn.py',
         '.memory/checkpoint-package.md']


class DeliveryTest(unittest.TestCase):
    def command(self, root, *args, expected=0):
        if len(args) > 1 and Path(args[1]).name in ("install.py", "update.py"):
            args = (*args, "--portable")
        result = subprocess.run(args, cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wiki-package-delivery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.command(self.root, 'git', 'init', '-q')
        self.command(self.root, 'git', 'config', 'user.name', 'Package delivery test')
        self.command(self.root, 'git', 'config', 'user.email', 'test@example.invalid')
        self.command(self.root, sys.executable, str(HERE/'install.py'), 'delivery-test', '--codex')

    def knowledge(self):
        paths = list((self.root/'.memory/wiki').rglob('*.md')) + [self.root/'.memory/STATE.md']
        return {str(p.relative_to(self.root)): p.read_bytes() for p in paths}

    def assert_delivered(self):
        for relative in FILES:
            self.assertEqual((self.root/relative).read_bytes(), (TREE/relative).read_bytes())
        self.assertIn('checkpoint_package.py', (self.root/'.agents/skills/wiki-checkpoint/SKILL.md').read_text())
        self.assertIn('checkpoint_collect.py --root .', (self.root/'.agents/skills/wiki-checkpoint/SKILL.md').read_text())
        self.assertIn('.memory/checkpoint-package.md', (self.root/'.memory/commands/wiki-checkpoint.md').read_text())
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_inventory.py', '--help')
        self.assertFalse((self.root/'.memory/inventory.json').exists())
        self.command(self.root, sys.executable, '.memory/tools/dual_sync.py', '--check')

    def test_fresh_install_executes_checkpoint_and_retains_draft(self):
        self.assert_delivered()
        before = self.knowledge()
        packet = json.loads(self.command(self.root, sys.executable, '.memory/tools/checkpoint_collect.py', '--root', '.'))
        self.assertEqual(packet['schema'], 1)
        self.assertNotIn('provenance', packet)
        self.assertNotIn('instruction_aliases', packet)
        self.assertTrue(packet['consistent'])
        self.assertEqual(packet['checkpoint'], packet['head'])
        self.assertEqual(self.knowledge(), before)
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'init',
                     '--name', 'first', '--block', 'First checkpoint', '--summary', 'Save fixture state')
        path = self.root/'.git/wiki-checkpoint-packages/first.json'
        package = json.loads(path.read_text())
        self.assertEqual(package['block'], 'First-checkpoint')
        state = next(e for e in package['files'] if e['path']=='.memory/STATE.md')
        state.update(action='write', content='# STATE\n\n## Now\n- Delivery fixture saved.\n')
        path.write_text(json.dumps(package))
        saved = path.read_bytes()
        before = self.knowledge()
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'check', '--name', 'first')
        self.assertEqual(self.knowledge(), before)
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'apply', '--name', 'first')
        self.assertEqual(path.read_bytes(), saved)
        self.command(self.root, sys.executable, '.memory/tools/structural.py', 'resume')
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'apply', '--name', 'first', expected=2)

    def prepare_draft(self, name='direct', extra=()):
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'init',
                     '--name', name, '--block', 'Direct application', '--summary', 'Save fixture state',
                     '--allow', *extra)
        path = self.root/f'.git/wiki-checkpoint-packages/{name}.json'
        package = json.loads(path.read_text())
        state = next(e for e in package['files'] if e['path'] == '.memory/STATE.md')
        state.update(action='write', content='# STATE\n\n## Now\n- Direct application fixture saved.\n')
        path.write_text(json.dumps(package))
        return path, package

    def test_direct_apply_matches_optional_preview_then_apply(self):
        path, _ = self.prepare_draft()
        saved = path.read_bytes()
        preview = self.root.parent/(self.root.name+'-preview')
        self.addCleanup(shutil.rmtree, preview, ignore_errors=True)
        shutil.copytree(self.root, preview)
        # The cloned fixture owns this copied absolute hook setting. Relocate it.
        self.command(preview, 'git', 'config', '--unset', 'core.hooksPath')
        self.command(preview, sys.executable, '.memory/tools/install_hooks.py')
        before = self.knowledge()
        self.command(preview, sys.executable, '.memory/tools/checkpoint_package.py',
                     'check', '--name', 'direct')
        for relative, content in before.items():
            self.assertEqual((preview/relative).read_bytes(), content)
        self.command(preview, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct')
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct')
        for relative, content in self.knowledge().items():
            self.assertEqual((preview/relative).read_bytes(), content)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual((preview/'.git/wiki-checkpoint-packages/direct.json').read_bytes(), saved)
        self.assertFalse((self.root/'.git/wiki-checkpoint-txn').exists())
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)

    def test_direct_apply_rejects_invalid_draft_without_writes(self):
        path, package = self.prepare_draft()
        state = next(e for e in package['files'] if e['path'] == '.memory/STATE.md')
        state['content'] = '# STATE\n\n## Noncanonical section\n- Invalid.\n'
        path.write_text(json.dumps(package))
        before = self.knowledge(); saved = path.read_bytes()
        head = self.command(self.root, 'git', 'rev-parse', 'HEAD')
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.command(self.root, 'git', 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.root/'.git/wiki-checkpoint-txn').exists())

    def test_direct_apply_rejects_stale_target_and_head(self):
        path, _ = self.prepare_draft()
        state = self.root/'.memory/STATE.md'
        original = state.read_bytes()
        state.write_text('# STATE\n\n## Now\n- Concurrent edit.\n')
        before = self.knowledge(); saved = path.read_bytes()
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)
        self.assertEqual(self.knowledge(), before)
        state.write_bytes(original)
        self.command(self.root, 'git', 'commit', '--allow-empty', '-qm', 'Move baseline')
        before = self.knowledge()
        head = self.command(self.root, 'git', 'rev-parse', 'HEAD')
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.command(self.root, 'git', 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.root/'.git/wiki-checkpoint-txn').exists())

    def test_direct_apply_rechecks_after_begin_and_preserves_concurrent_edit(self):
        path, _ = self.prepare_draft()
        saved = path.read_bytes(); before = self.knowledge()
        script = """import sys
from pathlib import Path
sys.path.insert(0, str(Path('.memory/tools').resolve()))
import checkpoint_package as package
begin = package.txn.begin
def concurrent_begin(root, allow):
    begin(root, allow)
    (root / '.memory/STATE.md').write_text('# STATE\\n\\n## Now\\n- Concurrent edit after BEGIN.\\n')
package.txn.begin = concurrent_begin
raise SystemExit(package.main(['apply', '--name', 'direct']))
"""
        self.command(self.root, sys.executable, '-c', script, expected=2)
        before['.memory/STATE.md'] = b'# STATE\n\n## Now\n- Concurrent edit after BEGIN.\n'
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(path.read_bytes(), saved)
        self.assertFalse((self.root/'.git/wiki-checkpoint-txn').exists())

    def test_direct_apply_rolls_back_guard_failure_and_retains_draft(self):
        target = '.memory/wiki/concepts/unsupported.md'
        path, package = self.prepare_draft(extra=(target,))
        entry = next(e for e in package['files'] if e['path'] == target)
        entry.update(action='write', content='# Unsupported\n\n## Claim\nInvented claim without a source.\n')
        path.write_text(json.dumps(package))
        saved = path.read_bytes(); before = self.knowledge()
        head = self.command(self.root, 'git', 'rev-parse', 'HEAD')
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.command(self.root, 'git', 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.root/target).exists())
        self.assertFalse((self.root/'.git/wiki-checkpoint-txn').exists())

    def test_direct_apply_requires_recovery_of_interrupted_transaction(self):
        path, _ = self.prepare_draft()
        saved = path.read_bytes(); before = self.knowledge()
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_txn.py', 'begin',
                     '--allow', '.memory/STATE.md', '.memory/wiki/index.md', '.memory/wiki/log.md')
        (self.root/'.memory/STATE.md').write_text('# STATE\n\n## Now\n- Interrupted write.\n')
        interrupted = self.knowledge()
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct', expected=2)
        self.assertEqual(self.knowledge(), interrupted)
        self.assertTrue((self.root/'.git/wiki-checkpoint-txn').exists())
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_txn.py', 'recover')
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(path.read_bytes(), saved)
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py',
                     'apply', '--name', 'direct')
        self.assertEqual(path.read_bytes(), saved)

    def test_transaction_selfcheck_uses_its_own_state_fixture(self):
        for content in ('# STATE\n\n## Open\n- Verification remains open.\n',
                        '# STATE\n## Next\n' + '- Pending work.\n' * 38):
            with self.subTest(lines=len(content.splitlines())):
                (self.root/'.memory/STATE.md').write_text(content)
                before = self.knowledge()
                head = self.command(self.root, 'git', 'rev-parse', 'HEAD')
                status = self.command(self.root, 'git', 'status', '--porcelain')
                result = self.command(self.root, sys.executable, '.memory/tools/checkpoint_txn.py', 'selfcheck')
                self.assertIn('selfcheck OK', result)
                self.assertEqual(self.knowledge(), before)
                self.assertEqual(self.command(self.root, 'git', 'rev-parse', 'HEAD'), head)
                self.assertEqual(self.command(self.root, 'git', 'status', '--porcelain'), status)

    def test_generic_state_scores_and_opt_in_legacy_tracker(self):
        path, package = self.prepare_draft()
        entry = next(e for e in package['files'] if e['path'] == '.memory/STATE.md')
        entry['content'] = '# STATE\n\n## Now\n- PostgreSQL migration: 3/5 checks passed.\n'
        path.write_text(json.dumps(package))
        self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'apply', '--name', 'direct')
        output = self.command(self.root, sys.executable, '.memory/tools/structural.py', 'resume')
        self.assertNotIn('РУТИНА', output)
        tracker = self.root/'.memory/tracker.md'
        tracker.write_text('# Routine\nstart: 2026-10-12\n2026-10-12 comments=3/3\n')
        path, package = self.prepare_draft(name='routine')
        entry = next(e for e in package['files'] if e['path'] == '.memory/STATE.md')
        entry['content'] = '# STATE\n\n## Now\n- comments 3/3 today.\n'
        path.write_text(json.dumps(package))
        before = self.knowledge()
        output = self.command(self.root, sys.executable, '.memory/tools/checkpoint_package.py', 'apply', '--name', 'routine', expected=2)
        self.assertIn('routine retold', output)
        self.assertEqual(self.knowledge(), before)
        self.assertIn('РУТИНА', self.command(self.root, sys.executable, '.memory/tools/structural.py', 'resume'))
        saved = tracker.read_bytes()
        self.command(self.root, sys.executable, '.memory/tools/structural.py', 'tracker')
        self.assertEqual(tracker.read_bytes(), saved)

    def test_upgrade_delivers_missing_files_without_changing_knowledge(self):
        # Model a committed older installation that has no package layer.
        for relative in ['.memory/tools/checkpoint_inventory.py', '.memory/checkpoint-inventory.md', '.memory/tools/checkpoint_collect.py', '.memory/tools/checkpoint_package.py', '.memory/checkpoint-package.md']:
            (self.root/relative).unlink()
        (self.root/'.memory/commands/wiki-checkpoint.md').write_text(
            '---\ndescription: Legacy checkpoint\n---\n\nUse the transaction coordinator.\n')
        self.command(self.root, sys.executable, '.memory/tools/dual_sync.py', '--sync')
        self.command(self.root, 'git', 'add', '-A')
        self.command(self.root, 'git', 'commit', '-qm', 'Older checkpoint interface fixture')
        before = self.knowledge()
        self.command(self.root, sys.executable, str(HERE/'update.py'))
        self.assert_delivered()
        self.assertEqual(self.knowledge(), before)
        head = self.command(self.root, 'git', 'rev-parse', 'HEAD')
        self.command(self.root, sys.executable, str(HERE/'update.py'))
        self.assertEqual(self.command(self.root, 'git', 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.knowledge(), before)

    def test_collector_captures_committed_and_dirty_work_without_writing(self):
        source = self.root/'code/example.py'
        source.write_text('value = 1\n')
        self.command(self.root, 'git', 'add', 'code/example.py')
        self.command(self.root, 'git', 'commit', '-qm', 'Add session work')
        source.write_text('value = 2\n')
        self.command(self.root, 'git', 'add', 'code/example.py')
        source.write_text('value = 3\n')
        research = self.root/'.memory/research/new evidence.md'
        research.parent.mkdir(parents=True, exist_ok=True)
        research.write_text('Untracked supporting evidence.\n')
        before = self.knowledge()
        status = self.command(self.root, 'git', 'status', '--porcelain')
        packet = json.loads(self.command(self.root, sys.executable, '.memory/tools/checkpoint_collect.py', '--root', '.'))
        for layer in ('committed', 'staged', 'unstaged'):
            self.assertIn('code/example.py', packet['changes'][layer])
        self.assertIn('.memory/research/new evidence.md', packet['changes']['untracked'])
        self.assertIn('Untracked supporting evidence.', '\n'.join(item['text'] for item in packet['items']))
        self.assertEqual(self.knowledge(), before)
        self.assertEqual(self.command(self.root, 'git', 'status', '--porcelain'), status)

    def test_update_protects_local_collector_edits(self):
        helper = self.root/'.memory/tools/checkpoint_collect.py'
        helper.write_text(helper.read_text()+'\n# Local change must survive.\n')
        saved = helper.read_bytes()
        before = self.knowledge()
        output = self.command(self.root, sys.executable, str(HERE/'update.py'), expected=3)
        self.assertIn('.memory/tools/checkpoint_collect.py', output)
        self.assertEqual(helper.read_bytes(), saved)
        self.assertEqual(self.knowledge(), before)

    def test_update_protects_local_package_documentation_edits(self):
        doc = self.root/'.memory/checkpoint-package.md'
        doc.write_text(doc.read_text()+'\nLocal changes must survive.\n')
        saved = doc.read_bytes()
        before = self.knowledge()
        output = self.command(self.root, sys.executable, str(HERE/'update.py'), expected=3)
        self.assertIn('.memory/checkpoint-package.md', output)
        self.assertEqual(doc.read_bytes(), saved)
        self.assertEqual(self.knowledge(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
