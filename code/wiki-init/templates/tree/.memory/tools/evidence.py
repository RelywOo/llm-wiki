"""Exact source receipts. Location/freshness checks never imply semantic support."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def require(condition, message):
    if not condition: raise ValueError(message)


def load_json(text):
    def pairs(items):
        out={}
        for key,value in items:
            require(key not in out, f'duplicate JSON key: {key}')
            out[key]=value
        return out
    def invalid(value):
        raise ValueError(f'non-finite JSON value: {value}')
    return json.loads(text,object_pairs_hook=pairs,parse_constant=invalid)


def source_path(root, value):
    require(isinstance(value,str) and value,'source path must be nonempty')
    relative=Path(value)
    require(not relative.is_absolute() and '..' not in relative.parts,'source must be repository-relative')
    root=root.resolve();target=root/relative
    require(target.resolve().is_relative_to(root),'source escapes repository')
    cursor=target
    while cursor!=root:
        require(not cursor.is_symlink(),'source symlinks are not supported')
        cursor=cursor.parent
    require(target.is_file(),f'source missing: {value}')
    return target


def normalized(text):
    return text.replace('\r\n','\n').replace('\r','\n')


def exact_quote(quote, text):
    return isinstance(quote,str) and bool(quote.strip()) and normalized(quote) in normalized(text)


def quote(root, path, start, end, event_id=None):
    target=source_path(root,path)
    raw=target.read_bytes();lines=normalized(raw.decode('utf-8')).splitlines(keepends=True)
    require(type(start) is int and type(end) is int and 1<=start<=end<=len(lines),'invalid line range')
    receipt={'schema':1,'path':path,'start_line':start,'end_line':end,
             'source_sha256':hashlib.sha256(raw).hexdigest(),'quote':''.join(lines[start-1:end])}
    if event_id is not None:
        require(isinstance(event_id,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',event_id),'invalid event ID')
        require(re.search(r'(?m)^\s*'+re.escape(event_id)+r'(?=[:\s])',receipt['quote']),
                'event ID is not a label in the quoted lines')
        receipt['event_id']=event_id
    return receipt


def check(root, receipt, resolver, current=False):
    errors=[]
    status='not_a_decision'
    try:
        required={'schema','path','start_line','end_line','source_sha256','quote'}
        require(isinstance(receipt,dict) and required <= set(receipt) and
                set(receipt)<=required|{'event_id','claim'},'invalid receipt fields')
        require(type(receipt['schema']) is int and receipt['schema']==1,'unsupported receipt schema')
        expected=quote(root,receipt['path'],receipt['start_line'],receipt['end_line'],receipt.get('event_id'))
        if receipt['quote']!=expected['quote']: errors.append('quote_mismatch: text is not the exact line range')
        if receipt['source_sha256']!=expected['source_sha256']: errors.append('source_changed: regenerate the receipt')
        relative=Path(receipt['path'])
        if relative.parent==Path('.memory/wiki/decisions'):
            try:
                packet=resolver(relative.name)
                status=packet['status']
            except ValueError as exc:
                status='unresolved';errors.append(f'unresolved_decision: {exc}')
        if current and status!='current':
            errors.append(f'current_decision_not_established: {status}; resolve lineage and review scope')
    except (ValueError, OSError, TypeError, KeyError) as exc:
        errors.append(str(exc))
    return {'location_and_freshness_verified':not errors,'decision_status':status,
            'semantic_support':'not_checked','errors':errors}


def main(root, args, resolver):
    usage='evidence quote PATH START END [--event ID] | check RECEIPT.json [--current]'
    require(bool(args),usage)
    if args[0]=='quote':
        require(len(args) in (4,6),usage)
        require(len(args)==4 or args[4]=='--event',usage)
        output=quote(root,args[1],int(args[2]),int(args[3]),args[5] if len(args)==6 else None)
        print(json.dumps(output,ensure_ascii=False,indent=2));return True
    require(args[0]=='check' and len(args) in (2,3),usage)
    require(len(args)==2 or args[2]=='--current',usage)
    receipts=load_json(Path(args[1]).read_text(encoding='utf-8'))
    results=[check(root,r,resolver,len(args)==3) for r in (receipts if isinstance(receipts,list) else [receipts])]
    require(bool(results),'empty receipt list')
    print(json.dumps({'results':results,'semantic_support':'not_checked'},ensure_ascii=False,indent=2))
    return all(r['location_and_freshness_verified'] for r in results)
