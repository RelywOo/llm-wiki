#!/usr/bin/env python3
"""Experimental read-only checkpoint context collector; stdout is the packet.

No inference, retention decisions, hooks, network calls, or persistent cache.
Requires the project's existing structural.py and checkpoint_txn.py tools.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run(root, args):
    result = subprocess.run(args, cwd=root, capture_output=True, timeout=60,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0",
                                 "PYTHONDONTWRITEBYTECODE": "1"})
    if result.returncode:
        raise RuntimeError(f"{args!r}: {result.stderr.decode(errors='replace')}")
    return result.stdout


def git(root, *args):
    return run(root, ["git", *args])


def names(raw):
    return [os.fsdecode(p) for p in raw.split(b"\0") if p]


def file_version(root, name):
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        return {"kind": "symlink", "target": os.readlink(path) if path.is_symlink() else None}
    if not path.exists():
        return {"kind": "missing"}
    if not path.is_file():
        return {"kind": "directory"}
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            h.update(block)
    return {"kind": "file", "sha256": h.hexdigest(), "bytes": path.stat().st_size}


def collect(root: Path, item_limit=24000, total_limit=100000):
    started = time.perf_counter()
    root = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    head = git(root, "rev-parse", "HEAD").decode().strip()
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    boundary = git(root, "log", "-1", "--format=%H", "--", ".memory/STATE.md").decode().strip()
    fallback = not bool(boundary)
    if fallback:
        boundary = git(root, "rev-list", "--max-parents=0", "HEAD").decode().splitlines()[0]
    tracked = names(git(root, "ls-files", "-z"))
    untracked = names(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    changed = {}
    for label, args in (
        ("committed", (f"{boundary}..HEAD",)),
        ("staged", ("--cached",)),
        ("unstaged", ())):
        changed[label] = names(git(root, "diff", "--name-only", "--no-renames", "-z", *args))
    changed["untracked"] = untracked
    instruction_paths = {p for p in tracked + untracked if Path(p).name == "AGENTS.md"}
    fixed = ["AGENTS.md", ".memory/runtimes.json", ".memory/commands/wiki-checkpoint.md",
             ".memory/STATE.md", ".memory/wiki/overview.md", ".memory/wiki/index.md"]
    scope = sorted(set(fixed) | instruction_paths | set().union(*map(set, changed.values())))
    versions = {p: file_version(root, p) for p in scope}
    items, omissions = [], []
    remaining = total_limit

    def add(label, data):
        nonlocal remaining
        budget = max(0, min(item_limit, remaining))
        kept = data[:budget]
        remaining -= len(kept)
        items.append({"source": label, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                      "truncated": len(kept) < len(data), "text": kept.decode("utf-8", errors="replace")})
        if len(kept) < len(data):
            omissions.append({"source": label, "reason": "content budget", "omitted_bytes": len(data)-len(kept)})

    for tool, command in [("structural.py", "checkpoint"), ("checkpoint_txn.py", "status"),
                          ("structural.py", "active"), ("structural.py", "flags")]:
        args = [sys.executable, f".memory/tools/{tool}", command]
        add(" ".join(args[1:]), run(root, args))
    for name in dict.fromkeys(fixed + sorted(instruction_paths)):
        if versions[name]["kind"] == "file":
            add(name, (root / name).read_bytes())
        else:
            omissions.append({"source": name, "reason": versions[name]["kind"]})
    add("git log checkpoint..HEAD", git(root, "log", "--format=%H %s", f"{boundary}..HEAD"))
    for label, args in [("committed", (f"{boundary}..HEAD",)), ("staged", ("--cached",)), ("unstaged", ())]:
        add(f"git diff {label}", git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", *args))
    for name in untracked:
        # Metadata is always included. Only public text in project source/input locations is opened.
        allowed = name.startswith(("code/", ".memory/specs/", ".memory/research/", ".memory/brainstorms/"))
        allowed = allowed and "/private/" not in name and "/results/" not in name
        allowed = allowed and Path(name).suffix in {".md", ".py", ".txt", ".json", ".toml"}
        if allowed and versions[name]["kind"] == "file":
            data = (root / name).read_bytes()
            if b"\0" not in data:
                add(name, data)
                continue
        omissions.append({"source": name, "reason": "untracked content outside text allowlist"})
    drift = [p for p in scope if versions[p] != file_version(root, p)]
    if git(root, "rev-parse", "HEAD").decode().strip() != head:
        drift.append("HEAD")
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") != status:
        drift.append("git status")
    return {"schema": 1, "root": str(root), "head": head, "checkpoint": boundary,
            "boundary_fallback_to_root": fallback, "changes": changed,
            "versions": versions, "items": items, "omissions": omissions,
            "drift": drift, "consistent": not drift,
            "limits": {"per_item_bytes": item_limit, "content_bytes": total_limit},
            "scope_note": "Conversation not collected. Candidate-page search and evidence assessment remain agent work. "
                          "Ignored files excluded. Binary diff bodies are not collected. No retention or readiness verdict.",
            "elapsed_seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--item-bytes", type=int, default=24000)
    parser.add_argument("--total-bytes", type=int, default=100000)
    args = parser.parse_args()
    if args.item_bytes < 1 or args.total_bytes < 1:
        parser.error("byte limits must be positive")
    try:
        packet = collect(args.root, args.item_bytes, args.total_bytes)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, ensure_ascii=True, indent=2))
    return 0 if packet["consistent"] else 2


if __name__ == "__main__":
    sys.exit(main())
