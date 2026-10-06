#!/usr/bin/env python3
"""Dual-runtime sync: Codex-side files are GENERATED from Claude-side sources.

Sources of truth -> generated artifacts:
    CLAUDE.md                  -> AGENTS.md                        (byte-identical copy)
    .claude/commands/<cmd>.md  -> .agents/skills/<cmd>/SKILL.md   (deterministic wrapper)

Every .claude/commands/*.md gets a Codex skill. A hand-written Codex skill (no
MARKER line in its SKILL.md) is never touched or checked — only generated ones are.

Modes:
    --sync       regenerate artifacts in the worktree; prints what changed
    --check      verify the git INDEX: staged artifacts must byte-match (EOL-normalized)
                 what the staged sources generate; prints violations, exit 1
    --selfcheck  assert-based test in a temp repo

A repo with no AGENTS.md and no generated skills in the index is not dual — check
passes. pre_commit_guard.py imports check_violations() so a desynced commit never
lands; fix is always: python .memory/tools/dual_sync.py --sync && git add -A
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from git_checked import GitIntegrityError, git_bytes

MARKER = "Use this skill when the user asks to run the migrated source command"
FIX = "run: python .memory/tools/dual_sync.py --sync && git add AGENTS.md .agents"


def _git_bytes(*args, cwd=None):
    return git_bytes(cwd or Path.cwd(), *args)


def _norm(b):
    return (b or b"").replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _frontmatter_desc_and_body(text):
    """(description, body) from a command file; body keeps source bytes verbatim."""
    desc = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if line.startswith("description:"):
                    desc = line[len("description:"):].strip().strip('"')
            body = text[end + 5:]
    return desc, body.lstrip("\n")


def _skill_md(cmd, command_text):
    desc, body = _frontmatter_desc_and_body(command_text)
    esc = desc.replace('"', '\\"')
    return (f'---\nname: "{cmd}"\ndescription: "{esc}"\n---\n\n'
            f"# {cmd}\n\n{MARKER} `{cmd}`.\n\n## Command Template\n\n{body}")


def _index_paths(cwd=None):
    out = _git_bytes("ls-files", "--cached", "-z", cwd=cwd) or b""
    return [p.decode("utf-8", "replace") for p in out.split(b"\0") if p]


def check_violations(cwd=None):
    """Violation strings for the INDEX; empty list = dual state is clean."""
    paths = set(_index_paths(cwd=cwd))
    probs = []
    if ".memory/runtimes.json" in paths:
        def read(rel):
            return _git_bytes("show", f":{rel}", cwd=cwd) if rel in paths else None
        try:
            expected = runtime_artifacts(read)
        except (ValueError, TypeError, KeyError) as exc:
            return [f"invalid shared runtime sources: {exc}"]
        for rel, wanted in expected.items():
            if _norm(read(rel)) != _norm(wanted):
                probs.append(f"{rel}: missing or drifted from shared memory sources — {FIX}")
        return probs

    agents = _git_bytes("show", ":AGENTS.md", cwd=cwd) if "AGENTS.md" in paths else None
    if agents is not None:
        claude = _git_bytes("show", ":CLAUDE.md", cwd=cwd) if "CLAUDE.md" in paths else None
        if claude is not None and _norm(agents) != _norm(claude):
            probs.append(f"AGENTS.md: not a byte-copy of CLAUDE.md — {FIX}")

    dual = agents is not None or any(p.startswith(".agents/skills/") for p in paths)
    if not dual:
        return probs

    for p in sorted(paths):
        if not (p.startswith(".claude/commands/") and p.endswith(".md")):
            continue
        cmd = p[len(".claude/commands/"):-3]
        src = _git_bytes("show", f":{p}", cwd=cwd)
        skill_path = f".agents/skills/{cmd}/SKILL.md"
        skill = (_git_bytes("show", f":{skill_path}", cwd=cwd)
                 if skill_path in paths else None)
        want = _skill_md(cmd, _norm(src).decode("utf-8")).encode("utf-8")
        if skill is None:
            probs.append(f"{skill_path}: missing Codex skill for {p} — {FIX}")
        elif _norm(skill) != want:
            probs.append(f"{skill_path}: drifted from {p} — {FIX}")

    for p in sorted(paths):
        parts = p.split("/")
        if not (p.startswith(".agents/skills/") and p.endswith("/SKILL.md")
                and len(parts) == 4):
            continue
        cmd = parts[2]
        content = _git_bytes("show", f":{p}", cwd=cwd)
        if MARKER.encode() in content and f".claude/commands/{cmd}.md" not in paths:
            probs.append(f"{p}: orphan generated skill — source command "
                         f".claude/commands/{cmd}.md is gone; delete the skill or "
                         "restore the command.")
    return probs


def runtime_artifacts(read):
    """Generate enabled integrations from harness-independent project sources."""
    cfg = json.loads(read(".memory/runtimes.json"))
    if not isinstance(cfg, dict) or set(cfg) != {"claude", "codex", "prime"} or any(type(v) is not bool for v in cfg.values()):
        raise ValueError("runtimes.json must contain claude/codex/prime booleans")
    instructions = read(".memory/INSTRUCTIONS.md")
    if instructions is None:
        raise ValueError(".memory/INSTRUCTIONS.md missing")
    result = {}
    if cfg["claude"]:
        result["CLAUDE.md"] = instructions
    if cfg["codex"]:
        result["AGENTS.md"] = instructions
    for cmd in ("wiki-resume", "wiki-checkpoint", "wiki-clear"):
        source = read(f".memory/commands/{cmd}.md")
        if source is None:
            raise ValueError(f"shared command missing: {cmd}")
        if cfg["claude"]:
            result[f".claude/commands/{cmd}.md"] = source
        if cfg["codex"]:
            result[f".agents/skills/{cmd}/SKILL.md"] = _skill_md(cmd, _norm(source).decode("utf-8")).encode("utf-8")
    return result


def sync(root="."):
    changed = []

    def write_if_diff(path, data):
        old = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                old = f.read()
        if old != data:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            changed.append(path)

    if os.path.isfile(os.path.join(root, ".memory/runtimes.json")):
        def read(rel):
            path = Path(root) / rel
            return path.read_bytes() if path.is_file() else None
        for rel, data in runtime_artifacts(read).items():
            write_if_diff(os.path.join(root, rel), data)
        for path in changed:
            print(f"synced: {path}")
        return 0

    claude_p = os.path.join(root, "CLAUDE.md")
    if os.path.exists(claude_p):
        with open(claude_p, "rb") as f:
            write_if_diff(os.path.join(root, "AGENTS.md"), f.read())

    cmd_dir = os.path.join(root, ".claude", "commands")
    if os.path.isdir(cmd_dir):
        for fn in sorted(os.listdir(cmd_dir)):
            if not fn.endswith(".md"):
                continue
            cmd = fn[:-3]
            with open(os.path.join(cmd_dir, fn), "rb") as f:
                text = _norm(f.read()).decode("utf-8")
            skill_p = os.path.join(root, ".agents", "skills", cmd, "SKILL.md")
            write_if_diff(skill_p, _skill_md(cmd, text).encode("utf-8"))

    skills_dir = os.path.join(root, ".agents", "skills")
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            skill_p = os.path.join(skills_dir, name, "SKILL.md")
            if not os.path.isfile(skill_p):
                continue
            with open(skill_p, "rb") as f:
                generated = MARKER.encode() in f.read()
            if generated and not os.path.isfile(os.path.join(cmd_dir, name + ".md")):
                shutil.rmtree(os.path.join(skills_dir, name))
                changed.append(f"{os.path.join(skills_dir, name)} (orphan removed)")

    for p in changed:
        print(f"synced: {p}")
    if not changed:
        print("dual state clean — nothing to sync")
    return 0


def selfcheck():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="dual-sync-selfcheck-")

    def write(rel, content):
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p) or tmp, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    def git(*args):
        subprocess.run(("git",) + args, capture_output=True, cwd=tmp)

    try:
        git("init", "-q")
        git("config", "user.email", "g@x")
        git("config", "user.name", "g")

        # non-dual repo -> clean
        write("CLAUDE.md", "# rules\nline\n")
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "non-dual -> clean"

        # sync generates artifacts; staged copy -> clean
        write(".claude/commands/wiki-x.md",
              "---\ndescription: Test command — does things.\n---\n\nBody line 1.\nBody 2.\n")
        sync(root=tmp)
        assert open(os.path.join(tmp, "AGENTS.md")).read() == "# rules\nline\n", \
            "AGENTS.md is byte-copy"
        skill = open(os.path.join(tmp, ".agents/skills/wiki-x/SKILL.md")).read()
        assert 'name: "wiki-x"' in skill and MARKER in skill and "Body line 1." in skill
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "freshly synced -> clean"

        # CLAUDE.md edited without sync -> violation
        write("CLAUDE.md", "# rules\nline\nNEW\n")
        git("add", "-A")
        assert any("AGENTS.md" in v for v in check_violations(cwd=tmp)), \
            "stale AGENTS.md -> violation"
        sync(root=tmp)
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "resynced -> clean"

        # command edited without sync -> violation
        write(".claude/commands/wiki-x.md",
              "---\ndescription: Test command — does things.\n---\n\nCHANGED body.\n")
        git("add", "-A")
        assert any("wiki-x/SKILL.md" in v for v in check_violations(cwd=tmp)), \
            "stale SKILL.md -> violation"
        sync(root=tmp)
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "resynced skill -> clean"

        # hand-written Codex skill (no MARKER) -> ignored
        write(".agents/skills/custom/SKILL.md", "---\nname: custom\n---\nhand-made\n")
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "hand-made skill -> ignored"

        # orphan generated skill -> violation
        git("rm", "-qf", ".claude/commands/wiki-x.md")
        assert any("orphan" in v for v in check_violations(cwd=tmp)), \
            "orphan generated skill -> violation"

        # sync removes the orphan generated skill; hand-made skill survives
        sync(root=tmp)
        assert not os.path.exists(os.path.join(tmp, ".agents/skills/wiki-x")), \
            "sync removes orphan generated skill"
        assert os.path.exists(os.path.join(tmp, ".agents/skills/custom/SKILL.md")), \
            "hand-made skill survives orphan cleanup"
        git("add", "-A")
        assert check_violations(cwd=tmp) == [], "orphan cleaned -> clean"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    elif "--check" in sys.argv:
        try:
            probs = check_violations()
        except GitIntegrityError as e:
            print(f"dual sync check REFUSED — Git integrity unavailable: {e}", file=sys.stderr)
            sys.exit(2)
        for p in probs:
            print(p, file=sys.stderr)
        sys.exit(1 if probs else 0)
    else:
        sys.exit(sync())
