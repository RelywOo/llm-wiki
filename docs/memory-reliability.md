# Memory reliability

This work addresses five observed failures in the pinned `aeca761` baseline:
current decision selection, required task fields, direct dependencies, current views,
and evidence fidelity. Changes are evaluated in that order. A successful deterministic
check establishes its stated contract; it does not prove that an arbitrary agent will
always follow instructions or that a source's assertions are true.

## 1. Resolve decisions before applying them

Use `python .memory/tools/structural.py decision 0008` (an ID or decision path).
The JSON response identifies the current successor and includes the entire ordered
lineage. Apply changes by scope, retaining unchanged scopes. The tool does not attempt
to extract or merge natural-language values. Partial changes remain the agent's semantic
responsibility, with all necessary ancestors in the same response.

`section` now identifies current or superseded decisions, and points to the resolver.
Search already marked historical hits in the baseline. A branched, cyclic, missing-target,
or malformed graph cannot yield an authoritative current packet. Branching requires an
explicit scope review; choosing the greatest ID would be unsafe. Direct filesystem reads
remain possible; the shared agent instructions require resolution before application.

### Alternatives evaluated

- Existing active set and search labels: retain history but leave section reads unmarked.
- Instruction alone: adds discipline but leaves manual traversal to the agent.
- Instruction plus current-chain command and section notice: a single read supplies
  the applicable lineage, including partial replacements. This is the selected design.

### Reproduction

`python -m unittest discover -s code/wiki-init -p test_memory_reliability.py -v`

`code/reliability-evaluation/run_decisions.py` compares the three configurations in fresh
isolated sessions. It accepts paths to a baseline checkout, output folder, and the local
experiment's `runtime.py` harness. The harness uses the existing Codex subscription and
Landlock; it neither supplies expected answers nor substitutes model calls. It is a
local research dependency, not part of the installed memory runtime. Raw results belong
outside the public distribution. Small scenario samples are diagnostic, not a universal
model reliability estimate.

### Development result (2026-10-10)

The minimal baseline reproduction returned only `Server and offline: 512 KiB.` from
`section`: the stale-read detection assertion failed. With the change, all six CLI
regressions pass, covering a new unrelated policy, partial replacement, refresh after a
new decision, history search, and invalid/ambiguous graphs. Replaying the original
36-decision snapshot resolves `0008 -> 0017 -> 0033` without modifying the snapshot.
The model experiment results are recorded alongside subsequent priorities below.

Fresh model sessions: baseline **3/3**, instruction-only **3/3**, resolver **3/3**; answer and generated policy both checked. This small set shows compatibility, not a measured accuracy advantage.
- baseline: median 22.3 seconds (three sessions).
- instruction: median 13.6 seconds (three sessions).
- resolved: median 11.4 seconds (three sessions).

## 2. Required task fields have one canonical home

The opt-in `.memory/wiki/task-ledger.md` stores sourced, ordered JSON events inside
readable Markdown. It is the task record, not a cache of a separately editable table.
The model captures source meaning; Python validates structure and computes values.
Legacy prose is not silently migrated. An absent ledger reports `tracked: false` and
`complete: false`, rather than claiming no work exists.

Commands:

```sh
python .memory/tools/structural.py tasks apply event.json
python .memory/tools/structural.py tasks show T01
python .memory/tools/structural.py tasks check --complete
```

A creation event contains every field, using `null` for explicit unknowns. Updates only
name changed fields, preserving everything else and its field-level event/source/reason.
Status changes require a sourced reason like every other event. Values are never coerced:
`"false"`, `0`, and `false` are different inputs. Missing fields are rejected before writing.
`--complete` fails on unknowns; ordinary reads retain and identify them.

The writer uses a sequence precondition, an exclusive lock, an atomic replacement, and
file/directory fsync on POSIX. A stale lock requires inspection after an interrupted writer;
it is not automatically discarded. These protections do not replace backups or prove
hardware power-loss behavior. The normal staged Git validator and history audit reject
ledger rewriting/deletion, including writes made outside the CLI. The updater delivers
the tool and instructions while preserving knowledge, including this ledger.

A richer prose table was considered but cannot enforce completeness. Repeated full snapshots
were rejected because a partial update could accidentally erase an unrelated field. The
selected event representation preserves unchanged fields without duplicated current truth.

Development checks: **8/8** task regressions; **14/14** combined regressions. The 24-task fixture retains **15/15** open release obligations after navigation compaction. Four paired capture/recovery experiments (eight fresh model sessions) pass: baseline **2/2**, new **2/2**. Both answer lists and explicit unknowns are checked. This demonstrates the workflow, not a statistically established accuracy advantage. The full installed distribution check passes.
