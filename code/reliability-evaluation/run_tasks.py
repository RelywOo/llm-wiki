"""Paired task capture and independent-session recovery with external raw artifacts."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

p=argparse.ArgumentParser()
p.add_argument('--runtime',type=Path,required=True)
p.add_argument('--baseline',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
spec=importlib.util.spec_from_file_location('runtime',a.runtime)
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
repo=Path(__file__).resolve().parents[2]
cases=[]
for name,count in [('release',24),('unknown',3)]:
    events=[]
    for i in range(1,count+1):
        values={'title':f'Task {i}','status':'done' if name=='release' and i<=2 else 'open',
                'owner':'Alex','due':'2026-11-12','priority':1,
                'release_required':i<=17 if name=='release' else (None if i==2 else i==1),
                'dependencies':[]}
        events.append({'id':f'T{i:02d}','values':values})
    expected={'open_release_obligations':[f'T{i:02d}' for i in range(3,18)] if name=='release' else ['T01'],
              'unknown_release_required':[] if name=='release' else ['T02']}
    cases.append((name,events,expected))
rows=[]
for ci,(name,events,expected) in enumerate(cases):
    for variant in (['baseline','new'] if ci%2==0 else ['new','baseline']):
        out=a.output/f'{name}-{variant}'
        if (out/'result.json').exists(): rows.append(json.loads((out/'result.json').read_text()));continue
        workspace=out/'workspace';workspace.mkdir(parents=True)
        for d in ('code','deliverables','.memory/wiki/concepts','.memory/research'):
            (workspace/d).mkdir(parents=True,exist_ok=True)
        source=repo if variant=='new' else a.baseline
        shutil.copytree(source/'code/wiki-init/templates/tree/.memory/tools',workspace/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
        lines=[f'E{i:03d}: '+json.dumps(e,ensure_ascii=False) for i,e in enumerate(events,1)]
        (workspace/'.memory/research/events.md').write_text('\n'.join(lines)+'\n')
        rules='Maintain project memory under .memory/wiki. Use sources. Never guess missing information. No checkpoint; no commits in this experiment.\n'
        if variant=='new':
            frag=(source/'code/wiki-init/templates/fragments/claude-md-memory.md').read_text()
            rules+=frag.split('## Tasks and obligations\n',1)[1].split('## Applying decisions',1)[0]
        else:
            rules+='Use living concept pages and an index for task memory; historical sources stay under .memory/research.\n'
        (workspace/'AGENTS.md').write_text(rules)
        subprocess.run(['git','init','-q',str(workspace)],check=True)
        print('LEARN',name,variant,flush=True)
        learning=runtime.session(workspace,out/'learn',
            'Read .memory/research/events.md and capture all tasks in durable memory for another session. Each event is an authoritative complete creation record. Preserve explicit unknowns. Do not perform project tasks.',timeout=600)
        print('EVAL',name,variant,flush=True)
        evaluation=runtime.session(workspace,out/'eval',
            'Recover the tasks from memory in this fresh session. Write deliverables/answer.json with exactly two sorted ID lists: open_release_obligations (explicitly release-required and neither done nor cancelled); unknown_release_required (release_required unknown). Explain any gap. Do not alter memory or create a checkpoint.',timeout=480,readonly=True)
        try: answer=json.loads((workspace/'deliverables/answer.json').read_text())
        except Exception as exc: answer={'error':str(exc)}
        row={'case':name,'variant':variant,'passed':answer==expected and learning['completed'] and evaluation['completed'],
             'expected':expected,'answer':answer,'learning':learning,'evaluation':evaluation}
        (out/'result.json').write_text(json.dumps(row,indent=2))
        for phase in ('learn','eval'):
            for link in (out/phase/'profile').glob('auth.json'):
                if link.is_symlink():link.unlink()
        rows.append(row);(a.output/'summary.json').write_text(json.dumps(rows,indent=2))
        print('RESULT',name,variant,row['passed'],flush=True)
