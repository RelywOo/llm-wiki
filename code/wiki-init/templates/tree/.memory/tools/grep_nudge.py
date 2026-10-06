#!/usr/bin/env python3
"""PreToolUse SOFT nudge, two branches:
- a Grep/Bash/PowerShell/exec-grep targeting .memory/wiki/ -> remind to prefer
  `structural.py search` (FTS5, porter-stemmed — word-forms grep can't match);
- one targeting code/ -> remind to prefer Serena MCP (LSP-backed semantic search:
  find_symbol / find_referencing_symbols / get_symbols_overview).
  Advice, not a gate: literal-string hunts stay grep.

SOFT = non-blocking: emits hookSpecificOutput.additionalContext, NEVER denies, NEVER
crashes the tool (any error -> stay silent).
# ponytail: code-branch needs the slash form (`code/`) in commands — a bare `code`
# dir arg passes silent (unparseable from a pattern literal; false negative is free).

Self-test: `python grep_nudge.py --selfcheck` (assert-based, no framework).
"""
import json
import re
import sys

_MSG = ("Reminder: for .memory/wiki/ content lookups prefer "
        "`python .memory/tools/structural.py search \"<terms>\"` "
        "(FTS5, porter-stemmed — finds word-forms grep misses), or "
        "`structural.py section|active` for structural queries. "
        "Plain grep is still fine for an exact literal (a decision id, a [[link]] target).")

_MSG_CODE = ("Reminder: for code/ hunts prefer Serena MCP (LSP-backed, semantic): "
             "find_symbol — where X is defined; find_referencing_symbols — who calls X; "
             "get_symbols_overview — file/module structure. Tools are deferred: load via "
             "ToolSearch, `activate_project` once per session. Plain grep stays fine for "
             "exact literals (log strings, config keys, TODO markers).")

# grep-family invocation tokens; \bgrep\b deliberately won't match inside 'ripgrep'
_GREP_RE = re.compile(r"\b(grep|egrep|fgrep|rg|ripgrep)\b|select-string|\bsls\b", re.I)

# `code/` as a path segment; lookbehind rejects .vscode/, mycode/, my.code/
_CODE_RE = re.compile(r"(?<![\w.\-])code/")


def _refs_wiki(s):
    """True if the string references the .memory/wiki path (either slash style)."""
    return bool(s) and ".memory/wiki" in s.replace("\\", "/")


def _refs_code(s):
    """True if the string references the project code/ dir (either slash style)."""
    if not s:
        return False
    s = s.replace("\\", "/")
    return bool(_CODE_RE.search(s)) or s.rstrip("/") == "code" or s.endswith("/code")


def nudge(tool_name, tool_input):
    """Return the reminder string if this call is a grep over .memory/wiki or code/, else None."""
    ti = tool_input or {}
    if tool_name == "Grep":
        for v in (ti.get("path"), ti.get("glob")):
            if _refs_wiki(v):
                return _MSG
            if _refs_code(v):
                return _MSG_CODE
        return None
    if tool_name in ("Bash", "PowerShell", "exec"):
        cmd = ti.get("command", "")
        if _GREP_RE.search(cmd):
            if _refs_wiki(cmd):
                return _MSG
            if _refs_code(cmd):
                return _MSG_CODE
        return None
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed/empty input -> stay silent, never break the tool
    msg = nudge(data.get("tool_name"), data.get("tool_input"))
    if msg:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": msg}}))


def selfcheck():
    assert nudge("Grep", {"path": ".memory/wiki/concepts/x.md"}), "Grep wiki path -> fire"
    assert nudge("Grep", {"glob": "**/.memory/wiki/**"}), "Grep wiki glob -> fire"
    assert nudge("Grep", {"path": "src/"}) is None, "Grep elsewhere -> silent"
    assert nudge("Grep", {"pattern": "foo"}) is None, "Grep no path -> silent"
    assert nudge("Bash", {"command": "grep -n foo .memory/wiki/log.md"}), "Bash grep wiki -> fire"
    assert nudge("exec", {"command": "rg foo .memory/wiki/log.md"}), "Codex exec grep wiki -> fire"
    assert nudge("Bash", {"command": "rg foo .memory\\wiki"}), "Bash rg backslash wiki -> fire"
    assert nudge("Bash", {"command": "grep foo src/main.py"}) is None, "Bash grep elsewhere -> silent"
    assert nudge("Bash", {"command": "ls .memory/wiki"}) is None, "Bash non-grep wiki -> silent"
    assert nudge("Bash", {"command": "cat .memory/wiki/log.md"}) is None, "Bash cat -> silent"
    assert nudge("Bash", {"command": "python .memory/tools/structural.py search foo"}) is None, \
        "already using search -> silent"
    assert nudge("PowerShell", {"command": "Select-String foo .memory/wiki/log.md"}), "PS select-string wiki -> fire"
    assert nudge("PowerShell", {"command": "sls foo .memory\\wiki"}), "PS sls backslash wiki -> fire"
    assert nudge("PowerShell", {"command": "rg foo .memory/wiki"}), "PS rg wiki -> fire"
    assert nudge("PowerShell", {"command": "Get-Content .memory/wiki/log.md"}) is None, "PS non-grep wiki -> silent"
    assert nudge("Write", {"file_path": ".memory/wiki/x.md"}) is None, "non-grep tool -> silent"

    # code/ branch -> Serena message
    def code_msg(r): return r is not None and "Serena" in r
    assert code_msg(nudge("Grep", {"path": "code/repo1/src"})), "Grep code path -> Serena"
    assert code_msg(nudge("Grep", {"path": "code"})), "Grep bare code dir -> Serena"
    assert code_msg(nudge("Grep", {"path": "D:/x/KISA_stack/code"})), "Grep abs .../code -> Serena"
    assert code_msg(nudge("Grep", {"glob": "code/**/*.py"})), "Grep code glob -> Serena"
    assert nudge("Grep", {"path": ".vscode/settings.json"}) is None, ".vscode -> silent"
    assert nudge("Grep", {"path": "mycode/x"}) is None, "mycode -> silent"
    assert code_msg(nudge("Bash", {"command": "grep -rn foo code/repo1"})), "Bash grep code -> Serena"
    assert code_msg(nudge("exec", {"command": "rg foo code/repo1"})), "Codex exec grep code -> Serena"
    assert code_msg(nudge("PowerShell", {"command": r"sls foo code\repo1"})), "PS backslash code -> Serena"
    assert nudge("Bash", {"command": "ls code/"}) is None, "Bash non-grep code -> silent"
    assert nudge("Bash", {"command": "grep foo my.code/x"}) is None, "my.code -> silent"
    assert nudge("Bash", {"command": "grep -rn code src/"}) is None, \
        "bare `code` as pattern/arg -> silent (documented ceiling)"
    assert nudge("Bash", {"command": "grep foo .memory/wiki/x.md code/y"}) == _MSG, \
        "wiki+code both -> wiki branch wins (checked first)"
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
