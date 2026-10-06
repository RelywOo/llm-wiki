#!/usr/bin/env python3
"""Strict, fail-closed Markdown decoding for memory-system tools.

Policy: Markdown is UTF-8 with no byte-order mark (BOM).  Callers receive a
path-aware ``MarkdownReadError`` rather than silently replacing malformed data.
"""
from __future__ import annotations

from pathlib import Path

_UTF8_BOM = b"\xef\xbb\xbf"


class MarkdownReadError(RuntimeError):
    """A Markdown file cannot be trusted under the UTF-8-no-BOM policy."""


def decode_markdown(raw: bytes, source: str | Path) -> str:
    """Decode filesystem or Git-blob Markdown under the same canonical policy."""
    label = str(source)
    if raw.startswith(_UTF8_BOM):
        raise MarkdownReadError(
            f"{label}: Markdown must be UTF-8 without BOM (UTF-8 BOM found)")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MarkdownReadError(
            f"{label}: Markdown is not valid UTF-8 (invalid byte at offset {exc.start})") from None


def read_markdown(path: str | Path) -> str:
    """Read one Markdown file as strict UTF-8 and reject a leading UTF-8 BOM."""
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise MarkdownReadError(f"{source}: cannot read Markdown: {detail}") from None
    return decode_markdown(raw, source)
