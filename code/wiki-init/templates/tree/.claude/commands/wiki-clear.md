---
description: Wipe STATE.md to idle — periodic garbage collection of volatile session memory. Asks nothing, keeps nothing.
---

You are running `wiki-clear`. Goal: throw away everything that accumulated in
`.memory/STATE.md` and leave it idle. This is garbage collection, not a task closure.

Run `python .memory/tools/structural.py state-reset` and report its output. That is the
whole command — the wipe itself is deterministic, you only relay the result.

Rules:
- Ask NOTHING. No "should I keep X?", no confirmation, no list of what is about to die.
  A fact worth keeping has a home page in the wiki (decision 0017), so whatever is left in
  STATE is homeless by construction — and git keeps the discarded version anyway.
- Touch NO other file. No checkpoint, no graduation to the wiki, no overview edit, no
  log line. Durable capture belongs to `/wiki-checkpoint`.
- The wipe is left UNcommitted on purpose: until the next checkpoint commits it,
  `git show HEAD:.memory/STATE.md` restores the old STATE.
- One exception to the silence: if THIS session produced durable knowledge that was never
  checkpointed, say so in a single line AFTER the wipe — the user decides what to do.
