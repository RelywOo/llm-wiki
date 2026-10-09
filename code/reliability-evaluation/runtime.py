"""Isolated local Codex evaluation runtime; no scenario answers or paid API fallback."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
CODEX = Path.home()/'.local/bin/codex'
def session(workspace, output, prompt, timeout=900, readonly=False):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile = output/'profile'
    profile.mkdir(exist_ok=True)
    (profile/'tmp').mkdir(exist_ok=True)
    for name in ['auth.json']:
        target=Path.home()/'.codex'/name
        if target.exists() and not (profile/name).exists(): (profile/name).symlink_to(target)
    cache=Path.home()/'.codex/models_cache.json'
    if cache.exists(): shutil.copyfile(cache,profile/'models_cache.json')
    env = {k:v for k,v in os.environ.items() if k in {'HOME','USER','LOGNAME','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR'}}
    env.update({'PATH':'/usr/local/bin:/usr/bin:/bin','CODEX_HOME':str(profile),'TMPDIR':str(profile/'tmp'),'PYTHONDONTWRITEBYTECODE':'1','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','PYTHONUNBUFFERED':'1'})
    # Wiki hooks use "python". Provide only this alias and the Codex launcher in PATH.
    (profile/'bin').mkdir(exist_ok=True)
    python_alias=profile/'bin/python'
    if not python_alias.exists(): python_alias.symlink_to('/usr/bin/python3')
    env['PATH']=str(profile/'bin')+':'+env['PATH']
    cmd=['python3',str(ROOT/'isolate.py'),'--workspace',str(workspace),'--profile',str(profile)]
    if readonly: cmd += ['--readonly']
    cmd += ['--',str(CODEX),'exec','--ignore-user-config','--ignore-rules','--ephemeral','--json','--color','never','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','--dangerously-bypass-hook-trust','-C',str(workspace),'-m','gpt-6-luna','-c','model_reasoning_effort="medium"','-c','web_search="disabled"','-c','features.shell_snapshot=false','-c','features.multi_agent=false','-c','features.memories=false','-c','features.plugins=false','-c','features.apps=false','-']
    (output/'prompt.txt').write_text(prompt)
    (output/'command.json').write_text(json.dumps(cmd,indent=2))
    start=time.monotonic()
    with (output/'events.jsonl').open('w') as out, (output/'stderr.log').open('w') as err:
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=out,stderr=err,cwd=workspace,env=env,start_new_session=True)
        try:
            proc.communicate(prompt.encode(),timeout=timeout)
            timed_out=False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGTERM)
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL); proc.wait()
            timed_out=True
    events=[]
    for line in (output/'events.jsonl').read_text().splitlines():
        try: events.append(json.loads(line))
        except ValueError: pass
    usage=[e.get('usage') for e in events if e.get('type')=='turn.completed']
    final=[e['item'].get('text','') for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='agent_message']
    result={'exit_code':proc.returncode,'timed_out':timed_out,'seconds':time.monotonic()-start,'usage':usage,'completed':bool(usage),'agent_messages':len(final)}
    (output/'meta.json').write_text(json.dumps(result,indent=2))
    (output/'final.txt').write_text('\n\n'.join(final))
    auth_link=profile/'auth.json'
    if auth_link.is_symlink(): auth_link.unlink()
    return result
