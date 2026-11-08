import asyncio
import json
import os
import sqlite3
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

tmp=tempfile.TemporaryDirectory()
root=Path(tmp.name)
(root/'static').mkdir()
(root/'static/index.html').write_text('test')
os.environ['MAVI_PLATFORM_ROOT']=str(root)
os.environ['MAVI_EXISTING_API_ENV']=str(root/'no-existing-env')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import core
import main
import gateway
import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

async def idle():
    while True:await asyncio.sleep(60)

class PlatformTests(unittest.TestCase):
    def setUp(self):
        core.initialize()
        self.password=json.loads((root/'data/bootstrap-credentials.json').read_text())['password']
        self.mon=patch('main.monitor',idle);self.mon.start()
        self.ctrl=patch('main.control',AsyncMock());self.control=self.ctrl.start()
        self.client=TestClient(main.app);self.client.__enter__()
        login=self.client.post('/api/login',json={'username':'admin','password':self.password})
        self.assertEqual(login.status_code,200)
        self.headers={'X-CSRF-Token':login.json()['csrf']}
        with core.connect() as c:c.execute('DELETE FROM login_attempts')
    def tearDown(self):
        self.client.__exit__(None,None,None);self.mon.stop();self.ctrl.stop()

    def test_auth_and_csrf(self):
        anon=TestClient(main.app)
        self.assertEqual(anon.get('/api/overview').status_code,401)
        self.assertEqual(self.client.post('/api/control/stop').status_code,403)
        self.assertEqual(self.client.post('/api/control/stop',headers=self.headers).status_code,200)
        self.control.assert_awaited_with('stop')

    def test_login_and_sessions_follow_current_host(self):
        for origin in ('http://192.168.50.88:20204','http://10.23.45.67:20204','http://mavi-server.local:20204','http://127.0.0.2:20204'):
            client=TestClient(main.app,base_url=origin)
            try:
                r=client.post('/api/login',headers={'Origin':origin},json={'username':'admin','password':self.password})
                self.assertEqual(r.status_code,200,(origin,r.text))
                self.assertEqual(client.get('/api/session').status_code,200)
                self.assertEqual(client.get('/api/overview').status_code,200)
                self.assertEqual(client.get('/api/export').status_code,200)
                rejected=client.post('/api/login',headers={'Origin':'http://different-host.invalid:20204'},json={'username':'admin','password':self.password})
                self.assertEqual(rejected.status_code,403)
            finally:client.close()

    def test_database_connections_close_and_roll_back(self):
        with core.connect() as connection:
            connection.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
        with self.assertRaises(RuntimeError):
            with core.connect() as connection:
                connection.execute("INSERT INTO meta VALUES ('rollback-test','true')")
                raise RuntimeError('abort transaction')
        self.assertIsNone(core.get_meta('rollback-test'))

    def test_short_context_launcher_reserves_image_space(self):
        previous=core.get_meta('applied_config')
        try:
            for n,context,pixels in ((8,1024,262144),(3,1537,393216),(2,12288,1048576)):
                cfg={**core.config(),'concurrency':n,'context_length':context}
                with patch('core.config',return_value=cfg),patch('os.execv') as launch,patch.dict(os.environ):
                    runpy.run_path(str(Path(__file__).resolve().parents[1]/'backend/engine_launcher.py'))
                arguments=launch.call_args.args[1]
                self.assertEqual(Path(arguments[2]),core.MODEL_DIR/core.MODEL_WEIGHT)
                loader=json.loads(arguments[arguments.index('--model-loader-extra-config')+1])
                self.assertEqual(Path(loader['mm_proj']),core.MODEL_DIR/core.VISION_WEIGHT)
                mm=json.loads(arguments[arguments.index('--mm-processor-kwargs')+1])
                self.assertEqual(mm['size']['longest_edge'],pixels)
                self.assertEqual(core.get_meta('applied_config')['image_max_pixels'],pixels)
        finally:core.set_meta('applied_config',previous)

    def test_playground_header_disconnect_closes_upstream(self):
        upstream=httpx.Response(200,headers={'content-type':'text/event-stream'},stream=BytesStream([b'data: {}\n\n']))
        async def handler(request):return upstream
        self.client.app.state.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def exercise():
            scope={'type':'http','method':'POST','path':'/api/playground','headers':[],'app':main.app,'asgi':{'spec_version':'2.4'}}
            async def receive():return {'type':'http.request','body':b'{}','more_body':False}
            response=await main.playground(Request(scope,receive),s={})
            async def send(message):raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):await response(scope,receive,send)
        asyncio.run(exercise());self.assertTrue(upstream.is_closed)

    def test_password_invalidates_old_session(self):
        old=TestClient(main.app)
        old.cookies.update(self.client.cookies)
        new='Temporary_Test_password_38'
        r=self.client.post('/api/password',headers=self.headers,json={'current_password':self.password,'new_password':new})
        self.assertEqual(r.status_code,200)
        self.assertEqual(old.get('/api/session').status_code,401)
        self.headers={'X-CSRF-Token':r.json()['csrf']}
        with core.connect() as c:c.execute('UPDATE users SET password=? WHERE username=?',(core.password_hash(self.password),'admin'))

    def test_failed_login_rate_limited(self):
        anon=TestClient(main.app)
        for _ in range(10):self.assertEqual(anon.post('/api/login',json={'username':'admin','password':'wrong'}).status_code,401)
        self.assertEqual(anon.post('/api/login',json={'username':'admin','password':'wrong'}).status_code,429)
        with core.connect() as c:c.execute('DELETE FROM login_attempts')

    def test_key_lifecycle_and_encryption(self):
        r=self.client.post('/api/keys',headers=self.headers,json={'name':'unit test'})
        self.assertEqual(r.status_code,200);data=r.json()
        self.assertIsNotNone(core.lookup_key(data['value']))
        self.assertEqual(self.client.get('/api/keys/'+data['id']+'/reveal').json()['value'],data['value'])
        with core.connect() as c:
            row=c.execute('SELECT encrypted FROM api_keys WHERE id=?',(data['id'],)).fetchone()
        self.assertNotIn(data['value'],row['encrypted'])
        self.client.patch('/api/keys/'+data['id'],headers=self.headers,json={'enabled':False})
        self.assertIsNone(core.lookup_key(data['value']))
        self.client.delete('/api/keys/'+data['id'],headers=self.headers)

    def test_capacity_recommendations_and_settings(self):
        p=self.client.get('/api/recommendations').json()['profiles']
        self.assertEqual([x['concurrency'] for x in p],[1,2,4,8])
        self.assertTrue(all(p[i]['context_length']>=p[i+1]['context_length'] for i in range(3)))
        cfg=core.config();bad={**cfg,'concurrency':8,'context_length':32768}
        self.assertEqual(self.client.post('/api/settings',headers=self.headers,json=bad).status_code,400)
        changed={**cfg,'served_model_name':'mavi-test-model'}
        self.assertEqual(self.client.post('/api/settings',headers=self.headers,json=changed).status_code,200)
        self.assertEqual(core.config()['served_model_name'],'mavi-test-model')
        core.set_meta('config',cfg)
        self.assertEqual(p[-1]['max_context'],1024)
        core.set_meta('capacity_profiles',{'4':{'kv_tokens':16000}})
        constrained=next(x for x in core.recommendations()['profiles'] if x['concurrency']==4)
        self.assertLessEqual(constrained['max_context']*4,16000*.85)
        core.set_meta('capacity_profiles',{})

    def test_pending_settings_apply_from_applied_state(self):
        cfg=core.config();old_state=main.CACHE['state']
        core.set_meta('applied_config',{'concurrency':1,'context_length':8192})
        main.CACHE['state']='running'
        changed={**cfg,'concurrency':2,'context_length':4096,'apply_restart':False}
        self.assertEqual(self.client.post('/api/settings',headers=self.headers,json=changed).status_code,200)
        self.control.assert_not_awaited()
        changed['apply_restart']=True
        self.assertEqual(self.client.post('/api/settings',headers=self.headers,json=changed).status_code,200)
        self.control.assert_awaited_with('restart')
        core.set_meta('config',cfg);main.CACHE['state']=old_state

    def test_custom_concurrency_and_exact_context_persist(self):
        original=core.config()
        try:
            result=self.client.get('/api/recommendations').json()
            self.assertEqual([p['concurrency'] for p in result['custom_profiles']],list(range(1,9)))
            for n,context in ((3,5001),(5,2501),(6,2049),(7,1031)):
                changed={**original,'concurrency':n,'context_length':context,'apply_restart':False}
                r=self.client.post('/api/settings',headers=self.headers,json=changed)
                self.assertEqual(r.status_code,200,r.text)
                saved=self.client.get('/api/overview').json()['config']
                self.assertEqual((saved['concurrency'],saved['context_length']),(n,context))
                too_large={**changed,'context_length':core.capacity_profile(n)['max_context']+1}
                self.assertEqual(self.client.post('/api/settings',headers=self.headers,json=too_large).status_code,400)
                self.assertEqual(core.config(),saved)
            self.control.assert_not_awaited()
        finally:core.set_meta('config',original)

    def test_custom_values_require_integers_and_safe_ranges(self):
        cfg=core.config()
        for field,values in (('concurrency',(0,9,2.5,True)),('context_length',(1023,32769,5001.5,True))):
            for value in values:
                r=self.client.post('/api/settings',headers=self.headers,json={**cfg,field:value})
                self.assertEqual(r.status_code,422,(field,value,r.text))
        self.assertEqual(core.config(),cfg)

    def test_small_context_measurement_preserves_capacity_reference(self):
        old_calibration=core.get_meta('calibration');old_profiles=core.get_meta('capacity_profiles',{})
        try:
            core.record_calibration(dict(concurrency=4,context_length=6144,kv_tokens=28964))
            core.record_calibration(dict(concurrency=4,context_length=1501,kv_tokens=10000))
            self.assertEqual(core.get_meta('calibration')['context_length'],1501)
            self.assertEqual(core.get_meta('capacity_profiles')['4']['kv_tokens'],28964)
            self.assertEqual(core.capacity_profile(4)['max_context'],6144)
        finally:
            core.set_meta('calibration',old_calibration);core.set_meta('capacity_profiles',old_profiles)

    def test_chart_retains_full_hour(self):
        import time
        now=time.time()
        with core.connect() as c:
            for i in range(720):
                ts=now-3595+i*5
                c.execute('INSERT OR REPLACE INTO samples VALUES (?,?)',(ts,json.dumps({'created':ts,'output_tps':1})))
        history=core.usage_stats()['history']
        self.assertLess(history[0]['created'],now-3000)
        self.assertGreaterEqual(history[-1]['created'],now-5)
        with core.connect() as c:c.execute('DELETE FROM samples')

    def test_metrics_timeout_does_not_mark_healthy_engine_loading(self):
        class Probe:
            async def get(self,url,**kwargs):
                if url.endswith('/health'):return httpx.Response(200)
                raise httpx.ReadTimeout('metrics timeout')
            async def aclose(self):pass
        async def fake_command(*args,**kwargs):
            if args[0]=='nvidia-smi':return 'RTX 4090,595.99.02,22000,24564,90,65,300'
            return 'ActiveState=active\nSubState=running\nNRestarts=0'
        async def stop(*args):raise asyncio.CancelledError()
        old=main.CACHE.copy();main.CACHE.update(metrics={'vllm:prompt_tokens_total':100},kv_tokens=12345)
        self.mon.stop()
        try:
            with patch('main.httpx.AsyncClient',return_value=Probe()),patch('main.command',fake_command),patch('main.asyncio.sleep',stop):
                with self.assertRaises(asyncio.CancelledError):asyncio.run(main.monitor())
            self.assertEqual(main.CACHE['state'],'running')
            self.assertEqual(main.CACHE['kv_tokens'],12345)
            self.assertEqual(main.CACHE['metrics']['vllm:prompt_tokens_total'],100)
        finally:
            main.CACHE.clear();main.CACHE.update(old);self.mon.start()

    def test_health_transport_retry_and_persistent_failure(self):
        class Probe:
            def __init__(self,failures):self.failures=failures;self.calls=0
            async def get(self,url):
                self.calls+=1
                if self.calls<=self.failures:raise httpx.RemoteProtocolError('peer closed idle connection')
                return httpx.Response(200)
        probe=Probe(1)
        self.assertEqual(asyncio.run(main.probe_health(probe)),(True,200,None))
        self.assertEqual(probe.calls,2)
        probe=Probe(2);result=asyncio.run(main.probe_health(probe))
        self.assertFalse(result[0]);self.assertIn('RemoteProtocolError',result[2])
        self.assertEqual(probe.calls,2)

class BytesStream(httpx.AsyncByteStream):
    def __init__(self,chunks):self.chunks=chunks
    async def __aiter__(self):
        for chunk in self.chunks:yield chunk

class GatewayTests(unittest.TestCase):
    def setUp(self):
        core.initialize();self.old=core.config();core.set_meta('config',{**self.old,'served_model_name':'unit-model','thinking_enabled':False})
        self.key=core.first_key();self.headers={'Authorization':'Bearer '+self.key}
        self.client=TestClient(gateway.app);self.client.__enter__();self.bodies=[]
    def tearDown(self):
        self.client.__exit__(None,None,None);core.set_meta('config',self.old)
    def mock_backend(self,stream=False,error=False):
        async def handler(request):
            body=json.loads(request.content);self.bodies.append(body)
            if stream:
                events=[{'model':core.INTERNAL_MODEL,'choices':[{'delta':{'content':'hello'}}]},
                        {'model':core.INTERNAL_MODEL,'choices':[],'usage':{'prompt_tokens':5,'completion_tokens':1}}]
                if error:events.insert(1,{'error':{'message':'test error'}})
                data=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
                return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=BytesStream([data[:13].encode(),data[13:].encode()]))
            return httpx.Response(200,json={'model':core.INTERNAL_MODEL,'choices':[{'message':{'content':'ok'}}],'usage':{'prompt_tokens':5,'completion_tokens':1}})
        self.client.app.state.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_auth_and_allowlist(self):
        self.assertEqual(self.client.get('/v1/models').status_code,401)
        self.assertEqual(self.client.get('/v1/models',headers=self.headers).json()['data'][0]['id'],'unit-model')
        self.assertEqual(self.client.post('/invocations',headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/metrics',headers=self.headers).status_code,404)

    def test_strict_thinking_and_model_alias(self):
        self.mock_backend()
        r=self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model','messages':[{'role':'user','content':'x'}],'reasoning_effort':'high','chat_template_kwargs':{'enable_thinking':True}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['model'],'unit-model')
        self.assertFalse(self.bodies[0]['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(self.bodies[0]['reasoning_effort'],'none')
        self.assertEqual(self.bodies[0]['model'],core.INTERNAL_MODEL)
        self.assertEqual(self.client.post('/v1/completions',headers=self.headers,json={'model':'unit-model','prompt':'<think>'}).status_code,400)

    def test_sse_usage_and_errors(self):
        self.mock_backend(stream=True)
        r=self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model','messages':[],'stream':True})
        self.assertIn('unit-model',r.text);self.assertIn('[DONE]',r.text)
        self.assertNotIn('prompt_tokens',r.text)
        self.assertTrue(self.bodies[0]['stream_options']['include_usage'])
        with core.connect() as c:row=dict(c.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 1').fetchone())
        self.assertEqual(row['output_tokens'],1);self.assertEqual(row['outcome'],'success')
        self.mock_backend(stream=True,error=True)
        self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model','stream':True})
        with core.connect() as c:row=dict(c.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 1').fetchone())
        self.assertEqual(row['outcome'],'error')

    def test_responses_policy(self):
        self.mock_backend()
        r=self.client.post('/v1/responses',headers=self.headers,json={'model':'unit-model','input':'hi','reasoning':{'effort':'high'}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.bodies[0]['reasoning']['effort'],'none')

    def test_chunked_body_limit(self):
        def huge_body():
            for _ in range(26):yield b'x'*(1024*1024)
        self.assertEqual(self.client.post('/v1/chat/completions',headers=self.headers,content=huge_body()).status_code,413)

    def test_invalid_structures_do_not_raise_500(self):
        for extra in [{'stream':'false'},{'stream_options':True},{'stream_options':{'include_usage':'yes'}},{'chat_template_kwargs':[]},{'n':100},{'best_of':10},{'use_beam_search':True},{'beam_width':8},{'max_tokens':999999}]:
            r=self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model',**extra})
            self.assertEqual(r.status_code,400,extra)
        r=self.client.post('/v1/chat/completions',headers=self.headers,content='{"model":"unit-model","temperature":NaN}')
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.client.app.state.admitted,0)
        with core.connect() as c:
            row=c.execute('SELECT status,outcome FROM requests ORDER BY created DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(row),(400,'error'))

    def test_admission_rejection_and_release(self):
        limit=min(16,core.get_meta('applied_config',core.config())['concurrency']+6)
        self.client.app.state.admitted=limit
        r=self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model'})
        self.assertEqual(r.status_code,429);self.assertEqual(r.headers['retry-after'],'3')
        self.assertEqual(self.client.app.state.admitted,limit)
        self.client.app.state.admitted=0
        self.mock_backend(stream=True)
        self.client.post('/v1/chat/completions',headers=self.headers,json={'model':'unit-model','stream':True})
        self.assertEqual(self.client.app.state.admitted,0)

    def test_disconnect_before_stream_headers_releases_admission(self):
        self.mock_backend(stream=True)
        async def exercise():
            body=json.dumps({'model':'unit-model','stream':True}).encode()
            scope={'type':'http','method':'POST','path':'/v1/chat/completions','headers':[(b'authorization',('Bearer '+self.key).encode())],'app':gateway.app,'asgi':{'spec_version':'2.4'}}
            async def receive():return {'type':'http.request','body':body,'more_body':False}
            request=Request(scope,receive)
            response=await gateway.inference('chat/completions',request)
            self.assertEqual(gateway.app.state.admitted,1)
            async def send(message):raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):await response(scope,receive,send)
        asyncio.run(exercise())
        self.assertEqual(gateway.app.state.admitted,0)
        with core.connect() as c:row=c.execute('SELECT outcome FROM requests ORDER BY created DESC LIMIT 1').fetchone()
        self.assertEqual(row['outcome'],'cancelled')

if __name__=='__main__':unittest.main(verbosity=2)
