"""Memory Markdown formats and candidate shape checks, independent of the CLI."""
from pathlib import Path
import re

WIKI = Path(__file__).resolve().parents[2] / '.memory/wiki'

FENCED = re.compile(r"```.*?```", re.S)


INLINE = re.compile(r"`[^`]*`")


ID_PREFIX = re.compile(r"^(\d+)")


DECISION_INDEX_LINK = re.compile(
    r"^\s*-\s*\[(\d{4})\]\((decisions/((\d{4})-[^)]+\.md))\)\s+(?:-|—)\s+(\S.*)\s*$")


DECISION_INDEX_LEGACY = re.compile(
    r"^\s*-\s*((\d{4})-[^\s—]+)\s+(?:-|—)\s+(\S.*)\s*$")


def _strip_code(text: str) -> str:
    # kill fenced then inline code so doc-text like `[[wikilinks]]` never matches
    return INLINE.sub("", FENCED.sub("", text))


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


STATE_CANON_HEADERS = ("Now", "Next", "Tried & rejected", "Open", "Pointers")


STATE_ROUTINE = re.compile(r"connect\w*|коннект\w*|comment\w*|коммент\w*|\bpost\w*|пост\w*", re.I)


STATE_ROUTINE_TELL = re.compile(r"\d+\s*/\s*\d+")


def _check_state(state_text: str, *, routine: bool = False) -> list[str]:
    """PURE. STATE.md schema: only canonical `## ` sections (resume parses them), <=40 lines,
    with optional legacy routine checks for projects using tracker.md. A parenthetical
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
    if routine:
        for line in _strip_code(state_text).splitlines():
            if STATE_ROUTINE.search(line) and (m := STATE_ROUTINE_TELL.search(line)):
                probs.append(f'STATE.md: routine retold in prose ("{m.group(0).strip()}" in '
                             f'"{line.strip()[:60]}") — drop it; tracker.md is the only home '
                             f"(resume reads it from there, so a retelling can only drift)")
    return probs


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


def check_candidate(root: Path, outputs: dict[str, bytes]) -> list[str]:
    """Validate rendered STATE/index/overview and proposed decision names together.

    Transaction baseline, staged-index, freshness and source semantics remain the
    caller's responsibility. This function performs no writes.
    """
    STATE, INDEX = '.memory/STATE.md', '.memory/wiki/index.md'
    errors = _check_state(outputs[STATE].decode('utf-8'),
                                     routine=(root / '.memory/tracker.md').is_file())
    headings = toc(outputs[STATE].decode('utf-8'))
    if len(headings) != len(set(headings)):
        errors.append('STATE contains duplicate sections')
    index = outputs[INDEX].decode('utf-8')
    inventory = _decision_inventory(root / '.memory/wiki')
    for relative in outputs:
        if relative.startswith('.memory/wiki/decisions/'):
            match = DECISION_FILE.fullmatch(Path(relative).name)
            if not match:
                errors.append('invalid decision filename: ' + relative)
            else:
                did = match.group(1)
                target_rel = relative.removeprefix('.memory/wiki/')
                if did in inventory and inventory[did] != target_rel:
                    errors.append('duplicate decision id: ' + did)
                inventory[did] = target_rel
    _, problems = decision_index_records(index, inventory)
    errors.extend(problems)
    errors.extend('index summary exceeds 100 characters' for line_text in index.splitlines()
                  if line_text.startswith('- ') and len(_prose_of(line_text)) > 100)
    overview = outputs.get('.memory/wiki/overview.md')
    if overview is not None:
        errors.extend(_check_overview(overview.decode('utf-8')))
    return errors
