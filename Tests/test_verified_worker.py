#!/usr/bin/env python3
"""Fail-closed, synthetic macOS integration tests for the verified worker.

    python3 -B Tests/test_verified_worker.py

Build first with Scripts/build-verified-worker.sh. No installed app, user
profile, controller or live node is read or contacted. A synthetic HTTP proxy
forwards only to a fixture download server. The worker can connect to that
proxy only inside a deny-by-default sandbox-exec profile. A separate Unix
socket and denied TCP/Unix/file canaries verify the active sandbox policy
before the worker is run. All fixture files are private and under build/.
"""
import base64
import json
import math
import os
from pathlib import Path
import pwd
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "build"
WORKER = BUILD / "verified-worker" / "clash-speedtest"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
NAME = "VisibleNode"
OTHER = "FilteredNode"
USER = "SYNTH_PRIVATE_USER_97"
PASSWORD = "SYNTH_PRIVATE_PASSWORD_83"
AUTH = base64.b64encode((USER + ":" + PASSWORD).encode("ascii")).decode("ascii")
# Every stream is larger than 256 KiB. Equal chunks make their sizes observable
# through the direct-download Range header without depending on scheduling.
STREAMS = 3
CHUNK = 320 * 1024 + 13
TOTAL = STREAMS * CHUNK
KEYS = {
    "version", "node_name", "node_type", "head_attempts", "head_successes",
    "latency_ms", "jitter_ms", "expected_bytes", "received_bytes",
    "download_complete", "download_bytes_per_second",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def quote_sandbox(value):
    value = str(value)
    if not value.isascii() or any(ch in value for ch in ('"', "\\", "\n", "\r")):
        raise RuntimeError("sandbox fixture path cannot be safely quoted")
    return '"' + value + '"'


def sandbox_profile(root, allowed, unix_socket, proxy_port):
    # file-read* has to include the signed macOS loader/system frameworks, then
    # explicitly deny all user homes, the real service, and our private canary
    # siblings. Only this invocation's synthetic directory and Unix socket are
    # re-opened by more specific rules. Nothing gets a general network grant.
    homes = {Path("/Users"), Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}
    if os.environ.get("HOME"):
        homes.add(Path(os.environ["HOME"]).resolve())
    if Path("/") in homes:
        raise RuntimeError("refusing to sandbox with / as a user home")
    for home in homes:
        if root == home:
            raise RuntimeError("fixture directory unexpectedly equals a user home")
    lines = ["(version 1)", "(deny default)", "(allow process*)",
             "(allow sysctl*)", "(allow mach-lookup)", "(allow file-read*)"]
    for path in sorted(homes, key=str):
        lines.append(f"(deny file-read* (subpath {quote_sandbox(path)}))")
    lines += [
        '(deny file-read* (subpath "/var/run/clash-verge-service"))',
        '(deny file-read* (subpath "/private/var/run/clash-verge-service"))',
        f"(deny file-read* (subpath {quote_sandbox(root)}))",
        f"(allow file-read* (subpath {quote_sandbox(allowed)}) "
        f"(literal {quote_sandbox(unix_socket)}))",
        f"(allow file-write* (subpath {quote_sandbox(allowed)}) (literal \"/dev/null\"))",
        f'(allow network-outbound (remote ip "localhost:{proxy_port}"))',
        f"(allow network-outbound (remote unix-socket (path {quote_sandbox(unix_socket)})))",
    ]
    return "\n".join(lines)


def execute(profile, command, allowed, env, **kwargs):
    return subprocess.run([str(SANDBOX_EXEC), "-p", profile, *command], cwd=allowed,
                          env=env, capture_output=True, timeout=25, **kwargs)


def verify_sandbox(profile, root, allowed, unix_socket, denied_unix, proxy_port,
                   denied_tcp_port, env):
    # The denied targets are private, listening local fixtures, so ECONNREFUSED
    # is not accepted as evidence of enforcement. Likewise use an existing
    # sibling file, not a nonexistent path. Require EPERM/EACCES explicitly.
    secret = root / "x"
    secret.write_text("private-canary", encoding="ascii")
    public = allowed / "y"
    public.write_text("allowed-canary", encoding="ascii")
    probe = r'''
import errno, os, socket, sys

def check(fn, expected):
    try:
        fn()
        actual = True
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES):
            raise
        actual = False
    if actual != expected:
        raise RuntimeError("sandbox canary failed")

def conn(family, address):
    with socket.socket(family) as s:
        s.settimeout(2)
        s.connect(address)

def read(path):
    with open(path, 'rb') as f: f.read()

def write(path):
    with open(path, 'wb') as f: f.write(b'canary')

check(lambda: conn(socket.AF_INET, ('127.0.0.1', int(sys.argv[1]))), True)
check(lambda: conn(socket.AF_INET, ('127.0.0.1', int(sys.argv[2]))), False)
check(lambda: conn(socket.AF_UNIX, sys.argv[3]), True)
check(lambda: conn(socket.AF_UNIX, sys.argv[4]), False)
check(lambda: read(sys.argv[5]), True)
check(lambda: read(sys.argv[6]), False)
check(lambda: write(sys.argv[7]), False)
'''
    # sys.executable is the already running interpreter, not the /usr/bin/xcrun
    # stub; this avoids even attempted writes to its shared developer cache.
    python = Path(sys.executable).resolve()
    if not python.is_file() or python.is_relative_to(Path.home()):
        raise RuntimeError("the sandbox canary requires a system Python outside the user home")
    run = execute(profile, [str(python), "-B", "-c", probe, str(proxy_port),
                            str(denied_tcp_port), str(unix_socket), str(denied_unix),
                            str(public), str(secret), str(root / "must-not-write")],
                  allowed, env, text=True)
    require(run.returncode == 0, "sandbox canary failed; refusing to run worker")
    require(not (root / "must-not-write").exists(), "sandbox allowed forbidden write")


class Case:
    def __init__(self, mode):
        self.mode = mode
        self.expected_path = (f"/__down?bytes={CHUNK}" if mode == "server-mode" else "/fixture")
        self.lock = threading.Lock()
        self.heads = 0
        self.gets = 0
        self.ranges = []
        self.redirects_followed = 0
        self.errors = []

    def event(self, method, path, range_header):
        with self.lock:
            if path == "/redirect-target":
                self.redirects_followed += 1
            elif path != self.expected_path:
                self.errors.append("unexpected fixture path")
            if method == "HEAD":
                self.heads += 1
                return self.heads
            self.gets += 1
            self.ranges.append(range_header)
            return self.gets

    def snapshot(self):
        with self.lock:
            return (self.heads, self.gets, list(self.ranges),
                    self.redirects_followed, list(self.errors))


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.case = None


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond_empty(self, status, location=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        self.close_connection = True

    def do_HEAD(self):
        case = self.server.case
        number = case.event("HEAD", self.path, self.headers.get("Range"))
        if self.path == "/redirect-target":
            return self.respond_empty(200)
        if case.mode == "head-error":
            return self.respond_empty(503)
        if case.mode == "head-redirect":
            return self.respond_empty(302, "/redirect-target")
        if case.mode == "one-head" and number > 1:
            return self.respond_empty(503)
        self.respond_empty(200)

    def do_GET(self):
        case = self.server.case
        number = case.event("GET", self.path, self.headers.get("Range"))
        if self.path == "/redirect-target":
            return self.respond_empty(200)
        if case.mode == "get-error" or (case.mode == "one-get-error" and number == 1):
            return self.respond_empty(503)
        if case.mode == "get-redirect":
            return self.respond_empty(302, "/redirect-target")
        if case.mode == "oversize" or (case.mode == "one-oversize" and number == 1):
            # Chunked encoding avoids Content-Length hiding surplus bytes from
            # net/http. This tests the mandatory size+1 read limit.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            # A large surplus distinguishes a true bounded read from merely
            # accepting an unbounded copy and later comparing the byte count.
            body = b"x" * (4 * CHUNK)
            try:
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            self.close_connection = True
            return
        self.send_response(206)
        start = 1 if case.mode == "bad-range" else 0
        self.send_header("Content-Range", f"bytes {start}-{start + CHUNK - 1}/{CHUNK + 100}")
        self.send_header("Content-Length", str(CHUNK))
        self.send_header("Connection", "close")
        self.end_headers()
        length = CHUNK
        if case.mode == "truncated" or (case.mode == "one-truncated" and number == 1):
            length = CHUNK // 2
        elif case.mode == "timeout":
            length = CHUNK // 4
        try:
            self.wfile.write(b"x" * length)
            self.wfile.flush()
            if case.mode == "timeout":
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        self.close_connection = True


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, target_port):
        super().__init__(("127.0.0.1", 0), ProxyHandler)
        self.target_port = target_port
        self.lock = threading.Lock()
        self.connections = 0
        self.errors = []


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        # A sandbox allow canary makes one empty connection; it is not a test
        # event. Never forward any CONNECT except the one exact fixture target.
        self.request.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < 16384:
                part = self.request.recv(4096)
                if not part:
                    return
                data += part
            target = f"CONNECT 127.0.0.1:{self.server.target_port} HTTP/1.1".encode()
            if not data.startswith(target + b"\r\n") or not data.endswith(b"\r\n\r\n"):
                with self.server.lock:
                    self.server.errors.append("unexpected proxy destination or CONNECT framing")
                return
            auth_headers = [line.split(b":", 1)[1].strip() for line in data.split(b"\r\n")[1:]
                            if line.lower().startswith(b"proxy-authorization:")]
            if auth_headers != [b"Basic " + AUTH.encode("ascii")]:
                with self.server.lock:
                    self.server.errors.append("synthetic proxy authentication missing or incorrect")
                return
            with self.server.lock:
                self.server.connections += 1
            with socket.create_connection(("127.0.0.1", self.server.target_port), timeout=5) as upstream:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                sockets = [self.request, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 6)
                    if not ready:
                        return
                    for src in ready:
                        payload = src.recv(65536)
                        if not payload:
                            return
                        (upstream if src is self.request else self.request).sendall(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return  # expected on cancellation, timeout, and limited reads


def start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def make_yaml(primary_port, other_port):
    def node(name, port):
        return ("  - name: " + json.dumps(name) + "\n    type: http\n"
                "    server: 127.0.0.1\n    port: " + str(port) + "\n"
                "    username: " + json.dumps(USER) + "\n"
                "    password: " + json.dumps(PASSWORD) + "\n")
    return "proxies:\n" + node(NAME, primary_port) + node(OTHER, other_port)


def no_leaks(output, secrets):
    for secret in secrets:
        require(secret not in output, "worker emitted a sensitive synthetic value")
    for field in ("proxy_config", "ProxyConfig", "download_error", "DownloadError"):
        require(field not in output, "worker emitted a forbidden config/error field")


def run_case(mode, profile, binary, target, primary, secondary, allowed, env):
    case = Case(mode)
    target.case = case
    primary_port = primary.server_address[1]
    other_port = secondary.server_address[1]
    url = f"http://127.0.0.1:{target.server_address[1]}"
    if mode != "server-mode":
        url += "/fixture"
    config = make_yaml(primary_port, other_port)
    secrets = (USER, PASSWORD, AUTH, "127.0.0.1", url, str(allowed), str(PROJECT))
    command = [str(binary), "-c", "/dev/stdin", "-f", "^" + NAME + "$",
               "-server-url", url, "-download-size", str(TOTAL),
               "-concurrent", str(STREAMS), "-timeout", "1s",
               "-max-latency", "1s", "-rename=false", "-json-v1"]
    with secondary.lock:
        old_other = secondary.connections
    run = execute(profile, command, allowed, env, input=config, text=True)
    no_leaks(run.stdout + run.stderr, secrets)
    require(not run.stderr, mode + ": worker emitted unexpected error output")
    require(run.returncode == 0, mode + ": worker failed; no result claimed")
    lines = run.stdout.splitlines()
    require(len(lines) == 1, mode + ": expected exactly one JSON line")
    try:
        value = json.loads(lines[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, json.JSONDecodeError):
        raise AssertionError(mode + ": invalid JSON") from None
    require(isinstance(value, dict) and set(value) == KEYS, mode + ": unexpected JSON fields")
    require(value["version"] == 1 and type(value["version"]) is int, mode + ": wrong protocol version")
    require(value["node_name"] == NAME and isinstance(value["node_type"], str), mode + ": wrong node")
    for key in ("head_attempts", "head_successes", "expected_bytes", "received_bytes"):
        require(type(value[key]) is int and value[key] >= 0, mode + ": invalid " + key)
    for key in ("latency_ms", "jitter_ms", "download_bytes_per_second"):
        number = value[key]
        require(type(number) in (int, float) and math.isfinite(number) and number >= 0,
                mode + ": invalid numeric " + key)
    require(type(value["download_complete"]) is bool, mode + ": invalid completion flag")
    require(value["head_attempts"] == 6, mode + ": expected six HEAD attempts")
    heads, gets, ranges, redirected, errors = case.snapshot()
    require(heads == 6 and not redirected and not errors, mode + ": fixture observed unexpected HEAD/redirect")
    with secondary.lock:
        require(secondary.connections == old_other, mode + ": filtered node was contacted")
    if mode in ("head-error", "head-redirect"):
        success = 0
    elif mode == "one-head":
        success = 1
    else:
        success = 6
    require(value["head_successes"] == success, mode + ": incorrect HEAD successes")
    # HEAD health and GET completeness are distinct observations. A worker may
    # still perform a GET after every HEAD failed; it must not count a 3xx/5xx
    # HEAD as success or follow redirects in either method.
    require(gets == STREAMS and ranges == [f"bytes=0-{CHUNK - 1}"] * STREAMS,
            mode + ": wrong stream count or per-stream size")
    require(value["expected_bytes"] == TOTAL, mode + ": wrong expected bytes")
    require(value["received_bytes"] <= TOTAL + STREAMS, mode + ": exceeded per-stream size+1")
    if mode in ("complete", "server-mode", "one-head", "head-error", "head-redirect"):
        require(value["download_complete"] and value["received_bytes"] == TOTAL,
                mode + ": complete download rejected or wrong byte count")
        require(value["download_bytes_per_second"] > 0, mode + ": missing valid speed")
    else:
        require(not value["download_complete"] and value["download_bytes_per_second"] == 0,
                mode + ": incomplete/error download accepted")
    if mode == "truncated":
        require(value["received_bytes"] == STREAMS * (CHUNK // 2), mode + ": partial bytes lost")
    if mode == "oversize":
        require(value["received_bytes"] == TOTAL + STREAMS, mode + ": excess byte was not detected")
    if mode == "one-truncated":
        require(value["received_bytes"] == (STREAMS - 1) * CHUNK + CHUNK // 2,
                mode + ": partial bytes lost")
    if mode == "one-get-error":
        require(value["received_bytes"] == (STREAMS - 1) * CHUNK, mode + ": wrong partial byte count")
    if mode == "one-oversize":
        require(value["received_bytes"] == TOTAL + 1, mode + ": excess byte was not detected")
    if mode == "one-head":
        require(value["jitter_ms"] == 0, "zero jitter was not represented numerically")
    print("PASS", mode)


def verify_rejected_inputs(profile, binary, target, primary, secondary, allowed, env):
    url = f"http://127.0.0.1:{target.server_address[1]}/fixture"
    config = make_yaml(primary.server_address[1], secondary.server_address[1])
    args = [str(binary), "-c", "/dev/stdin", "-server-url", url,
            "-download-size", str(TOTAL), "-concurrent", str(STREAMS), "-timeout", "1s",
            "-max-latency", "1s", "-rename=false", "-json-v1"]
    inputs = [("missing", args + ["-f", "^no_such_node$"], config),
              ("multiple", args + ["-f", ".+"], config),
              ("invalid-regex", args + ["-f", "["], config),
              ("invalid-yaml", args + ["-f", "^" + NAME + "$"],
               "proxies: [\n password: " + PASSWORD + "\n server: 127.0.0.1\n"),
              ("invalid-node", args + ["-f", "^" + NAME + "$"],
               "proxies:\n  - {name: " + NAME + ", type: http, server: 127.0.0.1, "
               "port: -1, username: " + USER + ", password: " + PASSWORD + "}\n")]
    for label, command, body in inputs:
        with primary.lock:
            before = primary.connections
        with secondary.lock:
            other_before = secondary.connections
        run = execute(profile, command, allowed, env, input=body, text=True)
        no_leaks(run.stdout + run.stderr, (USER, PASSWORD, AUTH, "127.0.0.1", url, str(PROJECT), str(allowed)))
        # This implementation is intentionally silent on invalid input: it
        # must never pass through YAML/parser/transport error details.
        require(not run.stderr, label + ": worker emitted unexpected error output")
        require(run.returncode != 0 and not run.stdout.strip(), label + ": invalid input produced a result")
        with primary.lock:
            require(before == primary.connections, label + ": invalid input contacted proxy")
        with secondary.lock:
            require(other_before == secondary.connections, label + ": invalid input contacted filtered proxy")
        print("PASS", label)


def verify_endpoint_validation(profile, binary, target, primary, allowed, env):
    # Remove the sole IP grant. The existing deny-by-default profile still
    # permits the fixture Unix socket but denies every TCP and UDP destination.
    grant = f'(allow network-outbound (remote ip "localhost:{primary.server_address[1]}"))\n'
    require(profile.count(grant) == 1, "cannot construct deny-network profile")
    profile = profile.replace(grant, "")
    probe = r"""
import errno, socket, sys

def denied(f):
    try:
        f()
    except OSError as e:
        if e.errno in (errno.EPERM, errno.EACCES):
            return
        raise
    raise RuntimeError('network deny canary failed')

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    denied(lambda: s.connect(('127.0.0.1', int(sys.argv[1]))))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    denied(lambda: s.sendto(b'x', ('127.0.0.1', int(sys.argv[2]))))
"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", 0))
        python = Path(sys.executable).resolve(strict=True)
        run = execute(profile, [str(python), "-B", "-c", probe,
                                str(primary.server_address[1]), str(udp.getsockname()[1])],
                      allowed, env, text=True)
        require(run.returncode == 0 and not run.stdout and not run.stderr,
                "TCP/UDP deny-network canary failed; refusing endpoint tests")

    url = f"http://127.0.0.1:{target.server_address[1]}/fixture"
    args = [str(binary), "-c", "/dev/stdin", "-f", "^" + NAME + "$", "-server-url", url,
            "-download-size", "1", "-concurrent", "1", "-timeout", "30ms",
            "-max-latency", "30ms", "-rename=false", "-json-v1"]
    private = base64.b64encode(bytes(range(1, 33))).decode("ascii")
    public = base64.b64encode(bytes(range(33, 65))).decode("ascii")
    host = "127.0.0.1"
    common = {"name": NAME, "server": host}
    http = dict(common, type="http", port=1, username=USER, password=PASSWORD)
    hy1 = dict(common, type="hysteria", up="10 Mbps", down="10 Mbps", **{"auth-str": PASSWORD})
    hy2 = dict(common, type="hysteria2", password=PASSWORD)
    mieru = dict(common, type="mieru", transport="TCP", username=USER, password=PASSWORD)
    peer = {"server": host, "port": 1, "public-key": public, "allowed-ips": ["0.0.0.0/0"]}
    wg = {"name": NAME, "type": "wireguard", "ip": "10.0.0.2", "private-key": private,
          "peers": [peer]}
    ss = dict(common, type="ss", port=1, cipher="aes-128-gcm", password=PASSWORD)
    vless = dict(common, type="vless", port=1, uuid="00000000-0000-4000-8000-000000000001")
    valid = [("http-low", http), ("http-high", dict(http, port=65535)),
             ("vless-normal", vless), ("ss-normal", ss),
             ("vless-ws-header", dict(vless, network="ws", **{"ws-opts": {"headers": {"Host": "fixture.invalid"}}})),
             ("hysteria-normal", dict(hy1, port=1)),
             ("hysteria2-normal", dict(hy2, port=65535)),
             ("hysteria-low-range", dict(hy1, ports="1-1")),
             ("hysteria-high-range", dict(hy1, ports="65535-65535")),
             ("hysteria-comma", dict(hy1, ports="1,65535")),
             ("hysteria-28", dict(hy1, ports=",".join(["1"] * 28))),
             ("hysteria2-low-range", dict(hy2, ports="1-1")),
             ("hysteria2-high-range", dict(hy2, ports="65535-65535")),
             ("hysteria2-comma", dict(hy2, ports="1-2,65534-65535")),
             ("hysteria2-slash", dict(hy2, ports="1/65535")),
             ("hysteria2-28", dict(hy2, ports=",".join(["1"] * 28))),
             ("hysteria2-folded", dict(hy2, **{"portſ": "1-2"})),
             ("hysteria2-both", dict(hy2, port=1, ports="1-2")),
             ("mieru-low-range", dict(mieru, **{"port-range": "1-1"})),
             ("mieru-high-range", dict(mieru, **{"port-range": "65535-65535"}))]
    # The injected Go constructor canary checks the pre-construction boundary;
    # these end-to-end cases additionally exercise YAML/JSON decoding while
    # TCP and UDP are denied by the independently verified test sandbox.
    ssh = dict(common, type="ssh", port=22, username=USER, password=PASSWORD)
    unsupported = [
        ("ssh-password", ssh),
        ("ssh-inline-key", dict(ssh, **{"private-key": "PRIVATE KEY FIXTURE"})),
        ("wireguard-peer", wg),
        ("wireguard-root", dict(common, type="wireguard", port=1, **{"private-key": private})),
        ("wireguard-unbounded-workers", dict(wg, workers=1000000000)),
        ("http-crlf-name", dict(http, headers={"X\r\nInjected": "value"})),
        ("http-crlf-value", dict(http, headers={"X": "value\r\nInjected: value"})),
        ("http-control-value", dict(http, headers={"X": "bad\x00value"})),
        ("http-benign-header", dict(http, headers={"X-Fixture": "value"})),
        ("http-uppercase-alias", dict(http, HEADERS={"X": "value"})),
        ("http-unicode-alias", dict(http, **{"headerſ": {"X": "value"}})),
        ("http-empty", dict(http, headers={})),
        ("http-null", dict(http, headers=None)),
        ("http-numeric-value", dict(http, headers={"X": 123})),
        ("http-scalar-headers", dict(http, headers="value")),
    ]
    invalid = []
    for index, value in enumerate((-1, 0, 65536, 65537, 18446744073709551616,
                                   "1", "65535", 1.0, True, None)):
        invalid.append(("root-invalid-" + str(index), dict(http, port=value)))
        invalid.append(("peer-invalid-" + str(index), dict(wg, peers=[dict(peer, port=value)])))
    invalid += [("root-missing", {k: v for k, v in http.items() if k != "port"}),
                ("root-alias", dict(http, PORT=65537)),
                ("ports-unicode-alias", dict(hy2, **{"portſ": "1,65537"})),
                ("ports-unicode-duplicate", dict(hy2, ports="1-2", **{"portſ": "1-2"})),
                ("peers-unicode-alias", dict(wg, **{"peerſ": [dict(peer, port=65537)]})),
                ("peer-alias", dict(wg, peers=[dict(peer, PORT=65537)])),
                ("peer-server-alias", dict(wg, peers=[dict(peer, SERVER=host)])),
                ("peer-missing", dict(wg, peers=[{k: v for k, v in peer.items() if k != "port"}])),
                ("peer-second-invalid", dict(wg, peers=[peer, dict(peer, port=65537)])),
                ("peer-first-invalid", dict(wg, peers=[dict(peer, port=-1), peer])),
                ("peer-nested-array", dict(wg, peers=[[peer]])),
                ("peer-null-entry", dict(wg, peers=[None])),
                ("peer-null", dict(wg, peers=None)),
                ("peer-empty", dict(wg, peers=[])),
                ("peer-map", dict(wg, peers=peer)),
                ("peer-dns", dict(wg, dns=[host])),
                ("root-invalid-with-peers", dict(wg, server=host, port=65537)),
                ("root-incomplete-with-peers", dict(wg, port=1)),
                ("deep-array-port", dict(vless, extra={"layers": [[{"PoRt": 65537}]]})),
                ("deep-array-ports", dict(ss, **{"plugin-opts": {"more": [{"ports": "1,65537"}]}})),
                ("unexpected-peers", dict(vless, peers=[peer])),
                ("unexpected-root-port-range", dict(ss, **{"port_range": "1-2"})),
                ("unexpected-endpoint", dict(ss, endpoint="hidden"))]
    invalid_ranges = ["0-1", "1-65536", "1-65537", "65537-65537", "1,65537",
                      "65536,1", "-1,1", "1-", "1,,2", "1,2junk", " 1,2", "[1-2]",
                      "2-1", "*", "443", "1/65537", ",".join(["1"] * 29),
                      None, 1, True, ["1-2"]]
    for typ, base in (("hysteria", hy1), ("hysteria2", hy2)):
        invalid.extend((typ + "-ports-" + str(i), dict(base, ports=value))
                       for i, value in enumerate(invalid_ranges))
    invalid.append(("hysteria-slash", dict(hy1, ports="1/2")))
    for i, value in enumerate(("0-1", "1-65536", "1-65537", "65537-65537", "2-1",
                               "1-2junk", " 1-2", "1", "1,2", None, 1, ["1-2"])):
        invalid.append(("mieru-range-" + str(i), dict(mieru, **{"port-range": value})))
    invalid.append(("mieru-both", dict(mieru, port=1, **{"port-range": "1-2"})))
    invalid.append(("mieru-range-alias", dict(mieru, **{"port-range": "1-2", "PORT_RANGE": "1-65537"})))
    with primary.lock:
        old_connections = primary.connections
    for expected_valid, cases in ((False, invalid + unsupported), (True, valid)):
        for label, node in cases:
            body = json.dumps({"proxies": [node]})
            run = execute(profile, args, allowed, env, input=body, text=True)
            no_leaks(run.stdout + run.stderr, (USER, PASSWORD, private, public, host, url, str(allowed), str(PROJECT)))
            require(not run.stderr, label + ": worker emitted error output")
            if not expected_valid:
                require(run.returncode != 0 and not run.stdout, label + ": unsafe endpoint was accepted")
            else:
                require(run.returncode == 0, label + ": valid endpoint was rejected")
                try:
                    result = json.loads(run.stdout)
                except ValueError:
                    raise AssertionError(label + ": invalid result") from None
                require(result.get("node_name") == NAME and result.get("head_successes") == 0 and
                        not result.get("download_complete"), label + ": unexpected deny-network result")
    # Exact duplicate keys are hidden by permissive YAML/JSON decoding unless
    # the worker performs its own strict parse before adapter construction.
    duplicates = [
        '{"proxies":[{"name":"' + NAME + '","type":"http","server":"' + host + '","port":65537,"port":1}]}',
        '{"proxies":[{"name":"' + NAME + '","type":"http","server":"' + host + '","port":null,"port":1}]}',
    ]
    for body in duplicates:
        run = execute(profile, args, allowed, env, input=body, text=True)
        no_leaks(run.stdout + run.stderr, (host, url, str(allowed), str(PROJECT)))
        require(run.returncode != 0 and not run.stdout and not run.stderr,
                "duplicate endpoint key was accepted or logged")
    with primary.lock:
        require(primary.connections == old_connections, "deny-network endpoint test contacted a proxy")
    print("PASS deny-network preflight:", len(invalid) + len(unsupported), "rejected and", len(valid),
          "accepted; SSH, WireGuard, and custom HTTP headers disabled")


def main():
    if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
        raise RuntimeError("macOS sandbox-exec is required; refusing to run unsandboxed")
    if WORKER.is_symlink() or not WORKER.is_file():
        raise RuntimeError("verified worker missing; run Scripts/build-verified-worker.sh first")
    if BUILD.is_symlink() or not BUILD.is_dir():
        raise RuntimeError("build directory missing or unsafe")
    build = BUILD.resolve(strict=True)
    if build != PROJECT.resolve(strict=True) / "build":
        raise RuntimeError("build path must be the project's own non-symlink directory")
    # Directory names are kept short for Darwin's 104-byte sun_path limit.
    with tempfile.TemporaryDirectory(prefix="t", dir=build) as folder:
        root = Path(folder).resolve(strict=True)
        if root.parent != build or root.stat().st_mode & 0o077:
            raise RuntimeError("fixture directory is not private or escaped build/")
        allowed = root / "a"
        allowed.mkdir(mode=0o700)
        unix_socket, denied_unix = root / "s", root / "d"
        if max(len(os.fsencode(unix_socket)), len(os.fsencode(denied_unix))) >= 104:
            raise RuntimeError("project path too long for a private Darwin Unix socket")
        binary = allowed / "worker"
        shutil.copyfile(WORKER, binary)
        binary.chmod(0o700)
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(allowed),
               "CFFIXED_USER_HOME": str(allowed), "TMPDIR": str(allowed),
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "no_proxy": "*"}
        with socket.socket(socket.AF_UNIX) as local, socket.socket(socket.AF_UNIX) as denied:
            local.bind(str(unix_socket)); local.listen()
            denied.bind(str(denied_unix)); denied.listen()
            target = FixtureServer()
            primary = ProxyServer(target.server_address[1])
            secondary = ProxyServer(target.server_address[1])
            servers = (target, primary, secondary)
            threads = [start(server) for server in servers]
            try:
                profile = sandbox_profile(root, allowed, unix_socket, primary.server_address[1])
                verify_sandbox(profile, root, allowed, unix_socket, denied_unix,
                               primary.server_address[1], target.server_address[1], env)
                print("PASS sandbox: only fixture proxy and Unix socket allowed; TCP/Unix/read/write canaries denied")
                verify_rejected_inputs(profile, binary, target, primary, secondary, allowed, env)
                verify_endpoint_validation(profile, binary, target, primary, allowed, env)
                for mode in ("complete", "server-mode", "truncated", "one-truncated", "timeout", "get-error",
                             "one-get-error", "oversize", "one-oversize", "head-error",
                             "head-redirect", "get-redirect", "bad-range", "one-head"):
                    run_case(mode, profile, binary, target, primary, secondary, allowed, env)
                with primary.lock:
                    require(not primary.errors, "proxy received an unexpected destination")
                with secondary.lock:
                    require(not secondary.errors and secondary.connections == 0, "filtered proxy was contacted")
                print("PASS synthetic-only verified worker integration")
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=3)


if __name__ == "__main__":
    main()
