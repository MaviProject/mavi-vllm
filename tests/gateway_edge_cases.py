import base64,json,time
from pathlib import Path
import httpx

root=Path('/home/mavi/mavi-vllm-platform')
creds=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',trust_env=False,timeout=45)
login=c.post('/api/login',json={'username':creds['username'],'password':creds['password']});login.raise_for_status()
headers={'X-CSRF-Token':login.json()['csrf']}
r=c.post('/api/keys',json={'name':'网关边界测试临时Key'},headers=headers);r.raise_for_status();key=r.json()
model=c.get('/api/overview').json()['config']['served_model_name']
p=httpx.Client(base_url='http://127.0.0.1',trust_env=False,timeout=45,headers={'Authorization':'Bearer '+key['value']})
rows=[]
def check(name,r,wanted):
    rows.append(dict(case=name,status=r.status_code,expected=wanted));assert r.status_code==wanted,(name,r.status_code,r.text[:200])
try:
    check('non-finite-json',p.post('/v1/chat/completions',content='{"model":"'+model+'","temperature":NaN}'),400)
    check('beam-search',p.post('/v1/chat/completions',json={'model':model,'use_beam_search':True}),400)
    valid_image='data:image/png;base64,'+base64.b64encode(Path('/home/mavi/inference/logs/vision-test.png').read_bytes()).decode()
    quantity=p.post('/v1/chat/completions',json={'model':model,'messages':[{'role':'user','content':[{'type':'text','text':'Describe both images.'}]+[{'type':'image_url','image_url':{'url':valid_image}}]*2}],'max_tokens':48})
    check('too-many-images',quantity,400)
    rows[-1]['message']=quantity.json()['error']['message']
    assert '1' in rows[-1]['message'] and 'image' in rows[-1]['message'].lower(),rows[-1]['message']
    check('large-body-chunked',p.post('/v1/chat/completions',content=(b'x'*1048576 for _ in range(26))),413)
    def slow_body():
        yield b'{"model":'
        time.sleep(32)
        yield b'"'+model.encode()+b'"}'
    start=time.monotonic();check('slow-body-deadline',p.post('/v1/chat/completions',content=slow_body()),408)
    rows[-1]['elapsed_seconds']=round(time.monotonic()-start,2)
    check('private-backend-endpoint',p.post('/invocations',json={}),404)
    check('metrics-not-public',p.get('/metrics'),404)
    check('secret-file-inaccessible',c.get('/static/../data/bootstrap-credentials.json'),404)
finally:
    c.patch('/api/keys/'+key['id'],headers=headers,json={'enabled':False})
(root/'data/production-qa/gateway-edge-cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
print(json.dumps(rows,ensure_ascii=False,indent=2))
