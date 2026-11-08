set -euo pipefail
python3 - <<'PY'
import os,subprocess,pathlib,time
root=pathlib.Path('/home/mavi/mavi-vllm-platform')
pid=subprocess.check_output(['pgrep','-u','mavi','-x','gnome-shell'],text=True).split()[0]
desktop=dict(item.split('=',1) for item in (pathlib.Path('/proc')/pid/'environ').read_bytes().decode(errors='replace').split('\0') if '=' in item)
env=os.environ.copy()
for name in ['DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','XAUTHORITY','XDG_CURRENT_DESKTOP','DESKTOP_SESSION','XDG_SESSION_TYPE']:
    if name in desktop:env[name]=desktop[name]
log=open(root/'data/desktop-launch.log','ab')
p=subprocess.Popen([str(root/'scripts/open-platform.sh')],env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
try:code=p.wait(timeout=8)
except subprocess.TimeoutExpired:code=None
print('DESKTOP_LAUNCH_PID',p.pid,'EXIT_CODE',code)
print('AUTOSTART_FILE',(pathlib.Path.home()/'.config/autostart/mavi-vllm-platform.desktop').exists())
PY
systemctl is-enabled mavi-vllm-platform.service mavi-vllm-gateway.service qwen3.8-vllm.service
