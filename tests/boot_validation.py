"""Record pre-reboot state, or validate service startup and data after reboot."""
import argparse,datetime,json,subprocess,time
from pathlib import Path
import httpx

parser=argparse.ArgumentParser();parser.add_argument('--before',action='store_true');args=parser.parse_args()
root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa'
units=['mavi-vllm-platform','mavi-vllm-gateway','qwen3.8-vllm']
creds=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=20,trust_env=False)
r=c.post('/api/login',json={'username':creds['username'],'password':creds['password']});r.raise_for_status()
headers={'X-CSRF-Token':r.json()['csrf']}
def view():
    r=c.get('/api/overview');r.raise_for_status();return r.json()
boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
if args.before:
    state=view()
    report={'boot_id':boot_id,'config':state['config'],'requests':state['totals']['requests'],'time':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (out/'pre-reboot.json').write_text(json.dumps(report,indent=2));print('Pre-reboot baseline saved')
else:
    before=json.loads((out/'pre-reboot.json').read_text());assert boot_id!=before['boot_id'],'Host has not rebooted'
    start=time.monotonic();last=0
    while time.monotonic()-start<300:
        state=view()
        if state['runtime']['state']=='running':break
        if time.monotonic()-last>=30:print('Waiting for boot inference readiness',round(time.monotonic()-start),flush=True);last=time.monotonic()
        time.sleep(3)
    else:raise TimeoutError('Inference did not become ready after reboot')
    enabled={u:subprocess.check_output(['systemctl','is-enabled',u],text=True).strip() for u in units}
    active={u:subprocess.check_output(['systemctl','is-active',u],text=True).strip() for u in units}
    assert all(v=='enabled' for v in enabled.values()) and all(v=='active' for v in active.values())
    assert state['config']==before['config'] and state['totals']['requests']>=before['requests']
    assert (state['applied']['concurrency'],state['applied']['context_length'])==(2,12288)
    report={'status':'passed','boot_id_changed':True,'services_enabled':enabled,'services_active':active,'settings_persisted':True,'requests_before':before['requests'],'requests_after':state['totals']['requests'],'runtime_state':state['runtime']['state'],'applied':state['applied'],'driver':state['runtime']['hardware']['driver'],'gpu_used_mb':state['runtime']['hardware']['gpu_used'],'gpu_total_mb':state['runtime']['hardware']['gpu_total'],'time':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (out/'boot-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
