"""Exercise the shipped backend binaries against a local fake vLLM."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(os.environ.get("MAVI_BACKEND_BINARY", ROOT / "build/mavi-backend")).resolve()
CHAT = "/v1/chat/completions"


def eventually(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Timed out waiting for condition")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, method, path, body=None, headers=None):
    headers = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    try:
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        result = response.read()
        if response.getheader("Content-Type", "").startswith("application/json"):
            result = json.loads(result)
        return response.status, result, dict(response.getheaders())
    finally:
        conn.close()


class FakeEngine(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, body=b"", content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health" and self.server.health_failures:
            self.server.health_failures -= 1
            self.close_connection = True
            return
        if self.path == "/metrics":
            if self.server.metrics_error:
                self.close_connection = True
                return
            metrics = (
                f'vllm:cache_config_info{{kv_cache_size_tokens="{self.server.kv}"}} 1\n'
                "vllm:prompt_tokens_total 10\n"
                "vllm:generation_tokens_total 2\n"
            )
            self.reply(metrics.encode(), "text/plain")
        else:
            self.reply()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((body, self.headers["Authorization"]))
        if body.get("hold"):
            self.connection.settimeout(5)
            try:
                if not self.connection.recv(1, socket.MSG_PEEK):
                    self.server.closed.append(body["hold"])
            except OSError:
                pass
            self.close_connection = True
            return
        usage = {"prompt_tokens": 5, "completion_tokens": 1}
        if not body.get("stream"):
            self.reply(json.dumps({"model": "liuxing", "usage": usage}).encode())
            return
        if self.path == "/v1/responses":
            events = [
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.completed", "response": {
                    "model": "liuxing", "usage": {"input_tokens": 5, "output_tokens": 1}}},
            ]
        else:
            events = [{"model": "liuxing", "choices": [{"delta": {"content": "hello"}}]},
                      {"model": "liuxing", "choices": [], "usage": usage}]
        if body.get("sse_error"):
            events.insert(1, {"error": {"message": "mock failure"}})
        data = "".join("data: " + json.dumps(event) + "\r\n\r\n" for event in events)
        if self.path != "/v1/responses" and not body.get("truncated"):
            data += "data: [DONE]\r\n\r\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # Exercise split CRLF and a JSON frame split over multiple HTTP chunks.
        split = data.index("\r\n") + 1
        for block in (data[:13], data[13:split], data[split:]):
            payload = block.encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BACKEND.is_file(), f"Build the backend first: {BACKEND}")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "data/platform.sqlite3"
        shutil.copytree(ROOT / "static", self.root / "static")
        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
        self.addCleanup(fake.server_close)
        thread = threading.Thread(target=fake.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
        thread.start()
        self.addCleanup(fake.shutdown)
        fake.calls, fake.closed = [], []
        fake.health_failures, fake.metrics_error, fake.kv = 1, False, 36864
        self.fake = fake
        control = self.root / "systemctl"
        control.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = show ]; then printf "ActiveState=active\\nSubState=running\\nNRestarts=0\\n"; exit; fi\n'
            'printf "%s\\n" "$1" >> "$MAVI_PLATFORM_ROOT/control.log"\n'
            'if [ "$1" = restart ] && [ -e "$MAVI_PLATFORM_ROOT/fail-restart" ]; then exit 1; fi\n'
        )
        control.chmod(0o755)
        self.admin_port, self.gateway_port = free_port(), free_port()
        while self.gateway_port == self.admin_port:
            self.gateway_port = free_port()
        self.env = {**os.environ, "MAVI_PLATFORM_ROOT": str(self.root),
                    "MAVI_EXISTING_API_ENV": str(self.root / "missing.env"), "MAVI_BIND_ADDRESS": "127.0.0.1",
                    "MAVI_ADMIN_PORT": str(self.admin_port), "MAVI_GATEWAY_PORT": str(self.gateway_port),
                    "MAVI_UPSTREAM_PORT": str(fake.server_port), "MAVI_SYSTEMCTL": str(control),
                    "MAVI_SUDO": "", "MAVI_NVIDIA_SMI": "/bin/false"}
        self.start_backend("admin", self.admin_port, "/api/health", self.env)
        self.start_backend("gateway", self.gateway_port, "/health", self.env)
        self.auth = self.login()
        status, self.key, _ = self.admin("POST", "/keys", {"name": "test key"})
        self.assertEqual(status, 200)
        self.bearer = {"Authorization": "Bearer " + self.key["value"]}

    def start_backend(self, mode, port, health, env):
        process = subprocess.Popen([str(BACKEND), mode], env=env)
        self.addCleanup(self.stop_backend, process)
        self.wait_for_backend(process, port, health)

    def wait_for_backend(self, process, port, health):
        def ready():
            if process.poll() is not None:
                self.fail(f"Backend exited with {process.returncode}")
            try:
                return request(port, "GET", health)[0] == 200
            except (OSError, http.client.HTTPException):
                return False

        eventually(ready)

    @staticmethod
    def stop_backend(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def login(self, headers=None, password="admin", port=None):
        status, result, response_headers = request(port or self.admin_port, "POST", "/api/login",
            {"username": "admin", "password": password}, headers)
        self.assertEqual(status, 200, result)
        self.assertIn("HttpOnly", response_headers["Set-Cookie"])
        return {"Cookie": response_headers["Set-Cookie"].split(";")[0], "X-CSRF-Token": result["csrf"]}

    def admin(self, method, path, body=None):
        return request(self.admin_port, method, "/api" + path, body, self.auth)

    def gateway(self, body, path=CHAT):
        return request(self.gateway_port, "POST", path, body, self.bearer)

    def overview(self):
        return self.admin("GET", "/overview")[1]

    def sql(self, statement, params=()):
        with sqlite3.connect(self.db, timeout=2) as db:
            return db.execute(statement, params).fetchall()

    def meta(self, key, value):
        self.sql("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def last_request(self):
        return self.sql("SELECT status,outcome,input_tokens,output_tokens,ttft_ms FROM requests ORDER BY created DESC LIMIT 1")[0]

    def test_auth_origin_csrf_and_session_rotation(self):
        status, html, headers = request(self.admin_port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", html)
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(request(self.admin_port, "GET", "/api/overview")[0], 401)
        host = f"new-host.test:{self.admin_port}"
        self.login({"Host": host, "Origin": f"http://{host}"})
        credentials = {"username": "admin", "password": "admin"}
        self.assertEqual(request(self.admin_port, "POST", "/api/login", credentials,
                                 {"Host": host, "Origin": "http://old-host.test"})[0], 403)
        stale = self.auth
        self.assertEqual(request(self.admin_port, "POST", "/api/logout", headers={"Cookie": stale["Cookie"]})[0], 403)
        self.assertEqual(self.admin("POST", "/password", {"current_password": "admin", "new_password": "new password"})[0], 200)
        self.assertEqual(self.admin("GET", "/session")[0], 401)
        self.auth = self.login(password="new password")
        self.assertEqual(self.admin("POST", "/logout")[0], 200)
        self.assertEqual(self.admin("GET", "/session")[0], 401)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(request(self.admin_port, "POST", "/api/login",
                             {"username": "admin", "password": "wrong"})[0], 401)
        self.assertEqual(request(self.admin_port, "POST", "/api/login",
                         {"username": "admin", "password": "admin"})[0], 429)

    def test_key_lifecycle(self):
        path = "/keys/" + self.key["id"]
        self.assertEqual(self.admin("GET", path + "/reveal")[1]["value"], self.key["value"])
        encrypted, = self.sql("SELECT encrypted FROM api_keys WHERE id=?", (self.key["id"],))[0]
        self.assertNotIn(self.key["value"], encrypted)
        self.assertEqual(request(self.gateway_port, "GET", "/v1/models")[0], 401)
        self.assertEqual(request(self.gateway_port, "GET", "/v1/models",
                         headers={"X-API-Key": self.key["value"]})[0], 200)
        self.assertEqual(self.admin("PATCH", path, {"enabled": False})[0], 200)
        self.assertEqual(self.gateway({"model": "liuxing"})[0], 401)
        self.assertEqual(self.admin("DELETE", path)[0], 200)

    def test_capacity_and_settings(self):
        cfg = self.overview()["config"]
        recommendations = self.admin("GET", "/recommendations")[1]
        self.assertEqual([p["concurrency"] for p in recommendations["profiles"]], [1, 2, 4, 8])
        profiles = {p["concurrency"]: p for p in recommendations["custom_profiles"]}
        self.assertEqual(list(profiles), list(range(1, 9)))
        self.assertEqual(profiles[8]["max_context"], 1024)
        for n, context in ((3, 5001), (5, 2501), (6, 2049), (7, 1031)):
            data = dict(cfg, concurrency=n, context_length=context, apply_restart=False)
            self.assertEqual(self.admin("POST", "/settings", data)[0], 200)
            saved = self.overview()["config"]
            self.assertEqual((saved["concurrency"], saved["context_length"]), (n, context))
            self.assertEqual(self.admin("POST", "/settings", dict(data, context_length=profiles[n]["max_context"] + 1))[0], 400)
            self.assertEqual(self.overview()["config"], saved)
        for field, values in (("concurrency", (0, 9, 2.5, True)), ("context_length", (1023, 32769, 5001.5, True))):
            for value in values:
                self.assertEqual(self.admin("POST", "/settings", {**cfg, field: value})[0], 422)

    def test_pending_settings_and_restart_rollback(self):
        view = eventually(lambda: (view := self.overview())["runtime"]["state"] == "running" and view)
        old = view["config"]
        self.meta("applied_config", {"concurrency": 1, "context_length": 8192})
        pending = dict(old, concurrency=2, context_length=4096, apply_restart=False)
        control = self.root / "control.log"
        self.assertTrue(self.admin("POST", "/settings", pending)[1]["restart"])
        self.assertFalse(control.exists())
        (self.root / "fail-restart").touch()
        failed = self.admin("POST", "/settings", dict(pending, context_length=2048, autostart_inference=False, apply_restart=True))
        self.assertEqual(failed[0], 500)
        self.assertEqual(self.overview()["config"]["context_length"], 4096)
        self.assertEqual(control.read_text().splitlines(), ["disable", "reset-failed", "restart", "enable"])
        (self.root / "fail-restart").unlink()
        self.assertEqual(self.admin("POST", "/settings", dict(pending, apply_restart=True))[0], 200)
        self.assertEqual(control.read_text().splitlines()[-2:], ["reset-failed", "restart"])
        self.assertEqual(self.admin("POST", "/control/stop")[1]["state"], "stopping")

    def test_metrics_failure_keeps_healthy_state_and_capacity_reference(self):
        view = eventually(lambda: (v := self.overview())["runtime"].get("kv_tokens") and v)
        tick = view["runtime"]["updated"]
        self.assertEqual(self.fake.health_failures, 0)
        self.meta("applied_config", {"concurrency": 2, "context_length": 1024})
        self.fake.kv = 10000
        def small_context_calibration():
            view = self.overview()
            return view if view["runtime"]["updated"] > tick and view["runtime"].get("kv_tokens") == 10000 else None

        view = eventually(small_context_calibration, timeout=7)
        profiles = json.loads(self.sql("SELECT value FROM meta WHERE key='capacity_profiles'")[0][0])
        self.assertEqual(profiles["2"]["kv_tokens"], 36864)
        tick = view["runtime"]["updated"]
        self.fake.metrics_error = True
        view = eventually(lambda: (v := self.overview())["runtime"]["updated"] > tick and v, timeout=7)
        self.assertEqual(view["runtime"]["state"], "running")
        self.assertEqual(view["runtime"]["kv_tokens"], 10000)
        self.assertEqual(view["runtime"]["metrics"]["vllm:prompt_tokens_total"], 10)

    def test_chart_retains_hour_and_csv_export(self):
        tick = time.time()
        with sqlite3.connect(self.db, timeout=2) as db:
            db.executemany("INSERT INTO samples VALUES (?,?)",
                ((ts, json.dumps({"created": ts, "output_tps": 1})) for ts in (tick - 3595 + 5 * i for i in range(720))))
        history = self.overview()["history"]
        self.assertLess(history[0]["created"], tick - 3000)
        self.assertGreaterEqual(history[-1]["created"], tick - 5)
        self.gateway({"model": "liuxing"})
        status, csv, _ = self.admin("GET", "/export")
        self.assertEqual(status, 200)
        self.assertTrue(csv.startswith(b"\xef\xbb\xbfid,created"))
        self.assertIn(b"test key,/v1/chat/completions,liuxing,200,success", csv)

    def test_policy_alias_and_legacy_routes(self):
        cfg = self.overview()["config"]
        self.assertEqual(self.admin("POST", "/settings", dict(cfg, served_model_name="unit-model"))[0], 200)
        status, body, _ = self.gateway({"model": "unit-model", "reasoning_effort": "high",
                                       "chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual((status, body["model"]), (200, "unit-model"))
        upstream, auth = self.fake.calls[-1]
        self.assertEqual(upstream["model"], "liuxing")
        self.assertEqual(upstream["reasoning_effort"], "none")
        self.assertFalse(upstream["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(auth.startswith("Bearer sk-internal-"))
        self.assertEqual(self.gateway({"model": "liuxing"})[0], 404)
        self.assertEqual(self.gateway({"model": "unit-model"}, "/v1/completions")[0], 400)
        self.assertEqual(self.gateway({"model": "unit-model"}, "/invocations")[0], 404)
        self.assertEqual(self.gateway({"model": "unit-model", "reasoning": {"effort": "high"}}, "/v1/responses")[0], 200)
        self.assertEqual(self.fake.calls[-1][0]["reasoning"], {"effort": "none"})
        self.assertEqual(self.admin("POST", "/settings", dict(cfg, thinking_enabled=True))[0], 200)
        self.assertEqual(self.gateway({"model": "liuxing", "prompt": [1, 2]}, "/v1/completions")[0], 200)
        self.assertEqual(self.gateway({"model": "liuxing", "prompt": ["a", "b"]}, "/v1/completions")[0], 400)

    def test_invalid_payloads_and_body_limits(self):
        for extra in ({"stream": "false"}, {"stream_options": True}, {"stream_options": {"include_usage": "yes"}},
                      {"chat_template_kwargs": []}, {"n": 100}, {"n": True}, {"best_of": 10},
                      {"use_beam_search": True}, {"beam_width": 8}, {"max_tokens": 999999}):
            self.assertEqual(self.gateway({"model": "liuxing", **extra})[0], 400, extra)
        self.assertEqual(self.gateway(b'{"model":"liuxing","temperature":NaN}')[0], 400)
        status, _, _ = request(self.gateway_port, "POST", CHAT, b"",
                               {**self.bearer, "Content-Length": str(26 * 1024 * 1024)})
        self.assertEqual(status, 413)
        status, _, _ = request(self.admin_port, "POST", "/api/login", b"",
                               {"Content-Length": str(17 * 1024)})
        self.assertEqual(status, 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway_port, timeout=8)
        try:
            conn.putrequest("POST", CHAT)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.putheader("Authorization", self.bearer["Authorization"])
            conn.endheaders()
            chunk = b"x" * (1024 * 1024)
            for _ in range(25):
                conn.send(b"100000\r\n" + chunk + b"\r\n")
            conn.send(b"1\r\n")
            self.assertEqual(conn.getresponse().status, 413)
        finally:
            conn.close()
        self.assertEqual(self.last_request()[:2], (413, "error"))
        self.assertEqual(self.gateway({"model": "liuxing", "stream_options": None, "chat_template_kwargs": None})[0], 200)

    def test_streaming_usage_responses_and_playground(self):
        body = {"model": "liuxing", "stream": True}
        status, data, headers = self.gateway(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn(b"data: [DONE]", data)
        self.assertNotIn(b"prompt_tokens", data)
        self.assertTrue(self.fake.calls[-1][0]["stream_options"]["include_usage"])
        self.assertEqual(self.last_request()[:4], (200, "success", 5, 1))
        self.assertGreater(self.last_request()[4], 0)
        self.assertEqual(self.gateway(dict(body, sse_error=True))[0], 200)
        self.assertEqual(self.last_request()[1], "error")
        self.assertEqual(self.gateway(dict(body, truncated=True))[0], 200)
        self.assertEqual(self.last_request()[1], "error")
        status, data, _ = self.gateway(body, "/v1/responses")
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", data)
        self.assertEqual(self.last_request()[:4], (200, "success", 5, 1))
        status, data, _ = self.admin("POST", "/playground", dict(body, stream_options={"include_usage": True}))
        self.assertEqual(status, 200)
        self.assertIn(b"prompt_tokens", data)

    def open_pending(self, port, path, headers, hold):
        body = json.dumps({"model": "liuxing", "stream": True, "hold": hold}).encode()
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(conn.close)
        head = f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n"
        for name, value in headers.items():
            head += f"{name}: {value}\r\n"
        conn.sendall(head.encode() + b"\r\n" + body)
        return conn

    def test_admission_and_disconnect_before_headers(self):
        sockets = [self.open_pending(self.gateway_port, CHAT, self.bearer, index + 1) for index in range(8)]
        eventually(lambda: len(self.fake.calls) == 8)
        status, _, headers = self.gateway({"model": "liuxing"})
        self.assertEqual((status, headers["Retry-After"]), (429, "3"))
        for sock in sockets:
            sock.close()
        eventually(lambda: len(self.fake.closed) == 8)
        eventually(lambda: self.sql("SELECT COUNT(*) FROM requests WHERE outcome='cancelled'")[0][0] == 8)
        self.assertEqual(self.gateway({"model": "liuxing"})[0], 200)

    def test_playground_disconnect_propagates(self):
        conn = self.open_pending(self.admin_port, "/api/playground", self.auth, 1)
        eventually(lambda: len(self.fake.calls) == 1)
        conn.close()
        eventually(lambda: self.fake.closed == [1])
        eventually(lambda: self.last_request()[:2] == (499, "cancelled") if self.sql("SELECT 1 FROM requests") else False)

    def test_engine_launcher_exec_and_context(self):
        cfg = dict(self.overview()["config"], concurrency=8, context_length=1024, apply_restart=False)
        self.assertEqual(self.admin("POST", "/settings", cfg)[0], 200)
        inference = self.root / "inference"
        launcher = inference / "venv-vllm/bin/vllm"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/usr/bin/python3\nimport json,os,sys\n"
                            "print(json.dumps({'args':sys.argv[1:],'key':os.environ['VLLM_API_KEY'].startswith('sk-internal-')}))\n")
        launcher.chmod(0o755)
        result = subprocess.run([str(BACKEND), "engine"], env={**self.env, "MAVI_INFERENCE_ROOT": str(inference)},
                                check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        args = payload["args"]
        self.assertTrue(payload["key"])
        self.assertEqual(args[args.index("--max-model-len") + 1], "1024")
        self.assertEqual(args[args.index("--max-num-seqs") + 1], "8")
        self.assertEqual(json.loads(args[args.index("--mm-processor-kwargs") + 1])["size"]["longest_edge"], 262144)
        self.assertEqual(self.overview()["applied"], {"concurrency": 8, "context_length": 1024, "image_max_pixels": 262144})

    def test_python_database_compatibility(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            self.skipTest("Python cryptography is needed for legacy Fernet interoperability")
        root = self.root / "legacy"
        data = root / "data"
        data.mkdir(parents=True)
        key = Fernet.generate_key()
        cipher = Fernet(key)
        (data / "secret.key").write_bytes(key)
        old_key = "sk-legacy-integration"
        internal = "sk-internal-legacy"
        token = "old-cookie-token"
        sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
        salt = bytes(range(16))
        password = "legacy password"
        pwd = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
        with sqlite3.connect(data / "platform.sqlite3") as db:
            db.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE users (username TEXT PRIMARY KEY,password TEXT NOT NULL);
                CREATE TABLE sessions (token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE api_keys (id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,
                  encrypted TEXT NOT NULL,tail TEXT NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,last_used REAL);
            """)
            db.executemany("INSERT INTO meta VALUES (?,?)", [
                ("keys_initialized", "true"), ("internal_key", json.dumps(cipher.encrypt(internal.encode()).decode())),
                ("config", json.dumps(dict(self.overview()["config"], served_model_name="legacy-model")))])
            db.execute("INSERT INTO users VALUES (?,?)", ("admin", salt.hex() + ":" + pwd))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (sha(token), "old-csrf", time.time() + 60))
            db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,NULL)",
                       ("old", "old key", sha(old_key), cipher.encrypt(old_key.encode()).decode(), "tion", 1, time.time()))
        admin, gateway = free_port(), free_port()
        while gateway == admin:
            gateway = free_port()
        env = {**self.env, "MAVI_PLATFORM_ROOT": str(root), "MAVI_ADMIN_PORT": str(admin), "MAVI_GATEWAY_PORT": str(gateway)}
        self.start_backend("admin", admin, "/api/health", env)
        self.start_backend("gateway", gateway, "/health", env)
        self.assertEqual(request(admin, "GET", "/api/session", headers={"Cookie": "mavi_session=" + token})[1]["csrf"], "old-csrf")
        auth = self.login(password=password, port=admin)
        self.assertEqual(request(admin, "GET", "/api/keys/old/reveal", headers=auth)[1]["value"], old_key)
        status, result, _ = request(gateway, "POST", CHAT, {"model": "legacy-model"},
                                    {"Authorization": "Bearer " + old_key})
        self.assertEqual((status, result["model"]), (200, "legacy-model"))
        self.assertEqual(self.fake.calls[-1][1], "Bearer " + internal)
        added = request(admin, "POST", "/api/keys", {"name": "native key"}, auth)[1]
        with sqlite3.connect(data / "platform.sqlite3") as db:
            sealed, = db.execute("SELECT encrypted FROM api_keys WHERE id=?", (added["id"],)).fetchone()
        self.assertEqual(cipher.decrypt(sealed.encode()).decode(), added["value"])
        self.assertEqual((data / "secret.key").read_bytes(), key)

    def test_concurrent_first_start(self):
        root = self.root / "parallel"
        admin, gateway = free_port(), free_port()
        while gateway == admin:
            gateway = free_port()
        env = {**self.env, "MAVI_PLATFORM_ROOT": str(root), "MAVI_ADMIN_PORT": str(admin),
               "MAVI_GATEWAY_PORT": str(gateway)}
        processes = []
        for role in ("admin", "gateway"):
            process = subprocess.Popen([str(BACKEND), role], env=env)
            self.addCleanup(self.stop_backend, process)
            processes.append(process)
        self.wait_for_backend(processes[0], admin, "/api/health")
        self.wait_for_backend(processes[1], gateway, "/health")
        auth = self.login(port=admin)
        keys = request(admin, "GET", "/api/keys", headers=auth)[1]["keys"]
        self.assertEqual(len(keys), 1)
        key = request(admin, "GET", f"/api/keys/{keys[0]['id']}/reveal", headers=auth)[1]["value"]
        self.assertEqual(request(gateway, "GET", "/v1/models", headers={"X-API-Key": key})[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
