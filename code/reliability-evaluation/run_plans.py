#!/usr/bin/env python3
"""Paired fresh-session work planning with independent correctness and integrity scores."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from runtime import session

REPO=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();base=args.output.resolve();base.mkdir(parents=True,exist_ok=False)
    new_tools=base/'frozen-tools'
    shutil.copytree(REPO/'code/wiki-init/templates/tree/.memory/tools',new_tools,ignore=shutil.ignore_patterns('__pycache__'))
    new_rules=(REPO/'code/wiki-init/templates/fragments/claude-md-memory.md').read_text().split('## Work plans\n',1)[1].split('## Evidence',1)[0]
    rows=[]
    for case in ('reopened','unknown'):
        definitions={
            'GATE':{'status':'done' if case=='reopened' else None},
            'QA':{'status':'done','dependencies':['GATE']},
            'SHIP':{'dependencies':['QA','GATE'],'owner':'Nora'},
            'MANUAL':{'status':'blocked'},'ACTIVE':{'status':'in_progress','dependencies':['GATE']},
            'NO_OWNER':{'owner':None},'WITHDRAWN':{'status':'cancelled'},'UNKNOWN':{'status':None}}
        definitions.update({f'N{i:02d}':{'status':'done' if i%3==0 else 'open','priority':i%4} for i in range(18)})
        events=[]
        for number,(key,changes) in enumerate(definitions.items(),1):
            values={'title':key,'status':'open','owner':'Ari','due':'2026-12-03','priority':1,
                    'release_required':True,'dependencies':[],**changes}
            events.append({'seq':number,'id':key,'op':'create','values':values,'reason':'New synthetic planning history',
                           'source':{'path':'.memory/research/events.md','quote':'E1: Reviewed synthetic planning events.'}})
        if case=='reopened':
            events.append({'seq':len(events)+1,'id':'GATE','op':'update','values':{'status':'open'},
                           'reason':'Signoff revoked; direct prerequisite reopened',
                           'source':{'path':'.memory/research/events.md','quote':'E2: Gate signoff revoked; it is open again.'}})
        # Each source describes exactly the event that actually occurred in this case.
        lines=[]
        for event in events:
            quote=f"E{event['seq']:03d}: "+json.dumps({k:v for k,v in event.items() if k!='source'},sort_keys=True)
            event['source']['quote']=quote;lines.append(quote)
        noise_ready=sorted(k for k,v in definitions.items() if k.startswith('N') and k[1:].isdigit() and v['status']=='open')
        noise_closed=sorted(k for k,v in definitions.items() if k.startswith('N') and k[1:].isdigit() and v['status']=='done')
        expected={'ready':sorted(noise_ready+(['GATE'] if case=='reopened' else [])),
                  'blocked':['MANUAL','SHIP'] if case=='reopened' else ['MANUAL'],
                  'uncertain':['NO_OWNER','UNKNOWN'] if case=='reopened' else ['GATE','NO_OWNER','SHIP','UNKNOWN'],
                  'ongoing':['ACTIVE'],'closed':sorted(noise_closed+['QA','WITHDRAWN']),
                  'ship_owner':'Nora','ship_blocked_by':['GATE'] if case=='reopened' else [],
                  'ship_unknown_dependencies':[] if case=='reopened' else ['GATE']}
        variants=['baseline','new'] if case=='reopened' else ['new','baseline']
        if case=='unknown':variants.append('sol-control')
        for variant in variants:
            out=base/f'{case}-{variant}';work=out/'workspace';work.mkdir(parents=True)
            tools=args.baseline/'code/wiki-init/templates/tree/.memory/tools' if variant=='baseline' else new_tools
            shutil.copytree(tools,work/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
            (work/'.memory/wiki').mkdir();(work/'.memory/research').mkdir();(work/'deliverables').mkdir();(work/'code').mkdir()
            (work/'.memory/research/events.md').write_text('\n'.join(lines)+'\n')
            (work/'code/capture.json').write_text(json.dumps(events))
            subprocess.run(['python3','.memory/tools/structural.py','tasks','apply','code/capture.json'],cwd=work,check=True,capture_output=True)
            rules='Use canonical task memory and sources. Never guess unknowns. Do not change memory. No checkpoint.\n'
            if variant!='baseline':
                rules+=new_rules
                rules+='Save unmodified generated plans to deliverables/plan.json and deliverables/plan.md and verify both before answering.\n'
            else:rules+='Read tasks current and compute a work plan manually. Ready work has a known owner and completed hard prerequisites. Keep ongoing, blocked, uncertain and closed work separate.\n'
            (work/'AGENTS.md').write_text(rules)
            subprocess.run(['git','init','-q',str(work)],check=True)
            prompt='''Recover all tracked work from memory in this fresh session. Write deliverables/answer.json with exactly these sorted ID lists: ready, blocked, uncertain, ongoing, closed. Also include ship_owner, ship_blocked_by, ship_unknown_dependencies for SHIP. Unknown status must not be assumed open; unknown owner prevents ready assignment. Advisory links do not block. Keep manual blockers and inconsistencies visible. Read AGENTS.md. Do not execute tasks or modify memory.'''
            model='gpt-6.1-sol' if variant=='sol-control' else 'gpt-6-luna'
            meta=session(work,out/'session',prompt,model=model,readonly=True,timeout=480)
            try:answer=json.loads((work/'deliverables/answer.json').read_text())
            except (OSError,ValueError):answer=None
            verified=False;markdown_verified=False
            if variant!='baseline' and (work/'deliverables/plan.json').exists():
                checked=subprocess.run(['python3','.memory/tools/structural.py','tasks','verify-plan','deliverables/plan.json'],cwd=work,text=True,capture_output=True)
                (out/'plan-verification.txt').write_text(checked.stdout+checked.stderr);verified=checked.returncode==0
                if (work/'deliverables/plan.md').exists():
                    checked=subprocess.run(['python3','.memory/tools/structural.py','tasks','verify-plan','deliverables/plan.md'],cwd=work,text=True,capture_output=True)
                    (out/'markdown-verification.txt').write_text(checked.stdout+checked.stderr);markdown_verified=checked.returncode==0
            row={'case':case,'variant':variant,'model':model,'runtime':meta,'expected':expected,'answer':answer,
                 'semantic_pass':meta['completed'] and answer==expected,'generated_plan_verified':verified,'markdown_plan_verified':markdown_verified}
            rows.append(row);(out/'result.json').write_text(json.dumps(row,indent=2));(base/'results.json').write_text(json.dumps(rows,indent=2))
            print(json.dumps(row),flush=True)


if __name__=='__main__':main()
