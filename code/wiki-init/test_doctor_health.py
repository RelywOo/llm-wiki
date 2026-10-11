"""Doctor probes each configured guard once while retaining refusal semantics."""
from contextlib import ExitStack, contextmanager
import importlib.util
import itertools
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TOOLS = HERE / 'templates/tree/.memory/tools'


def load_structural(source):
    spec = importlib.util.spec_from_file_location('tested_doctor', source)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOLS))
    try:
        spec.loader.exec_module(module)
        __import__('install_hooks')  # Load the deferred dependency before restoring sys.path.
    finally:
        sys.path.pop(0)
    return module


structural = load_structural(TOOLS / 'structural.py')


class DoctorHealthTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wiki-doctor-health-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(HERE / 'templates/tree/.memory', self.root / '.memory',
                        ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('concepts', 'decisions'):
            (self.root / '.memory/wiki' / name).mkdir(exist_ok=True)

    def configure(self, claude=True, codex=True, command=None):
        (self.root / '.memory/runtimes.json').write_text(json.dumps(
            {'claude': claude, 'codex': codex, 'prime': False}))
        for runtime, relative, matcher in (
                ('claude', '.claude/settings.json', 'Edit|Write'),
                ('codex', '.codex/hooks.json', 'Edit|Write|apply_patch')):
            target = self.root / relative
            target.parent.mkdir(exist_ok=True)
            guard = command if command is not None else (
                shlex.quote(sys.executable) + ' ' +
                shlex.quote(str(self.root / '.memory/tools/wiki_guard.py')))
            # Two native hook entries: the write guard and an unrelated benign hook.
            hooks = {'PreToolUse': [{'matcher': matcher, 'hooks': [
                {'command': guard, 'commandWindows': 'WINDOWS_GUARD', 'timeout': 3}]}],
                'SessionStart': [{'matcher': 'compact', 'hooks': [
                    {'command': 'true', 'commandWindows': 'WINDOWS_COMPACT', 'timeout': 3}]}]}
            target.write_text(json.dumps({'hooks': hooks}))

    @contextmanager
    def isolated_checks(self):
        import install_hooks
        run = subprocess.run

        def selfcheck_result(args, **kwargs):
            if len(args) == 3 and args[-1] in ('--selfcheck', 'selfcheck'):
                return subprocess.CompletedProcess(args, 0, 'selfcheck OK\n', '')
            return run(args, **kwargs)

        # These unrelated checks are exercised for real by the distribution selfcheck.
        # Configured hook probes here use real subprocesses and real wiki_guard.py.
        with patch.object(install_hooks, 'health', return_value=[]), \
                patch('subprocess.run', side_effect=selfcheck_result) as calls:
            yield calls

    def memory(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in (self.root / '.memory').rglob('*.md')}

    def test_doctor_runs_one_deny_probe_per_enabled_guard_and_keeps_benign_probes(self):
        for claude, codex in itertools.product((False, True), repeat=2):
            with self.subTest(claude=claude, codex=codex):
                self.configure(claude, codex)
                before = self.memory()
                with self.isolated_checks() as processes, \
                        patch.object(structural, '_probe_hook', wraps=structural._probe_hook) as benign, \
                        patch.object(structural, '_probe_hook_output', wraps=structural._probe_hook_output) as deny:
                    fails, _, _ = structural.doctor(self.root, portable=True)
                self.assertEqual(fails, [])
                self.assertEqual(deny.call_count, int(claude) + int(codex))
                self.assertEqual(benign.call_count, 2 * (int(claude) + int(codex)))
                checks = [c.args[0] for c in processes.call_args_list
                          if c.args[0][-1] in ('selfcheck', '--selfcheck')]
                self.assertEqual(len(checks), 6)
                self.assertEqual(self.memory(), before)

    def test_doctor_and_boundary_health_refuse_non_enforcing_guards(self):
        allow = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
            'print(\'{"hookSpecificOutput":{"permissionDecision":"allow"}}\')')
        commands = ('true', 'exit 7', 'wiki-doctor-command-does-not-exist',
                    'printf invalid-json', allow)
        for runtime, command in itertools.product(('claude', 'codex'), commands):
            with self.subTest(runtime=runtime, command=command):
                self.configure(runtime == 'claude', runtime == 'codex', command)
                before = self.memory()
                with self.isolated_checks():
                    doctor_fails, _, _ = structural.doctor(self.root, portable=True)
                    boundary_fails, _, _ = structural.enforcement_health(self.root)
                for fails in (doctor_fails, boundary_fails):
                    self.assertTrue(any('did not deny a harmful rewrite' in f for f in fails), fails)
                self.assertEqual(self.memory(), before)

    def test_doctor_refuses_missing_guard_and_missing_or_invalid_registry(self):
        for runtime, defect in itertools.product(('claude', 'codex'), ('guard', 'missing', 'invalid')):
            with self.subTest(runtime=runtime, defect=defect):
                self.configure(runtime == 'claude', runtime == 'codex')
                registry = self.root / ('.claude/settings.json' if runtime == 'claude' else '.codex/hooks.json')
                if defect == 'guard':
                    data = json.loads(registry.read_text())
                    data['hooks']['PreToolUse'] = []
                    registry.write_text(json.dumps(data))
                elif defect == 'missing':
                    registry.unlink()
                else:
                    registry.write_text('{bad JSON')
                with self.isolated_checks():
                    fails, _, _ = structural.doctor(self.root, portable=True)
                self.assertTrue(fails)
                expected = 'not configured' if defect == 'guard' else (
                    'runtime missing' if defect == 'missing' else 'unparseable')
                self.assertTrue(any(expected in f for f in fails), fails)

    def test_missing_script_is_detected_even_when_shell_masks_its_exit(self):
        command = 'python "$CLAUDE_PROJECT_DIR/.memory/tools/missing-guard.py" || true'
        self.configure(claude=True, codex=False, command=command)
        with self.isolated_checks():
            fails, _, _ = structural.doctor(self.root, portable=True)
        self.assertTrue(any('referenced script missing' in f for f in fails), fails)
        self.assertTrue(any('did not deny a harmful rewrite' in f for f in fails), fails)

    def test_doctor_refuses_probe_timeout_and_missing_shell(self):
        self.configure(claude=False, codex=True)
        for result in ((-1, '', '', 'timeout'), (None, '', '', 'sh not found')):
            with self.subTest(result=result), self.isolated_checks(), \
                    patch.object(structural, '_probe_hook_output', return_value=result):
                fails, _, _ = structural.doctor(self.root, portable=True)
                self.assertTrue(any('did not deny a harmful rewrite' in f for f in fails), fails)

    def test_windows_codex_command_and_powershell_branch_are_preserved(self):
        self.configure(claude=False, codex=True)
        select = structural._codex_hook_command
        observed = []

        def select_windows(entry):
            return select(entry, windows=True)

        def windows_run(args, **kwargs):
            if len(args) == 3 and args[-1] in ('--selfcheck', 'selfcheck'):
                return subprocess.CompletedProcess(args, 0, 'selfcheck OK\n', '')
            observed.append(args)
            return subprocess.CompletedProcess(args, 0,
                b'{"hookSpecificOutput":{"permissionDecision":"deny"}}\n', b'')

        with ExitStack() as stack:
            stack.enter_context(patch.object(structural, '_codex_hook_command', side_effect=select_windows))
            stack.enter_context(patch('subprocess.run', side_effect=windows_run))
            stack.enter_context(patch('install_hooks.health', return_value=[]))
            fails, _, _ = structural.enforcement_health(self.root)
            self.assertEqual(fails, [])
            self.assertEqual(observed, [['pwsh', '-NoProfile', '-Command', 'WINDOWS_GUARD']])
            observed.clear()
            fails, _, _ = structural.doctor(self.root, portable=True)
        self.assertEqual(fails, [])
        self.assertEqual(observed, [
            ['pwsh', '-NoProfile', '-Command', 'WINDOWS_GUARD'],
            ['pwsh', '-NoProfile', '-Command', 'WINDOWS_COMPACT'],
            ['pwsh', '-NoProfile', '-Command', 'WINDOWS_GUARD']])
        self.assertEqual(select({'command': 'POSIX', 'commandWindows': 'WIN'}, windows=False), ('POSIX', False))
        self.assertEqual(select({'command': 'POSIX', 'commandWindows': 'WIN'}, windows=True), ('WIN', True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
