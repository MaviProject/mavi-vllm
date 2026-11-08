import json,os,sqlite3,subprocess
from pathlib import Path
from cryptography.fernet import Fernet

data=Path(os.environ.get('MAVI_PLATFORM_ROOT',Path(__file__).resolve().parents[1]))/'data'
cipher=Fernet((data/'secret.key').read_bytes())
with sqlite3.connect(data/'platform.sqlite3') as connection:
    internal=connection.execute("SELECT value FROM meta WHERE key='internal_key'").fetchone()[0]
    tokens=[json.loads(internal),*(row[0] for row in connection.execute('SELECT encrypted FROM api_keys'))]
keys=[cipher.decrypt(token.encode()).decode() for token in tokens]
log=subprocess.check_output(['journalctl','-u','qwen3.8-vllm.service','-u','mavi-vllm-platform.service','-u','mavi-vllm-gateway.service','--since','2026-11-06 23:26:30','--no-pager','-o','short-iso'],text=True)
for key in keys:
    if key:log=log.replace(key,'<REDACTED_API_KEY>')
out=data/'production-qa/service-journal-redacted.log'
out.write_text(log);out.chmod(0o600)
print('Stored redacted service diagnostic journal')
