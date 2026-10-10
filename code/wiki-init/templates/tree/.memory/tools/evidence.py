"""Exact source receipts. Location/freshness checks never imply semantic support."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
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


def artifact_digest(artifact):
    payload={key:value for key,value in artifact.items() if key!='artifact_sha256'}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,
                                    separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def verify(root, artifact, resolver):
    errors=[];results=[]
    try:
        require(isinstance(artifact,dict) and set(artifact)==
                {'schema','answer','evidence','semantic_support','artifact_sha256'},'invalid artifact fields')
        require(type(artifact['schema']) is int and artifact['schema']==1,'unsupported artifact schema')
        require(isinstance(artifact['answer'],dict) and bool(artifact['answer']),'answer must be a nonempty object')
        require(artifact['semantic_support']=='not_checked','semantic support cannot be certified by this tool')
        require(isinstance(artifact['evidence'],list) and bool(artifact['evidence']),'empty or invalid evidence')
        if artifact['artifact_sha256']!=artifact_digest(artifact):
            errors.append('artifact_changed: publish a new artifact instead of editing the output')
        for item in artifact['evidence']:
            require(isinstance(item,dict) and set(item)=={'receipt','current'} and
                    type(item['current']) is bool,'invalid evidence entry')
            result=check(root,item['receipt'],resolver,item['current']);results.append(result)
            if not result['location_and_freshness_verified']: errors.extend(result['errors'])
    except (ValueError,OSError,TypeError,KeyError) as exc:
        errors.append(str(exc))
    return {'artifact_verified':not errors,'results':results,
            'semantic_support':'not_checked','errors':errors}


def publish(root, manifest, output, resolver):
    require(isinstance(manifest,dict) and set(manifest)=={'answer','sources'},'invalid manifest fields')
    require(isinstance(manifest['sources'],list) and bool(manifest['sources']),'empty or invalid sources')
    evidence=[]
    for selection in manifest['sources']:
        required={'path','start_line','end_line'}
        require(isinstance(selection,dict) and required<=set(selection) and
                set(selection)<=required|{'event_id','current'},'invalid source selection fields')
        current=selection.get('current',False)
        require(type(current) is bool,'current must be boolean')
        evidence.append({'receipt':quote(root,selection['path'],selection['start_line'],
                                         selection['end_line'],selection.get('event_id')),'current':current})
    artifact={'schema':1,'answer':manifest['answer'],'evidence':evidence,'semantic_support':'not_checked'}
    artifact['artifact_sha256']=artifact_digest(artifact)
    serialized=json.dumps(artifact,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    checked=verify(root,load_json(serialized),resolver)
    require(checked['artifact_verified'],'; '.join(checked['errors']))
    relative=Path(output)
    require(not relative.is_absolute() and len(relative.parts)==2 and
            relative.parts[0]=='deliverables' and relative.suffix=='.json',
            'output must be deliverables/NAME.json')
    directory=root/'deliverables'
    directory.mkdir(exist_ok=True)
    # Pin the directory and reject symlinks. Linking a flushed temporary file
    # publishes atomically and fails if the destination already exists.
    directory_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary='.evidence-'+uuid.uuid4().hex
    created=False
    try:
        fd=os.open(temporary,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory_fd)
        created=True
        with os.fdopen(fd,'w+',encoding='utf-8') as stream:
            stream.write(serialized);stream.flush();os.fsync(stream.fileno());stream.seek(0)
            checked=verify(root,load_json(stream.read()),resolver)
            require(checked['artifact_verified'],'; '.join(checked['errors']))
        os.link(temporary,relative.name,src_dir_fd=directory_fd,dst_dir_fd=directory_fd,follow_symlinks=False)
        os.fsync(directory_fd)
    finally:
        if created: os.unlink(temporary,dir_fd=directory_fd)
        os.close(directory_fd)
    return {'path':output,**checked}


def main(root, args, resolver):
    usage='evidence quote PATH START END [--event ID] | check RECEIPT.json [--current] | publish MANIFEST.json deliverables/NAME.json | verify ARTIFACT.json'
    require(bool(args),usage)
    if args[0]=='publish':
        require(len(args)==3,usage)
        manifest=load_json(source_path(root,args[1]).read_text(encoding='utf-8'))
        print(json.dumps(publish(root,manifest,args[2],resolver),ensure_ascii=False,indent=2));return True
    if args[0]=='verify':
        require(len(args)==2,usage)
        artifact=load_json(source_path(root,args[1]).read_text(encoding='utf-8'))
        checked=verify(root,artifact,resolver)
        print(json.dumps(checked,ensure_ascii=False,indent=2));return checked['artifact_verified']
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
