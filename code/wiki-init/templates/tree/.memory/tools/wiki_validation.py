#!/usr/bin/env python3
"""Pure baseline→candidate validators shared by guard and pre-commit."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

FENCED = re.compile(r"```.*?```", re.S)
INLINE = re.compile(r"`[^`]*`")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
VALIDATION_FLOOR_MARKER = "WIKI_VALIDATION_FLOOR_V1"

def strip_code(text: str) -> str:
    return INLINE.sub("", FENCED.sub("", text))


def plain_link_targets(text: str) -> set[str]:
    """Plain links only. `[[?x]]` is an intentional TODO and never promises a target."""
    out = set()
    for match in WIKILINK.findall(strip_code(text)):
        raw = match.strip()
        if raw.startswith("?"):
            continue
        target = raw.split("#", 1)[0].strip().lower()
        if target:
            out.add(target)
    return out


def _available_slugs(pages: dict[str, str]) -> dict[str, list[str]]:
    by_slug: dict[str, list[str]] = {}
    for path in pages:
        if not path.endswith(".md"):
            continue
        by_slug.setdefault(PurePosixPath(path).stem.lower(), []).append(path)
    return by_slug


def duplicate_slug_violations(pages: dict[str, str]) -> list[str]:
    return [f"duplicate wiki slug `{slug}` is ambiguous: {', '.join(sorted(paths))}"
            for slug, paths in sorted(_available_slugs(pages).items()) if len(paths) > 1]


def dangling_plain_link_counts(pages: dict[str, str]) -> Counter[tuple[str, str]]:
    available = {slug for slug, paths in _available_slugs(pages).items() if len(paths) == 1}
    counts: Counter[tuple[str, str]] = Counter()
    for path, text in pages.items():
        if not path.endswith(".md"):
            continue
        # Count occurrences, not just targets: adding another broken promise is new damage even
        # when one legacy occurrence of the same target is grandfathered.
        for match in WIKILINK.findall(strip_code(text)):
            raw = match.strip()
            if raw.startswith("?"):
                continue
            target = raw.split("#", 1)[0].strip().lower()
            if target and target not in available:
                counts[(path, target)] += 1
    return counts


def dangling_plain_links(pages: dict[str, str]) -> set[tuple[str, str]]:
    return set(dangling_plain_link_counts(pages))


def new_dangling_link_violations(baseline: dict[str, str],
                                 candidate: dict[str, str]) -> list[str]:
    """Block each dangling promise introduced by this transition; legacy counts are preserved."""
    introduced = dangling_plain_link_counts(candidate) - dangling_plain_link_counts(baseline)
    probs = []
    for (path, target), count in sorted(introduced.items()):
        suffix = f" ({count} new occurrences)" if count > 1 else ""
        probs.append(f"{path}: new plain [[{target}]]{suffix} points to no candidate wiki page; "
                     f"create it or mark the intentional TODO as [[?{target}]]")
    probs.extend(duplicate_slug_violations(candidate))
    return probs


SOURCE_MARKER = re.compile(r"\[source:\s*(.*?)\]", re.I | re.S)
H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
HTTP = re.compile(r"https?://[^\s`;)\]]+")
BACKTICK = re.compile(r"`([^`]+)`")
COMMIT_REF = re.compile(r"\bcommit\s+`?([0-9a-fA-F]{7,40})`?\b")
REPO_PATH = re.compile(
    r"(?<![\w./])((?:\.memory/|\.claude/|\.agents/|\.prime/|code/)[\w./{}\-]+|"
    r"(?:AGENTS|CLAUDE|README)\.md)(?=$|[\s`,;:)#])")


def _units(text: str) -> dict[str, str]:
    """Stable preamble/H2 units; duplicate headings are disambiguated by occurrence."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    out: dict[str, str] = {}
    heading = "<preamble>"
    count: dict[str, int] = {}
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        n = count.get(heading, 0) + 1
        count[heading] = n
        out[f"{heading}#{n}"] = "".join(buf)
        buf = []

    for line in lines:
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
        buf.append(line)
    flush()
    return out


def _has_claim(text: str) -> bool:
    body = re.sub(r"\A---\n.*?\n---\n?", "", text, flags=re.S)
    body = SOURCE_MARKER.sub("", body)
    body = strip_code(body)
    body = re.sub(r"^#{1,6}\s+.*$", "", body, flags=re.M)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return bool(re.search(r"[\wА-Яа-я]", body))


def _unit_owner_resolves(text: str, candidate: dict[str, str]) -> bool:
    available = {slug for slug, paths in _available_slugs(candidate).items() if len(paths) == 1}
    return bool(plain_link_targets(text) & available)


def _valid_source(marker: str, source_exists, commit_exists) -> bool:
    if any(url.startswith(("http://", "https://")) and "." in url.split("/", 3)[2]
           for url in HTTP.findall(marker)):
        return True
    for sha in COMMIT_REF.findall(marker):
        if commit_exists(sha):
            return True
    candidates = set(BACKTICK.findall(marker)) | set(REPO_PATH.findall(marker))
    for raw in candidates:
        path = raw.split("::", 1)[0].split("#", 1)[0].rstrip(".,;:")
        if path == ".memory/STATE.md":
            continue
        if path.startswith((".memory/", ".claude/", ".agents/", ".prime/", "code/")) \
                or path in {"AGENTS.md", "CLAUDE.md", "README.md"}:
            if source_exists(path):
                return True
    return False


def provenance_violations(baseline: dict[str, str], candidate: dict[str, str],
                          source_exists, commit_exists) -> list[str]:
    """Minimum evidence floor for new/changed durable Markdown units.

    A source marker proves only that an inspectable basis exists, never that the prose is true.
    Unchanged legacy units are grandfathered. Derived rollup units may cite a resolved owner page.
    """
    probs = []
    if "task-ledger.md" in baseline or "task-ledger.md" in candidate:
        from task_memory import validate_transition
        probs += validate_transition(baseline.get("task-ledger.md", ""),
                                     candidate.get("task-ledger.md", ""), source_exists)
    for path, text in sorted(candidate.items()):
        if path == "log.md" or not path.endswith(".md"):
            continue
        old_units = _units(baseline.get(path, ""))
        for key, unit in _units(text).items():
            if unit == old_units.get(key) or not _has_claim(unit):
                continue
            if path in {"overview.md", "index.md"} and _unit_owner_resolves(unit, candidate):
                continue
            markers = SOURCE_MARKER.findall(FENCED.sub("", unit))
            label = key.rsplit("#", 1)[0]
            if not markers:
                probs.append(f"{path} [{label}]: new/changed durable claims need a [source: ...] marker")
            elif not any(_valid_source(marker, source_exists, commit_exists)
                         for marker in markers):
                probs.append(f"{path} [{label}]: source marker has no reachable candidate source")
    return probs


def selfcheck() -> None:
    def expect(condition, message):
        if not condition:
            raise RuntimeError(message)

    base = {"concepts/a.md": "# A\n\n## Facts\nlegacy [[old-missing]]\n"}
    candidate = {**base, "concepts/b.md": "# B\n\n## Facts\nnew [[missing]]\n"}
    probs = new_dangling_link_violations(base, candidate)
    expect(any("[[missing]]" in p for p in probs), "new dangling plain link must block")
    duplicate_base = {"concepts/a.md": "[[missing]]\n"}
    duplicate_new = {"concepts/a.md": "[[missing]]\n[[missing]]\n"}
    expect(new_dangling_link_violations(duplicate_base, duplicate_new),
           "a second occurrence of legacy dangling target is new damage")
    expect(new_dangling_link_violations({}, {"concepts/a.md": "[[owner]]\n"}),
           "plain meta-looking slugs are not exempt from the iron rule")
    candidate["concepts/b.md"] = "# B\n\n## Facts\nnew [[?missing]]\n"
    expect(not new_dangling_link_violations(base, candidate), "wanted marker must pass")
    candidate["concepts/b.md"] = "# B\n\n## Facts\nnew [[target]]\n"
    candidate["concepts/target.md"] = "# Target\n"
    expect(not new_dangling_link_violations(base, candidate), "same-candidate target must pass")
    base2 = {"concepts/a.md": "[[target]]\n", "concepts/target.md": "# Target\n"}
    expect(new_dangling_link_violations(base2, {"concepts/a.md": "[[target]]\n"}),
           "deleting a target must create a dangling violation")

    source_exists = lambda p: p == "code/evidence.txt"
    commit_exists = lambda sha: sha == "abcdef0"
    legacy = {"concepts/a.md": "# A\n\n## Facts\nlegacy unsourced\n"}
    expect(not provenance_violations(legacy, dict(legacy), source_exists, commit_exists),
           "unchanged legacy unit must be grandfathered")
    changed = {"concepts/a.md": "# A\n\n## Facts\nchanged unsourced\n"}
    expect(provenance_violations(legacy, changed, source_exists, commit_exists),
           "changed unsourced unit must block")
    fenced = {"concepts/a.md": changed["concepts/a.md"]
              + "```text\n[source: `code/evidence.txt`]\n```\n"}
    expect(provenance_violations(legacy, fenced, source_exists, commit_exists),
           "source marker inside a code fence must not count")
    changed["concepts/a.md"] += "[source: `code/evidence.txt`]\n"
    expect(not provenance_violations(legacy, changed, source_exists, commit_exists),
           "reachable staged local source must pass")
    expect(_valid_source("`.prime/agent/extensions/wiki-memory.js`",
                         lambda p: p == ".prime/agent/extensions/wiki-memory.js",
                         commit_exists),
           "Prime project runtime file must be accepted as provenance")
    derived = {"overview.md": "# O\n\n## State\n- [[a]] — current\n",
               "concepts/a.md": "# A\n"}
    expect(not provenance_violations({}, derived, source_exists, commit_exists),
           "derived unit with a resolved owner must pass")
    print("selfcheck OK")


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        selfcheck()
