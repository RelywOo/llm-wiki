#!/usr/bin/env python3
"""PreCompact archiver: copy the session transcript to a safe place BEFORE compaction.

Two reasons: (1) compaction can clear message content in the on-disk .jsonl
(claude-code issue #40352), destroying the only raw record; (2) an archived raw
transcript is the seed for a future transcript-backed checkpoint (raw layer +
sublimation). PreCompact hooks CANNOT influence the summary text (verified against
docs) — a side-effect copy is all this event is good for, and it is enough.

Archive lands NEXT TO the transcript (same Claude projects dir, outside the repo):
<transcript_dir>/pre-compact-archive/<session_id>.<YYYYmmdd-HHMMSS>.jsonl

Always exits 0 — an archiver must never block compaction.

Self-test: `python precompact_archive.py --selfcheck` (assert-based, no framework).
"""
import json
import os
import shutil
import sys
import time


def archive(transcript_path, session_id, stamp=None):
    """Copy transcript into pre-compact-archive/ beside it; return dest path or None."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    dst_dir = os.path.join(os.path.dirname(transcript_path), "pre-compact-archive")
    os.makedirs(dst_dir, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(dst_dir, "%s.%s.jsonl" % (session_id or "unknown", stamp))
    shutil.copy2(transcript_path, dst)
    return dst


def main():
    try:
        data = json.load(sys.stdin)
        archive(data.get("transcript_path"), data.get("session_id"))
    except Exception as e:
        sys.stderr.write("precompact_archive: %s\n" % e)  # visible in hook debug, never blocks


def selfcheck():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="precompact-selfcheck-")
    try:
        src = os.path.join(tmp, "abc.jsonl")
        with open(src, "w") as f:
            f.write('{"line":1}\n{"line":2}\n')
        dst = archive(src, "abc", stamp="20260705-120000")
        assert dst and os.path.isfile(dst), "archive created"
        assert os.path.getsize(dst) == os.path.getsize(src), "byte-identical copy"
        assert os.path.basename(dst) == "abc.20260705-120000.jsonl", "name = session.stamp.jsonl"
        assert archive(os.path.join(tmp, "missing.jsonl"), "x") is None, "missing source -> None"
        assert archive(None, "x") is None, "no path -> None"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
