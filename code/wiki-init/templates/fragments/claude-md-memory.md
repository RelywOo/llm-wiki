# Language
- Always communicate with me only in English, everywhere and every time. This applies to all explanations, reports, questions, and other messages, as well as code, commit messages, and PR descriptions.

# Session Memory (wiki + STATE)

> Project: <project>. Durable cross-session memory, installed by /wiki-init.

## Two memory layers
- `.memory/wiki/` — DURABLE knowledge. Provenance-clean. Grows by page-type policy (below). Obeys the iron rules.
- `.memory/STATE.md` — VOLATILE "where I am now". Sourceless allowed. Rewritten each checkpoint.

## Raw project inputs (under .memory/, outside the wiki fence)
Everything about this project lives under `.memory/` so a single folder feeds the **memory system**. Besides `wiki/` and `STATE.md`:
- `.memory/brainstorms/` — grill-me capture files (the grill-me skill routes here automatically when `.memory/` exists).
- `.memory/specs/` — design docs, named `YYYY-MM-DD-<topic>-design.md`.
- `.memory/research/` — research reports and deep-dives.
These are RAW inputs: NOT bound by the iron rules / provenance (only `.memory/wiki/` is). Folders self-create on first use.

## Code layout
ALL project source code lives under `code/` (internal structure is free — organize as convenient).
Exceptions that stay where they are:
- `.memory/tools/` — memory-system scripts (part of the memory system, not project code).
- `.claude/` — Claude harness config and hooks.
- `.prime/` — Prime Agent project settings and native extensions.
Repo-level manifests/configs (`package.json`, `pyproject.toml`, `.gitignore`, `README.md`) stay at the repo root — they are not code.

## Page-type policy (how each durable page changes)
History is git's job — every old version is recoverable from commits. So append-only is kept ONLY where it also guards audit + blocks silent LLM overwrite:
- `log.md` — STRICTLY append-only. One line per event, never edited.
- `decisions/*.md` — IMMUTABLE bodies + forward supersession. Never edit a filed decision. To revise one, CREATE a new decision whose frontmatter has `supersedes: <old-id>`; leave the old file untouched. "Active" is COMPUTED, not stored: a decision is superseded iff another decision lists its `id` in `supersedes`.
- concept pages — LIVING. A new page = `concepts/<slug>.md` (lowercase slug = its `[[wikilink]]` target). Edit/refine in place; every claim keeps its source; git holds the history.
- `overview.md`, `index.md` — DERIVED rollups. Edit freely (already rewritten each checkpoint); every claim still traces to a source (iron rule 1).

Decision frontmatter:
    ---
    id: 0001            # zero-padded, matches filename 0001-<slug>.md
    date: YYYY-MM-DD     # created
    supersedes: 0000     # OPTIONAL — id this decision replaces; omit if net-new
    ---
To supersede several decisions at once, file one supersession decision per replaced id (each with its own `supersedes:`) — stay forward-only.

## Freshness (decay) — a stale-flag, never deletion
Freshness is COMPUTED from git, never stored: last-touch = `git log -1 --format=%cs -- <file>`. At resume, a concept page or active decision untouched for >90 days gets flagged "⚠ stale (verify)" — never auto-deleted, never re-ranked. Skip derived pages (`overview.md`, `index.md`, `log.md`).

## Structural pass (non-LLM, 0 tokens) — `.memory/tools/structural.py`
Deterministic regex/FTS pass over `.memory/wiki/` — prefer it over ad-hoc grep and full-page reads for structural AND content questions. Subcommands: `resume`, `active`, `section`, `toc`, `search`, `lint`, `pack-budget`, `wanted`, `flags`, `last-done`, `guard`, `doctor`, `freshness`, `help`, `selfcheck`.
One-line docs per subcommand on demand: `python .memory/tools/structural.py help`.
Most used: `search "<terms>"` for content lookups (stemmed FTS5 top-5 snippets — finds word forms grep misses); `section <file> "<## header>"` to load ONE section instead of a full page (`toc <file>` lists the headers); `active` for the active decision set.

## Wiki write-guard
A PreToolUse hook (`.memory/tools/wiki_guard.py`) DENIES edits to filed (committed) `decisions/*.md` and any non-append change to `log.md`; `structural.py guard` re-checks at checkpoint (catches Bash/sed edits the hook can't see). Denied? Don't fight the hook: file a new decision with `supersedes:`, or append to the log instead.

## Anti-compaction contour
Compaction is NOT a working mode — the cycle is `/wiki-checkpoint → clear → /wiki-resume`; a deliberate memory write beats a lossy compact summary. Hooks in `.memory/tools/` insure the UNPLANNED auto-compact (post-compact anchor, pre-compact transcript archive). After a compact: run `/wiki-checkpoint` at the first natural pause.

## Commands (run manually at session boundaries)
- `/wiki-resume` — start of session: load STATE + meanings, report where we are. Read-only.
- `/wiki-checkpoint` — end of session (or before a risky step): split work durable→wiki /
  volatile→STATE, then commit.
- `/wiki-clear` — garbage-collect STATE: wipe it to idle, no questions, nothing kept.
  Durable capture is the checkpoint's job; git holds the discarded STATE.

## Fence rule
`.memory/STATE.md` is the ONLY page allowed sourceless volatile claims. Everything in
`.memory/wiki/` must trace to a source. Durable facts graduate from STATE → a wiki page;
STATE then only links to them. Keep STATE.md ≤ ~40 lines.

**Reference, don't copy (freshness).** A durable page (concept/rollup) REFERENCES a mutable
fact, never restates its current VALUE: link the code / decision / STATE instead of copying a
count, config value, path, or status that will move. Volatile current-state — build status,
"placeholder", "photo not provided yet" — belongs in STATE (it self-heals each checkpoint),
NOT a living concept (it rots there unfixed). If a value must appear inline, anchor it with
provenance (as-of / source pointer). `structural.py freshness` catches the path + volatile-
status slice deterministically at checkpoint (also folded into `guard`); the impl-detail slice
(a bare count/config value) has no regex — that one is on you at write time.

## Session boundary = checkpoint
A checkpoint is the single commit that rewrites STATE.md (message prefixed `checkpoint:`).
Mid-session commits are normal and do NOT touch STATE.md.
Last checkpoint = `git log -1 --format=%H -- .memory/STATE.md`.

## Iron rules
1. Every claim in `.memory/wiki/` traces to a source.
2. Verify a page exists before writing [[Link]]. A page that is needed but not written yet is referenced as `[[?slug]]` — the deliberate-TODO marker (decision 0003); a plain `[[link]]` to a missing page stays an error. `structural.py wanted` lists the `?`-backlog; when the page gets written, drop the `?`.
3. Surface contradictions explicitly — via the protocol (decision 0004): BOTH living pages get `contradicted: <other-slug>` frontmatter + a `> [!warning] CONTRADICTS [[other]]: <what conflicts>` callout up top. A page with shaky facts gets `confidence: low` frontmatter + a callout naming the gap. Decisions stay immutable — a contradicted decision is superseded, not marked. `structural.py flags` reports both markers; resume surfaces them.
4. If asked something not in the wiki, say "not in wiki".
5. Decisions are never edited — supersede forward (new file with `supersedes: <id>`); the active set is computed from frontmatter, not stored.
