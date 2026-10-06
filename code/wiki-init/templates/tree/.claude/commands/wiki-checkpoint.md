---
description: End-of-session (or pre-risky-step) checkpoint — split session work into durable wiki knowledge vs volatile STATE, then commit.
---

You are running `wiki-checkpoint`. Goal: capture the current session so a fresh
session can resume with zero re-explanation.

CODEX BACKGROUND MODE: when native Codex app fork tools are available and this
checkout has an installed watchdog, read
`.memory/checkpoint-background.md` and follow its parent/worker routing. Background
fork with ordinary drafting is the default unless the user asks for foreground.
Without an installed watchdog, use foreground checkpointing and say so; setup is
explicit through wiki-init/wiki-update --watchdog. An installed but unhealthy
watchdog is a hard stop for background mode, not a reason to bypass supervision.
The parent stays in this conversation. Workers use their actual native worktree and
frozen input boundary, never recursively fork. The procedure below remains the
ordinary drafting/validation procedure; background mode supplies frozen inputs and
adds attach/ready plus coordinated publication. Other runtimes remain foreground.
Background preparation requires a healthy installed watchdog. Report pending
supervision notices before dispatch; do not suppress a health failure or silently
fall back to an unsupervised fork. The watchdog only observes and notifies.

TURN ECONOMY: the checkpoint runs on the session's fattest context — batch independent
read-only calls into ONE message; never one call per turn.
WRITE-BATCH: the inventory (step 3) fixes the complete edit list before any write.
Batch independent searches and reads, then fill one reusable checkpoint package.
Correct failed entries in that package; do not regenerate unaffected content.

Steps:
1. Prepare context — run `python .memory/tools/checkpoint_collect.py --root .` — ONE
   read-only call. The packet includes the existing authoritative checkpoint recon,
   transaction status, active decision summaries, flags, project instructions,
   STATE/overview/index, session commits, committed/staged/unstaged diffs, untracked
   inputs and file versions. In the recon item, `session` is AUTHORITATIVE (never
   recompute it); `block` is ADVISORY (replace if this session worked another block);
   `next decision id` numbers any new decision file.
   Check `consistent`, `drift`, `omissions` and item `truncated` fields. If sources
   changed, refresh collection before relying on it. An active transaction remains
   a hard stop; inspect/recover it through the existing coordinator. Retrieve needed
   omitted content, including anything hidden by tool-output truncation. The packet
   does not interpret the conversation, search candidate pages or decide retention;
   those belong to the selected inventory backend below. Full edit-target Reads remain
   required before edits.
   (Older installations without the collector: run
   `python .memory/tools/structural.py checkpoint` and gather the remaining context
   with read-only commands.)
2. Combine that with this conversation = the session's work. Read
   `.memory/inventory.json` if present to select the inventory backend.
3. INVENTORY, then SPLIT:
   - When backend is `jev-combined`, read `.memory/checkpoint-inventory.md` and run
     its source-extraction and Jev procedure. Scripts + Jev own semantic steps 1–4:
     inventory, evidence/applicability, retention, and prior-memory comparison. Use
     the accepted combined prompts and source attachments in `checkpoint_inventory.py`.
     Consume only a completed manifest. Missing inputs, credentials, or failed calls
     stop this stage; do not silently fall back to main-agent reclassification.
     The main agent starts at semantic step 5: choose edits; step 6: draft changes;
     step 7: review the draft. Resolve specific edit-relevant uncertainty, but do NOT
     repeat the entire inventory after Jev. Derive dropped-item accounting from output.
   - When unconfigured, use the legacy agent inventory: enumerate the session's
     facts/events/decisions, including meaningful asides, with `wiki`, `STATE`, or
     `drop — <one-clause reason>`. Unknown configured backends are errors, not fallback.
   Writing starts after the inventory and target reads. Both paths preserve source
   references, uncertainty and explicit dropped-item accounting for the final report.

   REUSABLE PACKAGE — after the inventory and target Reads, read
   `.memory/checkpoint-package.md` and run:
   `python .memory/tools/checkpoint_package.py init --name session-<N> --block "<block>" --summary "<summary>" --allow <paths...>`
   Use the authoritative session number and exact intended wiki/code/config/source paths.
   STATE and index are included automatically; do not list or edit log.md. Choose a
   nonempty single-line block and summary now: block spaces become hyphens, and the
   generated log line must fit 200 characters. This validates metadata before writes.
   The command prints a persistent JSON draft path in Git metadata. Read it and fill
   its operations in one batch: write STATE/new files, edit existing pages, keep
   already-written sources. One entry per file, combining that file's edits. Preserve
   the captured baseline and hashes. Nothing protected is written at this stage.

   SPLIT the work by those verdicts:
   - DURABLE (true in 3 sessions regardless of where I am — decisions, how-it-works,
     why-X, constraints) → `.memory/wiki/`:

     ANTI-DUP first: `python .memory/tools/structural.py search "<topic terms>"`; a hit
     that already covers the topic → Edit that page in place (a decision → supersede
     forward), never a duplicate page. No covering hit → GRANULARITY.

     GRANULARITY (page vs fold): a new SELF-STANDING mechanic/entity — its own name +
     own procedure/parameters + a position that evolves across sessions — gets its OWN
     `concepts/<slug>.md` (lowercase slug = the [[wikilink]] target), even when a broader
     block/channel page could absorb it; the rollup and STATE link it as [[slug]]. A new
     fact ABOUT an existing topic = Edit that owner page, never a fork. Neither: a
     choice → decision file, a position → STATE. Torn? Its own runnable procedure →
     page; otherwise fold.

     DEPTH (how much detail): a decision body records question + verdict + WHY +
     the rejected alternatives; a mechanic/procedure must be re-runnable WITHOUT
     this session (exact commands, parameters, paths); a bare fact keeps its
     source. Too thin = a page that forces git archaeology in 3 sessions. Detail
     lives on the owner page (resume never loads bodies — depth is pack-free).

     PROVENANCE FLOOR: every new/changed factual H2 unit needs a parseable
     `[source: .memory/research/report.md; commit abcdef0; https://...]` marker in that unit. A changed
     overview/index unit may instead resolve a plain owner-page link. Unchanged legacy units are
     grandfathered; `[[?todo]]`, missing/unstaged local files, absolute session paths, and markers
     inside code examples do not satisfy the floor.

     Page-type policy: concept pages are LIVING (edit in place; every claim keeps its
     source); a NEW decision = `decisions/<id>-<slug>.md` with `id`/`date` frontmatter;
     revising one = a NEW decision with `supersedes: <old-id>` (never edit the old file);
     `log.md` = append only. Update index.md: one-liners ≤100 chars; decision lines =
     `«закрытый вопрос?» → вердикт` (decision 0015), concept lines WHAT-only.

     Edit targets: Read the page, then Edit — `toc`/`section` only for pages consulted
     WITHOUT editing. EDIT, don't rewrite (a full-file Write only for STATE, which is
     rewritten by design). overview.md: Read once, make ALL overview edits from that Read.

     overview "State of the project" = fixed micro-schema of bullets, ONE line ≤400 chars
     each (`Цель / Движки / Конверсия / Текущий блок — <блок> / Закрыто — <тема>`). Edit
     only the bullets this session changed; CURATE, do NOT append — no date-stamps, no
     session diary; DELETE text the new facts obsolete (history lives in git + log.md).

     overview "Open threads" = an INDEX: each entry ONE line `- [[owner-page]] — hook`
     (≤200 chars). The fact lives on the owner page; an open item with NO owner page is
     volatile → STATE only. The CURRENT block is not an open thread. Prune resolved entries.
   - VOLATILE (my position — in-flight, tried-rejected, next, open questions) → Read
     `.memory/STATE.md` FIRST, then rewrite it WHOLE. One fact — one home: STATE links a
     durable fact as `[[slug]]` + the volatile hook, never a retelling of the page.
     CANONICAL sections only (parsed by tools — never rename, add none, omit empty ones):
     `## Now`, `## Next`, `## Tried & rejected`, `## Open`, `## Pointers`.
4. Use the selected backend's verdicts for the durable/volatile split; choose edits,
   draft and review proposed changes without asking for routine confirmation. With
   Jev selected, do not repeat its complete classification work. Chatter is not an
   inventory item. The final report + `git diff` are review-after-the-fact; git reverts
   any mistake.
5. Do NOT edit `log.md` yourself. The package contains the chosen outcome summary and
   block; the coordinator computes the next session and appends exactly one line.
6. PREFLIGHT + TAIL — run:
   `python .memory/tools/checkpoint_package.py check --name session-<N>`
   then `python .memory/tools/checkpoint_package.py apply --name session-<N>`.
   The helper checks format, unique file operations, edit anchors and baseline/file
   freshness before writes, and checks again after BEGIN. It invokes the existing
   coordinator for compact, allowlisted private-index staging, structural guard,
   pinned pre-commit, log append and commit. Do not run a separate BEGIN or TAIL.
   - Preflight failure writes no target files. Correct the offending package entry.
   - Validation/hook/commit failure restores protected writes. The package survives:
     correct the cause and reapply it, reusing all unaffected edits. Do not manually
     salvage a log append or automatically retry the same failure.
   - Changed HEAD/file hashes require reviewing a fresh baseline; never simply refresh
     hashes to force the old edits through. An active transaction is a hard stop:
     inspect `checkpoint_txn.py status`; use `recover` after an interruption before
     retrying. A landed package cannot be applied twice.
   - Never use `git add -A` or a shared-index checkpoint commit.

7. Report — a short human digest in plain English, NO slugs/ids/wiki-jargon
   (the reader is the project owner):
   ```
   checkpoint ready · <short-hash> · <block> · session N

   recorded in memory:
   - <one line per topic: what was recorded, in plain English>

   dropped:
   - <fact> — <reason>          (empty → “- nothing”)
   ```
   "recorded in memory" = one line per wiki target (decision or page): what entered it, in
   plain words. STATE updates are NOT listed (the next resume shows them anyway).
   "dropped" is MANDATORY: every consciously dropped fact from the step-3
   inventory + a one-clause reason; “nothing” when empty (distinguishes "nothing
   to drop" from "didn't track"). Forgotten ≠ dropped — only conscious verdicts
   land here.
   Extra lines ONLY for anomalies (each omitted when clean): guard violations
   fixed / new-tracked paths adopted / open items needing the user.

Rules: `.memory/STATE.md` is the ONLY sourceless page. Keep it ≤ ~40 lines — permanent
facts graduate to a wiki page.
