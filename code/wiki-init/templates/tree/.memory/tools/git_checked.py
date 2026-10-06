#!/usr/bin/env python3
"""Checked Git subprocess primitives for integrity-sensitive memory-system code."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitIntegrityError(RuntimeError):
    """Git could not prove an integrity-sensitive result."""


def run_git(root: str | Path, *args: str, input_bytes: bytes | None = None,
            env: dict[str, str] | None = None,
            allowed_rc: frozenset[int] = frozenset({0})) -> subprocess.CompletedProcess:
    cmd = ("git",) + tuple(args)
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, input=input_bytes, env=env)
    except OSError as e:
        raise GitIntegrityError(f"cannot execute {' '.join(cmd)}: {e}") from e
    if proc.returncode not in allowed_rc:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = proc.stdout.decode("utf-8", "replace").strip()
        raise GitIntegrityError(
            f"{' '.join(cmd)} failed with rc={proc.returncode}: {detail or 'no diagnostic'}")
    return proc


def git_text(root: str | Path, *args: str, strip: bool = True,
             env: dict[str, str] | None = None) -> str:
    out = run_git(root, *args, env=env).stdout.decode("utf-8", "replace")
    return out.strip() if strip else out


def git_bytes(root: str | Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    return run_git(root, *args, env=env).stdout


def require_repo(root: str | Path, *, allow_unborn: bool = False) -> None:
    """Prove repository identity and readability; never infer health from blank stdout."""
    root = Path(root).resolve()
    top = Path(git_text(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise GitIntegrityError(f"Git top-level mismatch: expected {root}, got {top}")
    # The object store can be healthy while the shared/private index is corrupt.
    git_text(root, "status", "--porcelain=v1", "--untracked-files=no")
    refs = git_text(root, "rev-list", "--all", "--max-count=1")
    if not refs:
        if allow_unborn:
            return
        raise GitIntegrityError("repository has no reachable commit; bootstrap mode is required")
    # No revision argument: fsck checks HEAD plus all refs and reports corrupt/missing objects.
    git_text(root, "fsck", "--connectivity-only", "--no-dangling")


def selfcheck() -> None:
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="git-checked-selfcheck-"))
    try:
        try:
            require_repo(tmp)
        except GitIntegrityError:
            pass
        else:
            raise RuntimeError("non-repository must be rejected")
        run_git(tmp, "init", "-q")
        (tmp / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        run_git(tmp, "add", "tracked.txt")
        run_git(tmp, "-c", "user.email=git@selfcheck", "-c", "user.name=git-selfcheck",
                "commit", "-qm", "seed")
        require_repo(tmp)
        index = tmp / ".git" / "index"
        index_bytes = index.read_bytes()
        index.write_bytes(b"corrupt-index")
        try:
            require_repo(tmp)
        except GitIntegrityError:
            pass
        else:
            raise RuntimeError("corrupt index must be rejected")
        index.write_bytes(index_bytes)
        obj = git_text(tmp, "rev-parse", "HEAD:tracked.txt")
        loose = tmp / ".git" / "objects" / obj[:2] / obj[2:]
        if not loose.is_file():
            raise RuntimeError("selfcheck expected a loose object")
        loose.unlink()
        try:
            require_repo(tmp)
        except GitIntegrityError:
            pass
        else:
            raise RuntimeError("missing reachable object must be rejected")
        print("selfcheck OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        selfcheck()
