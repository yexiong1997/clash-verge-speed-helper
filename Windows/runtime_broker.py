"""Local bearer-token broker for Windows Clash Verge seamless profile switching.

Strictly binds to 127.0.0.1 on an ephemeral port, authenticates every request
with a high-entropy random bearer token, restricts CORS origins to official
Tauri localhost origins, and exposes only an exact-profile switch + detach
command whitelist (never arbitrary JavaScript execution or filesystem access).
"""

from dataclasses import dataclass
import hmac
import http.server
import json
import os
from pathlib import Path
import secrets
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlparse
import uuid

ALLOWED_TAURI_ORIGINS = frozenset({
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
})
BRIDGE_WINDOW_SLOT = "__clashSpeedHelperRuntimeBridgeV1"
MAX_REQUEST_BYTES = 64 * 1024
HEARTBEAT_TTL_SECONDS = 12.0


class RuntimeBrokerError(Exception):
    """Generic sanitized runtime broker error (never carries tokens or secrets)."""


class RuntimeBridgeUnavailable(RuntimeBrokerError):
    """Bridge is not attached or command was never picked up by Clash Verge."""


class RuntimeSwitchCancelled(RuntimeBrokerError):
    """Switch request was cancelled before completion."""


class RuntimeSwitchOutcomeUnknown(RuntimeBrokerError):
    """Switch command reached Clash Verge but completion could not be confirmed."""


@dataclass(frozen=True)
class RuntimeProfileSwitch:
    previous: str
    current: str
    name: str


def valid_profile_uid(uid):
    if not isinstance(uid, str) or not uid:
        return False
    try:
        raw = uid.encode("utf-8", "strict")
    except UnicodeError:
        return False
    if len(raw) > 256:
        return False
    return not any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in uid)


def harden_current_user_file_acl(path):
    """Restrict an existing file's DACL strictly to the current user and SYSTEM."""
    import win32api
    import win32con
    import win32security

    FILE_ALL_ACCESS = 0x001F01FF
    target = Path(path)
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, FILE_ALL_ACCESS, user_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, FILE_ALL_ACCESS, system_sid)
    win32security.SetNamedSecurityInfo(
        str(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def make_bridge_loader_script(base_url, token, slot=BRIDGE_WINDOW_SLOT):
    """Build the minimal Tauri-side bridge script injected over local CDP.

    Security invariants:
    - Runs only on official Tauri localhost origins with __TAURI_INTERNALS__.invoke.
    - Authenticates every broker request with Authorization: Bearer <token>.
    - Whitelists only `switch` (get_profiles + patch_profiles_config + verify)
      and `detach`.
    - Never logs tokens, profile URLs, node secrets, or credentials to console.
    """
    if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
        raise ValueError("base_url must target loopback 127.0.0.1")
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("token too short")
    base_json = json.dumps(base_url.rstrip("/"))
    auth_json = json.dumps(f"Bearer {token}")
    slot_json = json.dumps(slot)
    return (
        "(()=>{"
        f"const B={base_json},A={auth_json},S={slot_json};"
        "const W=ms=>new Promise(r=>setTimeout(r,ms));"
        "const OK_ORIGINS=['http://tauri.localhost','https://tauri.localhost','tauri://localhost'];"
        "if(!window.__TAURI_INTERNALS__||typeof window.__TAURI_INTERNALS__.invoke!=='function'"
        "||!OK_ORIGINS.includes(location.origin))throw Error('Tauri page required');"
        "try{window[S]?.stop?.();}catch(_){}"
        "let live=true,errs=0;"
        "const C=(crypto&&crypto.randomUUID)?crypto.randomUUID():String(Date.now())+'-'+Math.random().toString(16).slice(2);"
        "const I=window.__TAURI_INTERNALS__.invoke;"
        "const J=(p,m='GET',b)=>fetch(B+p,{method:m,cache:'no-store',"
        "headers:{Authorization:A,'Content-Type':'application/json'},"
        "body:b===undefined?undefined:JSON.stringify(b)});"
        "const stop=()=>{live=false;try{if(window[S]?.client===C)delete window[S];}catch(_){}};"
        "const T0=Date.now();window[S]={stop,client:C};"
        "(async()=>{"
        "try{"
        "const hi=await J('/hello','POST',{client:C,origin:location.origin});"
        "if(!hi.ok)throw Error('HTTP '+hi.status);"
        "while(live){"
        "let r;"
        "try{"
        "r=await J('/poll?client='+encodeURIComponent(C));"
        "if(!r.ok&&r.status!==204){"
        "if(r.status===401||r.status===403||r.status===409)throw Error('HTTP '+r.status);"
        "throw Error('poll '+r.status);"
        "}"
        "}catch(pe){"
        "if(!live)break;"
        "if(++errs>=8||String(pe?.message||'').startsWith('HTTP '))throw pe;"
        "await W(400);continue;"
        "}"
        "errs=0;"
        "const P=Date.now()-T0<3000?40:220;"
        "if(r.status===204){await W(P);continue;}"
        "const q=await r.json();"
        "if(!q||!q.command){await W(P);continue;}"
        "let out,dispatched=false;"
        "try{"
        "if(q.command.op==='switch'){"
        "const before=await I('get_profiles');"
        "const target=q.command.uid;"
        "const matches=(before?.items||[]).filter(x=>x&&x.uid===target);"
        "if(matches.length!==1)throw Error('profile not found');"
        "dispatched=true;"
        "const outcome=await I('patch_profiles_config',{profiles:{current:target}});"
        "const after=await I('get_profiles');"
        "if((outcome&&typeof outcome==='object'&&'status' in outcome&&outcome.status!=='valid')"
        "||after?.current!==target)throw Error('official transaction not verified');"
        "out={ok:true,dispatched:true,value:{previous:String(before?.current||''),current:String(after.current),name:String(matches[0].name||target)}};"
        "}else if(q.command.op==='detach'){"
        "out={ok:true,dispatched:true,value:{detached:true}};"
        "}else{"
        "throw Error('unsupported operation');"
        "}"
        "}catch(e){"
        "out={ok:false,dispatched,error:String(e?.message||e)};"
        "}"
        "await J('/result','POST',{client:C,id:q.command.id,...out});"
        "if(q.command.op==='detach')stop();"
        "}"
        "}catch(_){stop();}"
        "})();"
        "return 'attached';"
        "})()"
    )


class _PendingCommand:
    def __init__(self, command_id, payload):
        self.id = command_id
        self.payload = payload
        self.event = threading.Event()
        self.picked_up = False
        self.picked_client = None
        self.cancelled = False
        self.result = None


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Suppress tracebacks on stdout/stderr so no headers or tokens ever print.
        return


class RuntimeBroker:
    """Loopback-only HTTP broker mediating switch commands with the Tauri page."""

    def __init__(self, heartbeat_ttl=HEARTBEAT_TTL_SECONDS):
        self._token = secrets.token_urlsafe(32)
        self._heartbeat_ttl = max(2.0, float(heartbeat_ttl))
        self._lock = threading.RLock()
        self._attached_cond = threading.Condition(self._lock)
        self._client_id = None
        self._client_origin = None
        self._last_seen = 0.0
        self._queue = []
        self._inflight = {}
        self._stopped = False
        self._server = None
        self._thread = None
        self._port = 0

    def __repr__(self):
        return f"<RuntimeBroker listening={self.is_running} attached={self.is_attached}>"

    @property
    def port(self):
        return self._port

    @property
    def base_url(self):
        if not self._port:
            raise RuntimeBridgeUnavailable("broker not started")
        return f"http://127.0.0.1:{self._port}"

    @property
    def is_running(self):
        return self._server is not None and not self._stopped

    @property
    def is_attached(self):
        with self._lock:
            return (
                self._client_id is not None
                and not self._stopped
                and (time.monotonic() - self._last_seen) <= self._heartbeat_ttl
            )

    def start(self):
        with self._lock:
            if self._server is not None and not self._stopped:
                return self._port
            self._stopped = False
            broker = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format, *args):
                    return

                def _checked_origin(self):
                    origin = self.headers.get("Origin")
                    if origin is None:
                        return ""
                    if origin in ALLOWED_TAURI_ORIGINS:
                        return origin
                    return None

                def _send(self, status, body=None, origin=""):
                    payload = b"" if body is None else json.dumps(body, ensure_ascii=True).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    if origin:
                        self.send_header("Access-Control-Allow-Origin", origin)
                        self.send_header("Vary", "Origin")
                    if body is not None:
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)

                def _authorized(self):
                    header = self.headers.get("Authorization", "")
                    expected = f"Bearer {broker._token}"
                    return hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))

                def _read_json(self):
                    raw_len = self.headers.get("Content-Length", "0")
                    try:
                        length = int(raw_len)
                    except ValueError:
                        return None
                    if length < 0 or length > MAX_REQUEST_BYTES:
                        return None
                    data = self.rfile.read(length) if length else b"{}"
                    try:
                        parsed = json.loads(data.decode("utf-8", "strict"))
                    except Exception:
                        return None
                    return parsed if isinstance(parsed, dict) else None

                def _drain_body(self):
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        return
                    if 0 < length <= MAX_REQUEST_BYTES:
                        try:
                            self.rfile.read(length)
                        except Exception:
                            pass

                def do_OPTIONS(self):
                    origin = self._checked_origin()
                    if not origin:
                        self._send(403)
                        return
                    self.send_response(204)
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                    self.send_header("Access-Control-Max-Age", "60")
                    self.send_header("Vary", "Origin")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()

                def do_GET(self):
                    origin = self._checked_origin()
                    if origin is None:
                        self._send(403)
                        return
                    if not self._authorized():
                        self._send(401, {"error": "unauthorized"}, origin=origin)
                        return
                    parsed = urlparse(self.path)
                    if parsed.path != "/poll":
                        self._send(404, {"error": "not found"}, origin=origin)
                        return
                    params = parse_qs(parsed.query)
                    client = (params.get("client") or [""])[0]
                    status, body = broker._handle_poll(client)
                    self._send(status, body, origin=origin)

                def do_POST(self):
                    origin = self._checked_origin()
                    if origin is None:
                        self._drain_body()
                        self._send(403)
                        return
                    if not self._authorized():
                        self._drain_body()
                        self._send(401, {"error": "unauthorized"}, origin=origin)
                        return
                    parsed = urlparse(self.path)
                    payload = self._read_json()
                    if payload is None:
                        self._send(400, {"error": "invalid payload"}, origin=origin)
                        return
                    if parsed.path == "/hello":
                        status, body = broker._handle_hello(payload, origin)
                        self._send(status, body, origin=origin)
                        return
                    if parsed.path == "/result":
                        status, body = broker._handle_result(payload)
                        self._send(status, body, origin=origin)
                        return
                    self._send(404, {"error": "not found"}, origin=origin)

            server = _QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server = server
            self._port = int(server.server_address[1])
            self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self._thread.start()
            return self._port

    def make_loader_script(self):
        self.start()
        return make_bridge_loader_script(self.base_url, self._token)

    def _handle_hello(self, payload, header_origin):
        client = payload.get("client")
        origin = payload.get("origin")
        if not isinstance(client, str) or not 4 <= len(client) <= 128:
            return 400, {"error": "invalid client"}
        if not isinstance(origin, str) or origin not in ALLOWED_TAURI_ORIGINS:
            return 403, {"error": "invalid origin"}
        if header_origin and header_origin != origin:
            return 403, {"error": "origin mismatch"}
        with self._lock:
            if self._stopped:
                return 409, {"error": "stopped"}
            self._client_id = client
            self._client_origin = origin
            self._last_seen = time.monotonic()
            self._attached_cond.notify_all()
        return 200, {"ok": True}

    def _handle_poll(self, client):
        with self._lock:
            if self._stopped:
                return 409, {"error": "stopped"}
            if not client or client != self._client_id:
                return 409, {"error": "stale client"}
            self._last_seen = time.monotonic()
            while self._queue:
                cmd = self._queue.pop(0)
                if cmd.cancelled:
                    continue
                cmd.picked_up = True
                cmd.picked_client = client
                self._inflight[cmd.id] = cmd
                return 200, {"command": {"id": cmd.id, **cmd.payload}}
        return 204, None

    def _handle_result(self, payload):
        client = payload.get("client")
        cmd_id = payload.get("id")
        if not isinstance(client, str) or not isinstance(cmd_id, str):
            return 400, {"error": "invalid result"}
        with self._lock:
            if client != self._client_id:
                return 409, {"error": "stale client"}
            self._last_seen = time.monotonic()
            cmd = self._inflight.pop(cmd_id, None)
            if cmd is not None:
                cmd.result = {
                    "ok": bool(payload.get("ok")),
                    "dispatched": bool(payload.get("dispatched", True)),
                    "value": payload.get("value"),
                    "error": payload.get("error"),
                }
                cmd.event.set()
        return 200, {"ok": True}

    def wait_until_attached(self, timeout=8.0, is_cancelled=None):
        deadline = time.monotonic() + max(0.2, float(timeout))
        with self._lock:
            while not self._stopped:
                if is_cancelled is not None and is_cancelled():
                    raise RuntimeSwitchCancelled("cancelled")
                if self._client_id is not None and (time.monotonic() - self._last_seen) <= self._heartbeat_ttl:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._attached_cond.wait(timeout=min(0.15, remaining))
        return False

    def invalidate_client(self):
        with self._lock:
            self._client_id = None
            self._last_seen = 0.0

    def enqueue_switch(self, uid, pickup_timeout=3.0, execution_timeout=25.0, is_cancelled=None):
        if not valid_profile_uid(uid):
            raise RuntimeBrokerError("invalid profile uid")
        cmd = _PendingCommand(uuid.uuid4().hex, {"op": "switch", "uid": uid})
        with self._lock:
            if not self.is_attached:
                raise RuntimeBridgeUnavailable("bridge not attached")
            self._queue.append(cmd)

        pickup_deadline = time.monotonic() + max(0.2, float(pickup_timeout))
        exec_deadline = None
        while True:
            if is_cancelled is not None and is_cancelled():
                with self._lock:
                    cmd.cancelled = True
                    picked = cmd.picked_up
                    if cmd in self._queue:
                        self._queue.remove(cmd)
                if picked:
                    raise RuntimeSwitchOutcomeUnknown("switch cancelled after dispatch")
                raise RuntimeSwitchCancelled("cancelled")

            if cmd.event.wait(timeout=0.05):
                break

            now = time.monotonic()
            with self._lock:
                if self._stopped:
                    if cmd.picked_up:
                        raise RuntimeSwitchOutcomeUnknown("broker stopped after dispatch")
                    raise RuntimeBridgeUnavailable("broker stopped")
                if not cmd.picked_up:
                    if now >= pickup_deadline:
                        cmd.cancelled = True
                        if cmd in self._queue:
                            self._queue.remove(cmd)
                        self._client_id = None
                        self._last_seen = 0.0
                        raise RuntimeBridgeUnavailable("bridge did not pick up command")
                else:
                    if exec_deadline is None:
                        exec_deadline = now + max(1.0, float(execution_timeout))
                    elif now >= exec_deadline:
                        self._inflight.pop(cmd.id, None)
                        raise RuntimeSwitchOutcomeUnknown("switch timed out after dispatch")

        res = cmd.result
        if not isinstance(res, dict):
            raise RuntimeSwitchOutcomeUnknown("invalid bridge result")
        if not res.get("ok"):
            err = str(res.get("error") or "switch failed")
            if not res.get("dispatched") and err in ("profile not found", "unsupported operation"):
                raise RuntimeBrokerError(err)
            raise RuntimeSwitchOutcomeUnknown("switch outcome unknown")
        val = res.get("value")
        if (
            not isinstance(val, dict)
            or not isinstance(val.get("previous"), str)
            or val.get("current") != uid
            or not isinstance(val.get("name"), str)
        ):
            raise RuntimeSwitchOutcomeUnknown("unverified bridge result")
        return RuntimeProfileSwitch(previous=val["previous"], current=val["current"], name=val["name"])

    def stop(self, detach_timeout=0.6):
        cmd = None
        with self._lock:
            if self._stopped:
                return
            if self.is_attached:
                cmd = _PendingCommand(uuid.uuid4().hex, {"op": "detach"})
                self._queue.append(cmd)
        if cmd is not None:
            cmd.event.wait(timeout=max(0.05, float(detach_timeout)))
        with self._lock:
            self._stopped = True
            server = self._server
            self._server = None
            self._port = 0
            self._client_id = None
            self._last_seen = 0.0
            for pending in self._queue:
                pending.cancelled = True
                pending.event.set()
            self._queue.clear()
            for inflight in self._inflight.values():
                inflight.event.set()
            self._inflight.clear()
            self._attached_cond.notify_all()
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
