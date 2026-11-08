import base64,concurrent.futures,datetime,io,json,time
from pathlib import Path
import httpx
from PIL import Image,ImageDraw
from transformers import AutoTokenizer

root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa/profile8-1k-validation.json'
credentials=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=30,trust_env=False)
r=c.post('/api/login',json={'username':credentials['username'],'password':credentials['password']});r.raise_for_status();csrf={'X-CSRF-Token':r.json()['csrf']}
def admin(method,path,body=None):
    r=c.request(method,'/api'+path,json=body,headers=csrf);r.raise_for_status();return r.json()
def ready():
    start=time.monotonic();last=0
    while time.monotonic()-start<260:
        state=admin('GET','/overview')
        if state['runtime']['state']=='running':return state
        if state['runtime']['state']=='error':raise RuntimeError('Engine startup failed')
        if time.monotonic()-last>=30:print('Waiting for engine',round(time.monotonic()-start),flush=True);last=time.monotonic()
        time.sleep(3)
    raise TimeoutError('Engine startup timed out')
original=ready()['config'];key=admin('POST','/keys',{'name':'八并发低上下文图文验收Key'})
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'running'}
tokenizer=AutoTokenizer.from_pretrained('/home/mavi/inference/models/liuxing/hf-config',local_files_only=True)
prompt=tokenizer.decode(tokenizer.encode('The neutral inventory record is complete and consistent. '*200,add_special_tokens=False)[:800])
image=Image.new('RGB',(1024,1024),'white');draw=ImageDraw.Draw(image);draw.rectangle((100,100,400,400),fill='red');draw.ellipse((600,100,900,400),fill='blue')
buffer=io.BytesIO();image.save(buffer,format='PNG');image64=base64.b64encode(buffer.getvalue()).decode()
def run(body,vision=False):
    start=time.monotonic()
    r=httpx.post('http://127.0.0.1/v1/chat/completions',headers={'Authorization':'Bearer '+key['value']},json=body,timeout=180,trust_env=False);r.raise_for_status();result=r.json()
    content=result['choices'][0]['message']['content']
    if vision:assert ('red' in content.lower() or '红' in content) and ('blue' in content.lower() or '蓝' in content),content
    return {'status':r.status_code,'seconds':round(time.monotonic()-start,3),'usage':result['usage'],**({'answer':content} if vision else {})}
try:
    admin('POST','/settings',{**original,'concurrency':8,'context_length':1024,'apply_restart':True})
    state=ready();assert state['applied']['image_max_pixels']==262144
    report['applied']=state['applied'];report['kv_tokens']=state['runtime']['kv_tokens']
    body={'model':original['served_model_name'],'messages':[{'role':'user','content':prompt}],'max_tokens':128,'ignore_eos':True,'temperature':0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:report['near_context']=list(pool.map(lambda _:run(body),range(8)))
    body['messages'][0]['content']=[{'type':'text','text':'Describe the two shapes and their colors.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+image64}}];body['ignore_eos']=False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:report['vision']=list(pool.map(lambda _:run(body,True),range(8)))
    report['status']='passed';print('8 x 1K text and image batches passed',flush=True)
except Exception as exc:
    report.update(status='failed',error=str(exc));raise
finally:
    try:
        admin('POST','/settings',{**original,'apply_restart':False});admin('POST','/control/restart')
        state=ready();report['restored_default']=state['applied']['concurrency']==2 and state['applied']['context_length']==12288
        assert report['restored_default']
    finally:
        admin('PATCH','/keys/'+key['id'],{'enabled':False})
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k not in ('near_context','vision')},ensure_ascii=False,indent=2),flush=True)
