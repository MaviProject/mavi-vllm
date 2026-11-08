import datetime,json,time
from pathlib import Path
import httpx

root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa/stream-cancel-validation.json'
credentials=json.loads((root/'data/bootstrap-credentials.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:20204',timeout=30,trust_env=False)
r=c.post('/api/login',json={'username':credentials['username'],'password':credentials['password']});r.raise_for_status();csrf={'X-CSRF-Token':r.json()['csrf']}
def overview():
    r=c.get('/api/overview');r.raise_for_status();return r.json()
before=overview();model=before['config']['served_model_name']
r=c.post('/api/keys',headers=csrf,json={'name':'流式断线专项验收临时Key'});r.raise_for_status();key=r.json()
p=httpx.Client(base_url='http://127.0.0.1',timeout=90,trust_env=False,headers={'Authorization':'Bearer '+key['value']})
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'disconnects':[]}
try:
    for index in range(12):
        mode='headers-only' if index%2==0 else 'after-first-content'
        body={'model':model,'messages':[{'role':'user','content':'Write a long neutral essay about libraries.'}],'stream':True,'max_tokens':512,'ignore_eos':True}
        with p.stream('POST','/v1/chat/completions',json=body) as response:
            assert response.status_code==200,(index,response.status_code)
            if mode=='after-first-content':
                for line in response.iter_lines():
                    if line.startswith('data:') and 'content' in line:break
        report['disconnects'].append({'mode':mode,'status':200});time.sleep(.2)
    for _ in range(2):
        with c.stream('POST','/api/playground',headers=csrf,json=body) as response:
            assert response.status_code==200
        time.sleep(.2)
    report['playground_disconnects']=2
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        state=overview()
        if state['totals']['cancelled']-before['totals']['cancelled']>=14:break
        time.sleep(1)
    report['new_cancelled_records']=state['totals']['cancelled']-before['totals']['cancelled'];assert report['new_cancelled_records']>=14
    r=p.post('/v1/chat/completions',json={'model':model,'messages':[{'role':'user','content':'What is 6 times 7? Reply only with the number.'}],'max_tokens':48,'temperature':0});r.raise_for_status();assert '42' in r.json()['choices'][0]['message']['content']
    report['normal_request_after_disconnects']=200
    text='';completed=False
    with p.stream('POST','/v1/responses',json={'model':model,'input':'What is 9 plus 8? Reply only with the number.','max_output_tokens':48,'stream':True}) as response:
        assert response.status_code==200
        for line in response.iter_lines():
            if not line.startswith('data:'):continue
            raw=line[5:].strip()
            if raw=='[DONE]':continue
            item=json.loads(raw)
            if item.get('type')=='response.output_text.delta':text+=item.get('delta','')
            if item.get('type')=='response.completed':completed=True
    assert completed and '17' in text,(completed,text)
    report['responses_streaming']=True;report['status']='passed'
except Exception as exc:
    report.update(status='failed',error=str(exc));raise
finally:
    c.patch('/api/keys/'+key['id'],headers=csrf,json={'enabled':False})
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
