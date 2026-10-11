#!/usr/bin/env python3
"""PreToolUse SOFT nudge, two branches:
- a Grep/Bash/PowerShell/exec-grep targeting .memory/wiki/ -> remind to prefer
  `structural.py search` (FTS5, porter-stemmed — word-forms grep can't match);

SOFT = non-blocking: emits hookSpecificOutput.additionalContext, NEVER denies, NEVER
crashes the tool (any error -> stay silent).

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

# grep-family invocation tokens; \bgrep\b deliberately won't match inside 'ripgrep'
_GREP_RE = re.compile(r"\b(grep|egrep|fgrep|rg|ripgrep)\b|select-string|\bsls\b", re.I)

def _refs_wiki(s):
    """True if the string references the .memory/wiki path (either slash style)."""
    return bool(s) and ".memory/wiki" in s.replace("\\", "/")


def nudge(tool_name, tool_input):
    """Return the reminder string if this call is a grep over .memory/wiki, else None."""
    ti = tool_input or {}
    if tool_name == "Grep":
        for v in (ti.get("path"), ti.get("glob")):
            if _refs_wiki(v):
                return _MSG
        return None
    if tool_name in ("Bash", "PowerShell", "exec"):
        cmd = ti.get("command", "")
        if _GREP_RE.search(cmd):
            if _refs_wiki(cmd):
                return _MSG
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

    assert nudge("Grep", {"path": "code/"}) is None, "project code has no memory-specific advice"
    assert nudge("exec", {"command": "rg foo code/"}) is None
    assert nudge("Bash", {"command": "rg foo .memory/wiki/x.md code/y"}) == _MSG
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
