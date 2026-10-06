#!/usr/bin/env python3
"""Prepare reusable checkpoint edits and validate them before any protected write.

init --name NAME --block BLOCK --summary TEXT --allow PATH [PATH ...]
check --name NAME
apply --name NAME

init prints the JSON package path in Git metadata. Read the target files, fill in
the package's operations, then check/apply. Failed checks and rolled-back commits
leave the package intact. See .memory/checkpoint-package.md for the schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys

import checkpoint_txn as txn
from git_checked import GitIntegrityError, git_text, run_git
import structural

STATE = '.memory/STATE.md'
INDEX = '.memory/wiki/index.md'
LOG = '.memory/wiki/log.md'


class PackageError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise PackageError('unexpected or missing fields; expected: ' + ', '.join(required))


def string(value, label):
    if not isinstance(value, str):
        raise PackageError(label + ' must be a string')
    return value


def target(root, relative):
    string(relative, 'path')
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(p in ('.', '..') or p.lower() == '.git' for p in path.parts)
            or any(c in relative for c in '\\:*?[]') or any(ord(c) < 32 for c in relative)):
        raise PackageError('path must be a literal repository-relative file: ' + relative)
    result = root / relative
    for parent in (result, *result.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise PackageError('symlink path refused: ' + relative)
        if parent.exists() and parent != result and not parent.is_dir():
            raise PackageError('non-directory parent: ' + relative)
    if result.exists() and not stat.S_ISREG(result.stat().st_mode):
        raise PackageError('non-regular file: ' + relative)
    return result


def current_bytes(root, relative):
    path = target(root, relative)
    return path.read_bytes() if path.exists() else None


def package_path(root, name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise PackageError('name must contain 1–64 letters, digits, hyphens or underscores')
    git_dir = Path(git_text(root, 'rev-parse', '--absolute-git-dir'))
    directory = git_dir / 'wiki-checkpoint-packages'
    result = directory / (name + '.json')
    if directory.is_symlink() or result.is_symlink():
        raise PackageError('symlink package storage refused')
    return result


def metadata(root, package):
    block = string(package['block'], 'block')
    summary = string(package['summary'], 'summary')
    if any(ord(c) < 32 for c in block + summary):
        raise PackageError('block and summary must be single-line printable text')
    block = '-'.join(block.split())
    summary = ' '.join(summary.split())
    committed = run_git(root, 'show', f"{package['baseline']}:{LOG}").stdout
    if current_bytes(root, LOG) != committed:
        raise PackageError('log differs from committed baseline')
    session, line = txn.format_checkpoint_log(committed.decode('utf-8'), block, summary)
    return block, summary, session, line


def initialize(root, name, block, summary, paths):
    destination = package_path(root, name)
    if destination.exists():
        raise PackageError('package already exists; edit it or choose another name')
    if len(paths) != len(set(paths)) or LOG in paths:
        raise PackageError('duplicate target or manual log target; log is coordinator-owned')
    if txn._txn_dir(root).exists() or not txn._index_is_clean(root):
        raise PackageError('active transaction or staged changes; resolve before preparing a package')
    package = {'schema': 1, 'baseline': git_text(root, 'rev-parse', 'HEAD'),
               'block': block, 'summary': summary, 'files': []}
    package['block'], package['summary'], _, _ = metadata(root, package)
    for relative in sorted(set(paths) | {STATE, INDEX}):
        data = current_bytes(root, relative)
        package['files'].append({'path': relative, 'before_sha256': sha(data), 'action': 'keep'})
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation: a saved draft is never silently overwritten.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(package, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PackageError('duplicate JSON field: ' + key)
        value[key] = item
    return value


def load(root, name):
    path = package_path(root, name)
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def render(entry, before):
    action = entry['action']
    allowed = {'keep': (), 'write': ('content',), 'edit': ('edits',)}
    if action not in allowed:
        raise PackageError('action must be keep, write or edit')
    keys(entry, ('path', 'before_sha256', 'action', *allowed[action]))
    if action == 'keep':
        if before is None:
            raise PackageError('keep requires an existing file: ' + entry['path'])
        return before
    if action == 'write':
        if before is not None and entry['path'] != STATE:
            raise PackageError('existing files require edit; only STATE is rewritten whole')
        return string(entry['content'], 'content').encode('utf-8')
    if before is None or entry['path'] == STATE:
        raise PackageError('edit requires an existing non-STATE file')
    text = before.decode('utf-8')
    if not isinstance(entry['edits'], list) or not entry['edits']:
        raise PackageError('edits must be a nonempty list')
    for edit in entry['edits']:
        keys(edit, ('old', 'new'))
        old, new = string(edit['old'], 'old'), string(edit['new'], 'new')
        if not old or text.count(old) != 1:
            raise PackageError('edit anchor must match exactly once: ' + entry['path'])
        text = text.replace(old, new, 1)
    return text.encode('utf-8')


def check(root, package, *, active=False):
    """Return rendered bytes and metadata; never write protected files."""
    keys(package, ('schema', 'baseline', 'block', 'summary', 'files'))
    if type(package['schema']) is not int or package['schema'] != 1:
        raise PackageError('unsupported package schema')
    if package['baseline'] != git_text(root, 'rev-parse', 'HEAD'):
        raise PackageError('HEAD changed; review and prepare a fresh package, do not rebase blindly')
    if not active and txn._txn_dir(root).exists():
        raise PackageError('active transaction; inspect status/recover before applying')
    if not txn._index_is_clean(root):
        raise PackageError('real index is dirty')
    block, summary, session, line = metadata(root, package)
    if not isinstance(package['files'], list):
        raise PackageError('files must be a list')
    outputs, seen = {}, set()
    for entry in package['files']:
        keys(entry, ('path', 'before_sha256', 'action'), ('content', 'edits'))
        relative = string(entry['path'], 'path')
        if relative == LOG or relative in seen:
            raise PackageError('duplicate target or manual log target: ' + relative)
        seen.add(relative)
        before = current_bytes(root, relative)
        expected = entry['before_sha256']
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise PackageError('before_sha256 must be a SHA-256 hex digest or null')
        if sha(before) != expected:
            raise PackageError('target changed since preparation: ' + relative)
        if relative.startswith('.memory/wiki/decisions/') and before is not None and entry['action'] != 'keep':
            raise PackageError('filed decision cannot be edited: ' + relative)
        if not isinstance(entry['action'], str):
            raise PackageError('action must be a string')
        outputs[relative] = render(entry, before)
    if STATE not in outputs or outputs[STATE] == current_bytes(root, STATE):
        raise PackageError('STATE must be rewritten in the package')
    if INDEX not in outputs:
        raise PackageError('index.md must be listed, normally as keep or edit')
    sensitive = {p for p in txn._changed_paths(root) if p == STATE or p.startswith('.memory/wiki/')}
    if sensitive - seen:
        raise PackageError('memory changes missing from package: ' + ', '.join(sorted(sensitive - seen)))
    errors = structural._check_state(outputs[STATE].decode('utf-8'))
    headings = structural.toc(outputs[STATE].decode('utf-8'))
    if len(headings) != len(set(headings)):
        errors.append('STATE contains duplicate sections')
    index = outputs[INDEX].decode('utf-8')
    inventory = structural._decision_inventory(root / '.memory/wiki')
    for relative in outputs:
        if relative.startswith('.memory/wiki/decisions/'):
            match = structural.DECISION_FILE.fullmatch(Path(relative).name)
            if not match:
                errors.append('invalid decision filename: ' + relative)
            else:
                did = match.group(1)
                target_rel = relative.removeprefix('.memory/wiki/')
                if did in inventory and inventory[did] != target_rel:
                    errors.append('duplicate decision id: ' + did)
                inventory[did] = target_rel
    _, problems = structural.decision_index_records(index, inventory)
    errors.extend(problems)
    errors.extend('index summary exceeds 100 characters' for line_text in index.splitlines()
                  if line_text.startswith('- ') and len(structural._prose_of(line_text)) > 100)
    overview = outputs.get('.memory/wiki/overview.md')
    if overview is not None:
        errors.extend(structural._check_overview(overview.decode('utf-8')))
    if errors:
        raise PackageError('\n'.join(errors))
    return outputs, block, summary, session, line


def apply(root, package, *, precondition=None):
    outputs, block, summary, _, _ = check(root, package)
    allow = sorted(set(outputs) | txn.MANDATORY_ALLOW)
    txn.begin(root, allow)  # Outside try: never abort somebody else's transaction.
    manifest = None
    writes_started = False
    try:
        # Recheck after acquiring the transaction, including targets changed during BEGIN.
        manifest = txn._load_manifest(txn._txn_dir(root))
        if manifest['head'] != package['baseline']:
            raise PackageError('HEAD changed during preparation')
        outputs, block, summary, _, _ = check(root, package, active=True)
        # Background publication rechecks its complete frozen read set after BEGIN.
        # The caller coordinates other writers for this short apply operation.
        if precondition is not None:
            precondition()
        for entry in package['files']:
            if entry['action'] == 'keep':
                continue
            path = target(root, entry['path'])
            writes_started = True
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(outputs[entry['path']])
    except BaseException:
        if manifest is not None and git_text(root, 'rev-parse', 'HEAD') == manifest['head']:
            if writes_started:
                txn.abort(root)
            else:
                # BEGIN only captured a journal. Restoring it before our first write
                # would overwrite an edit that caused the freshness check to fail.
                shutil.rmtree(txn._txn_dir(root))
        raise
    # The coordinator owns commit failure recovery. Do not interfere with its journal
    # after a possible landed commit or a rollback failure.
    txn.commit(root, block, summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--name', default='draft')
    init.add_argument('--block', required=True)
    init.add_argument('--summary', required=True)
    init.add_argument('--allow', nargs='*', default=[])
    for verb in ('check', 'apply'):
        command = commands.add_parser(verb)
        command.add_argument('--name', default='draft')
    args = parser.parse_args(argv)
    try:
        root = Path(__file__).resolve().parents[2]
        if args.command == 'init':
            path = initialize(root, args.name, args.block, args.summary, args.allow)
            print(f'checkpoint package: {path}\nFill in its operations, then check/apply. Targets are unchanged.')
        else:
            package = load(root, args.name)
            if args.command == 'check':
                outputs, _, _, session, line = check(root, package)
                print(f'package valid: {len(outputs)} files; session {session}\n{line}')
            else:
                apply(root, package)
                print(f'package retained: {package_path(root, args.name)}')
        return 0
    except (PackageError, txn.TransactionError, GitIntegrityError, OSError, UnicodeError, ValueError) as exc:
        print(f'checkpoint package REFUSED: {exc}\nPackage retained; correct it and retry. For an active transaction, inspect status/recover first.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
