#!/usr/bin/env python3
"""Deterministic checkpoint supervision. Never publish, retry, cancel, or edit memory.

Linux systemd user timer + desktop alerts; durable inbox is the delivery fallback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import checkpoint_background as bg
from git_checked import git_text, run_git

INTERVAL = 30
STALE = 300
HEALTH_MAX = 120
REPEAT = 900
MESSAGES = {
    'started': 'Started: source boundary recorded; checkpoint is not saved yet.',
    'ready': 'Ready: validated draft awaits guarded publication in the originating conversation.',
    'saved': 'Saved: guarded publication and its transcript cursor were verified.',
    'cancelled': 'Cancelled: source and draft artifacts are retained; this job did not save a checkpoint.',
    'failed': 'Needs attention: the checkpoint failed; source and available draft artifacts are retained.',
    'overdue': 'Needs attention: no checkpoint stage progress for five minutes; inspect the worker. This is not proof of failure.',
    'dispatch_overdue': 'Needs attention: checkpoint preparation or fork binding has not completed within two minutes.',
    'recovery': 'Needs attention: publication was interrupted or is overdue; inspect the transaction before recovery.',
    'worker_ended': 'Needs attention: the worker turn ended without a sealed ready checkpoint.',
    'source_changed': 'Needs attention: source inputs changed; the current checkpoint cannot publish without a fresh attempt.',
    'integrity': 'Needs attention: checkpoint evidence is missing, corrupt, or inconsistent; preserve artifacts and inspect.',
    'journal': 'Needs attention: the checkpoint job journal or reservation cannot be read.',
}


def directory(root):
    return bg.gitdir(root) / 'wiki-background-monitor'


def state_path(root):
    return directory(root) / 'state.json'


def state(root):
    p = state_path(root)
    value = bg.read(p) if p.exists() else {'schema': 1, 'events': {}, 'observations': {}}
    if value.get('schema') != 1 or not isinstance(value.get('events'), dict):
        raise ValueError('Invalid watchdog inbox; preserve it for recovery')
    return value


@contextmanager
def lock(root):
    import fcntl
    path = directory(root) / 'scan.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def require_healthy(root):
    reg = directory(root) / 'registration.json'
    if not reg.exists():
        raise ValueError('Checkpoint supervision is not installed; run checkpoint_watchdog.py install')
    registration = bg.read(reg)
    heartbeat = bg.read(directory(root) / 'heartbeat.json')
    age = time.time() - heartbeat['unix']
    if registration.get('root') != str(root.resolve()) or age < -30 or age > HEALTH_MAX or not heartbeat.get('ok'):
        raise ValueError('Checkpoint watchdog is unhealthy or overdue; inspect checkpoint_watchdog.py health')
    if registration.get('unit') != unit_name(root):
        raise ValueError('Checkpoint watchdog registration does not match this checkout')
    running = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', registration['unit'] + '.timer'],
                             capture_output=True, timeout=10)
    if running.returncode:
        raise ValueError('Checkpoint watchdog timer is not active; a recent heartbeat alone is insufficient')


def event_id(name, code):
    return hashlib.sha256((name + ':' + code).encode()).hexdigest()[:24]


def notice(value, name, code, now, job=None):
    identity = event_id(name, code)
    previous = value['events'].get(identity)
    if previous and not previous.get('active'):
        previous.update(acknowledged=False, transport_accepted=False,
                        last_attempt_unix=-REPEAT)
    item = value['events'].setdefault(identity, {
        'id': identity, 'job': name, 'code': code, 'message': MESSAGES[code],
        'created_unix': now, 'acknowledged': False, 'attempts': 0,
    })
    item.update(active=True, observed_unix=now)
    if job:
        item['owner_thread'] = job.get('owner_thread')
        item['worker_thread'] = job.get('worker_thread')
        item['source_lines'] = [job.get('start_line'), job.get('end_line')]
        item['commit'] = job.get('commit')
        item['worker_commit'] = job.get('worker_commit')
    return identity


def worker_ended(job):
    """Read only observable lifecycle events; no model reasoning or message text.

    Rollout format is a best-effort early signal. The stage deadline remains the
    independent fallback if logs are missing, rotated, or change format.
    """
    worker = job.get('worker_thread')
    if not worker or not all(c in '0123456789abcdef-' for c in worker):
        return False
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    bound = next((e['unix'] for e in reversed(job['events']) if e['state'] == 'running'), 0)
    for path in (home / 'sessions').glob(f'*/*/*/*{worker}.jsonl'):
        try:
            with path.open('rb') as stream:
                meta = json.loads(stream.readline()).get('payload', {})
                if meta.get('id') != worker or meta.get('cwd') != job.get('worker'):
                    continue
                size = path.stat().st_size
                if size > 262144:
                    stream.seek(size - 262144); stream.readline()
                lines = stream.readlines()
            ended = False
            for line in lines:
                try:
                    event = json.loads(line)
                    if event.get('type') != 'event_msg':
                        continue
                    payload = event.get('payload', {})
                    kind = payload.get('type')
                    if kind not in ('task_started', 'task_complete', 'turn_aborted'):
                        continue
                    stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                    if stamp >= bound:
                        ended = kind != 'task_started'
                except (ValueError, KeyError, TypeError):
                    continue
            if ended:
                return True
        except (OSError, ValueError):
            continue
    return False


def classification(root, job, now):
    current = job['state']
    changed = job['events'][-1]['unix']
    if current == 'published':
        commit = job['commit']
        cursor = json.loads(run_git(root, 'show', f'{commit}:{bg.CURSORS}').stdout)
        expected = json.loads(bg.frozen_cursor(job))
        if cursor['threads'][job['thread_id']] != expected['threads'][job['thread_id']]:
            raise ValueError('Published cursor mismatch')
        run_git(root, 'merge-base', '--is-ancestor', commit, job['branch'])
        return 'saved'
    if current in ('failed', 'cancelled'):
        return current
    if current in ('preparing', 'prepared'):
        return 'dispatch_overdue' if now - changed >= 120 else 'started'
    if current == 'publishing':
        return 'recovery' if now - changed >= 120 else None
    if current not in ('running', 'ready'):
        raise ValueError('Unknown checkpoint state')
    bg.verify_inputs(root, job)
    if current == 'ready':
        if bg.digest((bg.jobdir(root, job['name']) / 'publication.json').read_bytes()) != job['publication_sha256']:
            raise ValueError('Publication package digest mismatch')
    try:
        bg.fresh(root, job)
    except (ValueError, OSError, bg.GitIntegrityError):
        return 'source_changed'
    if current == 'ready':
        return 'ready'
    if worker_ended(job):
        return 'worker_ended'
    progress = job.get('progress', {}).get('unix', changed)
    return 'overdue' if now - progress >= STALE else 'started'


def desktop(item):
    """Transport acceptance is not human acknowledgement. No source text is sent."""
    result = subprocess.run([
        'notify-send', '--print-id', '--app-name=Checkpoint watchdog', '--urgency=normal',
        '--expire-time=0', 'Checkpoint ' + html.escape(item['job']),
        html.escape(item['message'] + ' Return to Codex to inspect this checkpoint.'),
    ], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise OSError('Desktop notification transport failed')
    return result.stdout.strip()


def scan(root, *, now=None, deliver=False, sender=desktop):
    now = time.time() if now is None else now
    with lock(root):
        value = state(root)
        active_ids = set()
        seen = set()
        store = bg.store(root)
        # Monitor only jobs enrolled by this version. Historic experiments do not
        # generate retrospective alerts on installation.
        for path in sorted(store.glob('*/job.json')):
            name = path.parent.name
            seen.add(name)
            try:
                job = bg.load(root, name)
                if job.get('monitor_schema') != 1:
                    continue
                # Writers retain their original journal; the observer never updates it.
                code = classification(root, job, now)
                value['observations'][name] = {'state': job['state'], 'unix': now}
                if code:
                    active_ids.add(notice(value, name, code, now, job))
            except (OSError, ValueError, KeyError, TypeError, bg.GitIntegrityError):
                active_ids.add(notice(value, name, 'integrity', now))
        reservation = store / 'active'
        if reservation.exists():
            try:
                name = bg.read(reservation / 'owner.json')['name']
                if not (bg.jobdir(root, name) / 'job.json').exists():
                    raise ValueError('Reserved job has no journal')
            except (OSError, ValueError, KeyError, TypeError):
                # Do not alert during the tiny reservation-before-journal window.
                if now - reservation.stat().st_mtime >= 120:
                    active_ids.add(notice(value, 'reservation', 'journal', now))
        for name, observed in value['observations'].items():
            if name not in seen and observed['state'] not in ('published', 'cancelled'):
                active_ids.add(notice(value, name, 'journal', now))
        for identity, item in value['events'].items():
            item['active'] = identity in active_ids
        # Persist detection BEFORE sending: a crash can cause a duplicate alert,
        # never silently consume an alert that was not delivered.
        bg.write(state_path(root), value)
        if deliver:
            for item in value['events'].values():
                if not item['active'] or item['acknowledged']:
                    continue
                if now - item.get('last_attempt_unix', -REPEAT) < (REPEAT if item.get('transport_accepted') else INTERVAL):
                    continue
                item['attempts'] += 1
                item['last_attempt_unix'] = now
                try:
                    receipt = sender(item)
                    item['transport_accepted'] = True
                    item['transport_receipt'] = receipt
                except (OSError, subprocess.SubprocessError):
                    item['transport_accepted'] = False
                bg.write(state_path(root), value)
        bg.write(directory(root) / 'heartbeat.json', {'unix': now, 'ok': True})
        return [i for i in value['events'].values() if i['active'] and not i['acknowledged']]


def inbox(root):
    return [i for i in state(root)['events'].values() if i['active'] and not i['acknowledged']]


def acknowledge(root, identity):
    with lock(root):
        value = state(root)
        value['events'][identity]['acknowledged'] = True
        value['events'][identity]['acknowledged_unix'] = time.time()
        bg.write(state_path(root), value)


def prompt_notice(root):
    """Read-only fallback for the existing prompt hook and resume procedure."""
    if not (directory(root) / 'registration.json').exists():
        return ''
    lines = []
    try:
        require_healthy(root)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        lines.append('Checkpoint watchdog is not healthy: inspect it; silence is not success.')
    for item in inbox(root):
        lines.append(f"Job {item['job']}: {item['message']} [notice {item['id']}]")
    if not lines:
        return ''
    return ('CHECKPOINT SUPERVISION: report these pending notices to the user. '
            'Inspect job status before any action; never publish, retry, or cancel solely from this notice. '
            'Acknowledge a notice only after reporting it.\n' + '\n'.join(lines))


def unit_name(root):
    return 'jev-checkpoint-watchdog-' + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def systemd_quote(value):
    # systemd uses its own argument parser, not shell quoting. Escape specifiers too.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def pin_runtime(root):
    sources = {}
    for path in sorted((root / '.memory/tools').glob('*.py')):
        if path.is_symlink():
            raise ValueError('Symlink runtime source refused')
        sources[path.name] = path.read_bytes()
    if 'checkpoint_watchdog.py' not in sources:
        raise ValueError('Watchdog runtime source is missing')
    hashes = {name: bg.digest(data) for name, data in sources.items()}
    identity = bg.digest(json.dumps(hashes, sort_keys=True).encode())
    base = directory(root) / 'runtime'; base.mkdir(parents=True, exist_ok=True)
    destination = base / identity
    if not destination.exists():
        temporary = Path(tempfile.mkdtemp(dir=base, prefix='.install-'))
        try:
            for name, data in sources.items():
                (temporary / name).write_bytes(data)
            os.replace(temporary, destination)
        finally:
            if temporary.exists(): shutil.rmtree(temporary)
    for name, expected in hashes.items():
        path = destination / name
        if path.is_symlink() or bg.digest(path.read_bytes()) != expected:
            raise ValueError('Installed watchdog runtime is corrupt; preserve and inspect it')
    return destination / 'checkpoint_watchdog.py'


UNIT_MARKER = '# Managed by JEV checkpoint watchdog\n'


def deployment_preflight(root):
    """Validate host and owned unit paths without writing project or user files."""
    if sys.platform != 'linux':
        raise ValueError('Automatic watchdog installation currently supports Linux user systemd only')
    subprocess.run(['systemctl', '--user', 'show-environment'], check=True, capture_output=True, timeout=10)
    name = unit_name(root)
    units = Path.home() / '.config/systemd/user'
    old = None
    registration = directory(root) / 'registration.json'
    if registration.exists():
        old = bg.read(registration)
        if old.get('schema') != 1 or old.get('unit') != unit_name(Path(old['root'])):
            raise ValueError('Unrecognized watchdog registration; preserve and inspect it')
    for candidate in {name, old['unit'] if old else name}:
        for suffix in ('service', 'timer'):
            path = units / f'{candidate}.{suffix}'
            if path.is_symlink() or (path.exists() and not path.read_text().startswith(UNIT_MARKER)):
                raise ValueError('Refusing to overwrite a foreign systemd unit: ' + str(path))
    if old:
        for state, command in [('enabled', 'is-enabled'), ('active', 'is-active')]:
            result = subprocess.run(['systemctl', '--user', command, '--quiet', old['unit'] + '.timer'],
                                    capture_output=True, timeout=10)
            old[state] = result.returncode == 0
    return old


def install(root, preserve_status=False):
    old = deployment_preflight(root)
    name = unit_name(root)
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    marker = UNIT_MARKER
    runtime = pin_runtime(root)
    service = marker + '[Unit]\nDescription=JEV checkpoint supervision (no LLM)\n\n[Service]\nType=oneshot\n'
    service += 'ExecStart=' + ' '.join(systemd_quote(v) for v in [sys.executable, runtime, '--root', root, 'scan', '--notify']) + '\nTimeoutStartSec=25\n'
    timer = marker + '[Unit]\nDescription=Check pending JEV checkpoints every 30 seconds\n\n[Timer]\nOnStartupSec=15\nOnCalendar=*-*-* *:*:00,30\nPersistent=true\nAccuracySec=1\n\n[Install]\nWantedBy=timers.target\n'
    for suffix, content in [('service', service), ('timer', timer)]:
        path = units / f'{name}.{suffix}'
        path.write_text(content)
    enabled = old['enabled'] if preserve_status and old else True
    active = old['active'] if preserve_status and old else True
    if old and old['unit'] != name:
        # A relocated checkout gets a new unit. Remove only the verified, owned old
        # units so an obsolete timer cannot keep running against the old path.
        subprocess.run(['systemctl', '--user', 'disable', '--now', old['unit'] + '.timer'],
                       check=True, capture_output=True, timeout=10)
        for suffix in ('service', 'timer'):
            (units / f"{old['unit']}.{suffix}").unlink(missing_ok=True)
    bg.write(directory(root) / 'registration.json', {'schema': 1, 'root': str(root.resolve()), 'unit': name, 'runtime': str(runtime)})
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True, timeout=10)
    if enabled:
        subprocess.run(['systemctl', '--user', 'enable', name + '.timer'], check=True, capture_output=True, timeout=10)
    if active:
        subprocess.run(['systemctl', '--user', 'start', name + '.timer'], check=True, capture_output=True, timeout=10)
        subprocess.run(['systemctl', '--user', 'start', name + '.service'], check=True, capture_output=True, timeout=30)
        require_healthy(root)
    return {'timer': name + '.timer', 'interval_seconds': INTERVAL,
            'enabled': enabled, 'active': active, 'preserved_status': preserve_status}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('scan'); s.add_argument('--notify', action='store_true')
    for name in ['install', 'refresh', 'preflight', 'health', 'inbox', 'prompt']:
        sub.add_parser(name)
    a = sub.add_parser('ack'); a.add_argument('--id', required=True)
    args = p.parse_args(); root = args.root.resolve()
    try:
        if args.action == 'scan':
            result = scan(root, deliver=args.notify)
        elif args.action in ('install', 'refresh'):
            if args.action == 'refresh' and not (directory(root) / 'registration.json').exists():
                raise ValueError('No installed watchdog to refresh; use install explicitly')
            result = install(root, preserve_status=args.action == 'refresh')
        elif args.action == 'preflight':
            result = {'previous': deployment_preflight(root), 'supported': True}
        elif args.action == 'health':
            require_healthy(root)
            reg = bg.read(directory(root) / 'registration.json')
            result = {'healthy': True, 'timer': reg['unit'] + '.timer'}
        elif args.action == 'inbox':
            result = inbox(root)
        elif args.action == 'prompt':
            print(prompt_notice(root)); return 0
        else:
            acknowledge(root, args.id); result = {'acknowledged': args.id}
        print(json.dumps(result, indent=2)); return 0
    except BlockingIOError:
        print('A watchdog scan is already running; its lock and pending notices were preserved.')
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, bg.GitIntegrityError) as exc:
        print(f'Checkpoint watchdog needs attention: {exc}', file=sys.stderr)
        if args.action == 'scan' and args.notify:
            try:
                desktop({'job': 'supervision', 'message': 'The watchdog could not inspect its inbox. Return to Codex and check watchdog health; do not assume pending checkpoints were saved.'})
            except (OSError, subprocess.SubprocessError):
                pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
