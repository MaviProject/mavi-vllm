set -euo pipefail
cd /home/mavi/mavi-vllm-platform/backend
/home/mavi/inference/venv-vllm/bin/python - <<'PY'
import core,json
core.initialize()
with core.connect() as c:
    c.execute('DELETE FROM users')
    c.execute('INSERT INTO users VALUES (?,?)',('admin',core.password_hash('admin')))
    c.execute('DELETE FROM sessions')
credentials=dict(url='http://127.0.0.1:20204/',remote_url_template='http://<server-ip>:20204/',username='admin',password='admin')
path=core.DATA/'bootstrap-credentials.json';path.write_text(json.dumps(credentials,indent=2));path.chmod(0o600)
core.audit('account.default_updated','admin')
print('Default web account updated; sessions invalidated.')
PY
