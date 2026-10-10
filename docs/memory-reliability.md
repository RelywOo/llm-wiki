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
isolated sessions. It accepts paths to a baseline checkout and output folder, and optionally an alternative
`runtime.py` harness. A Linux Landlock/Codex harness is bundled beside the runner. The harness uses the existing Codex subscription and
Landlock; it neither supplies expected answers nor substitutes model calls. The harness is research tooling, not part of the installed memory runtime. Raw results belong
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

For example, after preserving the quoted statement in `.memory/research/events.md`,
`event.json` can contain:

```json
{
  "seq": 1,
  "id": "T01",
  "op": "create",
  "values": {
    "title": "Migration tests", "status": "open", "owner": "Alex",
    "due": "2026-11-12", "priority": 1,
    "release_required": true, "dependencies": []
  },
  "reason": "Capture the approved release obligation",
  "source": {
    "path": ".memory/research/events.md",
    "quote": "E001: Migration tests are open, assigned to Alex, due 2026-11-12, priority 1, required for release, with no prerequisites."
  }
}
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

## 5. Evidence location, freshness and meaning are separate checks

```sh
python .memory/tools/structural.py evidence quote .memory/research/events.md 1 2 --event E001 > receipt.json
python .memory/tools/structural.py evidence check receipt.json
python .memory/tools/structural.py evidence check decision-receipt.json --current
```

The extractor returns exact line text (line endings normalized), line numbers, a source
SHA-256 and an optional verified event label. The checker accepts a receipt or array and
rejects altered text/ranges, changed sources, wrong files, invented event labels, unsafe
paths and malformed input. `--current` specifically requires an unambiguous active decision;
a raw research file has no automatic current-decision status. Historical decision quotes
remain valid history. An inherited scope needs explicit review of ancestor plus successor,
not automatic rejection of every historical fact and not blind acceptance of an old rule.

Final delivery uses a manifest instead of model-transcribed receipts:

```json
{
  "answer": {"region": "eu-west-1", "explanation": "Production scope."},
  "sources": [{"path": ".memory/research/events.md", "start_line": 1, "end_line": 1, "event_id": "E001"}]
}
```

```sh
python .memory/tools/structural.py evidence publish manifest.json deliverables/answer.json
python .memory/tools/structural.py evidence verify deliverables/answer.json
```

The publisher extracts quotes and source hashes itself. If a selection omits `event_id`,
it derives that metadata only when the quoted text has exactly one distinct ASCII `ID:`
line label. With multiple labels, select smaller ranges or specify the intended event.
An explicit label is always checked against the quoted lines. This labels text syntactically;
it does not infer that the source's claim is true. It keeps interpretation in `answer`,
checks the serialized artifact, flushes a temporary file, and atomically publishes a new
`deliverables/NAME.json` without overwriting anything. Existing destinations are rejected;
use a new name for a revision. It rejects symlink destinations and non-deliverable paths.
The final file contains `schema`, `answer`, `evidence` (each entry has `receipt` and a boolean
`current` requirement), `semantic_support`, and `artifact_sha256`. The content digest detects
accidental edits to the answer, receipts or requirements; it is not a signature, trusted
attestation, or proof of meaning. Share the generated file, not a model-retyped JSON copy.
`verify` rechecks source freshness and any `current: true` decision requirements, so a newly
superseded decision invalidates current proof even when its own source bytes are unchanged.
Checks are point-in-time: subsequent source edits require another verification/publication.
There is no repository-wide read lock or protection against a writer deliberately recomputing
the digest. Atomic publication and fsync are tested on Linux; physical power-loss recovery
and other platforms remain untested. A crash can leave a harmless hidden temporary file.

Every result says `semantic_support: not_checked`. For example, an exact quote saying a
region is lab-only is real evidence, but it cannot support a claim that production is
allowed there. Scope, reason and entailment remain a separate agent/reviewer assessment.
Paraphrases must be labeled, and a multi-source inference must cite its component excerpts.

New task events require an exact source excerpt both at capture and against the staged
source tree at commit. An unstaged source repair cannot hide a bad staged citation. Once
filed, excerpts remain historical evidence when the source changes. Current views expose
`matches_current_file`, `historical_excerpt`, or `source_unavailable`; the stored fact is
not silently discarded. Retain stable capture sources through the first checkpoint and
use Git to inspect older source versions. Structured capture still requires the agent to
interpret the source correctly: a matching quote cannot prove its chosen field values.

This addition does not retroactively certify arbitrary legacy Markdown source markers.
Those still establish source reachability; use receipts for exact quotations and the new
ledger contract for tracked tasks. Handwritten prose and bypassed workflows can still be
wrong. Tests deliberately preserve this distinction rather than reporting a misleading
universal "evidence verified" flag.

## Final validation and limits (2026-10-10)

- **35 new regression tests pass.** Full discovery: **56 run, 51 pass, 5 skip** because
  Linux user systemd was unavailable. Fresh distribution and updater selfchecks pass.
- **42 real fresh Codex sessions completed**, with no timeout or substituted model call:
  9 decision sessions; 8 each for fields, edges and views; 6 evidence sessions;
  2 original-corpus replays; 1 final combined held-out smoke test.
- The full original decision evaluation yields **9/9 facts and 55/55 code checks** for
  both baseline and new tooling, starting from identical knowledge digests. The earlier
  stochastic failure did not recur in this pair. This is not proof of an accuracy gain.
- The evidence experiment gets **12/12 semantic verdicts and 12/12 exact excerpts** across
  both arms. All **6 new receipts** verify. Strict JSON shape passes baseline **2/3** and
  new **1/3**: nested verdict objects remain recorded as shape failures, not erased by
  semantic scoring. The initial scorer path bug was fixed without rerunning subjects.
- The final new smoke test combines an inherited server limit, changed edge limit,
  unknown task owner, release obligation and verified E103 receipt; it passes.

Measured CLI usage across these sessions: **3,297,467 input tokens**, of which **2,792,448
cached**, **505,019 uncached**, and **42,798 output tokens**. Summed subject time was about
13.4 minutes. These are cumulative CLI measurements, not peak context or subscription
percentage; they exclude the controlling conversation and setup. Local checks overlapped
some sessions, so no controlled speed comparison is claimed. Existing ChatGPT login was
used, with no paid API fallback. The final auth-link inventory was empty.

The branch closes concrete tool-level failure paths under its documented workflow. It does
not establish that every agent will follow instructions, correctly interpret every source,
or use exact JSON output shapes. Legacy prose requires source-reviewed migration into the
canonical task ledger; existing projects were not automatically upgraded. Semantic support,
long-running real-project behavior, concurrent human edits and physical power-loss recovery
still need separate validation. Exact citations and valid schemas do not substitute for that.

Reproduction commands and the bundled isolation harness are documented in
[the evaluation guide](../code/reliability-evaluation/README.md).

## Acceptance follow up 2026 10 10

The initial candidate `6717865` did not pass independent review. Follow-up fixes
`1f3f1ca` and `d84a45d` reject malformed supersession metadata, distinguish task
events filed at HEAD from new events since a STATE checkpoint, preserve each
evidence experiment's prompt across both arms, and reject numeric verdicts where
booleans are required. Each finding has a regression that failed before its fix.

Final discovery runs **60 tests: 55 pass and 5 existing user-systemd tests skip**.
Distribution and updater checks pass. Three actual baseline-to-candidate upgrades
preserve nine knowledge files byte-for-byte, preserve runtime choices, support a
guarded task commit, and remain unchanged on a second update. Additional CLI checks
cover eight competing writers, interrupted-lock recovery, 120 sparse updates,
atomic rejection, and evidence falsification.

The acceptance experiment contains five new scenarios, two repetitions, and separate
capture/recovery sessions. The candidate runs on `gpt-6-luna` and `gpt-6.1-sol`, both
at medium reasoning; baseline `aeca761` runs on Luna. All three arms recover 58/58
fields by meaning and pass 10/10 generated-code evaluations. Strict trajectory
scores are baseline Luna 4/10, candidate Luna 7/10, and candidate Sol 10/10. Strict
scores include representation differences: unknown versus null, list ordering, and
the inclusion of "because" or a terminal period in an exact reason phrase. Those
failures remain recorded and are not described as lost facts. Candidate receipts
have stricter hash/range requirements than baseline excerpt citations, so these
trajectory scores are not comparable measures of memory accuracy alone.

Canonical capture audits pass 20/20; reverse-order decision reads with three-source
provenance pass 2/2. However, one candidate Luna recovery manually corrupts an
extracted receipt's SHA-256. The checker detects the mismatch and the agent discloses
it, but the final receipt remains invalid. A fresh six-session run of the corrected
evidence experiment also finds repeated output-schema violations: meanings and
all 12 quotations are correct, while extra fields invalidate four of six candidate
receipts. These failures were not rerun or normalized away.

**Acceptance remains on hold for the complete five-priority workflow.** Mechanical
validation and knowledge preservation pass, but reliable final evidence delivery is
not established. The next change should preserve tool-generated receipts through
programmatic file/output assembly, keep interpretation separate, and validate the
actual final artifact before publishing it. Passing facts or a successful model
turn alone does not satisfy that contract. This is a pending change, not a delivered
guarantee. The branch has not been merged or deployed to existing projects.

The subject total is 68 real Codex sessions, including the two reverse-order reads
and six evidence-runner sessions. All completed using existing ChatGPT authorization;
no Astra or paid API fallback was used. Subject CLI usage totals 10,358,202 input
tokens (9,088,640 cached) and 151,882 output tokens. This excludes the controlling
conversation and independent Sol reviewers and is not subscription-quota usage.
Concurrent local/additional checks prevent a controlled speed comparison. The
small synthetic corpus cannot establish universal model reliability or accuracy
superiority; runtime hooks/checkpoints were tested separately from subject sessions.

## Final evidence delivery acceptance (2026-10-10)

The pending delivery change is implemented in `4612b3f`, with replay validation in
`0ccfea2` and deterministic event-label extraction in `03689f4`. Receipts are now
assembled and verified in the publisher; the agent delivers the generated artifact
instead of transcribing receipt JSON. The previously corrupted SHA remains rejected.

The first focused series has 24 fresh recovery sessions: 12 publisher Luna, six
publisher Sol and six pre-fix manual-receipt Luna controls. All facts and all 36
publisher receipts pass. Strict publisher scores are Luna **10/12**, Sol **6/6**;
manual controls pass **6/6**. Two Luna sessions omit `event_id` metadata while quoting
the correct labeled events. The original prompt required event labels but did not
explicitly distinguish metadata from labels in quote text, whereas the scorer required
metadata. Those failures remain failures; the initial delivery-series decision is HOLD.

The subsequent fix derives an unambiguous label from selected source text. A separate,
predefined follow-up explicitly states the metadata requirement and runs all six cases
on Luna, with additional Luna and Sol repetitions of the two affected cases. Final
results are **8/8 Luna and 2/2 Sol**, without answer repair or normalization. The
publisher also mechanically replays all 18 initial source selections successfully,
including both failures, while preserving original artifacts. These mechanical replays
reuse original answers and are not new model-quality measurements.

Final discovery runs **68 tests: 63 pass and 5 existing user-systemd tests skip**.
Distribution and updater selfchecks pass. Public CLI coverage includes final-file
mutation, source changes, decision supersession, malformed selections, symlinks,
no-overwrite publication, competing writers and ambiguous labels. Independent Standards
and Spec reviews have no open findings. A reviewer-found replay-input overwrite was
fixed with a regression before the final follow-up. The first series used its frozen
runner; its supplied event source was audited as byte-identical to the fixed fixture,
and its neutral neighbor addition is disclosed rather than claiming directory identity.

**Recommendation: accept the five-priority branch under the documented workflows.**
The previous four priorities retain their earlier acceptance evidence and pass the
repeated regression suite. The final evidence-delivery blocker is closed for the tested
contract. Tools and instructions must be updated together, and generated files must be
delivered without model retyping. This does not certify semantic support, prove universal
model reliability, or establish an accuracy improvement over baseline. Windows/macOS,
physical power-loss recovery and the skipped systemd checks remain untested. The branch
has not been merged or deployed to existing projects.

This follow-up contains **34 real subject sessions: 26 Luna and eight Sol**, all at
medium reasoning and all completed. Actual command logs confirm publisher/verification
use in all 28 publisher sessions. Existing ChatGPT authorization was used; no Astra or
paid API fallback. Total measured subject usage is **3,770,749 input tokens**
(**3,261,568 cached**, **509,181 uncached**) and **38,530 output tokens**. Summed subject
time is about **15.7 minutes**. These figures exclude the controlling conversation and
independent reviewers; they are not subscription-quota percentages or controlled speed
comparisons. Small synthetic samples do not estimate real-world failure rates.

Reproduce with the final-delivery runner in the evaluation guide; `--followup` selects
the ten-session final schedule. Initial and follow-up protocols, raw outputs, failed
results, mechanical replays, checks and resource measurements were retained separately.


## Integration approval (2026-10-10)

The project owner accepted the five developed priorities and authorized merging the
reliability branch into `main` and publishing it to the upstream repository. The accepted
implementation is `03689f4`, with final acceptance documentation in `794cb94`.

| Development priority | Status | Delivered mechanism |
|---|---|---|
| 1. Current decisions | Closed | Full supersession resolution, scope-preserving lineage and fail-closed metadata validation |
| 2. Complete task fields | Closed | Canonical sourced event ledger, required fields and explicit unknown values |
| 3. Direct dependencies | Closed | Explicit hard/soft edges and computed readiness |
| 4. Current task state | Closed | Derived current views, separate history and revision verification |
| 5. Evidence delivery | Closed under the documented workflow | Programmatic quote/hash/label extraction, final artifact publication and verification |

Closure refers to these five development priorities, not the first five rows of an earlier
research planning table. A general runtime-enforced read-only mode and checkpoint workflow
simplification were separate research proposals and remain open. Exact citations do not
certify semantic support. The final 8/8 Luna and 2/2 Sol sessions, 63 passing tests with five
systemd skips, distribution/updater checks, and independent reviews support this acceptance.
Earlier failed results and the documented limitations remain part of the evidence.
