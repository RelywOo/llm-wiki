# Checkpoint inventory: scripts + Jev

## Selection and boundary

Use this pipeline when `.memory/inventory.json` contains `{"backend":"jev-combined"}`.
This project setting is explicit; the shared template does not enable network-backed
inventory in other projects automatically. Unconfigured projects retain their existing
agent inventory. When selected, missing input, credential or a failed request stops
this stage; never silently replace it with a full main-agent classification pass.

Scripts extract exact source passages; Jev performs inventory, applicability/evidence,
retention and comparison with prior memory. The accepted combined pipeline includes
source attachments, enduring-decision status, partially superseded scope and improved
prior-memory relationships. It preserves uncertainty and a skipped-item ledger.
The main agent continues at step 5 (choose edits), 6 (draft changes), and 7 (review the
draft). It does not reclassify the entire transcript after Jev.

## Prepare source input

Run the original context collector first. Select the current session's transcript and
an explicit inclusive line range since the previous checkpoint, including the latest
user decision but excluding future checkpoint results. Verify the boundary rather
than guessing from file recency. Use available source metadata to locate the transcript.
If the source is unavailable, obtain it rather than reconstructing a semantic inventory.

Choose unique run directories under Git metadata (`git rev-parse --git-path
wiki-inventory-runs`) so source packets and API responses remain outside tracked files.
Do not overwrite prior runs. Paths in the following commands are placeholders:

```sh
python .memory/tools/checkpoint_inventory.py prepare --root . --transcript TRANSCRIPT.jsonl --start-line FIRST --end-line LAST --prior .memory/STATE.md RELEVANT_WIKI_PAGES --output INPUT_DIRECTORY
python .memory/tools/checkpoint_inventory.py run --root . --packet INPUT_DIRECTORY/packet.json --output RESULT_DIRECTORY
```

`prepare` supports Codex JSONL response_item user and final-assistant messages. It
preserves source wording and offsets after credential redaction and records the
boundary/hash and omitted message types. Tools, commentary and reasoning are excluded;
important tool evidence absent from final messages must be supplied as explicit source
records, not assumed verified. Retrieve relevant wiki pages with structural search.
Prior memory is only the supplied snapshot, so an apparent new item may require target
lookup during edit selection. Do not use checkpoint pages that already contain this
session's outcome as prior memory.

Other transcript formats can use the explicit packet interface: JSON with a scenario
string and `records` and `prior_memory` lists. Each record needs unique simple `ref`,
`role`, and `text` strings. Produce these records mechanically from source messages or
files; do not have the main agent select individual findings before Jev. Optional tool
records should name their actual evidence role. Input files must contain no credentials.

The helper uses `TYPESAFE_API_KEY` from the environment or project `.env`; never put
credentials in packets or commands. Known credential forms are redacted, and private-key
material is rejected. This is not a universal detector for every kind of secret.
Requests use the pinned model and tested prompts in the helper. Before creating output
or making any API call, the helper checks every potential question in all four stages,
including the longer downstream instructions and whole parent paragraphs used by
relation. Oversized input reports stage, source ID, bytes and excess without truncation.
This is conservative: some downstream questions may later be discarded by Jev.
No automatic retry or semantic fallback is performed. The helper
only writes its new run directory; it does not write wiki, STATE, or commit.

## Consume the completed handoff

Only consume a directory with `manifest.json` and `complete: true`; verify its listed
file hashes before reuse. `answer.json` carries source IDs, Jev judgments and exact
context attachments; `spans.json` maps parent IDs to text and offsets; `packet.json`
holds chronology and prior memory; `children.json` preserves the classification ledger.
Raw API requests/responses remain local for diagnosis. The completion manifest binds
the input and helper hashes and records timing/usage.

5. Choose edits from the inventory: resolve target pages, combine duplicate retained
   information, distinguish additions/updates/supersessions, and handle the specific
   uncertainties relevant to proposed edits. Read full edit targets and linked evidence.
   Do not redo the full inventory or let a review label silently disappear.
6. Draft source-backed wiki/STATE changes in the existing reusable package. Carry
   table qualifications, links and still-valid scope into claims that depend on them.
   Attachments are context, not automatically retained independent facts. Avoid
   repeatedly pasting identical attachments into the drafting context.
7. Review the actual proposed draft against its sources, scope, user decisions and
   existing memory. This is draft validation, not mandatory step-1–4 reclassification.
   Apply the existing provenance, immutable-decision and rollback rules unchanged.

Jev labels are judgments, not proof of external facts. Resolve uncertainty only as far
as source evidence permits; preserve unfinished questions in STATE where appropriate.
Then use package `apply` and the normal checkpoint report; a separate package `check`
is an optional read-only preview. Generate source-backed
dropped-item accounting from the inventory; do not silently omit unresolved items.
