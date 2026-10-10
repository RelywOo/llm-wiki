"""One held-out end-to-end read through the bundled isolated runtime."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import runtime

p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);a=p.parse_args()
repo=Path(__file__).resolve().parents[2]
workspace=a.output/'workspace';workspace.mkdir(parents=True)
for d in ('code','deliverables','.memory/wiki/decisions','.memory/research'):
    (workspace/d).mkdir(parents=True,exist_ok=True)
shutil.copytree(repo/'code/wiki-init/templates/tree/.memory/tools',workspace/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
source='E101: Server buffer 64 KiB; edge buffer 32 KiB.\nE102: Change only edge buffer to 8 KiB.\nE103: Audit gate is required for release; owner and due date unknown; priority 1; status open; it has no prerequisites.\n'
(workspace/'.memory/research/events.md').write_text(source)
for number,body in [(1,'Server buffer 64 KiB; edge buffer 32 KiB.'),(2,'Change only edge buffer to 8 KiB.')]:
    (workspace/f'.memory/wiki/decisions/{number:04d}-buffer.md').write_text(
        f'---\nid: {number:04d}\ndate: 2026-10-10\n'+('supersedes: 0001\n' if number==2 else '')+f'---\n# Buffer\n## Decision\n{body}\n')
event={'seq':1,'id':'AUDIT','op':'create','values':{'title':'Audit gate','status':'open','owner':None,'due':None,
       'priority':1,'release_required':True,'dependencies':[]},'reason':'Capture release gate',
       'source':{'path':'.memory/research/events.md','quote':source.splitlines()[2]}}
(workspace/'event.json').write_text(json.dumps(event))
subprocess.run(['python3','.memory/tools/structural.py','tasks','apply','event.json'],cwd=workspace,check=True)
(workspace/'event.json').unlink()
(workspace/'AGENTS.md').write_text((repo/'code/wiki-init/templates/fragments/claude-md-memory.md').read_text())
subprocess.run(['git','init','-q',str(workspace)],check=True)
meta=runtime.session(workspace,a.output/'session',
    'Fresh session, read-only memory; no checkpoint. Starting from decision 0001, recover the CURRENT buffer sizes and task AUDIT. Use the current-decision, current-task and exact-evidence commands. Write deliverables/answer.json with fields server_kib, edge_kib, owner (null if unknown), release_required, and evidence (the exact E103 receipt returned by the tool). Do not repeat historical values as current.',readonly=True,timeout=480)
answer=json.loads((workspace/'deliverables/answer.json').read_text())
receipt=workspace/'deliverables/receipt.json';receipt.write_text(json.dumps(answer['evidence']))
checked=subprocess.run(['python3','.memory/tools/structural.py','evidence','check',str(receipt.resolve())],cwd=workspace,capture_output=True,text=True)
expected={'server_kib':64,'edge_kib':8,'owner':None,'release_required':True}
result={'meta':meta,'answer':answer,'expected':expected,'passed':meta['completed'] and all(answer.get(k)==v for k,v in expected.items()) and checked.returncode==0,
        'evidence_check':checked.stdout}
(a.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print('FINAL SMOKE',result['passed'])
