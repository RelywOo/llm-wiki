"""Exercise a fresh all-runtime installation and its bundled integrity checks."""
from pathlib import Path
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent / "wiki-init"


def run(root, *args):
    print("RUN " + " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=root, check=True)


def main():
    with tempfile.TemporaryDirectory(prefix="llm-wiki-check-") as tmp:
        root = Path(tmp)
        run(root, "git", "init", "-q")
        run(root, "git", "config", "user.name", "LLM Wiki test")
        run(root, "git", "config", "user.email", "wiki-test@example.invalid")
        run(root, sys.executable, SOURCE / "install.py", "distribution-check",
            "--claude", "--codex", "--prime", "--portable")
        for script in ("install_hooks", "pre_commit_guard", "wiki_guard", "git_checked",
                       "wiki_validation", "dual_sync", "consult_nudge", "consult_gate"):
            run(root, sys.executable, f".memory/tools/{script}.py", "--selfcheck")
        run(root, sys.executable, ".memory/tools/checkpoint_txn.py", "selfcheck")
        run(root, sys.executable, ".memory/tools/structural.py", "selfcheck")
        run(root, "node", ".memory/tools/prime_adapter_selfcheck.mjs")
        run(root, sys.executable, ".memory/tools/structural.py", "guard")
        run(root, sys.executable, ".memory/tools/structural.py", "resume")
        run(root, sys.executable, ".memory/tools/structural.py", "doctor", "--portable")
    print("Distribution checks passed.")


if __name__ == "__main__":
    main()
