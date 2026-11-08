import base64
import concurrent.futures
import json
from pathlib import Path
import subprocess
import time
import httpx

ROOT=Path('/home/mavi/mavi-vllm-platform')
ADMIN='http://127.0.0.1:20204'
PUBLIC='http://127.0.0.1'
creds=json.loads((ROOT/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url=ADMIN,timeout=180,trust_env=False)
login=c.post('/api/login',json={'username':creds['username'],'password':creds['password']});login.raise_for_status()
csrf={'X-CSRF-Token':login.json()['csrf']}
report={}
def admin(method,path,body=None):
    r=c.request(method,'/api'+path,json=body,headers=csrf);r.raise_for_status();return r.json()
def public(path,body=None,key=None):
    with httpx.Client(base_url=PUBLIC,timeout=180,trust_env=False) as p:
        return p.request('POST' if body is not None else 'GET',path,json=body,headers={'Authorization':'Bearer '+key} if key else {})
def wait_state(wanted,timeout=240):
    started=time.time();last=0
    while time.time()-started<timeout:
        view=admin('GET','/overview')
        if view['runtime']['state']==wanted:return view
        if time.time()-last>30:
            print('Waiting',wanted,'current',view['runtime']['state'],round(time.time()-started),'seconds',flush=True);last=time.time()
        time.sleep(3)
    raise RuntimeError('Timed out waiting for '+wanted)

view=wait_state('running')
original=view['config']
key=admin('POST','/keys',{'name':'平台验收临时 Key'})
token=key['value'];kid=key['id'];alias='mavi-validation-model'
try:
    assert public('/v1/models').status_code==401
    assert public('/v1/models',key=token).status_code==200
    admin('PATCH','/keys/'+kid,{'enabled':False})
    assert public('/v1/models',key=token).status_code==401
    admin('PATCH','/keys/'+kid,{'enabled':True})
    report['multiple_keys_and_immediate_revocation']=True
    settings={**original,'served_model_name':alias,'thinking_enabled':False,'apply_restart':False}
    admin('POST','/settings',settings)
    r=public('/v1/chat/completions',{'model':alias,'messages':[{'role':'user','content':'What is 6 times 7? Reply only with the number.'}],'max_tokens':64,'temperature':0,'reasoning_effort':'high','chat_template_kwargs':{'enable_thinking':True}},token)
    r.raise_for_status();j=r.json()
    assert j['model']==alias and '42' in j['choices'][0]['message']['content']
    assert not j['choices'][0]['message'].get('reasoning')
    assert j['usage'].get('completion_tokens_details',{}).get('reasoning_tokens',0)==0
    report['strict_thinking_off_with_explicit_enable']=True
    report['model_alias_hot_change']=True
    r=public('/v1/responses',{'model':alias,'input':'What is 9 plus 8? Reply only with the number.','max_output_tokens':64,'reasoning':{'effort':'high'},'chat_template_kwargs':{'enable_thinking':True}},token)
    r.raise_for_status();report['responses_api']=r.status_code
    image=base64.b64encode(Path('/home/mavi/inference/logs/vision-test.png').read_bytes()).decode()
    payload={'model':alias,'messages':[{'role':'user','content':[{'type':'text','text':'Read the four-digit number at the top of this image. Reply with only the number.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]}],'temperature':0,'max_tokens':48}
    r=public('/v1/chat/completions',payload,token);r.raise_for_status()
    assert '7319' in r.json()['choices'][0]['message']['content']
    report['vision_through_authenticated_gateway']=True
    with httpx.Client(base_url=PUBLIC,timeout=180,trust_env=False) as p:
        with p.stream('POST','/v1/chat/completions',json={'model':alias,'messages':[{'role':'user','content':'Count from 1 to 20.'}],'stream':True,'max_tokens':80,'stream_options':{'include_usage':True}},headers={'Authorization':'Bearer '+token}) as stream:
            usage=None;done=False
            for line in stream.iter_lines():
                if not line.startswith('data:'):continue
                data=line[5:].strip()
                if data=='[DONE]':done=True;continue
                obj=json.loads(data)
                if obj.get('model'):assert obj['model']==alias
                if obj.get('usage'):usage=obj['usage']
        assert done and usage['completion_tokens']>0
    report['streaming_alias_and_usage']=True
    with httpx.Client(base_url=PUBLIC,timeout=180,trust_env=False) as p:
        with p.stream('POST','/v1/chat/completions',json={'model':alias,'messages':[{'role':'user','content':'Write a long essay about the history of astronomy.'}],'stream':True,'max_tokens':1024},headers={'Authorization':'Bearer '+token}) as stream:
            for line in stream.iter_lines():
                if line.startswith('data:') and 'content' in line:break
    time.sleep(2)
    before=admin('GET','/overview')
    report['cancelled_requests_recorded']=before['totals']['cancelled']>0
    report['gpu_before_stop_mb']=before['runtime']['hardware']['gpu_used']
    admin('POST','/control/stop')
    stopped=wait_state('stopped',60)
    gpu=float(subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True).strip())
    assert gpu<1000,'Model memory was not released'
    report['gpu_after_stop_mb']=gpu
    assert c.get('/api/health').status_code==200
    assert public('/v1/chat/completions',{'model':alias,'messages':[{'role':'user','content':'hi'}]},token).status_code==503
    report['admin_stays_online_when_model_unloaded']=True
    rec=admin('GET','/recommendations')
    profile=next(x for x in rec['profiles'] if x['concurrency']==2)
    settings={**original,'concurrency':2,'context_length':profile['context_length'],'thinking_enabled':False,'apply_restart':True}
    admin('POST','/settings',settings)
    admin('POST','/control/start')
    running=wait_state('running',240)
    def call(n):
        response=public('/v1/chat/completions',{'model':original['served_model_name'],'messages':[{'role':'user','content':f'What is {n} plus 100? Reply only with the number.'}],'temperature':0,'max_tokens':48},token)
        response.raise_for_status();data=response.json();assert str(n+100) in data['choices'][0]['message']['content'];return response.status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(call,[11,22]))
    report['two_parallel_requests']=result
    report['final_concurrency']=running['config']['concurrency']
    report['final_context_length']=running['config']['context_length']
    assert running['totals']['requests']>=5
    assert c.get('/api/export').status_code==200
    report['csv_export']=True
    report['persistent_request_count']=running['totals']['requests']
    report['final_state']=running['runtime']['state']
finally:
    admin('DELETE','/keys/'+kid)
    cfg=admin('GET','/overview')['config']
    if cfg['served_model_name']!=original['served_model_name']:
        admin('POST','/settings',{**cfg,'served_model_name':original['served_model_name'],'apply_restart':False})
(ROOT/'data/integration-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
