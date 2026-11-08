#!/bin/bash
set -euo pipefail
app=/home/mavi/mavi-vllm-platform
runtime=/home/mavi/inference/venv-vllm/bin
/usr/sbin/runuser -u mavi -- "$runtime/python" "$app/scripts/patch-loader-memory.py"
install -d -o mavi -g mavi /home/mavi/.config/autostart
cat > /home/mavi/.config/autostart/mavi-vllm-platform.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=mavi_vllm管理平台
Comment=Open the local inference management platform
Exec=/home/mavi/mavi-vllm-platform/scripts/open-platform.sh
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
DESKTOP
chown mavi:mavi /home/mavi/.config/autostart/mavi-vllm-platform.desktop
chmod +x "$app/scripts/open-platform.sh"
mkdir -p "$app/backups"
if [ ! -f "$app/backups/qwen3.8-vllm.original.service" ]; then
  cp /etc/systemd/system/qwen3.8-vllm.service "$app/backups/qwen3.8-vllm.original.service"
fi
cat > /etc/sudoers.d/mavi-vllm-platform <<'SUDO'
mavi ALL=(root) NOPASSWD: /usr/bin/systemctl start qwen3.8-vllm.service --no-block, /usr/bin/systemctl stop qwen3.8-vllm.service --no-block, /usr/bin/systemctl restart qwen3.8-vllm.service --no-block, /usr/bin/systemctl enable qwen3.8-vllm.service --no-block, /usr/bin/systemctl disable qwen3.8-vllm.service --no-block, /usr/bin/systemctl reset-failed qwen3.8-vllm.service --no-block
SUDO
chmod 440 /etc/sudoers.d/mavi-vllm-platform
visudo -cf /etc/sudoers.d/mavi-vllm-platform
cat > /etc/systemd/system/mavi-vllm-platform.service <<'UNIT'
[Unit]
Description=mavi_vllm management platform
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=mavi
Group=mavi
WorkingDirectory=/home/mavi/mavi-vllm-platform/backend
Environment=MAVI_PLATFORM_ROOT=/home/mavi/mavi-vllm-platform
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mavi/inference/venv-vllm/bin/python -m uvicorn main:app --host 0.0.0.0 --port 20204 --no-proxy-headers --no-server-header
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=false
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/mavi-vllm-gateway.service <<'UNIT'
[Unit]
Description=mavi OpenAI authenticated API gateway
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=mavi
Group=mavi
WorkingDirectory=/home/mavi/mavi-vllm-platform/backend
Environment=MAVI_PLATFORM_ROOT=/home/mavi/mavi-vllm-platform
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mavi/inference/venv-vllm/bin/python -m uvicorn gateway:app --host 0.0.0.0 --port 80 --no-proxy-headers --no-server-header
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/qwen3.8-vllm.service <<'UNIT'
[Unit]
Description=liuxing vision MTP inference engine managed by mavi
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=3
[Service]
Type=simple
User=mavi
Group=mavi
WorkingDirectory=/home/mavi/mavi-vllm-platform/backend
Environment=MAVI_PLATFORM_ROOT=/home/mavi/mavi-vllm-platform
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mavi/inference/venv-vllm/bin/python /home/mavi/mavi-vllm-platform/backend/engine_launcher.py
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
KillMode=control-group
UMask=0077
NoNewPrivileges=true
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl stop qwen3.8-vllm.service
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow 20204/tcp comment 'mavi vLLM management'
fi
systemctl enable mavi-vllm-platform.service mavi-vllm-gateway.service qwen3.8-vllm.service
systemctl restart mavi-vllm-platform.service mavi-vllm-gateway.service
systemctl reset-failed qwen3.8-vllm.service
systemctl start qwen3.8-vllm.service
systemctl is-active mavi-vllm-platform.service mavi-vllm-gateway.service qwen3.8-vllm.service
