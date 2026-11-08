#!/bin/bash
set -euo pipefail
journalctl -u qwen3.8-vllm.service -n 0 --show-cursor --no-pager > /home/mavi/mavi-vllm-platform/data/production-qa/start-journal-cursor.txt
date --iso-8601=seconds > /home/mavi/mavi-vllm-platform/data/production-qa/start-time.txt
mkdir -p /etc/systemd/system/qwen3.8-vllm.service.d
cat > /etc/systemd/system/qwen3.8-vllm.service.d/stability.conf <<'CONF'
[Unit]
StartLimitIntervalSec=600
StartLimitBurst=3
[Service]
RestartSec=15
CONF
if ! grep -q 'reset-failed' /etc/sudoers.d/mavi-vllm-platform; then
  printf '\nmavi ALL=(root) NOPASSWD: /usr/bin/systemctl reset-failed qwen3.8-vllm.service --no-block\n' >> /etc/sudoers.d/mavi-vllm-platform
fi
visudo -cf /etc/sudoers.d/mavi-vllm-platform
systemctl daemon-reload
systemctl restart mavi-vllm-platform.service mavi-vllm-gateway.service
systemctl reset-failed qwen3.8-vllm.service
systemctl restart qwen3.8-vllm.service
