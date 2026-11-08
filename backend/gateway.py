from __future__ import annotations
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import anyio
from starlette.requests import ClientDisconnect
from streaming import ManagedStreamingResponse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import core


@asynccontextmanager
async def lifespan(app):
    core.initialize()
    app.state.admitted = 0
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5), trust_env=False,limits=httpx.Limits(max_keepalive_connections=0,max_connections=32))
    yield
    await app.state.client.aclose()


app = FastAPI(title='mavi OpenAI API', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def error(message, status=400, kind='invalid_request_error'):
    return JSONResponse({'error': {'message': message, 'type': kind, 'code': status}}, status_code=status)


def authenticated(request):
    token = request.headers.get('authorization', '')
    if token.lower().startswith('bearer '):
        token = token[7:]
    else:
        token = request.headers.get('x-api-key', '')
    return core.lookup_key(token)


def enforce_policy(body, cfg, route):
    body = dict(body)
    if 'stream' in body and not isinstance(body['stream'], bool):
        raise ValueError('stream 必须是布尔值。')
    for field in ('stream_options', 'chat_template_kwargs'):
        if body.get(field) is not None and not isinstance(body[field], dict):
            raise ValueError(field+' 必须是 JSON 对象。')
    if 'include_usage' in (body.get('stream_options') or {}) and not isinstance(body['stream_options']['include_usage'], bool):
        raise ValueError('include_usage 必须是布尔值。')
    for field in ('n', 'best_of'):
        if field in body and (isinstance(body[field], bool) or not isinstance(body[field], int) or body[field] != 1):
            raise ValueError('单卡稳定配置仅支持 '+field+'=1；请使用独立并发请求。')
    if body.get('use_beam_search') not in (None,False) or ('beam_width' in body and (type(body['beam_width']) is not int or body['beam_width']!=1)):
        raise ValueError('单卡稳定配置不支持 beam search。')
    if route=='completions' and isinstance(body.get('prompt'),list) and any(not isinstance(item,int) or isinstance(item,bool) for item in body['prompt']):
        raise ValueError('批量 prompt 请拆为独立请求，以便执行并发与队列限制。')
    applied=core.get_meta('applied_config',cfg)
    output_limit=min(4096,applied['context_length'])
    for field in ('max_tokens','max_completion_tokens','max_output_tokens'):
        if body.get(field) is not None and (isinstance(body[field], bool) or not isinstance(body[field], int) or not 1<=body[field]<=output_limit):
            raise ValueError(f'{field} 必须为 1–{output_limit} 的整数。')
    for key in ('chat_template', 'continue_final_message', 'add_generation_prompt'):
        if key in body:
            raise ValueError('此网关不接受请求自定义模板或续写控制参数。')
    if not cfg['thinking_enabled']:
        body.pop('thinking', None)
        body.pop('enable_thinking', None)
        kwargs = dict(body.get('chat_template_kwargs') or {})
        kwargs.pop('reasoning_effort', None)
        kwargs['enable_thinking'] = False
        body['chat_template_kwargs'] = kwargs
        if route == 'chat/completions':
            body['reasoning_effort'] = 'none'
        elif route == 'responses':
            body['reasoning'] = {'effort': 'none'}
        elif route == 'completions':
            # Raw prompts bypass the model's chat template and cannot enforce this policy.
            raise ValueError('强制关闭思考时请使用 /v1/chat/completions 或 /v1/responses；原始补全无法保证此策略。')
    body['model'] = core.INTERNAL_MODEL
    return body


def extract_usage(obj):
    u = obj.get('usage') or (obj.get('response') or {}).get('usage') or {}
    return int(u.get('prompt_tokens', u.get('input_tokens', 0)) or 0), int(u.get('completion_tokens', u.get('output_tokens', 0)) or 0)


def rename_model(obj, alias):
    if isinstance(obj, dict):
        if 'model' in obj:
            obj['model'] = alias
        if isinstance(obj.get('response'), dict):
            rename_model(obj['response'], alias)
    return obj


@app.get('/health')
async def health():
    return {'gateway': 'online'}


@app.get('/v1/models')
async def models(request: Request):
    if not authenticated(request):
        return error('API Key 无效或已停用。', 401, 'authentication_error')
    cfg=core.config()
    applied=core.get_meta('applied_config',cfg)
    return {'object': 'list', 'data': [{'id': cfg['served_model_name'], 'object': 'model', 'created': 1793923200, 'owned_by': 'mavi', 'max_model_len': applied['context_length']}]}


@app.post('/v1/{route:path}')
async def inference(route: str, request: Request):
    key = authenticated(request)
    if not key:
        return error('API Key 无效或已停用。', 401, 'authentication_error')
    if route not in ('chat/completions', 'completions', 'responses'):
        return error('不支持的 OpenAI API 路径。', 404)
    cfg=core.config()
    applied=core.get_meta('applied_config',cfg)
    limit=min(16,applied['concurrency']+6)
    if request.app.state.admitted>=limit:
        now=time.time()
        core.record_request(dict(id=uuid.uuid4().hex,created=now,key_id=key['id'],key_name=key['name'],route='/v1/'+route,model=cfg['served_model_name'],status=429,outcome='error',latency_ms=0,ttft_ms=None,input_tokens=0,output_tokens=0,stream=0))
        response=error('推理队列已满，请稍后重试。',429,'rate_limit_error')
        response.headers['Retry-After']='3'
        return response
    request.app.state.admitted+=1
    released=False
    def release():
        nonlocal released
        if not released:
            request.app.state.admitted=max(0,request.app.state.admitted-1)
            released=True
    streaming=False
    try:
        response=await forward(route,request,key,release)
        streaming=isinstance(response,StreamingResponse)
        return response
    finally:
        if not streaming:release()


def reject_constant(value):
    raise ValueError('JSON 不允许 '+value)


async def forward(route,request,key,release):
    cfg = core.config()
    alias = cfg['served_model_name']
    started = time.monotonic()
    rec = dict(id=uuid.uuid4().hex, created=time.time(), key_id=key['id'], key_name=key['name'], route='/v1/'+route,
               model=alias, status=500, outcome='error', latency_ms=0, ttft_ms=None, input_tokens=0, output_tokens=0, stream=0)
    def record():
        rec['latency_ms'] = round((time.monotonic()-started)*1000, 2)
        core.record_request(rec)
    def reject(message,status=400):
        rec['status']=status;record()
        return error(message,status)
    try:
        length = int(request.headers.get('content-length', 0) or 0)
    except ValueError:
        return reject('无效 Content-Length。')
    if length > 25*1024*1024:
        return reject('请求体超过 25 MB。', 413)
    try:
        raw = await asyncio.wait_for(core.read_body(request),30)
        body = json.loads(raw,parse_constant=reject_constant)
        if not isinstance(body, dict):
            raise ValueError('请求必须是 JSON 对象。')
    except core.BodyLimitError:
        return reject('请求体超过 25 MB。', 413)
    except asyncio.TimeoutError:
        return reject('请求体读取超时。',408)
    except ClientDisconnect:
        rec['outcome']='cancelled'
        return reject('客户端已断开。',499)
    except (ValueError, TypeError,RecursionError):
        return reject('请求必须是有效的 JSON 对象。')
    rec['stream']=int(bool(body.get('stream')))
    if body.get('model') != alias:
        rec['status'] = 404; record()
        return error('未知模型名称。请使用 '+alias, 404)
    try:
        body = enforce_policy(body, cfg, route)
    except (ValueError, TypeError) as exc:
        rec['status'] = 400; record()
        return error(str(exc))
    original_usage = bool((body.get('stream_options') or {}).get('include_usage'))
    if body.get('stream') and route != 'responses':
        body['stream_options'] = dict(body.get('stream_options') or {}, include_usage=True)
    headers = {'Authorization': 'Bearer '+core.internal_key(), 'Content-Type': 'application/json'}
    try:
        upstream = await request.app.state.client.send(request.app.state.client.build_request('POST', core.BACKEND_URL+'/v1/'+route, json=body, headers=headers), stream=True)
    except httpx.HTTPError:
        rec['status'] = 503; record()
        return error('推理服务未就绪或已停止，请在管理平台启动模型。', 503, 'service_unavailable')
    rec['status'] = upstream.status_code
    if upstream.status_code >= 400 or not body.get('stream'):
        try:
            content = await upstream.aread()
            obj = json.loads(content)
            rename_model(obj, alias)
            rec['input_tokens'], rec['output_tokens'] = extract_usage(obj)
            rec['outcome'] = 'success' if upstream.status_code < 400 else 'error'
            return JSONResponse(obj, status_code=upstream.status_code)
        except (ValueError, httpx.HTTPError):
            rec['status'] = 502
            return error('上游返回无效响应。', 502, 'upstream_error')
        finally:
            record()
            release()
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

    finalized=False
    async def finalize():
        nonlocal finalized
        if finalized:return
        finalized=True
        release()
        try:
            if rec['outcome']=='pending':rec['outcome']='cancelled'
            record()
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

    async def stream():
        buffer = ''
        completed = False
        try:
            async for chunk in upstream.aiter_text():
                buffer += chunk.replace('\r\n', '\n')
                while '\n\n' in buffer:
                    event, buffer = buffer.split('\n\n', 1)
                    data = next((line[5:].strip() for line in event.splitlines() if line.startswith('data:')), None)
                    if not data:
                        yield (event+'\n\n').encode(); continue
                    if data == '[DONE]':
                        completed = True
                        yield b'data: [DONE]\n\n'; continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        yield (event+'\n\n').encode(); continue
                    kind = obj.get('type', '')
                    if 'error' in obj or kind in ('error', 'response.failed', 'response.incomplete'):
                        rec['outcome'] = 'sse_error'
                    if kind == 'response.completed':
                        completed = True
                    inp, out = extract_usage(obj)
                    if inp or out:
                        rec['input_tokens'], rec['output_tokens'] = inp, out
                    substantive = any((c.get('delta') or {}).get(field) for c in obj.get('choices', []) for field in ('content', 'reasoning', 'reasoning_content', 'tool_calls')) or (kind.endswith('.delta') and bool(obj.get('delta')))
                    if substantive and rec['ttft_ms'] is None:
                        rec['ttft_ms'] = round((time.monotonic()-started)*1000, 2)
                    if not original_usage and route != 'responses' and obj.get('choices') == [] and obj.get('usage'):
                        continue
                    rename_model(obj, alias)
                    prefix = ''.join(line+'\n' for line in event.splitlines() if not line.startswith('data:'))
                    yield (prefix+'data: '+json.dumps(obj, ensure_ascii=False)+'\n\n').encode()
            if completed and rec['outcome'] != 'cancelled':
                rec['outcome'] = 'success' if rec['status'] == 200 and rec['outcome'] != 'sse_error' else 'error'
            elif rec['outcome'] != 'cancelled':
                rec['outcome'] = 'error'
        except asyncio.CancelledError:
            rec['outcome'] = 'cancelled'
            raise
        except httpx.HTTPError:
            rec['outcome'] = 'error'
            yield b'data: {"error":{"message":"Upstream stream interrupted"}}\n\n'
        finally:
            await finalize()
    rec['outcome'] = 'pending'
    return ManagedStreamingResponse(stream(),on_finish=finalize,media_type='text/event-stream',headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'X-Request-Id': rec['id']})
