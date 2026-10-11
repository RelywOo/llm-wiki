# LLM Wiki

Persistent, Git-backed project memory for AI coding agents. Created by [RelywOo](https://github.com/RelywOo).

LLM Wiki separates durable knowledge from the current session state. Agents can resume work from a compact memory packet, preserve decisions with their sources, and checkpoint progress without treating a conversation summary as the project record.

## How memory works

- **Wiki** (`.memory/wiki/`): sourced concepts, immutable decisions, derived navigation pages, and an append-only activity log.
- **STATE** (`.memory/STATE.md`): a short, replaceable description of the current position and remaining work.
- **Git**: version history, checkpoint commits, and integrity checks.

Concept pages evolve in place. Decisions change through forward supersession. Structural tools check links, provenance conventions, freshness, and protected history; agents still assess the meaning and quality of evidence.

## Requirements

Python **3.12 or newer** and Git are required. The core Python tooling uses the standard library, including SQLite FTS5 for search. Prime adapter verification also requires Node.js. Use the coding agent corresponding to the integration you enable.

The installer and updater accept `--portable` for distribution checks on machines without the Prime CLI. This keeps adapter and integrity checks enabled but does not establish live Prime readiness.

Linux is covered by the distribution CI. Windows hook templates are included, but this initial public release has not been tested on Windows or macOS.

## Install in a project

Clone this distribution separately from your target project:

```sh
git clone https://github.com/RelywOo/llm-wiki.git
```

From a target Git repository, run the installer using the path to your clone:

```sh
python /path/to/llm-wiki/code/wiki-init/install.py my-project --codex
```

Choose integrations explicitly:

```sh
# Claude Code
python /path/to/llm-wiki/code/wiki-init/install.py my-project --claude

# Prime Agent
python /path/to/llm-wiki/code/wiki-init/install.py my-project --prime

# All three
python /path/to/llm-wiki/code/wiki-init/install.py my-project --claude --codex --prime
```

All integrations default to **off**. Running without integration flags installs shared memory and Git tooling only. Set a Git author identity before installing; the installer creates a bootstrap commit and installs a local Git hook. Use `--root /path/to/project` to select the target explicitly.

Start with a clean target repository and an empty Git index. The installer refuses existing memory, conflicting integration files, foreign hooks, and unfinished checkpoint jobs. Inspect its report: a nonzero exit can mean installation files were committed but a later health check or optional watchdog activation failed.

For Codex, open the installed project interactively and accept its hook trust prompt. For Prime, start a fresh session or reload extensions. Agent commands are generated in the target project; they do not depend on this distribution's original machine paths.

## Daily use

| Command | Purpose |
| --- | --- |
| `/wiki-resume` | Load session state and relevant project knowledge. |
| `/wiki-checkpoint` | Save durable knowledge, replace STATE, and commit a checkpoint. |
| `/wiki-clear` | Reset STATE after its previous content has been captured safely. |

The agent integrations expose these commands. You can also inspect memory directly:

```sh
python .memory/tools/structural.py search "topic"
python .memory/tools/structural.py resume
python .memory/tools/structural.py guard
python .memory/tools/structural.py doctor
```

Run these inside the installed target project. Use `structural.py help` for the command list. Installation supplies project instructions, including an English default and a `code/` layout convention; review them for your project.

## Reliable decisions and task memory

Before applying a mutable rule, resolve its full decision lineage:

```sh
python .memory/tools/structural.py decision 0008
```

Tracked tasks can use a canonical, sourced event ledger. It preserves required fields and
explicit unknowns, keeps direct dependencies, and computes readiness:

```sh
python .memory/tools/structural.py tasks apply event.json
python .memory/tools/structural.py tasks current
python .memory/tools/structural.py tasks check --complete
```

Exact quotations can be extracted and checked without asking a model to reproduce them:

```sh
python .memory/tools/structural.py evidence quote .memory/research/events.md 1 2 > receipt.json
python .memory/tools/structural.py evidence check receipt.json
# Final delivery: see docs/memory-reliability.md for the manifest format.
python .memory/tools/structural.py evidence publish manifest.json deliverables/answer.json
python .memory/tools/structural.py evidence verify deliverables/answer.json
```

A valid quotation proves its location and freshness; semantic support requires a separate review.
Legacy prose is preserved and requires a source-reviewed migration before it becomes a
structured task record. See [memory reliability](docs/memory-reliability.md) for the event
contract, regression coverage, tradeoffs, and measured limitations.

For an enforced inspection, start the whole agent process through the installed launcher:

```sh
python .memory/tools/readonly_session.py --output /external/inspection -- <agent-command>
```

This requires Linux x86_64/aarch64 with Landlock ABI 6+. Project files and Git storage are
protected throughout the child process tree. Output must be outside the project and its Git
storage; use `--writable` for an external runtime profile and `--network` when needed.
Metadata changes and System V IPC are blocked globally. Existing writers and remote actions
remain outside this local process boundary. See [stage two](docs/memory-reliability-phase2.md).

## Update an installed project

```sh
git -C /path/to/llm-wiki pull --ff-only
python /path/to/llm-wiki/code/wiki-init/update.py --root /path/to/project
```

Updates preserve wiki pages, STATE, raw project inputs, and runtime selections. They refresh managed tools, instructions, and integrations. Adding `--claude`, `--codex`, or `--prime` enables another integration; updating does not uninstall integrations.

## Optional background checkpoints

Foreground operation is the default. The distribution also includes guarded background checkpoint packages and a persistent watchdog. Activating the watchdog with `--watchdog` requires an explicitly enabled Codex integration and a working Linux user systemd session. See the bundled [background procedure](code/wiki-init/templates/tree/.memory/checkpoint-background.md).

Jev inventory is a separate opt-in Typesafe API integration. It requires your own configuration and credentials, can send the selected source material to the API, and is not necessary for ordinary foreground memory use. No credentials are bundled. See the [inventory procedure](code/wiki-init/templates/tree/.memory/checkpoint-inventory.md).

## Development and checks

```sh
python -m unittest discover -s code/wiki-init -p 'test_*.py' -v
python code/wiki-init/update.py --selfcheck
python code/check_distribution.py
```

Tests install into disposable Git repositories. When user systemd is available, watchdog delivery tests create temporary user units and clean them up. No live model API calls are needed for these checks.

The source distribution is under `code/wiki-init/`; templates sit beside the installer and updater. Changes to tooling should be made here and delivered to installed projects through the updater.

A [GitHub Actions workflow template](code/distribution-tests.yml) runs delivery tests and integrity self-checks. To activate it, copy it to `.github/workflows/tests.yml` and push with credentials that permit workflow changes. The initial publication credentials did not include that permission, so hosted CI is not enabled yet.

This initial release imports the maintained reusable distribution. It includes empty scaffold pages, not the author's personal wiki, transcripts, credentials, or social automation. Earlier development history was not imported; public history begins with this release.

## License

MIT. See [LICENSE](LICENSE).
