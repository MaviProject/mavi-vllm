#!/bin/bash
set -euo pipefail
/usr/sbin/runuser -u mavi -- /home/mavi/inference/venv-vllm/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('/home/mavi/mavi-vllm-platform/data/bootstrap-credentials.json')
if p.exists():
    data=json.loads(p.read_text())
    data.update(url='http://127.0.0.1:20204/',remote_url_template='http://<server-ip>:20204/')
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2));p.chmod(0o600)
PY
systemctl restart mavi-vllm-platform.service mavi-vllm-gateway.service
systemctl is-active mavi-vllm-platform.service mavi-vllm-gateway.service qwen3.8-vllm.service
ss -lnt | grep -E ':80 |:8000 |:20204 '
if command -v ufw >/dev/null; then ufw status verbose; fi
