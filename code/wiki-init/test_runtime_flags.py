"""Integration coverage for independent runtimes; uses disposable Git repositories."""
import itertools
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class RuntimeFlagsTest(unittest.TestCase):
    def run_command(self, root, *args):
        if len(args) > 1 and Path(args[1]).name in ("install.py", "update.py"):
            args = (*args, "--portable")
        result = subprocess.run(args, cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_all_combinations_and_updates(self):
        for flags in itertools.product((False, True), repeat=3):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory(prefix="wiki-flags-") as tmp:
                root = Path(tmp)
                self.run_command(root, "git", "init", "-q")
                self.run_command(root, "git", "config", "user.name", "Wiki test")
                self.run_command(root, "git", "config", "user.email", "wiki-test@example.invalid")
                names = ("claude", "codex", "prime")
                options = ([f"--{name}" for name in names]
                           + [f"--no-{name}" for name, enabled in zip(names, flags) if not enabled]) if any(flags) else []
                self.run_command(root, sys.executable, str(HERE / "install.py"), "test-project", *options)
                expected = dict(zip(names, flags))
                self.assertEqual(json.loads((root / ".memory/runtimes.json").read_text()), expected)
                for name, rel in (("claude", ".claude"), ("codex", ".codex"), ("prime", ".prime")):
                    self.assertEqual((root / rel).exists(), expected[name], rel)
                self.assertEqual((root / "CLAUDE.md").exists(), flags[0])
                self.assertEqual((root / "AGENTS.md").exists(), flags[1])
                packet = json.loads(self.run_command(root, sys.executable, '.memory/tools/checkpoint_collect.py', '--root', '.'))
                self.assertEqual(packet['schema'], 1)
                self.assertTrue(packet['consistent'])
                self.assertEqual(packet['head'], packet['checkpoint'])
                knowledge = {p.relative_to(root): p.read_bytes() for p in (root / ".memory/wiki").rglob("*") if p.is_file()}
                knowledge[Path(".memory/STATE.md")] = (root / ".memory/STATE.md").read_bytes()
                head = self.run_command(root, "git", "rev-parse", "HEAD")
                self.run_command(root, sys.executable, str(HERE / "update.py"))
                current_head = self.run_command(root, "git", "rev-parse", "HEAD")
                if current_head != head:
                    self.fail("update must be idempotent: " + self.run_command(root, "git", "diff", "HEAD~", "HEAD"))
                for rel, content in knowledge.items():
                    self.assertEqual((root / rel).read_bytes(), content)
                if not any(flags):
                    self.run_command(root, sys.executable, str(HERE / "update.py"), "--claude", "--codex", "--prime")
                    self.assertTrue(all(json.loads((root / ".memory/runtimes.json").read_text()).values()))
                self.run_command(root, sys.executable, ".memory/tools/dual_sync.py", "--check")
                print(f"PASS runtime combination {expected}", flush=True)

    @unittest.skipUnless(sys.platform == "linux", "Linux PATH fixture")
    def test_portable_mode_does_not_weaken_normal_prime_check(self):
        env = {**os.environ, "PATH": "/usr/bin:/bin"}
        if shutil.which("prime-agent", path=env["PATH"]):
            self.skipTest("Prime CLI is present in fixture PATH")
        if not shutil.which("node", path=env["PATH"]):
            self.skipTest("Node is required for the adapter check")
        with tempfile.TemporaryDirectory(prefix="wiki-portable-") as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            tools = Path(tmp) / "bin"
            tools.mkdir()
            (tools / "python").symlink_to(sys.executable)
            env["PATH"] = str(tools) + ":" + env["PATH"]
            self.run_command(root, "git", "init", "-q")
            self.run_command(root, "git", "config", "user.name", "Wiki test")
            self.run_command(root, "git", "config", "user.email", "test@example.invalid")
            # Bypass the test helper's portable flag to exercise normal readiness.
            result = subprocess.run([sys.executable, str(HERE / "install.py"),
                                     "portable-check", "--prime"], cwd=root,
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertIn("prime-agent is not on PATH", result.stdout + result.stderr)
            result = subprocess.run([sys.executable, str(HERE / "update.py"),
                                     "--portable"], cwd=root,
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("portable CI check", result.stdout + result.stderr)
            result = subprocess.run([sys.executable, str(HERE / "update.py")],
                                    cwd=root, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)

    def test_disabled_claude_preserves_existing_files(self):
        with tempfile.TemporaryDirectory(prefix="wiki-disabled-") as tmp:
            root = Path(tmp)
            self.run_command(root, "git", "init", "-q")
            self.run_command(root, "git", "config", "user.name", "Wiki test")
            self.run_command(root, "git", "config", "user.email", "wiki-test@example.invalid")
            (root / ".claude/commands").mkdir(parents=True)
            existing = {"CLAUDE.md": "# Unrelated project\n> Project: other.\n",
                        ".claude/settings.json": '{"hooks": {}, "custom": true}\n',
                        ".claude/commands/wiki-resume.md": "User-owned command\n"}
            for rel, content in existing.items():
                (root / rel).write_text(content)
            self.run_command(root, "git", "add", ".")
            self.run_command(root, "git", "commit", "-qm", "existing integration")
            self.run_command(root, sys.executable, str(HERE / "install.py"), "selected-project", "--codex")
            self.run_command(root, sys.executable, str(HERE / "update.py"))
            for rel, content in existing.items():
                self.assertEqual((root / rel).read_text(), content)
            self.assertIn("> Project: selected-project.", (root / "AGENTS.md").read_text())
            (root / "AGENTS.md").write_text("drift\n")
            self.run_command(root, "git", "add", "AGENTS.md")
            result = subprocess.run([sys.executable, ".memory/tools/dual_sync.py", "--check"], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("AGENTS.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
