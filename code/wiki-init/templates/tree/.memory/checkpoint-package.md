# Reusable checkpoint packages

The selected inventory backend supplies source-linked judgments; see
`.memory/checkpoint-inventory.md` for scripts + Jev. The agent chooses edits, drafts
and reviews the proposed changes, and reads the pages it will edit. This helper moves
format checks and applying saved edits into code. Structural guard and the pinned Git pre-commit still validate the
actual commit candidate. A successful preflight is not proof of factual correctness.

## Prepare

After the inventory and reads, run:

```sh
python .memory/tools/checkpoint_package.py init --name session-6 \
  --block "Practical evaluation" --summary "Record the evaluation decision" \
  --allow .memory/wiki/concepts/example.md .memory/research/evidence.md
```

Use the actual session number from recon and the exact intended paths. STATE and
index are added automatically; log is owned by the coordinator and must not appear
in `--allow`. Include existing source/code/config files that belong in this commit.
The command normalizes spaces in the block to hyphens and validates the generated
log line before creating a draft. It prints the package's exact path under the
worktree's Git metadata, `wiki-checkpoint-packages/<name>.json`. Existing names are
never overwritten. Do not assume `.git` is a directory in a linked worktree.

## Fill the draft

Read and edit the printed JSON file. Preserve `schema`, `baseline`, paths and
`before_sha256`: they bind the package to the files you inspected. Do not recompute
hashes merely to get past a stale-package error; reread and reconsider the edits.
`block` and `summary` can be corrected without regenerating the other entries.

Each file has exactly one entry. Replace its initial `keep` action as appropriate:

- `keep`: no content fields; retain and include an existing file as-is. Useful for
  unchanged index and already-written research/code. A missing file cannot be kept.
- `write`: add `content`, the complete UTF-8 text. Allowed for STATE and new files.
- `edit`: add `edits`, a list of `{"old": "unique text", "new": "replacement"}`.
  Each old string must occur exactly once when that edit is applied. Combine all
  edits to the same file into one entry. Existing wiki pages are edited in place;
  filed decisions may only be kept, never changed.

STATE must use `write` and actually change. Keep canonical sections and the 40-line
limit. Use normal canonical index entries, for example:
`- [0001](decisions/0001-example.md) — Which approach? → Use the chosen approach.`
The script checks index entries against both existing and proposed decision files.
JSON duplicate fields, unknown fields, duplicate targets, missing fields, unsafe
paths, symlinks, stale files, bad edit anchors and invalid formats are rejected.

## Validate and save

```sh
python .memory/tools/checkpoint_package.py check --name session-6
python .memory/tools/checkpoint_package.py apply --name session-6
```

`check` is read-only and renders proposed edits in memory. `apply` repeats those
checks, starts the existing allowlisted transaction, checks freshness again, writes
the prepared edits, then invokes the existing commit coordinator. It owns BEGIN,
log append and TAIL: do not also run them manually. It never edits the package.

If preflight fails, no target files have been written. Correct the offending JSON
entry and retry. If a commit validator fails, the coordinator restores protected
files; the package survives outside that rollback. Fix the entry and reapply the
same package. Never rerun automatically without addressing the reported cause.

If HEAD or target bytes changed, review the new baseline and prepare a new package.
If a transaction remains active after an interruption, use
`checkpoint_txn.py status` and `checkpoint_txn.py recover` before retrying. A package
whose commit already landed is stale and will refuse to apply twice. Never blindly
change its baseline to force a retry. The helper does not add concurrent-writer
support; preserve the coordinator's existing HEAD/index/recovery rules.

Packages are retained after success for inspection. They contain draft content:
keep credentials out, and remove an obsolete package explicitly when it is no longer
needed. Do not stage or copy package files into the wiki.
