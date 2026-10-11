#!/usr/bin/env python3
"""Install/check an isolated, content-pinned wiki pre-commit runtime.

The tracked hook is only a fail-closed bootstrap notice.  The active hook and every
Python module used by the guard live under Git's private metadata.  Bundle bytes are
read from ``HEAD`` (never from the mutable worktree), hashed into the installed runner,
and Python is started with ``-I``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CORE_RUNTIME_FILES = (
    ".memory/tools/pre_commit_guard.py",
    ".memory/tools/git_checked.py",
    ".memory/tools/wiki_validation.py",
    ".memory/tools/dual_sync.py",
)
OPTIONAL_RUNTIME_FILES = (".memory/tools/markdown_io.py", ".memory/tools/task_memory.py", ".memory/tools/evidence.py", ".memory/tools/readonly_session.py")
RUNTIME_FILES = CORE_RUNTIME_FILES + OPTIONAL_RUNTIME_FILES
INSTALL_NAME = "wiki-memory-hooks"
MARKER = "WIKI_MEMORY_ISOLATED_PRE_COMMIT_V1"
RUNNER_MARKER = "# WIKI_MEMORY_PINNED_RUNNER_V1"
PYTHON_EXE = str(Path(sys.executable).resolve())
_GIT_FOUND = shutil.which("git")
if not _GIT_FOUND:
    raise RuntimeError("git executable is unavailable")
GIT_EXE = str(Path(_GIT_FOUND).resolve())
SAFE_PATH = str(Path(GIT_EXE).parent) + os.pathsep + os.defpath
LEGACY_CORE_HOOKS_PATH = ".memory/hooks"
LEGACY_OWNED_HOOK = """#!/bin/sh
python "$(git rev-parse --show-toplevel)/.memory/tools/pre_commit_guard.py"
"""
LEGACY_MANAGED_HOOK = """#!/bin/sh
# WIKI_MEMORY_MANAGED_PRE_COMMIT
set -eu
export PYTHONDONTWRITEBYTECODE=1
root=$(git rev-parse --show-toplevel)
exec "${PYTHON:-python}" "$root/.memory/tools/pre_commit_guard.py"
"""
BOOTSTRAP_HOOK_TEXT = """#!/bin/sh
# WIKI_MEMORY_TRACKED_BOOTSTRAP_ONLY
printf '%s\\n' 'wiki pre-commit unavailable: run python .memory/tools/install_hooks.py' >&2
exit 2
"""
ACTIVE_HOOK_TEXT = f"""#!/bin/sh
# {MARKER}
set -eu
export PYTHONDONTWRITEBYTECODE=1
export PATH={shlex.quote(SAFE_PATH)}
root=$({shlex.quote(GIT_EXE)} rev-parse --show-toplevel)
git_dir=$({shlex.quote(GIT_EXE)} rev-parse --absolute-git-dir)
exec {shlex.quote(PYTHON_EXE)} -I "$git_dir/{INSTALL_NAME}/runner.py" "$root"
"""


class InstallError(RuntimeError):
    pass


def _run_git(root: Path, *args: str, allowed=(0,)) -> subprocess.CompletedProcess:
    try:
        run = subprocess.run((GIT_EXE,) + args, cwd=root, capture_output=True)
    except OSError as e:
        raise InstallError(f"cannot execute git: {e}") from e
    if run.returncode not in allowed:
        detail = (run.stderr or run.stdout).decode("utf-8", "replace").strip()
        raise InstallError(f"git {' '.join(args)} failed (rc={run.returncode}): {detail}")
    return run


def _git_text(root: Path, *args: str, allowed=(0,)) -> str:
    return _run_git(root, *args, allowed=allowed).stdout.decode("utf-8", "replace").strip()


def _resolve_git_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _repo_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    top = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise InstallError(f"Git top-level mismatch: expected {root.resolve()}, got {top}")
    # Refuse corrupt/unreadable indexes before touching config or private hook state.
    _run_git(root, "status", "--porcelain=v1", "--untracked-files=no")
    if not _git_text(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128)):
        raise InstallError("HEAD is required; make the initial commit before installing hooks")
    _run_git(root, "fsck", "--connectivity-only", "--no-dangling")
    common = _resolve_git_path(root, _git_text(root, "rev-parse", "--git-common-dir"))
    git_dir = Path(_git_text(root, "rev-parse", "--absolute-git-dir")).resolve()
    install = git_dir / INSTALL_NAME
    hooks = common / INSTALL_NAME / "hooks"
    return top, common, git_dir, hooks


def _head_sources(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for rel in RUNTIME_FILES:
        tree = _run_git(root, "ls-tree", "-z", "HEAD", "--", rel).stdout
        if not tree or b"\t" not in tree:
            if rel in OPTIONAL_RUNTIME_FILES:
                continue  # migration: older guards do not import the later optional module
            raise InstallError(f"required runtime is not tracked at HEAD: {rel}")
        meta = tree.split(b"\t", 1)[0].split()
        if len(meta) < 2 or meta[0] not in {b"100644", b"100755"} or meta[1] != b"blob":
            raise InstallError(f"required runtime is not a regular HEAD blob: {rel}")
        out[rel] = _run_git(root, "show", f"HEAD:{rel}").stdout
    return out


def _bundle_spec(sources: dict[str, bytes]) -> tuple[str, dict[str, str], dict[str, bytes]]:
    by_name = {Path(rel).name: data for rel, data in sources.items()}
    if len(by_name) != len(sources):
        raise InstallError("runtime basenames collide")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(by_name.items())}
    aggregate = hashlib.sha256()
    for name, digest in hashes.items():
        aggregate.update(name.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
    return f"bundle-{aggregate.hexdigest()[:24]}", hashes, by_name


def _runner_text(bundle_name: str, hashes: dict[str, str], sources: dict[str, bytes]) -> str:
    expected = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    expected_head = json.dumps(
        {rel: hashlib.sha256(data).hexdigest() for rel, data in sorted(sources.items())},
        sort_keys=True, separators=(",", ":"))
    return f"""#!/usr/bin/env python3
{RUNNER_MARKER}
import hashlib
import os
import runpy
import subprocess
import sys
from pathlib import Path

BUNDLE_NAME = {bundle_name!r}
EXPECTED = {expected}
EXPECTED_HEAD = {expected_head}
GIT_EXE = {GIT_EXE!r}
SAFE_PATH = {SAFE_PATH!r}
sys.dont_write_bytecode = True


def refuse(message):
    sys.stderr.write("wiki pre-commit guard: COMMIT BLOCKED — pinned bundle integrity unavailable: " + message + "\\n")
    return 2


def main():
    if len(sys.argv) != 2:
        return refuse("runner requires exactly one repository root")
    root = Path(sys.argv[1]).resolve()
    bundle = Path(__file__).resolve().parent / BUNDLE_NAME
    try:
        if not bundle.is_dir() or bundle.is_symlink():
            return refuse(f"missing/non-directory bundle {{bundle}}")
        actual = {{p.name for p in bundle.iterdir()}}
        if actual != set(EXPECTED):
            return refuse(f"bundle file set differs: expected {{sorted(EXPECTED)}}, got {{sorted(actual)}}")
        for name, wanted in EXPECTED.items():
            path = bundle / name
            if path.is_symlink() or not path.is_file():
                return refuse(f"{{name}} is not a regular file")
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            if got != wanted:
                return refuse(f"{{name}} digest mismatch")
        for rel, wanted in EXPECTED_HEAD.items():
            read = subprocess.run((GIT_EXE, "show", f"HEAD:{{rel}}"), cwd=root,
                                  capture_output=True)
            got = hashlib.sha256(read.stdout).hexdigest() if read.returncode == 0 else None
            if got != wanted:
                return refuse(f"HEAD runtime differs at {{rel}}; run install_hooks.py")
        entry = bundle / "pre_commit_guard.py"
        os.chdir(root)
        os.environ["PATH"] = SAFE_PATH
        os.environ.pop("PYTHONPATH", None)
        sys.path.insert(0, str(bundle))
        sys.argv = [str(entry)]
        runpy.run_path(str(entry), run_name="__main__")
        return 0
    except (OSError, UnicodeError) as e:
        return refuse(str(e))


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _normalized(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _same_path(raw: str, expected: Path, root: Path) -> bool:
    if not raw:
        return False
    try:
        return _resolve_git_path(root, raw) == expected.resolve()
    except OSError:
        return False


def _legacy_hooks_are_safe(root: Path) -> None:
    raw = _git_text(root, "rev-parse", "--git-path", "hooks")
    directory = _resolve_git_path(root, raw)
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path.name.endswith(".sample") or not path.is_file():
            continue
        if path.name == "pre-commit":
            try:
                text = _normalized(path)
            except OSError as e:
                raise InstallError(f"cannot inspect legacy hook {path}: {e}") from e
            if text in {LEGACY_OWNED_HOOK, LEGACY_MANAGED_HOOK, BOOTSTRAP_HOOK_TEXT}:
                continue
        raise InstallError(
            f"existing hook `{path}` is foreign; changing core.hooksPath would disable it. "
            "Chain/migrate every legacy hook explicitly before bootstrap.")


def _private_hook_dir_is_safe(hooks: Path) -> None:
    if not hooks.is_dir():
        return
    extras = [p.name for p in hooks.iterdir() if p.name != "pre-commit"]
    if extras:
        raise InstallError(f"private managed hook directory contains unexpected hooks: {extras}")
    active = hooks / "pre-commit"
    if active.exists() and _normalized(active) != ACTIVE_HOOK_TEXT:
        raise InstallError(f"installed active hook is corrupt/foreign: {active}")


def _tracked_bootstrap_is_safe(root: Path) -> None:
    hook = root / ".memory/hooks/pre-commit"
    if not hook.is_file() or hook.is_symlink():
        raise InstallError(f"tracked bootstrap hook missing/non-regular: {hook}")
    text = _normalized(hook)
    # Accept the previous managed hook only while upgrading a checkout whose HEAD predates V1.
    head = _run_git(root, "show", "HEAD:.memory/hooks/pre-commit", allowed=(0, 128))
    head_text = head.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    allowed = {BOOTSTRAP_HOOK_TEXT}
    if head_text == LEGACY_MANAGED_HOOK:
        allowed.add(LEGACY_MANAGED_HOOK)
    if text not in allowed:
        raise InstallError(f"tracked bootstrap hook content is corrupt/foreign: {hook}")


def _expected(root: Path):
    top, common, git_dir, hooks = _repo_paths(root)
    sources = _head_sources(root)
    bundle_name, hashes, by_name = _bundle_spec(sources)
    runner = _runner_text(bundle_name, hashes, sources).encode("utf-8")
    return top, common, git_dir, hooks, bundle_name, hashes, by_name, runner


def health(root: Path) -> list[str]:
    """Return exact deployment failures; empty means hook+runner+bundle match HEAD."""
    fails: list[str] = []
    try:
        top, _common, git_dir, hooks, bundle_name, hashes, _by_name, runner = _expected(root)
        current_run = _run_git(top, "config", "--local", "--get", "core.hooksPath",
                               allowed=(0, 1))
        current = current_run.stdout.decode("utf-8", "replace").strip() \
            if current_run.returncode == 0 else ""
        if not _same_path(current, hooks, top):
            fails.append(f"core.hooksPath is {current or '<unset>'}, expected {hooks}")
        active = hooks / "pre-commit"
        if not active.is_file() or active.is_symlink():
            fails.append(f"active hook missing/non-regular: {active}")
        else:
            try:
                if _normalized(active) != ACTIVE_HOOK_TEXT:
                    fails.append(f"active hook content is corrupt: {active}")
                if os.name != "nt" and not os.access(active, os.X_OK):
                    fails.append(f"active hook is not executable: {active}")
            except OSError as e:
                fails.append(f"cannot inspect active hook {active}: {e}")
        runner_path = git_dir / INSTALL_NAME / "runner.py"
        try:
            if runner_path.is_symlink() or runner_path.read_bytes() != runner:
                fails.append(f"pinned runner is missing/corrupt/stale: {runner_path}")
        except OSError as e:
            fails.append(f"pinned runner is missing/unreadable: {runner_path}: {e}")
        bundle = git_dir / INSTALL_NAME / bundle_name
        if bundle.is_symlink() or not bundle.is_dir():
            fails.append(f"pinned bundle is missing/non-directory/symlink: {bundle}")
        try:
            actual = {p.name for p in bundle.iterdir()}
        except OSError as e:
            fails.append(f"pinned bundle is missing/unreadable: {bundle}: {e}")
            actual = set()
        if actual != set(hashes):
            fails.append(f"pinned bundle file set differs: expected {sorted(hashes)}, got {sorted(actual)}")
        for name, wanted in hashes.items():
            path = bundle / name
            try:
                got = hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_symlink() or got != wanted:
                    fails.append(f"pinned bundle digest mismatch: {path}")
            except OSError as e:
                fails.append(f"pinned bundle file unreadable: {path}: {e}")
        try:
            _tracked_bootstrap_is_safe(top)
        except InstallError as e:
            fails.append(str(e))
    except (InstallError, OSError, UnicodeError) as e:
        fails.append(str(e))
    return fails


def install(root: Path) -> None:
    top, _common, git_dir, hooks, bundle_name, hashes, by_name, runner = _expected(root)
    _tracked_bootstrap_is_safe(top)
    probe = _run_git(top, "config", "--local", "--get", "core.hooksPath", allowed=(0, 1))
    current = probe.stdout.decode("utf-8", "replace").strip() if probe.returncode == 0 else ""
    if current and current.replace("\\", "/").rstrip("/") != LEGACY_CORE_HOOKS_PATH \
            and not _same_path(current, hooks, top):
        raise InstallError(f"foreign core.hooksPath `{current}` is configured; refusing to overwrite it")
    if not current or current.replace("\\", "/").rstrip("/") == LEGACY_CORE_HOOKS_PATH:
        _legacy_hooks_are_safe(top)
    _private_hook_dir_is_safe(hooks)

    base = git_dir / INSTALL_NAME
    bundle = base / bundle_name
    if bundle.exists():
        # A content-addressed location must never be silently repaired in place.
        if bundle.is_symlink() or not bundle.is_dir():
            raise InstallError(f"existing content-addressed bundle is not a real directory: {bundle}")
        actual = {p.name for p in bundle.iterdir()}
        if actual != set(hashes):
            raise InstallError(f"existing content-addressed bundle has wrong file set: {bundle}")
        for name, wanted in hashes.items():
            path = bundle / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
                raise InstallError(f"existing content-addressed bundle is corrupt: {path}")
    else:
        base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{bundle_name}.", dir=base))
        try:
            for name, data in by_name.items():
                (staging / name).write_bytes(data)
                os.chmod(staging / name, 0o444)
            # Directory stays owner-writable so repositories/bundles can be removed cleanly;
            # runner digests, not advisory mode bits, are the tamper boundary.
            os.chmod(staging, 0o755)
            os.replace(staging, bundle)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    _write_atomic(base / "runner.py", runner, 0o555)
    _write_atomic(hooks / "pre-commit", ACTIVE_HOOK_TEXT.encode("utf-8"), 0o755)
    _run_git(top, "config", "--local", "core.hooksPath", hooks.as_posix())
    fails = health(top)
    if fails:
        raise InstallError("post-install health failed: " + "; ".join(fails))


def selfcheck() -> None:
    source_root = Path(__file__).resolve().parents[2]
    tmp = Path(tempfile.mkdtemp(prefix="install-hooks-selfcheck-"))
    try:
        for rel in RUNTIME_FILES + (".memory/tools/install_hooks.py", ".memory/hooks/pre-commit"):
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_root / rel, target)
        subprocess.run(("git", "init", "-q"), cwd=tmp, check=True)
        subprocess.run(("git", "config", "user.email", "hooks@selfcheck"), cwd=tmp, check=True)
        subprocess.run(("git", "config", "user.name", "hooks-selfcheck"), cwd=tmp, check=True)
        subprocess.run(("git", "add", "-A"), cwd=tmp, check=True)
        subprocess.run(("git", "commit", "--no-verify", "-qm", "tooling"), cwd=tmp, check=True)

        legacy = tmp / ".git/hooks/pre-commit"
        legacy.write_text("#!/bin/sh\necho FOREIGN-RAN >&2\nexit 77\n", encoding="utf-8")
        legacy.chmod(0o755)

        def invoke(*args):
            return subprocess.run((sys.executable, ".memory/tools/install_hooks.py") + args,
                                  cwd=tmp, capture_output=True, text=True,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

        refused = invoke()
        if refused.returncode != 2 or "foreign" not in refused.stderr.lower():
            raise RuntimeError("foreign legacy pre-commit was not refused")
        legacy.write_text(LEGACY_OWNED_HOOK, encoding="utf-8")
        foreign_commit_msg = tmp / ".git/hooks/commit-msg"
        foreign_commit_msg.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
        foreign_commit_msg.chmod(0o755)
        other_refused = invoke()
        if other_refused.returncode != 2 or "commit-msg" not in other_refused.stderr:
            raise RuntimeError("foreign non-pre-commit legacy hook was not refused")
        foreign_commit_msg.unlink()
        subprocess.run(("git", "config", "--local", "core.hooksPath", LEGACY_CORE_HOOKS_PATH),
                       cwd=tmp, check=True)
        legacy_foreign = tmp / LEGACY_CORE_HOOKS_PATH / "commit-msg"
        legacy_foreign.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
        legacy_foreign.chmod(0o755)
        configured_refused = invoke()
        if configured_refused.returncode != 2 or "commit-msg" not in configured_refused.stderr:
            raise RuntimeError("foreign hook inside configured legacy hooksPath was not refused")
        legacy_foreign.unlink()
        if invoke().returncode != 0 or invoke().returncode != 0 \
                or invoke("--check").returncode != 0:
            raise RuntimeError("clean/legacy migration/idempotent isolated installation failed")

        evidence = tmp / "code/evidence.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("evidence\n", encoding="utf-8")
        decision = tmp / ".memory/wiki/decisions/0001-x.md"
        decision.parent.mkdir(parents=True)
        decision.write_text("---\nid: 0001\n---\nbody\n[source: `code/evidence.txt`]\n", encoding="utf-8")
        log = tmp / ".memory/wiki/log.md"
        log.write_text("2026-01-01 init\n", encoding="utf-8")
        subprocess.run(("git", "add", "-A"), cwd=tmp, check=True)
        seeded = subprocess.run(("git", "commit", "-qm", "seed wiki"), cwd=tmp,
                                capture_output=True, text=True)
        if seeded.returncode != 0:
            raise RuntimeError(f"isolated hook rejected valid seed: {seeded.stderr}")

        # Once a runtime source changes in HEAD, the old runner must stop ordinary commits
        # until explicit reinstall; otherwise an obsolete floor silently remains active.
        validator = tmp / ".memory/tools/wiki_validation.py"
        validator.write_text(validator.read_text(encoding="utf-8") + "\n# runtime update\n",
                             encoding="utf-8")
        subprocess.run(("git", "add", str(validator.relative_to(tmp))), cwd=tmp, check=True)
        subprocess.run(("git", "commit", "--no-verify", "-qm", "runtime update"),
                       cwd=tmp, check=True)
        stale_probe = tmp / "code/stale-probe.txt"
        stale_probe.write_text("must not land\n", encoding="utf-8")
        subprocess.run(("git", "add", str(stale_probe.relative_to(tmp))), cwd=tmp, check=True)
        stale_blocked = subprocess.run(("git", "commit", "-m", "must require reinstall"),
                                       cwd=tmp, capture_output=True, text=True)
        if stale_blocked.returncode == 0 or "HEAD runtime differs" not in stale_blocked.stderr:
            raise RuntimeError("stale installed runtime allowed an ordinary commit: "
                               f"rc={stale_blocked.returncode} stdout={stale_blocked.stdout!r} "
                               f"stderr={stale_blocked.stderr!r}")
        subprocess.run(("git", "reset", "--hard", "-q", "HEAD^"), cwd=tmp, check=True)

        # The exact bypass that motivated V1: both the guard and one of its dependencies are
        # unstaged no-ops, while an immutable decision edit is staged.  The ordinary Git commit
        # must still execute the HEAD-pinned private bundle and deny it.
        (tmp / ".memory/tools/pre_commit_guard.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (tmp / ".memory/tools/git_checked.py").write_text("# no-op dependency\n", encoding="utf-8")
        decision.write_text("---\nid: 0001\n---\nFORBIDDEN EDIT\n", encoding="utf-8")
        subprocess.run(("git", "add", str(decision.relative_to(tmp))), cwd=tmp, check=True)
        blocked = subprocess.run(("git", "commit", "-m", "must block"), cwd=tmp,
                                 capture_output=True, text=True,
                                 env={**os.environ, "PYTHON": os.devnull,
                                      "PYTHONPATH": str(tmp / ".memory/tools")})
        if blocked.returncode == 0 or "COMMIT BLOCKED" not in blocked.stderr:
            raise RuntimeError("mutable worktree guard/dependency bypassed pinned hook")
        if invoke("--check").returncode != 0:
            raise RuntimeError("unstaged worktree no-ops incorrectly changed installed health")

        # Private-metadata corruption is fail-closed at execution and reported by health.
        git_dir = Path(_git_text(tmp, "rev-parse", "--absolute-git-dir"))
        runner_text = (git_dir / INSTALL_NAME / "runner.py").read_text(encoding="utf-8")
        bundle_name = next(line.split("=", 1)[1].strip().strip("'\"") for line in runner_text.splitlines()
                           if line.startswith("BUNDLE_NAME ="))
        dep = git_dir / INSTALL_NAME / bundle_name / "git_checked.py"
        dep.chmod(0o644)
        original = dep.read_bytes()
        dep.write_text("# private no-op\n", encoding="utf-8")
        blocked_corrupt = subprocess.run(("git", "commit", "-m", "must still block"), cwd=tmp,
                                         capture_output=True, text=True)
        if blocked_corrupt.returncode == 0 or "digest mismatch" not in blocked_corrupt.stderr:
            raise RuntimeError("corrupt private dependency did not fail closed")
        checked = invoke("--check")
        if checked.returncode != 2 or "digest mismatch" not in checked.stderr:
            raise RuntimeError("install health did not report private bundle tampering")
        dep.write_bytes(original)
        dep.chmod(0o444)

        active = Path(_git_text(tmp, "rev-parse", "--git-path", "hooks/pre-commit"))
        active_original = active.read_bytes()
        active.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if invoke("--check").returncode != 2:
            raise RuntimeError("install health did not report active-hook tampering")
        active.write_bytes(active_original)
        active.chmod(0o755)
        runner_path = git_dir / INSTALL_NAME / "runner.py"
        runner_original = runner_path.read_bytes()
        runner_path.chmod(0o644)
        runner_path.write_bytes(runner_original + b"# tamper\n")
        if invoke("--check").returncode != 2:
            raise RuntimeError("install health did not report pinned-runner tampering")
        runner_path.write_bytes(runner_original)
        runner_path.chmod(0o555)

        # CI's clean invocation remains an independent backstop for a local --no-verify bypass:
        # the pinned guard audits the newly reachable bad transition, not only the current index.
        bypassed = subprocess.run(("git", "commit", "--no-verify", "-m", "import violation"),
                                  cwd=tmp, capture_output=True, text=True)
        if bypassed.returncode != 0:
            raise RuntimeError(f"could not construct --no-verify regression: {bypassed.stderr}")
        ci_backstop = subprocess.run(("git", "hook", "run", "pre-commit"), cwd=tmp,
                                    capture_output=True, text=True)
        if ci_backstop.returncode == 0 or "history" not in ci_backstop.stderr:
            raise RuntimeError("clean CI hook invocation missed --no-verify history violation")
        subprocess.run(("git", "reset", "--hard", "-q", "HEAD^"), cwd=tmp, check=True)

        # A freshly cloned repository has no local config/artifacts and installs cleanly.
        clone = tmp.parent / (tmp.name + "-clone")
        subprocess.run(("git", "clone", "-q", str(tmp), str(clone)), check=True)
        clean_install = subprocess.run((sys.executable, ".memory/tools/install_hooks.py"), cwd=clone,
                                       capture_output=True, text=True)
        if clean_install.returncode != 0:
            raise RuntimeError(f"clean clone install failed: {clean_install.stderr}")
        clean_health = subprocess.run((sys.executable, ".memory/tools/install_hooks.py", "--check"),
                                      cwd=clone, capture_output=True, text=True)
        if clean_health.returncode != 0:
            raise RuntimeError(f"clean clone health failed: {clean_health.stderr}")
        shutil.rmtree(clone, ignore_errors=True)

        # Linked worktrees share the private hook directory but receive a bundle/runner under
        # their own git-dir, so branches with different guard versions cannot overwrite it.
        linked = tmp.parent / (tmp.name + "-linked")
        subprocess.run(("git", "worktree", "add", "--detach", "-q", str(linked), "HEAD"),
                       cwd=tmp, check=True)
        try:
            linked_install = subprocess.run((sys.executable, ".memory/tools/install_hooks.py"),
                                            cwd=linked, capture_output=True, text=True)
            if linked_install.returncode != 0:
                raise RuntimeError(f"linked worktree install failed: {linked_install.stderr}")
            linked_health = subprocess.run((sys.executable, ".memory/tools/install_hooks.py", "--check"),
                                           cwd=linked, capture_output=True, text=True)
            if linked_health.returncode != 0:
                raise RuntimeError(f"linked worktree health failed: {linked_health.stderr}")
        finally:
            subprocess.run(("git", "worktree", "remove", "--force", str(linked)),
                           cwd=tmp, capture_output=True)
            shutil.rmtree(linked, ignore_errors=True)
        print("selfcheck OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    args = sys.argv[1:]
    if args not in ([], ["--check"], ["--selfcheck"]):
        print("usage: install_hooks.py [--check | --selfcheck]", file=sys.stderr)
        return 2
    try:
        if args == ["--selfcheck"]:
            selfcheck()
        elif args == ["--check"]:
            fails = health(root)
            if fails:
                for failure in fails:
                    print(f"hook health FAILED: {failure}", file=sys.stderr)
                return 2
            print("isolated pinned wiki pre-commit healthy")
        else:
            install(root)
            print(f"isolated pinned wiki pre-commit active ({INSTALL_NAME})")
        return 0
    except (InstallError, OSError, UnicodeError, subprocess.SubprocessError) as e:
        print(f"hook bootstrap REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
