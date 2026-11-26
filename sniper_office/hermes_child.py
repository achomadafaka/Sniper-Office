"""One wake of one Hermes seat, inside the harness sandbox. Same walls and same proxy route as a GPT seat: Hermes talks to the ChatGPT Codex backend
(provider openai-codex) with a fake token that the host proxy swaps; office tools arrive over the same MCP bridge. Reads a JSON request on stdin,
prints seat_child's event shape. The Hermes runtime (its python, site-packages and agent source) is published read-only at /opt/hermes."""
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0,'/opt/release')
from sniper_office import codex_child,seat_child,seat_forward

ROOT='/opt/hermes'
HOME='/opt/workspace/.hermes'
CA='/opt/ca.pem'
LOG='/opt/workspace/tmp/hermes-wake.log'
TOOLSETS='terminal,file,todo'        # office tools come over MCP; Hermes' own web, browser, messaging and cron stay off
BOOT=("import sys;sys.path[:0]=[%r,%r];sys.argv=['hermes']+sys.argv[1:];from hermes_cli.main import main;sys.exit(main())"%(ROOT+'/agent',ROOT+'/site'))
emit=codex_child.emit


def write_home(doc,home=HOME):
    os.makedirs(home,exist_ok=True)
    token=codex_child.fake_token(doc['account_id'])
    entry=dict(id='seat',label='seat',auth_type='oauth',priority=0,source='manual:device_code',access_token=token,refresh_token='',base_url='https://chatgpt.com/backend-api/codex',
               last_refresh='2026-10-30T00:00:00Z',request_count=0,**{k:None for k in ('last_status','last_status_at','last_error_code','last_error_reason','last_error_message','last_error_reset_at')})
    with open(os.open(home+'/auth.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
        json.dump(dict(version=1,providers={},active_provider='openai-codex',updated_at='2026-10-30T00:00:00Z',credential_pool={'openai-codex':[entry]}),f)
    env={k:os.environ[k] for k in ('PYTHONHOME','LD_LIBRARY_PATH') if k in os.environ}
    cfg=dict(model=dict(default=doc['model'],provider='openai-codex'),
             mcp_servers=dict(office=dict(command=sys.executable,args=['-c',seat_child.BRIDGE],env=env,timeout=3600,connect_timeout=180)))
    with open(home+'/config.yaml','w') as f:json.dump(cfg,f)     # JSON is YAML
    with open(home+'/SOUL.md','w') as f:f.write(doc['system'])


def relay(doc,stream,**limits):
    """Prints events as they arrive. Returns (session, usage, failed, stalled). A tool call in flight gets the long stall limit."""
    session,usage,failed,stalled,open_tools=doc.get('session_id'),None,False,False,[0]
    for raw in codex_child.lines(stream,busy=lambda:open_tools[0]>0,**limits):
        if raw=='stalled':stalled=True;break
        try:event=json.loads(raw)
        except ValueError:continue
        if not isinstance(event,dict):continue
        kind=str(event.get('type'))
        if kind in ('tool_use','tool_call','tool_start'):open_tools[0]+=1
        elif kind in ('tool_result','tool_end'):open_tools[0]=max(0,open_tools[0]-1)
        if kind=='text' and isinstance(event.get('text'),str) and event['text'].strip():
            emit('thought',text=event['text'][:4000])
        elif kind=='result':
            session=event.get('session_id') or session
            if session:emit('session',session_id=session)
            t=event.get('tokens') if isinstance(event.get('tokens'),dict) else {}
            usage=dict(input_tokens=t.get('input'),output_tokens=t.get('output'),cache_read_input_tokens=t.get('cache_read'),cache_creation_input_tokens=t.get('cache_write'))
            failed=event.get('exit_code') not in (0,None) or bool(event.get('is_error'))
        elif kind=='error':failed=True
    return session,usage,failed,stalled


def attempt(doc,env,session_id):
    argv=[ROOT+'/python/bin/python3','-I','-c',BOOT,'chat','--query-file','/opt/workspace/tmp/hermes-prompt.txt','--oneshot','--format','stream-json',
          '--provider','openai-codex','-m',doc['model'],'-t',TOOLSETS,'--yolo']+(['--resume',session_id] if session_id else [])
    with open('/opt/workspace/tmp/hermes-prompt.txt','w') as f:f.write(doc['prompt'])
    with open(LOG,'w') as log:
        child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=log,text=True,env=env,cwd='/opt/workspace')
        session,usage,failed,stalled=relay(dict(doc,session_id=session_id),child.stdout)
        if stalled:
            child.kill();emit('error',reason='cli_stalled')
        code=child.wait()
    return session,usage,failed,code


def run(doc):
    write_home(doc)
    os.makedirs('/opt/workspace/tmp',exist_ok=True)
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH','LD_LIBRARY_PATH')}
    env.update(seat_forward.start(),HERMES_HOME=HOME,HOME='/opt/workspace',SSL_CERT_FILE=CA,REQUESTS_CA_BUNDLE=CA,NO_COLOR='1')
    session,usage,failed,code=attempt(doc,env,doc.get('session_id'))
    if doc.get('session_id') and code!=0 and session==doc.get('session_id'):
        emit('thought',text='session could not resume; started a fresh session')
        session,usage,failed,code=attempt(doc,env,None)
    emit('result',sdk_version='hermes',session_id=session,result_id=uuid.uuid4().hex,usage=usage,is_error=failed or code!=0)
    return code


if __name__=='__main__':
    try:sys.exit(run(json.loads(sys.stdin.readline())))
    except Exception as exc:
        emit('error',reason=type(exc).__name__);sys.exit(4)
