from __future__ import annotations
import asyncio
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from contextlib import asynccontextmanager

import httpx
import anyio
import psutil
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
import core
from streaming import ManagedStreamingResponse

CACHE = {'state': 'loading', 'hardware': {}, 'metrics': {}, 'updated': 0}
CONTROL_LOCK = asyncio.Lock()
COOKIE = 'mavi_session'


async def command(*args, timeout=12):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill(); await p.communicate()
        raise RuntimeError('系统操作超时，请查看运行日志。')
    if p.returncode:
        raise RuntimeError(err.decode(errors='replace').strip()[:300] or '系统操作失败。')
    return out.decode(errors='replace')


async def control(action):
    if action not in ('start', 'stop', 'restart', 'enable', 'disable'):
        raise ValueError('无效操作')
    async with CONTROL_LOCK:
        if action in ('start','restart'):
            await command('/usr/bin/sudo','-n','/usr/bin/systemctl','reset-failed',core.ENGINE_UNIT,'--no-block')
        await command('/usr/bin/sudo', '-n', '/usr/bin/systemctl', action, core.ENGINE_UNIT, '--no-block')
        core.audit('inference.'+action)


def metric_values(text):
    values = {}; labels = {}
    for line in text.splitlines():
        m = re.match(r'^([^#\s{]+)(?:\{(.*?)\})?\s+([-+0-9.eE]+)', line)
        if not m:
            continue
        name, label, value = m.groups()
        try:
            values[name] = values.get(name, 0) + float(value)
        except ValueError:
            continue
        if name == 'vllm:cache_config_info':
            labels = dict(re.findall(r'(\w+)="([^"]*)"', label or ''))
    return values, labels


async def monitor():
    client = httpx.AsyncClient(timeout=8, trust_env=False,limits=httpx.Limits(max_keepalive_connections=0))
    previous = None; previous_time = time.time()
    try:
        while True:
            tick = time.time()
            snapshot = dict(created=tick, gpu_used=0, gpu_total=0, gpu_util=0, gpu_temp=0, gpu_power=0,
                            cpu=psutil.cpu_percent(), ram_used=psutil.virtual_memory().used/2**30,
                            ram_total=psutil.virtual_memory().total/2**30, disk_free=psutil.disk_usage('/').free/2**30,
                            output_tps=0, input_tps=0, running=0, waiting=0, kv_usage=0)
            try:
                gpu = await command('nvidia-smi', '--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw', '--format=csv,noheader,nounits', timeout=8)
                row = next(csv.reader(io.StringIO(gpu)))
                snapshot.update(gpu_name=row[0].strip(), driver=row[1].strip(), gpu_used=float(row[2]), gpu_total=float(row[3]), gpu_util=float(row[4]), gpu_temp=float(row[5]), gpu_power=float(row[6]))
            except Exception:
                snapshot['gpu_name'] = 'GPU 状态暂不可用'
            try:
                service = await command('/usr/bin/systemctl', 'show', core.ENGINE_UNIT, '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts', '-p', 'ActiveEnterTimestampMonotonic')
                service = dict(line.split('=', 1) for line in service.splitlines() if '=' in line)
            except Exception:
                service = {'ActiveState': 'unknown'}
            healthy = False; vals = {}; cache_labels = {}; metrics_fresh=False
            health_started=time.monotonic()
            healthy,health_status,health_error=await probe_health(client)
            snapshot.update(health_ms=round((time.monotonic()-health_started)*1000,2),health_ok=healthy,health_status=health_status,health_error=health_error)
            if healthy:
                try:
                    text = (await client.get(core.BACKEND_URL+'/metrics',timeout=2)).text
                    vals, cache_labels = metric_values(text)
                    metrics_fresh=True
                except httpx.HTTPError:
                    vals=CACHE.get('metrics',{})
            active = service.get('ActiveState')
            state = 'running' if healthy else ('stopping' if active == 'deactivating' else 'loading' if active in ('active','activating') else 'error' if active == 'failed' else 'stopped')
            if healthy:
                snapshot.update(running=vals.get('vllm:num_requests_running', 0), waiting=vals.get('vllm:num_requests_waiting', 0), kv_usage=vals.get('vllm:kv_cache_usage_perc', 0))
                if previous and metrics_fresh:
                    dt = max(1, tick-previous_time)
                    for name, field in [('vllm:generation_tokens_total','output_tps'),('vllm:prompt_tokens_total','input_tps')]:
                        current, old = vals.get(name, 0), previous.get(name, 0)
                        snapshot[field] = max(0, current-old if current>=old else current)/dt
                if metrics_fresh:previous, previous_time = vals, tick
                if cache_labels.get('kv_cache_size_tokens'):
                    applied=core.get_meta('applied_config',core.config())
                    calibration=dict(kv_tokens=int(cache_labels['kv_cache_size_tokens']),concurrency=applied.get('concurrency',1),context_length=applied.get('context_length',core.config()['context_length']),measured_at=tick,source='当前配置启动后实测 · 视觉 + MTP')
                    core.record_calibration(calibration)
            else:
                previous = None; previous_time = tick
            draft = vals.get('vllm:spec_decode_num_draft_tokens_total', 0)
            snapshot.update(mtp_acceptance=vals.get('vllm:spec_decode_num_accepted_tokens_total', 0)/draft if draft else None)
            CACHE.update(state=state, hardware=snapshot, metrics=vals, service=service, updated=tick,
                         kv_tokens=int(cache_labels.get('kv_cache_size_tokens',CACHE.get('kv_tokens',0) if healthy else 0)), mtp_acceptance=snapshot['mtp_acceptance'])
            with core.connect() as c:
                c.execute('INSERT OR REPLACE INTO samples VALUES (?,?)', (tick,json.dumps(snapshot)))
                c.execute('DELETE FROM samples WHERE created<?', (tick-7*86400,))
                c.execute('DELETE FROM sessions WHERE expires<?', (tick,))
                c.execute('DELETE FROM login_attempts WHERE created<?', (tick-900,))
            await asyncio.sleep(max(.5, 5-(time.time()-tick)))
    finally:
        await client.aclose()


async def probe_health(client):
    for attempt in range(2):
        try:
            response=await client.get(core.BACKEND_URL+'/health')
            return response.status_code==200,response.status_code,None
        except httpx.HTTPError as exc:
            if attempt:return False,None,type(exc).__name__+': '+str(exc)[:160]
            await asyncio.sleep(.05)


@asynccontextmanager
async def lifespan(app):
    core.initialize()
    app.state.client = httpx.AsyncClient(timeout=600, trust_env=False,limits=httpx.Limits(max_keepalive_connections=0))
    task = asyncio.create_task(monitor())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await app.state.client.aclose()


app = FastAPI(title='mavi_vllm管理平台', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"})
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response


def session(request: Request):
    token = request.cookies.get(COOKIE,'')
    if not token:
        raise HTTPException(401,'请先登录。')
    with core.connect() as c:
        row = c.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row:
        raise HTTPException(401,'登录已过期，请重新登录。')
    if request.method not in ('GET','HEAD') and not hmac.compare_digest(request.headers.get('x-csrf-token',''),row['csrf']):
        raise HTTPException(403,'会话验证失败，请刷新页面。')
    return dict(row)


def new_session():
    token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
    with core.connect() as c:
        c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+43200))
    response=JSONResponse({'username':core.username(),'csrf':csrf})
    response.set_cookie(COOKIE,token,max_age=43200,httponly=True,samesite='strict',path='/')
    return response


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


@app.get('/api/health')
async def health():
    return {'status':'ok','product':'mavi_vllm管理平台'}


@app.post('/api/login')
async def login(data: Login, request: Request):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'不允许的请求来源。')
    ip=request.client.host
    with core.connect() as c:
        attempts=c.execute('SELECT COUNT(*) FROM login_attempts WHERE ip=? AND created>?',(ip,time.time()-300)).fetchone()[0]
        if attempts>=10:
            raise HTTPException(429,'登录尝试过于频繁，请 5 分钟后重试。')
        row=c.execute('SELECT password FROM users WHERE username=?',(data.username,)).fetchone()
        valid=core.check_password(data.password,row['password']) if row else False
        if not valid:
            c.execute('INSERT INTO login_attempts VALUES (?,?)',(ip,time.time()))
        else:
            c.execute('DELETE FROM login_attempts WHERE ip=?',(ip,))
    if not valid:
        raise HTTPException(401,'用户名或密码不正确。')
    core.audit('account.login',ip)
    return new_session()


@app.get('/api/session')
async def me(s=Depends(session)):
    return {'username':core.username(),'csrf':s['csrf']}


@app.post('/api/logout')
async def logout(s=Depends(session)):
    with core.connect() as c:c.execute('DELETE FROM sessions WHERE token=?',(s['token'],))
    r=JSONResponse({'ok':True});r.delete_cookie(COOKIE);return r


class PasswordChange(BaseModel):
    current_password:str=Field(max_length=128)
    new_password:str=Field(min_length=10,max_length=128)


@app.post('/api/password')
async def password(data:PasswordChange,s=Depends(session)):
    with core.connect() as c:
        row=c.execute('SELECT password FROM users WHERE username=?',(core.username(),)).fetchone()
        if not core.check_password(data.current_password,row['password']):raise HTTPException(400,'当前密码不正确。')
        c.execute('UPDATE users SET password=? WHERE username=?',(core.password_hash(data.new_password),core.username()))
        c.execute('DELETE FROM sessions')
    core.audit('account.password_changed')
    return new_session()


@app.get('/api/overview')
async def overview(s=Depends(session)):
    return dict(runtime=CACHE,config=core.config(),applied=core.get_meta('applied_config',{}),**core.usage_stats())


@app.get('/api/recommendations')
async def recommend(s=Depends(session)):
    return core.recommendations()


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    served_model_name:str=Field(min_length=1,max_length=128,pattern=r'^[a-zA-Z0-9._/-]+$')
    concurrency:int=Field(ge=1,le=8,strict=True)
    context_length:int=Field(ge=1024,le=32768,strict=True)
    thinking_enabled:bool
    autostart_inference:bool
    apply_restart:bool=True


@app.post('/api/settings')
async def settings(data:Settings,s=Depends(session)):
    profile=core.capacity_profile(data.concurrency)
    if data.context_length>profile['max_context']:
        raise HTTPException(400,f"{data.concurrency} 并发下容量上限为 {profile['max_context']} token，请降低上下文或并发。")
    old=core.config();new=data.model_dump(exclude={'apply_restart'})
    applied=core.get_meta('applied_config',old)
    restart=any(applied.get(k,old[k])!=new[k] for k in ('concurrency','context_length'))
    try:
        if old['autostart_inference']!=new['autostart_inference']:
            await control('enable' if new['autostart_inference'] else 'disable')
        core.set_meta('config',new)
        if restart and data.apply_restart and CACHE['state'] in ('running','loading'):
            await control('restart')
            CACHE['state']='loading'
    except Exception as exc:
        core.set_meta('config',old)
        raise HTTPException(500,str(exc))
    core.audit('settings.updated',json.dumps(new,ensure_ascii=False))
    return {'ok':True,'restart':restart,'config':new}


@app.post('/api/control/{action}')
async def engine_control(action:str,s=Depends(session)):
    if action not in ('start','stop','restart'):raise HTTPException(400,'未知操作。')
    try:await control(action)
    except Exception as exc:raise HTTPException(500,str(exc))
    CACHE['state']='stopping' if action=='stop' else 'loading'
    return {'ok':True,'state':CACHE['state']}


@app.get('/api/keys')
async def keys(s=Depends(session)):
    return {'keys':core.usage_stats()['keys']}


class KeyCreate(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    value:str=Field(default='',max_length=512)


@app.post('/api/keys')
async def add_key(data:KeyCreate,s=Depends(session)):
    value=data.value or 'sk-mavi-'+secrets.token_hex(24)
    if len(value)<12 or re.search(r'\s',value):raise HTTPException(400,'Key 至少 12 个字符且不能包含空格。')
    try:
        with core.connect() as c:kid=core.insert_key(c,data.name.strip(),value)
    except Exception:raise HTTPException(400,'此 API Key 已存在。')
    core.audit('key.created',data.name)
    return {'id':kid,'value':value}


@app.get('/api/keys/{kid}/reveal')
async def reveal(kid:str,s=Depends(session)):
    value=core.reveal_key(kid)
    if not value:raise HTTPException(404,'Key 不存在。')
    core.audit('key.revealed',kid)
    return {'value':value}


class KeyState(BaseModel):
    enabled:bool


@app.patch('/api/keys/{kid}')
async def key_state(kid:str,data:KeyState,s=Depends(session)):
    with core.connect() as c:c.execute('UPDATE api_keys SET enabled=? WHERE id=?',(int(data.enabled),kid))
    core.audit('key.enabled' if data.enabled else 'key.disabled',kid)
    return {'ok':True}


@app.delete('/api/keys/{kid}')
async def delete_key(kid:str,s=Depends(session)):
    with core.connect() as c:c.execute('DELETE FROM api_keys WHERE id=?',(kid,))
    core.audit('key.deleted',kid)
    return {'ok':True}


@app.get('/api/logs')
async def logs(s=Depends(session)):
    try:out=await command('/usr/bin/journalctl','-u',core.ENGINE_UNIT,'-n','120','--no-pager','-o','short-iso')
    except Exception as exc:out=str(exc)
    for old, current in (
        ('Qwen3.8-27B-UD-Q4_K_M.gguf', core.MODEL_WEIGHT),
        ('mtp-Qwen3.8-27B-Q4_0.gguf', core.MTP_WEIGHT),
        ('mmproj-BF16.gguf', core.VISION_WEIGHT),
        (core.LEGACY_MODEL_NAME, core.INTERNAL_MODEL),
    ):
        out = out.replace(old, current)
    return {'text':out,'audit':core.usage_stats()['audit']}


@app.get('/api/export')
async def export(s=Depends(session)):
    with core.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 10000')]
    stream=io.StringIO()
    fields=['id','created','key_name','route','model','status','outcome','latency_ms','ttft_ms','input_tokens','output_tokens','stream']
    writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
    for row in rows:
        row['created']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(row['created']));writer.writerow(row)
    return Response('\ufeff'+stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=mavi-requests.csv'})


@app.post('/api/playground')
async def playground(request:Request,s=Depends(session)):
    key=core.first_key()
    if not key:raise HTTPException(400,'请先启用一个 API Key。')
    try:body=await core.read_body(request)
    except core.BodyLimitError:raise HTTPException(413,'图片请求过大。')
    try:r=await request.app.state.client.send(request.app.state.client.build_request('POST','http://127.0.0.1/v1/chat/completions',content=body,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}),stream=True)
    except httpx.HTTPError:raise HTTPException(503,'API 网关尚未就绪。')
    closed=False
    async def close():
        nonlocal closed
        if closed:return
        closed=True
        with anyio.CancelScope(shield=True):await r.aclose()
    async def chunks():
        try:
            async for chunk in r.aiter_bytes():yield chunk
        finally:await close()
    return ManagedStreamingResponse(chunks(),on_finish=close,status_code=r.status_code,media_type=r.headers.get('content-type','application/json'))


app.mount('/static',StaticFiles(directory=core.ROOT/'static'),name='static')


@app.get('/')
async def index():
    return FileResponse(core.ROOT/'static/index.html',headers={'Cache-Control':'no-cache'})
