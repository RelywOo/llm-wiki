#!/usr/bin/env python3
"""Structural pre-extraction pass — non-LLM, 0-token regex over .memory/wiki/.

Extracts the EXPLICIT structure (``[[wikilinks]]`` + decision ``supersedes:`` frontmatter)
deterministically, no LLM. The edge list is an INTERMEDIATE, code-consumed representation;
only bounded computed answers (active set, section slices, trend) are meant to reach a session.

Subcommands:
  resume            one-call /wiki-resume loader: STATE + routine + last run per queue +
                    overview sections + active decisions paired with index summaries +
                    flags + checkpoint delta + change-frontier/stale + pointers list
                    (pointer PAGES not loaded)
  checkpoint        one-call /wiki-checkpoint write-side recon: cp delta + dirty tree +
                    computed session/block/next-decision-id + log/commit skeletons
                    (SLIM by design: pages are Read on the write path anyway)
  decision ID       JSON current decision + full scope-preserving lineage; ambiguity fails
  active            active decision set, newest-first (used by /wiki-resume)
  section F "H"     print the "## H..." section of file F through the next "## " or EOF
  toc F             list file F's "## " headers in order (the cheap nav-aid TOC, ~0 tokens)
  search "<terms>"  full-text search over wiki bodies+titles (FTS5/bm25, snippet result).
                    Prints STATUS first (hit/weak/miss); weak/miss also dumps the wiki map
                    so a lexical miss can never silently end the lookup
  map               the whole-memory map: one line per knowledge page (path — H1 title)
  lint              flag index.md "## Decisions" lines whose prose exceeds the budget
  pack-budget       render the resume pack, print per-section token split + verdict vs
                    the 2400-tok ceiling (decision 0017; over -> exit 1)
  compact           deterministic pack knife (EVICT rung): fold oldest closed decision
                    groups into the archive line until the pack fits; --budget N overrides
                    the ceiling; nothing left to fold -> exit 1 (manual COMPRESS remains)
  wanted            list [[?slug]] markers — pages wanted but not written yet (open TODOs)
  flags             contradicted pairs + confidence:low pages (+ protocol violations)
  last-done         the last log.md `` task-done `` line, or a "none yet" sentinel
  guard             checkpoint integrity: decisions immutable, log append-only, links kept,
                    routine scores stay in tracker.md (not STATE prose), resume pack within
                    budget (vs last checkpoint; violations -> exit 1)
  doctor            contour self-check: Claude/Codex hooks, executable git hard floor,
                    optional Prime adapter syntax/runtime, referenced scripts, FTS5,
                    fence structure, decision frontmatter (failures -> exit 1)
  freshness         decay backstop on durable pages: missing paths + volatile current-state
                    phrasing that belongs in STATE (advisory; also in `guard`; any -> exit 1)
  state-reset       wipe .memory/STATE.md to the idle template — no questions, no keeps;
                    an uncommitted STATE is committed first (git is its only copy)
  tracker           daily-routine habit report from .memory/tracker.md: today's cells, the
                    7-day grid, per-item streaks, unlogged gaps (also inside `resume`)
  tracker-log I=D/T append ONE routine event; `--date YYYY-MM-DD` sets the day, `--blocked R`
                    marks a day a rule stopped (⏸, never a miss). Append-only, last wins
  help              print this subcommand catalog
  selfcheck         run invariants self-check
  (none)            full edge report
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from git_checked import (GitIntegrityError as GitCommandError,
                         git_bytes as _git_bytes,
                         git_text as _git_text,
                         require_repo as _git_repo_preflight,
                         run_git as _run_git)
from wiki_validation import new_dangling_link_violations, provenance_violations
from markdown_io import MarkdownReadError, read_markdown

# Codex captures UTF-8; Windows' inherited cp1251 cannot print valid wiki text such as `×`.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WIKI = Path(__file__).resolve().parents[2] / ".memory" / "wiki"
LOG = WIKI / "log.md"

FENCED = re.compile(r"```.*?```", re.S)
FENCE_OPEN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})[^\n]*$")
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
INLINE = re.compile(r"`[^`]*`")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
# meta-doc placeholders: [[slug]]/[[owner]]-style tokens used when a page DOCUMENTS the
# link syntax itself (decision 0017 does) — never real pages, never broken links
PLACEHOLDER_SLUGS = frozenset({"slug", "owner", "wikilink", "link"})
SUPERSEDES = re.compile(r"^supersedes:\s*(\d{4})\s*$", re.M)
SUPERSEDES_KEY = re.compile(r"^supersedes\s*:", re.M)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---", re.S)
CONTRADICTED = re.compile(r"^contradicted:\s*(\S+)\s*$", re.M)
CONTRADICTED_KEY = re.compile(r"^contradicted\s*:", re.M)
LOW_CONF = re.compile(r"^confidence:\s*low\s*$", re.M)
LOW_CONF_KEY = re.compile(r"^confidence\s*:\s*low\b", re.M)
CONTRADICTION_CALLOUT = re.compile(
    r"^[ ]{0,3}>\s*\[!warning\]\s+CONTRADICTS\s+\[\[([^]#|]+)\]\]\s*:\s*(\S.*)$",
    re.I | re.M)
WARNING_CALLOUT = re.compile(
    r"^[ ]{0,3}>\s*\[!warning\](?:[ \t]+(\S.*)|[ \t]*\n[ ]{0,3}>\s*(\S.*))",
    re.I | re.M)
ID_PREFIX = re.compile(r"^(\d+)")
DECISION_INDEX_LINK = re.compile(
    r"^\s*-\s*\[(\d{4})\]\((decisions/((\d{4})-[^)]+\.md))\)\s+(?:-|—)\s+(\S.*)\s*$")
DECISION_INDEX_LEGACY = re.compile(
    r"^\s*-\s*((\d{4})-[^\s—]+)\s+(?:-|—)\s+(\S.*)\s*$")
H1 = re.compile(r"^#\s+(.*)", re.M)
TERMS = re.compile(r"[^\W_]+")  # unicode word chars — wiki content is RU/EN mixed

# --- freshness backstop (tail measure, session 7) — deterministic decay catches on
# DURABLE pages. Two clean anchors; the semantic rest stays the write-side discipline's job.
# missing-path anchor: ONLY explicit repo-relative prefixes (.memory/ or code/) — an
# unambiguous base. Bare `foo/bar.py` is dropped on purpose: it's usually an ABBREVIATED
# or nested/vendor path (code/backend/.../payments/service.py cited as `payments/service.py`)
# and resolving it deterministically across a heterogeneous fleet is pure false-positive noise.
FRESH_PATH = re.compile(r"(?<![\w./])((?:\.memory/|code/)[\w./\-]+)")
FRESH_PATH_META = re.compile(r"[*<>?]|\.\.|NNNN|YYYY|<slug>|<id>|<old>|<file>|<topic>|<other>|_$|-$")
FRESH_URL_LINE = re.compile(r"https?://|github\.com|\]\(")
# volatile CURRENT-STATE status that belongs in STATE, not a durable page (fence rule) — the
# deterministic arm of the write-side "reference-not-copy" rule. STRONG build-status signals
# only (a deliverable that isn't there yet); the weak temporal markers ("not yet", "coming
# soon", "пока не", TODO) are dropped — they describe mechanisms/research too and only add
# noise. RU+EN. Advisory (recall-first): the checkpoint model confirms. The impl-detail class
# (a raw count/config value) is not regex-catchable and stays the prose discipline's job.
FRESH_VOLATILE = re.compile(
    r"\bplaceholder\b|\bawaiting\b|\bTBD\b|"
    r"(?:hasn'?t|haven'?t|isn'?t|aren'?t|not) (?:yet )?(?:been )?"
    r"(?:provided|supplied|added|filled(?: in)?|uploaded|delivered)\b|"
    r"заглушк\w*|плейсхолдер\w*|не (?:предоставл|прислал|дал|получен|загруж|готов\w*\s+файл)\w*|"
    r"ожида\w+ (?:фото|данн\w*|файл\w*|контент\w*|материал\w*)", re.I)


def _strip_code(text: str) -> str:
    # kill fenced then inline code so doc-text like `[[wikilinks]]` never matches
    return INLINE.sub("", FENCED.sub("", text))


def _strip_markdown_fences(text: str) -> str:
    """Drop CommonMark-style backtick/tilde fences, including unclosed fences to EOF."""
    out, fence_char, fence_len = [], None, 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if fence_char is None:
            opening = FENCE_OPEN.match(bare)
            if opening:
                run = opening.group(1)
                fence_char, fence_len = run[0], len(run)
            else:
                out.append(line)
            continue
        closing = re.match(r"^[ ]{0,3}([`~]+)[ \t]*$", bare)
        if closing:
            run = closing.group(1)
            if run[0] == fence_char and len(run) >= fence_len:
                fence_char, fence_len = None, 0
    return "".join(out)


def _visible_markdown_body(text: str) -> str:
    """Rendered body surface for protocol callouts: no frontmatter/comments/code fences."""
    text = _norm_eol(text)
    fm = FRONTMATTER.match(text)
    if fm:
        text = text[fm.end():]
    return INLINE.sub("", HTML_COMMENT.sub("", _strip_markdown_fences(text)))


def _slug(path: Path) -> str:
    return path.stem.lower()


def _supersession_cycle_nodes(graph: dict[str, str]) -> set[str]:
    """Nodes participating in cycles in a one-outgoing-edge supersession graph."""
    state, stack, bad = {}, [], set()

    def visit(node):
        state[node] = 1
        stack.append(node)
        nxt = graph.get(node)
        if nxt in graph:
            if state.get(nxt, 0) == 0:
                visit(nxt)
            elif state.get(nxt) == 1:
                bad.update(stack[stack.index(nxt):])
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node)
    return bad


def extract(wiki: Path = WIKI):
    """Return (edges, warnings, superseded, active). edges = list of (src, relation, dst).

    ``supersedes`` is executable metadata, so it is read ONLY from strict frontmatter of a
    decision file. Prose, concepts and fenced examples are inert. Malformed, ambiguous,
    self-referential or cyclic edges are reported and never alter the active set.
    """
    files = sorted(wiki.rglob("*.md"))
    slug_map = {_slug(f): f for f in files}  # slug -> path, directory-agnostic
    by_id = {}
    for f in files:
        m = ID_PREFIX.match(f.stem)
        if f.parent == wiki / "decisions" and m:
            by_id.setdefault(m.group(1), []).append(f)
    decisions = {did: paths[0] for did, paths in by_id.items()}

    edges, warnings = [], []
    candidates = {}  # new decision id -> old decision id
    source_file = {}
    for did, paths in sorted(by_id.items()):
        if len(paths) > 1:
            warnings.append(f"duplicate decision id {did}: "
                            + ", ".join(p.name for p in paths))
            continue
        f = paths[0]
        raw = read_markdown(f)
        fm = FRONTMATTER.match(_norm_eol(raw))
        if not fm:
            continue  # doctor owns the hard schema error; no metadata executes
        body = fm.group(1)
        keys = SUPERSEDES_KEY.findall(body)
        vals = SUPERSEDES.findall(body)
        if not keys:
            continue
        if len(keys) != 1 or len(vals) != 1:
            warnings.append(f"{f.name}: malformed/duplicate supersedes frontmatter")
            continue
        old = vals[0]
        if old == did:
            warnings.append(f"{f.name}: supersedes itself ({did})")
            continue
        if old not in decisions:
            warnings.append(f"{f.name}: supersedes {old} -> no decision file")
            continue
        candidates[did] = old
        source_file[did] = f

    cycle_nodes = _supersession_cycle_nodes(candidates)
    if cycle_nodes:
        warnings.append("supersedes cycle: " + " -> ".join(sorted(cycle_nodes)))
    superseded = set()
    for new, old in candidates.items():
        if new in cycle_nodes:
            continue  # invalid graph metadata must never change computed truth
        superseded.add(old)
        edges.append((_slug(source_file[new]), "supersedes", _slug(decisions[old])))

    for f in files:
        raw = read_markdown(f)
        src = _slug(f)
        seen = set()
        for m in WIKILINK.findall(_strip_code(raw)):
            target = m.split("#", 1)[0].strip().lower()  # drop #heading anchor
            if not target or (src, target) in seen:
                continue
            seen.add((src, target))
            if target.lstrip("?").strip() in PLACEHOLDER_SLUGS:
                continue  # syntax-doc placeholder, not a reference
            if target.startswith("?"):
                # [[?slug]] = deliberate page-wanted TODO (iron rule 2 stays strict for
                # plain links; the "?" is the ONLY legal way to reference a not-yet page)
                want = target[1:].strip()
                if not want:
                    continue
                if want in slug_map:
                    warnings.append(f"{f.name}: [[?{want}]] resolved — page exists, drop the '?'")
                else:
                    edges.append((src, "wants", want))
            elif target in slug_map:
                edges.append((src, "references", target))
            else:
                warnings.append(f"{f.name}: broken [[{m}]]")

    active = sorted(i for i in decisions if i not in superseded)
    return edges, warnings, sorted(superseded), active


def decision_packet(identifier: str, wiki: Path = WIKI) -> dict:
    """Resolve a decision in either direction without inferring partial replacements.

    Full lineage remains available: an unchanged scope may be defined by an ancestor.
    Branches are ambiguous, never arbitrarily reduced to the largest/newest ID.
    """
    files = sorted((wiki / "decisions").glob("*.md"))
    problems = _doctor_decisions({p.name: read_markdown(p) for p in files})
    if problems:
        raise ValueError("invalid decision graph: " + "; ".join(problems))
    by_id = {p.stem[:4]: p for p in files}
    did = Path(identifier).name.split("-", 1)[0].removesuffix(".md")
    if did not in by_id:
        raise ValueError(f"unknown decision: {identifier}")
    parent, children = {}, {}
    for key, path in by_id.items():
        fm = FRONTMATTER.match(_norm_eol(read_markdown(path)))
        found = SUPERSEDES.search(fm.group(1)) if fm else None
        if found:
            old = found.group(1)
            parent[key] = old
            children.setdefault(old, []).append(key)
    first = did
    while first in parent:
        first = parent[first]
    chain, node = [], first
    while True:
        chain.append(node)
        next_ids = children.get(node, [])
        if len(next_ids) > 1:
            raise ValueError(f"ambiguous successors of {node}: {', '.join(next_ids)}; "
                             "review scopes explicitly before applying a rule")
        if not next_ids:
            break
        node = next_ids[0]
    return {
        "requested": did, "current": node,
        "status": "current" if did == node else "superseded",
        "instruction": "Apply changes oldest to newest by scope. Preserve unchanged scopes. "
                       "Do not treat historical values as current. If scope is unclear, report unknown.",
        "lineage": [{"id": key, "path": ".memory/wiki/" + by_id[key].relative_to(wiki).as_posix(),
                     "status": "current" if key == node else "superseded",
                     "text": read_markdown(by_id[key])} for key in chain],
    }


def decision_read_notice(path: Path, wiki: Path = WIKI) -> str:
    if path.resolve().parent != (wiki / "decisions").resolve():
        return ""
    try:
        packet = decision_packet(path.name, wiki)
    except ValueError as exc:
        return f"DECISION UNRESOLVED: {exc}"
    if packet["status"] == "superseded":
        return (f"SUPERSEDED: {packet['requested']} is historical; current successor "
                f"{packet['current']}. Before applying: structural.py decision {packet['requested']}")
    return (f"CURRENT decision {packet['current']}; resolve inherited scopes with "
            f"structural.py decision {packet['current']}")


def section(text: str, header_prefix: str) -> str | None:
    """Return the first ``## `` section whose title startswith ``header_prefix``.

    0-token slice primitive: resume/checkpoint FULL-load overview.md/index.md but only
    consume a few named sections — route those reads through this so the big chunks never
    enter context. Matches the first "## " line whose text (after stripping the "## ")
    startswith the prefix (the prefix may be passed with or without a leading "## "), and
    returns from that header line through the line BEFORE the next "## " header — or
    through EOF if none follows. The EOF/last-section case is generic: it never assumes
    which header is last.
    """
    want = header_prefix.strip()
    if want.startswith("## "):
        want = want[3:].strip()
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and ln[3:].strip().startswith(want):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def section_exact(text: str, title: str) -> str | None:
    """Return one exact ``## title`` section (unlike the navigation prefix matcher)."""
    lines = text.splitlines()
    header = f"## {title.strip()}"
    starts = [i for i, line in enumerate(lines) if line.rstrip() == header]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def toc(text: str) -> list[str]:
    """Return the page's ``## `` header titles in document order — the cheap nav-aid TOC.

    Lists EXACTLY the headers ``section()`` can slice (same ``startswith("## ")`` criterion),
    so the TOC the agent reads and the section it then loads always agree. ~0 tokens (tens of
    header strings vs a whole page). At /wiki-checkpoint the agent reads this to confirm — or
    reroute — an edit-locate from the page structure before loading ONE section, the signal that
    makes the nav-aid safe (idea #7, decision 0008). The leading ``## `` is stripped, mirroring
    ``section()``'s prefix handling, so a title here feeds straight back into ``section``.
    """
    return [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]


def _resolve_page(arg: str) -> Path | None:
    """Resolve a page argument in every spelling callers actually use.

    Accepts, in order: absolute path; repo-root-relative (``.memory/...``); cwd-relative;
    wiki-relative as ``search``/``map``/resume print them (``concepts/x.md``, backslashes
    ok); a bare filename/slug found anywhere under the wiki. Returns None when nothing
    exists — the caller owns the error message. Born of a live session where the model fed
    ``section`` the path straight from ``search`` output and got 3x "file not found".
    """
    p = Path(arg.replace("\\", "/"))
    if p.is_absolute():
        return p if p.exists() else None
    if p.parts and p.parts[0] == ".memory":
        p = WIKI.parents[1] / p
        return p if p.exists() else None
    for cand in (p, WIKI / p):
        if cand.exists():
            return cand
    name = p.name if p.name.endswith(".md") else p.name + ".md"
    return next(WIKI.rglob(name), None)


def parse_decision_index_line(line: str):
    """Parse canonical/legacy decision summary -> (id, target path, summary, rendered)."""
    m = DECISION_INDEX_LINK.match(line)
    if m:
        label_id, target, _stem, path_id, summary = m.groups()
        if label_id != path_id:
            return None
        return label_id, target, summary.strip(), line.strip()
    m = DECISION_INDEX_LEGACY.match(line)
    if m:
        stem, did, summary = m.groups()
        return did, f"decisions/{stem}.md", summary.strip(), line.strip()
    return None


def _decision_inventory(wiki: Path = WIKI) -> dict[str, str]:
    """Decision id -> unique wiki-relative path; duplicate ids are owned by doctor()."""
    out = {}
    decisions = wiki / "decisions"
    if not decisions.is_dir():
        return out
    for f in sorted(decisions.glob("*.md")):
        m = ID_PREFIX.match(f.stem)
        if m and m.group(1) not in out:
            out[m.group(1)] = str(f.relative_to(wiki)).replace("\\", "/")
    return out


def decision_index_records(index_text: str, inventory: dict[str, str] | None = None):
    """Return ({id: rendered line}, problems) for the exact Decisions section."""
    block = section_exact(index_text, "Decisions")
    if block is None:
        return {}, ["missing or duplicate exact `## Decisions` section"]
    records, targets, problems = {}, {}, []
    for line in block.splitlines():
        stripped = line.strip()
        if (not stripped or stripped.startswith("## ") or stripped == "(none yet)"
                or (stripped.startswith("[source:") and stripped.endswith("]"))):
            continue
        parsed = parse_decision_index_line(line)
        if not parsed:
            problems.append(f"unrecognized decision summary line: {stripped[:100]}")
            continue
        did, target, _summary, rendered = parsed
        if did in records:
            problems.append(f"duplicate decision summary id {did}")
            continue
        records[did], targets[did] = rendered, target
    if inventory is not None:
        for did, target in targets.items():
            expected = inventory.get(did)
            if expected is None:
                problems.append(f"decision summary {did} targets no decision file: {target}")
            elif target != expected:
                problems.append(f"decision summary {did} targets {target}, expected {expected}")
        for did, target in sorted(inventory.items()):
            if did not in records:
                problems.append(f"decision {did} ({target}) lacks an index summary line")
    return records, problems



def _prose_of(line: str) -> str:
    """Return parsed decision prose; non-decision lines stay unchanged for lint visibility."""
    parsed = parse_decision_index_line(line)
    return parsed[2] if parsed else line


def lint_index(index: Path = WIKI / "index.md", limit: int = 100):
    """Advisory 0-token check: flag bloated "## Decisions" prose in index.md.

    Returns [(prose_len, line)] for every line under the "## Decisions" section whose PROSE
    (the WHAT after the ``[id](path) - `` link prefix) exceeds ``limit`` chars. The link
    prefix is fixed overhead and excluded from the measure, so the guard agrees with the
    documented one-line-per-page index policy.
    """
    if not index.exists():
        return []
    block = section_exact(read_markdown(index), "Decisions")
    if block is None:
        return []
    offenders = []
    for ln in block.splitlines():
        if ln.startswith("## "):
            continue  # the header itself
        prose = _prose_of(ln)
        if len(prose) > limit:
            offenders.append((len(prose), ln))
    return offenders


PACK_BUDGET_TOK = 2400   # resume-pack ceiling (decision 0017) — guard blocks a checkpoint over it
PACK_CHARS_PER_TOK = 2.05  # measured chars/token on wiki text, Cyrillic-heavy (decision 0013)
GROUP_LINE = re.compile(r"^-\s*ids:\s*(\d[\d ]*?)\s*::\s*(.+)$")


def parse_groups(index_text: str):
    """PURE. index.md `## Decision groups` lines -> [(set-of-ids, group line)].

    A group collapses a CLOSED track's decisions into ONE resume line (the pack-budget
    compress rung, decision 0017): resume prints the group line at the position of the
    group's newest active member and suppresses the members' individual index lines.
    Line format: `- ids: 0001 0002 :: <вопросы -> вердикты>`. No section -> [] (old
    snapshots / fleet instances without groups keep the ungrouped rendering).
    """
    block = section(index_text, "Decision groups")
    if block is None:
        return []
    out = []
    for ln in block.splitlines():
        m = GROUP_LINE.match(ln.strip())
        if m:
            out.append((set(m.group(1).split()), m.group(2).strip()))
    return out


def _collapse_decisions(active, by_id, groups):
    """PURE. Newest-first decision lines with grouped ids collapsed to ONE line per group.

    Ungrouped actives keep their index one-liners; a grouped id prints its group's line
    once (newest member's position) and later members print nothing.
    """
    gof = {i: gi for gi, (ids, _) in enumerate(groups) for i in ids}
    out, printed = [], set()
    for i in sorted(active, reverse=True):
        gi = gof.get(i)
        if gi is None:
            out.append(by_id.get(i, f"- {i} (no index.md summary line)"))
        elif gi not in printed:
            printed.add(gi)
            out.append(f"- {groups[gi][1]}")
    return out


def pack_size():
    """Render resume_pack() to a string; return (est. tokens, text). Guard's wall measure."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resume_pack()
    text = buf.getvalue()
    return round(len(text) / PACK_CHARS_PER_TOK), text


def _apply_fold(index_text: str, fold_ids: set):
    """PURE. Fold ONE group's ids into the archive line of `## Decision groups`.

    The archive line is the group whose text starts with "архив"; if none exists the
    folded line BECOMES it (same position). The folded group's own line is removed;
    the archive line is rewritten `- ids: <all, sorted> :: архив: N вопросов закрыто —
    decisions/, search найдёт` (topics live in index one-liners + FTS — the parenthetical
    is dropped deliberately). Unknown fold_ids -> text returned unchanged.
    """
    lines = index_text.splitlines()
    in_sec, arch_i, fold_i, arch_ids = False, None, None, set()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            in_sec = ln[3:].strip().startswith("Decision groups")
            continue
        m = GROUP_LINE.match(ln.strip()) if in_sec else None
        if not m:
            continue
        ids, txt = set(m.group(1).split()), m.group(2).strip()
        if txt.startswith("архив"):
            arch_i, arch_ids = i, ids
        elif ids == set(fold_ids):
            fold_i = i
    if fold_i is None:
        return index_text
    merged = sorted(arch_ids | set(fold_ids))
    arch_ln = (f"- ids: {' '.join(merged)} :: архив: {len(merged)} вопросов закрыто — "
               f"decisions/, search найдёт")
    if arch_i is None:
        lines[fold_i] = arch_ln
    else:
        lines[arch_i] = arch_ln
        del lines[fold_i]
    return "\n".join(lines) + ("\n" if index_text.endswith("\n") else "")


def compact(budget: int = PACK_BUDGET_TOK):
    """Deterministic pack knife — the ladder's EVICT rung mechanized (0 tokens, same
    result every run; kills the "6 runs, 6 different cuts" nondeterminism).

    Over budget -> fold closed (non-archive) decision groups into the archive line,
    oldest last-activity first (newest git %cs across the group's decision files;
    tie -> smaller newest id), re-measuring the pack after each fold, until it fits
    or nothing foldable remains. Knowledge untouched: decision bodies, index
    one-liners and FTS stay — only resume VISIBILITY changes. Still over with no
    group left -> exit 1: the COMPRESS rungs (current-block prose, STATE) stay on
    the model. Under budget -> no-op.
    """
    root = WIKI.parents[1]
    _git_repo_preflight(root)
    idx = WIKI / "index.md"
    tok, _ = pack_size()
    if tok <= budget:
        print(f"compact: pack ≈ {tok} tok <= budget {budget} — no-op")
        return 0

    def _age(ids):
        dates = []
        for i in sorted(ids):
            for f in (WIKI / "decisions").glob(f"{i}-*.md"):
                cs = _git_text(
                    root, "log", "-1", "--format=%cs", "--", f.relative_to(root).as_posix())
                dates.append(cs or "9999-12-31")  # untracked = newest by definition
        return max(dates) if dates else "9999-12-31"

    folded = []
    while tok > budget:
        text = read_markdown(idx)
        cands = [(ids, txt) for ids, txt in parse_groups(text) if not txt.startswith("архив")]
        if not cands:
            break
        cands.sort(key=lambda g: (_age(g[0]), max(g[0])))
        ids, txt = cands[0]
        idx.write_text(_apply_fold(text, ids), encoding="utf-8", newline="\n")
        folded.append(txt)
        tok, _ = pack_size()
    for t in folded:
        print(f"folded into archive: {t}")
    if tok > budget:
        print(f"compact: pack ≈ {tok} tok > budget {budget}, no closed group left to fold — "
              f"manual COMPRESS: current-block one-liners / overview bullets / STATE <= 40")
        return 1
    print(f"compact: pack ≈ {tok} tok <= budget {budget} — done, {len(folded)} group(s) folded")
    return 0


def flags(wiki: Path = WIKI):
    """Validate and report the complete contradiction/confidence protocol.

    A contradiction needs reciprocal frontmatter *and* a visible warning callout on each
    page targeting the peer. ``confidence: low`` needs a separate non-empty warning callout
    naming the evidence gap.  Fenced examples are inert.  Returns (pairs, low, warnings).
    """
    files = sorted(wiki.rglob("*.md"))
    slug_map = {_slug(f): f for f in files}
    marks, callouts, low, warnings = {}, {}, [], []
    for f in files:
        raw = read_markdown(f)
        normalized = _norm_eol(raw)
        m = FRONTMATTER.match(normalized)
        src = _slug(f)
        fm = m.group(1) if m else ""
        target_values = [x.strip().lower() for x in CONTRADICTED.findall(fm)]
        target_keys = CONTRADICTED_KEY.findall(fm)
        if target_keys and (len(target_keys) != 1 or len(target_values) != 1):
            warnings.append(f"{src}: contradicted frontmatter must be one exact `contradicted: <slug>` field")
        targets = set(target_values)
        if targets:
            marks[src] = targets

        prose = _visible_markdown_body(normalized)
        page_callouts = {target.strip().lower() for target, _why in CONTRADICTION_CALLOUT.findall(prose)}
        callouts[src] = page_callouts
        for dst in sorted(page_callouts - targets):
            warnings.append(f"{src}: CONTRADICTS callout targets {dst} without matching frontmatter")

        low_matches = LOW_CONF.findall(fm)
        low_keys = LOW_CONF_KEY.findall(fm)
        if low_keys and (len(low_keys) != 1 or len(low_matches) != 1):
            warnings.append(f"{src}: confidence frontmatter must be one exact `confidence: low` field")
        if low_matches:
            low.append(str(f.relative_to(wiki)))
            warning_bodies = [same_line or next_line
                              for same_line, next_line in WARNING_CALLOUT.findall(prose)]
            gap_callouts = [body for body in warning_bodies
                            if not body.lstrip().upper().startswith("CONTRADICTS")]
            if not gap_callouts:
                warnings.append(f"{src}: confidence: low lacks a separate warning callout naming the gap")

    pairs = set()
    for src, targets in marks.items():
        for dst in targets:
            if dst not in slug_map:
                warnings.append(f"{src}: contradicted: {dst} -> no such page")
                continue
            if src not in marks.get(dst, set()):
                warnings.append(f"{src}: contradicted: {dst} is one-sided — "
                                f"{dst} lacks the reciprocal marker")
            if dst not in callouts.get(src, set()):
                warnings.append(f"{src}: contradicted: {dst} lacks `CONTRADICTS [[{dst}]]: ...` callout")
            pairs.add(tuple(sorted((src, dst))))
    return sorted(pairs), low, warnings


def last_done(log: Path = LOG):
    """Reverse-scan log.md for the last `` task-done `` event line; None if none.

    Read-only. log.md is append-only (one line per event); a fresh project has 0 task-done
    lines, so the None sentinel is the real start state.
    """
    if not log.exists():
        return None
    for ln in reversed(read_markdown(log).splitlines()):
        if " task-done " in ln:
            return ln
    return None


GUARD_DERIVED = ("overview.md", "index.md")  # rewritten each checkpoint — link loss is advisory


def _norm_eol(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _link_targets(text: str) -> set[str]:
    """Normalized [[wikilink]] target set: code stripped, #anchor dropped, '?' prefix dropped
    (so a [[?x]] -> [[x]] resolution is the SAME target, never a 'lost link')."""
    out = set()
    for m in WIKILINK.findall(_strip_code(text)):
        t = m.split("#", 1)[0].strip().lower().lstrip("?").strip()
        if t and t not in PLACEHOLDER_SLUGS:
            out.add(t)
    return out


def _guard_compare(baseline: dict, current: dict):
    """Checkpoint integrity check (idea §2) — PURE compare, no IO. Both args map
    wiki-relative posix path -> content; ``baseline`` = last checkpoint, ``current`` = now.

    Returns (violations, warnings). Violations (must fix before commit): a filed decision
    modified or deleted (immutable — supersede forward); log.md rewritten/truncated/deleted
    (append-only); [[wikilinks]] lost from a knowledge page. Warnings (advisory): link loss
    on the derived rollups (rewritten each checkpoint by design) and whole-page deletions
    (git-recoverable — verify deliberate). EOL differences are normalized away; log.md's own
    branch subsumes its link check; decisions' byte-compare subsumes theirs.
    """
    violations, warnings = [], []
    for path in sorted(baseline):
        old = _norm_eol(baseline[path])
        new = _norm_eol(current[path]) if path in current else None
        if path.startswith("decisions/"):
            if new is None:
                violations.append(f"{path}: filed decision DELETED (immutable — restore it)")
            elif new != old:
                violations.append(f"{path}: filed decision MODIFIED (immutable — revert, "
                                  f"then supersede forward with a new decision)")
        elif path == "log.md":
            if new is None:
                violations.append("log.md: DELETED (append-only — restore it)")
            elif not new.startswith(old):
                violations.append("log.md: existing lines REWRITTEN (append-only — restore "
                                  "old lines, re-add yours at the end)")
        else:
            if new is None:
                warnings.append(f"{path}: page deleted this session (git-recoverable; "
                                f"verify deliberate)")
                continue
            lost = _link_targets(old) - _link_targets(new)
            if lost:
                msg = f"{path}: [[wikilinks]] LOST: {', '.join(sorted(lost))}"
                (warnings if path in GUARD_DERIVED else violations).append(msg)
    violations.extend(new_dangling_link_violations(baseline, current))
    return violations, warnings


def _newly_filed_decision_compare(
        head: dict, checkpoint: dict, current: dict, index: dict | None = None) -> list[str]:
    """Protect decisions filed in HEAD after the last checkpoint, in worktree *and* index."""
    violations = []
    for path in sorted(head):
        if not path.startswith("decisions/") or path in checkpoint:
            continue
        old = _norm_eol(head[path])
        new = _norm_eol(current[path]) if path in current else None
        if new is None:
            violations.append(f"{path}: filed decision DELETED after its mid-session commit (restore it)")
        elif new != old:
            violations.append(f"{path}: filed decision MODIFIED after its mid-session commit "
                              f"(revert, then supersede forward)")
        if index is not None:
            staged = _norm_eol(index[path]) if path in index else None
            if staged is None:
                violations.append(f"{path}: filed decision STAGED FOR DELETION after its mid-session commit")
            elif staged != old:
                violations.append(f"{path}: filed decision has a STAGED MODIFICATION after its mid-session commit")
    return violations


def _head_decision_snapshot(root: Path) -> dict[str, str]:
    """Read Markdown decisions from HEAD; an unborn repository has an empty snapshot."""
    probe = _run_git(root, "rev-parse", "--verify", "HEAD^{commit}",
                     allowed_rc=frozenset({0, 128}))
    if probe.returncode != 0:
        return {}
    out = {}
    raw_names = _git_bytes(
        root, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".memory/wiki/decisions")
    for raw_name in raw_names.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", "surrogateescape")
        if name.endswith(".md"):
            rel = name.split(".memory/wiki/", 1)[1]
            out[rel] = _git_text(root, "show", f"HEAD:{name}", strip=False)
    return out


def _index_decision_snapshot(root: Path) -> dict[str, str]:
    """Read the candidate Git-index decision snapshot (staged-only edits included)."""
    out = {}
    raw_names = _git_bytes(root, "ls-files", "-z", "--", ".memory/wiki/decisions")
    for raw_name in raw_names.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", "surrogateescape")
        if name.endswith(".md"):
            rel = name.split(".memory/wiki/", 1)[1]
            out[rel] = _git_text(root, "show", f":{name}", strip=False)
    return out


def _worktree_wiki_type_violations(wiki: Path = WIKI) -> list[str]:
    """Reject symlinks/special files anywhere in the live wiki path."""
    import os
    import stat

    probs = []
    root = wiki.resolve()
    for dirpath, dirnames, filenames in os.walk(wiki, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            p = base / name
            if p.is_symlink():
                probs.append(f"{p.relative_to(wiki).as_posix()}/: symlink directory inside wiki")
                dirnames.remove(name)
        for name in filenames:
            if not name.endswith(".md"):
                continue
            p = base / name
            try:
                mode = p.lstat().st_mode
            except OSError as e:
                probs.append(f"{p.relative_to(wiki).as_posix()}: cannot lstat: {e}")
                continue
            rel = p.relative_to(wiki).as_posix()
            if not stat.S_ISREG(mode):
                probs.append(f"{rel}: wiki Markdown must be a regular file (symlink/special found)")
                continue
            try:
                p.resolve().relative_to(root)
            except (OSError, ValueError):
                probs.append(f"{rel}: resolved path escapes .memory/wiki")
    return probs


def _history_integrity_violations(root: Path) -> list[str]:
    """Tracked full-history backstop shared with the Git pre-commit guard."""
    import os
    import sys
    tools = str(root / ".memory" / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import pre_commit_guard
        return pre_commit_guard.history_violations(cwd=root)
    except Exception as e:
        return [f"history guard unavailable: {type(e).__name__}: {e}"]


def _worktree_source_exists(root: Path, rel: str) -> bool:
    p = root / rel
    try:
        p.resolve(strict=True).relative_to(root.resolve())
        return p.is_file() and not p.is_symlink()
    except (OSError, ValueError):
        return False


def _commit_exists(root: Path, sha: str) -> bool:
    proc = _run_git(root, "cat-file", "-e", f"{sha}^{{commit}}",
                    allowed_rc=frozenset({0, 1, 128}))
    if proc.returncode != 0:
        return False
    ancestor = _run_git(root, "merge-base", "--is-ancestor", sha, "HEAD",
                        allowed_rc=frozenset({0, 1}))
    return ancestor.returncode == 0


def guard():
    """Collect baseline (last checkpoint commit) vs working tree and run _guard_compare.

    Baseline = the last commit touching .memory/STATE.md (the checkpoint definition in
    CLAUDE.md); no checkpoint yet -> empty baseline, everything passes trivially. Second
    line of defense behind the wiki_guard.py PreToolUse hook: catches edits the hook can't
    see (Bash/sed, MCP file tools). Returns (baseline_sha, violations, warnings).
    """
    root = WIKI.parents[1]
    _git_repo_preflight(root)

    sha = _git_text(root, "log", "-1", "--format=%H", "--", ".memory/STATE.md")
    baseline = {}
    if sha:
        raw_names = _git_bytes(
            root, "ls-tree", "-r", "--name-only", "-z", sha, "--", ".memory/wiki")
        for raw_name in raw_names.split(b"\0"):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8", "surrogateescape")
            if name.endswith(".md"):
                baseline[name.split(".memory/wiki/", 1)[1]] = _git_text(
                    root, "show", f"{sha}:{name}", strip=False)
    current = {f.relative_to(WIKI).as_posix(): read_markdown(f)
               for f in WIKI.rglob("*.md") if not f.is_symlink()}
    violations = (_history_integrity_violations(root)
                  + _worktree_wiki_type_violations(WIKI)
                  + _newly_filed_decision_compare(
                      _head_decision_snapshot(root), baseline, current,
                      _index_decision_snapshot(root)))
    health_fail, health_warn, _health_ok = enforcement_health(root, full=False)
    violations += [f"enforcement health: {x}" for x in health_fail]
    compare_violations, warnings = _guard_compare(baseline, current)
    warnings += [f"enforcement health: {x}" for x in health_warn]
    violations += compare_violations
    violations += provenance_violations(
        baseline, current,
        source_exists=lambda rel: _worktree_source_exists(root, rel),
        commit_exists=lambda sha: _commit_exists(root, sha))

    # checkpoint-shape checks (prompt-only rules kept drifting — enforce them here):
    # new log lines only (history never re-judged), STATE schema, index one-liner budget.
    log_old = _norm_eol(baseline.get("log.md", ""))
    log_new = _norm_eol(current.get("log.md", ""))
    if log_new.startswith(log_old):  # else append-only violation already fired above
        violations += _check_new_log_lines(log_old, log_new)
    state_path = root / ".memory" / "STATE.md"
    if state_path.exists():
        state_text = read_markdown(state_path)
        violations += _check_state(state_text)
        if sha:
            at_cp = _git_text(root, "show", f"{sha}:.memory/STATE.md", strip=False)
            if _norm_eol(state_text) == _norm_eol(at_cp):
                warnings.append("STATE.md identical to last checkpoint — a checkpoint "
                                "must rewrite it (volatile position moved on)")
    decision_pages = {f.name: read_markdown(f)
                      for f in sorted((WIKI / "decisions").glob("*.md"))
                      if not f.is_symlink()} if (WIKI / "decisions").is_dir() else {}
    violations += _doctor_decisions(decision_pages)
    _flag_pairs, _flag_low, flag_warnings = flags(WIKI)
    violations += [f"flags protocol: {problem}" for problem in flag_warnings]
    if (WIKI / "index.md").exists():
        _index_records, index_problems = decision_index_records(
            read_markdown(WIKI / "index.md"), _decision_inventory(WIKI))
        violations += [f"index.md: {problem}" for problem in index_problems]
    violations += [f"index.md: Decisions one-liner {plen} ch prose > 100 — trim: {ln[:70]}"
                   for plen, ln in lint_index()]
    if "overview.md" in current:
        violations += _check_overview(current["overview.md"])
    # freshness backstop (advisory): decay catches on durable pages — the write-side pair.
    miss, vol = freshness_scan(root)
    warnings += [f"{pg}: durable page cites MISSING path `{tok}` (moved/deleted? — fix or drop)"
                 for pg, tok in miss]
    warnings += [f"{pg}: volatile status \"{ph}\" in a durable concept — belongs in STATE "
                 f"(fence); reference code/STATE, don't copy the current value" for pg, ph in vol]
    # pack-budget ceiling (decision 0017): the wall. Guard only MEASURES tomorrow's resume
    # pack; the knife is the checkpoint ladder — (1) compress, (2) evict — run by the model.
    ptok, _ = pack_size()
    if ptok > PACK_BUDGET_TOK:
        violations.append(
            f"resume pack ≈ {ptok} tok > budget {PACK_BUDGET_TOK} — run "
            f"`python .memory/tools/structural.py compact` (deterministic: folds oldest "
            f"closed groups into the archive line, knowledge untouched), then rerun guard. "
            f"compact exit 1 -> manual COMPRESS: closed track -> one `- ids: ... :: ...` "
            f"group line, overview bullets sans numbers, STATE <= 40. "
            f"Split: `structural.py pack-budget`")
    return sha, violations, warnings


def freshness_scan(root: Path | None = None):
    """Deterministic decay catches on DURABLE pages (0-token, tail measure session 7).
    Returns two advisory lists:
      missing_path : (page, token) — a cited local file/dir absent under ALL of {repo root,
                     root/code, wiki dir}. A durable page pointing at a moved/deleted artifact
                     is a materialized stale claim. Checked on concepts + overview + index.
      volatile     : (page, phrase) — a CONCEPT carrying volatile CURRENT-STATE status
                     ("placeholder", "not yet provided", "заглушка", "ожидает фото") that
                     belongs in STATE by the fence rule; it rots the moment reality moves.
                     CONCEPTS only — overview/index are rewritten each checkpoint (self-heal).
    Both recall-first: the checkpoint model confirms. Decisions (immutable), log, STATE skipped.
    """
    root = root or WIKI.parents[1]
    wiki = root / ".memory" / "wiki"
    concepts = sorted((wiki / "concepts").glob("*.md")) if (wiki / "concepts").is_dir() else []
    derived = [wiki / n for n in ("overview.md", "index.md") if (wiki / n).is_file()]
    missing_path, volatile = [], []

    def _body(p):
        raw = read_markdown(p)
        m = FRONTMATTER.search(raw)
        return raw[m.end():] if m else raw

    for p in concepts + derived:
        rel = p.relative_to(root).as_posix()
        body = _body(p)
        seen = set()
        for line in body.splitlines():
            if FRESH_URL_LINE.search(line):
                continue
            for tok in FRESH_PATH.findall(line):
                tok = tok.rstrip("/.,);:")
                if tok in seen or FRESH_PATH_META.search(tok):
                    continue
                seen.add(tok)
                if not (tok.startswith((".memory/", "code/"))
                        or tok.endswith((".py", ".md", ".json", ".js", ".ts", ".txt"))):
                    continue
                if any((root / base / tok).exists() for base in ("", "code")) or (wiki / tok).exists():
                    continue
                missing_path.append((rel, tok))

    for p in concepts:  # volatile-status is a fence violation only on living durable pages
        rel = p.relative_to(root).as_posix()
        vm = FRESH_VOLATILE.search(_strip_code(_body(p)))
        if vm:
            volatile.append((rel, vm.group(0).strip()))
    return missing_path, volatile


LOG_CHECKPOINT_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} checkpoint (\S+) session (\d+) — .+$")
LOG_TASKDONE_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} task-done .+ — .+$")
STATE_CANON_HEADERS = ("Now", "Next", "Tried & rejected", "Open", "Pointers")


def _max_session(log_text: str) -> int | None:
    """Highest `session N` across log.md checkpoint lines; None if no numbered line yet."""
    ns = [int(m.group(2))
          for m in (LOG_CHECKPOINT_LINE.match(ln) for ln in log_text.splitlines()) if m]
    return max(ns) if ns else None


def _check_new_log_lines(old_log: str, new_log: str) -> list[str]:
    """PURE. Format/cap/session-number checks on lines APPENDED since the baseline —
    history is never re-judged (pre-guard lines stay as they are). Assumes append-only
    already holds (caller verifies new startswith old; otherwise pass tail='')."""
    probs = []
    prev_max = _max_session(old_log)
    tail = new_log[len(old_log):] if new_log.startswith(old_log) else ""
    for ln in tail.splitlines():
        if not ln.strip():
            continue
        cm = LOG_CHECKPOINT_LINE.match(ln)
        if cm:
            n = int(cm.group(2))
            if prev_max is not None and n != prev_max + 1:
                probs.append(f"log.md: new checkpoint line says session {n}, "
                             f"expected {prev_max + 1} (last logged + 1)")
            prev_max = n if prev_max is None else max(prev_max, n)
        elif not LOG_TASKDONE_LINE.match(ln):
            probs.append("log.md: new line matches neither `<date> checkpoint <block> "
                         f"session N — <summary>` nor `<date> task-done <task> — <summary>`: {ln[:80]}")
        if len(ln) > 200:
            probs.append(f"log.md: new line is {len(ln)} chars, cap is 200 — cut the summary")
    return probs


# routine facts live in tracker.md ONLY — never retold in STATE prose (session 42). The resume
# card reads STATE aloud, so a hand-written retelling ships as a LIE the moment it drifts from
# the log: session 41 wrote "рутина закрыта Ромой без меня" into the same commit that carried
# the machine's own run file. One home, nothing to diverge. The tell is a SCORE next to a
# routine word on the same line; inline code is stripped first, so `tracker-log comments=3/3`
# is the fix being quoted, not the violation.
# ponytail: score-only. A count-free attribution ("рутину закрыл Рома сам") still slips —
# tried matching Рома/без меня and it fired on legitimate lines ("Рома сказал не надо" about
# a comments feature). Add a verb-scoped second arm only if a count-free lie actually ships.
STATE_ROUTINE = re.compile(r"connect\w*|коннект\w*|comment\w*|коммент\w*|\bpost\w*|пост\w*", re.I)
STATE_ROUTINE_TELL = re.compile(r"\d+\s*/\s*\d+")


def _check_state(state_text: str) -> list[str]:
    """PURE. STATE.md schema: only canonical `## ` sections (resume parses them), <=40 lines,
    and no retelling of the daily routine (that is tracker.md's single home). A parenthetical
    annotation after the canon name is fine — `## Tried & rejected (не повторять)` parses
    identically (section() matches by prefix); an alien header does not."""
    probs = []
    for h in toc(state_text):
        if not any(h == c or (h.startswith(c + " (") and h.endswith(")"))
                   for c in STATE_CANON_HEADERS):
            probs.append(f'STATE.md: non-canonical section "## {h}" — '
                         f"fold into {'/'.join(STATE_CANON_HEADERS)}")
    n = len(state_text.splitlines())
    if n > 40:
        probs.append(f"STATE.md: {n} lines > 40 — graduate permanent facts to a wiki page")
    for line in _strip_code(state_text).splitlines():
        if STATE_ROUTINE.search(line) and (m := STATE_ROUTINE_TELL.search(line)):
            probs.append(f'STATE.md: routine retold in prose ("{m.group(0).strip()}" in '
                         f'"{line.strip()[:60]}") — drop it; tracker.md is the only home '
                         f"(resume reads it from there, so a retelling can only drift)")
    return probs


STATE_PATH = Path(__file__).resolve().parents[2] / ".memory" / "STATE.md"
STATE_IDLE_BODY = """
> Volatile session memory. Sourceless OK. Rewritten on checkpoint.
> NOT bound by wiki provenance rules.

## Now
- No active task. Start work + /wiki-checkpoint to begin a new task,
  or tell me what we're starting.
"""


def _state_idle(old_text: str) -> str:
    """PURE. The idle STATE, keeping the project's own H1 (`# STATE — <project>`)."""
    head = old_text.splitlines()[0] if old_text.strip() else ""
    return (head if head.startswith("# ") else "# STATE") + "\n" + STATE_IDLE_BODY.lstrip("\n")


class StateResetError(RuntimeError):
    """Safe abort: STATE was not wiped."""


def state_reset(root: Path | None = None) -> list[str]:
    """Wipe STATE only after a path-limited, verified Git snapshot exists.

    Every Git return code is checked. A failed/ambiguous snapshot aborts before the first
    write, leaves STATE byte-for-byte intact, and never prints a success/restore lie.
    ``git commit --only`` keeps unrelated staged files out of the snapshot commit.
    """
    import os
    import subprocess
    import tempfile

    root = root or Path(__file__).resolve().parents[2]
    path = root / ".memory" / "STATE.md"
    if not path.exists():
        return [f"state-reset: {path} not found — nothing to wipe"]

    def run(*args, text=True):
        return subprocess.run(("git",) + args, cwd=root, capture_output=True,
                              text=text, encoding="utf-8" if text else None,
                              errors="replace" if text else None)

    lock = path.with_name(".STATE.reset.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise StateResetError(f"state-reset ABORTED: lock exists ({lock}); STATE untouched")

    out = []
    try:
        old_bytes = path.read_bytes()
        old_text = old_bytes.decode("utf-8")
        status = run("status", "--porcelain", "--untracked-files=all", "--",
                     ".memory/STATE.md")
        if status.returncode != 0:
            raise StateResetError("state-reset ABORTED: git status failed; STATE untouched: "
                                  + (status.stderr.strip() or "unknown git error"))

        if status.stdout.strip():
            commit = run("commit", "--only", "-m", "chore: snapshot STATE before wipe",
                         "--", ".memory/STATE.md")
            if commit.returncode != 0:
                detail = commit.stderr.strip() or commit.stdout.strip() or "git commit failed"
                raise StateResetError("state-reset ABORTED: snapshot commit failed; "
                                      f"STATE untouched: {detail}")
            out.append("dirty STATE committed first — snapshot verified before wipe")

        head = run("rev-parse", "--verify", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            raise StateResetError("state-reset ABORTED: no snapshot commit/HEAD; STATE untouched")
        sha = head.stdout.strip()
        snap = run("show", f"{sha}:.memory/STATE.md", text=False)
        if snap.returncode != 0 or _norm_eol(snap.stdout.decode("utf-8", "replace")) != \
                _norm_eol(old_text):
            raise StateResetError("state-reset ABORTED: snapshot verification mismatch; "
                                  "STATE untouched")

        idle = _state_idle(old_text)
        fd, tmp_name = tempfile.mkstemp(prefix=".STATE.reset-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(idle)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, path.stat().st_mode & 0o777)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

        dropped = sum(1 for ln in old_text.splitlines() if ln.lstrip().startswith("- "))
        out.append(f"state-reset: STATE wiped to idle — {dropped} bullet(s) dropped")
        out.append(f"restore: git show {sha[:8]}:.memory/STATE.md")
        return out
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


OVERVIEW_LINE_CAP = 400
OPEN_THREADS_LINE_CAP = 200
OPEN_THREADS_LINE = re.compile(r"^- \[\[[^\]]+\]\] — .+$")


def _check_overview(overview_text: str) -> list[str]:
    """PURE. overview.md "State of the project" = micro-schema bullets (O5): every line
    <= 400 chars. A mega-paragraph hides a session diary and breaks old_string Edits;
    one bullet = one line keeps checkpoint edits surgical.
    "Open threads" = index lines (O2): `- [[owner]] — hook`, <= 200 chars per line —
    the fact itself lives on the owner page (or in STATE if volatile); a thread line
    only points. One fact — one home."""
    probs = []
    sec = section(overview_text, "State of the project")
    if sec is not None:
        probs += [f"overview.md: State-of-the-project line is {len(ln)} chars, cap is "
                  f"{OVERVIEW_LINE_CAP} — one bullet = one line, split it: {ln[:60]}"
                  for ln in sec.splitlines() if len(ln) > OVERVIEW_LINE_CAP]
    ot = section(overview_text, "Open threads")
    if ot is not None:
        for ln in ot.splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            if not OPEN_THREADS_LINE.match(ln):
                probs.append("overview.md: Open-threads entry must be an index line "
                             f"`- [[owner]] — hook` (fact lives on the page): {ln[:60]}")
            elif len(ln) > OPEN_THREADS_LINE_CAP:
                probs.append(f"overview.md: Open-threads line is {len(ln)} chars, cap is "
                             f"{OPEN_THREADS_LINE_CAP} — hook only, move detail to the "
                             f"owner page: {ln[:60]}")
    return probs


DECISION_FILE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
FM_ID = re.compile(r"^id:\s*(\S+)", re.M)
FM_DATE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)
PROJECT_DIR_REF = re.compile(r"\$CLAUDE_PROJECT_DIR/([^\"'\s]+)")


def _doctor_decisions(pages: dict) -> list[str]:
    """Strict decision frontmatter + supersession graph validation — PURE."""
    probs, seen, graph = [], {}, {}
    for name, text in sorted(pages.items()):
        m = DECISION_FILE.match(name)
        if not m:
            probs.append(f"decisions/{name}: filename not NNNN-slug.md")
            continue
        fid = m.group(1)
        fm = FRONTMATTER.match(_norm_eol(text))
        if not fm:
            probs.append(f"decisions/{name}: no frontmatter")
            continue
        body = fm.group(1)
        got = FM_ID.search(body)
        if not got or got.group(1) != fid:
            probs.append(f"decisions/{name}: id {got.group(1) if got else '(none)'} "
                         f"!= filename prefix {fid}")
        if fid in seen:
            probs.append(f"decisions/{name}: duplicate id {fid} (also {seen[fid]})")
        seen.setdefault(fid, name)
        date_field = FM_DATE.search(body)
        if not date_field:
            probs.append(f"decisions/{name}: date missing or not YYYY-MM-DD")
        else:
            try:
                date.fromisoformat(date_field.group(1))
            except ValueError:
                probs.append(f"decisions/{name}: impossible calendar date {date_field.group(1)}")
        keys = SUPERSEDES_KEY.findall(body)
        vals = SUPERSEDES.findall(body)
        if keys:
            if len(keys) != 1 or len(vals) != 1:
                probs.append(f"decisions/{name}: supersedes must be one exact `supersedes: NNNN` field")
            else:
                graph[fid] = vals[0]

    for new, old in graph.items():
        if old == new:
            probs.append(f"decisions/{seen.get(new, new)}: supersedes itself ({new})")
        elif old not in seen:
            probs.append(f"decisions/{seen.get(new, new)}: supersedes {old} -> no decision file")
    cycles = _supersession_cycle_nodes({n: o for n, o in graph.items()
                                        if n in seen and o in seen and n != o})
    if cycles:
        probs.append("decisions: supersedes cycle: " + " -> ".join(sorted(cycles)))
    return probs


def _probe_payload(event: str, matcher: str, root: Path, tool_name: str = "Bash") -> str:
    """Benign no-op stdin JSON for a hook probe, shaped like the harness payload.

    Edit-matcher hooks get a file_path OUTSIDE the wiki fence (wiki_guard must allow);
    Bash-matcher hooks get command "true"; PreCompact gets a nonexistent transcript_path
    (precompact_archive must no-op silently). Nothing a probe runs may leave junk behind.
    """
    import json

    d = {"session_id": "doctor-probe", "hook_event_name": event, "cwd": str(root),
         "transcript_path": str(root / ".doctor-probe-nonexistent.jsonl")}
    if event == "PreToolUse":
        if "Edit" in (matcher or "") or "Write" in (matcher or ""):
            d["tool_name"] = "Edit"
            d["tool_input"] = {"file_path": str(root / "README.md"),
                               "old_string": "doctor", "new_string": "doctor"}
        else:
            d["tool_name"] = tool_name
            d["tool_input"] = {"command": "true", "description": "doctor probe"}
    elif event == "UserPromptSubmit":
        d["prompt"] = "doctor probe"
    return json.dumps(d)


def _probe_hook(command: str, payload: str, root: Path, timeout: int = 10,
                windows: bool = False):
    """Run a hook with its runtime shell and a benign JSON payload on stdin.

    Claude hooks run through ``sh -c``; Codex ``commandWindows`` hooks run through PowerShell.
    Returns (exit_code, note); None means the required shell is missing, -1 = timeout.
    """
    import os
    import shutil
    import subprocess

    if windows:
        shell = "pwsh"
        runner = [shell, "-NoProfile", "-Command"]
    else:
        shell = shutil.which("sh")
        if not shell and os.name == "nt":
            git_sh = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/sh.exe"
            shell = str(git_sh) if git_sh.is_file() else None
        if not shell:
            return None, "sh not found — cannot probe hooks the way the harness runs them"
        runner = [shell, "-c"]
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root))
    try:
        p = subprocess.run([*runner, command], input=payload.encode("utf-8"),
                           capture_output=True, timeout=timeout, env=env, cwd=root)
        return p.returncode, ""
    except FileNotFoundError:
        return None, f"{shell} not found — cannot probe hooks the way the harness runs them"
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s"


def _probe_hook_output(command: str, payload: str, root: Path, timeout: int = 10,
                       windows: bool = False):
    """Probe a configured hook and preserve stdout so deny semantics can be verified."""
    import os
    import shutil
    import subprocess

    if windows:
        shell = "pwsh"
        runner = [shell, "-NoProfile", "-Command"]
    else:
        shell = shutil.which("sh")
        if not shell:
            return None, "", "", "sh not found"
        runner = [shell, "-c"]
    try:
        p = subprocess.run([*runner, command], input=payload.encode("utf-8"),
                           capture_output=True, timeout=timeout, cwd=root,
                           env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)})
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"), "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, "", "", str(e)


def _deny_probe_payload(root: Path, *, codex: bool) -> str:
    import json

    if codex:
        patch = ("*** Begin Patch\n*** Update File: .memory/wiki/log.md\n@@\n"
                 "-protected\n+rewritten\n*** End Patch")
        tool_name, tool_input = "apply_patch", {"command": patch}
    else:
        tool_name = "Write"
        tool_input = {"file_path": str(root / ".memory/wiki/log.md"),
                      "content": "doctor destructive rewrite\n"}
    return json.dumps({"session_id": "doctor-deny-probe", "hook_event_name": "PreToolUse",
                       "cwd": str(root), "tool_name": tool_name, "tool_input": tool_input})


def _stdout_denies(stdout: str) -> bool:
    import json

    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("decision") == "block":
            return True
        if (data.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return True
    return False


def _codex_hook_key(windows: bool | None = None) -> str:
    """Which field Codex actually runs on this platform: ``commandWindows`` on Windows,
    plain ``command`` everywhere else. doctor must probe the branch the runtime takes."""
    import os

    if windows is None:
        windows = os.name == "nt"
    return "commandWindows" if windows else "command"


def _codex_hook_command(entry: dict, windows: bool | None = None):
    """Returns (command, is_windows_shell) for one ``.codex/hooks.json`` entry."""
    import os

    if windows is None:
        windows = os.name == "nt"
    return entry.get(_codex_hook_key(windows), ""), windows


def _runtime_enabled(root, name, legacy_default=True):
    import json
    path = root / ".memory/runtimes.json"
    if not path.is_file():
        return legacy_default
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(name) is True
    except (ValueError, OSError, AttributeError):
        return False  # doctor separately reports the invalid manifest


def enforcement_health(root: Path, *, full: bool = False):
    """Fast deployment health used at resume/write boundaries; full adds executable selfchecks."""
    import os
    import subprocess

    fails, warns, oks = [], [], []
    try:
        import install_hooks
        hook_failures = install_hooks.health(root)
        if hook_failures:
            fails.extend(f"isolated Git pre-commit: {problem}" for problem in hook_failures)
        else:
            oks.append("isolated HEAD-pinned Git pre-commit bundle is healthy")
    except (ImportError, OSError, ValueError) as e:
        fails.append(f"cannot verify isolated Git pre-commit: {e}")

    for rel in (".memory/tools/git_checked.py", ".memory/tools/wiki_validation.py",
                ".memory/tools/markdown_io.py", ".memory/tools/pre_commit_guard.py",
                ".memory/tools/install_hooks.py", ".memory/tools/wiki_guard.py",
                ".memory/tools/checkpoint_txn.py"):
        if not (root / rel).is_file():
            fails.append(f"required enforcement script missing: {rel}")

    # Read/write-boundary behavior proof: configured soft guards must actually emit deny JSON.
    import json
    configured = []
    for rel, codex in ((".claude/settings.json", False), (".codex/hooks.json", True)):
        if not _runtime_enabled(root, "codex" if codex else "claude"):
            continue
        path = root / rel
        if not path.exists() and (codex or (root / ".memory/runtimes.json").is_file()):
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            fails.append(f"cannot read configured hook registry {rel}: {e}")
            continue
        for event, groups in (cfg.get("hooks") or {}).items():
            if event != "PreToolUse":
                continue
            for group in groups:
                matcher = group.get("matcher", "")
                if not ((not codex and ("Edit" in matcher or "Write" in matcher))
                        or (codex and "apply_patch" in matcher)):
                    continue
                for hook in group.get("hooks") or []:
                    cmd, windows = (_codex_hook_command(hook) if codex
                                    else (hook.get("command", ""), False))
                    if cmd:
                        configured.append((rel, cmd, hook.get("timeout", 10), codex, windows))
    kinds = {codex for _rel, _cmd, _timeout, codex, _windows in configured}
    if False not in kinds and _runtime_enabled(root, "claude"):
        fails.append("Claude Edit/Write wiki guard is not configured")
    if _runtime_enabled(root, "codex") and (root / ".codex/hooks.json").exists() and True not in kinds:
        fails.append("Codex apply_patch wiki guard is not configured")
    for rel, cmd, timeout, codex, windows in configured:
        code, stdout, stderr, note = _probe_hook_output(
            cmd, _deny_probe_payload(root, codex=codex), root,
            timeout=timeout, windows=windows)
        if code != 0 or not _stdout_denies(stdout):
            detail = note or stderr.strip() or stdout.strip() or "no deny JSON emitted"
            fails.append(f"{rel}: configured wiki guard did not deny a harmful rewrite: {detail}")
        else:
            oks.append(f"{rel}: configured wiki guard behaviorally denies rewrites")

    if full:
        if sys.flags.optimize:
            fails.append("Python -O disables assert-based selfchecks; rerun doctor without -O")
        checks = ((".memory/tools/wiki_guard.py", "--selfcheck"),
                  (".memory/tools/pre_commit_guard.py", "--selfcheck"),
                  (".memory/tools/git_checked.py", "--selfcheck"),
                  (".memory/tools/install_hooks.py", "--selfcheck"),
                  (".memory/tools/wiki_validation.py", "--selfcheck"),
                  (".memory/tools/checkpoint_txn.py", "selfcheck"))
        for rel, arg in checks:
            p = root / rel
            if not p.is_file():
                continue
            try:
                run = subprocess.run(
                    (sys.executable, str(p), arg), cwd=root,
                    capture_output=True, text=True, timeout=45,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            except subprocess.TimeoutExpired:
                fails.append(f"{rel} selfcheck timed out")
                continue
            if run.returncode != 0 or run.stdout.strip().splitlines()[-1:] != ["selfcheck OK"]:
                detail = (run.stderr or run.stdout).strip()
                fails.append(f"{rel} selfcheck failed or gave no exact success marker: {detail}")
            else:
                oks.append(f"{rel} selfcheck passes")
    return fails, warns, oks


def doctor(root: Path | None = None, *, portable: bool = False):
    """Contour self-check (idea §8) — is the insurance actually alive? Returns
    (fails, warns, oks). Config health only; content integrity stays guard()'s job.

    Hook checks derive from settings.json dynamically (a new hook is covered with no doctor
    change). Referenced $CLAUDE_PROJECT_DIR scripts get an existence check on top of the
    probe because the ``|| true`` wrappers mask a missing script from exit codes. The
    git hard floor and an installed Prime Agent adapter are checked explicitly."""
    import json
    import os
    import shutil
    import subprocess

    root = root or WIKI.parents[1]
    fails, warns, oks = [], [], []

    settings = root / ".claude" / "settings.json"
    commands = []
    deny_hooks = []
    runtime_file = root / ".memory/runtimes.json"
    if runtime_file.is_file():
        try:
            runtimes = json.loads(runtime_file.read_text(encoding="utf-8"))
            if not isinstance(runtimes, dict) or set(runtimes) != {"claude", "codex", "prime"} or any(type(v) is not bool for v in runtimes.values()):
                raise ValueError("expected claude/codex/prime booleans")
            for name, rel in (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json"), ("prime", ".prime/agent/extensions/wiki-memory.js")):
                if runtimes[name] and not (root / rel).is_file():
                    fails.append(f"enabled {name} runtime missing: {rel}")
        except (ValueError, OSError) as exc:
            fails.append(f"invalid runtime selection: {exc}")
    if not settings.exists() and not runtime_file.is_file():
        fails.append(".claude/settings.json missing — no hooks registered, contour dead")
    elif settings.exists() and _runtime_enabled(root, "claude"):
        try:
            cfg = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            fails.append(f".claude/settings.json unparseable: {e}")
            cfg = {}
        for event, groups in (cfg.get("hooks") or {}).items():
            for g in groups:
                for h in g.get("hooks") or []:
                    cmd = h.get("command", "")
                    matcher = g.get("matcher", "*")
                    commands.append(cmd)
                    code, note = _probe_hook(cmd, _probe_payload(event, matcher, root), root)
                    label = f"hook {event}[{matcher}]"
                    if event == "PreToolUse" and ("Edit" in matcher or "Write" in matcher):
                        deny_hooks.append((label, cmd, h.get("timeout", 10), False))
                    if code is None:
                        warns.append(f"{label}: {note}")
                    elif code in (126, 127):
                        hint = (" — backslash path? the POSIX shell eats them"
                                if "\\" in cmd else "")
                        fails.append(f"{label}: exit {code}, command not runnable{hint}: {cmd}")
                    elif code == -1:
                        fails.append(f"{label}: {note}: {cmd}")
                    elif code != 0:
                        warns.append(f"{label}: exit {code} on a benign probe: {cmd}")
                    else:
                        oks.append(f"{label}: runs (exit 0)")

    codex_hooks = root / ".codex" / "hooks.json"
    if codex_hooks.exists() and _runtime_enabled(root, "codex"):
        try:
            cfg = json.loads(codex_hooks.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            fails.append(f".codex/hooks.json unparseable: {e}")
            cfg = {}
        for event, groups in (cfg.get("hooks") or {}).items():
            for g in groups:
                for h in g.get("hooks") or []:
                    cmd, on_windows = _codex_hook_command(h)
                    matcher = g.get("matcher", "*")
                    label = f"hook Codex {event}[{matcher}]"
                    if event == "PreToolUse" and "apply_patch" in matcher and cmd:
                        deny_hooks.append((label, cmd, h.get("timeout", 10), True))
                    if not cmd:
                        fails.append(f"{label}: {_codex_hook_key(on_windows)} missing")
                        continue
                    code, note = _probe_hook(
                        cmd, _probe_payload(event, matcher, root, tool_name="exec"), root,
                        timeout=h.get("timeout", 10), windows=on_windows)
                    if code is None:
                        warns.append(f"{label}: {note}")
                    elif code == -1:
                        fails.append(f"{label}: {note}: {cmd}")
                    elif code != 0:
                        warns.append(f"{label}: exit {code} on a benign probe: {cmd}")
                    else:
                        oks.append(f"{label}: runs (exit 0)")

    for label, cmd, timeout, codex in deny_hooks:
        code, stdout, stderr, note = _probe_hook_output(
            cmd, _deny_probe_payload(root, codex=codex), root,
            timeout=timeout, windows=(os.name == "nt" and codex))
        if code != 0 or not _stdout_denies(stdout):
            detail = note or stderr.strip() or stdout.strip() or "no deny JSON emitted"
            fails.append(f"{label}: harmful rewrite was NOT behaviorally denied: {detail}")
        else:
            oks.append(f"{label}: harmful rewrite behaviorally denied")

    for cmd in commands:
        for rel in PROJECT_DIR_REF.findall(cmd):
            if not (root / rel).exists():
                fails.append(f"referenced script missing: {rel} (|| true masks this from probes)")

    efail, ewarn, eok = enforcement_health(root, full=True)
    fails.extend(efail)
    warns.extend(ewarn)
    oks.extend(eok)

    prime_adapter = root / ".prime" / "agent" / "extensions" / "wiki-memory.js"
    prime_selfcheck = root / ".memory" / "tools" / "prime_adapter_selfcheck.mjs"
    if _runtime_enabled(root, "prime") and prime_selfcheck.exists() and not prime_adapter.exists():
        fails.append("Prime adapter missing while its WikiInit selfcheck is installed")
    if prime_adapter.exists() and _runtime_enabled(root, "prime"):
        if not prime_adapter.is_file():
            fails.append("Prime adapter path is not a file: .prime/agent/extensions/wiki-memory.js")
        else:
            try:
                prime_text = prime_adapter.read_text(encoding="utf-8")
            except OSError as e:
                fails.append(f"Prime adapter unreadable: {e}")
                prime_text = ""
            if "WIKIINIT_PRIME_ADAPTER" not in prime_text:
                fails.append("Prime adapter marker missing — refusing a foreign/corrupt owned file")
            for rel in (".claude/commands/wiki-resume.md",
                        ".claude/commands/wiki-checkpoint.md",
                        ".claude/commands/wiki-clear.md"):
                if runtime_file.is_file():
                    rel = rel.replace(".claude/commands/", ".memory/commands/")
                if not (root / rel).is_file():
                    fails.append(f"Prime command source missing: {rel}")
            node = shutil.which("node")
            prime_cli = shutil.which("prime-agent")
            if not node:
                fails.append("Prime adapter installed but node is not on PATH")
            else:
                probe = subprocess.run([node, "--input-type=module", "--check"],
                                       input=prime_text, capture_output=True, text=True)
                if probe.returncode != 0:
                    fails.append(f"Prime adapter syntax invalid: {probe.stderr.strip()}")
                elif "WIKIINIT_PRIME_ADAPTER" in prime_text:
                    oks.append("Prime Agent adapter syntax + marker valid")
                if not prime_selfcheck.is_file():
                    fails.append("Prime adapter selfcheck missing: "
                                 ".memory/tools/prime_adapter_selfcheck.mjs")
                else:
                    try:
                        selfcheck_text = prime_selfcheck.read_text(encoding="utf-8")
                    except OSError as e:
                        fails.append(f"Prime adapter selfcheck unreadable: {e}")
                        selfcheck_text = ""
                    if "WIKIINIT_PRIME_SELFCHECK" not in selfcheck_text:
                        fails.append("Prime adapter selfcheck marker missing — foreign/corrupt file")
                    elif probe.returncode == 0:
                        check = subprocess.run([node, str(prime_selfcheck)], cwd=root,
                                               capture_output=True, text=True, timeout=30)
                        if check.returncode != 0:
                            detail = (check.stderr or check.stdout).strip()
                            fails.append(f"Prime adapter selfcheck failed: {detail}")
                        else:
                            oks.append("Prime Agent lifecycle adapter selfcheck passes")
            if not prime_cli:
                (warns if portable else fails).append(
                    "Prime adapter installed but prime-agent is not on PATH"
                    + (" (portable CI check)" if portable else ""))
            else:
                oks.append(f"Prime Agent runtime available: {prime_cli}")

    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        oks.append("FTS5 available — search runs full-text")
    except sqlite3.OperationalError:
        warns.append("FTS5 unavailable in this sqlite3 build — search degrades to grep")

    wiki = root / ".memory" / "wiki"
    fence_ok = True
    for rel in ("wiki/concepts", "wiki/decisions"):
        if not (root / ".memory" / rel).is_dir():
            fails.append(f".memory/{rel}/ missing")
            fence_ok = False
    for rel in ("wiki/overview.md", "wiki/index.md", "wiki/log.md", "STATE.md"):
        if not (root / ".memory" / rel).is_file():
            fails.append(f".memory/{rel} missing")
            fence_ok = False
    if fence_ok:
        oks.append("fence structure present")

    if wiki.is_dir():
        pages = {f.name: read_markdown(f)
                 for f in sorted((wiki / "decisions").glob("*.md"))} if (wiki / "decisions").is_dir() else {}
        probs = _doctor_decisions(pages)
        fails.extend(probs)
        if not probs and pages:
            oks.append(f"decisions frontmatter valid ({len(pages)} files)")
        _, ewarn, *_ = extract(wiki=wiki)
        warns.extend(f"wiki: {w}" for w in ewarn)

    return fails, warns, oks


def _decision_search_statuses(wiki: Path = WIKI) -> dict[str, tuple[str, str]]:
    """wiki-relative decision path -> (active|superseded, replacement slugs)."""
    edges, _warnings, superseded, _active = extract(wiki=wiki)
    replacements: dict[str, set[str]] = {}
    for src, relation, dst in edges:
        if relation == "supersedes":
            replacements.setdefault(dst, set()).add(src)
    out = {}
    for f in sorted((wiki / "decisions").glob("*.md")) if (wiki / "decisions").is_dir() else []:
        m = ID_PREFIX.match(f.stem)
        is_old = bool(m and m.group(1) in superseded)
        repl = ", ".join(sorted(replacements.get(f.stem.lower(), set())))
        out[str(f.relative_to(wiki))] = ("superseded" if is_old else "active", repl)
    return out


def _page_rows(wiki: Path = WIKI):
    """(path, kind, title, body, status, replacement) for every wiki Markdown page."""
    rows = []
    statuses = _decision_search_statuses(wiki)
    for f in sorted(wiki.rglob("*.md")):
        raw = read_markdown(f)
        m = H1.search(raw)
        title = m.group(1).strip() if m else f.stem
        kind = f.parent.name if f.parent != wiki else "page"
        body = re.sub(r"`([^`\n]+)`", r" \1 ", FENCED.sub("", raw))
        path = str(f.relative_to(wiki))
        status, replacement = statuses.get(path, ("current", ""))
        rows.append((path, kind, f"{title} · {f.stem}", body, status, replacement))
    return rows


# RU query-side light stemming: porter is EN-only, so Russian word forms never met across
# query/page. Fix on the QUERY side only: strip one common ending, prefix-match the root
# («промахивается» -> "промах"* -> finds both «промах» and «промахивается»). Index untouched.
# ponytail: not a real stemmer — misses stay possible; the map dump is the guarantee floor.
_RU_END = sorted(
    ("иваться", "ываться", "ивается", "ывается", "ации", "ацию", "ация",
     "ется", "ится", "ать", "ять", "еть", "ить", "ыми", "ими", "ами", "ями",
     "иях", "ях", "ах", "ов", "ев", "ей", "ом", "ем", "ой", "ый", "ий",
     "ая", "яя", "ое", "ее", "ые", "ие", "ья", "ье",
     "а", "я", "ы", "и", "е", "о", "у", "ю", "ь"),
    key=len, reverse=True)
_CYR = re.compile(r"^[а-яё]+$")


def _term_expr(t: str) -> str:
    """One query term -> FTS5 expression: RU-rooted + prefix-starred where safe."""
    cyr = bool(_CYR.match(t))
    if cyr and len(t) >= 4:
        for end in _RU_END:
            if t.endswith(end) and len(t) - len(end) >= 3:
                t = t[: -len(end)]
                break
    return f'"{t}"*' if len(t) >= (3 if cyr else 4) else f'"{t}"'


def _search_results(query: str, wiki: Path = WIKI, limit: int = 5):
    """Structured FTS rows: (path, snippet, status, replacement), current before history."""
    terms = TERMS.findall(query.lower())
    if not terms:
        return []
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE pages USING fts5("
               "path UNINDEXED, kind UNINDEXED, title, body, status UNINDEXED, "
               "replacement UNINDEXED, tokenize='porter unicode61')")
    db.executemany(
        "INSERT INTO pages(path, kind, title, body, status, replacement) VALUES (?,?,?,?,?,?)",
        _page_rows(wiki))
    return db.execute(
        "SELECT path, snippet(pages, 3, '>>>', '<<<', '…', 12), status, replacement "
        "FROM pages WHERE pages MATCH ? AND kind != 'page' "
        "ORDER BY CASE status WHEN 'superseded' THEN 1 ELSE 0 END, "
        "bm25(pages, 0.0, 0.0, 5.0, 1.0, 0.0, 0.0) LIMIT ?",
        (" OR ".join(_term_expr(t) for t in terms), limit),
    ).fetchall()


def _render_search_snippet(snippet: str, status: str, replacement: str) -> str:
    label = f"[SUPERSEDED by {replacement or 'newer decision'}] " if status == "superseded" else ""
    return label + snippet


def search(query: str, wiki: Path = WIKI, limit: int = 5):
    """Compatibility API -> [(path, rendered snippet)]; dispatch keeps structured status."""
    return [(path, _render_search_snippet(snip, status, replacement))
            for path, snip, status, replacement in _search_results(query, wiki, limit)]


# header parsed by consult_gate.py (map evidence) — keep the string in sync
_MAP_HEADER = "MAP (вся память — выбери страницу сам; дальше section/toc):"


def wiki_map(wiki: Path = WIKI):
    """One line per knowledge page, with superseded decisions visibly labelled."""
    lines = []
    statuses = _decision_search_statuses(wiki)
    for f in sorted(wiki.rglob("*.md")):
        if f.parent == wiki:
            continue  # derived rollups (overview/index/log) — pointers, not knowledge
        m = H1.search(read_markdown(f))
        path = str(f.relative_to(wiki))
        status, replacement = statuses.get(path, ("current", ""))
        suffix = f" [SUPERSEDED by {replacement or 'newer decision'}]" if status == "superseded" else ""
        lines.append(f"{f.relative_to(wiki)} — {m.group(1).strip() if m else f.stem}{suffix}")
    return lines


def _grep_fallback_results(query: str, wiki: Path = WIKI, limit: int = 5):
    """Structured literal fallback rows, current knowledge before superseded history."""
    terms = TERMS.findall(query.lower())
    hits = []
    statuses = _decision_search_statuses(wiki)
    for f in sorted(wiki.rglob("*.md")):
        if f.parent == wiki:
            continue
        path = str(f.relative_to(wiki))
        status, replacement = statuses.get(path, ("current", ""))
        for i, ln in enumerate(read_markdown(f).splitlines(), 1):
            if terms and any(t in ln.lower() for t in terms):
                hits.append((1 if status == "superseded" else 0,
                             path, i, ln.strip(), status, replacement))
    hits.sort(key=lambda row: (row[0], row[1], row[2]))
    return [(path, i, line, status, replacement)
            for _rank, path, i, line, status, replacement in hits[: limit * 4]]


def _grep_fallback(query: str, wiki: Path = WIKI, limit: int = 5):
    return [(path, i, _render_search_snippet(line, status, replacement))
            for path, i, line, status, replacement in _grep_fallback_results(query, wiki, limit)]


def _demo():
    # project-agnostic invariants (a fresh wiki has no decisions -> empty, still passes)
    edges, warnings, superseded, active = extract()
    assert set(active).isdisjoint(superseded), "active/superseded overlap"
    assert all(t for _, _, t in edges), "edge with empty target"

    # section(): whole-section slice + GENERIC EOF/last-section handling. A synthetic
    # multi-section string so the test never assumes which header is last. Middle section
    # stops at the next "## "; the LAST section runs to EOF.
    doc = "intro\n## Alpha\na1\na2\n## Beta\nb1\n## Gamma\ng1\ng2\n"
    assert section(doc, "Alpha") == "## Alpha\na1\na2", section(doc, "Alpha")
    assert section(doc, "## Beta") == "## Beta\nb1", section(doc, "## Beta")  # prefix w/ "## "
    assert section(doc, "Gamma") == "## Gamma\ng1\ng2", section(doc, "Gamma")  # EOF case
    assert section(doc, "Nope") is None, "missing header must be None"
    doc2 = "## Only\nx\ny"
    assert section(doc2, "Only") == "## Only\nx\ny", section(doc2, "Only")  # single section -> EOF

    # toc(): the page's "## " header list in order; SAME criterion as section() so they agree
    # (every title toc returns is sliceable by section). No "## " -> empty list, never None.
    assert toc(doc) == ["Alpha", "Beta", "Gamma"], toc(doc)
    assert toc(doc2) == ["Only"], toc(doc2)
    assert toc("no headers\njust prose\n### deeper") == [], toc("no headers\njust prose\n### deeper")

    # _resolve_page(): every spelling a caller actually types lands on the same file —
    # wiki-relative as search/map print it (backslash ok), repo-relative, absolute, bare
    # name/slug. Picked from the live wiki so the check is project-agnostic (fresh wiki
    # with no pages skips the positive cases, the negative one always runs).
    page = next(WIKI.rglob("*.md"), None)
    if page:
        rel = page.relative_to(WIKI).as_posix()
        want = page.resolve()
        assert _resolve_page(rel) is not None and _resolve_page(rel).resolve() == want, rel
        assert _resolve_page(rel.replace("/", "\\")).resolve() == want, "backslash form"
        assert _resolve_page(f".memory/wiki/{rel}").resolve() == want, "repo-relative form"
        assert _resolve_page(str(page)).resolve() == want, "absolute form"
        bare = _resolve_page(page.name)
        assert bare is not None and bare.exists(), "bare filename"
        slug = _resolve_page(page.name[:-3])
        assert slug is not None and slug.exists(), "bare slug without .md"
    assert _resolve_page("no-such-page-xyz.md") is None, "missing page must be None"

    # tracker: event log -> per-day grading. The invariants that actually bite are the three
    # "false red" ones — an unfinished today is never a miss, a rule-stop is ⏸ not ✗, and a
    # half-week at the start is not a failed post-week — plus append-only last-wins.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td) / "tracker.md"
        # start Sun 2026-07-26; fenced example lines must NOT parse as data
        tp.write_text(
            "start: 2026-07-26\n```\n2020-01-01 connects=9/9\n```\n"
            "2026-07-26 connects=4/4\n2026-07-26 comments=1/3\n"
            "2026-07-27 connects=0/4 blocked:pending95\n2026-07-27 comments=3/3\n"
            "2026-07-28 comments=2/3\n2026-07-28 comments=3/3\n"  # re-run: last line wins
            "2026-07-28 post=1/1\n2026-07-31 post=1/1\n",
            encoding="utf-8")
        start, ev = tracker_parse(tp)
        assert start == date(2026, 7, 26), start
        assert (date(2020, 1, 1), "connects") not in ev, "fenced example must not parse as data"
        assert ev[(date(2026, 7, 28), "comments")] == (3, 3, None), ev[(date(2026, 7, 28), "comments")]

        d29 = date(2026, 7, 29)  # Wed: 28th settled, 29th still running
        assert _tracker_cell(ev, date(2026, 7, 26), "connects", d29) == "✅"
        assert _tracker_cell(ev, date(2026, 7, 26), "comments", d29) == "◐"  # 1/3
        assert _tracker_cell(ev, date(2026, 7, 27), "connects", d29) == "⏸"  # rule-stop
        assert _tracker_cell(ev, d29, "connects", d29) == "·", "running day is never a miss"
        # new-item rule (tracker.md: «Прошлые дни его не имеют — честно»): days before an
        # item's FIRST logged line are untracked `·`, not misses — else a freshly added item
        # opens with a week of false ✗ that resume reads aloud every morning
        assert _tracker_cell(ev, date(2026, 7, 27), "post", d29) == "·", "before item's 1st line"
        assert _tracker_cell(ev, date(2026, 7, 25), "connects", d29) == "·", "before 1st line"
        assert _tracker_cell(ev, date(2026, 7, 28), "connects", d29) == "✗", \
            "a settled day AFTER the item started with no line stays a real miss"
        assert _tracker_cell(ev, date(2026, 7, 27), "threads_post", d29) == "·", \
            "an item with no lines at all has no misses yet"

        # streak: today unlogged -> judged from yesterday; ⏸ keeps it alive, ◐ and a settled
        # missing day break it (the 28th has no connects line = a real miss)
        assert _tracker_streak(ev, "connects", start, date(2026, 7, 28)) == 2, "✅ + ⏸, today ignored"
        assert _tracker_streak(ev, "connects", start, d29) == 0, "settled miss on the 28th kills it"
        assert _tracker_streak(ev, "comments", start, d29) == 2, "28th + 27th ok, 26th was ◐"

        # posts are weekly; week 31 (Mon 27.07) is the first countable week — the half week
        # containing the 26th has a Monday before start and must not render as a failed 0/2
        assert _post_week(ev, date(2026, 7, 28)) == 2, _post_week(ev, date(2026, 7, 28))
        assert _post_streak(ev, start, date(2026, 8, 5)) == 1, "week 31 hit 2/2"
        assert _post_streak(ev, start, d29) == 0, "no full week finished yet"

        # report: pre-start day says so instead of painting the whole grid red
        assert "стартует" in tracker_report(tp, today=date(2026, 7, 25))[0]
        rep = "\n".join(tracker_report(tp, today=d29))
        assert "connects ✅⏸✗·" in rep, rep  # the ✗ IS the miss record — no extra gaps line
        assert "день поста" not in rep, "Wed is not a post day"
        # the ⚑ fires on a post day ONLY while the week is short — week 31 already has 2/2
        # (28th + 31st), so Friday the 31st must stay quiet; Tue 04.08 opens an empty week
        fri = "\n".join(tracker_report(tp, today=date(2026, 7, 31)))
        assert "день поста" not in fri, "week already 2/2 — no reminder"
        assert "посты 2/2 за нед" in fri, fri
        assert "день поста" in "\n".join(tracker_report(tp, today=date(2026, 8, 4))), "Tue, 0/2"
        # ...but once TODAY's post is logged, the ⚑ must go quiet even though the week is 1/2 —
        # firing it anyway is exactly the false "пост не вышел" resume shouted on 2026-08-04
        tracker_log("post=1/1", day="2026-08-04", path=tp)
        assert "день поста" not in "\n".join(tracker_report(tp, today=date(2026, 8, 4))), \
            "today's post logged — no reminder"

        # tracker-log appends (never rewrites) and rejects an unknown item
        assert tracker_log("connects=5/5", day="2026-07-29", path=tp) == "2026-07-29 connects=5/5"
        assert tracker_parse(tp)[1][(d29, "connects")] == (5, 5, None)
        try:
            tracker_log("bogus=1/1", path=tp)
            raise AssertionError("unknown item must be rejected")
        except SystemExit:
            pass

    # last_done(): None on empty/no-match, the line on a hit (reverse-scan = LAST match).
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty.md"
        empty.write_text("", encoding="utf-8")
        assert last_done(empty) is None, "empty log -> None"
        nomatch = Path(td) / "nomatch.md"
        nomatch.write_text("2026-01-01 checkpoint x\n2026-01-02 checkpoint y\n", encoding="utf-8")
        assert last_done(nomatch) is None, "no task-done -> None"
        hit = Path(td) / "hit.md"
        hit.write_text(
            "2026-01-01 task-done first\n2026-01-02 checkpoint y\n2026-01-03 task-done second\n",
            encoding="utf-8",
        )
        assert last_done(hit) == "2026-01-03 task-done second", last_done(hit)

    # _state_idle(): the wipe target keeps the project's own H1, drops every other section,
    # and must itself pass the STATE schema guard (else `state-reset` would break `guard`).
    idle = _state_idle("# STATE — proj\n\n## Now\n- a\n\n## Open\n- b\n\n## Pointers\n- c\n")
    assert idle.startswith("# STATE — proj\n"), idle
    assert _check_state(idle) == [], _check_state(idle)
    assert "## Open" not in idle and "## Pointers" not in idle, idle
    assert _state_idle("").startswith("# STATE\n"), _state_idle("")  # empty/HEADless -> default

    # state_reset(): the git arm — an UNCOMMITTED STATE is the only thing a wipe can destroy,
    # so it is snapshotted first and `git show HEAD:` still restores it. Throwaway repo.
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        (r / ".memory").mkdir()
        st = r / ".memory" / "STATE.md"

        def _g(*a):
            return subprocess.run(("git",) + a, cwd=r, capture_output=True, text=True)

        _g("init", "-q")
        _g("config", "user.email", "t@t")
        _g("config", "user.name", "t")
        other = r / "other.txt"
        st.write_text("# STATE — t\n\n## Now\n- committed\n", encoding="utf-8")
        other.write_text("old\n", encoding="utf-8")
        _g("add", "-A")
        _g("commit", "-qm", "base")

        # Successful snapshot is path-limited: unrelated staged work stays staged and is
        # absent from the snapshot commit.
        other.write_text("STAGED OTHER\n", encoding="utf-8")
        _g("add", "other.txt")
        st.write_text("# STATE — t\n\n## Now\n- UNCOMMITTED\n", encoding="utf-8")
        out = state_reset(root=r)
        assert "No active task" in read_markdown(st), read_markdown(st)
        assert any("dirty STATE committed" in x for x in out), out
        assert "UNCOMMITTED" in _g("show", "HEAD:.memory/STATE.md").stdout, out
        assert _g("show", "HEAD:other.txt").stdout == "old\n", "foreign staged file leaked"
        assert "other.txt" in _g("diff", "--cached", "--name-only").stdout,             "foreign staged file must remain staged"

        # Failed snapshot is a hard abort: original STATE survives byte-for-byte and rc is
        # surfaced by the CLI wrapper instead of a false success/restore instruction.
        precious = b"# STATE -- t\n\n## Now\n- IRREPLACEABLE\n"
        st.write_bytes(precious)
        hook = r / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        try:
            state_reset(root=r)
            raise AssertionError("rejected snapshot must abort")
        except StateResetError as e:
            assert "ABORTED" in str(e), e
        assert st.read_bytes() == precious, "failed snapshot must not touch STATE"

    # Supersession metadata executes only in strict decision frontmatter. Prose/code-fence
    # examples are inert; malformed/self/cyclic graphs never alter the active set.
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "wiki"
        (w / "decisions").mkdir(parents=True)
        (w / "concepts").mkdir()
        d1 = w / "decisions" / "0001-base.md"
        d2 = w / "decisions" / "0002-next.md"
        d1.write_text("---\nid: 0001\ndate: 2026-01-01\n---\n# Base\n", encoding="utf-8")
        d2.write_text("---\nid: 0002\ndate: 2026-01-02\n---\n# Next\n", encoding="utf-8")
        (w / "concepts" / "example.md").write_text(
            "# Example\n```yaml\nsupersedes: 0001\n```\n", encoding="utf-8")
        *_, active = extract(wiki=w)
        assert active == ["0001", "0002"], active

        d2.write_text("---\nid: 0002\ndate: 2026-01-02\nsupersedes: 0001\n---\n# Next\n",
                      encoding="utf-8")
        _, warn, superseded, active = extract(wiki=w)
        assert superseded == ["0001"] and active == ["0002"] and not any(
            "supersedes" in x for x in warn), (warn, superseded, active)

        d2.write_text("---\nid: 0002\ndate: 2026-01-02\nsupersedes: 0002\n---\n# Next\n",
                      encoding="utf-8")
        _, warn, superseded, active = extract(wiki=w)
        assert superseded == [] and active == ["0001", "0002"]
        assert any("itself" in x for x in warn), warn

        d1.write_text("---\nid: 0001\ndate: 2026-01-01\nsupersedes: 0002\n---\n# Base\n",
                      encoding="utf-8")
        d2.write_text("---\nid: 0002\ndate: 2026-01-02\nsupersedes: 0001\n---\n# Next\n",
                      encoding="utf-8")
        _, warn, superseded, active = extract(wiki=w)
        assert superseded == [] and active == ["0001", "0002"], (warn, superseded, active)
        assert any("cycle" in x for x in warn), warn
        pages = {p.name: read_markdown(p) for p in (w / "decisions").glob("*.md")}
        assert any("cycle" in x for x in _doctor_decisions(pages)), _doctor_decisions(pages)
        impossible = {"0003-bad-date.md":
                      "---\nid: 0003\ndate: 2026-02-31\n---\n# Impossible\n"}
        assert any("impossible calendar date" in x for x in _doctor_decisions(impossible))

    # Live filesystem layer rejects a symlink before reading it as wiki content.
    import os
    if os.name != "nt":
        with tempfile.TemporaryDirectory() as td:
            w = Path(td) / "wiki"
            w.mkdir()
            outside = Path(td) / "outside.md"
            outside.write_text("external\n", encoding="utf-8")
            (w / "linked.md").symlink_to(outside)
            assert any("linked.md" in x and "regular" in x
                       for x in _worktree_wiki_type_violations(w))

    # [[?slug]] wanted-pages: a "?"-prefixed wikilink is a DELIBERATE TODO (page needed but
    # not written yet) — a wants-edge, never a broken-link warning. A plain [[missing]] still
    # warns; a [[?existing]] whose page exists warns "resolved — drop the '?'".
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "wiki"
        (w / "concepts").mkdir(parents=True)
        (w / "concepts" / "existing.md").write_text("# Existing\n", encoding="utf-8")
        (w / "concepts" / "src.md").write_text(
            "# Src\nsee [[existing]], [[missing]], [[?future-page]], [[?existing]], "
            "[[slug]], [[?owner]]\n",
            encoding="utf-8")
        e, warn, *_ = extract(wiki=w)
        assert ("src", "wants", "future-page") in e, e
        assert not any("slug" in x for x in warn), warn  # placeholder is NOT broken
        assert not any(r == "wants" and d == "owner" for _, r, d in e), e  # no wants-edge
        assert not any("future-page" in x for x in warn), warn  # ?-link is NOT broken
        assert any("broken [[missing]]" in x for x in warn), warn  # plain missing still warns
        assert any("?existing" in x and "resolved" in x for x in warn), warn

    # recent_runs (session 42): newest file PER queue dir; a quiet queue must survive beside a
    # chatty one — that asymmetry is the bug it exists for. Non-dated files are not runs.
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        for chan, name, dates in (("linkedin", "comment-queue", ("2026-07-29", "2026-07-31")),
                                  ("n8n", "job-queue", ("2026-07-30",)),
                                  ("make", "job-queue", ("2026-07-14",))):
            (r / chan / name).mkdir(parents=True)
            for d in dates:
                (r / chan / name / f"{d}.md").write_text(f"# Q {d}\n", encoding="utf-8")
        (r / "linkedin" / "comment-queue" / "notes.md").write_text("# x\n", encoding="utf-8")
        rr = recent_runs(r)
        assert rr == [("2026-07-31", "linkedin/comment"), ("2026-07-30", "n8n/job"),
                      ("2026-07-14", "make/job")], rr   # same-name queues stay distinct
        assert recent_runs(Path(td) / "nope") == []

    # flags(): markers alone are insufficient — reciprocal frontmatter, matching visible
    # callouts, and a separate low-confidence gap callout form one validated protocol.
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "wiki"
        (w / "concepts").mkdir(parents=True)
        (w / "concepts" / "a.md").write_text(
            "---\ncontradicted: b\n---\n# A\n> [!warning] CONTRADICTS [[b]]: A says left.\n", encoding="utf-8")
        (w / "concepts" / "b.md").write_text(
            "---\ncontradicted: a\n---\n# B\n> [!warning] CONTRADICTS [[a]]: B says right.\n", encoding="utf-8")
        (w / "concepts" / "c.md").write_text(
            "---\nconfidence: low\n---\n# C\n> [!warning] Evidence gap: primary source missing.\n", encoding="utf-8")
        (w / "concepts" / "d.md").write_text(
            "---\ncontradicted: e\n---\n# D\n> [!warning] CONTRADICTS [[e]]: values differ.\n", encoding="utf-8")
        (w / "concepts" / "e.md").write_text("# E\nno frontmatter\n", encoding="utf-8")
        (w / "concepts" / "f.md").write_text(
            "---\ncontradicted: nope\n---\n# F\n> [!warning] CONTRADICTS [[nope]]: absent.\n", encoding="utf-8")
        (w / "concepts" / "g.md").write_text(
            "---\ncontradicted: h\n---\n# G\n", encoding="utf-8")
        (w / "concepts" / "h.md").write_text(
            "---\ncontradicted: g\n---\n# H\n> [!warning] CONTRADICTS [[g]]: mismatch.\n", encoding="utf-8")
        (w / "concepts" / "i.md").write_text(
            "---\nconfidence: low\n---\n# I\n", encoding="utf-8")
        (w / "concepts" / "j.md").write_text(
            "# J\n> [!warning] CONTRADICTS [[a]]: orphan callout.\n", encoding="utf-8")
        (w / "concepts" / "k.md").write_bytes(
            b"---\r\nconfidence: low\r\n---\r\n# K\r\n> [!warning] Gap: CRLF evidence.\r\n")
        (w / "concepts" / "l.md").write_text(
            "---\nconfidence: low\n---\n# L\n<!-- > [!warning] hidden gap -->\n", encoding="utf-8")
        (w / "concepts" / "m.md").write_text(
            "---\nconfidence: low\n---\n# M\n~~~md\n> [!warning] fenced gap\n~~~\n", encoding="utf-8")
        (w / "concepts" / "n.md").write_text(
            "---\nconfidence: low\n> [!warning] frontmatter gap\n---\n# N\n", encoding="utf-8")
        (w / "concepts" / "o.md").write_text(
            "---\nconfidence: low\n---\n# O\n<!--\n> [!warning] unclosed hidden gap\n", encoding="utf-8")
        (w / "concepts" / "p.md").write_text(
            "---\nconfidence: low\n---\n# P\n```md\n> [!warning] unclosed fenced gap\n", encoding="utf-8")
        (w / "concepts" / "q.md").write_text(
            "---\nconfidence: low\n---\n# Q\n> [!warning]\n> Evidence gap is documented here.\n", encoding="utf-8")
        (w / "concepts" / "r.md").write_text(
            "---\nconfidence: low\n---\n# R\n````md\n> [!warning] hidden gap\n````\n", encoding="utf-8")
        (w / "concepts" / "s.md").write_text(
            "---\nconfidence: low\n---\n# S\n~~~~~md\n> [!warning] hidden gap\n~~~~~\n", encoding="utf-8")
        (w / "concepts" / "t.md").write_text(
            "---\nconfidence: low\n---\n# T\n````md\n> [!warning] hidden\n`````\n> [!warning] visible gap after fence.\n", encoding="utf-8")
        pairs, low, fwarn = flags(wiki=w)
        assert ("a", "b") in pairs and not any(x.startswith("a:") or x.startswith("b:") for x in fwarn), fwarn
        assert ("d", "e") in pairs and any("one-sided" in x and "d" in x for x in fwarn), fwarn
        assert any("nope" in x and "no such page" in x for x in fwarn), fwarn
        assert any("g" in x and "lacks `CONTRADICTS" in x for x in fwarn), fwarn
        assert any("i" in x and "naming the gap" in x for x in fwarn), fwarn
        assert any("j" in x and "without matching frontmatter" in x for x in fwarn), fwarn
        norm_low = [x.replace("\\", "/") for x in low]
        assert all(f"concepts/{name}.md" in norm_low
                   for name in ("c", "i", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t")), norm_low
        assert not any(x.startswith("k:") or x.startswith("q:") or x.startswith("t:") for x in fwarn), fwarn
        assert all(any(x.startswith(f"{name}:") and "naming the gap" in x for x in fwarn)
                   for name in ("l", "m", "n", "o", "p", "r", "s")), fwarn
        assert len(pairs) == 3, pairs

    # One parser owns the canonical Markdown-link summary format across lint/resume/compact.
    canonical = "- [0001](decisions/0001-a.md) - canonical verdict"
    assert parse_decision_index_line(canonical) == (
        "0001", "decisions/0001-a.md", "canonical verdict", canonical)
    legacy_parsed = parse_decision_index_line("- 0002-b — legacy verdict")
    assert (legacy_parsed[0], legacy_parsed[2]) == ("0002", "legacy verdict")
    assert parse_decision_index_line("- [0001](decisions/0002-b.md) - mismatch") is None
    assert _prose_of(canonical) == "canonical verdict"
    records, record_problems = decision_index_records(
        "## Decisions\n" + canonical + "\n- [0001](decisions/0001-a.md) - duplicate\n- malformed\n")
    assert records == {"0001": canonical}, records
    assert any("duplicate" in x for x in record_problems) and any("unrecognized" in x for x in record_problems)
    empty_records, empty_problems = decision_index_records(
        "## Decisions\n(none yet)\n[source: `CLAUDE.md`]\n## Concepts\n(none yet)\n", {})
    assert empty_records == {} and empty_problems == [], (empty_records, empty_problems)
    exact_records, exact_problems = decision_index_records(
        "## Decisions archive\n(none yet)\n## Decisions\n- malformed\n", {})
    assert exact_records == {} and any("unrecognized" in x for x in exact_problems), exact_problems
    target_records, target_problems = decision_index_records(
        "## Decisions\n" + canonical + "\n", {"0001": "decisions/0001-real.md", "0002": "decisions/0002-b.md"})
    assert "0001" in target_records
    assert any("expected decisions/0001-real.md" in x for x in target_problems), target_problems
    assert any("decision 0002" in x and "lacks" in x for x in target_problems), target_problems

    # parse_groups() + _collapse_decisions(): pack-budget compress rung (decision 0017) —
    # a closed track's decisions collapse to ONE group line at the newest member's position;
    # ungrouped actives keep their index lines; no `## Decision groups` section -> no-op.
    gidx = ("## Decisions\n- 0001-a — q1\n- 0002-b — q2\n- 0003-c — q3\n"
            "## Decision groups\n- ids: 0001 0002 :: track закрыт: q1/q2 — ок\n")
    gg = parse_groups(gidx)
    assert gg == [({"0001", "0002"}, "track закрыт: q1/q2 — ок")], gg
    gby = {"0001": "- 0001-a — q1", "0002": "- 0002-b — q2", "0003": "- 0003-c — q3"}
    gl = _collapse_decisions(["0001", "0002", "0003"], gby, gg)
    assert gl == ["- 0003-c — q3", "- track закрыт: q1/q2 — ок"], gl
    assert parse_groups("## Decisions\n- only\n") == [], "no groups section -> []"
    assert _collapse_decisions(["0003"], gby, []) == ["- 0003-c — q3"], "no groups -> as before"
    # a group whose members are ALL superseded simply never prints (no active member hits it)
    assert _collapse_decisions(["0003"], gby, gg) == ["- 0003-c — q3"]
    # pack_size(): renders the live pack — positive token estimate, text non-empty
    pstok, pstext = pack_size()
    assert pstok > 0 and pstext.startswith("# resume pack"), (pstok, pstext[:40])

    # _apply_fold(): compact's pure rung — one group folds into the archive line
    fidx = ("## Decision groups\n"
            "- ids: 0001 0002 :: старый трек: ок\n"
            "- ids: 0003 0004 :: архив: 2 вопросов закрыто — decisions/, search найдёт\n")
    ftxt = _apply_fold(fidx, {"0001", "0002"})
    assert ("- ids: 0001 0002 0003 0004 :: архив: 4 вопросов закрыто — decisions/, "
            "search найдёт") in ftxt, ftxt
    assert "старый трек" not in ftxt, ftxt
    fno = _apply_fold("## Decision groups\n- ids: 0005 0006 :: трек: ок\n", {"0005", "0006"})
    assert "архив: 2 вопросов" in fno and "трек: ок" not in fno, fno  # no archive -> becomes it
    assert _apply_fold(fidx, {"0009"}) == fidx, "unknown group -> no-op"

    # _guard_compare(): checkpoint-time integrity check (idea §2, KISA WIKI_LINKS_LOST
    # descendant) — PURE dict-vs-dict compare, git plumbing stays in guard(). Invariants:
    # filed decisions byte-immutable (modify/delete = violation), log.md append-only
    # (baseline must be a prefix), [[wikilinks]] never lost from a knowledge page
    # (concepts/ = violation; derived overview/index = advisory warning — they are
    # legitimately rewritten each checkpoint). [[?x]] -> [[x]] resolution is NOT a loss;
    # links inside code fences never count; CRLF/LF differences never trip anything.
    gbase = {
        "decisions/0001-a.md": "---\nid: 0001\n---\nbody\n",
        "log.md": "l1\nl2\n",
        "concepts/c.md": "# C\nsee [[other]] and [[?future]]\n",
        "concepts/fenced.md": "# F\n```\n[[code-only]]\n```\nprose\n",
        "overview.md": "# O\n[[c]]\n",
        "concepts/other.md": "# Other\n",
    }
    gclean = dict(gbase)
    gclean["concepts/future.md"] = "# Future\n"
    gclean["concepts/extra.md"] = "# Extra\n"
    gclean["decisions/0002-b.md"] = "new decision file\n"          # NEW decision -> fine
    gclean["log.md"] = "l1\nl2\nl3\n"                              # pure append -> fine
    gclean["concepts/c.md"] = "# C\nsee [[other]], [[future]], [[extra]]\n"  # ? resolved + gained
    gclean["concepts/fenced.md"] = "# F\nprose only now\n"         # fenced link was never a link
    gv, gw = _guard_compare(gbase, gclean)
    assert gv == [] and gw == [], (gv, gw)
    gv, _ = _guard_compare(gbase, {**gclean, "decisions/0001-a.md": "edited\n"})
    assert any("0001" in x and "MODIFIED" in x for x in gv), gv
    gdel = dict(gclean); del gdel["decisions/0001-a.md"]
    gv, _ = _guard_compare(gbase, gdel)
    assert any("0001" in x and "DELETED" in x for x in gv), gv
    gv, _ = _guard_compare(gbase, {**gclean, "log.md": "l1\nEDITED\nl3\n"})
    assert any("log.md" in x and "append-only" in x for x in gv), gv
    gv, _ = _guard_compare(gbase, {**gclean, "log.md": "l1\n"})   # truncation = rewrite
    assert any("log.md" in x for x in gv), gv
    gv, _ = _guard_compare(gbase, {**gclean, "concepts/c.md": "# C\nsee [[other]]\n"})
    assert any("concepts/c.md" in x and "future" in x for x in gv), gv  # knowledge page -> violation
    gv, gw = _guard_compare(gbase, {**gclean, "overview.md": "# O\nno links now\n"})
    assert gv == [] and any("overview.md" in x for x in gw), (gv, gw)   # derived -> warning only
    gdelc = dict(gclean); del gdelc["concepts/c.md"]
    gv, gw = _guard_compare(gbase, gdelc)
    assert (any("overview.md" in x and "[[c]]" in x for x in gv)
            and any("concepts/c.md" in x and "deleted" in x for x in gw)), (gv, gw)
    gv, gw = _guard_compare(gbase, {**gclean, "log.md": "l1\r\nl2\r\nl3\r\n"})
    assert gv == [] and gw == [], ("CRLF must not trip append-only", gv, gw)
    gv, gw = _guard_compare({}, gclean)                            # no checkpoint yet
    assert gv == [] and gw == [], (gv, gw)
    # A decision committed after the checkpoint is absent from gbase but present in HEAD.
    # The second baseline freezes it immediately; an untracked new decision remains editable.
    mid_head = {"decisions/0002-b.md": "filed\n"}
    assert _newly_filed_decision_compare(mid_head, gbase, {**gclean, "decisions/0002-b.md": "filed\n"}) == []
    mid_mod = _newly_filed_decision_compare(mid_head, gbase, {**gclean, "decisions/0002-b.md": "edited\n"})
    assert any("0002-b" in x and "MODIFIED" in x for x in mid_mod), mid_mod
    mid_del = dict(gclean); mid_del.pop("decisions/0002-b.md", None)
    assert any("0002-b" in x and "DELETED" in x
               for x in _newly_filed_decision_compare(mid_head, gbase, mid_del))
    staged_only = _newly_filed_decision_compare(
        mid_head, gbase, {**gclean, "decisions/0002-b.md": "filed\n"},
        {"decisions/0002-b.md": "staged illegal\n"})
    assert any("STAGED MODIFICATION" in x for x in staged_only), staged_only
    assert _newly_filed_decision_compare({}, gbase, gclean, {}) == [], "untracked decision stays editable"

    # _doctor_decisions(): PURE frontmatter validity — id == filename prefix, unique ids,
    # date YYYY-MM-DD, frontmatter present, filename NNNN-slug.md. Valid file -> no problems.
    good = "---\nid: 0001\ndate: 2026-07-05\n---\n# G\n"
    assert _doctor_decisions({"0001-good.md": good}) == [], _doctor_decisions({"0001-good.md": good})
    dp = _doctor_decisions({
        "0002-mismatch.md": "---\nid: 0009\ndate: 2026-07-05\n---\n",   # id != filename
        "0003-nodate.md": "---\nid: 0003\n---\n",                        # date missing
        "0004-nofm.md": "# no frontmatter\n",                            # no frontmatter
        "bad_name.md": good,                                             # filename shape
        "0005-baddate.md": "---\nid: 0005\ndate: 05.07.2026\n---\n",     # not ISO
    })
    assert any("0002" in x and "!=" in x for x in dp), dp
    assert any("0003" in x and "date" in x for x in dp), dp
    assert any("0004" in x and "frontmatter" in x for x in dp), dp
    assert any("bad_name" in x and "NNNN-slug" in x for x in dp), dp
    assert any("0005" in x and "date" in x for x in dp), dp
    # duplicate id across two files (both named validly)
    dup = _doctor_decisions({"0007-a.md": "---\nid: 0007\ndate: 2026-01-01\n---\n",
                             "0007-b.md": "---\nid: 0007\ndate: 2026-01-02\n---\n"})
    assert any("duplicate id 0007" in x for x in dup), dup

    # _probe_hook(): runs the command EXACTLY like the harness (sh -c, payload on stdin).
    # exit 0 passes through; nonzero passes through; a backslash Windows path must die 127
    # (the POSIX shell eats the backslashes) — TODAY'S live bug class, the reason doctor
    # exists. Skip gracefully where sh itself is absent.
    proot = WIKI.parents[1]
    code, _ = _probe_hook("true", "{}", proot)
    if code is not None:  # sh available
        assert code == 0, code
        code, _ = _probe_hook("exit 3", "{}", proot)
        assert code == 3, code
        code, _ = _probe_hook(r"C:\nope\rtk.exe hook claude", "{}", proot)
        assert code == 127, ("backslash path must be command-not-found", code)

    # doctor() wiring: on THIS repo it must return the right shape; and a synthetic broken
    # root (no settings.json, no fence) must produce fails and exit-1-worthy state.
    with tempfile.TemporaryDirectory() as td:
        broken = Path(td)
        bf, bw, bo = doctor(root=broken)
        assert any("settings.json missing" in x for x in bf), bf
        assert any("STATE.md missing" in x for x in bf), bf

    # search(): porter stemming finds an inflected form a literal grep MISSES; AND derived
    # rollups (top-level overview/index/log, kind='page') are EXCLUDED from results — they are
    # computed POINTERS, not knowledge sources, and otherwise pollute top-5 (idea #6 eviction
    # bench: rollups occupied a top-5 slot in 67% of queries and evicted a target). Skip
    # gracefully if FTS5 is not compiled into this Python's sqlite3 (CLI degrades to grep).
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "wiki"
        (w / "concepts").mkdir(parents=True)
        (w / "concepts" / "retry.md").write_text(
            "# Retry\nWe retry and the call is retried with backoff.\n", encoding="utf-8")
        (w / "overview.md").write_text(  # derived rollup, also mentions retry -> must be excluded
            "# Overview\nThe retry policy is summarized here for the rollup.\n", encoding="utf-8")
        (w / "concepts" / "morph-a.md").write_text(
            "# Морф-А\nОдин промах записан.\n", encoding="utf-8")
        (w / "concepts" / "morph-b.md").write_text(
            "# Морф-Б\nСеть промахивается на ретраях.\n", encoding="utf-8")
        (w / "concepts" / "inline.md").write_text(
            "# Инлайн\nГейт живёт в `consult_gate.py` и молчит.\n", encoding="utf-8")
        (w / "concepts" / "connects-autopilot.md").write_text(
            "# Автопилот\nКоннект-инвайты идут по венам.\n", encoding="utf-8")
        (w / "decisions").mkdir()
        (w / "decisions" / "0001-old.md").write_text(
            "---\nid: 0001\ndate: 2026-01-01\n---\n# Old\nlegacyneedle legacyneedle legacyneedle\n",
            encoding="utf-8")
        (w / "decisions" / "0002-new.md").write_text(
            "---\nid: 0002\ndate: 2026-01-02\nsupersedes: 0001\n---\n# New\nlegacyneedle current rule\n",
            encoding="utf-8")
        for old_id, new_id in (("0003", "0004"), ("0005", "0006")):
            (w / "decisions" / f"{old_id}-history.md").write_text(
                f"---\nid: {old_id}\ndate: 2026-01-03\n---\n# Old history\nhistoryonly\n",
                encoding="utf-8")
            (w / "decisions" / f"{new_id}-replacement.md").write_text(
                f"---\nid: {new_id}\ndate: 2026-01-04\nsupersedes: {old_id}\n---\n# Replacement\ncurrent text\n",
                encoding="utf-8")
        try:
            paths = [p for p, _ in search("retries", wiki=w)]  # 'retries' -> stem 'retri' -> retry
            assert any(p.endswith("retry.md") for p in paths), paths
            assert not any(p.endswith("overview.md") for p in paths), ("derived rollup not excluded", paths)
            # hole #4 fixes: RU root+prefix meets both form directions; backticked tokens
            # are indexed; an EN slug finds a RU-bodied page
            for q, suf in [("промах", "morph-b.md"), ("промахивается", "morph-a.md"),
                           ("consult_gate", "inline.md"), ("connects", "connects-autopilot.md")]:
                got = [p for p, _ in search(q, wiki=w)]
                assert any(p.endswith(suf) for p in got), (q, got)
            ranked = search("legacyneedle", wiki=w)
            ranked_paths = [p for p, _ in ranked]
            assert ranked_paths.index("decisions/0002-new.md") < ranked_paths.index("decisions/0001-old.md"), ranked
            old_snip = next(s for p, s in ranked if p.endswith("0001-old.md"))
            assert "SUPERSEDED by 0002-new" in old_snip, old_snip
            history_only = _search_results("historyonly", wiki=w)
            assert len(history_only) == 2 and all(status == "superseded"
                                                  for _p, _s, status, _r in history_only), history_only
        except sqlite3.OperationalError:
            pass  # no FTS5 in this build; runtime falls back to _grep_fallback
        assert not any(t in "we retry and the call is retried" for t in ["retries"]), "stem-gap sanity"
        # _term_expr: RU root+star, short-term and Latin behavior — pure, no FTS needed
        for term, expr in [("коннекты", '"коннект"*'), ("промахивается", '"промах"*'),
                           ("пак", '"пак"*'), ("py", '"py"'), ("consult", '"consult"*')]:
            assert _term_expr(term) == expr, (term, _term_expr(term))
        # wiki_map: knowledge pages only, 'path — title' lines, rollups out
        mlines = wiki_map(wiki=w)
        assert any("connects-autopilot.md — Автопилот" in ln for ln in mlines), mlines
        assert any("0001-old.md" in ln and "SUPERSEDED by 0002-new" in ln for ln in mlines), mlines
        assert not any("overview" in ln for ln in mlines), mlines
        fallback = _grep_fallback("legacyneedle", wiki=w)
        assert fallback[0][0].endswith("0002-new.md"), fallback
        assert any("0001-old.md" in p and "SUPERSEDED" in line for p, _i, line in fallback), fallback
        history_fallback = _grep_fallback_results("historyonly", wiki=w)
        assert history_fallback and all(status == "superseded"
                                        for _p, _i, _line, status, _r in history_fallback), history_fallback

    # _max_session / _check_new_log_lines / _check_state (O1+O3): PURE checkpoint-shape
    # invariants. History is never re-judged; only appended lines gate.
    hdr = "# Log\n> fmt\n"
    old = hdr + "2026-07-13 checkpoint migration session 1 — a\n" \
                "2026-07-14 checkpoint channels session 1 — restart anomaly, history\n" \
                "2026-07-14 checkpoint channels session 11 — b\n"
    assert _max_session(old) == 11, _max_session(old)
    assert _max_session(hdr) is None
    ok = old + "2026-07-15 checkpoint channels session 12 — fine\n"
    assert _check_new_log_lines(old, ok) == [], _check_new_log_lines(old, ok)
    td_ok = old + "2026-07-15 task-done channels — closed\n"
    assert _check_new_log_lines(old, td_ok) == []
    bad_n = old + "2026-07-15 checkpoint channels session 14 — skipped 12,13\n"
    assert any("expected 12" in x for x in _check_new_log_lines(old, bad_n))
    bad_fmt = old + "just a note\n"
    assert any("matches neither" in x for x in _check_new_log_lines(old, bad_fmt))
    bad_len = old + "2026-07-15 checkpoint channels session 12 — " + "x" * 200 + "\n"
    assert any("cap is 200" in x for x in _check_new_log_lines(old, bad_len))
    assert _check_new_log_lines(old, old) == []                     # nothing appended
    assert _check_new_log_lines("zzz", old) == []                   # not a prefix -> append-only branch owns it
    st_ok = "# STATE\n## Now\n- x\n## Next\n- y\n## Tried & rejected\n- z\n## Open\n- q\n## Pointers\n- p\n"
    assert _check_state(st_ok) == [], _check_state(st_ok)
    assert _check_state("## Tried & rejected (не повторять)\n- z\n") == []   # annotation OK
    assert any("non-canonical" in x for x in _check_state("## Now\n- x\n## Хвосты\n- misc\n"))
    assert any("non-canonical" in x for x in _check_state("## Nowhere\n- x\n"))
    assert any("> 40" in x for x in _check_state("## Now\n" + "- line\n" * 41))
    # routine-in-STATE (session 42): the exact line session 41 shipped, plus the score-only form
    assert any("routine retold" in x for x in
               _check_state("## Now\n- Рутина 31.07 закрыта Ромой без меня: комменты 3/3.\n"))
    assert any("routine retold" in x for x in _check_state("## Now\n- connects 4/4 сегодня\n"))
    # skill names, log commands and Roma-without-a-score stay legal — else STATE can't plan
    assert _check_state("## Next\n- `/comments` первым: publish-путь не исполнялся\n") == []
    assert _check_state("## Open\n- залогировать пост: `tracker-log post=1/1`\n") == []
    assert _check_state("## Tried & rejected\n- Сводка комментов — Рома сказал не надо\n") == []
    ov_ok = ("# O\n## State of the project\n- **Цель:** x\n- **Движки:** y\n"
             "## Open threads\n- [[portfolio]] — секреты не ротированы\n"
             "- [[?wiki-tooling]] — очередь O4/O6\n")
    assert _check_overview(ov_ok) == [], _check_overview(ov_ok)
    ov_bad = "# O\n## State of the project\n" + "-" * 401 + "\n"
    assert any("cap is 400" in x for x in _check_overview(ov_bad))
    assert any("index line" in x for x in _check_overview("# O\n## Open threads\n- x\n"))
    ot_long = "# O\n## Open threads\n- [[p]] — " + "x" * 200 + "\n"
    assert any("cap is 200" in x for x in _check_overview(ot_long))
    assert _check_overview("# O\n- free text\n") == []   # neither section -> no-op

    # CLI contract: unknown commands and missing required operands are usage errors
    # (EX_USAGE=2), emitted on stderr without polluting stdout.
    script = Path(__file__).resolve()
    for cli_args in (("definitely-not-a-command",), ("section",),
                     ("section", "definitely-missing.md", "Any"), ("toc",),
                     ("toc", "definitely-missing.md"), ("search",),
                     ("search", "one", "extra"), ("active", "extra"),
                     ("doctor", "--bad"), ("tracker-log",),
                     ("tracker-log", "comments=0/1", "--blocked"),
                     ("compact", "--budget"), ("compact", "--unknown")):
        proc = subprocess.run(
            (sys.executable, str(script), *cli_args), capture_output=True, text=True)
        assert proc.returncode == 2, (cli_args, proc.returncode, proc.stdout, proc.stderr)
        assert proc.stderr.strip() and not proc.stdout, (cli_args, proc.stdout, proc.stderr)

    # The single Markdown reader is fail-closed: BOM and malformed UTF-8 are both
    # path-aware, clean CLI failures (no replacement decoding and no traceback).
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        bad_cases = (("bom.md", b"\xef\xbb\xbf# BOM\n"),
                     ("malformed.md", b"# bad\n\xff\n"))
        for name, payload in bad_cases:
            bad = td_path / name
            bad.write_bytes(payload)
            proc = subprocess.run(
                (sys.executable, str(script), "section", str(bad), "Any"),
                capture_output=True, text=True)
            assert proc.returncode == 1, (name, proc.returncode, proc.stdout, proc.stderr)
            assert str(bad) in proc.stderr and "Traceback" not in proc.stderr, proc.stderr
            assert not proc.stdout, proc.stdout
            assert ("without BOM" in proc.stderr if name == "bom.md"
                    else "not valid UTF-8" in proc.stderr), proc.stderr

    # Idle resume must compute anomaly-bearing data before its early return.  A throwaway
    # repository proves all four channels survive: post-checkpoint commit, dirty tree,
    # confidence flag and stale durable page.
    import shutil
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td)
        tools = fixture / ".memory" / "tools"
        wiki = fixture / ".memory" / "wiki"
        tools.mkdir(parents=True)
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "decisions").mkdir()
        for name in ("structural.py", "markdown_io.py", "git_checked.py",
                     "wiki_validation.py", "pre_commit_guard.py"):
            shutil.copy2(script.parent / name, tools / name)
        (fixture / ".memory" / "STATE.md").write_text(
            "# STATE — fixture\n\n## Now\n- No active task.\n", encoding="utf-8")
        (wiki / "log.md").write_text(
            "2020-01-01 task-done fixture — closed\n", encoding="utf-8")
        (wiki / "index.md").write_text("# Index\n", encoding="utf-8")
        (wiki / "overview.md").write_text("# Overview\n", encoding="utf-8")
        (wiki / "concepts" / "old.md").write_text(
            "---\nconfidence: low\n---\n# Old\n", encoding="utf-8")

        def fixture_git(*args, env=None):
            return subprocess.run(("git", *args), cwd=fixture, env=env,
                                  capture_output=True, text=True, check=True)

        fixture_git("init", "-q")
        fixture_git("config", "user.email", "selfcheck@example.invalid")
        fixture_git("config", "user.name", "structural selfcheck")
        fixture_git("add", "-A")
        old_env = dict(os.environ)
        old_env.update(GIT_AUTHOR_DATE="2020-01-01T12:00:00+0000",
                       GIT_COMMITTER_DATE="2020-01-01T12:00:00+0000")
        fixture_git("commit", "-qm", "checkpoint: idle fixture", env=old_env)
        (fixture / "after.txt").write_text("after\n", encoding="utf-8")
        fixture_git("add", "after.txt")
        fixture_git("commit", "-qm", "after checkpoint")
        (fixture / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        clean_env = dict(os.environ)
        clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            (sys.executable, str(tools / "structural.py"), "resume"), cwd=fixture,
            env=clean_env, capture_output=True, text=True)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        report = proc.stdout
        labels = ("== IDLE — no active task ==", "== FLAGS ==", "== CHECKPOINT ==",
                  "== STALE (>90d, verify before trusting) ==")
        assert all(label in report for label in labels), report
        assert [report.index(label) for label in labels] == sorted(
            report.index(label) for label in labels), report
        assert "confidence low: concepts/old.md" in report, report
        assert "commits after (NOT in STATE yet):" in report and "after checkpoint" in report, report
        assert "working tree (uncommitted" in report and "dirty.txt" in report, report
        assert "2020-01-01  concepts/old.md" in report, report

    # freshness backstop (session 7): missing-path + volatile-status catches on durable
    # pages; the impl-detail class (a raw count) stays OUT — that's the write-side discipline.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        troot = Path(td)
        cdir = troot / ".memory" / "wiki" / "concepts"
        cdir.mkdir(parents=True)
        (troot / "code").mkdir()
        (troot / "code" / "real.py").write_text("x", encoding="utf-8")
        (cdir / "bad.md").write_text(                       # gone path + volatile status
            "# B\nArtifacts live under `code/gone_dir/x.py`.\n"
            "The real photo hasn't been provided yet — placeholder box shown.\n", encoding="utf-8")
        (cdir / "good.md").write_text(                      # existing path, no volatile, a count
            "# G\nThe scanner lives at `code/real.py`. Selfcheck has 20 asserts.\n", encoding="utf-8")
        fmiss, fvol = freshness_scan(troot)
        mp = {pg for pg, _ in fmiss}
        vp = {pg for pg, _ in fvol}
        assert ".memory/wiki/concepts/bad.md" in mp, fmiss   # gone path caught
        assert ".memory/wiki/concepts/bad.md" in vp, fvol    # volatile status caught
        assert all("good.md" not in pg for pg, _ in fmiss), fmiss  # existing path -> clean
        assert all("good.md" not in pg for pg, _ in fvol), fvol    # a raw count is NOT volatile
    print("self-check OK")


# --- daily routine tracker (.memory/tracker.md) -----------------------------------------
# Habit tracker over the social routine. Lives OUTSIDE the wiki fence: daily facts carry no
# provenance, and STATE is rewritten every checkpoint so history would die there. Storage is
# a flat event log on purpose (~50 B/day, 10 y ≈ 200 KB): git keeps the history, the diff
# stays readable, and one writer means a DB would only cost the diff and the eyeball.
TRACKER = Path(__file__).resolve().parents[2] / ".memory" / "tracker.md"
TRACKER_ITEMS = ("connects", "comments", "post", "threads_post")
TRACKER_DAILY = ("connects", "comments", "threads_post")  # graded per day
TRACKER_WEEK_TARGET = 2  # posts per ISO week (plan: Tue + Fri)
POST_DAYS = (1, 4)  # date.weekday(): Mon=0 -> Tue, Fri = the reminder days
RU_DOW = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
TRACKER_EVENT = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ \t]+(\w+)=(\d+)/(\d+)(?:[ \t]+blocked:(\S+))?[ \t]*$", re.M)
TRACKER_START = re.compile(r"^start:[ \t]*(\d{4}-\d{2}-\d{2})", re.M)


def tracker_parse(path: Path = TRACKER):
    """Return (start_date, {(day, item): (done, target, blocked_reason)}).

    Append-only log: the LAST line for a (day, item) wins, so a correction or a re-run is a
    new line, never an edit. Code fences are stripped first — the file documents its own
    format with example lines, and those must never parse as data.
    """
    if not path.exists():
        return None, {}
    text = read_markdown(path)
    m = TRACKER_START.search(text)
    start = date.fromisoformat(m.group(1)) if m else None
    events = {}
    for d, item, done, target, blocked in TRACKER_EVENT.findall(_strip_code(text)):
        events[(date.fromisoformat(d), item)] = (int(done), int(target), blocked or None)
    return start, events


def _tracker_cell(events, day, item, today):
    """One grid cell. A day is a miss only once it is OVER — the running day shows `·`.
    Days before the item's FIRST logged line are untracked `·`, not misses: a new item
    starts the day it starts (tracker.md: «Прошлые дни его не имеют — честно»)."""
    hit = events.get((day, item))
    if hit is None:
        logged = [d for (d, it) in events if it == item]
        if not logged or day < min(logged):
            return "·"
        return "·" if day >= today else "✗"
    done, target, blocked = hit
    if blocked:
        return "⏸"  # stopped by a rule (queue ceiling, no clean fits) — not slacking
    return "✅" if done >= target else "◐"


def _tracker_streak(events, item, start, today):
    """Consecutive days of ✅/⏸ ending at the last SETTLED day.

    An unfinished today never breaks a streak: if today has no line yet, judging starts at
    yesterday. Days before `start` are outside the tracker, so the streak stops there.
    """
    if not start:
        return 0
    day = today if (today, item) in events else today - timedelta(days=1)
    n = 0
    while day >= start:
        hit = events.get((day, item))
        if hit is None or not (hit[2] or hit[0] >= hit[1]):
            break
        n += 1
        day -= timedelta(days=1)
    return n


def _post_week(events, day):
    """Posts logged in day's ISO week. Posts are graded per week, not per day — the Tue/Fri
    plan is the reminder, the week is the commitment."""
    yw = day.isocalendar()[:2]
    return sum(done for (d, it), (done, _t, _b) in events.items()
               if it == "post" and d.isocalendar()[:2] == yw)


def _post_streak(events, start, today):
    """Consecutive FULL weeks hitting the target. The running week is excluded (not over yet).
    A week counts only if its Monday is on/after `start` — a half-week at the start would
    otherwise render as a false 0/2 on day one, which is how these trackers die."""
    if not start:
        return 0
    monday = today - timedelta(days=today.weekday() + 7)  # Monday of last week
    n = 0
    while monday >= start:
        if _post_week(events, monday) < TRACKER_WEEK_TARGET:
            break
        n += 1
        monday -= timedelta(days=7)
    return n


def tracker_report(path: Path = TRACKER, today: date | None = None) -> list[str]:
    """Render the routine block: today's cells, a 7-day grid, the streaks, unlogged misses.

    Read-side only — nothing is written on idle days, absence IS the miss record.
    """
    today = today or date.today()
    start, events = tracker_parse(path)
    if start is None:
        return ["(трекер не заведён — нет .memory/tracker.md)"]
    if today < start:
        return [f"трекер стартует {start} — ещё не начали"]

    # kept deliberately tight — the pack ceiling (0017) is real and this block renders on every
    # resume. Today is the LAST cell of the grid, so it gets no line of its own; a missed day
    # needs no line either, the ✗ in the grid IS the record.
    week = _post_week(events, today)
    out = [f"{today.strftime('%d.%m')} {RU_DOW[today.weekday()]} · посты "
           f"{week}/{TRACKER_WEEK_TARGET} за нед · серии: "
           + " ".join(f"{it} {_tracker_streak(events, it, start, today)}д" for it in TRACKER_DAILY)
           + f" посты {_post_streak(events, start, today)}нед"]

    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    shown = [d for d in days if d >= start]
    if shown:
        out.append(f"{shown[0].strftime('%d.%m')}→{shown[-1].strftime('%d.%m')}: "
                   + " ".join(f"{it} {''.join(_tracker_cell(events, d, it, today) for d in shown)}"
                              for it in TRACKER_DAILY))

    # the ⚑ is a REMINDER, not a calendar entry — a week already at target, or a post already
    # logged TODAY, has nothing to remind about; firing it anyway is what made resume shout
    # "пост не вышел" on a day the post had shipped. Header still carries the week count.
    if (today.weekday() in POST_DAYS and week < TRACKER_WEEK_TARGET
            and not events.get((today, "post"), (0,))[0]):
        out.append(f"⚑ {RU_DOW[today.weekday()]} — день поста")
    return out


def tracker_log(spec: str, day: str | None = None, blocked: str | None = None,
                path: Path = TRACKER) -> str:
    """Append ONE event line and return it. Append-only: a fix is a new line (last wins).
    History is never rewritten — and a day that closed unlogged is never re-opened (0027):
    --date is Roma's explicit ask only, never the model's initiative."""
    m = re.fullmatch(r"(\w+)=(\d+)/(\d+)", spec.strip())
    if not m:
        raise SystemExit("usage: tracker-log <item>=<done>/<target> "
                         "[--date YYYY-MM-DD] [--blocked <reason>]")
    item, done, target = m.group(1), m.group(2), m.group(3)
    if item not in TRACKER_ITEMS:
        raise SystemExit(f"tracker-log: unknown item {item!r} — known: {', '.join(TRACKER_ITEMS)}")
    when = date.fromisoformat(day) if day else date.today()
    line = f"{when} {item}={done}/{target}" + (f" blocked:{blocked}" if blocked else "")
    if not path.exists():
        raise SystemExit(f"tracker-log: {path} missing — the tracker was never installed")
    text = read_markdown(path)
    path.write_text(text + ("" if text.endswith("\n") else "\n") + line + "\n", encoding="utf-8")
    return line


def _print_active():
    *_, active = extract()
    print(", ".join(reversed(active)))  # newest-first (id descending)


RUN_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def recent_runs(root: Path | None = None) -> list[tuple[str, str]]:
    """Newest run file per `<dir>/*-queue/` — (date, kind), newest-first.

    The pack's fact-side counterweight to STATE prose (session 42). A plan line ("прогнать
    оба скилла живьём") outlived three sessions of the skills actually running, because
    nothing beside it said when the last run WAS: the tracker grid shows a day was green,
    never which skill wrote what. Run files are written BY the skills, so unlike a
    hand-typed retelling they cannot drift. One entry per queue (not the last N overall) —
    else a chatty queue hides a quiet one, which is exactly the case that went wrong.
    Date + kind only: the run's own headline cost 318 tok of a 2400 pack, and "when did
    this last run" is the whole question — the file is one Read away once it matters.
    Kind keeps its parent (`n8n/job`, `make/job`) — two channels own a `job-queue` each.
    """
    root = root or WIKI.parents[1]
    out = []
    for qdir in sorted(root.glob("*/*-queue")):
        runs = sorted(f.stem for f in qdir.glob("*.md") if RUN_FILE.match(f.name))
        if runs:
            out.append((runs[-1], f"{qdir.parent.name}/{qdir.name[:-len('-queue')]}"))
    return sorted(out, reverse=True)


def _resume_checkpoint_data(root: Path):
    """Checkpoint anomaly data shared by active and idle resume rendering."""
    sha = _git_text(root, "log", "-1", "--format=%H", "--", ".memory/STATE.md")
    since = _git_text(root, "log", f"{sha}..HEAD", "--oneline") if sha else ""
    dirty = _git_text(root, "status", "--porcelain").splitlines()
    return sha, since, dirty


def _resume_durable_data(root: Path):
    """Change-frontier ordering and stale durable pages, computed before idle branching."""
    durable = sorted(list(WIKI.glob("concepts/*.md")) + list(WIKI.glob("decisions/*.md")))
    tracked, untracked = [], []
    for page in durable:
        cs = _git_text(root, "log", "-1", "--format=%cs", "--",
                       page.relative_to(root).as_posix())
        # blank %cs = untracked/uncommitted -> newest by definition; mtime orders among them
        (tracked.append((cs, page)) if cs
         else untracked.append((page.stat().st_mtime, page)))
    untracked.sort(reverse=True)
    tracked.sort(reverse=True)
    ordered = [("(uncommitted)", page) for _, page in untracked] + tracked
    today = date.today()
    stale = [(cs, page) for cs, page in tracked
             if (today - date.fromisoformat(cs)).days > 90]
    return ordered, stale


def _print_resume_flags(pairs, low, warnings):
    print("\n== FLAGS ==")
    if not (pairs or low or warnings):
        print("none")
    for a, b in pairs:
        print(f"contradicted: {a} <-> {b}")
    for page in low:
        print(f"confidence low: {page}")
    for warning in warnings:
        print(f"protocol: {warning}")


def _print_resume_checkpoint(sha: str, since: str, dirty: list[str]):
    print("\n== CHECKPOINT ==")
    if not since and not dirty:
        print(f"last: {sha[:8] if sha else '(none yet)'} · commits after: none · tree: clean")
        return
    print(f"last: {sha[:8] if sha else '(none yet)'}")
    print(f"commits after (NOT in STATE yet):\n{since}" if since else "commits after: none")
    print(f"working tree (uncommitted, {len(dirty)}):\n" + "\n".join(dirty[:5])
          + (f"\n… +{len(dirty) - 5} more" if len(dirty) > 5 else "")
          if dirty else "working tree: clean")


def _print_resume_stale(stale):
    print("\n== STALE (>90d, verify before trusting) ==")
    if not stale:
        print("none")
    for cs, page in stale:
        print(f"{cs}  {page.relative_to(WIKI).as_posix()}")


def resume_pack():
    """One-call /wiki-resume loader — everything deterministic in a single labeled pack.

    Replaces the 5-8 ad-hoc tool calls resume used to make (STATE read, 3 section calls,
    active, flags, 2 git calls, frontier loop). Pointer PAGES are deliberately NOT loaded:
    the pack only lists them with sizes — the session reads a page when work touches it.
    Read-only; prints and exits.
    """
    from datetime import date

    root = WIKI.parents[1]
    _git_repo_preflight(root)

    print(f"# resume pack — {root.name}")
    health_fail, health_warn, _health_ok = enforcement_health(root, full=False)
    if health_fail or health_warn:
        print("\n== ENFORCEMENT DEGRADED — READ ALLOWED, CHECKPOINT BLOCKED ==")
        for problem in health_fail + health_warn:
            print(f"- {problem}")
    integrity = (_history_integrity_violations(root)
                 + _worktree_wiki_type_violations(WIKI))
    if integrity:
        print("\n== INTEGRITY VIOLATION — WIKI NOT LOADED ==")
        for problem in dict.fromkeys(integrity):
            print(f"- {problem}")
        print("Refusing to compute active decisions from an untrusted wiki; run `structural.py guard`.")
        return

    state_path = root / ".memory" / "STATE.md"
    state = read_markdown(state_path) if state_path.exists() else ""

    # Compute anomaly-bearing data before the IDLE early return.  Idle is between tasks,
    # not outside history: post-checkpoint commits, dirt, flags and stale pages still matter.
    pairs, low, fwarn = flags(WIKI)
    sha, since, dirty = _resume_checkpoint_data(root)
    ordered, stale = _resume_durable_data(root)

    now = section(state, "Now") or ""
    if "No active task" in now:
        ln = last_done(LOG)
        print("\n== IDLE — no active task ==")
        print(ln if ln is not None else "no task-done yet — first task not closed")
        print("\n== STATE (.memory/STATE.md) ==")
        print(state.rstrip())
        _print_resume_flags(pairs, low, fwarn)
        _print_resume_checkpoint(sha, since, dirty)
        _print_resume_stale(stale)
        return

    print("\n== STATE (.memory/STATE.md) ==")
    print(state.rstrip())

    # daily routine tracker — the one block that is about TODAY, not about the last session
    print("\n== РУТИНА ==")
    for ln in tracker_report():
        print(ln)

    # what the skills THEMSELVES last wrote — outranks any STATE line claiming a run did or
    # did not happen (session 42: prose said "живых ранов не было" over two run files)
    runs = recent_runs(root)
    if runs:  # MM-DD this year, full date otherwise — a year-old run must not read as fresh
        yr = f"{date.today().year}-"
        print("последний ран: " + " · ".join(
            f"{k} {d[len(yr):] if d.startswith(yr) else d}" for d, k in runs))

    for title in ("State of the project", "Open threads"):
        block = section(read_markdown(WIKI / "overview.md"), title)
        print(f"\n== OVERVIEW: {title} ==")
        print(block.rstrip() if block else "(section missing)")

    # active decisions paired with their one-line index.md summaries, newest-first;
    # closed tracks collapse to one `## Decision groups` line each (pack budget, 0017)
    *_, active = extract()
    index_text = read_markdown(WIKI / "index.md")
    by_id, _index_problems = decision_index_records(index_text, _decision_inventory(WIKI))
    groups = parse_groups(index_text)
    lines = _collapse_decisions(active, by_id, groups)
    collapsed = len(active) - len(lines)
    note = "; closed tracks collapsed — full lines in index.md" if collapsed else ""
    print(f"\n== ACTIVE DECISIONS ({len(active)}, newest-first{note}) ==")
    for ln in lines:
        print(ln)

    _print_resume_flags(pairs, low, fwarn)
    _print_resume_checkpoint(sha, since, dirty)

    print("\n== CHANGE-FRONTIER (durable pages, newest-first, top 3) ==")
    for cs, page in ordered[:3]:
        print(f"{cs}  {page.relative_to(WIKI).as_posix()}")
    _print_resume_stale(stale)

    # pointers: listed with sizes only — deliberately NOT loaded (lazy-read on touch)
    slug_map = {_slug(f): f for f in WIKI.rglob("*.md")}
    ptr = section(state, "Pointers") or ""
    print("\n== POINTERS (NOT loaded — read on touch) ==")
    seen = set()
    found = False
    for m in WIKILINK.findall(_strip_code(ptr)):
        slug = m.split("#", 1)[0].strip().lstrip("?").lower()
        f = slug_map.get(slug)
        if not f or slug in seen:
            continue
        seen.add(slug)
        found = True
        lines = len(read_markdown(f).splitlines())
        print(f"{f.relative_to(WIKI).as_posix()} ({lines} lines)")
    if not found:
        print("(no [[links]] in STATE Pointers)")


def checkpoint_pack():
    """One-call /wiki-checkpoint write-side recon: RECON delta + COMPUTED fills ONLY.

    Deliberately SLIM (bench 2026-07-15): the harness requires a Read tool call on any
    file before Edit/Write, so shipping full STATE/overview/index/log here only duplicates
    content the session must Read anyway on the write path. What stays is what the model
    cannot get deterministically on its own: the checkpoint delta (cp, commits after,
    diff stat, dirty tree) and the drift-prone fills — next session number (from log.md,
    append-only = the reliable source), block (from STATE — ADVISORY, override if this
    session's block differs), next free decision id, log-line/commit-msg skeletons.
    Read-only; prints and exits. The model fills only <summary> and does the edits."""
    from datetime import date

    root = WIKI.parents[1]
    _git_repo_preflight(root)

    print(f"# checkpoint pack — {root.name}")

    sha = _git_text(root, "log", "-1", "--format=%H", "--", ".memory/STATE.md")
    print("\n== RECON (vs last checkpoint) ==")
    print(f"cp: {sha[:8] if sha else '(none yet — use the repo root commit)'}")
    since = _git_text(root, "log", f"{sha}..HEAD", "--oneline") if sha else ""
    print(f"commits after:\n{since}" if since else "commits after: none")
    stat = _git_text(root, "diff", "--stat", f"{sha}..HEAD") if sha else ""
    print(f"committed diff stat:\n{stat}" if stat else "committed diff stat: (empty)")
    dirty = _git_text(root, "status", "--porcelain")
    print(f"working tree (uncommitted):\n{dirty}" if dirty else "working tree: clean")
    adopt = [l[3:] for l in dirty.splitlines()
             if l.startswith("??") and not l[3:].strip('"').startswith((".memory/", ".claude/"))]
    if adopt:
        print("new-tracked candidates (untracked outside .memory/.claude — `git add -A` will "
              "adopt them; list them in the step-7 report as `new-tracked:`):")
        for p in adopt:
            print(f"  {p}")

    log_text = read_markdown(LOG) if LOG.exists() else ""
    nxt = (_max_session(log_text) or 0) + 1
    state_path = root / ".memory" / "STATE.md"
    state = read_markdown(state_path) if state_path.exists() else ""
    m = re.search(r"\*\*([^*\s][^*]*)\*\*", section(state, "Now") or "")
    block = m.group(1).strip() if m else "<block>"
    ids = [int(dm.group(1)) for dm in (DECISION_FILE.match(f.name)
           for f in sorted((WIKI / "decisions").glob("*.md"))) if dm]
    next_id = f"{(max(ids) + 1) if ids else 1:04d}"
    today = date.today().isoformat()
    print("\n== COMPUTED (session number is authoritative; block is from STATE Now — "
          "override it if this session worked another block) ==")
    print(f"session: {nxt}")
    print(f"block: {block}")
    print(f"next decision id: {next_id}")
    print(f"log line skeleton: {today} checkpoint {block} session {nxt} — <summary>")
    print(f"commit msg skeleton: checkpoint: {block} session {nxt} — <summary>")


def _integrity_cli(label, func):
    try:
        return func()
    except GitCommandError as e:
        print(f"{label}: REFUSED — Git integrity unavailable: {e}", file=sys.stderr)
        raise SystemExit(2)


def main():
    edges, warnings, superseded, active = extract()
    print(f"# structural pass — {WIKI}")
    print(f"\nactive decisions ({len(active)}): {', '.join(reversed(active))}")
    print(f"superseded: {', '.join(superseded) or '(none)'}")
    sup = [e for e in edges if e[1] == "supersedes"]
    links = sorted(e for e in edges if e[1] == "references")
    print(f"\nsupersedes edges ({len(sup)}):")
    for s, _, d in sup:
        print(f"  {s} -> {d}")
    print(f"\nreference edges ({len(links)}):")
    for s, _, d in links:
        print(f"  {s} -> {d}")
    wants = sorted(e for e in edges if e[1] == "wants")
    print(f"\nwanted pages — [[?slug]] TODOs ({len(wants)}):")
    for s, _, d in wants:
        print(f"  {s} -> ?{d}")
    if warnings:
        print(f"\nwarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")


class CliUsageError(Exception):
    """Invalid command-line invocation (EX_USAGE / exit status 2)."""


def _dispatch(argv):
    cmd = argv[1] if len(argv) > 1 else "report"
    no_operand = {
        "resume", "checkpoint", "active", "map", "lint", "pack-budget", "wanted",
        "flags", "last-done", "guard", "freshness", "state-reset", "tracker", "help",
        "selfcheck",
    }
    if cmd in no_operand and len(argv) != 2:
        raise CliUsageError(f"usage: structural.py {cmd}")
    if cmd == "report" and len(argv) not in (1, 2):
        raise CliUsageError("usage: structural.py [report]")

    if cmd == "resume":
        _integrity_cli("resume", resume_pack)
    elif cmd == "checkpoint":
        _integrity_cli("checkpoint", checkpoint_pack)
    elif cmd == "active":
        _print_active()
    elif cmd == "decision":
        if len(argv) != 3:
            raise CliUsageError("usage: structural.py decision <id or decision path>")
        try:
            print(json.dumps(decision_packet(argv[2]), ensure_ascii=False, indent=2))
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    elif cmd == "section":
        # `section <file> "<header>"` — print one "## " section through the next "## " or
        # EOF. 0-token slice so resume/checkpoint full-load but only consume named sections.
        if len(argv) != 4:
            raise CliUsageError('usage: structural.py section <file> "<header prefix>"')
        path = _resolve_page(argv[2])
        if path is None:
            raise CliUsageError(f"section: file not found: {argv[2]}")
        notice = decision_read_notice(path)
        if notice:
            print(notice)
            if notice.startswith("DECISION UNRESOLVED"):
                raise CliUsageError("resolve the decision graph before reading a rule")
        block = section(read_markdown(path), argv[3])
        print(block if block is not None
              else f'section: no "## {argv[3].lstrip("# ").strip()}*" header in {argv[2]}')
    elif cmd == "toc":
        # `toc <file>` — list the page's "## " headers (the cheap nav-aid TOC, ~0 tokens). Same
        # path resolver as `section`; prints exactly the headers `section` can slice, one per line.
        if len(argv) != 3:
            raise CliUsageError("usage: structural.py toc <file>")
        path = _resolve_page(argv[2])
        if path is None:
            raise CliUsageError(f"toc: file not found: {argv[2]}")
        heads = toc(read_markdown(path))
        print("\n".join(f"## {h}" for h in heads) if heads
              else f'toc: no "## " headers in {argv[2]}')
    elif cmd == "search":
        # full-text search over wiki bodies+titles (FTS5/bm25, snippet result). In-memory index
        # rebuilt per call -> always fresh, nothing on disk. Degrades to grep without FTS5.
        if len(argv) != 3:
            raise CliUsageError('usage: structural.py search "<terms>"')
        else:
            q = argv[2]
            try:
                results = _search_results(q)
                current_results = [row for row in results if row[2] != "superseded"]
                old_results = [row for row in results if row[2] == "superseded"]
                n = len(current_results)  # history never upgrades current-knowledge STATUS
                # STATUS grammar is parsed by consult_gate.py — keep strings in sync
                print(f"STATUS: hit n={n}" if n >= 2
                      else f"STATUS: {'weak n=1' if n else 'miss'} → map")
                for path, snip, status, replacement in current_results:
                    print(f"{path}: {_render_search_snippet(snip, status, replacement)}")
                if old_results:
                    print("-- SUPERSEDED HISTORY (not current) --")
                    for path, snip, status, replacement in old_results:
                        print(f"{path}: {_render_search_snippet(snip, status, replacement)}")
                if n < 2:
                    print(_MAP_HEADER)
                    for ln in wiki_map():
                        print(f"  {ln}")
            except sqlite3.OperationalError:
                print("search: FTS5 unavailable in this sqlite3 build — grep fallback:")
                fallback = _grep_fallback_results(q)
                current_fallback = [row for row in fallback if row[3] != "superseded"]
                old_fallback = [row for row in fallback if row[3] == "superseded"]
                n = len({path for path, _i, _line, _status, _replacement in current_fallback})
                print(f"STATUS: hit n={n}" if n >= 2
                      else f"STATUS: {'weak n=1' if n else 'miss'} → map")
                for path, i, line, status, replacement in current_fallback:
                    print(f"{path}:{i}: {_render_search_snippet(line, status, replacement)}")
                if old_fallback:
                    print("-- SUPERSEDED HISTORY (not current) --")
                    for path, i, line, status, replacement in old_fallback:
                        print(f"{path}:{i}: {_render_search_snippet(line, status, replacement)}")
                if n < 2:
                    print(_MAP_HEADER)
                    for ln in wiki_map():
                        print(f"  {ln}")
    elif cmd == "map":
        print(_MAP_HEADER)
        for ln in wiki_map():
            print(f"  {ln}")
    elif cmd == "lint":
        offenders = lint_index()
        if not offenders:
            print("lint: index.md Decisions prose all within budget (<=100 ch)")
        else:
            print(f"lint: {len(offenders)} index.md Decisions line(s) with prose > 100 chars:")
            for plen, ln in offenders:
                print(f"  ({plen} ch prose) {ln}")
    elif cmd == "pack-budget":
        # `pack-budget` — render the resume pack, show the per-section token split and the
        # verdict vs the ceiling (decision 0017). The trim-time diagnostic for the ladder.
        ptok, text = pack_size()
        sizes, cur = {}, "HEAD"
        for line in text.splitlines(keepends=True):
            if line.startswith("== ") and "==" in line[3:]:
                cur = line.strip().strip("= ").split("(")[0].strip()
            sizes[cur] = sizes.get(cur, 0) + len(line)
        for k, v in sorted(sizes.items(), key=lambda kv: -kv[1]):
            print(f"{round(v / PACK_CHARS_PER_TOK):>5} tok  {k}")
        over = ptok - PACK_BUDGET_TOK
        print(f"total ≈ {ptok} tok / budget {PACK_BUDGET_TOK} — "
              + (f"OVER by {over} (run the checkpoint ladder: compress, then evict)"
                 if over > 0 else "OK"))
        sys.exit(1 if over > 0 else 0)
    elif cmd == "compact":
        # `compact [--budget N]` — deterministic EVICT: fold oldest closed groups into the
        # archive line until the resume pack fits. Guard points here before any manual cut.
        b = PACK_BUDGET_TOK
        if len(argv) == 4 and argv[2] == "--budget":
            try:
                b = int(argv[3])
            except ValueError:
                raise CliUsageError("compact: --budget must be an integer") from None
        elif len(argv) != 2:
            raise CliUsageError("usage: structural.py compact [--budget N]")
        if b <= 0:
            raise CliUsageError("compact: --budget must be a positive integer")
        sys.exit(_integrity_cli("compact", lambda: compact(b)))
    elif cmd == "wanted":
        # `wanted` — the [[?slug]] backlog: pages deliberately referenced before they exist.
        e, *_ = extract()
        wants = sorted((s, d) for s, rel, d in e if rel == "wants")
        if not wants:
            print("wanted: none — no [[?slug]] markers in the wiki")
        for s, d in wants:
            print(f"{s} -> ?{d}")
    elif cmd == "flags":
        # `flags` — the contradiction/confidence protocol report (decision 0004). Resume
        # surfaces these so a marked dispute is never silently trusted next session.
        pairs, low, fwarn = flags()
        if not pairs and not low and not fwarn:
            print("flags: none — no contradicted / confidence:low pages")
        if pairs:
            print(f"contradicted pairs ({len(pairs)}):")
            for a, b in pairs:
                print(f"  {a} <-> {b}")
        if low:
            print(f"confidence: low ({len(low)}):")
            for p in low:
                print(f"  {p}")
        if fwarn:
            print(f"protocol warnings ({len(fwarn)}):")
            for x in fwarn:
                print(f"  {x}")
    elif cmd == "last-done":
        ln = last_done()
        print(ln if ln is not None else "no task-done yet — first task not closed")
    elif cmd == "guard":
        # `guard` — checkpoint-time integrity check (idea §2): baseline = last checkpoint
        # commit, compared against the working tree. Violations gate the checkpoint (exit 1);
        # warnings are advisory. Second line behind the wiki_guard.py PreToolUse hook.
        sha, violations, gwarn = _integrity_cli("guard", guard)
        print(f"guard: baseline {sha[:8] if sha else '(no checkpoint yet — empty baseline)'}")
        if gwarn:
            print(f"warnings ({len(gwarn)}) — advisory:")
            for x in gwarn:
                print(f"  {x}")
        if violations:
            print(f"VIOLATIONS ({len(violations)}) — fix before committing the checkpoint:")
            for x in violations:
                print(f"  {x}")
            sys.exit(1)
        print("guard: OK — decisions immutable, log append-only + well-formed, "
              "no [[wikilinks]] lost or newly dangling, changed durable units sourced, "
              "STATE canonical, index + overview + pack within budget")
    elif cmd == "doctor":
        # `doctor` — contour self-check (idea §8): hooks probed the way the harness runs
        # them (sh -c + benign stdin payload), referenced scripts exist, FTS5, fence,
        # decisions frontmatter. Config health only — content integrity is `guard`'s job.
        # Run after harness updates / on a new machine / when the memory feels off.
        if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--portable"):
            raise CliUsageError("usage: structural.py doctor [--portable]")
        fails, warns, oks = doctor(portable=len(argv) == 3)
        for x in oks:
            print(f"ok:   {x}")
        if warns:
            print(f"warnings ({len(warns)}) — advisory:")
            for x in warns:
                print(f"  {x}")
        if fails:
            print(f"FAILURES ({len(fails)}) — the contour is NOT fully alive:")
            for x in fails:
                print(f"  {x}")
            sys.exit(1)
        print("doctor: OK — hooks runnable, scripts present, fence intact, decisions valid")
    elif cmd == "freshness":
        # `freshness` — deterministic decay backstop on durable pages (session-7 tail pair):
        # missing local paths (concepts+rollups) + volatile current-state phrasing (concepts).
        # Advisory, recall-first; also folded into `guard` warnings so it fires every checkpoint.
        miss, vol = freshness_scan()
        if not miss and not vol:
            print("freshness: OK — no missing paths, no volatile status in durable pages")
        if miss:
            print(f"missing paths ({len(miss)}) — durable page cites a file/dir that is gone:")
            for pg, tok in miss:
                print(f"  {pg}: {tok}")
        if vol:
            print(f"volatile-in-durable ({len(vol)}) — belongs in STATE (fence), not a concept:")
            for pg, ph in vol:
                print(f"  {pg}: \"{ph}\"")
        sys.exit(1 if (miss or vol) else 0)
    elif cmd == "state-reset":
        # `state-reset` — periodic GC of STATE. Deliberately question-free: durable facts have
        # a home page (0017), so whatever accumulated in STATE is homeless by construction.
        try:
            for ln in state_reset():
                print(ln)
        except StateResetError as e:
            print(e)
            sys.exit(1)
    elif cmd == "tracker":
        # `tracker` — daily-routine habit report (also folded into the resume pack, which is
        # the only place Roma reads it: he always opens a session with /wiki-resume).
        for ln in tracker_report():
            print(ln)
    elif cmd == "tracker-log":
        # `tracker-log <item>=<done>/<target> [--date D] [--blocked R]` — the ONE deterministic
        # write. Called by /connects and /comments as their last step, always for TODAY;
        # --date is Roma's explicit ask only — a past ✗ is left alone (0027).
        usage = ("usage: structural.py tracker-log <item>=<done>/<target> "
                 "[--date YYYY-MM-DD] [--blocked <reason>]")
        if len(argv) < 3:
            raise CliUsageError(usage)
        options = {}
        pos = 3
        while pos < len(argv):
            name = argv[pos]
            if (name not in {"--date", "--blocked"} or name in options
                    or pos + 1 >= len(argv) or argv[pos + 1].startswith("--")):
                raise CliUsageError(usage)
            options[name] = argv[pos + 1]
            pos += 2
        day = options.get("--date")
        if day:
            try:
                date.fromisoformat(day)
            except ValueError:
                raise CliUsageError(f"tracker-log: invalid --date {day!r}; expected YYYY-MM-DD") from None
        print(tracker_log(argv[2], day=day, blocked=options.get("--blocked")))
    elif cmd == "help":
        # `help` — the on-demand subcommand catalog (T4: replaces the CLAUDE.md catalog section)
        print(__doc__.strip())
    elif cmd == "selfcheck":
        _demo()
    elif cmd == "report":
        main()
    else:
        raise CliUsageError(
            f"structural.py: unknown command {cmd!r} (run `structural.py help` for usage)")


def _run_cli(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        _dispatch(argv)
        return 0
    except CliUsageError as exc:
        print(exc, file=sys.stderr)
        return 2
    except MarkdownReadError as exc:
        print(f"structural.py: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        # Utility functions historically raise SystemExit("message") for invalid user input.
        # Normalize those usage failures to stderr + EX_USAGE without changing numeric exits.
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(_run_cli())
