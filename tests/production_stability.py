import argparse,base64,concurrent.futures,datetime,io,json,statistics,subprocess,time,traceback
from pathlib import Path
import httpx
from PIL import Image,ImageDraw
from transformers import AutoTokenizer

parser=argparse.ArgumentParser();parser.add_argument('--soak-seconds',type=int,default=1200);parser.add_argument('--matrix',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
ROOT=Path('/home/mavi/mavi-vllm-platform');OUT=ROOT/'data/production-qa';OUT.mkdir(exist_ok=True)
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cases':[],'profiles':[],'soak':{},'failures':[]}
if args.resume:
    report=json.loads((OUT/'stability-results.json').read_text())
    report.setdefault('previous_attempts',[]).extend(report.pop('failures',[]))
    if report.get('soak'):report.setdefault('previous_soaks',[]).append(report['soak'])
    report['soak']={}
    report['failures']=[];report['status']='running'
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=300,trust_env=False)
credentials=json.loads((ROOT/'data/bootstrap-credentials.json').read_text())
login=c.post('/api/login',json={'username':credentials['username'],'password':credentials['password']});login.raise_for_status();csrf={'X-CSRF-Token':login.json()['csrf']}
def admin(method,path,body=None):
    r=c.request(method,'/api'+path,json=body,headers=csrf);r.raise_for_status();return r.json()
def save():
    (OUT/'stability-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
def announce(text):print(datetime.datetime.now().strftime('%H:%M:%S'),text,flush=True)
def state():return admin('GET','/overview')
def wait_ready(timeout=240):
    start=time.time();last=0
    while time.time()-start<timeout:
        s=state()
        if s['runtime']['state']=='running':return s
        if s['runtime']['state']=='error':raise RuntimeError('Engine startup failed')
        if time.time()-last>=30:announce('Loading engine '+str(round(time.time()-start))+'s');last=time.time()
        time.sleep(3)
    raise TimeoutError('Engine readiness timeout')
initial=wait_ready();original=initial['config'];model=original['served_model_name']
keydata=admin('POST','/keys',{'name':'生产稳定性验收临时Key'});key=keydata['value'];keyid=keydata['id']
tok=AutoTokenizer.from_pretrained('/home/mavi/inference/models/liuxing/hf-config',local_files_only=True)
def payload(prompt,max_tokens=128,image=None,forced=False):
    content=prompt if image is None else [{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]
    return dict(model=model,messages=[{'role':'user','content':content}],temperature=0,max_tokens=max_tokens,ignore_eos=forced)
def run(body,expected=200,label='request'):
    start=time.time()
    with httpx.Client(base_url='http://127.0.0.1',timeout=600,trust_env=False) as p:r=p.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=body)
    elapsed=time.time()-start
    result=dict(case=label,status=r.status_code,seconds=round(elapsed,3),expected=expected)
    try:
        j=r.json();result['input_tokens']=j.get('usage',{}).get('prompt_tokens',0);result['output_tokens']=j.get('usage',{}).get('completion_tokens',0)
        if r.status_code>=400:result['error']=str(j.get('error',{}).get('message',''))[:180]
    except Exception:result['error']=r.text[:180]
    if r.status_code!=expected:raise AssertionError(json.dumps(result,ensure_ascii=False))
    return result
def long_prompt(target):
    text=('The warehouse inventory record is verified, complete and consistent. This passage is neutral test data. '*max(100,int(target/12)))
    ids=tok.encode(text,add_special_tokens=False)
    while len(ids)<target:ids+=ids
    return tok.decode(ids[:target-64])+'\nSummarize this neutral inventory record in plain language.'
def batch(n,body,label):
    started=time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:rows=list(pool.map(lambda _:run(body,label=label),range(n)))
    return dict(label=label,concurrency=n,wall_seconds=round(time.time()-started,3),requests=rows)
image=Image.new('RGB',(1024,1024),'white');d=ImageDraw.Draw(image)
d.rectangle((60,200,440,580),fill='red');d.ellipse((560,200,940,580),fill='blue')
buffer=io.BytesIO();image.save(buffer,format='PNG');image64=base64.b64encode(buffer.getvalue()).decode()
try:
    if not args.resume:
        announce('BASELINE AND VALIDATION')
        for extra,label in [({'stream':'false'},'invalid-stream'),({'stream_options':True},'invalid-options'),({'n':1000},'candidate-fanout'),({'max_tokens':999999},'oversized-output')]:
            report['cases'].append(run({**payload('hello'),**extra},400,label))
        report['cases'].append(run(payload(long_prompt(original['context_length']+1000),32),400,'over-context'))
        report['cases'].append(batch(2,payload('Describe the shapes and their colors.',128,image64),'two-1MP-images'))
        announce('OVERLOAD ADMISSION')
        burst=payload(long_prompt(2048),256,forced=True)
        def burst_call(_):
            try:return run(burst,200,'burst')
            except AssertionError as exc:
                item=json.loads(str(exc))
                if item['status']!=429:raise
                return item
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:overload=list(pool.map(burst_call,range(24)))
        assert any(r['status']==429 for r in overload),'No admission rejections observed'
        report['cases'].append({'case':'overload-24','responses':overload})
        save()
    profiles=[(2,12288)]
    if args.matrix:profiles=[(1,24576),(4,6144),(8,1024),(2,12288)]
    for n,context in profiles:
        if any(p['concurrency']==n and p['context']==context for p in report['profiles']):continue
        announce(f'PROFILE concurrency={n} context={context}')
        cfg=state()['config']
        admin('POST','/settings',{**cfg,'concurrency':n,'context_length':context,'apply_restart':True})
        s=wait_ready()
        assert s['applied']['concurrency']==n and s['applied']['context_length']==context
        profile=dict(concurrency=n,context=context,started=time.time())
        profile['near_context']=batch(n,payload(long_prompt(context-256),128,forced=True),'near-context')
        profile['vision']=batch(n,payload('Describe these colored shapes clearly.',128,image64),'vision-1MP')
        s=state();profile['gpu_used_mb']=s['runtime']['hardware']['gpu_used'];profile['kv_tokens']=s['runtime']['kv_tokens'];profile['restarts']=s['runtime']['service'].get('NRestarts')
        report['profiles'].append(profile);save();announce(f'PROFILE PASSED concurrency={n} context={context}')
    announce('SOAK START '+str(args.soak_seconds)+' seconds')
    cfg=state()['config']
    if (cfg['concurrency'],cfg['context_length'])!=(2,12288):
        admin('POST','/settings',{**cfg,'concurrency':2,'context_length':12288,'apply_restart':True});wait_ready()
    start=time.time();last=0;rounds=0;rows=[]
    prompts=[long_prompt(512),long_prompt(2048),long_prompt(4096),'Write a concise neutral summary of how libraries organize books.']
    while time.time()-start<args.soak_seconds:
        prompt=prompts[rounds%len(prompts)]
        if rounds%5==0:body=payload('Describe the colors and shapes shown in the image.',128,image64,True)
        else:body=payload(prompt,128,forced=True)
        result=batch(2,body,'soak');rows.extend(result['requests']);rounds+=1
        s=state();assert s['runtime']['state']=='running',json.dumps({'unexpected_runtime_state':s['runtime']['state'],'service':s['runtime'].get('service'),'health_ms':s['runtime'].get('hardware',{}).get('health_ms')})
        report['soak']={'elapsed_seconds':round(time.time()-start),'rounds':rounds,'requests':len(rows),'failures':0,'max_latency_seconds':max(r['seconds'] for r in rows),'output_tokens':sum(r['output_tokens'] for r in rows)}
        if time.time()-last>=30:
            announce('SOAK '+json.dumps(report['soak']));last=time.time();save()
    report['soak']['request_details']=rows
    report['finished']=datetime.datetime.now(datetime.timezone.utc).isoformat();report['status']='passed';save();announce('STABILITY SUITE PASSED')
except Exception as exc:
    report['failures'].append({'error':str(exc),'traceback':traceback.format_exc()});report['status']='failed';save();announce('STABILITY SUITE FAILED '+str(exc));raise
finally:
    admin('PATCH','/keys/'+keyid,{'enabled':False})
