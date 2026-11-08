import subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import core

keys=[core.internal_key()]
with core.connect() as connection:
    ids=[row['id'] for row in connection.execute('SELECT id FROM api_keys')]
keys.extend(core.reveal_key(kid) for kid in ids)
log=subprocess.check_output(['journalctl','-u','qwen3.8-vllm.service','-u','mavi-vllm-platform.service','-u','mavi-vllm-gateway.service','--since','2026-11-06 23:26:30','--no-pager','-o','short-iso'],text=True)
for key in keys:
    if key:log=log.replace(key,'<REDACTED_API_KEY>')
out=core.DATA/'production-qa/service-journal-redacted.log'
out.write_text(log);out.chmod(0o600)
print('Stored redacted service diagnostic journal')
