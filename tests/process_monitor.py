import json,time,subprocess
from pathlib import Path
import psutil

root=Path('/home/mavi/mavi-vllm-platform/data/production-qa')
(root/'process-monitor.pid').write_text(str(__import__('os').getpid()))
with (root/'process-samples.jsonl').open('a',buffering=1) as output:
    while True:
        row={'time':time.time(),'services':{}}
        for unit in ('mavi-vllm-platform','mavi-vllm-gateway','qwen3.8-vllm'):
            raw=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True)
            try:
                process=psutil.Process(int(raw.strip()))
                group=[process]+process.children(recursive=True)
                row['services'][unit]={'pid':process.pid,'rss_mb':sum(p.memory_info().rss for p in group)/2**20,'fds':sum(p.num_fds() for p in group),'threads':sum(p.num_threads() for p in group)}
            except (ValueError,psutil.Error):row['services'][unit]={}
        output.write(json.dumps(row)+'\n');time.sleep(5)
