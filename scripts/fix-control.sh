#!/bin/bash
set -euo pipefail
sed -i 's/^NoNewPrivileges=true$/NoNewPrivileges=false/' /etc/systemd/system/mavi-vllm-platform.service
systemctl daemon-reload
systemctl restart mavi-vllm-platform.service
systemctl is-active mavi-vllm-platform.service
