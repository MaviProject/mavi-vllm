from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(os.environ.get('MAVI_PLATFORM_ROOT', Path(__file__).resolve().parents[1]))
DATA = ROOT / 'data'
DB = DATA / 'platform.sqlite3'
ENGINE_UNIT = 'qwen3.8-vllm.service'
INTERNAL_MODEL = 'liuxing'
LEGACY_MODEL_NAME = 'qwen3.8-27b'
MODEL_DIR = Path('/home/mavi/inference/models') / INTERNAL_MODEL
MODEL_WEIGHT = 'liuxing-UD-Q4_K_M.gguf'
VISION_WEIGHT = 'mmproj-liuxing-BF16.gguf'
MTP_WEIGHT = 'mtp-liuxing-Q4_0.gguf'
BACKEND_URL = 'http://127.0.0.1:8000'
DEFAULTS = dict(served_model_name=INTERNAL_MODEL, concurrency=2, context_length=12288,
                thinking_enabled=False, autostart_inference=True)
# This deployment's RTX 4090 / vision / MTP acceptance envelope.
CONTEXT_LIMITS = {1:24576, 2:12288, 4:6144, 8:1024}


class BodyLimitError(Exception):
    pass


async def read_body(request, limit=25*1024*1024):
    data = bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>limit:
            raise BodyLimitError()
        data.extend(chunk)
    return bytes(data)


@contextmanager
def connect():
    c = sqlite3.connect(DB, timeout=20)
    try:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=20000')
        with c:
            yield c
    finally:
        c.close()


def cipher():
    return Fernet((DATA / 'secret.key').read_bytes())


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def check_password(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)


def initialize():
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    (DATA / 'secret.key').touch(mode=0o600, exist_ok=True)
    if not (DATA / 'secret.key').stat().st_size:
        (DATA / 'secret.key').write_bytes(Fernet.generate_key())
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY,password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,
          encrypted TEXT NOT NULL,tail TEXT NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,last_used REAL);
        CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY,created REAL NOT NULL,key_id TEXT,key_name TEXT,
          route TEXT,model TEXT,status INTEGER,outcome TEXT,latency_ms REAL,ttft_ms REAL,input_tokens INTEGER,
          output_tokens INTEGER,stream INTEGER);
        CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
        CREATE INDEX IF NOT EXISTS requests_key ON requests(key_id);
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY,created REAL,action TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS samples (created REAL PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT,created REAL);
        ''')
        if not c.execute("SELECT 1 FROM meta WHERE key='config'").fetchone():
            c.execute('INSERT INTO meta VALUES (?,?)', ('config', json.dumps(DEFAULTS)))
        if not c.execute('SELECT 1 FROM users').fetchone():
            password = 'admin'
            c.execute('INSERT INTO users VALUES (?,?)', ('admin', password_hash(password)))
            credentials = dict(url='http://127.0.0.1:20204/',remote_url_template='http://<server-ip>:20204/',username='admin',password=password)
            (DATA / 'bootstrap-credentials.json').write_text(json.dumps(credentials, indent=2))
        if not c.execute("SELECT 1 FROM meta WHERE key='internal_key'").fetchone():
            c.execute('INSERT INTO meta VALUES (?,?)', ('internal_key', cipher().encrypt(('sk-internal-'+secrets.token_hex(24)).encode()).decode()))
        if not c.execute("SELECT 1 FROM meta WHERE key='keys_initialized'").fetchone():
            old = Path(os.environ.get('MAVI_EXISTING_API_ENV', '/home/mavi/inference/config/api.env'))
            existing = dict(line.split('=', 1) for line in old.read_text().splitlines() if '=' in line).get('VLLM_API_KEY') if old.exists() else None
            value = existing or 'sk-mavi-' + secrets.token_hex(24)
            insert_key(c, '原有 API Key', value)
            c.execute('INSERT INTO meta VALUES (?,?)', ('keys_initialized', 'true'))
        if not c.execute("SELECT 1 FROM meta WHERE key='calibration'").fetchone():
            c.execute('INSERT INTO meta VALUES (?,?)', ('calibration', json.dumps(dict(kv_tokens=35659, concurrency=1, source='该主机视觉 + MTP 启动实测', measured_at=time.time()))))
    DB.chmod(0o600)


def get_meta(key, default=None):
    with connect() as c:
        row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def set_meta(key, value):
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def config():
    return get_meta('config', DEFAULTS.copy())


def username():
    with connect() as c:
        return c.execute('SELECT username FROM users LIMIT 1').fetchone()[0]


def internal_key():
    with connect() as c:
        value = c.execute("SELECT value FROM meta WHERE key='internal_key'").fetchone()[0]
    return cipher().decrypt(value.encode()).decode()


def insert_key(c, name, value):
    kid = secrets.token_hex(8)
    c.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,NULL)',
              (kid, name, hashlib.sha256(value.encode()).hexdigest(), cipher().encrypt(value.encode()).decode(), value[-4:], 1, time.time()))
    return kid


def lookup_key(value):
    if not value or len(value) > 512:
        return None
    with connect() as c:
        row = c.execute('SELECT id,name FROM api_keys WHERE digest=? AND enabled=1', (hashlib.sha256(value.encode()).hexdigest(),)).fetchone()
    return dict(row) if row else None


def reveal_key(kid):
    with connect() as c:
        row = c.execute('SELECT encrypted FROM api_keys WHERE id=?', (kid,)).fetchone()
    return cipher().decrypt(row['encrypted'].encode()).decode() if row else None


def first_key():
    with connect() as c:
        row = c.execute('SELECT id FROM api_keys WHERE enabled=1 ORDER BY created LIMIT 1').fetchone()
    return reveal_key(row['id']) if row else None


def audit(action, detail=''):
    with connect() as c:
        c.execute('INSERT INTO audit(created,action,detail) VALUES (?,?,?)', (time.time(), action, detail))


def record_request(data):
    with connect() as c:
        c.execute('INSERT INTO requests VALUES (:id,:created,:key_id,:key_name,:route,:model,:status,:outcome,:latency_ms,:ttft_ms,:input_tokens,:output_tokens,:stream)', data)
        c.execute('UPDATE api_keys SET last_used=? WHERE id=?', (time.time(), data['key_id']))


def context_envelope(n):
    if type(n) is not int or not 1<=n<=8:
        raise ValueError('本机支持 1–8 的整数并发数。')
    lower=max(anchor for anchor in CONTEXT_LIMITS if anchor<=n)
    upper=min(anchor for anchor in CONTEXT_LIMITS if anchor>=n)
    fraction=(n-lower)/(upper-lower) if upper!=lower else 0
    return int((CONTEXT_LIMITS[lower]*lower*(1-fraction)+CONTEXT_LIMITS[upper]*upper*fraction)/n)


def record_calibration(calibration):
    set_meta('calibration',calibration)
    # Hybrid attention reports different KV token counts at different context
    # sizes. A small-context measurement must not replace a larger reference.
    if calibration['context_length']>=context_envelope(calibration['concurrency']):
        profiles=get_meta('capacity_profiles',{})
        profiles[str(calibration['concurrency'])]=calibration
        set_meta('capacity_profiles',profiles)


def capacity_profile(n, calibration=None, measured=None):
    envelope=context_envelope(n)
    calibration=calibration if calibration is not None else get_meta('calibration',dict(kv_tokens=35659,concurrency=1))
    measured=measured if measured is not None else get_meta('capacity_profiles',{})
    baseline=max(8192,calibration['kv_tokens']+(calibration.get('concurrency',1)-1)*1536)
    lower=max(anchor for anchor in CONTEXT_LIMITS if anchor<=n)
    upper=min(anchor for anchor in CONTEXT_LIMITS if anchor>=n)
    fraction=(n-lower)/(upper-lower) if upper!=lower else 0
    # Interpolate aggregate context budget, rather than extrapolating beyond
    # the measured 1/2/4/8 GPU state and KV envelopes.
    low_capacity=measured.get(str(lower),{}).get('kv_tokens',max(4096,baseline-(lower-1)*1536))
    high_capacity=measured.get(str(upper),{}).get('kv_tokens',max(4096,baseline-(upper-1)*1536))
    capacity=measured.get(str(n),{}).get('kv_tokens',low_capacity*(1-fraction)+high_capacity*fraction)
    limit=max(1024,min(envelope,int(capacity*.85/n)))
    recommended=max(1024,min(limit,int(capacity*.75/n/1024)*1024))
    return dict(concurrency=n,context_length=recommended,max_context=limit,estimated=str(n) not in measured)


def image_max_pixels(context_length):
    # Reserve space for text/history and output even at non-K context sizes.
    return min(1048576,max(65536,(context_length//4)*1024))


def recommendations():
    calibration=get_meta('calibration',dict(kv_tokens=35659,concurrency=1))
    measured=get_meta('capacity_profiles',{})
    custom_profiles=[capacity_profile(n,calibration,measured) for n in range(1,9)]
    return dict(profiles=[p for p in custom_profiles if p['concurrency'] in CONTEXT_LIMITS],custom_profiles=custom_profiles,
                limits=dict(min_concurrency=1,max_concurrency=8,min_context=1024,max_context=32768),calibration=calibration,
                explanation='快捷档位参考本机压测，自定义并发参考实测或相邻档位估算；上下文支持整数 token，超出容量上限会被拒绝。')


def usage_stats():
    with connect() as c:
        totals = dict(c.execute('''SELECT COUNT(*) requests,COALESCE(SUM(outcome='success'),0) success,
          COALESCE(SUM(outcome='error'),0) errors,COALESCE(SUM(outcome='cancelled'),0) cancelled,
          COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,
          COALESCE(AVG(latency_ms),0) latency_ms,COALESCE(AVG(ttft_ms),0) ttft_ms FROM requests''').fetchone())
        totals['today'] = c.execute('SELECT COUNT(*) FROM requests WHERE created>?', (time.time()-86400,)).fetchone()[0]
        latencies = sorted(r[0] for r in c.execute('SELECT latency_ms FROM requests ORDER BY created DESC LIMIT 500'))
        totals['p95_ms'] = latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else 0
        recent = [dict(r) for r in c.execute('SELECT * FROM requests ORDER BY created DESC LIMIT 20')]
        keys = [dict(r) for r in c.execute('''SELECT k.id,k.name,k.tail,k.enabled,k.created,k.last_used,COUNT(r.id) requests,
          COALESCE(SUM(r.output_tokens),0) output_tokens FROM api_keys k LEFT JOIN requests r ON k.id=r.key_id GROUP BY k.id ORDER BY k.created''')]
        history = [json.loads(r[0]) for r in c.execute('SELECT payload FROM samples WHERE created>? ORDER BY created', (time.time()-3600,))]
        audit_rows = [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30')]
    if history:
        last=history[-1]
        history=history[::max(1,(len(history)+359)//360)]
        if history[-1]['created']!=last['created']:
            history.append(last)
    return dict(totals=totals, recent=recent, keys=keys, history=history, audit=audit_rows)
