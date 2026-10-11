# Memory reliability: phase-two integration

The first reliability stage was integrated into `main` at `91ef6d6`. Its five
accepted priorities remain part of the baseline. Phase-two numbering is separate.

The owner authorized selective integration of phase-two P1 and P2 on 2026-10-11.
P3, P4 and P5 remain experimental on `improve/memory-reliability-phase-2`.
Technical implementation or earlier test closure does not authorize their rollout.

| Phase-two priority | Integration disposition |
| --- | --- |
| P1: enforced read-only inspection | Included in this selective integration |
| P2: verifiable work plans | Included as an on-request planning tool |
| P3: structured rule applicability | Experimental; excluded from main integration |
| P4: structured decision reasoning | Experimental; excluded from main integration |
| P5: checkpoint simplification and review/repair | Experimental and open; excluded from main integration |

## P1: inspection sessions

Start the whole inspection process through the installed `readonly_session.py`
launcher. Its restrictions apply to descendants; launching a child does not turn
an already-running parent session into a protected session. Normal write sessions
remain available. External output/profile directories must be explicitly declared.
Unsupported Linux enforcement is refused; other processes and remote service
mutations are outside this local boundary.

The implementation, installer delivery and public-interface tests are selected
from `63b35e2` and `edaf53c`. Fourteen kernel-enforcement regressions pass on the
integration checkout. They cover descendant writes, Git, metadata, shared memory,
links, local IPC, declared outputs and ordinary write recovery. The diagnostic
harness is retained for reproduction; no new model sessions were needed to copy
and verify this mechanism.

## P2: plans when requested

The agent uses `tasks plan` when the user requests a work plan or asks which
tracked tasks can be started next. It saves the generated output and checks it
with `tasks verify-plan` before delivery or use. Markdown output is optional and
must also be verified when delivered. Checkpoint, resume, ordinary edits and
status reports do not acquire an automatic planning step.

The implementation and tests are selected from `076ac63` and `9348fe2`. The small
plan-only clarification and regression from `860e612` are retained: task readiness
does not select or authorize the next action. The late continuation subsystem is
excluded. Eleven planning regressions pass on the integration checkout, including
reopened prerequisites, unknowns, source changes, stale/altered JSON and Markdown,
manual blockers and preserved selected continuation.

The canonical ledger format and ordinary write/checkpoint path remain those of
main. This feature checks consistency with recorded data, not source truth or
completeness of work that has never been tracked.

## Experimental work is retained

The experimental branch retains its existing implementation, uncommitted work,
research and failed trials. This integration does not adopt the checkpoint
compiler, independent model reviewers, repair coordinator, scoped-rule schema or
reason-history schema. Existing main checkpoint and resume behavior remains the
baseline. Existing installed projects are not automatically upgraded by merging
this distribution.

## Selective integration validation

The combined P1/P2 checkout passed the full suite: **93 tests, 88 executed and five
existing Linux user-systemd checks skipped** (308.084 seconds). Distribution checks
and the updater selfcheck passed. All eight runtime flag combinations passed.
A separate old-main-to-candidate upgrade preserved eight knowledge files
byte-for-byte and retained runtime flags; its second update was idempotent.
The upgraded installation verified JSON/Markdown plans and refused an attempted
STATE write inside the protected launcher. Experimental modules were absent.

Checkpoint/resume procedures, the package implementation, structural dispatch and
updater implementation were verified byte-identical to main. This integration does
not claim new model-quality results: no model sessions were launched. Earlier
component-study scores retain their documented question/representation limits.
The environment-dependent systemd skips and untested non-Linux enforcement remain
limitations; they are not counted as passes.
