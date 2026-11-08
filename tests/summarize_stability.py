import datetime,json,re,sqlite3,statistics,subprocess
from contextlib import closing
import httpx
from pathlib import Path

root=Path('/home/mavi/mavi-vllm-platform/data/production-qa')
report=json.loads((root/'stability-results.json').read_text())
assert report['status']=='passed'
finished=datetime.datetime.fromisoformat(report['finished']).timestamp()
soak=report['soak'];since=finished-soak['elapsed_seconds']
rows=soak['request_details'];latency=sorted(r['seconds'] for r in rows)
summary={'status':report['status'],'started':report['started'],'finished':report['finished'],
 'profiles':[{'concurrency':p['concurrency'],'context':p['context'],'kv_tokens':p['kv_tokens'],'restarts':p['restarts'],'near_context_requests':len(p['near_context']['requests']),'vision_requests':len(p['vision']['requests'])} for p in report['profiles']],
 'soak':{**{k:v for k,v in soak.items() if k!='request_details'},'median_latency_seconds':statistics.median(latency),'p95_latency_seconds':latency[min(len(latency)-1,int(len(latency)*.95))],'aggregate_output_tokens_per_second':round(soak['output_tokens']/soak['elapsed_seconds'],2)},
 'browser_acceptance':'blocked by saved permission / managed browser origin policy; no real browser pass claimed'}
gpu=[json.loads(line) for line in (root/'gpu-samples.jsonl').read_text().splitlines()]
gpu=[r for r in gpu if since<=r['time']<=finished]
summary['soak_resources']={'samples':len(gpu),'gpu_peak_used_mb':max(r['gpu_used_mb'] for r in gpu),'gpu_min_free_mb':min(r['gpu_free_mb'] for r in gpu),'gpu_max_temperature_c':max(r['temperature'] for r in gpu),'ram_min_available_mb':min(r['ram_available_mb'] for r in gpu),'swap_max_used_mb':max(r['swap_used_mb'] for r in gpu)}
process=[json.loads(line) for line in (root/'process-samples.jsonl').read_text().splitlines()]
process=[r for r in process if since<=r['time']<=finished]
summary['soak_processes']={}
for unit in ('mavi-vllm-platform','mavi-vllm-gateway','qwen3.8-vllm'):
    values=[r['services'][unit] for r in process if r['services'].get(unit)]
    summary['soak_processes'][unit]={'pids':sorted(set(v['pid'] for v in values)),'rss_start_mb':values[0]['rss_mb'],'rss_end_mb':values[-1]['rss_mb'],'rss_peak_mb':max(v['rss_mb'] for v in values),'fds_start':values[0]['fds'],'fds_end':values[-1]['fds'],'fds_peak':max(v['fds'] for v in values)}
burst=next(c for c in report['cases'] if c.get('case')=='overload-24')['responses']
summary['overload']={str(status):sum(r['status']==status for r in burst) for status in sorted(set(r['status'] for r in burst))}
logs=subprocess.check_output(['journalctl','-u','qwen3.8-vllm.service','--since',datetime.datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M:%S'),'--until',datetime.datetime.fromtimestamp(finished).strftime('%Y-%m-%d %H:%M:%S'),'--no-pager'],text=True)
summary['soak_log_errors']=[line[:500] for line in logs.splitlines() if any(word in line for word in ('OutOfMemoryError','CUDA out of memory','memory allocation failed with OOM','EngineCore encountered a fatal error','Traceback (most recent call last)'))]
metrics=httpx.get('http://127.0.0.1:8000/metrics',trust_env=False,timeout=10).text
summary['engine_since_last_cold_start']={}
for name in ('spec_decode_num_draft_tokens_total','spec_decode_num_accepted_tokens_total','num_requests_running','num_requests_waiting'):
    values=re.findall(r'^vllm:'+name+r'(?:\{[^}]*\})?\s+([-+0-9.eE]+)',metrics,re.M)
    summary['engine_since_last_cold_start'][name]=sum(float(value) for value in values)
with closing(sqlite3.connect(root.parent/'platform.sqlite3')) as connection:
    health=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM samples WHERE created BETWEEN ? AND ? ORDER BY created',(since,finished))]
health=[row for row in health if 'health_ok' in row]
summary['soak_health_checks']={'samples':len(health),'failures':sum(not row['health_ok'] for row in health),'max_latency_ms':max((row['health_ms'] for row in health),default=0)}
(root/'final-engine-metrics.txt').write_text(metrics)
(root/'stability-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
