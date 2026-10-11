# Memory reliability: phase-two integration

The first reliability stage was integrated into `main` at `91ef6d6`. Its five
accepted priorities remain part of the baseline. Phase-two numbering is separate.

The owner authorized selective integration of phase-two P1 and P2 on 2026-10-11.
P3, P4 and P5 remain experimental on `improve/memory-reliability-phase-2`.
Technical implementation or earlier test closure does not authorize their rollout.

| Phase-two priority | Integration disposition |
| --- | --- |
| P1: enforced read-only inspection | Included in this selective integration |
| P2: verifiable work plans | Authorized; follows as a separate integration commit |
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

## Experimental work is retained

The experimental branch retains its existing implementation, uncommitted work,
research and failed trials. This integration does not adopt the checkpoint
compiler, independent model reviewers, repair coordinator, scoped-rule schema or
reason-history schema. Existing main checkpoint and resume behavior remains the
baseline. Existing installed projects are not automatically upgraded by merging
this distribution.
