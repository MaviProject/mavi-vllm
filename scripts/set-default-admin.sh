#!/bin/bash
set -euo pipefail
export MAVI_PLATFORM_ROOT=/home/mavi/mavi-vllm-platform
if [ "$EUID" -eq 0 ]; then
  exec /usr/sbin/runuser -u mavi -- /usr/local/bin/mavi-vllm-backend reset-admin
fi
exec /usr/local/bin/mavi-vllm-backend reset-admin
