"""Fresh-session evidence extraction and independent semantic-scope assessment."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

p=argparse.ArgumentParser()
p.add_argument('--runtime',type=Path,default=Path(__file__).with_name('runtime.py'))
p.add_argument('--baseline',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
spec=importlib.util.spec_from_file_location('runtime',a.runtime)
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
repo=Path(__file__).resolve().parents[2]
cases=[
 ('scope','E031: Production is permitted in eu-west-1 and us-east-1.\nE032: ap-south-1 is approved for isolated laboratory use only.\n',
  'A: ap-south-1 is approved for production. B: eu-west-1 is approved for production.',{'A':False,'B':True}),
 ('date','E010: The rollout was proposed for 12 November 2026; this date was not approved.\nE041: The approved rollout is 19 November 2026, after completion of migration testing.\n',
  'A: The currently approved rollout date is 12 November 2026. B: The approved rollout is 19 November 2026 after migration testing.',{'A':False,'B':True}),
 ('reason','E050: The release date is 12 November because partner operators reserved that deployment window.\nE071: The board reconfirmed the date; it did not change the original reason.\n',
  'A: The board reconfirmation is the original reason for the release date. B: The partner operators reserved the deployment window, which explains the date.',{'A':False,'B':True}),
]
rows=[]
for ci,(name,source,claims,expected) in enumerate(cases):
    for variant in (['baseline','new'] if ci%2==0 else ['new','baseline']):
        out=a.output/f'{name}-{variant}'
        if (out/'result.json').exists():rows.append(json.loads((out/'result.json').read_text()));continue
        workspace=out/'workspace';workspace.mkdir(parents=True)
        for d in ('code','deliverables','.memory/wiki/decisions','.memory/research'):
            (workspace/d).mkdir(parents=True,exist_ok=True)
        source_repo=repo if variant=='new' else a.baseline
        shutil.copytree(source_repo/'code/wiki-init/templates/tree/.memory/tools',workspace/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
        (workspace/'.memory/research/events.md').write_text(source)
        (workspace/'.memory/research/neighbor.md').write_text('E999: A different project has no approved schedule.\n')
        instructions='Use memory source files under .memory/research. Do not change memory. Do not confuse quoted text with its implications.\n'
        if variant=='new':
            frag=(source_repo/'code/wiki-init/templates/fragments/claude-md-memory.md').read_text()
            instructions+=frag.split('## Evidence and quotations\n',1)[1].split('## Applying decisions',1)[0]
        (workspace/'AGENTS.md').write_text(instructions)
        subprocess.run(['git','init','-q',str(workspace)],check=True)
        prompt=('Assess these claims against memory: '+claims+
                ' Write deliverables/answer.json with claims (A and B boolean verdicts) and evidence (at least two exact source excerpts explaining the verdicts). Each evidence entry needs path, quote, and event_id; preserve extra receipt fields if your tools provide them. Separately assess meaning; a literal match is not proof of scope. Explain the verdicts briefly. No checkpoint.')
        print('RUN',name,variant,flush=True)
        meta=runtime.session(workspace,out/'session',prompt,timeout=480,readonly=True)
        try:
            answer=json.loads((workspace/'deliverables/answer.json').read_text())
            receipts=answer['evidence'];exact=[];verified=[]
            for i,r in enumerate(receipts):
                path=workspace/r['path']
                text=path.read_text() if path.resolve().is_relative_to(workspace.resolve()) else ''
                exact.append(bool(r['quote'].strip()) and r['quote'] in text and
                             bool(re.search(r'(?m)^\s*'+re.escape(r['event_id'])+r'(?=[:\s])',r['quote'])))
                if variant=='new':
                    receipt_file=workspace/f'deliverables/receipt-{i}.json';receipt_file.write_text(json.dumps(r))
                    result=subprocess.run(['python3','.memory/tools/structural.py','evidence','check',str(receipt_file.resolve())],cwd=workspace,capture_output=True,text=True)
                    verified.append(result.returncode==0)
            passed=meta['completed'] and answer['claims']==expected and len(receipts)>=2 and all(exact) and (all(verified) if variant=='new' else True)
        except Exception as exc:answer={'error':str(exc)};exact=[];verified=[];passed=False
        verdicts=answer.get('claims',{})
        if not isinstance(verdicts,dict): verdicts={}
        normalized={k:(v.get('verdict') if isinstance(v,dict) else v) for k,v in verdicts.items()}
        schema_valid=set(verdicts)==set(expected) and all(type(v) is bool for v in verdicts.values())
        semantic_passed=normalized==expected and all(type(v) is bool for v in normalized.values())
        row={'schema_valid':schema_valid,'semantic_passed':semantic_passed,'case':name,'variant':variant,'passed':passed,'expected':expected,'answer':answer,'exact_quotes':exact,'receipt_checks':verified,'meta':meta}
        (out/'result.json').write_text(json.dumps(row,indent=2))
        for link in (out/'session/profile').glob('auth.json'):
            if link.is_symlink():link.unlink()
        rows.append(row);(a.output/'summary.json').write_text(json.dumps(rows,indent=2))
        print('RESULT',name,variant,passed,flush=True)
