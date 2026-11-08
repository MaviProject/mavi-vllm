import json,os,sqlite3
from pathlib import Path
root=Path(os.environ.get('MAVI_PLATFORM_ROOT',Path(__file__).resolve().parents[1]))
data=root/'data'
report=json.loads((data/'production-qa/stability-results.json').read_text())
profiles={str(p['concurrency']):dict(kv_tokens=p['kv_tokens'],concurrency=p['concurrency'],measured_at=p['started'],source='本机图文 + MTP 验收实测') for p in report['profiles']}
# Initial 2 x 12K cold boot, recorded in the service journal at 23:33:30.
profiles['2']=dict(kv_tokens=36864,concurrency=2,measured_at=1793979210,source='本机图文 + MTP 启动日志实测')
with sqlite3.connect(data/'platform.sqlite3',timeout=20) as db:
    db.execute("INSERT OR REPLACE INTO meta VALUES ('capacity_profiles',?)",(json.dumps(profiles),))
print('Stored measured capacities:',{n:p['kv_tokens'] for n,p in profiles.items()})
