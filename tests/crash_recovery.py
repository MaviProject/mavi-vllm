"""Run only in a maintenance window: intentionally terminate the engine parent."""
import datetime,json,os,signal,subprocess,time
from pathlib import Path
import httpx

root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa/crash-recovery.json'
creds=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=30,trust_env=False)
r=c.post('/api/login',json={'username':creds['username'],'password':creds['password']});r.raise_for_status()
headers={'X-CSRF-Token':r.json()['csrf']}
def overview():
    response=c.get('/api/overview');response.raise_for_status();return response.json()
def pid():return int(subprocess.check_output(['systemctl','show','qwen3.8-vllm','-p','MainPID','--value'],text=True).strip())
view=overview();assert view['runtime']['state']=='running'
r=c.post('/api/keys',headers=headers,json={'name':'进程故障恢复验收临时Key'});r.raise_for_status();key=r.json()
before=pid();started=time.monotonic()
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'intentional_signal':'SIGKILL engine API parent','pid_before':before}
def save():out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
try:
    save();os.kill(before,signal.SIGKILL);print('Intentional engine failure injected',flush=True)
    time.sleep(3)
    assert c.get('/api/health').status_code==200
    assert httpx.get('http://127.0.0.1/health',timeout=10,trust_env=False).status_code==200
    report['management_and_gateway_stayed_online']=True
    payload={'model':view['config']['served_model_name'],'messages':[{'role':'user','content':'What is 6 times 7? Reply only with the number.'}],'max_tokens':48,'temperature':0}
    r=httpx.post('http://127.0.0.1/v1/chat/completions',headers={'Authorization':'Bearer '+key['value']},json=payload,timeout=10,trust_env=False)
    assert r.status_code==503;report['unavailable_inference_status']=r.status_code
    last=0
    while time.monotonic()-started<240:
        state=overview()
        if pid()>0 and pid()!=before and state['runtime']['state']=='running':break
        if time.monotonic()-last>=30:print('Waiting for automatic restart',round(time.monotonic()-started),flush=True);last=time.monotonic()
        time.sleep(3)
    else:raise TimeoutError('Automatic restart did not become healthy')
    report.update(pid_after=pid(),recovery_seconds=round(time.monotonic()-started,2),service=state['runtime']['service'])
    assert int(report['service']['NRestarts'])>=1
    r=httpx.post('http://127.0.0.1/v1/chat/completions',headers={'Authorization':'Bearer '+key['value']},json=payload,timeout=90,trust_env=False);r.raise_for_status()
    assert '42' in r.json()['choices'][0]['message']['content']
    report['inference_recovered']=True;report['status']='passed';save();print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
except Exception as exc:
    report.update(status='failed',error=str(exc));save();raise
finally:c.patch('/api/keys/'+key['id'],headers=headers,json={'enabled':False})
