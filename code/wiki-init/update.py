#!/usr/bin/env python3
"""wiki-update — surgical update of an INSTALLED wiki+STATE memory system to the
current templates. install.py's sibling: install scaffolds once; update re-applies
the same templates over a live project — MECHANISM only, never KNOWLEDGE.

Updates: .memory/tools/*.py, .claude/hooks/*.py, .claude/commands/wiki-*.md
(<project> re-substituted), .claude/settings.json (same idempotent merge, plus
RETIRED hook entries stripped), CLAUDE.md "# Session Memory" section (h1-bounded
replace — user sections untouched; a retired "# rtk" section is REMOVED, decision
0047), tracked `.memory/hooks/pre-commit` + local `core.hooksPath` bootstrap, and —
when .codex/hooks.json exists or --codex is passed — the Codex layer (hooks.json
re-rendered with the CURRENT absolute repo path, AGENTS.md + skills regenerated
by dual_sync, so a repo that moved on disk is healed as a side effect); and when the
WikiInit Prime adapter exists or --prime is passed — its project-local extension.

NEVER touched: .memory/STATE.md, .memory/wiki/**, .memory/brainstorms|specs|
research, code/ — knowledge belongs to the project.

RETIRED scripts (template-owned once, deliberately dropped from the system) are
deleted along with their settings.json hook entries — recoverable from the update
commit. Anything unrecognized in .memory/tools / .claude/hooks is reported,
never deleted.

Idempotent by byte-diff (no VERSION bookkeeping): a second run prints
"already up to date" and creates no commit.

Usage: python update.py [--root DIR] [--project NAME] [--codex] [--prime] [--watchdog]
Exit:  0 ok/up-to-date · 2 not a git repo · 3 not installed / dirty targets /
       foreign AGENTS.md / project name undetectable · 4 doctor failed ·
       5 commit or dual_sync failed · 6 watchdog deployment failed
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import (CODEX_TREE, PRIME_TREE, FRAGS, ROLES, TREE, _git, _write,
                     entry_key, install_codex, install_precommit, install_prime,
                     merge_settings, precheck_precommit, run_dual_sync, watchdog_plan, deploy_watchdog)

# template-owned in a past generation (wiki-init.md.bak4/5), deliberately dropped
RETIRED = {"prompt_reminder.py"}
# retired hook scripts — deleted from BOTH runtimes' hook dirs, entries stripped
RETIRED_HOOKS = {"rtk_deny_net.py"}  # rtk dropped whole (decision 0047)
# retired .claude/commands/ files — superseded forward, deleted on update
RETIRED_COMMANDS = {"wiki-finish-task.md"}  # -> /wiki-clear (structural.py state-reset)
# retired CLAUDE.md h1 sections — spliced out when found
RETIRED_SECTIONS = ("# rtk",)

TOOLING_PREFIXES = (".memory/tools/", ".memory/hooks/", ".claude/hooks/",
                    ".claude/commands/", ".github/")
TOOLING_FILES = {".memory/.gitignore", ".memory/checkpoint-package.md", ".memory/checkpoint-inventory.md", ".memory/checkpoint-background.md"}
COMMIT_MSG = "chore: update wiki+STATE session memory tooling (/wiki-update)"
PRIME_ADAPTER_REL = ".prime/agent/extensions/wiki-memory.js"
PRIME_SETTINGS_REL = ".prime/agent/settings.json"
PRIME_SELFCHECK_REL = ".memory/tools/prime_adapter_selfcheck.mjs"
PRIME_MARKER = "WIKIINIT_PRIME_ADAPTER"
PRIME_SELFCHECK_MARKER = "WIKIINIT_PRIME_SELFCHECK"


def ufail(code: int, msg: str):
    print(f"wiki-update: {msg}", file=sys.stderr)
    sys.exit(code)


def tooling_rels() -> list[str]:
    """Template files update owns outright (the tooling subset of tree/)."""
    rels = []
    for src in sorted(TREE.rglob("*")):
        if src.is_dir() or "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(TREE).as_posix()
        if not (rel.startswith(TOOLING_PREFIXES) or rel in TOOLING_FILES):
            continue  # content (STATE/wiki seeds) — never update's business
        rels.append(rel)
    return rels


def detect_project(root: Path) -> str | None:
    cm = root / (".memory/INSTRUCTIONS.md" if (root / ".memory/runtimes.json").is_file() else "CLAUDE.md")
    if cm.is_file():
        m = re.search(r"^> Project: (.+?)\.", cm.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip()
    st = root / ".memory" / "STATE.md"
    if st.is_file():
        m = re.search(r"^# STATE — (.+)$", st.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip()
    return None


def prime_rels() -> list[str]:
    """WikiInit-owned Prime adapter files (never the user's other .prime resources)."""
    return [src.relative_to(PRIME_TREE).as_posix()
            for src in sorted(PRIME_TREE.rglob("*")) if src.is_file()]


def target_paths(root: Path, codex_active: bool, prime_active: bool = False) -> list[str]:
    """Every path update may write — the dirty-precheck + staging scope."""
    paths = tooling_rels() + [".claude/settings.json", "CLAUDE.md"]
    paths += [f".memory/tools/{name}" for name in RETIRED]
    paths += [f".claude/hooks/{name}" for name in RETIRED_HOOKS]
    paths += [f".claude/commands/{name}" for name in RETIRED_COMMANDS]
    if codex_active:
        paths += [src.relative_to(CODEX_TREE).as_posix()
                  for src in sorted(CODEX_TREE.rglob("*")) if src.is_file()]
        paths += [f".codex/hooks/{name}" for name in RETIRED_HOOKS] + ["AGENTS.md"]
        cmd_dir = TREE / ".claude" / "commands"
        paths += [f".agents/skills/{p.stem}/SKILL.md" for p in cmd_dir.glob("*.md")]
        # dual_sync prunes the generated skill of a retired command — stage that too
        for name in sorted(RETIRED_COMMANDS):
            stem = Path(name).stem
            paths += [f".agents/skills/{stem}/SKILL.md",
                      f".agents/skills/{stem}/agents/openai.yaml"]
    if prime_active:
        paths += prime_rels() + [PRIME_SETTINGS_REL]
    return paths


def dirty_targets(root: Path, paths: list[str]) -> list[str]:
    r = _git(root, "status", "--porcelain", "-z", "--", *paths)
    entries = [e for e in r.stdout.split("\0") if e.strip()]
    return [e[3:] for e in entries]


# --- CLAUDE.md section surgery ------------------------------------------------

def _find_h1_section(lines: list[str], prefix: str) -> tuple[int, int] | None:
    """[start, end) of the section whose h1 line starts with prefix; end = next
    h1 line ('# ', not '##') or EOF."""
    start = next((i for i, l in enumerate(lines) if l.startswith(prefix)), None)
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].startswith("# ")), len(lines))
    return start, end


def _splice(lines: list[str], start: int, end: int, frag: str) -> list[str]:
    repl = frag.rstrip("\n") + ("\n\n" if end < len(lines) else "\n")
    return lines[:start] + [repl] + lines[end:]


def update_claude_md(root: Path, project: str) -> list[str]:
    """Replace the memory section in place, splice out retired ones; report notes."""
    mem = (FRAGS / "claude-md-memory.md").read_text(encoding="utf-8") \
        .replace("<project>", project)
    cm = root / "CLAUDE.md"
    notes = []
    if not cm.is_file():
        _write(cm, mem)
        return ["CLAUDE.md was MISSING — created fresh with memory section"]
    text = cm.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    span = _find_h1_section(lines, "# Session Memory")
    if span:
        lines = _splice(lines, *span, frag=mem[mem.index("# Session Memory"):])
        notes.append("CLAUDE.md: memory section replaced (review diff — in-section "
                     "hand-tweaks are superseded, recoverable via git)")
    else:
        sep = "\n" if (lines and lines[-1].endswith("\n")) else "\n\n"
        lines.append(sep + mem)
        notes.append("CLAUDE.md: '# Session Memory' anchor NOT FOUND — appended fresh")

    for prefix in RETIRED_SECTIONS:
        span = _find_h1_section(lines, prefix)
        if span:
            lines = lines[:span[0]] + lines[span[1]:]
            notes.append(f"CLAUDE.md: retired '{prefix}' section REMOVED "
                         "(recoverable via git)")

    new_text = "".join(lines)
    if new_text == text:
        return []  # byte-identical — report nothing
    _write(cm, new_text)
    return notes


# --- RETIRED + orphans ---------------------------------------------------------

def remove_retired(root: Path) -> list[str]:
    removed = []
    for name in sorted(RETIRED):
        p = root / ".memory" / "tools" / name
        if p.is_file():
            p.unlink()
            removed.append(f".memory/tools/{name}")
    for name in sorted(RETIRED_HOOKS):
        for hook_dir in (".claude/hooks", ".codex/hooks"):
            p = root / hook_dir / name
            if p.is_file():
                p.unlink()
                removed.append(f"{hook_dir}/{name}")
    for name in sorted(RETIRED_COMMANDS):
        p = root / ".claude" / "commands" / name
        if p.is_file():
            p.unlink()
            removed.append(f".claude/commands/{name}")
    sj = root / ".claude" / "settings.json"
    if sj.is_file():
        cfg = json.loads(sj.read_text(encoding="utf-8"))
        changed = False
        for event, arr in (cfg.get("hooks") or {}).items():
            kept = [e for e in arr if entry_key(e) not in RETIRED | RETIRED_HOOKS]
            if len(kept) != len(arr):
                cfg["hooks"][event] = kept
                changed = True
        if changed:
            _write(sj, json.dumps(cfg, indent=2) + "\n")
            removed.append(".claude/settings.json: retired hook entries stripped")
    return removed


def orphan_report(root: Path) -> list[str]:
    """Files in update-owned dirs that neither the template nor RETIRED knows.
    Report-only — they may be the project's own; deletion stays with the user."""
    msgs = []
    for rel_dir, pattern in ((".memory/tools", "*.py"), (".claude/hooks", "*.py"),
                             (".claude/commands", "wiki-*.md")):
        known = {p.name for p in (TREE / rel_dir).glob(pattern)} | RETIRED | RETIRED_HOOKS
        d = root / rel_dir
        for p in sorted(d.glob(pattern)) if d.is_dir() else []:
            if p.name not in known:
                msgs.append(f"{rel_dir}/{p.name} — not template-owned, left untouched")
    return msgs


# --- main -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="target repo root (default: cwd)")
    ap.add_argument("--project", help="override the auto-detected project name")
    ap.add_argument("--claude", action="store_true",
                    help="retrofit the Claude Code layer onto a project that lacks it")
    ap.add_argument("--codex", action="store_true",
                    help="retrofit the Codex layer onto a project that lacks it")
    ap.add_argument("--prime", action="store_true",
                    help="retrofit the Prime Agent native adapter onto a project that lacks it")
    ap.add_argument("--watchdog", action="store_true", help="activate supervised background checkpoints; an existing watchdog is otherwise refreshed with its enabled/stopped state preserved")
    ap.add_argument("--portable", action="store_true", help="verify distribution files without requiring the Prime CLI; for CI, not live runtime readiness")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    r = _git(root, "rev-parse", "--is-inside-work-tree")
    if r.returncode != 0 or r.stdout.strip() != "true":
        ufail(2, f"{root} is not a git repo.")
    precheck_precommit(root)
    if not (root / ".memory" / "wiki").is_dir():
        ufail(3, "no .memory/wiki here — the system is not installed; "
                 "run /wiki-init instead.")
    if (root / ".memory/runtimes.json").is_file():
        update_independent(root, args)
        return
    codex_installed = (root / ".codex" / "hooks.json").is_file()
    codex_active = codex_installed or args.codex
    prime_path = root / PRIME_ADAPTER_REL
    prime_selfcheck_path = root / PRIME_SELFCHECK_REL
    prime_adapter_marked = (prime_path.is_file()
                            and PRIME_MARKER in prime_path.read_text(encoding="utf-8"))
    prime_selfcheck_marked = (prime_selfcheck_path.is_file()
                              and PRIME_SELFCHECK_MARKER in
                              prime_selfcheck_path.read_text(encoding="utf-8"))
    prime_installed = prime_adapter_marked or prime_selfcheck_marked
    if prime_path.exists() and not prime_adapter_marked:
        ufail(3, f"{PRIME_ADAPTER_REL} exists but is not the WikiInit Prime adapter — "
                 "refusing to ignore or overwrite a foreign/corrupt owned file. "
                 "Rename/remove it or wire the Prime layer manually.")
    if prime_selfcheck_path.exists() and not prime_selfcheck_marked:
        ufail(3, f"{PRIME_SELFCHECK_REL} exists but is not WikiInit-owned — refusing "
                 "to overwrite an unrecognized memory tool. Rename/remove it or wire "
                 "the Prime layer manually.")
    prime_active = prime_installed or args.prime
    if (root / "AGENTS.md").exists() and not codex_installed:
        ufail(3, "AGENTS.md exists but no .codex/hooks.json — a foreign AGENTS.md. "
                 "The dual guard would block commits (and --codex would overwrite "
                 "it). Resolve it first: rename/remove it, or wire the Codex layer "
                 "manually.")
    project = args.project or detect_project(root)
    if not project:
        ufail(3, "cannot detect the project name (no '> Project:' in CLAUDE.md, "
                 "no '# STATE —' in STATE.md) — pass --project <name>.")

    targets = target_paths(root, codex_active, prime_active)
    dirty = dirty_targets(root, targets)
    if dirty:
        ufail(3, "uncommitted changes on files this update would overwrite — "
                 "commit or stash them first (uncommitted work is unrecoverable):"
                 "\n  " + "\n  ".join(sorted(set(dirty))))

    watchdog_action = watchdog_plan(root, args.watchdog, codex_active)

    # writes — order matters: dual_sync must regenerate AFTER CLAUDE.md/commands
    for rel in tooling_rels():
        content = (TREE / rel).read_text(encoding="utf-8")
        if rel.endswith(".md"):  # <project> lives only in md templates; code copies byte-exact
            content = content.replace("<project>", project)
        _write(root / rel, content)
    settings_note = merge_settings(root)
    retired_notes = remove_retired(root)
    cm_notes = update_claude_md(root, project)
    codex_notes = []
    if codex_active:
        hooks_p = root / ".codex" / "hooks.json"
        before = hooks_p.read_bytes() if hooks_p.is_file() else None
        install_codex(root, project)
        run_dual_sync(root)
        if before is None:
            codex_notes.append("Codex layer RETROFITTED — open the repo in Codex "
                               "once and accept the hook-trust prompt.")
        elif hooks_p.read_bytes() != before:
            codex_notes.append(".codex/hooks.json changed — re-accept the "
                               "hook-trust prompt in Codex (per-entry hashes).")
    prime_notes = []
    if prime_active:
        install_prime(root, project)
        if not prime_installed:
            prime_notes.append("Prime Agent layer RETROFITTED — open a fresh Prime session "
                               "or run /reload to activate the adapter and /wiki-* commands.")

    changed = dirty_targets(root, targets)
    sha = None
    if not changed:
        print(f"wiki-update: {root} already up to date (project: {project})")
    else:
        r = _git(root, "add", "--", *sorted(set(changed)))
        if r.returncode != 0:
            ufail(5, f"git add failed:\n{r.stderr}")
        if _git(root, "diff", "--cached", "--quiet").returncode == 0:
            print("wiki-update: files written but NOTHING STAGED — are these "
                  "paths gitignored? Review and commit manually.")
        else:
            r = _git(root, "commit", "-m", COMMIT_MSG)
            if r.returncode != 0:
                ufail(5, "git commit failed (files ARE updated — fix the cause, "
                         f"then commit manually):\n{r.stderr}")
            sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()

        print(f"wiki-update: updated {root} (project: {project}, "
              f"codex: {'yes' if codex_active else 'no'}, "
              f"prime: {'yes' if prime_active else 'no'})")
        for rel in sorted(set(changed)):
            print(f"  {rel} — {ROLES.get(rel, '')}")
        if sha:
            print(f"  commit: {sha}")

    # The pre-write install pins the previous trusted HEAD so the update commit is guarded.
    # Refresh once more after commit so doctor sees the new runtime closure, not a stale bundle.
    precommit_note = install_precommit(root)

    for note in ([settings_note and f".claude/settings.json — {settings_note}"]
                 + retired_notes + cm_notes + [f".memory/hooks/pre-commit — "
                 f"{precommit_note}"] + codex_notes + prime_notes + orphan_report(root)):
        if note:
            print(f"  note: {note}")

    print("\ndoctor (contour self-check):")
    r = subprocess.run([sys.executable, ".memory/tools/structural.py", "doctor", *(["--portable"] if args.portable else [])],
                       cwd=root, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print("  " + "\n  ".join(out.splitlines()))
    if r.returncode != 0:
        ufail(4, "doctor FAILED — the contour is not alive; fix before relying "
                 "on this project's memory system.")
    deploy_watchdog(root, watchdog_action, codex_active)


def update_independent(root: Path, args):
    """Refresh independent installations without enabling unselected runtimes."""
    selection = root / ".memory/runtimes.json"
    cfg = json.loads(selection.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or set(cfg) != {"claude", "codex", "prime"} or any(type(v) is not bool for v in cfg.values()):
        ufail(3, "invalid .memory/runtimes.json: expected claude/codex/prime booleans")
    previous = cfg.copy()
    if cfg["prime"]:
        for rel, marker in ((PRIME_ADAPTER_REL, PRIME_MARKER), (PRIME_SELFCHECK_REL, PRIME_SELFCHECK_MARKER)):
            path = root / rel
            if path.exists() and (not path.is_file() or marker not in path.read_text(encoding="utf-8")):
                ufail(3, f"foreign or corrupt Prime target: {rel}")
    for name in cfg:
        cfg[name] = cfg[name] or getattr(args, name)
    project = args.project or detect_project(root)
    if not project:
        ufail(3, "cannot detect project name; pass --project")
    for name, rels in (("claude", ("CLAUDE.md", ".claude/commands/wiki-resume.md", ".claude/commands/wiki-checkpoint.md", ".claude/commands/wiki-clear.md")),
                       ("codex", ("AGENTS.md", ".codex", ".agents")),
                       ("prime", (PRIME_ADAPTER_REL, PRIME_SELFCHECK_REL))):
        if cfg[name] and not previous[name]:
            for rel in rels:
                if (root / rel).exists():
                    ufail(3, f"cannot add {name}: foreign target exists: {rel}")
    sources = {}
    for rel in tooling_rels():
        if rel.startswith(".claude/") and not rel.startswith(".claude/commands/"):
            continue
        dst = rel.replace(".claude/commands/", ".memory/commands/", 1)
        sources[dst] = TREE / rel
    targets = list(sources) + [".memory/runtimes.json", ".memory/INSTRUCTIONS.md"]
    if cfg["claude"]:
        targets += ["CLAUDE.md", ".claude/settings.json"]
        targets += [f".claude/commands/wiki-{name}.md" for name in ("resume", "checkpoint", "clear")]
    if cfg["codex"]:
        targets += [src.relative_to(CODEX_TREE).as_posix() for src in CODEX_TREE.rglob("*") if src.is_file()]
        targets += ["AGENTS.md"] + [f".agents/skills/wiki-{name}/SKILL.md" for name in ("resume", "checkpoint", "clear")]
    if cfg["prime"]:
        targets += prime_rels() + [PRIME_SETTINGS_REL]
    dirty = dirty_targets(root, targets)
    if dirty:
        ufail(3, "uncommitted changes on update targets:\n  " + "\n  ".join(dirty))
    watchdog_action = watchdog_plan(root, args.watchdog, cfg["codex"])
    for rel, source in sources.items():
        content = source.read_text(encoding="utf-8")
        if source.suffix == ".md":
            content = content.replace("<project>", project).replace("`CLAUDE.md`", "`.memory/INSTRUCTIONS.md`")
        _write(root / rel, content)
    instructions = root / ".memory/INSTRUCTIONS.md"
    lines = instructions.read_text(encoding="utf-8").splitlines(keepends=True)
    fragment = (FRAGS / "claude-md-memory.md").read_text(encoding="utf-8").replace("<project>", project)
    span = _find_h1_section(lines, "# Session Memory")
    if span:
        lines = _splice(lines, *span, frag=fragment[fragment.index("# Session Memory"):])
    else:
        lines.append("\n" + fragment)
    _write(instructions, "".join(lines))
    _write(selection, json.dumps(cfg, indent=2) + "\n")
    if cfg["claude"]:
        merge_settings(root)
    if cfg["codex"]:
        install_codex(root, project)
    run_dual_sync(root)
    if cfg["prime"]:
        install_prime(root, project)
    changed = dirty_targets(root, targets)
    if changed:
        r = _git(root, "add", "--", *sorted(set(changed)))
        if r.returncode:
            ufail(5, r.stderr)
        r = _git(root, "commit", "-m", COMMIT_MSG)
        if r.returncode:
            ufail(5, r.stderr)
        print("wiki-update: updated independent runtime installation")
        for rel in sorted(set(changed)):
            print(f"  {rel}")
    else:
        print("wiki-update: already up to date")
    print(install_precommit(root))
    for note in orphan_report(root):
        print("  note: " + note)
    if ".codex/hooks.json" in changed:
        print("Codex hook trust: open the repo in Codex and accept the hook-trust prompt.")
    if cfg["prime"] and not previous["prime"]:
        print("Prime Agent: open a fresh session or run /reload.")
    result = subprocess.run([sys.executable, ".memory/tools/structural.py", "doctor", *(["--portable"] if args.portable else [])], cwd=root)
    if result.returncode:
        ufail(4, "doctor FAILED")
    deploy_watchdog(root, watchdog_action, cfg["codex"])


# --- selfcheck -------------------------------------------------------------------

def selfcheck():
    import shutil
    import tempfile
    import install

    me = Path(__file__).resolve()

    def git(tmp, *args):
        return _git(Path(tmp), *args)

    def run_update(tmp, *extra):
        return subprocess.run([sys.executable, str(me), "--root", str(tmp), "--portable", *extra],
                              capture_output=True, text=True)

    def scaffold(tmp: Path, codex: bool, prime: bool = False):
        git(tmp, "init", "-q")
        git(tmp, "config", "user.email", "g@x")
        git(tmp, "config", "user.name", "g")
        install.install_tree(tmp, "proj")
        install.merge_settings(tmp)
        install.claude_md(tmp, "proj")
        if codex:
            install.install_codex(tmp, "proj")
            install.run_dual_sync(tmp)
        if prime:
            install.install_prime(tmp, "proj")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "seed")
        if codex:
            install.install_precommit(tmp)

    def head(tmp):
        return git(tmp, "rev-parse", "HEAD").stdout.strip()

    # ---- case A: aged non-codex repo (gen-2-style) ----
    tmp = Path(tempfile.mkdtemp(prefix="wiki-update-selfcheck-"))
    try:
        scaffold(tmp, codex=False)
        # age: old tools, missing tools, retired scripts + their hook entries,
        # old memory section, retired rtk section, user h1 sections both sides
        (tmp / ".memory/tools/structural.py").write_text("OLD\n", encoding="utf-8")
        for gone in ("dual_sync.py", "pre_commit_guard.py", "wiki_guard.py"):
            (tmp / ".memory/tools" / gone).unlink()
        (tmp / ".memory/tools/prompt_reminder.py").write_text("old\n", encoding="utf-8")
        (tmp / ".claude/commands/wiki-finish-task.md").write_text("old cmd\n", encoding="utf-8")
        (tmp / ".claude/hooks").mkdir(parents=True, exist_ok=True)
        (tmp / ".claude/hooks/rtk_deny_net.py").write_text("old deny-net\n", encoding="utf-8")
        sj = tmp / ".claude/settings.json"
        cfg = json.loads(sj.read_text(encoding="utf-8"))
        cfg["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [
            {"type": "command", "command": "python x/prompt_reminder.py"}]})
        cfg["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python x/.claude/hooks/rtk_deny_net.py"}]})
        cfg.setdefault("permissions", {}).setdefault("allow", []).append("Bash(rtk:*)")
        sj.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        (tmp / "CLAUDE.md").write_text(
            "# My rules\nkeep me\n\n# Session Memory (wiki + STATE)\n\n"
            "> Project: proj. Durable cross-session memory, installed by /wiki-init.\n"
            "OLD CONTENT LINE\n\n# rtk (token compression)\nprefix commands with rtk\n"
            "\n# My tail\ntail line\n", encoding="utf-8")
        (tmp / ".memory/wiki/concepts/x.md").write_text(
            "# X\nfact\n[source: `CLAUDE.md`]\n", encoding="utf-8")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "age")

        knowledge = {p: p.read_bytes()
                     for p in (tmp / ".memory").rglob("*")
                     if p.is_file() and "tools" not in p.parts}

        # dirty target -> refuse, write nothing
        (tmp / ".memory/tools/structural.py").write_text("DIRTY\n", encoding="utf-8")
        r = run_update(tmp)
        assert r.returncode == 3 and "uncommitted" in r.stderr, \
            f"dirty -> exit 3: rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}"
        assert (tmp / ".memory/tools/structural.py").read_text() == "DIRTY\n", \
            "dirty refusal writes nothing"
        git(tmp, "checkout", "--", ".memory/tools/structural.py")

        # foreign AGENTS.md -> refuse
        (tmp / "AGENTS.md").write_text("foreign\n", encoding="utf-8")
        r = run_update(tmp)
        assert r.returncode == 3 and "AGENTS.md" in r.stderr, "foreign AGENTS.md -> 3"
        (tmp / "AGENTS.md").unlink()

        # the real update
        r = run_update(tmp)
        assert r.returncode == 0, f"update failed:\n{r.stdout}\n{r.stderr}"
        tpl = (TREE / ".memory/tools/structural.py").read_text(encoding="utf-8")
        assert (tmp / ".memory/tools/structural.py").read_text(
            encoding="utf-8") == tpl, "structural.py refreshed"
        for back in ("dual_sync.py", "pre_commit_guard.py", "wiki_guard.py"):
            assert (tmp / ".memory/tools" / back).is_file(), f"{back} restored"
        assert not (tmp / ".memory/tools/prompt_reminder.py").exists(), "retired gone"
        assert "prompt_reminder" not in sj.read_text(encoding="utf-8"), \
            "retired hook entry stripped"
        assert not (tmp / ".claude/hooks/rtk_deny_net.py").exists(), \
            "retired hook SCRIPT deleted"
        assert "rtk_deny_net" not in sj.read_text(encoding="utf-8"), \
            "retired rtk hook entry stripped"
        want_wc = (TREE / ".claude/commands/wiki-clear.md").read_text(encoding="utf-8")
        assert (tmp / ".claude/commands/wiki-clear.md").read_text(
            encoding="utf-8") == want_wc, "commands refreshed from template"
        assert not (tmp / ".claude/commands/wiki-finish-task.md").exists(), \
            "retired command gone"
        cm = (tmp / "CLAUDE.md").read_text(encoding="utf-8")
        mem = (FRAGS / "claude-md-memory.md").read_text(
            encoding="utf-8").replace("<project>", "proj")
        assert mem[mem.index("# Session Memory"):].rstrip("\n") in cm, "memory section == fresh fragment"
        assert "OLD CONTENT LINE" not in cm, "old section content replaced"
        assert "# My rules\nkeep me" in cm and "# My tail\ntail line" in cm, \
            "user h1 sections survive"
        assert cm.index("# My rules") < cm.index("# Session Memory") < \
            cm.index("# My tail"), "section order preserved"
        assert "# rtk" not in cm, "retired rtk section spliced out"
        assert "prefix commands with rtk" not in cm, "retired section body gone too"
        for p, data in knowledge.items():
            assert p.read_bytes() == data, f"knowledge untouched: {p}"
        assert git(tmp, "log", "-1", "--format=%s").stdout.strip() == COMMIT_MSG
        hook = tmp / ".memory/hooks/pre-commit"
        assert hook.is_file(), "tracked fail-closed bootstrap installed"
        configured = git(tmp, "config", "--local", "--get", "core.hooksPath").stdout.strip()
        assert configured.replace("\\", "/").endswith("/wiki-memory-hooks/hooks"), configured
        hook_health = subprocess.run(
            (sys.executable, ".memory/tools/install_hooks.py", "--check"), cwd=tmp,
            capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert hook_health.returncode == 0, hook_health.stderr
        leftover = [ln for ln in git(tmp, "status", "--porcelain").stdout.splitlines()
                    if ln.strip() and "__pycache__" not in ln]
        assert not leftover, f"worktree clean after update (got: {leftover})"

        # idempotent second run
        h = head(tmp)
        r = run_update(tmp)
        assert r.returncode == 0 and "up to date" in r.stdout, "second run no-op"
        assert head(tmp) == h, "no second commit"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- case B: codex repo, aged CLAUDE.md, dual guard must pass ----
    tmp = Path(tempfile.mkdtemp(prefix="wiki-update-selfcheck-codex-")).resolve()
    try:
        scaffold(tmp, codex=True)
        cm_p = tmp / "CLAUDE.md"
        aged = cm_p.read_text(encoding="utf-8").replace(
            "## Iron rules", "OLD MARKER\n\n## Iron rules")
        cm_p.write_text(aged, encoding="utf-8")
        (tmp / "AGENTS.md").write_bytes(cm_p.read_bytes())  # keep dual clean
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "age")

        r = run_update(tmp)
        assert r.returncode == 0, f"codex update failed:\n{r.stdout}\n{r.stderr}"
        assert (tmp / "AGENTS.md").read_bytes() == cm_p.read_bytes(), \
            "AGENTS.md regenerated as byte-copy"
        assert "OLD MARKER" not in cm_p.read_text(encoding="utf-8")
        hooks = (tmp / ".codex/hooks.json").read_text(encoding="utf-8")
        assert json.dumps(str(tmp))[1:-1] in hooks, "hooks.json has CURRENT abs path"
        for cmd in ("wiki-resume", "wiki-checkpoint", "wiki-clear"):
            assert (tmp / f".agents/skills/{cmd}/SKILL.md").is_file(), \
                f"generated skill {cmd}"
        assert git(tmp, "log", "-1", "--format=%s").stdout.strip() == COMMIT_MSG, \
            "update commit passed the pre-commit dual guard"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- case C: Prime retrofit, coexistence, foreign-file safety, idempotency ----
    tmp = Path(tempfile.mkdtemp(prefix="wiki-update-selfcheck-prime-")).resolve()
    try:
        scaffold(tmp, codex=False)
        assert not (tmp / PRIME_ADAPTER_REL).exists(), "non-Prime scaffold stays unchanged"

        r = run_update(tmp, "--prime")
        assert r.returncode == 0, f"Prime retrofit failed:\n{r.stdout}\n{r.stderr}"
        adapter = tmp / PRIME_ADAPTER_REL
        assert adapter.is_file() and PRIME_MARKER in adapter.read_text(encoding="utf-8"), \
            "Prime adapter installed with marker"
        assert "prime: yes" in r.stdout, "Prime status reported"
        prime_settings = tmp / PRIME_SETTINGS_REL
        cfg = json.loads(prime_settings.read_text(encoding="utf-8"))
        assert cfg["compaction"] == {
            "enabled": True, "reserveTokens": 32768, "keepRecentTokens": 20000,
        }, "Prime project compaction floor installed"
        node = shutil.which("node")
        if node:
            syntax = subprocess.run([node, "--input-type=module", "--check"],
                                    input=adapter.read_text(encoding="utf-8"),
                                    capture_output=True, text=True)
            assert syntax.returncode == 0, f"Prime adapter syntax: {syntax.stderr}"

        knowledge_path = tmp / ".memory/wiki/concepts/prime-knowledge.md"
        knowledge_path.write_text(
            "# Prime knowledge\n\nDurable fact.\n[source: `CLAUDE.md`]\n",
            encoding="utf-8")
        git(tmp, "add", str(knowledge_path.relative_to(tmp)))
        git(tmp, "commit", "-qm", "add Prime knowledge")
        knowledge_before = knowledge_path.read_bytes()

        # User Prime settings remain authoritative; updater fills only missing compaction keys.
        cfg["theme"] = "dark"
        cfg["compaction"]["reserveTokens"] = 12345
        prime_settings.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        git(tmp, "add", PRIME_SETTINGS_REL)
        git(tmp, "commit", "-qm", "custom Prime project settings")

        h = head(tmp)
        r = run_update(tmp)
        assert r.returncode == 0 and "up to date" in r.stdout, "Prime update idempotent"
        assert head(tmp) == h, "Prime no-op makes no commit"
        kept = json.loads(prime_settings.read_text(encoding="utf-8"))
        assert kept["theme"] == "dark" and kept["compaction"]["reserveTokens"] == 12345, \
            "Prime update preserves explicit user settings"

        # Auto-detected installed adapters refresh without repeating --prime.
        adapter.write_text(adapter.read_text(encoding="utf-8") + "// OLD PRIME ADAPTER\n",
                           encoding="utf-8")
        git(tmp, "add", PRIME_ADAPTER_REL)
        git(tmp, "commit", "-qm", "age Prime adapter")
        r = run_update(tmp)
        assert r.returncode == 0 and PRIME_ADAPTER_REL in r.stdout, "Prime adapter refreshed"
        want_prime = (PRIME_TREE / PRIME_ADAPTER_REL).read_bytes()
        assert adapter.read_bytes() == want_prime, "Prime adapter restored byte-exact"

        # The marked companion keeps the layer detectable and heals a missing adapter.
        adapter.unlink()
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "remove Prime adapter")
        r = run_update(tmp)
        assert r.returncode == 0 and adapter.is_file(), "missing Prime adapter healed"

        # Dirty WikiInit-owned Prime targets are protected before any overwrite.
        adapter.write_text(adapter.read_text(encoding="utf-8") + "// DIRTY\n", encoding="utf-8")
        r = run_update(tmp)
        assert r.returncode == 3 and "uncommitted" in r.stderr, "dirty Prime target refused"
        assert adapter.read_text(encoding="utf-8").endswith("// DIRTY\n"), "dirty adapter untouched"
        git(tmp, "checkout", "--", PRIME_ADAPTER_REL)
        assert knowledge_path.read_bytes() == knowledge_before, "Prime updates preserve wiki knowledge"

        # A foreign extension at WikiInit's owned path is never overwritten on retrofit.
        adapter.unlink()
        adapter.write_text("export default function foreign() {}\n", encoding="utf-8")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "foreign adapter")
        before = adapter.read_bytes()
        r = run_update(tmp, "--prime")
        assert r.returncode == 3 and "foreign/corrupt" in r.stderr, "foreign adapter refused"
        assert adapter.read_bytes() == before, "foreign adapter untouched"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- case D: foreign companion tool is not a Prime install marker ----
    tmp = Path(tempfile.mkdtemp(prefix="wiki-update-selfcheck-prime-foreign-tool-")).resolve()
    try:
        scaffold(tmp, codex=False)
        foreign = tmp / PRIME_SELFCHECK_REL
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("// foreign custom tool\n", encoding="utf-8")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-qm", "foreign companion")
        before = foreign.read_bytes()
        r = run_update(tmp, "--prime")
        assert r.returncode == 3 and PRIME_SELFCHECK_REL in r.stderr, \
            "foreign companion refused"
        assert foreign.read_bytes() == before, "foreign companion untouched"
        assert not (tmp / PRIME_ADAPTER_REL).exists(), "retrofit did not partially write"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
