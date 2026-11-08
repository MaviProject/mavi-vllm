"""Live integer concurrency / non-K context verification, restoring original settings."""
import base64,concurrent.futures,datetime,io,json,threading,time
from pathlib import Path
import httpx,pynvml
from PIL import Image,ImageDraw
from transformers import AutoTokenizer

root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa/custom-settings-validation.json'
credentials=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=30,trust_env=False)
r=c.post('/api/login',json={'username':credentials['username'],'password':credentials['password']});r.raise_for_status();csrf={'X-CSRF-Token':r.json()['csrf']}
def admin(method,path,body=None):
    r=c.request(method,'/api'+path,json=body,headers=csrf);r.raise_for_status();return r.json()
def ready(n=None,context=None):
    start=time.monotonic();last=0
    while time.monotonic()-start<260:
        state=admin('GET','/overview');applied=state['applied']
        if state['runtime']['state']=='error':raise RuntimeError('Engine startup failed')
        if state['runtime']['state']=='running' and (n is None or (applied['concurrency'],applied['context_length'])==(n,context)):
            try:
                if httpx.get('http://127.0.0.1:8000/health',trust_env=False,timeout=3).status_code==200:return state
            except httpx.HTTPError:pass
        if time.monotonic()-last>=30:print('Loading',n,context,round(time.monotonic()-start),'seconds',flush=True);last=time.monotonic()
        time.sleep(3)
    raise TimeoutError('Engine readiness timeout')
original=ready()['config'];key=admin('POST','/keys',{'name':'自定义并发与上下文验收Key'})
tokenizer=AutoTokenizer.from_pretrained('/home/mavi/inference/models/liuxing/hf-config',local_files_only=True)
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'profiles':[],'status':'running'}
image=Image.new('RGB',(1024,1024),'white');draw=ImageDraw.Draw(image);draw.rectangle((100,100,400,400),fill='red');draw.ellipse((600,100,900,400),fill='blue')
buffer=io.BytesIO();image.save(buffer,format='PNG');image64=base64.b64encode(buffer.getvalue()).decode()
pynvml.nvmlInit();device=pynvml.nvmlDeviceGetHandleByIndex(0);samples=[];stop=threading.Event()
def monitor():
    while not stop.is_set():
        memory=pynvml.nvmlDeviceGetMemoryInfo(device)
        samples.append(dict(time=time.time(),used=memory.used/2**20,free=memory.free/2**20))
        stop.wait(.5)
thread=threading.Thread(target=monitor,daemon=True);thread.start()
def call(body,vision=False):
    start=time.monotonic()
    r=httpx.post('http://127.0.0.1/v1/chat/completions',headers={'Authorization':'Bearer '+key['value']},json=body,trust_env=False,timeout=180);r.raise_for_status();data=r.json()
    answer=data['choices'][0]['message']['content']
    if vision:assert 'red' in answer.lower() and 'blue' in answer.lower(),answer
    return {'status':r.status_code,'seconds':round(time.monotonic()-start,3),'usage':data['usage'],**({'answer':answer} if vision else {})}
try:
    for n,context in ((3,5001),(5,1501)):
        sample_start=len(samples)
        print('CUSTOM PROFILE',n,context,flush=True)
        admin('POST','/settings',{**original,'concurrency':n,'context_length':context,'apply_restart':True})
        state=ready(n,context)
        assert (state['config']['concurrency'],state['config']['context_length'])==(n,context)
        assert int(state['runtime']['service']['NRestarts'])==0
        models=httpx.get('http://127.0.0.1/v1/models',headers={'Authorization':'Bearer '+key['value']},trust_env=False).json()
        assert models['data'][0]['max_model_len']==context
        ids=tokenizer.encode('The neutral warehouse inventory record is complete and consistent. '*1000,add_special_tokens=False)
        prompt=tokenizer.decode(ids[:context-320])
        body={'model':original['served_model_name'],'messages':[{'role':'user','content':prompt}],'max_tokens':128,'ignore_eos':True,'temperature':0}
        profile={'concurrency':n,'context_length':context,'applied':state['applied']}
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:profile['near_context']=list(pool.map(lambda _:call(body),range(n)))
        body['messages'][0]['content']=[{'type':'text','text':'Describe the two shapes and their colors.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+image64}}];body['ignore_eos']=False
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:profile['vision']=list(pool.map(lambda _:call(body,True),range(n)))
        rows=samples[sample_start:];profile.update(gpu_peak_used_mb=max(s['used'] for s in rows),gpu_min_free_mb=min(s['free'] for s in rows))
        report['profiles'].append(profile);out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('CUSTOM PROFILE PASSED',n,context,'minimum free MiB',profile['gpu_min_free_mb'],flush=True)
    report['status']='passed'
except Exception as exc:
    report.update(status='failed',error=str(exc));raise
finally:
    try:
        admin('POST','/settings',{**original,'apply_restart':False});admin('POST','/control/restart')
        state=ready(original['concurrency'],original['context_length'])
        report['restored_original']=state['config']==original
        assert report['restored_original']
    finally:
        stop.set();thread.join(timeout=2);pynvml.nvmlShutdown()
        admin('PATCH','/keys/'+key['id'],{'enabled':False})
        report['finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k!='profiles'},ensure_ascii=False,indent=2),flush=True)
