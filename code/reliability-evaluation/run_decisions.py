"""Paired fresh-session decision experiment. Requires the isolated local CLI harness.

No expected values are passed to the subject. Outputs and usage stay outside the repo.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument('--runtime', type=Path, default=Path(__file__).with_name('runtime.py'))
p.add_argument('--baseline', required=True, type=Path)
p.add_argument('--output', required=True, type=Path)
a = p.parse_args()
spec = importlib.util.spec_from_file_location('runtime', a.runtime)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
repo = Path(__file__).resolve().parents[2]
cases = [
    ('payload', ['Server and offline payload limits are 512 KiB.',
                 'Server limit becomes 2048 KiB. Offline remains 512 KiB.',
                 'Offline limit becomes 256 KiB after memory exhaustion. Server remains 2048 KiB.'],
     'Return current limits in KiB using keys server and offline.', {'server':2048,'offline':256}),
    ('retention', ['EU retention is 30 days and US retention is 90 days.',
                   'Only EU retention changes to 7 days after the privacy review.'],
     'Return current retention days using keys eu and us.', {'eu':7,'us':90}),
    ('retry', ['Веб-клиент: 3 повтора; фоновый процесс: 5 повторов.',
               'Для фонового процесса теперь 8 повторов из-за временных ошибок сети.',
               'Изменяется только веб-клиент: 1 повтор, чтобы исключить дублирование платежей.'],
     'Return current retry counts using keys web and background.', {'web':1,'background':8}),
]
rows=[]
for ci, (name, texts, question, expected) in enumerate(cases):
    variants = ['baseline','instruction','resolved']
    variants = variants[ci:] + variants[:ci]
    for variant in variants:
        out = a.output / f'{name}-{variant}'
        if (out/'result.json').exists():
            rows.append(json.loads((out/'result.json').read_text())); continue
        workspace = out/'workspace'
        workspace.mkdir(parents=True)
        for d in ('code','deliverables','.memory/wiki/decisions'):
            (workspace/d).mkdir(parents=True, exist_ok=True)
        source = repo if variant == 'resolved' else a.baseline
        shutil.copytree(source/'code/wiki-init/templates/tree/.memory/tools', workspace/'.memory/tools', ignore=shutil.ignore_patterns('__pycache__'))
        for index, text in enumerate(texts, 1):
            (workspace/f'.memory/wiki/decisions/{index:04d}-rule.md').write_text(
                f'---\nid: {index:04d}\ndate: 2026-10-10\n'+
                (f'supersedes: {index-1:04d}\n' if index>1 else '') + f'---\n# Policy\n## Decision\n{text}\n')
        instructions = 'Use the project memory under .memory/wiki. structural.py help describes memory commands. Do not change memory. No checkpoint.\n'
        if variant != 'baseline':
            instructions += 'Before applying a rule, resolve supersession oldest to newest by scope. Preserve scopes that did not change. Historical values are not current.\n'
        if variant == 'resolved':
            instructions += 'Use python .memory/tools/structural.py decision <id> to obtain the current decision and its full lineage.\n'
        (workspace/'AGENTS.md').write_text(instructions)
        subprocess.run(['git','init','-q',str(workspace)], check=True)
        prompt = ('Continue work from memory in a fresh session. The original entry is decision 0001. '+question+
                  ' Write deliverables/answer.json containing the numeric keys, and code/policy.py with current_values() returning that same mapping. Explain the source briefly in the final response. Read memory yourself; do not guess.')
        print(f'RUN {name} {variant}', flush=True)
        meta=runtime.session(workspace, out/'session', prompt, timeout=480, readonly=True)
        try:
            answer=json.loads((workspace/'deliverables/answer.json').read_text())
            module={};exec((workspace/'code/policy.py').read_text(), module)
            actual=module['current_values']()
            passed=answer==expected and actual==expected
        except Exception as exc:
            answer={'error':str(exc)};actual=None;passed=False
        row={'case':name,'variant':variant,'passed':passed,'answer':answer,'code':actual,'expected':expected,'meta':meta}
        (out/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2))
        for link in (out/'session/profile').glob('auth.json'):
            if link.is_symlink(): link.unlink()
        rows.append(row)
        (a.output/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        print(f'RESULT {name} {variant}: {passed}', flush=True)
