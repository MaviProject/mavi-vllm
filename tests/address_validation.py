"""Verify another real loopback IP and future Host/Origin values without changing NICs."""
import base64,datetime,json
from pathlib import Path
import httpx

root=Path('/home/mavi/mavi-vllm-platform');out=root/'data/production-qa/address-validation.json'
credentials=json.loads((root/'data/bootstrap-credentials.json').read_text())
report={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'management':[],'api':[],'status':'running','method':'HTTP on 127.0.0.1 / 127.0.0.2 and simulated future Host / Origin; no NIC address modification'}
def login(client,origin):
    r=client.post('/api/login',headers={'Origin':origin},json={'username':credentials['username'],'password':credentials['password']});r.raise_for_status();return {'X-CSRF-Token':r.json()['csrf']}
c=httpx.Client(base_url='http://127.0.0.1:20204',trust_env=False,timeout=30)
csrf=login(c,'http://127.0.0.1:20204')
model=c.get('/api/overview').json()['config']['served_model_name']
r=c.post('/api/keys',headers=csrf,json={'name':'IP变化兼容验收Key'});r.raise_for_status();key=r.json()
try:
    for base in ('http://127.0.0.1:20204','http://127.0.0.2:20204'):
        with httpx.Client(base_url=base,trust_env=False,timeout=30) as client:
            headers=login(client,base)
            statuses={path:client.get(path).status_code for path in ('/api/session','/api/overview','/api/recommendations','/api/logs','/api/export')}
            assert all(code==200 for code in statuses.values()),statuses
            wrong=client.post('/api/login',headers={'Origin':'http://different-host.invalid:20204'},json={'username':credentials['username'],'password':credentials['password']})
            assert wrong.status_code==403
            report['management'].append({'origin':base,'login':200,'authenticated_paths':statuses,'cross_origin_login':403})
            if '127.0.0.2' in base:
                image=base64.b64encode(Path('/home/mavi/inference/logs/vision-test.png').read_bytes()).decode()
                r=client.post('/api/playground',headers=headers,json={'model':model,'messages':[{'role':'user','content':[{'type':'text','text':'Read the four-digit number at the top of this image. Reply with only the number.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]}],'max_tokens':48,'temperature':0})
                r.raise_for_status();assert '7319' in r.json()['choices'][0]['message']['content'];report['alternate_ip_playground_vision']=True
    for host in ('192.168.50.88:20204','10.23.45.67:20204','mavi-server.local:20204'):
        with httpx.Client(base_url='http://127.0.0.2:20204',headers={'Host':host},trust_env=False,timeout=30) as client:
            login(client,'http://'+host);assert client.get('/api/session').status_code==200
            report['management'].append({'simulated_host':host,'same_origin_login':200,'session':200})
    for base in ('http://127.0.0.1','http://127.0.0.2'):
        with httpx.Client(base_url=base,trust_env=False,timeout=90) as client:
            assert client.get('/v1/models').status_code==401
            headers={'Authorization':'Bearer '+key['value']}
            assert client.get('/v1/models',headers=headers).status_code==200
            r=client.post('/v1/chat/completions',headers=headers,json={'model':model,'messages':[{'role':'user','content':'What is 6 times 7? Reply only with the number.'}],'max_tokens':48,'temperature':0})
            r.raise_for_status();assert '42' in r.json()['choices'][0]['message']['content']
            report['api'].append({'base_url':base+'/v1','unauthenticated':401,'authenticated_models':200,'text_inference':200})
    report['status']='passed'
except Exception as exc:
    report.update(status='failed',error=str(exc));raise
finally:
    c.patch('/api/keys/'+key['id'],headers=csrf,json={'enabled':False})
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
