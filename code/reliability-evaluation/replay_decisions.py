"""Replay the original full decision evaluation against unchanged learned knowledge."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

p=argparse.ArgumentParser()
p.add_argument('--experiment-root',required=True,type=Path)
p.add_argument('--baseline',required=True,type=Path)
p.add_argument('--output',required=True,type=Path)
a=p.parse_args()
spec=importlib.util.spec_from_file_location('runtime',a.experiment_root/'tools/runtime.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
repo=Path(__file__).resolve().parents[2]
original=a.experiment_root/'workspaces/e1-r3-wiki'
prompt=(a.experiment_root/'results/experiment-1/e1-r3-wiki/eval-2/prompt.txt').read_text()
def digest(root):
    h=hashlib.sha256()
    for sub in ('.memory/wiki','.memory/research'):
        for path in sorted((root/sub).rglob('*')):
            if path.is_file():h.update(path.relative_to(root).as_posix().encode());h.update(path.read_bytes())
    h.update((root/'.memory/STATE.md').read_bytes())
    return h.hexdigest()
rows=[]
for variant,source in [('baseline',a.baseline),('new',repo)]:
    out=a.output/variant;out.mkdir(parents=True)
    workspace=out/'workspace'
    subprocess.run(['git','clone','--no-local',str(original.resolve()),str(workspace.resolve())],check=True,capture_output=True)
    for key,value in [('user.name','Wiki evaluation'),('user.email','wiki-evaluation@example.invalid')]:
        subprocess.run(['git','config',key,value],cwd=workspace,check=True)
    before=digest(workspace)
    prepared=subprocess.run(['python3',str((source/'code/wiki-init/update.py').resolve()),'--root',str(workspace.resolve()),'--portable'],text=True,capture_output=True)
    (out/'update.log').write_text(prepared.stdout+prepared.stderr)
    if prepared.returncode:raise RuntimeError('update failed; inspect '+str(out/'update.log'))
    assert digest(workspace)==before
    (workspace/'code').mkdir(exist_ok=True);(workspace/'deliverables').mkdir(exist_ok=True)
    print('REPLAY',variant,flush=True)
    meta=runtime.session(workspace,out/'session',prompt,timeout=900,readonly=True)
    score_file=out/'score.json'
    scored=subprocess.run(['python3','-B',str((a.experiment_root/'tools/score.py').resolve()),'1','2',str(workspace.resolve()),str(score_file.resolve())],capture_output=True,text=True,timeout=60)
    (out/'scoring.log').write_text(scored.stdout+scored.stderr)
    score=json.loads(score_file.read_text())
    row={'variant':variant,'meta':meta,'knowledge_unchanged':digest(workspace)==before,
         'knowledge_digest':before,'facts':[score['facts_passed'],score['facts_total']],
         'code':[score['code_passed'],score['code_total']],
         'failures':[x for x in score['code'] if not x['passed']]}
    for link in (out/'session/profile').glob('auth.json'):
        if link.is_symlink():link.unlink()
    rows.append(row);(a.output/'summary.json').write_text(json.dumps(rows,indent=2))
    print('RESULT',variant,row['facts'],row['code'],flush=True)
