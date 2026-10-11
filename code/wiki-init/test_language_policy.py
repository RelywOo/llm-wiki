"""Language policy delivery and conservative migration in disposable repositories."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import install

HERE = Path(__file__).resolve().parent
STANDARD_LANGUAGE = """# Language
- Communicate in English by default in every new session.
- If the user explicitly requests Russian or English, switch to that language and keep using it until they request another switch in the same session.
- Do not switch languages merely because the user writes in another language.
- Keep code, code comments, commit messages, and PR descriptions in English unless explicitly requested otherwise.

"""
LEGACY_LANGUAGE = """# Language
- Always communicate with me only in English, everywhere and every time. This applies to all explanations, reports, questions, and other messages, as well as code, commit messages, and PR descriptions.

"""
PREFIX = "# Project rules\nKeep this user-owned section.\n\n"
TAIL = "\n# Project tail\nKeep this section too.\n"


class LanguagePolicyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="wiki-language-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.command("git", "init", "-q")
        self.command("git", "config", "user.name", "Language policy test")
        self.command("git", "config", "user.email", "wiki-test@example.invalid")

    def command(self, *args):
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def scaffold(self, legacy=False):
        if legacy:
            install.install_tree(self.root, "language-test")
            install.merge_settings(self.root)
            install.claude_md(self.root, "language-test")
            install.install_codex(self.root, "language-test")
            install.run_dual_sync(self.root)
            self.command("git", "add", ".")
            self.command("git", "commit", "-qm", "Legacy installation fixture")
            install.install_precommit(self.root)
            self.source = self.root / "CLAUDE.md"
        else:
            self.command(sys.executable, str(HERE / "install.py"), "language-test",
                         "--claude", "--codex", "--prime", "--portable")
            self.source = self.root / ".memory/INSTRUCTIONS.md"

    def set_language(self, language):
        memory = self.source.read_text(encoding="utf-8").split("# Session Memory", 1)[1]
        memory = memory.split("\n# Project tail", 1)[0]
        self.source.write_text(PREFIX + language + "# Session Memory" + memory + TAIL,
                               encoding="utf-8")
        self.command(sys.executable, ".memory/tools/dual_sync.py", "--sync")
        self.command("git", "add", ".")
        self.command("git", "commit", "-qm", "Existing language policy fixture")

    def knowledge(self):
        paths = [p for p in (self.root / ".memory/wiki").rglob("*") if p.is_file()]
        paths += [self.root / ".memory/STATE.md"]
        return {p.relative_to(self.root): p.read_bytes() for p in paths}

    def assert_runtime_policy(self, language):
        self.assertIn(language, self.source.read_text(encoding="utf-8"))
        for rel in ("CLAUDE.md", "AGENTS.md"):
            self.assertEqual((self.root / rel).read_bytes(), self.source.read_bytes())
        self.command(sys.executable, ".memory/tools/dual_sync.py", "--check")

    def assert_upgrade(self, language):
        before = self.knowledge()
        self.command(sys.executable, str(HERE / "update.py"), "--portable")
        self.assert_runtime_policy(language)
        text = self.source.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(PREFIX + language))
        self.assertTrue(text.endswith(TAIL))
        self.assertEqual(self.knowledge(), before)
        head = self.command("git", "rev-parse", "HEAD")
        self.command(sys.executable, str(HERE / "update.py"), "--portable")
        self.assertEqual(self.command("git", "rev-parse", "HEAD"), head)
        self.assertEqual(self.knowledge(), before)
        self.assertFalse(self.command("git", "status", "--porcelain").strip())

    def test_fresh_install_shares_policy_and_language_neutral_guidance(self):
        self.scaffold()
        self.assert_runtime_policy(STANDARD_LANGUAGE)
        for rel in (".memory/commands/wiki-checkpoint.md",
                    ".claude/commands/wiki-checkpoint.md",
                    ".agents/skills/wiki-checkpoint/SKILL.md"):
            text = (self.root / rel).read_text(encoding="utf-8")
            self.assertIn("session's selected conversation language", text)
            self.assertNotIn("plain English", text)
        nudge = (self.root / ".memory/tools/consult_nudge.py").read_text(encoding="utf-8")
        self.assertIn("Follow the project's Language instructions", nudge)
        self.assertNotIn("All user-facing output must be in English", nudge)
        # The hook defers to project rules, including custom policies after an update.
        self.assertNotIn("# Language", nudge)
        self.assertIn('"Memory check: no"', nudge)
        self.assertIn("Keep the Memory check markers exactly as written", nudge)

    def test_independent_install_upgrades_old_standard(self):
        self.scaffold()
        self.set_language(LEGACY_LANGUAGE)
        self.assert_upgrade(STANDARD_LANGUAGE)

    def test_legacy_install_upgrades_old_standard(self):
        self.scaffold(legacy=True)
        self.set_language(LEGACY_LANGUAGE)
        self.assert_upgrade(STANDARD_LANGUAGE)

    def check_custom_policies(self, legacy=False):
        self.scaffold(legacy=legacy)
        for language in (
            "# Language\n- Respond in French unless I ask otherwise.\n\n",
            LEGACY_LANGUAGE.rstrip() + "\n- Use Russian for release reports.\n\n",
            STANDARD_LANGUAGE,
        ):
            with self.subTest(language=language):
                self.set_language(language)
                self.assert_upgrade(language)

    def test_independent_install_preserves_custom_and_current_policies(self):
        self.check_custom_policies()

    def test_legacy_install_preserves_custom_and_current_policies(self):
        self.check_custom_policies(legacy=True)


if __name__ == "__main__":
    unittest.main()
