#!/usr/bin/env python3
"""SessionStart(matcher=compact) anchor: reinject memory directives after compaction.

Compaction rewrites the conversation into a lossy summary — session-start injections
and mid-session discipline get washed out. This hook fires ONLY on source=compact
(the matcher filters; script itself is a constant print) and re-anchors the agent:
where memory lives, the iron rules, and the instruction to checkpoint early because
compacted-away details are unrecoverable from context.

Stdout of a SessionStart hook is added to the model context directly.
ASCII-only on purpose (Windows console codepage safety).
"""
import sys

_ANCHOR = """[reinject after compaction] Context was compacted - conversation details are partially LOST. Restoring memory directives:

## Session memory (wiki + STATE) - re-anchor
- Volatile state: .memory/STATE.md (now / next / open questions). Durable knowledge: .memory/wiki/.
- Wiki content lookups: python .memory/tools/structural.py search "<terms>"; structure: structural.py active|section|toc.
- Iron rules: (1) every wiki claim traces to a source; (2) verify a page exists before writing [[Link]] - a needed-but-unwritten page is [[?slug]] (deliberate TODO), never a bare link; (3) surface contradictions explicitly - mark BOTH living pages (contradicted: <slug> frontmatter + warning callout), structural.py flags reports them; (4) asked something not in the wiki -> say "not in wiki"; (5) decisions are never edited - supersede forward (new file with supersedes: <id>).
- log.md is STRICTLY append-only; decisions/*.md bodies are immutable.
- IMPORTANT: details lost to compaction cannot be recovered from this context. Run /wiki-checkpoint at the first natural pause (do NOT wait for session end) to persist what survived. The pre-compact transcript was archived by the PreCompact hook (pre-compact-archive/ next to the session .jsonl) if raw recovery is ever needed.
"""


def main():
    sys.stdin.read()  # drain hook input; content is irrelevant (matcher already filtered)
    print(_ANCHOR)


if __name__ == "__main__":
    main()
