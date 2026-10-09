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

## 3. Direct edges are explicit constraints

Known hard prerequisite lists cannot be overwritten by an `update`. Use a sourced
`link` or `unlink` event for one `(dependent, prerequisite, kind)` pair. Removing an
absent edge is an error, not a successful no-op. Direct edges are never removed by
transitive reduction. `soft` edges are advisory and kept separately from hard constraints.

```json
{"seq": 5, "id": "T18", "op": "unlink", "values": {"target": "T23", "kind": "hard"}, "reason": "T23 explicitly waived; all other constraints remain", "source": {"path": ".memory/research/events.md", "quote": "T23 is waived for launch."}}
```

A JSON array is one atomic batch, useful for creating a graph with forward references.
All targets must exist in the resulting graph. Hard cycles and self links are rejected;
soft cycles do not impose ordering. Unknown hard dependencies must be explicitly resolved
before individual edge edits. No guessed empty list is substituted for unknowns.

`tasks show` computes readiness. Only `open` tasks whose known hard prerequisites are all
`done` are ready. Unknown status/dependencies remain unknown. A cancelled prerequisite is
not a completed prerequisite; waive its edge explicitly if it no longer blocks. An agent
can still misinterpret a raw source while capturing it; the tool preserves the recorded
constraints and makes later changes explicit, rather than claiming natural-language truth.

Development regression: unrestricted replacement previously accepted dropping T24 while
keeping T14. The new command rejects it. Removing only T23 keeps T14 and T24; reopening
T24 blocks T18 even when T14 remains done. New tests cover advisory versus hard links,
atomic failures, unknown targets, cycles, and eight readiness/status combinations.

Validation: **13/13** task tests (**19/19** combined); four capture/recovery experiments, **8** fresh model sessions. Both baseline and new workflow pass **2/2** original/new scenarios. Deterministic edge and readiness checks establish the new guarantees; this model sample does not establish superiority.

## 4. Current views are computed, never independently edited

```sh
python .memory/tools/structural.py tasks current
python .memory/tools/structural.py tasks current T01
python .memory/tools/structural.py tasks at 1 T01
python .memory/tools/structural.py tasks history T01
python .memory/tools/structural.py tasks current > task-view.json
python .memory/tools/structural.py tasks verify task-view.json
```

Every current view has a revision, content digest, selection scope, completeness flag,
current values, ready tasks, definite/uncertain release obligations, and event references.
Sources are deduplicated rather than repeated for each field. This reduces output without
losing provenance. History and historical views are explicitly labeled. A revision inside
an atomic batch with unresolved forward references cannot form a valid graph; query the
completed batch revision instead. A current view is rebuilt on every read, with no cache.

`verify` compares the complete selected view, including types and watermark, against a fresh
reduction. It rejects stale or altered output and refuses to publish a historical view as
current. This verifies generated JSON snapshots, not arbitrary natural-language summaries.
Unknown task status is an uncertain obligation, not an assumed open task. STATE's focus
and the task ledger's obligations remain distinct; an idle session does not cancel tasks.

Alternatives: manually maintained current tables can diverge; replaying prose shifts the
merge work back to each reader. The selected derived view has one canonical event source
and lets a reader request just one task. A single task with forty owner changes yields a
current packet smaller than one third of the full history in the regression fixture.

The installed resume packet includes bounded task counts and a current-view pointer.
A real fresh install, normal guarded commit, resume, and updater preserved the task ledger
byte-for-byte. The view/history/verification tests add coverage for newly withdrawn dates,
unchanged fields, stale publication, unknown status, and JSON boolean/number distinctions.

Validation: **25** cumulative targeted regressions, including the installed update test.
Four additional paired capture/recovery experiments (**8** fresh model sessions) pass:
baseline **2/2**, new **2/2**. Both retain the current/historical distinction and the
withdrawn date in this small sample. No accuracy superiority is claimed.
