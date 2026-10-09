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
        require(op in {'create','update','link','unlink'}, 'op must be create, update, link or unlink')
        require((key not in tasks) if op=='create' else (key in tasks),
                f'{key}: create requires a new ID; update requires an existing ID')
        if op in {'link','unlink'}:
            change=event['values']
            require(isinstance(change,dict) and set(change)=={'target','kind'}, 'link/unlink requires target and kind')
            require(change['kind'] in {'hard','soft'}, 'dependency kind must be hard or soft')
            require(isinstance(change['target'],str) and ID.fullmatch(change['target']), 'invalid dependency target')
            task=tasks[key]
            field='dependencies' if change['kind']=='hard' else 'advisory_dependencies'
            links=task['values'][field] if field=='dependencies' else task[field]
            require(links is not None, 'resolve unknown dependencies before changing an edge')
            target=change['target']
            require((target not in links) if op=='link' else (target in links), 'edge already present or absent')
            if op=='link': links.append(target)
            else: links.remove(target)
            links.sort()
            edge_key=change['kind']+':'+target
            task['edge_events'][edge_key]={'seq':number,'source':source,'reason':event['reason'],'op':op}
            task['field_events'][field]={'seq':number,'source':source,'reason':event['reason']}
            continue
        validate_values(event['values'], op=='create')
        if op=='update' and 'dependencies' in event['values']:
            require(tasks[key]['values']['dependencies'] is None,
                    'use link/unlink with a specific pair; replacing known dependencies is forbidden')
        if op=='create': tasks[key]={'id':key,'values':{},'field_events':{},'advisory_dependencies':[],'edge_events':{}}
        task=tasks[key]
        task['values'].update(json.loads(json.dumps(event['values'])))
        for field in event['values']:
            task['field_events'][field]={'seq':number,'source':source,'reason':event['reason']}
        if 'dependencies' in event['values'] and event['values']['dependencies'] is not None:
            for target in event['values']['dependencies']:
                task['edge_events']['hard:'+target]={'seq':number,'source':source,'reason':event['reason'],'op':'link'}
    validate_graph(tasks)
    return tasks


def validate_graph(tasks):
    for key,task in tasks.items():
        for target in (task['values']['dependencies'] or []) + task['advisory_dependencies']:
            require(target in tasks, f'{key}: unknown dependency target {target}')
            require(target != key, f'{key}: self dependency')
    # Iterative topological check: never replace the stored graph with its transitive reduction.
    pending={k:set(t['values']['dependencies'] or []) for k,t in tasks.items()}
    reverse={k:[] for k in tasks}
    for key,deps in pending.items():
        for target in deps: reverse[target].append(key)
    ready=[k for k,deps in pending.items() if not deps]
    count=0
    while ready:
        key=ready.pop();count+=1
        for child in reverse[key]:
            pending[child].remove(key)
            if not pending[child]:ready.append(child)
    require(count==len(tasks),'hard dependency cycle')


def readiness(task, tasks):
    values=task['values']
    if values['status'] is None: return {'state':'unknown','blocked_by':[]}
    if values['status']!='open': return {'state':'not_open','blocked_by':[]}
    if values['dependencies'] is None: return {'state':'unknown','blocked_by':[]}
    blocked=[k for k in values['dependencies'] if tasks[k]['values']['status'] not in {'done',None}]
    unknown=[k for k in values['dependencies'] if tasks[k]['values']['status'] is None]
    return {'state':'blocked' if blocked else ('unknown' if unknown else 'ready'),
            'blocked_by':sorted(blocked),'unknown_dependencies':sorted(unknown)}


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
        task['readiness']=readiness(task,tasks)
    return {'schema':1,'revision':len(events),'digest':hashlib.sha256(text.encode()).hexdigest(),
            'ready_now':sorted(k for k,t in tasks.items() if t['readiness']['state']=='ready'),
            'tracked':bool(text),'complete':bool(text) and all(t['complete'] for t in tasks.values()),'tasks':tasks}


def current(text, selected=None, revision=None):
    events=parse(text)
    if revision is not None:
        require(type(revision) is int and 0 <= revision <= len(events), 'revision outside ledger')
        events=events[:revision]
        text=HEADER+''.join(render_event(e) for e in events)
    full=view(text)
    if selected is not None:
        require(selected in full['tasks'], 'unknown task ID')
    tasks={}
    references=set()
    for key,task in full['tasks'].items():
        if selected is not None and key!=selected: continue
        fields={k:v['seq'] for k,v in task['field_events'].items()}
        edges={k:{'seq':v['seq'],'op':v['op']} for k,v in task['edge_events'].items()}
        references.update(fields.values())
        references.update(v['seq'] for v in edges.values())
        tasks[key]={'id':key,**task['values'],'advisory_dependencies':task['advisory_dependencies'],
                    'field_events':fields,'edge_events':edges,'unknown_fields':task['unknown_fields'],
                    'readiness':task['readiness']}
    return {'schema':1,'mode':'historical' if revision is not None else 'current',
            'selection':selected,'revision':full['revision'],'digest':full['digest'],
            'tracked':full['tracked'],'complete':full['tracked'] and all(not t['unknown_fields'] for t in tasks.values()),
            'ready_now':[k for k in full['ready_now'] if k in tasks],
            'open_release_obligations':sorted(k for k,t in tasks.items() if t['release_required'] is True
                                             and t['status'] in {'open','in_progress','blocked'}),
            'uncertain_obligations':sorted(k for k,t in tasks.items() if t['release_required'] is not False
                                          and t['status'] not in {'done','cancelled'}
                                          and (t['release_required'] is None or t['status'] is None)),
            'tasks':tasks,
            'sources':{str(n):{'source':events[n-1]['source'],'reason':events[n-1]['reason']} for n in sorted(references)}}


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
        additions=event if isinstance(event,list) else [event]
        require(bool(additions),'empty event batch')
        events=parse(old)+additions
        reduce_events(events)
        for added in additions: source_path(root,added['source']['path'])
        candidate=(old or HEADER)+''.join(render_event(added) for added in additions)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=wiki,prefix='.tasks-',delete=False) as f:
            temporary=Path(f.name)
            f.write(candidate);f.flush();os.fsync(f.fileno())
        os.replace(temporary,wiki/LEDGER)
        if os.name=='posix':
            directory=os.open(wiki,os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        return {'revision':len(events),'ids':[e['id'] for e in additions]}
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
        lock.unlink()


def main(root, args):
    usage='tasks apply EVENT.json | current [ID] | show [ID] | at REV [ID] | history [ID] | verify SNAPSHOT.json | check [--complete]'
    require(bool(args),usage)
    if args[0]=='apply' and len(args)==2:
        print(json.dumps(apply(root,json.loads(Path(args[1]).read_text(encoding='utf-8')))))
        return
    if args[0] in {'current','at','history','verify'}:
        text=load(root)
        for event in parse(text): source_path(root,event['source']['path'])
        if args[0]=='current':
            require(len(args)<=2,usage)
            output=current(text,args[1] if len(args)==2 else None)
        elif args[0]=='at':
            require(len(args) in (2,3),usage)
            output=current(text,args[2] if len(args)==3 else None,int(args[1]))
        elif args[0]=='history':
            require(len(args)<=2,usage)
            events=parse(text)
            if len(args)==2:
                require(any(e['id']==args[1] for e in events),'unknown task ID')
                events=[e for e in events if e['id']==args[1]]
            output={'mode':'history','events':events}
        else:
            require(len(args)==2,usage)
            snapshot=json.loads(Path(args[1]).read_text(encoding='utf-8'))
            require(isinstance(snapshot,dict),'snapshot must be an object')
            require(snapshot.get('mode')=='current','only a current view can be verified for publication')
            require(json.dumps(snapshot,sort_keys=True)==json.dumps(current(text,snapshot.get('selection')),sort_keys=True),
                    'snapshot is stale or altered; regenerate with tasks current')
            output={'verified':True,'revision':snapshot['revision'],'digest':snapshot['digest']}
        print(json.dumps(output,ensure_ascii=False,indent=2))
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
