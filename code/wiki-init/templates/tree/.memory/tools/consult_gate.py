#!/usr/bin/env python3
"""Stop hook — consultation gate (closes the "said да, never looked" hole).

consult_nudge.py makes the model declare aloud («Memory check: yes/нет») whether the
answer depends on project memory; the directive then demands a search — but
that demand was prose only. This gate makes it physical: at turn end, if any
assistant text block in the CURRENT turn opens with «Memory check: yes», the same turn
must contain at least one memory access — a tool_use whose input references
structural.py, .memory/wiki or .memory/STATE.md (search/section/active/resume,
Read/Grep of a wiki page all count). Missing -> block the stop; the injected
reason tells the model to actually search and finish properly.

Stage glyph (search-ladder indicator, session 58): the declaration line may carry
the stage — «Memory check: yes · 🎯 <page>» (search hit) / «· 🗺 <page>» (picked from the
map) / «· ∅» (map seen, genuinely not in wiki). 🗺/∅ claim the map was looked at,
so the same turn must contain a tool_result with the map header ("MAP (") —
structural.py prints it on `search` weak/miss and on `map`. 🎯 claims a HIT, so
the turn must contain proof of one: a "STATUS: hit" tool_result (printed by
structural.py search, unforgeable by the model) OR a direct page touch (Read/Grep
of a concrete wiki page, or `structural.py section`). Claimed but absent ->
block. A missing glyph is never blocked (older sessions, skills).

Loop safety: stop_hook_active=true -> always allow (one block per turn max).
Any parse/IO error -> allow silently (a gate must never brick the session).

Self-test: `python consult_gate.py --selfcheck` (assert-based, no framework).
"""
import json
import re
import sys

# evidence substrings, checked against a slash-normalized dump of tool_use input
# ponytail: substring match — editing a memory tool also counts as evidence;
# false-allow is the benign direction, a stricter parse isn't worth it.
_EVIDENCE = ("structural.py", ".memory/wiki", ".memory/STATE.md")

# appended to every block reason: the rejected answer is ALREADY on the user's screen —
# a Stop hook cannot retract it, so a full rewrite reads as a duplicate (session 65 UX catch)
_TAIL = ' The rejected answer was already shown. Do not repeat it in full: provide only the corrected memory-check line and substantive changes supported by the search.'

_REASON = 'Memory check gate: this turn declares "Memory check: yes" without accessing project memory. Run python .memory/tools/structural.py search "<terms>", use the results, and finish the turn. If yes was mistaken, correct the declaration to no.' + _TAIL


_REASON_MAP = 'Memory check gate: the 🗺/∅ marker has no memory-map evidence this turn. Run structural.py map or search (weak/miss prints the map), select a page, and provide only the necessary correction.' + _TAIL


# declaration = FIRST non-empty line of a text block; prose mentions mid-text don't count
_DECL_ANY_RE = re.compile(r"^Memory check:\s*(yes|no)\b", re.I)
_GLYPH_RE = re.compile(r"(🎯|🗺|∅)")
_MAP_MARK = "MAP ("  # printed by structural.py search (weak/miss) and map — keep in sync
_HIT_MARK = "STATUS: hit"  # printed by structural.py search on n>=2 — keep in sync
# direct-page-touch substrings (slash-normalized blob): a CONCRETE page, not just any
# memory reference — this is what lets 🎯 stand without a search hit
_HIT_TOUCH = (".memory/wiki/concepts", ".memory/wiki/decisions", "structural.py section")

_REASON_HIT = 'Memory check gate: the 🎯 marker has neither STATUS: hit nor a concrete wiki-page read this turn. Find supporting evidence or correct the marker after consulting the map.' + _TAIL


def _is_prompt(o):
    """Real user prompt: non-meta, non-sidechain user entry without tool_result blocks."""
    if o.get("type") != "user" or o.get("isSidechain") or o.get("isMeta"):
        return False
    c = (o.get("message") or {}).get("content")
    return not (isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c
    ))


def _split_turns(entries):
    """Split into turns: list of (prompt_entry, entries_after_it_until_next_prompt)."""
    turns = []
    for o in entries:
        if _is_prompt(o):
            turns.append((o, []))
        elif turns:
            turns[-1][1].append(o)
    return turns


def _result_text(o):
    """Concatenated text of tool_result blocks in a user entry ('' if none)."""
    c = (o.get("message") or {}).get("content")
    if not isinstance(c, list):
        return ""
    out = []
    for b in c:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            rc = b.get("content")
            if isinstance(rc, str):
                out.append(rc)
            elif isinstance(rc, list):
                out.extend(x.get("text", "") for x in rc if isinstance(x, dict))
    return "\n".join(out)


def _analyze(body):
    """(decl, evidence, glyph, saw_map, hit_ok) for one turn body.
    decl: 'да' | 'нет' | None; glyph: '🎯' | '🗺' | '∅' | None (from the declaration line);
    saw_map: a tool_result contained the wiki-map header; hit_ok: proof of a hit exists —
    a "STATUS: hit" tool_result OR a direct concrete-page touch in tool_use input."""
    decl, evidence, glyph, saw_map, hit_ok = None, False, None, False, False
    for o in body:
        if o.get("isSidechain"):
            continue
        if o.get("type") == "user":
            rt = _result_text(o)
            if _MAP_MARK in rt:
                saw_map = True
            if _HIT_MARK in rt:
                hit_ok = True
            continue
        if o.get("type") != "assistant":
            continue
        for b in (o.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                first = next(
                    (ln.strip() for ln in b.get("text", "").splitlines() if ln.strip()), ""
                )
                m = _DECL_ANY_RE.match(first)
                if m:
                    decl = m.group(1).lower()
                    g = _GLYPH_RE.search(first)
                    glyph = g.group(1) if g else None
            elif b.get("type") == "tool_use":
                blob = (json.dumps(b.get("input") or {}, ensure_ascii=False)
                        .replace("\\\\", "/").replace("\\", "/"))
                if any(s in blob for s in _EVIDENCE):
                    evidence = True
                if any(s in blob for s in _HIT_TOUCH):
                    hit_ok = True
    return decl, evidence, glyph, saw_map, hit_ok


def check(entries):
    """Return a block reason, or None to allow the stop."""
    turns = _split_turns(entries)
    if not turns:
        return None
    decl, evidence, glyph, saw_map, hit_ok = _analyze(turns[-1][1])
    if decl == "yes" and not evidence:
        return _REASON
    if glyph in ("🗺", "∅") and not saw_map:
        return _REASON_MAP
    if glyph == "🎯" and not hit_ok:
        return _REASON_HIT
    return None


def stats(project_dir, days):
    """Scan session transcripts of the last `days` days, print сверка counters."""
    import glob
    import os
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = {"yes": 0, "no": 0, None: 0}
    g = {"🎯": 0, "🗺": 0, "∅": 0, None: 0}
    da_no_ev = net_ev = hit_lie = total = 0
    sessions = set()
    for path in glob.glob(os.path.join(project_dir, "*.jsonl")):
        if os.path.getmtime(path) < cutoff.timestamp():
            continue  # file untouched since cutoff -> no turns in window
        entries = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        for prompt, body in _split_turns(entries):
            ts = prompt.get("timestamp", "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if when < cutoff:
                continue
            if not any(o.get("type") == "assistant" for o in body):
                continue  # interrupted / empty turn
            decl, ev, glyph, _, hit_ok = _analyze(body)
            total += 1
            n[decl] += 1
            if decl == "yes":
                g[glyph] += 1
                if not ev:
                    da_no_ev += 1
                if glyph == "🎯" and not hit_ok:
                    hit_lie += 1
            if decl == "no" and ev:
                net_ev += 1
            sessions.add(path)
    if not total:
        print(f"сверка-статистика: за {days} дн ходов не найдено")
        return
    def pct(x):
        return f"{x} ({round(100 * x / total)}%)"
    print(f"сверка-статистика за {days} дн — {len(sessions)} сессий, {total} ходов")
    print(f"  да:          {pct(n['yes'])}")
    print(f"    без поиска: {da_no_ev}  <- цель гейта")
    print(f"  нет:         {pct(n['no'])}")
    print(f"    но заглянул: {net_ev}")
    print(f"  без маркера: {pct(n[None])}")
    print(f"  этапы «да»:  🎯 {g['🎯']} · 🗺 {g['🗺']} · ∅ {g['∅']} · без значка {g[None]}")
    print(f"    🎯 без hit-доказательства: {hit_lie}  <- цель 🎯-проверки")


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("stop_hook_active"):
            return
        entries = []
        with open(data["transcript_path"], encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        reason = check(entries)
    except Exception:
        return  # gate must never brick the session
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))


# --- selfcheck ---------------------------------------------------------------

def _u(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def _tr():
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x"}]}}


def _tr_text(txt, as_str=False):
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x",
                                     "content": txt if as_str
                                     else [{"type": "text", "text": txt}]}]}}


def _a_text(text, side=False):
    return {"type": "assistant", "isSidechain": side,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _a_tool(name, inp, side=False):
    return {"type": "assistant", "isSidechain": side,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": name, "input": inp}]}}


def selfcheck():
    da, net = _a_text("Memory check: yes\nОтвет."), _a_text("Memory check: no\nОтвет.")
    da_paren = _a_text("Memory check: yes (resume сам грузит)")
    search = _a_tool("Bash", {"command": "python .memory/tools/structural.py search \"foo\""})
    read_wiki = _a_tool("Read", {"file_path": "D:\\x\\.memory\\wiki\\concepts\\a.md"})
    grep_wiki = _a_tool("Grep", {"pattern": "foo", "path": ".memory/wiki"})
    other = _a_tool("Bash", {"command": "git status"})

    assert check([_u("q"), da, other]) is not None, "да + no memory access -> block"
    assert check([_u("q"), da, search]) is None, "да + structural.py -> allow"
    assert check([_u("q"), da_paren, _a_tool(
        "Bash", {"command": "python .memory/tools/structural.py resume"})]) is None, \
        "да(...) + resume -> allow"
    assert check([_u("q"), da, read_wiki]) is None, "да + Read wiki (backslashes) -> allow"
    assert check([_u("q"), da, grep_wiki]) is None, "да + Grep wiki -> allow"
    assert check([_u("q"), net, other]) is None, "нет -> allow"
    assert check([_u("q"), _a_text("Обычный ответ."), other]) is None, "no declaration -> allow"
    assert check([_u("q"), _a_text("Правило: сказал «Memory check: yes» → ищи.")]) is None, \
        "prose mention mid-text -> not a declaration"
    assert check([_u("old"), da, other, _u("new"), net]) is None, \
        "declaration in a PREVIOUS turn -> ignored"
    assert check([_u("q"), da, _tr(), other]) is not None, \
        "tool_result user entry does not reset the turn boundary"
    assert check([_u("q"), _a_text("Memory check: yes", side=True), other]) is None, \
        "sidechain declaration -> ignored"
    assert check([_u("q"), da, _a_tool("Bash", {"command": "x"}, side=True),
                  _a_tool("Bash", {"command": "python .memory/tools/structural.py search y"})]
                 ) is None, "evidence in main chain counts"
    assert check([_u("meta", isMeta=True)]) is None, "meta-only transcript -> allow"
    assert check([]) is None, "empty transcript -> allow"

    # stage glyphs: 🗺/∅ claim the map was seen -> a tool_result with "MAP (" must exist;
    # 🎯 claims a hit -> a "STATUS: hit" tool_result OR a concrete-page touch must exist
    da_hit = _a_text("Memory check: yes · 🎯 memory-consult\nОтвет.")
    da_map = _a_text("Memory check: yes · 🗺 concepts/x.md\nОтвет.")
    da_nil = _a_text("Memory check: yes · ∅\nОтвет.")
    map_res = _tr_text("STATUS: miss → map\nMAP (вся память — выбери страницу сам):\n  x.md — X")
    hit_res = _tr_text("STATUS: hit n=5\nconcepts/a.md: …snippet…")
    section_cmd = _a_tool("Bash", {"command": 'python .memory/tools/structural.py section overview.md "## X"'})
    assert check([_u("q"), da_hit, search, hit_res]) is None, "🎯 + STATUS hit -> allow"
    assert check([_u("q"), da_hit, search]) is not None, "🎯 + search w/o hit proof -> block"
    assert check([_u("q"), da_hit, search, map_res]) is not None, "🎯 while map fired -> block"
    assert check([_u("q"), da_hit, read_wiki]) is None, "🎯 + concrete page Read -> allow"
    assert check([_u("q"), da_hit, section_cmd]) is None, "🎯 + section -> allow"
    assert check([_u("q"), da_map, search, map_res]) is None, "🗺 + map result -> allow"
    assert check([_u("q"), da_map, search]) is not None, "🗺 without map -> block"
    assert check([_u("q"), da_nil, search, map_res]) is None, "∅ + map result -> allow"
    assert check([_u("q"), da_nil, search]) is not None, "∅ without map -> block"
    assert check([_u("q"), da_nil, search,
                  _tr_text("MAP (вся память):\n  x.md — X", as_str=True)]) is None, \
        "map result as plain-string content also counts"
    assert check([_u("q"), da, search, _a_text("в прозе 🗺 не значок")]) is None, \
        "glyph mid-prose (not on the declaration line) -> ignored"
    assert check([_u("q"), da_hit, search, map_res, da_nil]) is None, \
        "corrected final declaration overrides an earlier glyph (last wins)"

    # stats internals: _split_turns + _analyze over a multi-turn transcript
    turns = _split_turns([_u("q1"), da, search, _u("q2"), net, other, _u("q3"), da, _tr(), other])
    assert len(turns) == 3, "3 prompts -> 3 turns"
    assert _analyze(turns[0][1]) == ("yes", True, None, False, False), "turn1: да + search"
    assert _analyze(turns[1][1]) == ("no", False, None, False, False), "turn2: нет"
    assert _analyze(turns[2][1]) == ("yes", False, None, False, False), "turn3: tool_result no reset"
    assert _analyze([_a_text("Обычный ответ."), read_wiki]) == (None, True, None, False, True), \
        "no marker + wiki read (concrete page = hit proof)"
    assert _analyze([da_hit, search]) == ("yes", True, "🎯", False, False), "glyph off decl line"
    assert _analyze([da_map, map_res])[3] is True, "map result detected"
    assert _analyze([da_hit, hit_res])[4] is True, "STATUS hit detected in tool_result"

    # every block reason must carry the no-rewrite tail (the block is post-emission)
    for r in (check([_u("q"), da, other]),
              check([_u("q"), da_map, search]),
              check([_u("q"), da_hit, search])):
        assert r and r.endswith(_TAIL), f"reason without _TAIL: {r!r}"
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    elif "--stats" in sys.argv:
        import os
        args = [a for a in sys.argv[1:] if a != "--stats"]
        days = int(args[0]) if args else 7
        munged = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
        stats(os.path.expanduser(os.path.join("~", ".claude", "projects", munged)), days)
    else:
        main()
