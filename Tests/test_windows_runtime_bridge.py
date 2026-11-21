import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    import pywintypes
    import win32file
    import win32pipe
    import win32security
    from Windows.desktop import Desktop
    from Windows.measurement import VerifiedMeasurement
    from Windows.runtime_bridge import (
        WindowsRuntimeBridge,
        _find_clash_tauri_ws_target,
        _read_active_port_file,
    )
    from Windows.runtime_broker import (
        RuntimeBridgeUnavailable,
        RuntimeBroker,
        RuntimeBrokerError,
        RuntimeProfileSwitch,
        RuntimeSwitchOutcomeUnknown,
        harden_current_user_file_acl,
        make_bridge_loader_script,
    )

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _http_call(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, dict(resp.getheaders()), raw
    finally:
        conn.close()


class _MockCdpServer:
    """Minimal loopback HTTP + RFC 6455 WebSocket server simulating Edge WebView2 CDP."""

    def __init__(self, page_url="http://tauri.localhost/profile", on_eval=None):
        self.page_url = page_url
        self.on_eval = on_eval
        self.evaluated = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            self._sock.settimeout(0.1)
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, conn):
        try:
            conn.settimeout(3.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, rest = buf.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            req_line = lines[0].split(" ")
            path = req_line[1]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if path == "/json/list":
                body = json.dumps([
                    {
                        "id": "page-1",
                        "type": "page",
                        "url": self.page_url,
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/page-1",
                    }
                ]).encode("utf-8")
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
                return
            if path.startswith("/devtools/page/") and headers.get("upgrade", "").lower() == "websocket":
                key = headers["sec-websocket-key"]
                accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                data = bytearray(rest)

                def read_exact(n):
                    while len(data) < n:
                        c = conn.recv(4096)
                        if not c:
                            raise EOFError()
                        data.extend(c)
                    out = bytes(data[:n])
                    del data[:n]
                    return out

                b1, b2 = read_exact(2)
                masked = bool(b2 & 0x80)
                ln = b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack("!H", read_exact(2))[0]
                elif ln == 127:
                    ln = struct.unpack("!Q", read_exact(8))[0]
                mkey = read_exact(4) if masked else b""
                payload = read_exact(ln)
                if masked:
                    payload = bytes(b ^ mkey[i & 3] for i, b in enumerate(payload))
                req = json.loads(payload.decode("utf-8"))
                expr = req["params"]["expression"]
                self.evaluated.append(expr)
                val = self.on_eval(expr) if self.on_eval else "attached"
                resp_payload = json.dumps(
                    {"id": req["id"], "result": {"result": {"type": "string", "value": val}}}
                ).encode("utf-8")
                hdr = bytearray([0x81])
                if len(resp_payload) < 126:
                    hdr.append(len(resp_payload))
                elif len(resp_payload) < 65536:
                    hdr.append(126)
                    hdr.extend(struct.pack("!H", len(resp_payload)))
                else:
                    hdr.append(127)
                    hdr.extend(struct.pack("!Q", len(resp_payload)))
                conn.sendall(bytes(hdr) + resp_payload)
                return
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=1.0)


@unittest.skipUnless(sys.platform == "win32", "Windows-only runtime bridge test")
class RuntimeBrokerAndBridgeTests(unittest.TestCase):
    def test_broker_auth_origin_whitelist_and_no_token_leak(self):
        broker = RuntimeBroker(heartbeat_ttl=5.0)
        try:
            port = broker.start()
            self.assertTrue(1 <= port <= 65535)
            self.assertNotIn(broker._token, repr(broker))

            st, _, _ = _http_call(port, "GET", "/poll?client=c1")
            self.assertEqual(st, 401)

            st, _, _ = _http_call(port, "GET", "/poll?client=c1", headers={"Authorization": "Bearer wrong"})
            self.assertEqual(st, 401)

            auth = {"Authorization": f"Bearer {broker._token}"}
            st, _, _ = _http_call(port, "OPTIONS", "/hello", headers={"Origin": "https://evil.example"})
            self.assertEqual(st, 403)
            st, _, _ = _http_call(
                port, "POST", "/hello",
                headers={**auth, "Origin": "https://evil.example"},
                body={"client": "client-1", "origin": "https://evil.example"},
            )
            self.assertEqual(st, 403)

            st, hdrs, _ = _http_call(port, "OPTIONS", "/hello", headers={"Origin": "http://tauri.localhost"})
            self.assertEqual(st, 204)
            self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "http://tauri.localhost")

            st, _, _ = _http_call(
                port, "POST", "/hello",
                headers={**auth, "Origin": "http://tauri.localhost"},
                body={"client": "client-1", "origin": "http://tauri.localhost"},
            )
            self.assertEqual(st, 200)
            self.assertTrue(broker.is_attached)

            def fake_tauri_client():
                for _ in range(40):
                    s, _, raw = _http_call(
                        port, "GET", "/poll?client=client-1",
                        headers={**auth, "Origin": "http://tauri.localhost"},
                    )
                    if s == 200:
                        cmd = json.loads(raw.decode("utf-8"))["command"]
                        self.assertEqual(cmd["op"], "switch")
                        self.assertEqual(cmd["uid"], "uid2")
                        _http_call(
                            port, "POST", "/result",
                            headers={**auth, "Origin": "http://tauri.localhost"},
                            body={
                                "client": "client-1",
                                "id": cmd["id"],
                                "ok": True,
                                "dispatched": True,
                                "value": {"previous": "uid1", "current": "uid2", "name": "Sub2"},
                            },
                        )
                        return
                    time.sleep(0.02)

            t = threading.Thread(target=fake_tauri_client, daemon=True)
            t.start()
            res = broker.enqueue_switch("uid2", pickup_timeout=2.0, execution_timeout=3.0)
            t.join(timeout=2.0)
            self.assertEqual(res, RuntimeProfileSwitch(previous="uid1", current="uid2", name="Sub2"))
        finally:
            broker.stop()

    def test_broker_distinguishes_unpicked_vs_dispatched_unknown_outcome(self):
        broker = RuntimeBroker(heartbeat_ttl=5.0)
        try:
            port = broker.start()
            auth = {"Authorization": f"Bearer {broker._token}", "Origin": "http://tauri.localhost"}
            _http_call(port, "POST", "/hello", headers=auth, body={"client": "c-one", "origin": "http://tauri.localhost"})

            with self.assertRaises(RuntimeBridgeUnavailable):
                broker.enqueue_switch("uid2", pickup_timeout=0.25, execution_timeout=0.5)

            _http_call(port, "POST", "/hello", headers=auth, body={"client": "c-two", "origin": "http://tauri.localhost"})

            def respond_not_found():
                for _ in range(30):
                    s, _, raw = _http_call(port, "GET", "/poll?client=c-two", headers=auth)
                    if s == 200:
                        cmd = json.loads(raw.decode("utf-8"))["command"]
                        _http_call(
                            port, "POST", "/result", headers=auth,
                            body={"client": "c-two", "id": cmd["id"], "ok": False, "dispatched": False, "error": "profile not found"},
                        )
                        return
                    time.sleep(0.02)

            t = threading.Thread(target=respond_not_found, daemon=True)
            t.start()
            with self.assertRaises(RuntimeBrokerError) as ctx:
                broker.enqueue_switch("uid_missing", pickup_timeout=2.0, execution_timeout=2.0)
            self.assertNotIsInstance(ctx.exception, RuntimeSwitchOutcomeUnknown)
            t.join(timeout=2.0)

            def pickup_and_hang():
                for _ in range(30):
                    s, _, _ = _http_call(port, "GET", "/poll?client=c-two", headers=auth)
                    if s == 200:
                        return
                    time.sleep(0.02)

            t2 = threading.Thread(target=pickup_and_hang, daemon=True)
            t2.start()
            with self.assertRaises(RuntimeSwitchOutcomeUnknown):
                broker.enqueue_switch("uid2", pickup_timeout=1.0, execution_timeout=0.3)
            t2.join(timeout=2.0)
        finally:
            broker.stop()

    def test_loader_script_enforces_tauri_whitelist_and_harden_acl(self):
        script = make_bridge_loader_script("http://127.0.0.1:43210", "t" * 32)
        self.assertIn("http://tauri.localhost", script)
        self.assertIn("patch_profiles_config", script)
        self.assertIn("get_profiles", script)
        with self.assertRaises(ValueError):
            make_bridge_loader_script("http://0.0.0.0:43210", "t" * 32)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "private.json"
            p.write_text("{}", encoding="utf-8")
            harden_current_user_file_acl(p)
            sd = win32security.GetFileSecurity(str(p), win32security.DACL_SECURITY_INFORMATION)
            dacl = sd.GetSecurityDescriptorDacl()
            self.assertEqual(dacl.GetAceCount(), 2)

    def test_cdp_bridge_discovers_tauri_target_and_injects_silently(self):
        with tempfile.TemporaryDirectory() as td:
            wv_dir = Path(td) / "EBWebView"
            wv_dir.mkdir()

            bridge_holder = {}

            def on_eval(_expr):
                broker = bridge_holder["bridge"]._broker
                auth = {"Authorization": f"Bearer {broker._token}", "Origin": "http://tauri.localhost"}
                threading.Thread(
                    target=lambda: _http_call(
                        broker.port, "POST", "/hello", headers=auth,
                        body={"client": "sim-tauri", "origin": "http://tauri.localhost"},
                    ),
                    daemon=True,
                ).start()
                return "attached"

            cdp = _MockCdpServer(page_url="http://tauri.localhost/profile", on_eval=on_eval)
            try:
                (wv_dir / "DevToolsActivePort").write_text(f"{cdp.port}\n/devtools/browser/test\n", encoding="utf-8")
                self.assertEqual(_read_active_port_file(wv_dir), (cdp.port, "/devtools/browser/test"))
                self.assertIsNotNone(_find_clash_tauri_ws_target(cdp.port))

                bridge = WindowsRuntimeBridge(webview_dir=wv_dir, policy_checker=lambda: True)
                bridge_holder["bridge"] = bridge
                try:
                    st = bridge.status(force_probe=True)
                    self.assertTrue(st["ready"])
                    self.assertEqual(st["state"], "cdp_ready")
                    self.assertTrue(bridge.ensure_attached(timeout=4.0))
                    self.assertEqual(len(cdp.evaluated), 1)
                    st2 = bridge.status(force_probe=True)
                    self.assertTrue(st2["attached"])
                    self.assertEqual(st2["state"], "attached")
                finally:
                    bridge.close()
            finally:
                cdp.close()

            bad_cdp = _MockCdpServer(page_url="https://example.com/")
            try:
                self.assertIsNone(_find_clash_tauri_ws_target(bad_cdp.port))
            finally:
                bad_cdp.close()


@unittest.skipUnless(sys.platform == "win32", "Windows-only desktop cross-profile hot-switch test")
class DesktopCrossProfileHotSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "profiles").mkdir()
        self.pipe_name = f"\\\\.\\pipe\\clash-hot-switch-test-{os.getpid()}-{id(self)}"
        self.secret = "hot-switch-secret"
        self.current_profile = "uid1"
        self.now_by_profile = {"uid1": "A", "uid2": "X"}
        self.members_by_profile = {"uid1": ["A", "B"], "uid2": ["X", "Y"]}
        self.puts = []
        self.stop_pipe = threading.Event()
        self.ready_pipe = threading.Event()
        self.pipe_thread = threading.Thread(target=self._serve_pipe, daemon=True)
        self.pipe_thread.start()
        self.ready_pipe.wait(5)

        (self.root / "profiles" / "one.yaml").write_text("proxies:\n- {name: A}\n- {name: B}\n", encoding="utf-8")
        (self.root / "profiles" / "two.yaml").write_text("proxies:\n- {name: X}\n- {name: Y}\n", encoding="utf-8")
        self._write_profiles_yaml("uid1")
        (self.root / "clash-verge.yaml").write_text(
            f"external-controller-pipe: '{self.pipe_name}'\nsecret: {self.secret}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.stop_pipe.set()
        try:
            h = win32file.CreateFile(self.pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                     0, None, win32file.OPEN_EXISTING, 0, None)
            h.Close()
        except Exception:
            pass
        self.pipe_thread.join(timeout=2)
        self.temp.cleanup()

    def _write_profiles_yaml(self, current_uid):
        self.current_profile = current_uid
        (self.root / "profiles.yaml").write_text(
            f"current: {current_uid}\nitems:\n"
            "- {uid: uid1, name: SubOne, type: remote, file: one.yaml}\n"
            "- {uid: uid2, name: SubTwo, type: remote, file: two.yaml}\n",
            encoding="utf-8",
        )

    def _serve_pipe(self):
        def create_pipe():
            return win32pipe.CreateNamedPipe(
                self.pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 1000, None,
            )

        pipe = create_pipe()
        self.ready_pipe.set()
        while not self.stop_pipe.is_set():
            next_pipe = None
            try:
                try:
                    win32pipe.ConnectNamedPipe(pipe, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                        raise
                if self.stop_pipe.is_set():
                    continue
                next_pipe = create_pipe()
                data = b""
                while b"\r\n\r\n" not in data:
                    _, chunk = win32file.ReadFile(pipe, 65536)
                    if not chunk:
                        break
                    data += chunk
                if b"\r\n\r\n" not in data:
                    continue
                head, body = data.split(b"\r\n\r\n", 1)
                lines = head.decode("utf-8").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
                length = int(headers.get("content-length", "0"))
                while len(body) < length:
                    _, chunk = win32file.ReadFile(pipe, 65536)
                    body += chunk
                cur = self.current_profile
                now = self.now_by_profile[cur]
                members = self.members_by_profile[cur]
                if method == "GET" and path == "/proxies":
                    payload = json.dumps({"proxies": {"Proxy": {"name": "Proxy", "type": "Selector", "now": now, "all": members}}}).encode()
                    resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
                elif method == "GET" and path == "/proxies/Proxy":
                    payload = json.dumps({"name": "Proxy", "type": "Selector", "now": now, "all": members}).encode()
                    resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
                elif method == "PUT" and path == "/proxies/Proxy":
                    target = json.loads(body.decode("utf-8"))["name"]
                    self.now_by_profile[cur] = target
                    self.puts.append((cur, target))
                    resp = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                else:
                    resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                win32file.WriteFile(pipe, resp)
                win32file.FlushFileBuffers(pipe)
            except pywintypes.error:
                pass
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(pipe)
                except Exception:
                    pass
                pipe.Close()
                if next_pipe is not None:
                    pipe = next_pipe
                elif not self.stop_pipe.is_set():
                    pipe = create_pipe()

    def test_automatic_and_manual_cross_profile_hot_switch_with_fallback_and_lock(self):
        test_self = self

        class StubBridge:
            def __init__(self, mode="ok"):
                self.mode = mode
                self.switched_to = []

            def status(self, force_probe=False):
                if self.mode == "unavailable":
                    return {"state": "not_configured", "ready": False, "attached": False,
                            "cdp_ready": False, "policy_configured": False, "label": "跨订阅热切 未启用"}
                return {"state": "attached", "ready": True, "attached": True,
                        "cdp_ready": True, "policy_configured": True, "label": "跨订阅热切 已连接"}

            def switch_profile(self, uid, timeout=25.0, is_cancelled=None):
                if self.mode == "unavailable":
                    raise RuntimeBridgeUnavailable("unavailable")
                if self.mode == "unknown":
                    raise RuntimeSwitchOutcomeUnknown("timeout after dispatch")
                self.switched_to.append(uid)
                prev = test_self.current_profile
                test_self._write_profiles_yaml(uid)
                return RuntimeProfileSwitch(previous=prev, current=uid, name="SubTwo" if uid == "uid2" else "SubOne")

            def close(self):
                return

        stub = StubBridge("ok")
        app = Desktop(self.root / "worker.exe", root=self.root, runtime_bridge=stub)
        try:
            self.assertTrue(app.refresh()["ok"])
            st = app.get_state()
            self.assertEqual([p["uid"] for p in st["profiles"] if p["selected"]], ["uid1"])
            self.assertTrue(st["runtime_bridge"]["ready"])
            self.assertTrue(app.refresh_groups()["ok"])

            res = app.switch_manual({"uid": "uid2", "name": "Y", "group": "Proxy"})
            self.assertTrue(res["ok"], res)
            self.assertEqual(stub.switched_to, ["uid2"])
            self.assertEqual(self.current_profile, "uid2")
            self.assertEqual(self.now_by_profile["uid2"], "Y")
            self.assertEqual(self.puts, [("uid2", "Y")])
            self.assertIsNone(app.get_state()["pending"])
            self.assertFalse(app.get_state()["outcome_unknown"])

            with app.lock:
                winner = next(r for r in app.rows if r.uid == "uid1" and r.name == "B")
                winner.measurement = VerifiedMeasurement("B", "Synthetic", 6, 6, 15, 1, 262144, 262144, True, 8_000_000)
                cap = app.capture
                gen = app.generation
            app._automatic(winner, "Proxy", cap, gen)
            self.assertEqual(self.current_profile, "uid1")
            self.assertEqual(self.now_by_profile["uid1"], "B")
            self.assertIsNone(app.get_state()["pending"])
            self.assertIn("已自动热切订阅", app.get_state()["status"])
        finally:
            app.close()

        self._write_profiles_yaml("uid1")
        stub_off = StubBridge("unavailable")
        app2 = Desktop(self.root / "worker.exe", root=self.root, runtime_bridge=stub_off)
        try:
            self.assertTrue(app2.refresh()["ok"])
            self.assertTrue(app2.refresh_groups()["ok"])
            with app2.lock:
                winner = next(r for r in app2.rows if r.uid == "uid2" and r.name == "Y")
                winner.measurement = VerifiedMeasurement("Y", "Synthetic", 6, 6, 15, 1, 262144, 262144, True, 8_000_000)
                cap = app2.capture
                gen = app2.generation
            app2._automatic(winner, "Proxy", cap, gen)
            st2 = app2.get_state()
            self.assertIsNotNone(st2["pending"])
            self.assertEqual(st2["pending"]["uid"], "uid2")
            self.assertFalse(st2["pending"]["ready"])
            self.assertFalse(st2["outcome_unknown"])

            res_m = app2.switch_manual({"uid": "uid2", "name": "Y", "group": "Proxy"})
            self.assertFalse(res_m["ok"])
            self.assertFalse(app2.get_state()["outcome_unknown"])
        finally:
            app2.close()

        stub_unk = StubBridge("unknown")
        app3 = Desktop(self.root / "worker.exe", root=self.root, runtime_bridge=stub_unk)
        try:
            self.assertTrue(app3.refresh()["ok"])
            self.assertTrue(app3.refresh_groups()["ok"])
            res_u = app3.switch_manual({"uid": "uid2", "name": "Y", "group": "Proxy"})
            self.assertFalse(res_u["ok"])
            self.assertTrue(app3.get_state()["outcome_unknown"])
            self.assertIn("未知", app3.get_state()["status"])
            self.assertFalse(app3.refresh_groups()["ok"])
            self.assertFalse(app3.switch_manual({"uid": "uid1", "name": "A", "group": "Proxy"})["ok"])
        finally:
            app3.close()

    def test_resolve_target_group_matches_core_name_across_emoji_variants(self):
        from Windows.controller import ProxyGroup
        groups = (
            ProxyGroup("GLOBAL", "DIRECT", ("DIRECT", "X", "Y")),
            ProxyGroup("🚀 节点选择", "X", ("X", "Y")),
            ProxyGroup("🐟 漏网之鱼", "DIRECT", ("DIRECT", "X", "Y")),
        )
        self.assertEqual(
            Desktop._resolve_target_group(groups, "🔰 节点选择", "Y", ("X", "Y")),
            "🚀 节点选择",
        )
        # Ambiguous without matching core name must stay None (never guess)
        self.assertIsNone(
            Desktop._resolve_target_group(groups, "完全无关组", "Y", ("X", "Y"))
        )


if __name__ == "__main__":
    unittest.main()
