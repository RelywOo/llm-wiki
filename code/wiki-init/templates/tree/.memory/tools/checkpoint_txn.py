#!/usr/bin/env python3
"""Fail-closed, allowlisted transaction for a wiki checkpoint commit.

Usage:
  checkpoint_txn.py begin --allow PATH [PATH ...]
  checkpoint_txn.py commit --block BLOCK --summary TEXT
  checkpoint_txn.py abort
  checkpoint_txn.py recover
  checkpoint_txn.py status
  checkpoint_txn.py selfcheck

`begin` is called immediately before checkpoint-generated writes. It snapshots the protected
worktree in Git metadata. `commit` builds the candidate with a private index and either lands one
verified commit or restores the pre-transaction protected worktree. The caller must explicitly
allow every intended commit path; unrelated worktree files are never staged.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from git_checked import (GitIntegrityError as GitCommandError,
                         git_text as _git_text,
                         require_repo as _git_repo_preflight,
                         run_git as _run_git)

MANDATORY_ALLOW = {".memory/STATE.md", ".memory/wiki/log.md", ".memory/wiki/index.md"}
MANDATORY_CHANGED = {".memory/STATE.md", ".memory/wiki/log.md"}
TXN_NAME = "wiki-checkpoint-txn"


class TransactionError(RuntimeError):
    pass


def _root() -> Path:
    root = Path(__file__).resolve().parents[2]
    _git_repo_preflight(root)
    return root


def _txn_dir(root: Path) -> Path:
    git_dir = Path(_git_text(root, "rev-parse", "--absolute-git-dir"))
    return git_dir / TXN_NAME


def _normalize(root: Path, raw: str) -> str:
    p = Path(raw)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root.resolve())
        except ValueError as e:
            raise TransactionError(f"allowlisted path escapes repository: {raw}") from e
    else:
        p = Path(os.path.normpath(str(p)))
    rel = p.as_posix()
    if rel in ("", ".") or rel == ".." or rel.startswith("../"):
        raise TransactionError(f"invalid allowlisted path: {raw}")
    resolved = (root / rel).resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as e:
        raise TransactionError(f"allowlisted path escapes repository: {raw}") from e
    return rel


def _index_is_clean(root: Path) -> bool:
    return not _git_text(root, "diff", "--cached", "--name-only")


def _snapshot_entry(root: Path, rel: str) -> dict:
    p = root / rel
    if not p.exists() and not p.is_symlink():
        return {"kind": "absent"}
    mode = p.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise TransactionError(f"cannot transact non-regular path: {rel}")
    return {
        "kind": "file",
        "mode": stat.S_IMODE(mode),
        "data": base64.b64encode(p.read_bytes()).decode("ascii"),
    }


def _protected_paths(root: Path, allow: set[str]) -> set[str]:
    wiki = root / ".memory" / "wiki"
    paths = {p.relative_to(root).as_posix() for p in wiki.rglob("*.md")}
    return paths | allow | {".memory/STATE.md"}


def _write_manifest(txn: Path, data: dict) -> None:
    tmp = txn / "manifest.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, txn / "manifest.json")


def _load_manifest(txn: Path) -> dict:
    path = txn / "manifest.json"
    if not path.is_file():
        raise TransactionError(f"active transaction is incomplete: {path} missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TransactionError(f"cannot read transaction manifest: {e}") from e


def begin(root: Path, raw_allow: list[str]) -> None:
    txn = _txn_dir(root)
    try:
        txn.mkdir(mode=0o700)
    except FileExistsError as e:
        raise TransactionError(
            f"checkpoint transaction already active at {txn}; run status or abort") from e
    try:
        if not _index_is_clean(root):
            raise TransactionError("Git index is not clean; commit or unstage it before checkpoint")
        allow = {_normalize(root, p) for p in raw_allow}
        missing = MANDATORY_ALLOW - allow
        if missing:
            raise TransactionError("allowlist must include: " + ", ".join(sorted(missing)))
        head = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}")
        committed_log = _run_git(root, "show", f"{head}:.memory/wiki/log.md").stdout
        if (root / ".memory/wiki/log.md").read_bytes() != committed_log:
            raise TransactionError("log.md already differs from HEAD; restore it before BEGIN")
        protected = _protected_paths(root, allow)
        manifest = {
            "version": 1,
            "head": head,
            "allow": sorted(allow),
            "snapshot": {rel: _snapshot_entry(root, rel) for rel in sorted(protected)},
        }
        _write_manifest(txn, manifest)
        print(f"checkpoint transaction begun: {head[:8]} · {len(allow)} allowlisted path(s)")
    except Exception:
        shutil.rmtree(txn, ignore_errors=True)
        raise


def _restore(root: Path, manifest: dict) -> None:
    snapshot = manifest["snapshot"]
    candidates = set(snapshot)
    wiki = root / ".memory" / "wiki"
    candidates.update(p.relative_to(root).as_posix() for p in wiki.rglob("*.md"))
    candidates.update(manifest["allow"])
    for rel in sorted(candidates, key=lambda x: (x.count("/"), x), reverse=True):
        p = root / rel
        entry = snapshot.get(rel, {"kind": "absent"})
        if entry["kind"] == "absent":
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.exists():
                raise TransactionError(f"rollback refuses to remove directory/special path: {rel}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(entry["data"])
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, entry["mode"])
            os.replace(tmp_name, p)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _changed_paths(root: Path) -> set[str]:
    changed = set()
    for args in (("diff", "--name-only", "-z"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        raw = _run_git(root, *args).stdout
        changed.update(x.decode("utf-8", "surrogateescape")
                       for x in raw.split(b"\0") if x)
    return changed


def format_checkpoint_log(committed: str, block: str, summary: str) -> tuple[int, str]:
    """Validate metadata without writing; shared by package preflight and commit."""
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2} checkpoint \S+ session (\d+) — .+$")
    sessions = [int(m.group(1)) for line in committed.splitlines()
                if (m := pattern.match(line))]
    session = (max(sessions) if sessions else 0) + 1
    clean_block = " ".join(block.split())
    clean_summary = " ".join(summary.split())
    if (not clean_block or not clean_summary
            or any(ord(c) < 32 for c in block + summary)):
        raise TransactionError("block and summary must be non-empty single-line values")
    if any(ch.isspace() for ch in clean_block):
        raise TransactionError("block must be one log-safe token (use hyphens, not spaces)")
    line = f"{date.today().isoformat()} checkpoint {clean_block} session {session} — {clean_summary}"
    if len(line) > 200:
        raise TransactionError(f"generated log line is {len(line)} chars; maximum is 200")
    return session, line


def _append_checkpoint_log(root: Path, manifest: dict, block: str, summary: str) -> tuple[int, str]:
    log_path = root / ".memory/wiki/log.md"
    before = base64.b64decode(manifest["snapshot"][".memory/wiki/log.md"]["data"])
    if log_path.read_bytes() != before:
        raise TransactionError("log.md must not be edited manually; the transaction owns its append")
    committed = _git_text(root, "show", f"{manifest['head']}:.memory/wiki/log.md", strip=False)
    session, line = format_checkpoint_log(committed, block, summary)
    with log_path.open("ab") as f:
        if before and not before.endswith(b"\n"):
            raise TransactionError("existing log.md must end with a newline")
        f.write((line + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    return session, line


def _run_compact(root: Path) -> None:
    proc = subprocess.run(
        (sys.executable, str(root / ".memory/tools/structural.py"),
         "compact", "--budget", "2200"), cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and "manual COMPRESS" not in proc.stdout):
        raise TransactionError("compact failed:\n" + (proc.stdout + proc.stderr).strip())


def _validate_checkpoint_delta(root: Path, manifest: dict) -> None:
    allow = set(manifest["allow"])
    changed = _changed_paths(root)
    sensitive = {p for p in changed
                 if p == ".memory/STATE.md" or p.startswith(".memory/wiki/")}
    outside = sensitive - allow
    if outside:
        raise TransactionError(
            "checkpoint-sensitive changes missing from allowlist: " + ", ".join(sorted(outside)))

    before_state = manifest["snapshot"][".memory/STATE.md"]
    before_log = manifest["snapshot"][".memory/wiki/log.md"]
    if before_state["kind"] != "file" or before_log["kind"] != "file":
        raise TransactionError("STATE.md and log.md must exist before checkpoint")
    state_now = (root / ".memory/STATE.md").read_bytes()
    log_now = (root / ".memory/wiki/log.md").read_bytes()
    state_before = base64.b64decode(before_state["data"])
    log_before = base64.b64decode(before_log["data"])
    if state_now == state_before:
        raise TransactionError("STATE.md was not rewritten after transaction begin")
    if not log_now.startswith(log_before):
        raise TransactionError("log.md is not an append of the transaction baseline")
    tail = log_now[len(log_before):]
    if not tail or not tail.endswith(b"\n") or tail.count(b"\n") != 1:
        raise TransactionError("checkpoint must append exactly one complete log.md line")


def _private_index(root: Path, txn: Path, allow: list[str]) -> tuple[Path, dict[str, str]]:
    index = txn / "index"
    if index.exists():
        index.unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    _run_git(root, "read-tree", "HEAD", env=env)
    _run_git(root, "add", "-A", "--", *allow, env=env)
    return index, env


def _candidate_paths(root: Path, env: dict[str, str]) -> set[str]:
    raw = _run_git(root, "diff", "--cached", "--name-only", "-z", "HEAD", env=env).stdout
    return {x.decode("utf-8", "surrogateescape") for x in raw.split(b"\0") if x}


def _active_hook_path(root: Path, name: str) -> Path:
    path = Path(_git_text(root, "rev-parse", "--git-path", f"hooks/{name}"))
    return path if path.is_absolute() else root / path


def _run_active_hook(root: Path, name: str, *, env: dict[str, str],
                     args: tuple[str, ...] = (), required: bool = False,
                     blocking: bool = True) -> None:
    hook = _active_hook_path(root, name)
    if not hook.is_file():
        if required:
            raise TransactionError(f"required active Git hook missing: {hook}")
        return
    try:
        proc = subprocess.run(("git", "hook", "run", name, "--", *args), cwd=root,
                              capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired as e:
        if blocking:
            raise TransactionError(f"Git {name} hook timed out after 120s") from e
        print(f"warning: post-commit hook timed out: {e}", file=sys.stderr)
        return
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        if blocking:
            raise TransactionError(f"Git {name} hook rejected checkpoint: {detail}")
        print(f"warning: Git {name} hook failed after commit: {detail}", file=sys.stderr)


def commit(root: Path, block: str, summary: str) -> None:
    txn = _txn_dir(root)
    manifest = _load_manifest(txn)
    committed = False
    try:
        if _git_text(root, "rev-parse", "--verify", "HEAD^{commit}") != manifest["head"]:
            raise TransactionError("HEAD moved after transaction begin; refusing mixed checkpoint")
        if not _index_is_clean(root):
            raise TransactionError("real Git index changed after transaction begin")
        _run_compact(root)
        session, _log_line = _append_checkpoint_log(root, manifest, block, summary)
        _validate_checkpoint_delta(root, manifest)
        _index, env = _private_index(root, txn, manifest["allow"])
        candidate = _candidate_paths(root, env)
        if not candidate:
            raise TransactionError("candidate checkpoint is empty")
        if not MANDATORY_CHANGED.issubset(candidate):
            raise TransactionError("candidate must contain changed STATE.md and log.md")
        outside = candidate - set(manifest["allow"])
        if outside:
            raise TransactionError("private index captured non-allowlisted paths: "
                                   + ", ".join(sorted(outside)))

        # Worktree guard validates the complete wiki shape; the pre-commit hook then validates
        # the exact private-index candidate. Both failures are transactional failures.
        _run_git(root, "diff", "--cached", "--check", "HEAD", env=env)
        try:
            guard = subprocess.run(
                (sys.executable, str(root / ".memory/tools/structural.py"), "guard"),
                cwd=root, capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired as e:
            raise TransactionError("structural guard timed out after 120s") from e
        if guard.returncode != 0:
            raise TransactionError("structural guard rejected candidate:\n" +
                                   (guard.stdout + guard.stderr).strip())
        _run_active_hook(root, "pre-commit", env=env, required=True)

        message = f"checkpoint: {' '.join(block.split())} session {session} — {' '.join(summary.split())}"
        message_path = txn / "COMMIT_EDITMSG"
        message_path.write_text(message + "\n", encoding="utf-8", newline="\n")
        _run_active_hook(root, "prepare-commit-msg", env=env,
                         args=(str(message_path), "message"))
        _run_active_hook(root, "commit-msg", env=env, args=(str(message_path),))
        message = message_path.read_text(encoding="utf-8").strip()
        if not message:
            raise TransactionError("commit hooks produced an empty commit message")
        tree = _git_text(root, "write-tree", env=env)
        new_head = _run_git(
            root, "commit-tree", tree, "-p", manifest["head"],
            input_bytes=(message + "\n").encode("utf-8")).stdout.decode("ascii").strip()
        # Atomic compare-and-swap: a concurrent HEAD move cannot be incorporated silently.
        _run_git(root, "update-ref", "-m", message, "HEAD", new_head, manifest["head"])
        committed = True
        parent = _git_text(root, "show", "-s", "--format=%P", new_head).split()
        if not parent or parent[0] != manifest["head"]:
            raise TransactionError("checkpoint commit parent is not the transaction baseline")
        _run_active_hook(root, "post-commit", env=env, blocking=False)
        # The real index was required clean. Advance it to the new HEAD without touching unrelated
        # worktree edits, which remain visible as ordinary unstaged changes.
        _run_git(root, "read-tree", "HEAD")
        shutil.rmtree(txn)
        print(f"checkpoint committed transactionally: {new_head[:8]}")
    except Exception as e:
        if not committed:
            try:
                _restore(root, manifest)
                shutil.rmtree(txn, ignore_errors=True)
            except Exception as rollback_error:
                raise TransactionError(
                    f"checkpoint failed ({e}); ROLLBACK ALSO FAILED: {rollback_error}; "
                    f"recovery data remains at {txn}") from rollback_error
            raise TransactionError(f"checkpoint failed and protected writes were rolled back: {e}") from e
        raise


def abort(root: Path) -> None:
    txn = _txn_dir(root)
    manifest = _load_manifest(txn)
    if _git_text(root, "rev-parse", "--verify", "HEAD^{commit}") != manifest["head"]:
        raise TransactionError("HEAD moved; refusing automatic abort after a possible commit")
    _restore(root, manifest)
    shutil.rmtree(txn)
    print("checkpoint transaction aborted; protected worktree restored")


def recover(root: Path) -> None:
    """Deterministically finish or roll back an interrupted transaction journal."""
    txn = _txn_dir(root)
    manifest = _load_manifest(txn)
    current = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}")
    if current == manifest["head"]:
        _restore(root, manifest)
        shutil.rmtree(txn)
        print("checkpoint recovery: no commit landed; protected worktree rolled back")
        return
    parents = _git_text(root, "show", "-s", "--format=%P", current).split()
    if not parents or parents[0] != manifest["head"]:
        raise TransactionError(
            f"cannot recover: current HEAD {current[:8]} is not a child of baseline "
            f"{manifest['head'][:8]}")
    raw = _run_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", current).stdout
    landed = {x.decode("utf-8", "surrogateescape") for x in raw.split(b"\0") if x}
    outside = landed - set(manifest["allow"])
    if outside or not MANDATORY_CHANGED.issubset(landed):
        raise TransactionError(
            "landed child does not match transaction manifest: "
            + ("outside=" + ",".join(sorted(outside)) if outside else "mandatory paths missing"))
    _run_git(root, "read-tree", "HEAD")
    shutil.rmtree(txn)
    print(f"checkpoint recovery: landed commit {current[:8]} verified; real index synchronized")


def status(root: Path) -> None:
    txn = _txn_dir(root)
    if not txn.exists():
        print("checkpoint transaction: none")
        return
    manifest = _load_manifest(txn)
    current = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}")
    print(f"checkpoint transaction: ACTIVE · baseline {manifest['head'][:8]} · "
          f"current {current[:8]} · {len(manifest['allow'])} allowlisted path(s)")


def selfcheck() -> None:
    """Integration proof: exact allowlist success, rollback, staged-index refusal, recovery."""
    import hashlib

    source_root = Path(__file__).resolve().parents[2]
    tmp = Path(tempfile.mkdtemp(prefix="checkpoint-txn-selfcheck-"))
    project = tmp / "project"

    def run(*args, expect=0):
        proc = subprocess.run(args, cwd=project, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if proc.returncode != expect:
            raise RuntimeError(
                f"selfcheck command rc={proc.returncode}, expected {expect}: {' '.join(args)}\n"
                + (proc.stdout + proc.stderr))
        return proc

    def digest(rel):
        return hashlib.sha256((project / rel).read_bytes()).hexdigest()

    try:
        shutil.copytree(source_root, project,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        run("git", "init", "-q")
        run("git", "config", "user.email", "txn@selfcheck")
        run("git", "config", "user.name", "txn-selfcheck")
        unrelated = project / "code" / "unrelated.txt"
        unrelated.parent.mkdir(exist_ok=True)
        unrelated.write_text("baseline\n", encoding="utf-8")
        # Selfcheck inputs belong to this disposable fixture, not the caller's STATE.
        # Valid projects can omit Now or already use the whole STATE line budget.
        state = project / ".memory" / "STATE.md"
        state.write_text("# STATE\n\n## Now\n- Synthetic transaction fixture.\n", encoding="utf-8")
        run("git", "add", "-A")
        run("git", "commit", "--no-verify", "-qm", "baseline")
        py = sys.executable
        run(py, ".memory/tools/install_hooks.py")
        unrelated.write_text("unrelated trailing whitespace   \n", encoding="utf-8")
        hook_raw = run("git", "rev-parse", "--git-path", "hooks/commit-msg").stdout.strip()
        commit_msg_hook = Path(hook_raw)
        if not commit_msg_hook.is_absolute():
            commit_msg_hook = project / commit_msg_hook
        commit_msg_hook.parent.mkdir(parents=True, exist_ok=True)
        commit_msg_hook.write_text("#!/bin/sh\necho checkpoint-hook-ran >> \"$1\"\n",
                                   encoding="utf-8")
        commit_msg_hook.chmod(0o755)

        work = project / "code" / "txn-acceptance.txt"
        work.parent.mkdir(exist_ok=True)
        work.write_text("session work\n", encoding="utf-8")
        allow = [".memory/STATE.md", ".memory/wiki/log.md", ".memory/wiki/index.md",
                 "code/txn-acceptance.txt"]
        run(py, ".memory/tools/checkpoint_txn.py", "begin", "--allow", *allow)
        journal = (project / ".git" / TXN_NAME / "manifest.json").read_bytes()
        state.write_text(state.read_text(encoding="utf-8").replace(
            "## Now\n", "## Now\n- transaction selfcheck\n", 1), encoding="utf-8")
        run(py, ".memory/tools/checkpoint_txn.py", "commit", "--block", "txn-selfcheck",
            "--summary", "transaction acceptance")
        landed = set(_git_text(project, "diff-tree", "--no-commit-id", "--name-only", "-r",
                              "HEAD").splitlines())
        expected = {".memory/STATE.md", ".memory/wiki/log.md", "code/txn-acceptance.txt"}
        if landed != expected:
            raise RuntimeError(f"transaction landed wrong paths: {landed} != {expected}")
        if unrelated.read_text(encoding="utf-8") != "unrelated trailing whitespace   \n":
            raise RuntimeError("transaction changed unrelated dirty worktree content")
        if "checkpoint-hook-ran" not in _git_text(project, "show", "-s", "--format=%B", "HEAD"):
            raise RuntimeError("transaction bypassed active commit-msg hook")

        # Simulate a crash after commit but before real-index synchronization/cleanup.
        journal_dir = project / ".git" / TXN_NAME
        journal_dir.mkdir()
        (journal_dir / "manifest.json").write_bytes(journal)
        run("git", "read-tree", "HEAD^")
        run(py, ".memory/tools/checkpoint_txn.py", "recover")
        if journal_dir.exists() or _git_text(project, "diff", "--cached", "--name-only"):
            raise RuntimeError("post-commit recovery did not synchronize/clean the real index")

        # Existing staged material blocks begin and is left byte-identical in the index.
        staged = project / "staged-secret.txt"
        staged.write_text("do not capture\n", encoding="utf-8")
        run("git", "add", "staged-secret.txt")
        before_index = _git_text(project, "diff", "--cached", "--name-only")
        run(py, ".memory/tools/checkpoint_txn.py", "begin", "--allow", *allow, expect=2)
        if _git_text(project, "diff", "--cached", "--name-only") != before_index:
            raise RuntimeError("refused begin changed the real index")
        run("git", "reset", "-q", "HEAD", "--", "staged-secret.txt")
        staged.unlink()

        # A post-BEGIN undeclared wiki edit forces rollback to the exact protected preimages.
        rollback_work = project / "code" / "rollback.txt"
        rollback_work.write_text("preserve pre-begin work\n", encoding="utf-8")
        allow2 = [".memory/STATE.md", ".memory/wiki/log.md", ".memory/wiki/index.md",
                  "code/rollback.txt"]
        state_hash, overview_hash = digest(".memory/STATE.md"), digest(".memory/wiki/overview.md")
        head = _git_text(project, "rev-parse", "HEAD")
        run(py, ".memory/tools/checkpoint_txn.py", "begin", "--allow", *allow2)
        state.write_text(state.read_text(encoding="utf-8").replace(
            "## Now\n", "## Now\n- rollback mutation\n", 1), encoding="utf-8")
        overview = project / ".memory" / "wiki" / "overview.md"
        overview.write_text(overview.read_text(encoding="utf-8") + "\nundeclared\n",
                            encoding="utf-8")
        run(py, ".memory/tools/checkpoint_txn.py", "commit", "--block", "txn-selfcheck",
            "--summary", "must roll back", expect=2)
        if _git_text(project, "rev-parse", "HEAD") != head:
            raise RuntimeError("failed transaction moved HEAD")
        if digest(".memory/STATE.md") != state_hash or digest(".memory/wiki/overview.md") != overview_hash:
            raise RuntimeError("failed transaction did not restore protected preimages")
        if rollback_work.read_text(encoding="utf-8") != "preserve pre-begin work\n":
            raise RuntimeError("rollback destroyed pre-BEGIN session work")
        print("selfcheck OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_begin = sub.add_parser("begin")
    p_begin.add_argument("--allow", nargs="+", required=True)
    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--block", required=True)
    p_commit.add_argument("--summary", required=True)
    sub.add_parser("abort")
    sub.add_parser("recover")
    sub.add_parser("status")
    sub.add_parser("selfcheck")
    args = parser.parse_args(argv)
    try:
        if args.command == "selfcheck":
            selfcheck()
            return 0
        root = _root()
        if args.command == "begin":
            begin(root, args.allow)
        elif args.command == "commit":
            commit(root, args.block, args.summary)
        elif args.command == "abort":
            abort(root)
        elif args.command == "recover":
            recover(root)
        else:
            status(root)
        return 0
    except (GitCommandError, TransactionError, OSError) as e:
        sys.stderr.write(f"checkpoint transaction REFUSED: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
