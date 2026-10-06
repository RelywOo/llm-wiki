---
description: Start-of-session resume — load project context and briefly state where the work stopped. Read-only.
---

You are running `wiki-resume`. Goal: get fully in context for this project without
the user re-explaining anything. Write nothing — this is read-only. Do not modify any file.

Steps:
0. Run `python .memory/tools/checkpoint_watchdog.py prompt` if that helper exists.
   This is read-only. Surface pending checkpoint notices or unhealthy supervision
   alongside the resume summary, including when STATE is idle. Never infer that a
   checkpoint succeeded from an absent worker message. Do not acknowledge notices
   during this read-only resume; reporting them does not authorize recovery writes.
1. Run `python .memory/tools/structural.py resume` — ONE call, the whole loader. It prints
   a labeled pack: STATE.md, overview "State of the project" + "Open threads", active
   decisions paired with summaries (newest-first), flags, checkpoint + commits after it,
   change-frontier, stale pages, and the Pointers list. Everything in the pack is now your
   working context — it BINDS you silently (active decisions, tried-rejected, flags —
   never relitigate or re-ask them).
   FALLBACK if the `resume` subcommand is absent (older clone): assemble the same pack by
   hand — Read `.memory/STATE.md`; `structural.py section/active/flags` (see
   `structural.py help`); `git log -1 --format=%H -- .memory/STATE.md` + commits after it.
2. IDLE branch: if the pack prints `== IDLE — no active task ==`, report "No active task.
   We last finished <the task-done summary>." Omit the second sentence if no completed
   task is recorded. Do not invent an unfinished step or ask what to start. Stop.
3. Pointer pages are listed but NOT loaded — deliberate. Do not read them during resume;
   read a pointer page at the moment work actually touches its topic.
4. Report only the point where the work stopped, in one short paragraph of one to three
   sentences. Start naturally with "We left off..." and name the specific unfinished
   step, discussion, or decision. Use STATE's Now and Next together to locate that point;
   a proposed next step is not evidence that work on it has already begun.
   Include only the context needed to recognize the stopping point and, when relevant,
   what remained undecided or blocked. If the stopping point is unclear or contradicted,
   say so briefly instead of guessing.

   Example:
   > We left off choosing a topic for your NASA Space Apps preparation: precipitation,
   > land-surface temperature, or vegetation greenness. You hadn't picked one yet.

   Use plain language and follow the conversation's language instructions. No report
   card, headings, code fence, completed-work list, next-step plan, session numbers,
   counts, or routine diagnostics. Keep the loaded context available for continuing
   work; mention a warning only if it materially changes the stopping point or makes
   it unreliable. Do not append a follow-up question or an offer to show more detail.

On “details” / “full report” (any later turn): render the classic full report from the
ALREADY-LOADED pack, zero re-reads — block+session goal; Now + Next; ALL active decisions
as `id — summary` newest-first; change-frontier pages with dates; done / in flight /
tried & rejected / open questions; every flag and post-checkpoint commit.
