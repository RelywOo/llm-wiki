# Reliability development experiments

These are diagnostic, paired fresh-session experiments, not a large statistical benchmark.
The regression suite does not require a model:

```sh
python -m unittest discover -s code/wiki-init -p 'test_*.py' -v
python code/check_distribution.py
python code/wiki-init/update.py --selfcheck
```

Model experiments require Linux with Landlock ABI 6+, Python 3.12+, Git, and the logged-in
Codex CLI at `~/.local/bin/codex`. The checked runtime uses `gpt-6-luna`, medium reasoning,
and a temporary profile referencing the normal auth file without copying it. It disables
user configuration, plugins, persistent model memory and other agents. No API fallback is
configured. Existing ChatGPT/Codex usage is consumed. A model call is never replaced by a
mock if access fails. Auth symlinks are removed on normal completion; after an interrupted
operator process inspect/remove only the experiment's own `profile/auth.json` symlinks.

Create a baseline source export outside the repository, then run sequentially:

```sh
mkdir -p /tmp/wiki-baseline
# Run from the distribution repository root.
git archive aeca761 | tar -x -C /tmp/wiki-baseline
python code/reliability-evaluation/run_decisions.py --baseline /tmp/wiki-baseline --output /tmp/wiki-eval-decisions
python code/reliability-evaluation/run_tasks.py --baseline /tmp/wiki-baseline --stage 2 --output /tmp/wiki-eval-fields
python code/reliability-evaluation/run_tasks.py --baseline /tmp/wiki-baseline --stage 3 --output /tmp/wiki-eval-edges
python code/reliability-evaluation/run_tasks.py --baseline /tmp/wiki-baseline --stage 4 --output /tmp/wiki-eval-views
python code/reliability-evaluation/run_evidence.py --baseline /tmp/wiki-baseline --output /tmp/wiki-eval-evidence
python code/reliability-evaluation/smoke.py --output /tmp/wiki-eval-final
```

Use a fresh output directory for a new experiment. Completed cases are retained on resume;
do not treat a resumed directory as an independent replication. The optional `--runtime`
argument selects another compatible isolation harness. Raw prompts, stdout events, results,
CLI usage and outputs remain outside the source distribution. No expected answers enter
subject workspaces. Learning and evaluation use separate conversations; evaluation memory
is read-only, with only `code/` and `deliverables/` writable.

`replay_decisions.py` additionally accepts the original local comparison artifact directory
via `--experiment-root`, a baseline export and an output path. It clones the original
learned snapshot, applies the appropriate updater, verifies unchanged knowledge, and reuses
the original prompt and private scorer. Those earlier artifacts are an explicit external
dependency; they are not bundled as a personal knowledge base in this public repository.

The development evidence experiment retained three strict output-shape deviations while
separately scoring unambiguous nested `verdict` booleans for semantic content in both arms.
A relative-path error in its first receipt scorer was corrected without model reruns;
initial artifacts and the correction remain in the local report directory. The checked-in
runner uses the corrected absolute path. Timing was not isolated from background local
checks, and sample sizes are too small to claim model-accuracy or speed superiority.
