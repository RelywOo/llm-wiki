"""Canonical append-only task events and computed views. Standard library only."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

LEDGER = 'task-ledger.md'
HEADER = '# Task ledger\n\n'
FIELDS = {'title', 'status', 'owner', 'due', 'priority', 'release_required', 'dependencies'}
STATUSES = {'open', 'in_progress', 'blocked', 'done', 'cancelled'}
ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_path(root, value):
    require(isinstance(value, str) and value, 'source path must be nonempty')
    path = Path(value)
    require(not path.is_absolute() and '..' not in path.parts, 'source must be repository-relative')
    target = root / path
    require(target.resolve().is_relative_to(root.resolve()), 'source escapes repository')
    require(not any(p.is_symlink() for p in [target, *target.parents] if p != root.parent),
            'source symlinks are not supported')
    require(target.is_file(), f'source missing: {value}')
    return target


def render_event(event):
    return (f"## Event {event['seq']}\n```json\n" +
            json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) +
            f"\n```\n\n[source: `{event['source']['path']}`]\n\n")


def parse(text):
    if not text:
        return []
    require(text.startswith(HEADER), 'task ledger header missing')
    events=[]
    for match in re.finditer(r'^```json\n(.*?)\n```$', text, re.M | re.S):
        events.append(json.loads(match.group(1)))
    try:
        canonical = HEADER + ''.join(render_event(e) for e in events)
    except (KeyError, TypeError) as exc:
        raise ValueError(f'invalid task event: {exc}') from exc
    require(text == canonical, 'task ledger has malformed or unrecognized content; use tasks apply')
    return events


def validate_values(values, creating):
    require(isinstance(values, dict), 'values must be an object')
    require(not set(values) - FIELDS, 'unknown task fields: ' + ', '.join(sorted(set(values)-FIELDS)))
    if creating:
        require(set(values) == FIELDS, 'missing required fields: ' + ', '.join(sorted(FIELDS-set(values))))
    require(bool(values), 'empty task update')
    for name, value in values.items():
        if value is None:
            continue  # explicit unknown, never an implicit false/empty/default
        if name in {'title','owner'}:
            require(isinstance(value,str) and bool(value.strip()), f'{name} must be nonempty text or null')
        elif name == 'status':
            require(isinstance(value,str) and value in STATUSES, 'invalid status')
        elif name == 'due':
            require(isinstance(value,str), 'due must be an ISO date or null')
            try: require(date.fromisoformat(value).isoformat() == value, 'due must be YYYY-MM-DD')
            except (ValueError, TypeError) as exc: raise ValueError('due must be YYYY-MM-DD or null') from exc
        elif name == 'priority':
            require(type(value) is int and 0 <= value <= 4, 'priority must be an integer 0..4 or null')
        elif name == 'release_required':
            require(type(value) is bool, 'release_required must be true, false or null')
        elif name == 'dependencies':
            require(isinstance(value,list) and all(isinstance(x,str) and ID.fullmatch(x) for x in value),
                    'dependencies must be task IDs or null')
            require(len(value)==len(set(value)), 'duplicate dependencies')


def reduce_events(events):
    tasks={}
    for number,event in enumerate(events,1):
        require(isinstance(event,dict) and set(event)=={'seq','id','op','values','reason','source'},
                'event requires seq, id, op, values, reason, source')
        require(type(event['seq']) is int and event['seq']==number, f'expected seq {number}')
        key=event['id']
        require(isinstance(key,str) and ID.fullmatch(key), 'invalid task ID')
        require(isinstance(event['reason'],str) and event['reason'].strip(), 'event reason is required')
        source=event['source']
        require(isinstance(source,dict) and set(source)=={'path','quote'} and
                all(isinstance(source[x],str) and source[x].strip() for x in source),
                'source requires path and exact quote')
        require(not any(x in source['path'] for x in ('`','\n','\r')), 'invalid source path')
        op=event['op']
        require(op in {'create','update'}, 'op must be create or update')
        require((key not in tasks) if op=='create' else (key in tasks),
                f'{key}: create requires a new ID; update requires an existing ID')
        validate_values(event['values'], op=='create')
        if op=='create': tasks[key]={'id':key,'values':{},'field_events':{}}
        task=tasks[key]
        task['values'].update(event['values'])
        for field in event['values']:
            task['field_events'][field]={'seq':number,'source':source,'reason':event['reason']}
    return tasks


def validate_transition(before, after, source_exists=None):
    """Pure validator shared by live reads, staged commits and history checks."""
    try:
        require(after.startswith(before), 'task ledger is append-only')
        events=parse(after)
        reduce_events(events)
        if source_exists:
            for event in events:
                require(source_exists(event['source']['path']), f"task source missing: {event['source']['path']}")
        return []
    except (ValueError, TypeError, KeyError) as exc:
        return [f'task ledger: {exc}']


def load(root):
    path=root/'.memory/wiki'/LEDGER
    require(not path.is_symlink(), 'task ledger must be a regular file')
    return path.read_text(encoding='utf-8') if path.exists() else ''


def view(text):
    events=parse(text)
    tasks=reduce_events(events)
    for task in tasks.values():
        task['unknown_fields']=sorted(k for k,v in task['values'].items() if v is None)
        task['complete']=not task['unknown_fields']
    return {'schema':1,'revision':len(events),'digest':hashlib.sha256(text.encode()).hexdigest(),
            'tracked':bool(text),'complete':bool(text) and all(t['complete'] for t in tasks.values()),'tasks':tasks}


def apply(root, event):
    wiki=root/'.memory/wiki'
    require(wiki.is_dir() and not wiki.is_symlink(), 'wiki directory missing or symlinked')
    lock=wiki/'.task-ledger.lock'
    try: fd=os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    except FileExistsError as exc: raise ValueError('task ledger locked; inspect interrupted writer before removing lock') from exc
    os.close(fd)
    temporary=None
    try:
        old=load(root)
        events=parse(old)+[event]
        reduce_events(events)
        source_path(root,event['source']['path'])
        candidate=(old or HEADER)+render_event(event)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=wiki,prefix='.tasks-',delete=False) as f:
            temporary=Path(f.name)
            f.write(candidate);f.flush();os.fsync(f.fileno())
        os.replace(temporary,wiki/LEDGER)
        if os.name=='posix':
            directory=os.open(wiki,os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        return {'revision':len(events),'id':event['id']}
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
        lock.unlink()


def main(root, args):
    usage='tasks apply EVENT.json | show [ID] | check [--complete]'
    require(bool(args),usage)
    if args[0]=='apply' and len(args)==2:
        print(json.dumps(apply(root,json.loads(Path(args[1]).read_text(encoding='utf-8')))))
        return
    require(args[0] in {'show','check'} and len(args)<=2,usage)
    text=load(root)
    for event in parse(text): source_path(root,event['source']['path'])
    output=view(text)
    if args[0]=='show' and len(args)==2:
        require(args[1] in output['tasks'], 'unknown task ID')
        output['tasks']={args[1]:output['tasks'][args[1]]}
        output['complete']=output['tasks'][args[1]]['complete']
    if args[0]=='check':
        require(len(args)==1 or args[1]=='--complete',usage)
        if len(args)==2: require(output['complete'], 'incomplete tasks: unknown fields must be resolved explicitly')
    print(json.dumps(output,ensure_ascii=False,indent=2))
