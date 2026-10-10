"""Focused final-delivery evaluation. Private oracle never enters subject workspaces."""
import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import shutil
import subprocess

REPO=Path(__file__).resolve().parents[2]
CASES=[
    {'name':'scope','source':'E031: Production is permitted in eu-west-1 and us-east-1.\nE032: ap-south-1 is approved for isolated laboratory use only.\n',
     'question':'A: ap-south-1 is approved for production. B: eu-west-1 is approved for production. Return A and B as booleans.',
     'expected':{'A':False,'B':True},'events':['E031','E032']},
    {'name':'date','source':'E010: The rollout was proposed for 12 November 2026; this date was not approved.\nE041: The approved rollout is 19 November 2026, after completion of migration testing.\n',
     'question':'A: The currently approved rollout date is 12 November 2026. B: The approved rollout is 19 November 2026 after migration testing. Return A and B as booleans.',
     'expected':{'A':False,'B':True},'events':['E010','E041']},
    {'name':'reason','source':'E050: The release date is 12 November because partner operators reserved that deployment window.\nE071: The board reconfirmed the date; it did not change the original reason.\n',
     'question':'A: The board reconfirmation is the original reason for the release date. B: The partner operators reserved the deployment window, which explains the date. Return A and B as booleans.',
     'expected':{'A':False,'B':True},'events':['E050','E071']},
    {'name':'current','source':'''E431: Task CUTOVER: title Regional cutover; status open; owner Vera; due 2026-12-11; priority 2; required for release true; no prerequisites.
E432: CUTOVER owner changes to Tomas because Vera is unavailable. Other fields stay unchanged.
E433: CUTOVER due date changes to 2026-12-18 because migration validation needs more time.
E434: CUTOVER is marked done after initial approval; all other fields unchanged.
E435: Approval is revoked after a failed migration check. CUTOVER becomes blocked, its due date is withdrawn with no replacement date, and its owner changes to Noor. Priority and release obligation remain unchanged.
E436: A retrospective quotes the E431 owner and deadline; this is historical and changes no current field.
E437: The latest work session has no active focus. CUTOVER remains an outstanding obligation.
''',
     'question':'Return current_owner, current_due (null when withdrawn), current_status, current_priority (integer), release_required (boolean), initial_owner, initial_due (ISO date), open_release_obligations (sorted ID list). Distinguish history and current state.',
     'expected':{'current_owner':'Noor','current_due':None,'current_status':'blocked','current_priority':2,'release_required':True,'initial_owner':'Vera','initial_due':'2026-12-11','open_release_obligations':['CUTOVER']},'events':['E431','E435']},
    {'name':'inherited','source':'E801: History and current policy are in the decision pages.\n',
     'decisions':{
         '0001-limits.md':'---\nid: 0001\ndate: 2026-10-01\n---\n# Limits\n## Decision\nE811: Alder limits: server 4096 KiB; offline 768 KiB; diagnostic 128 KiB. Reason: bounded queues.\n',
         '0002-offline.md':'---\nid: 0002\ndate: 2026-10-02\nsupersedes: 0001\n---\n# Offline limit\n## Decision\nE812: Replace only the offline limit with 256 KiB because mobile devices lack RAM. Other scopes remain unchanged.\n'},
     'question':'For Alder return server_kib, offline_kib, diagnostic_kib, initial_offline_kib as integers. Resolve the lineage. Cite both decisions and explicitly require that the successor is current.',
     'expected':{'server_kib':4096,'offline_kib':256,'diagnostic_kib':128,'initial_offline_kib':768},'events':['E811','E812'],'current_path':'.memory/wiki/decisions/0002-offline.md'},
    {'name':'unicode','source':'E901: Проект Липа-918: релиз утверждён на 14 декабря 2026 года; ответственная Дарья.\r\nE902: Релиз отложен из-за неполных миграционных тестов. Новый срок не утверждён. Теперь ответственная Ника.\r\nE903: В другом проекте Липа-919 релиз назначен на 21 декабря; это не меняет Липу-918.\r\n',
     'question':'For Липа-918 return current_due (ISO date or null if not approved), initial_due (ISO date), owner (exact Russian name), reason (exact Russian phrase after из-за without terminal period). Do not borrow a date from another project.',
     'expected':{'current_due':None,'initial_due':'2026-12-14','owner':'Ника','reason':'неполных миграционных тестов'},'events':['E901','E902']},
]


def digest_memory(workspace):
    return {str(path.relative_to(workspace)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((workspace/'.memory').rglob('*'))
            if path.is_file() and '__pycache__' not in path.parts}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',type=Path,required=True,help='Pre-delivery-fix source export, with evidence quote/check.')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,default=Path(__file__).with_name('runtime.py'))
    parser.add_argument('--replay-workspace',type=Path,help='Optional original current-case captured memory.')
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('delivery_runtime',args.runtime)
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    args.output.mkdir(parents=True,exist_ok=False)
    schedule=[]
    for index,case in enumerate(CASES):
        arms=[('manual','gpt-6-luna',1),('publisher','gpt-6-luna',1),
              ('publisher','gpt-6-luna',2),('publisher','gpt-6.1-sol',1)]
        if index%2: arms.reverse()
        schedule.extend((case,arm,model,repeat) for arm,model,repeat in arms)
    protocol={'cases':CASES,'schedule':[(c['name'],a,m,r) for c,a,m,r in schedule],
              'rules':['No reruns or normalization; every attempted session is retained.',
                       'Exact answer field types/values; nonempty explanation separately.',
                       'All required event labels with exact source receipts; at least two excerpts.',
                       'Publisher: verify the actual final file and current successor requirement.',
                       'Manual control: pre-fix quote/check receipts, same semantic questions.',
                       'Read-only durable memory; final response links generated artifact.',
                       'All sessions fresh; replayed memory only for current if supplied.',
                       'Any publisher failure keeps acceptance on hold. No universal reliability claim.'],
              'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'baseline':str(args.baseline.resolve()),'candidate':str(REPO),
              'replay_workspace':str(args.replay_workspace.resolve()) if args.replay_workspace else None}
    (args.output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    rows=[]
    for case,arm,model,repeat in schedule:
        out=args.output/f"{case['name']}-{arm}-{model}-r{repeat}"
        workspace=out/'workspace';workspace.mkdir(parents=True)
        if case['name']=='current' and args.replay_workspace:
            shutil.copytree(args.replay_workspace/'.memory',workspace/'.memory',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.rmtree(workspace/'.memory/tools')
        for directory in ('code','deliverables','.memory/research','.memory/wiki/decisions'):
            (workspace/directory).mkdir(parents=True,exist_ok=True)
        source_repo=REPO if arm=='publisher' else args.baseline
        shutil.copytree(source_repo/'code/wiki-init/templates/tree/.memory/tools',workspace/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
        (workspace/'.memory/research/events.md').write_bytes(case['source'].encode('utf-8'))
        (workspace/'.memory/research/neighbor.md').write_text('E999: Unrelated project with no approved schedule.\n')
        for name,body in case.get('decisions',{}).items():
            (workspace/'.memory/wiki/decisions'/name).write_text(body)
        instructions=(source_repo/'code/wiki-init/templates/fragments/claude-md-memory.md').read_text()
        instructions+='\nUse only this synthetic repository. Do not change memory, checkpoint or commit. Write only in deliverables/.\n'
        (workspace/'AGENTS.md').write_text(instructions)
        subprocess.run(['git','init','-q',str(workspace)],check=True)
        before=digest_memory(workspace)
        prompt=('Answer from durable memory in this fresh session. '+case['question']+
                ' Include exactly the requested fields and a nonempty explanation string in the answer object. '
                'Cite at least two excerpts, including event labels '+', '.join(case['events'])+'. ')
        if arm=='publisher':
            prompt+=('Use the installed final evidence publication workflow to produce deliverables/answer.json. '
                     'Verify that final file and link it in your final response; do not retype its contents.')
        else:
            prompt+=('Write deliverables/answer.json with exactly answer (the answer object) and evidence '
                     '(an array of exact receipts returned by evidence quote with --event). '
                     'Save and check receipts before delivery; explanations belong only in answer. '
                     'Link the final file in your final response.')
        print('RUN',out.name,flush=True)
        meta=runtime.session(workspace,out/'session',prompt,timeout=480,readonly=True,model=model)
        row={'case':case['name'],'arm':arm,'model':model,'repeat':repeat,'meta':meta,'passed':False}
        try:
            artifact=json.loads((workspace/'deliverables/answer.json').read_text())
            answer=artifact['answer'];expected=case['expected']
            facts={key:type(answer.get(key)) is type(value) and answer.get(key)==value for key,value in expected.items()}
            shape=set(answer)==set(expected)|{'explanation'} and isinstance(answer['explanation'],str) and bool(answer['explanation'].strip())
            receipts=[entry['receipt'] for entry in artifact['evidence']] if arm=='publisher' else artifact['evidence']
            evidence_checks=[]
            for index,receipt in enumerate(receipts):
                saved=out/f'receipt-{index}.json';saved.write_text(json.dumps(receipt))
                command=['python3','.memory/tools/structural.py','evidence','check',str(saved.resolve())]
                if receipt.get('path')==case.get('current_path'):command+=['--current']
                checked=subprocess.run(command,cwd=workspace,text=True,capture_output=True)
                evidence_checks.append({'returncode':checked.returncode,'output':checked.stdout+checked.stderr})
            events={receipt.get('event_id') for receipt in receipts}
            required=set(case['events'])<=events
            final_check=None;current=True
            if arm=='publisher':
                checked=subprocess.run(['python3','.memory/tools/structural.py','evidence','verify','deliverables/answer.json'],cwd=workspace,text=True,capture_output=True)
                final_check={'returncode':checked.returncode,'output':checked.stdout+checked.stderr}
                if case.get('current_path'):
                    current=any(entry['receipt']['path']==case['current_path'] and entry['current'] is True for entry in artifact['evidence'])
            else:shape=shape and set(artifact)=={'answer','evidence'}
            unchanged=before==digest_memory(workspace)
            final=(out/'session/final.txt').read_text()
            links=re.findall(r'\[[^\]]*\]\(([^)\n]+)\)',final)
            linked=any((workspace/target.strip('<>')).resolve()==(workspace/'deliverables/answer.json').resolve()
                       for target in links)
            # The final response must deliver the file, not a manually copied JSON receipt.
            no_receipt_copy='source_sha256' not in final and 'artifact_sha256' not in final
            passed=(meta['completed'] and meta['exit_code']==0 and not meta['timed_out'] and shape and
                    all(facts.values()) and len(receipts)>=2 and required and all(c['returncode']==0 for c in evidence_checks) and
                    (final_check is None or final_check['returncode']==0) and current and unchanged and linked and no_receipt_copy)
            row.update({'artifact':artifact,'facts':facts,'shape':shape,'receipt_checks':evidence_checks,
                        'required_events':required,'current_requirement':current,'final_check':final_check,
                        'memory_unchanged':unchanged,'linked':linked,'no_receipt_copy':no_receipt_copy,'passed':bool(passed)})
        except Exception as exc:row['scoring_error']=str(exc)
        (out/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2))
        rows.append(row);(args.output/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        print('RESULT',out.name,row['passed'],flush=True)
    print('COMPLETE',sum(row['passed'] for row in rows),'/',len(rows),flush=True)


if __name__=='__main__':main()
