#!/bin/bash
/usr/bin/python3 - <<'PY'
import urllib.request,time
for _ in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:20204/api/health',timeout=2) as r:
            if r.status==200:break
    except Exception:pass
    time.sleep(2)
PY
browser=$(/usr/bin/xdg-settings get default-web-browser 2>/dev/null || true)
if [ -z "$browser" ] && [ -x /usr/bin/firefox ]; then
    exec /usr/bin/firefox --new-window http://127.0.0.1:20204/
fi
exec /usr/bin/xdg-open http://127.0.0.1:20204/
