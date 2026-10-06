#!/usr/bin/env python3
"""PreToolUse HARD guard: iron rules as enforcement, not discipline (idea §2).

Denies, before the tool runs: (a) Edit/Write of a FILED decision — a tracked
`.memory/wiki/decisions/*.md` file (creation of a new one and edits to a not-yet-committed
one stay allowed: filed = committed); (b) any Edit/Write of `.memory/wiki/log.md` that is
not a pure append (existing content must survive as a prefix; EOL differences normalized).
Living pages (concepts/, overview, index) and everything outside `.memory/wiki/` pass
untouched. The hard brother of grep_nudge.py (that one only reminds).

Deny = JSON on stdout (permissionDecision "deny" + legacy decision "block" for older
clients) — exit codes are useless here because settings.json wraps hooks in `|| true`.
Fail-OPEN: any internal error -> silent allow; a guard bug must never brick editing.
`structural.py guard` is the second line that catches what slips past (Bash/sed edits,
MCP file tools, hook failure). Matcher covers Edit|Write only (no MultiEdit in this
client's tool set).

Codex CLI (dual-runtime): file edits arrive as `tool_name: "apply_patch"` with the patch
envelope in `tool_input.command` (per https://developers.openai.com/codex/hooks) — the
Edit|Write matcher aliases it, but the payload shape differs, so a dedicated branch parses
`*** Update/Add/Delete File:` sections. log.md append-detection there is a heuristic
(no `-` lines, added lines only at section end) — a mid-file pure-insert can slip the hook;
pre-commit guard is the hard line behind it.

Self-test: `python wiki_guard.py --selfcheck` (assert-based, no framework).
"""
import json
import os
import re
import sys

_DENY_DECISION = ("Iron rule: filed decisions are IMMUTABLE. {path} is committed — never "
                  "edit or overwrite it. To revise it, CREATE a new decision file with "
                  "`supersedes: <its id>` frontmatter (forward supersession).")
_DENY_LOG = ("Iron rule: log.md is APPEND-ONLY. This change rewrites/removes existing "
             "lines. Append new lines at the end; never modify or delete old ones.")


def _norm(s):
    """EOL-normalize so CRLF/LF re-serialization never causes a false deny."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _portable_path(path):
    """Treat backslashes as separators too (hook clients are cross-platform)."""
    return os.fspath(path).replace("\\", os.sep)


def _repo_root(cwd, candidate=None):
    """Return the memory project owning the target/cwd, with this installation as fallback.

    Neither the nearest Git repository nor payload ``cwd`` is authoritative: both can point at
    a nested repository or outside the project.  Walking the target and cwd for the explicit
    ``.memory/wiki`` fence also keeps copied selfchecks faithful; the installed location is the
    final runtime anchor.
    """
    starts = []
    if candidate:
        starts.append(candidate if os.path.isdir(candidate) else os.path.dirname(candidate))
    starts.append(os.path.realpath(os.path.abspath(_portable_path(cwd or os.getcwd()))))
    for start in starts:
        cur = os.path.realpath(start)
        while True:
            if os.path.isdir(os.path.join(cur, ".memory", "wiki")):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    installed = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return installed if os.path.isdir(os.path.join(installed, ".memory", "wiki")) else None


def _wiki_path(path, cwd):
    """Return ``(canonical_path, wiki-relative/path)`` only for this repo's wiki.

    Classification is based on canonical containment, never on a path substring.
    ``realpath`` deliberately resolves existing symlinks while still normalizing a
    missing leaf, so aliases into the wiki are covered and escapes out of it are not.
    """
    if not path:
        return None
    raw = _portable_path(path)
    base = os.path.realpath(os.path.abspath(_portable_path(cwd or os.getcwd())))
    candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
    candidate = os.path.realpath(os.path.abspath(candidate))
    root = _repo_root(base, candidate)
    if root is None:
        return None
    wiki = os.path.realpath(os.path.join(root, ".memory", "wiki"))
    try:
        contained = (os.path.commonpath((os.path.normcase(candidate), os.path.normcase(wiki)))
                     == os.path.normcase(wiki))
    except ValueError:  # e.g. different Windows drives
        contained = False
    if contained:
        rel = os.path.relpath(candidate, wiki).replace(os.sep, "/")
        return candidate, rel

    # ``realpath`` covers symlinks but not hard links.  An existing outside pathname can
    # still be the same inode as an immutable decision or append-only log, so bind such
    # aliases back to the protected canonical pathname before classifying the operation.
    if os.path.isfile(candidate):
        protected = [os.path.join(wiki, "log.md")]
        decisions = os.path.join(wiki, "decisions")
        if os.path.isdir(decisions):
            protected.extend(os.path.join(decisions, name)
                             for name in os.listdir(decisions) if name.endswith(".md"))
        for target in protected:
            try:
                if os.path.isfile(target) and os.path.samefile(candidate, target):
                    rel = os.path.relpath(target, wiki).replace(os.sep, "/")
                    return os.path.realpath(target), rel
            except OSError:
                continue
    return None


def _git_tracked(path):
    """True iff git tracks the file — tracked = committed/staged = FILED. A decision
    created this session stays editable until it lands in git."""
    import subprocess
    r = subprocess.run(["git", "ls-files", "--error-unmatch", os.path.basename(path)],
                       cwd=os.path.dirname(path), capture_output=True)
    return r.returncode == 0


def _read(path):
    with open(path, encoding="utf-8") as f:
        return _norm(f.read())


_PATCH_OP = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$")


def _patch_sections(patch):
    """Split an apply_patch envelope into per-file sections."""
    secs, cur = [], None
    for line in _norm(patch).split("\n"):
        m = _PATCH_OP.match(line)
        if m:
            cur = {"op": m.group(1), "path": m.group(2).strip(),
                   "move_to": None, "lines": []}
            secs.append(cur)
        elif line.startswith("*** Move to:") and cur:
            cur["move_to"] = line.split(":", 1)[1].strip()
        elif line.startswith("*** "):
            cur = None  # Begin/End Patch, End of File
        elif cur is not None:
            cur["lines"].append(line)
    return secs


def _filed_decision(info, tracked):
    """Whether a classified wiki path names an existing, filed decision."""
    if info is None:
        return False
    path, rel = info
    return rel.startswith("decisions/") and os.path.exists(path) and tracked(path)


def _decide_patch(patch, tracked, cwd):
    """Codex apply_patch branch: same iron rules, patch-envelope payload."""
    for sec in _patch_sections(patch):
        source = _wiki_path(sec["path"], cwd)
        target = _wiki_path(sec["move_to"], cwd) if sec["move_to"] else None

        # Protect both ends of a move.  Missing/untracked destination decisions
        # remain legal new decisions; an Add header never exempts an existing one.
        for info in (source, target):
            if _filed_decision(info, tracked):
                return _DENY_DECISION.format(path=info[1])

        source_log = source is not None and source[1] == "log.md"
        target_log = target is not None and target[1] == "log.md"
        if target_log and os.path.exists(target[0]):
            return _DENY_LOG  # a move onto the filed log replaces it
        if source_log:
            if sec["op"] == "Delete" or sec["move_to"] \
                    or any(l.startswith("-") for l in sec["lines"]):
                return _DENY_LOG
            seen_plus = False
            for line in sec["lines"]:
                if line.startswith("+"):
                    seen_plus = True
                elif seen_plus and line.strip():
                    return _DENY_LOG  # context after added lines = not an EOF append
    return None


def decide(tool_name, tool_input, tracked=_git_tracked, cwd=None):
    """Return a deny reason, or None to allow. Tests can inject ``tracked``."""
    if tool_name == "apply_patch":
        return _decide_patch((tool_input or {}).get("command") or "", tracked, cwd)
    if tool_name not in ("Edit", "Write"):
        return None
    ti = tool_input or {}
    info = _wiki_path(ti.get("file_path") or "", cwd)
    if info is None:
        return None
    path, rel = info

    if rel.startswith("decisions/"):
        if not os.path.exists(path):
            return None  # filing a NEW decision — the one legal write
        if not tracked(path):
            return None  # created this session, not yet committed = not yet filed
        return _DENY_DECISION.format(path=rel)

    if rel == "log.md":
        if not os.path.exists(path):
            return None  # initial creation; a failing Edit fails on its own
        content = _read(path)
        if tool_name == "Write":
            result = _norm(ti.get("content") or "")
        else:
            old, new = _norm(ti.get("old_string") or ""), _norm(ti.get("new_string") or "")
            result = (content.replace(old, new) if ti.get("replace_all")
                      else content.replace(old, new, 1))
        return None if result.startswith(content) else _DENY_LOG

    return None  # living pages (concepts/, overview, index, anything new) stay free


def main():
    try:
        data = json.load(sys.stdin)
        msg = decide(data.get("tool_name"), data.get("tool_input"), cwd=data.get("cwd"))
    except Exception:
        return  # fail-OPEN: a broken guard must never brick editing (2nd line: structural.py guard)
    if msg:
        print(json.dumps({
            "decision": "block", "reason": msg,  # legacy shape, older clients
            "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": msg}}))


def selfcheck():
    import shutil
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="wiki-guard-selfcheck-")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True, check=True)
        dec_dir = os.path.join(tmp, ".memory", "wiki", "decisions")
        os.makedirs(dec_dir)
        d1 = os.path.join(dec_dir, "0001-x.md")
        with open(d1, "w", encoding="utf-8") as f:
            f.write("---\nid: 0001\n---\nbody\n")
        log = os.path.join(tmp, ".memory", "wiki", "log.md")
        with open(log, "w", encoding="utf-8") as f:
            f.write("2026-01-01 init\n2026-01-02 two\n")
        nested = os.path.join(tmp, "code", "deep")
        os.makedirs(nested)
        subprocess.run(["git", "init", "-q"], cwd=nested, capture_output=True, check=True)
        external_cwd = tempfile.mkdtemp(prefix="wiki-guard-outside-")
        alias = os.path.join(tmp, "wiki-alias")
        os.symlink(os.path.join(tmp, ".memory", "wiki"), alias)
        outside = os.path.join(tmp, "outside")
        os.makedirs(os.path.join(outside, "decisions"))
        escaped = os.path.join(tmp, ".memory", "wiki", "escape")
        os.symlink(outside, escaped)

        yes = lambda p: True   # noqa: E731 — injected "is tracked" stand-ins
        no = lambda p: False   # noqa: E731
        call = lambda tool, ti, t=yes, at=tmp: decide(tool, ti, t, cwd=at)  # noqa: E731

        # Canonical containment matrix for Edit/Write: absolute/relative, dot and
        # dot-dot components, repeated separators, backslashes, nested cwd, and
        # a symlink alias into the wiki must all identify the same filed decision.
        spellings = [
            (d1, tmp, "absolute"),
            (".memory/wiki/decisions/0001-x.md", tmp, "relative"),
            ("./.memory//wiki/decisions/0001-x.md", tmp, "dot + double slash"),
            ("scratch/../.memory/wiki/decisions/0001-x.md", tmp, "dot-dot"),
            (r".memory\wiki\decisions\0001-x.md", tmp, "backslashes"),
            ("../../.memory/wiki/decisions/0001-x.md", nested, "nested Git cwd"),
            (d1, external_cwd, "absolute path from outside cwd"),
            ("wiki-alias/decisions/0001-x.md", tmp, "symlink alias"),
        ]
        for path, at, label in spellings:
            assert call("Edit", {"file_path": path, "old_string": "body",
                                 "new_string": "B"}, at=at), label + " Edit -> deny"
            assert call("Write", {"file_path": path, "content": "x"}, at=at), \
                label + " Write -> deny"

        # Legal new/unfiled decisions remain editable through normalized paths.
        assert call("Edit", {"file_path": d1, "old_string": "a", "new_string": "b"},
                    no) is None, "untracked decision stays editable until filed"
        assert call("Write", {"file_path": "./.memory//wiki/decisions/0002-y.md",
                              "content": "x"}) is None, "missing decision -> allow"
        assert call("Write", {"file_path": "wiki-alias/decisions/0003-z.md",
                              "content": "x"}) is None, "missing decision via alias -> allow"

        # Textual lookalikes and symlink escapes are outside canonical repo/wiki.
        fake = os.path.join(tmp, "not-the-wiki", ".memory", "wiki", "decisions", "0001.md")
        assert call("Write", {"file_path": fake, "content": "x"}) is None, \
            "substring lookalike outside canonical wiki -> allow"
        assert call("Write", {"file_path": ".memory/wiki/../outside.md", "content": "x"}) \
            is None, "dot-dot escape -> allow"
        assert call("Write", {"file_path": ".memory/wiki/escape/decisions/0001.md",
                              "content": "x"}) is None, "symlink escape -> allow"
        hardlink = os.path.join(outside, "decision-hardlink.md")
        os.link(d1, hardlink)
        assert call("Write", {"file_path": hardlink, "content": "x"}), \
            "outside hardlink to filed decision -> deny"

        # log.md via Edit: only a pure append survives simulation, through aliases too.
        assert call("Edit", {"file_path": log, "old_string": "2026-01-02 two",
                             "new_string": "2026-01-02 two\n2026-01-03 three"}) is None, \
            "append anchored on last line -> allow"
        assert call("Edit", {"file_path": "wiki-alias/log.md",
                             "old_string": "2026-01-02 two",
                             "new_string": "2026-01-02 two\n2026-01-03 three"}) is None, \
            "append through symlink alias -> allow"
        assert call("Edit", {"file_path": ".memory//wiki/log.md",
                             "old_string": "2026-01-01 init",
                             "new_string": "2026-01-01 EDITED"}), "old-line rewrite -> deny"
        assert call("Edit", {"file_path": log, "old_string": "2026-01-01 init\n",
                             "new_string": ""}), "line deletion -> deny"
        assert call("Edit", {"file_path": log, "old_string": "2026-01-01 init",
                             "new_string": "2026-01-01 init\nMID"}), "mid-file insert -> deny"
        assert call("Edit", {"file_path": log, "old_string": "2026", "new_string": "1999",
                             "replace_all": True}), "replace_all rewrite -> deny"

        # log.md via Write: new content must keep the old as prefix (EOL-normalized).
        with open(log, encoding="utf-8") as f:
            cur = f.read()
        assert call("Write", {"file_path": "../../.memory/wiki/log.md",
                              "content": cur + "2026-01-03 three\n"}, at=nested) is None, \
            "append via Write from nested cwd -> allow"
        assert call("Write", {"file_path": log, "content": cur}) is None, \
            "identical rewrite -> allow"
        assert call("Write", {"file_path": r".memory\wiki\log.md",
                              "content": "rewritten\n"}), "truncating Write -> deny"
        crlf = cur.replace("\n", "\r\n") + "2026-01-03 three\r\n"
        assert call("Write", {"file_path": log, "content": crlf}) is None, \
            "CRLF re-serialization + append -> allow"

        # Living pages + outside the wiki stay free; malformed input stays silent.
        con = os.path.join(tmp, ".memory", "wiki", "concepts", "c.md")
        assert call("Edit", {"file_path": con, "old_string": "a", "new_string": "b"}) \
            is None, "concept page -> allow"
        assert call("Write", {"file_path": os.path.join(tmp, ".memory", "wiki",
                                                         "overview.md"), "content": "x"}) \
            is None, "derived rollup -> allow"
        assert call("Edit", {"file_path": os.path.join(tmp, "src", "m.py"),
                             "old_string": "a", "new_string": "b"}) is None, \
            "outside wiki -> allow"
        assert call("Edit", {}) is None, "no file_path -> silent"
        assert call("Read", {"file_path": d1}) is None, "non-edit tool -> silent"

        # apply_patch gets the same normalization/containment matrix.
        P = lambda *ls: "\n".join(("*** Begin Patch",) + ls + ("*** End Patch",))  # noqa: E731
        def ap(path, at=tmp, t=yes):
            return call("apply_patch", {"command": P("*** Update File: " + path,
                                                       "@@", " body", "-body", "+B")},
                        t=t, at=at)

        for path, at, label in spellings:
            assert ap(path, at=at), label + " apply_patch -> deny"
        assert call("apply_patch", {"command": P(
            "*** Delete File: .memory/wiki/decisions/0001-x.md")}), \
            "delete filed decision -> deny"
        assert call("apply_patch", {"command": P(
            "*** Add File: .memory/wiki/decisions/0001-x.md", "+replacement")}), \
            "Add header cannot exempt an existing filed decision"
        assert ap(".memory/wiki/decisions/0001-x.md", t=no) is None, \
            "untracked decision patch -> allow"
        assert call("apply_patch", {"command": P(
            "*** Add File: .memory//wiki/decisions/0002-y.md", "+---")}) is None, \
            "missing decision patch -> allow"
        assert call("apply_patch", {"command": P(
            "*** Add File: wiki-alias/decisions/0003-z.md", "+---")}) is None, \
            "missing decision patch through alias -> allow"
        assert ap(".memory/wiki/escape/decisions/0001.md") is None, \
            "patch symlink escape -> allow"
        assert ap(fake) is None, "patch substring lookalike -> allow"

        # Moves protect both canonical endpoints, including aliased targets.
        assert call("apply_patch", {"command": P(
            "*** Update File: src.md", "*** Move to: wiki-alias/decisions/0001-x.md",
            "@@", "-a", "+b")}), "move onto filed decision -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: .memory/wiki/decisions/0001-x.md", "*** Move to: dst.md")}), \
            "move filed decision away -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: src.md", "*** Move to: .memory/wiki/decisions/0004-new.md",
            "@@", "-a", "+b")}) is None, "move to missing new decision -> allow"

        assert call("apply_patch", {"command": P(
            "*** Update File: .memory/wiki/log.md", "@@", " 2026-01-02 two",
            "+2026-01-03 three")}) is None, "EOF append patch -> allow"
        assert call("apply_patch", {"command": P(
            r"*** Update File: .memory\wiki\log.md", "@@", "-2026-01-01 init",
            "+1999 EDITED")}), "backslash log rewrite patch -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: wiki-alias/log.md", "@@", "+MID", " 2026-01-02 two")}), \
            "aliased mid-file insert patch -> deny"
        assert call("apply_patch", {"command": P(
            "*** Delete File: .memory/wiki/log.md")}), "delete log patch -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: .memory/wiki/log.md", "*** Move to: log2.md")}), \
            "rename log patch -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: src.md", "*** Move to: wiki-alias/log.md")}), \
            "move onto existing log -> deny"
        assert call("apply_patch", {"command": P(
            "*** Update File: .memory/wiki/concepts/c.md", "-a", "+b")}) is None, \
            "concept page patch -> allow"
        assert call("apply_patch", {"command": P(
            "*** Update File: src/m.py", "-a", "+b")}) is None, \
            "outside wiki patch -> allow"
        assert call("apply_patch", {}) is None, "no command -> silent"
        assert decide("apply_patch", None, yes, cwd=tmp) is None, "no tool_input -> silent"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if "external_cwd" in locals():
            shutil.rmtree(external_cwd, ignore_errors=True)

    # _git_tracked integration: real temp repo — untracked False, committed True.
    tmp2 = tempfile.mkdtemp(prefix="wiki-guard-git-")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp2, capture_output=True, check=True)
        p = os.path.join(tmp2, "a.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x\n")
        assert _git_tracked(p) is False, "fresh file -> untracked"
        subprocess.run(["git", "add", "a.md"], cwd=tmp2, capture_output=True, check=True)
        subprocess.run(["git", "-c", "user.email=g@x", "-c", "user.name=g",
                        "commit", "-qm", "t"], cwd=tmp2, capture_output=True, check=True)
        assert _git_tracked(p) is True, "committed file -> tracked"
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
