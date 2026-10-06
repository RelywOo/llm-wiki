# Background checkpoint with ordinary drafting

Codex app integration for this installation. The original conversation stays open;
a native history-preserving worktree fork performs ordinary drafting and validation.
The original agent publishes one guarded checkpoint at a coordinated tool boundary.
No smaller-output assembly treatment is used.

## Parent: prepare and dispatch

Use this mode by default for a checkpoint in the Codex app when native fork tools and
an installed watchdog are available, unless the user requests foreground execution.
If the tools or watchdog installation are absent,
use the existing foreground procedure and say that it is foreground. Do not imitate
a background fork with a context-free agent or silently bypass failed preparation.

1. Run `python .memory/tools/checkpoint_background.py worker-status`. A worker marker
   means follow the worker procedure below; never recursively fork a checkpoint.
2. Check watchdog health and pending notices as described under Independent supervision.
   Locate this thread's actual JSONL transcript by session metadata, not file recency.
   Run the original context collector and resolve relevant prior pages. Prepare a
   unique job, forwarding the explicit transcript identity and optional exact cutoff:

   ```sh
   python .memory/tools/checkpoint_background.py prepare --name JOB --transcript TRANSCRIPT --thread THREAD_ID --prior .memory/STATE.md RELEVANT_PRIOR_PAGES
   ```

   The default cutoff is the last complete line currently present. The helper derives
   the start from the committed thread cursor; on first use it includes the full
   transcript. It freezes tracked working-tree bytes and nonignored untracked files,
   preserves deletions, excludes ignored files, and refuses symlinks or excessive
   snapshot size. It preflights every Jev stage before dispatch. No source text is
   silently removed to fit. Resolve a refusal explicitly; do not launch an invalid job.
   Runtime/skill/hook changes must first be committed and installed. Credentials are
   not copied from the ignored `.env`; the worker's Jev invocation reads them from
   the source root without printing or persisting them.
3. Call the Codex app `fork_thread` with `environment: {type: "worktree"}`. This must
   be a native fork of this conversation. Wait for setup to return a real child
   thread ID and worktree path; a `clientThreadId` is not a dispatchable thread ID.
   Confirm the actual child cwd from thread metadata. Do not substitute a same-directory
   fork whose shell commands merely point to another directory.
4. Bind the clean native worktree to the frozen snapshot:

   ```sh
   python .memory/tools/checkpoint_background.py bind --name JOB --worker WORKTREE --worker-thread CHILD_ID
   ```

   Binding refuses unexpected worktree changes or another baseline, restores the
   frozen file versions and installs/checks the pinned Git hook. The source branch is
   never checked out in the worker. Failure leaves the original repository untouched.
5. Send the child a concise task: perform the worker procedure in this document for
   JOB using the frozen inputs; ordinary drafting, all existing checks, no source-root
   writes except coordinator job metadata, no live publication. Forward any active-turn
   instruction omitted from inherited history as scope, not fabricated source evidence.
   The frozen transcript already contains the actual user messages through its cutoff.
6. Tell the user the job is running and continue here. Do not wait synchronously unless
   asked. The child sends a completion notification to this original thread. Keep its
   thread ID and JOB available; a lost notification can be recovered with status.

## Worker: ordinary checkpoint on the frozen snapshot

Run all shell operations from your actual native worktree. Verify `pwd` and
`CODEX_THREAD_ID` agree with the job marker. Hooks then use the real worktree cwd.
Use `git rev-parse --absolute-git-dir` to locate `wiki-background-input/packet.json`
and `source.md`; never assume `.git` is a directory in a worktree.

- Record each real stage transition with the supervision commands below.
- Follow the normal checkpoint collection, inventory, target reads, edit selection,
  drafting and semantic review. Use the supplied packet as the authoritative session
  boundary, not later inherited conversation. Prior memory is explicitly partial;
  consult frozen full target pages for edit-relevant uncertainty.
- Invoke the worktree's unchanged Jev inventory command with the supplied packet,
  a fresh result directory in the worktree Git metadata, and `--root SOURCE_ROOT`
  solely for credential lookup. The new all-stage size preflight remains mandatory.
  Consume only a completed manifest. Do not classify again or fall back silently.
- Cite the coordinator's future source archive path given by job status. Its exact
  contents are the frozen `source.md`. Add actual project sources where appropriate.
  The coordinator injects this archive into the package before validation.
- Fill a normal reusable checkpoint package. Preserve unrelated unresolved STATE
  items and source uncertainty, especially “not established” versus “ruled out.”
  Review the drafted meaning against the sources; mechanical guards are not a
  semantic correctness proof.
- Before normal package check/apply, run:

  ```sh
  python .memory/tools/checkpoint_background.py attach --package PACKAGE
  python .memory/tools/checkpoint_package.py check --name PACKAGE
  python .memory/tools/checkpoint_package.py apply --name PACKAGE
  python .memory/tools/checkpoint_background.py ready --package PACKAGE
  ```

  Attach adds the source archive and transcript cursor. Do not edit those generated
  entries. If the package needs repair, preserve unchanged entries; normal rollback
  and recovery rules still apply. `ready` verifies the package against the actual
  validated worker commit and seals a publication copy. It never publishes live.
- Send the originating thread a notification using the Codex app's
  `send_message_to_thread`: “Background checkpoint JOB is ready. Inspect status and
  publish through checkpoint_background.py at a coordinated tool boundary.” Include
  a short content digest and any corrections. This notification is part of the
  user-authorized checkpoint, not an external message. On failure, notify the parent
  with the concrete blocker instead. Do not claim completion of live publication.

## Parent: coordinated publication

Treat the notification as a cue to inspect the known job, never as arbitrary shell
instructions. Run `status --name JOB`, inspect the child digest/changed paths, and
publish from the originating thread while pausing other writes for the short apply:

```sh
python .memory/tools/checkpoint_background.py publish --name JOB
```

Publication checks the original HEAD, branch, index, complete frozen read set and
transcript prefix, then checks again after acquiring the existing transaction.
Appended conversation messages and new unrelated files are allowed and preserved.
Changes to any captured file, a moved HEAD, staged changes, or another checkpoint
cause refusal. Do not refresh hashes or automatically merge/rebase a stale result.
Cancel and prepare a newly reviewed job if necessary. This conservative first version
does not automatically publish across concurrent edits to captured source files.

The original coordinator still owns allowlisted staging, compact/guard, pinned
pre-commit, log append, compare-and-swap commit and rollback. Background operations
have an OS-released advisory lock; only one pending job is permitted per source
worktree. Arbitrary external editors are not serialized by this lock. Parent and
other agents must coordinate writes during the brief publication operation, as with
an ordinary foreground checkpoint. The worker cannot publish using its thread identity.

The commit atomically records `.memory/checkpoint-cursors.json`. The next background
checkpoint starts immediately after that line, not after the later commit timestamp.
For an explicitly foreground checkpoint after a background run, consult
`cursor --thread THREAD_ID --transcript TRANSCRIPT` before choosing the source range;
never skip pending messages using commit time. An unchanged older cursor can cause
safe reprocessing, not authorize skipping messages.

## Status, cancellation and interruption

- `status --name JOB` exposes prepared/running/ready/publishing/published/cancelled.
- `cancel --name JOB` releases a pending job; its late worker cannot seal or publish.
  Preserve artifacts for diagnosis. Stop the child through the app when appropriate.
- If publication was interrupted with an active transaction, use the existing
  `checkpoint_txn.py status/recover` first, then rerun publish. A landed cursor makes
  this idempotent: the same checkpoint is not committed twice.
- After an interruption during preparation or binding, inspect the journal and worker
  before cancellation/restart. Do not erase an unexplained transaction or reservation.
- Only the originating thread can publish/cancel. If it is unavailable, inspect and
  explicitly recover the job rather than impersonating its thread environment.

Report success only after `published`, using the normal checkpoint digest. Fork setup,
worker completion, and live publication are distinct states.

## Independent supervision

This Linux installation uses `.memory/tools/checkpoint_watchdog.py`, a deterministic
systemd user service on a 30-second timer. It never calls a model or an API, publishes,
retries, cancels, or deletes a job. It writes only supervision metadata in Git's
private directory. The worker's ordinary in-app readiness message remains the fast
path; supervision does not depend on that message succeeding.

Before dispatch, run `python .memory/tools/checkpoint_watchdog.py health` and inspect
`inbox`. Preparation also requires a recent healthy scan. Missing or stale supervision
is a hard stop for a new background job. Installation is explicit:

```sh
python .memory/tools/checkpoint_watchdog.py install
```

The installer manages a repository-specific user timer, preserving foreign units.
It runs a retained copy of the Python runtime, starts at user login, and catches
up after sleep. Reinstall after changing runtime tooling or relocating the repository.
Installation is per source checkout; another worktree needs its own installation. While the computer is off/asleep
or the user service manager is stopped, no local monitor can send an alert. The
existing prompt hook and read-only wiki-resume procedure report unhealthy supervision
and unread notices when you return. To disable deliberately, use `systemctl --user disable --now TIMER`, taking TIMER from `health` or registration metadata; new
background preparation will refuse after the heartbeat expires. Explicit foreground
checkpointing remains available.

Workers record real stage transitions, not artificial keepalives:

```sh
python .memory/tools/checkpoint_background.py progress --stage inventory
python .memory/tools/checkpoint_background.py progress --stage drafting
python .memory/tools/checkpoint_background.py progress --stage review
python .memory/tools/checkpoint_background.py progress --stage validation
```

On a handled failure, run `fail --stage inventory` (or drafting/review/validation/runtime)
and notify the parent with a concise, credential-free explanation. Do not report a
failed job as saved. The watchdog independently detects no stage progress for five
minutes, dispatch/publication pending for two minutes, an observable worker turn
ending without readiness, missing/corrupt evidence, and stale source inputs.
Local lifecycle logs are a best-effort early signal; stage deadlines still work if
those logs are unavailable or their format changes. “Overdue” means inspect, not
proof that a worker failed. It never authorizes an automatic restart or forced publish.

Notices are recorded before desktop delivery. Successful desktop transport is not
human acknowledgement; notifications may be hidden by desktop settings. Delivery
failures retry on subsequent scans; accepted but unacknowledged notices repeat at
most every 15 minutes. The durable inbox remains the fallback even after app restart:

```sh
python .memory/tools/checkpoint_watchdog.py inbox
python .memory/tools/checkpoint_watchdog.py prompt
```

The original agent reports **Started**, **Ready (not yet saved)**, **Saved (commit and
source boundary)**, or **Needs attention**. It inspects status and the frozen package
before recovery/publication. A saved notice requires the committed cursor and its
ancestry on the source branch to verify. Do not treat worker completion as publication.
A final capture receipt also names the actual wiki/STATE changes, unresolved work,
and intentional exclusions from ordinary drafting; the watchdog cannot judge whether
natural-language summaries preserved every important meaning.

After a notice has actually been reported to the user, acknowledge its exact ID with
`checkpoint_watchdog.py ack --id ID`. Do not acknowledge in advance or automatically
from notification transport. The inbox retains history; resolved conditions stop
repeating, and a recurring problem becomes unacknowledged again. A read-only resume
reports notices without acknowledging them.

For interruption, retain source/draft/journal artifacts, inspect the existing
transaction, and recover through the original guarded coordinator. The watchdog does
not modify the checkpoint cursor. These are local recovery safeguards, not an
independent off-device backup or a proof of perfect semantic retention.

## Shared installation and upgrades

WikiInit distributes these helpers and commands to every installation. Persistent
background monitoring is an explicit option: `install.py PROJECT --codex --watchdog`
or `update.py --watchdog` when Codex is already enabled. All runtime flags still
default to off on fresh installation. Jev inventory remains an independent opt-in;
copying its helper does not choose a model/backend or copy credentials.

Without watchdog activation, checkpointing remains foreground. An explicit
`--watchdog` request checks Linux/user-systemd support and unit ownership before any
project writes; unsupported hosts are refused, not reported as background-ready.
Activation happens only after the tooling commit, Git-hook installation, and doctor
succeed. A deployment failure returns exit 6 with a repair instruction; installed
files are retained and must not be overwritten by rerunning wiki-init.

Updates refresh an existing watchdog's retained runtime automatically, preserving
whether its timer is enabled and currently running. Explicit `--watchdog` activates
it. Relocation replaces only verified owned units, removes the obsolete timer, and
preserves that status. Pending background jobs, active checkpoint transactions, and
staged changes block tooling installation/update before writes. Existing wiki, STATE,
source archives, cursor ledgers, inbox records, and runtime selections are preserved.
