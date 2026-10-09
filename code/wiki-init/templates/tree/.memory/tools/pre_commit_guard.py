#!/usr/bin/env python3
"""git pre-commit HARD line (decision 0020): wiki iron rules enforced at commit time.

Agent-agnostic: blocks ANY committer (Claude, Codex, sed, manual) from landing a commit
that (a) modifies or deletes a FILED decision, (b) rewrites `.memory/wiki/log.md`
other than a pure append, or (c) stages any wiki Markdown as a symlink/non-regular mode.
It also audits the reachable commit DAG, so a violation imported by merge/rebase/
cherry-pick/fast-forward cannot become trusted merely because it bypassed pre-commit.
Unstaged worktree noise never matters. Deliberate human override remains possible for a
human, but the next guard/resume audit still reports the bad historical transition.

The soft lines in front of this: wiki_guard.py (Claude PreToolUse deny; on Codex
apply_patch deny is signal-only — openai/codex#27833) and `structural.py guard` at
checkpoint. This hook is the floor for ordinary commits while the private hook trust root
is healthy. ``--no-verify`` and direct ``.git`` tampering are explicit local overrides; the
protected-base CI verifier is the independent backstop.

Also enforces dual-runtime cleanliness when dual_sync.py is present: AGENTS.md and
generated Codex skills must match their Claude-side sources (see dual_sync.py).

Install an isolated HEAD-pinned runtime (never point a hook at this mutable worktree file):
    python .memory/tools/install_hooks.py
Health check: `python .memory/tools/install_hooks.py --check`.

Self-test: `python pre_commit_guard.py --selfcheck` (temp repo, assert-based).
"""
import os
import subprocess
import sys
from pathlib import Path

from git_checked import (GitIntegrityError, git_bytes, git_text,
                         require_repo, run_git)
from markdown_io import MarkdownReadError, decode_markdown
from wiki_validation import (VALIDATION_FLOOR_MARKER, new_dangling_link_violations,
                             provenance_violations)

WIKI_PREFIX = ".memory/wiki/"
PROTECTED_PATHS = (".memory/wiki/decisions", ".memory/wiki/log.md")


def _git(*args, cwd=None, input_text=None):
    """Unchecked helper used only to construct disposable selfcheck repositories."""
    return subprocess.run(("git",) + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd,
                          input=input_text)


def _norm(s):
    """EOL-normalize so CRLF/LF differences never cause a false block."""
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def _norm_bytes(data):
    return (data or b"").replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _path(raw):
    """Git -z output is byte-safe; surrogateescape preserves every Unix filename."""
    return raw.decode("utf-8", "surrogateescape")


def _name_status(raw):
    """Parse --name-status --no-renames -z output into (status, path)."""
    fields = raw.split(b"\0")
    return [(fields[i].decode("ascii", "replace"), _path(fields[i + 1]))
            for i in range(0, len(fields) - 1, 2) if fields[i] and fields[i + 1]]


def _has_commits(cwd=None):
    require_repo(cwd or Path.cwd(), allow_unborn=True)
    return bool(git_text(cwd or Path.cwd(), "rev-list", "--all", "--max-count=1"))


def _tree_entry(ref, path, cwd=None):
    """Return (mode, type, object) for path in a commit/tree, or None."""
    raw = git_bytes(cwd or Path.cwd(), "ls-tree", "-z", ref, "--", path)
    if not raw or b"\t" not in raw:
        return None
    meta = raw.split(b"\t", 1)[0].decode("ascii", "replace").split()
    return tuple(meta[:3]) if len(meta) >= 3 else None


def _index_entry(path, cwd=None):
    raw = git_bytes(cwd or Path.cwd(), "ls-files", "--stage", "-z", "--", path)
    if not raw or b"\t" not in raw:
        return None
    meta = raw.split(b"\t", 1)[0].decode("ascii", "replace").split()
    return tuple(meta[:3]) if len(meta) >= 3 else None


def _blob(ref, path, cwd=None):
    return git_bytes(cwd or Path.cwd(), "show", f"{ref}:{path}")


def _tree_mode_violations(ref="HEAD", cwd=None):
    """Every tracked wiki Markdown page must be a regular non-executable file (100644)."""
    if not _has_commits(cwd):
        return []
    raw = git_bytes(cwd or Path.cwd(), "ls-tree", "-r", "-z", ref, "--", ".memory/wiki")
    probs = []
    for rec in raw.split(b"\0"):
        if not rec or b"\t" not in rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        mode = meta.split(None, 1)[0].decode("ascii", "replace")
        path = _path(raw_path)
        if path.endswith(".md") and mode != "100644":
            probs.append(f"{path}: wiki Markdown must be a regular 100644 file "
                         f"(got Git mode {mode}; symlinks/executables are forbidden)")
    return probs


def _index_mode_violations(cwd=None):
    """The staged target tree must contain only regular 100644 wiki Markdown entries."""
    raw = git_bytes(cwd or Path.cwd(), "ls-files", "--stage", "-z", "--", ".memory/wiki")
    probs = []
    for rec in raw.split(b"\0"):
        if not rec or b"\t" not in rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 3:
            continue
        mode, _obj, stage = fields
        path = _path(raw_path)
        if path.endswith(".md") and (mode != "100644" or stage != "0"):
            probs.append(f"{path}: staged wiki Markdown must be one regular 100644 file "
                         f"(got mode {mode}, stage {stage})")
    return probs


def _transition_violations(parent, child, cwd=None, *, floor_active=False):
    """Protected invariants for one committed parent -> child transition."""
    raw = git_bytes(cwd or Path.cwd(), "diff", "--name-status", "--no-renames", "-z",
                    parent, child, "--", ".memory/wiki")
    probs = []
    for status, path in _name_status(raw):
        if not path.endswith(".md"):
            continue
        new_entry = _tree_entry(child, path, cwd=cwd)
        if new_entry is not None and new_entry[0] != "100644":
            probs.append(f"history {parent[:8]}..{child[:8]}: {path} became Git mode "
                         f"{new_entry[0]} (wiki Markdown must stay 100644)")
        rel = path[len(WIKI_PREFIX):] if path.startswith(WIKI_PREFIX) else ""
        old_entry = _tree_entry(parent, path, cwd=cwd)
        if rel.startswith("decisions/") and old_entry is not None:
            if new_entry is None or new_entry != old_entry:
                probs.append(f"history {parent[:8]}..{child[:8]}: {path} changed after filing "
                             "(decisions are immutable; supersede forward)")
        elif rel == "log.md" and old_entry is not None:
            old = _blob(parent, path, cwd=cwd)
            new = _blob(child, path, cwd=cwd) if new_entry is not None else None
            if new is None or not _norm_bytes(new).startswith(_norm_bytes(old)):
                probs.append(f"history {parent[:8]}..{child[:8]}: {path} is not a pure append")

    # Activation is monotonic: inherited history state, not immediate path presence, controls
    # the floor. Deleting/corrupting the validator is itself a permanent history violation.
    if floor_active:
        if not _validation_marker_present(child, cwd=cwd):
            probs.append(f"history {parent[:8]}..{child[:8]}: validation floor removed/corrupt")
        before, after = _tree_pages(parent, cwd=cwd), _tree_pages(child, cwd=cwd)
        trans = new_dangling_link_violations(before, after)
        child_sources = _tree_source_entries(child, cwd=cwd)
        trans += provenance_violations(
            before, after,
            source_exists=lambda rel: _source_entry_exists(cwd, child_sources, rel),
            commit_exists=lambda sha: _candidate_commit_exists(cwd, sha, ref=child),
            source_read=lambda rel: decode_markdown(_blob(child, rel, cwd=cwd), rel))
        probs.extend(f"history {parent[:8]}..{child[:8]}: {p}" for p in trans)
    return probs


def _validation_marker_present(ref, cwd=None):
    path = ".memory/tools/wiki_validation.py"
    entry = _tree_entry(ref, path, cwd=cwd)
    if entry is None or entry[0] not in {"100644", "100755"} or entry[1] != "blob":
        return False
    text = _blob(ref, path, cwd=cwd).decode("utf-8", "replace")
    return VALIDATION_FLOOR_MARKER in text


def _index_validation_marker_present(cwd=None):
    path = ".memory/tools/wiki_validation.py"
    entry = _index_entry(path, cwd=cwd)
    if entry is None or entry[0] not in {"100644", "100755"} or entry[2] != "0":
        return False
    text = git_text(cwd or Path.cwd(), "show", f":{path}", strip=False)
    return VALIDATION_FLOOR_MARKER in text


def _tree_source_entries(ref, cwd=None):
    """Candidate evidence map; symlinks (120000) and gitlinks (160000) are excluded."""
    root = cwd or Path.cwd()
    raw = git_bytes(root, "ls-tree", "-r", "-z", ref)
    entries = {}
    for rec in raw.split(b"\0"):
        if not rec or b"\t" not in rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 3:
            continue
        mode, kind, obj = fields
        if mode in {"100644", "100755"} and kind == "blob":
            entries[_path(raw_path)] = obj
    return entries


def _source_entry_exists(cwd, entries, rel):
    obj = entries.get(rel)
    if not obj:
        return False
    git_text(cwd or Path.cwd(), "cat-file", "-e", f"{obj}^{{blob}}")
    return True


def _tree_pages(ref, cwd=None):
    root = cwd or Path.cwd()
    raw = git_bytes(root, "ls-tree", "-r", "--name-only", "-z", ref, "--", ".memory/wiki")
    pages = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = _path(item)
        if path.endswith(".md"):
            rel = path[len(WIKI_PREFIX):]
            source = f"{ref}:{path}"
            pages[rel] = decode_markdown(git_bytes(root, "show", source), source)
    return pages


def _index_pages(cwd=None):
    root = cwd or Path.cwd()
    raw = git_bytes(root, "ls-files", "--cached", "-z", "--", ".memory/wiki")
    pages = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = _path(item)
        if path.endswith(".md"):
            rel = path[len(WIKI_PREFIX):]
            source = f"index:{path}"
            pages[rel] = decode_markdown(git_bytes(root, "show", f":{path}"), source)
    return pages


def history_violations(cwd=None, ref="HEAD"):
    """Audit the full reachable Git history, failing closed on unreadable Git state."""
    if not _has_commits(cwd):
        return []
    root = cwd or Path.cwd()
    commits = git_text(root, "rev-list", "--reverse", "--topo-order", ref).splitlines()
    probs = _tree_mode_violations(ref=ref, cwd=cwd)
    active = {}
    for commit in commits:
        parents = git_text(root, "show", "-s", "--format=%P", commit).split()
        inherited = any(active.get(parent, False) for parent in parents)
        for parent in parents:
            probs.extend(_transition_violations(
                parent, commit, cwd=cwd, floor_active=active.get(parent, False)))
        active[commit] = inherited or _validation_marker_present(commit, cwd=cwd)
    return list(dict.fromkeys(probs))


def _candidate_source_entries(cwd=None):
    """Index evidence map; names alone and non-regular modes are insufficient provenance."""
    root = cwd or Path.cwd()
    raw = git_bytes(root, "ls-files", "--stage", "-z")
    entries = {}
    for rec in raw.split(b"\0"):
        if not rec or b"\t" not in rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 3:
            continue
        mode, obj, stage = fields
        if mode in {"100644", "100755"} and stage == "0":
            entries[_path(raw_path)] = obj
    return entries


def _candidate_commit_exists(cwd, sha, *, ref="HEAD"):
    root = cwd or Path.cwd()
    obj = run_git(root, "cat-file", "-e", f"{sha}^{{commit}}",
                  allowed_rc=frozenset({0, 1, 128}))
    if obj.returncode != 0:
        return False
    ancestor = run_git(root, "merge-base", "--is-ancestor", sha, ref,
                       allowed_rc=frozenset({0, 1}))
    return ancestor.returncode == 0


def violations(cwd=None):
    """Violations in reachable history and in the exact staged INDEX target."""
    root = cwd or Path.cwd()
    has_head = _has_commits(cwd)
    probs = history_violations(cwd=cwd) if has_head else []
    probs += _index_mode_violations(cwd=cwd)

    out = git_bytes(root, "diff", "--cached", "--name-status", "--no-renames", "-z")
    for status, path in _name_status(out):
        p = path.replace("\\", "/")
        if p.startswith(".memory/wiki/decisions/") and status != "A":
            probs.append(f"{p}: filed decisions are IMMUTABLE (staged: {status}). "
                         "Unstage/revert it; to revise, file a NEW decision with "
                         "`supersedes: <its id>`.")
        elif p == ".memory/wiki/log.md" and has_head:
            old_entry = _tree_entry("HEAD", p, cwd=cwd)
            staged_entry = _index_entry(p, cwd=cwd)
            old = _norm(decode_markdown(_blob("HEAD", p, cwd=cwd), f"HEAD:{p}")) \
                if old_entry is not None else ""
            staged = _norm(decode_markdown(git_bytes(root, "show", f":{p}"), f"index:{p}")) \
                if staged_entry is not None else ""
            if not staged.startswith(old):
                probs.append(".memory/wiki/log.md: APPEND-ONLY — staged content does not "
                             "keep the HEAD version as a prefix. Append; never edit or "
                             "delete existing lines.")

    baseline = _tree_pages("HEAD", cwd=cwd) if has_head else {}
    candidate = _index_pages(cwd=cwd)
    if has_head and _validation_marker_present("HEAD", cwd=cwd)             and not _index_validation_marker_present(cwd=cwd):
        probs.append("validation floor is monotonic; staged candidate removes/corrupts it")
    probs += new_dangling_link_violations(baseline, candidate)
    candidate_sources = _candidate_source_entries(cwd=cwd)
    probs += provenance_violations(
        baseline, candidate,
        source_exists=lambda rel: _source_entry_exists(cwd, candidate_sources, rel),
        commit_exists=lambda sha: _candidate_commit_exists(cwd, sha),
        source_read=lambda rel: decode_markdown(git_bytes(root, "show", f":{rel}"), rel))
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import dual_sync
        probs += dual_sync.check_violations(cwd=cwd)
    except ImportError:
        pass
    return list(dict.fromkeys(probs))


def main():
    try:
        probs = violations()
    except (GitIntegrityError, MarkdownReadError, OSError, UnicodeError) as e:
        sys.stderr.write(f"wiki pre-commit guard: COMMIT BLOCKED — integrity unavailable: {e}\n")
        return 2
    if not probs:
        return 0
    sys.stderr.write("wiki pre-commit guard: COMMIT BLOCKED\n")
    for p in probs:
        sys.stderr.write(f"  - {p}\n")
    sys.stderr.write("(deliberate human override: git commit --no-verify)\n")
    return 1


def selfcheck():
    import os
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="pre-commit-guard-selfcheck-")

    def write(rel, content):
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    try:
        _git("init", "-q", cwd=tmp)
        _git("config", "user.email", "g@x", cwd=tmp)
        _git("config", "user.name", "g", cwd=tmp)

        # initial commit: guard passes with no HEAD
        write("code/evidence.txt", "selfcheck evidence\n")
        write(".memory/tools/wiki_validation.py", "# WIKI_VALIDATION_FLOOR_V1\n")
        write(".memory/wiki/decisions/0001-x.md", "---\nid: 0001\n---\nbody\n[source: `code/evidence.txt`]\n")
        write(".memory/wiki/log.md", "2026-01-01 init\n2026-01-02 two\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "no HEAD -> allow"
        _git("commit", "-qm", "seed", cwd=tmp)

        # clean index -> allow
        assert violations(cwd=tmp) == [], "clean index -> allow"

        # Staged Markdown uses the same strict UTF-8/no-BOM reader as filesystem tools.
        log_path = Path(tmp) / ".memory/wiki/log.md"
        for payload in (b"\xef\xbb\xbf2026-01-01 init\n", b"2026-01-01 init\n\xff\n"):
            log_path.write_bytes(payload)
            _git("add", ".memory/wiki/log.md", cwd=tmp)
            try:
                violations(cwd=tmp)
                raise AssertionError("non-canonical staged Markdown must fail closed")
            except MarkdownReadError as exc:
                assert "index:.memory/wiki/log.md" in str(exc), exc
            _git("reset", "-q", "--hard", cwd=tmp)

        # editing a filed decision -> block
        write(".memory/wiki/decisions/0001-x.md", "---\nid: 0001\n---\nEDITED\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp), "staged edit of filed decision -> block"
        _git("reset", "-q", "--hard", cwd=tmp)

        # deleting a filed decision -> block
        _git("rm", "-q", ".memory/wiki/decisions/0001-x.md", cwd=tmp)
        assert violations(cwd=tmp), "staged delete of filed decision -> block"
        _git("reset", "-q", "--hard", cwd=tmp)

        # new decision (supersession) -> allow
        write(".memory/wiki/decisions/0002-y.md", "---\nid: 0002\nsupersedes: 0001\n---\nz\n[source: `code/evidence.txt`]\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "NEW decision file -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)

        # new dangling plain link -> block; wanted marker -> allow
        write(".memory/wiki/concepts/link.md",
              "# Link\n\n## Facts\n[[missing]]\n[source: `code/evidence.txt`]\n")
        _git("add", "-A", cwd=tmp)
        assert any("new plain" in p for p in violations(cwd=tmp)), "new dangling link -> block"
        _git("reset", "-q", "--hard", cwd=tmp)
        write(".memory/wiki/concepts/link.md",
              "# Link\n\n## Facts\n[[?missing]]\n[source: `code/evidence.txt`]\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "wanted link + source -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # changed durable unit requires a candidate-visible source
        write(".memory/wiki/concepts/p.md", "# P\n\n## Facts\nclaim\n")
        _git("add", "-A", cwd=tmp)
        assert any("source" in p for p in violations(cwd=tmp)), "unsourced claim -> block"
        _git("reset", "-q", "--hard", cwd=tmp)
        write("code/new-evidence.txt", "new evidence\n")
        write(".memory/wiki/concepts/p.md",
              "# P\n\n## Facts\nclaim\n[source: `code/new-evidence.txt`]\n")
        _git("add", ".memory/wiki/concepts/p.md", cwd=tmp)
        assert any("reachable candidate source" in p for p in violations(cwd=tmp)), \
            "unstaged new source -> block"
        _git("add", "code/new-evidence.txt", cwd=tmp)
        assert violations(cwd=tmp) == [], "staged new source -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # A path is evidence only when the candidate stores a regular readable blob.
        fake_obj = _git("hash-object", "-w", "--stdin", cwd=tmp,
                        input_text="/definitely/missing/outside\n").stdout.strip()
        _git("update-index", "--add", "--cacheinfo", "120000", fake_obj,
             "code/fake-evidence", cwd=tmp)
        write(".memory/wiki/concepts/symlink-source.md",
              "# S\n\n## Facts\nclaim\n[source: `code/fake-evidence`]\n")
        _git("add", ".memory/wiki/concepts/symlink-source.md", cwd=tmp)
        assert any("reachable candidate source" in p for p in violations(cwd=tmp)), \
            "symlink index entry must not satisfy provenance"
        _git("reset", "-q", "--hard", cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # A commit object must be reachable from HEAD, not merely present in the object store.
        tree = _git("write-tree", cwd=tmp).stdout.strip()
        dangling_commit = _git("commit-tree", tree, "-p", "HEAD", cwd=tmp,
                               input_text="dangling evidence\n").stdout.strip()
        write(".memory/wiki/concepts/dangling-commit.md",
              f"# D\n\n## Facts\nclaim\n[source: commit {dangling_commit}]\n")
        _git("add", ".memory/wiki/concepts/dangling-commit.md", cwd=tmp)
        assert any("reachable candidate source" in p for p in violations(cwd=tmp)), \
            "unreachable commit object must not satisfy provenance"
        _git("reset", "-q", "--hard", cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)
        head_commit = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
        write(".memory/wiki/concepts/reachable-commit.md",
              f"# R\n\n## Facts\nclaim\n[source: commit {head_commit}]\n")
        _git("add", ".memory/wiki/concepts/reachable-commit.md", cwd=tmp)
        assert violations(cwd=tmp) == [], "reachable HEAD commit source -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # log.md pure append -> allow
        write(".memory/wiki/log.md", "2026-01-01 init\n2026-01-02 two\n2026-01-03 three\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "log append -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)

        # log.md rewrite of an old line -> block
        write(".memory/wiki/log.md", "2026-01-01 EDITED\n2026-01-02 two\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp), "log rewrite -> block"
        _git("reset", "-q", "--hard", cwd=tmp)

        # log.md CRLF re-serialization + append -> allow (EOL-normalized)
        write(".memory/wiki/log.md", "2026-01-01 init\r\n2026-01-02 two\r\n2026-01-03 three\r\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "CRLF + append -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)

        # symlink/non-regular Git mode is blocked even when its blob starts with old log.
        old_log = _git("show", "HEAD:.memory/wiki/log.md", cwd=tmp).stdout
        obj = _git("hash-object", "-w", "--stdin", cwd=tmp,
                   input_text=old_log + "suffix\n").stdout.strip()
        _git("update-index", "--cacheinfo", "120000", obj,
             ".memory/wiki/log.md", cwd=tmp)
        assert any("120000" in p for p in violations(cwd=tmp)),             "log symlink mode -> block"
        _git("reset", "-q", "--hard", cwd=tmp)

        # A violating commit imported into HEAD remains detectable from history forever;
        # it cannot be laundered by a later clean checkpoint/index.
        safe = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
        write(".memory/wiki/decisions/0001-x.md", "---\nid: 0001\n---\nHISTORY EDIT\n")
        _git("add", ".memory/wiki/decisions/0001-x.md", cwd=tmp)
        _git("commit", "-qm", "imported violation", cwd=tmp)  # no hook in this temp repo
        assert any("history" in p and "0001-x.md" in p for p in history_violations(cwd=tmp)),             "history-imported decision edit -> persistent violation"
        _git("reset", "-q", "--hard", safe, cwd=tmp)
        assert history_violations(cwd=tmp) == [], "clean history after reset"

        # The activation floor itself is immutable-forward: staged deletion blocks.
        _git("rm", "-q", ".memory/tools/wiki_validation.py", cwd=tmp)
        assert any("monotonic" in p for p in violations(cwd=tmp)), \
            "staged validation-floor deletion -> block"
        _git("reset", "-q", "--hard", cwd=tmp)

        # Post-activation imported commits cannot bypass link/provenance gates with --no-verify.
        safe = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
        write(".memory/wiki/concepts/imported-link.md",
              "# Imported\n\n## Facts\n[[missing]]\n[source: `code/evidence.txt`]\n")
        _git("add", "-A", cwd=tmp)
        _git("commit", "-qm", "import bad link", cwd=tmp)
        assert any("new plain" in p for p in history_violations(cwd=tmp)), \
            "imported dangling link -> persistent history violation"
        _git("reset", "-q", "--hard", safe, cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)
        write(".memory/wiki/concepts/imported-source.md", "# Imported\n\n## Facts\nunsourced\n")
        _git("add", "-A", cwd=tmp)
        _git("commit", "-qm", "import unsourced", cwd=tmp)
        assert any("source" in p for p in history_violations(cwd=tmp)), \
            "imported unsourced claim -> persistent history violation"
        _git("reset", "-q", "--hard", safe, cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # Delete/restore cannot launder the semantic floor: inherited activation is monotonic.
        _git("rm", "-q", ".memory/tools/wiki_validation.py", cwd=tmp)
        _git("commit", "-qm", "remove validation floor", cwd=tmp)
        assert any("validation floor removed" in p for p in history_violations(cwd=tmp)), \
            "imported floor deletion -> permanent violation"
        write(".memory/tools/wiki_validation.py", "# WIKI_VALIDATION_FLOOR_V1\n")
        write(".memory/wiki/concepts/laundered.md", "# Laundered\n\n## Facts\nunsourced\n")
        _git("add", "-A", cwd=tmp)
        _git("commit", "-qm", "restore with bad claim", cwd=tmp)
        hp = history_violations(cwd=tmp)
        assert any("laundered.md" in p and "source" in p for p in hp), \
            "restored floor must still audit the bad transition"
        _git("reset", "-q", "--hard", safe, cwd=tmp)
        _git("clean", "-qfd", cwd=tmp)

        # unrelated file churn -> allow
        write("src/a.py", "x = 1\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "outside wiki -> allow"
        _git("reset", "-q", "--hard", cwd=tmp)

        # dual-runtime drift (AGENTS.md != CLAUDE.md) -> block
        write("CLAUDE.md", "rules v2\n")
        write("AGENTS.md", "rules v1\n")
        _git("add", "-A", cwd=tmp)
        assert any("AGENTS.md" in p for p in violations(cwd=tmp)), \
            "desynced AGENTS.md -> block"
        write("AGENTS.md", "rules v2\n")
        _git("add", "-A", cwd=tmp)
        assert violations(cwd=tmp) == [], "synced AGENTS.md -> allow"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        sys.exit(main())
