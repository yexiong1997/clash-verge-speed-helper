#!/usr/bin/env python3
"""Synthetic localhost-only integrity test; no user app/config/node is read.

Usage: python3 Worker/self-test.py build/verified-worker/clash-speedtest
On macOS the worker runs under sandbox-exec with outbound network restricted
solely to loopback. The in-memory HTTP CONNECT proxy additionally rejects all
non-fixture destinations. The configuration is passed only through stdin.
"""
import http.server
import json
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

SIZE = 64
SECRET = "fixture-private-credential-never-print"


class Target(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    redirected = 0
    mixed_gets = 0
    mixed_lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/forbidden-redirect")
        elif self.path == "/bad-head":
            self.send_response(503)
        else:
            if self.path == "/forbidden-redirect":
                type(self).redirected += 1
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/forbidden-redirect")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/forbidden-redirect":
            type(self).redirected += 1
        if self.path == "/bad-status":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/bad-range":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 1-{SIZE}/{SIZE + 1}")
        elif self.path == "/good-range":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-{SIZE - 1}/{SIZE}")
        else:
            self.send_response(200)
        length = SIZE + 1 if self.path == "/oversized" else SIZE
        if self.path != "/no-eof":
            self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            if self.path in ("/timeout", "/no-eof"):
                self.wfile.write(b"x" * (SIZE if self.path == "/no-eof" else 1))
                self.wfile.flush()
                time.sleep(1)
                self.close_connection = True
            else:
                mixed_short = False
                if self.path == "/mixed":
                    with type(self).mixed_lock:
                        type(self).mixed_gets += 1
                        mixed_short = type(self).mixed_gets == 1
                body_len = length - 1 if self.path == "/truncated" or mixed_short else length
                self.wfile.write(b"x" * body_len)
                self.wfile.flush()
                if self.path == "/truncated" or mixed_short:
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass


class TargetServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            line = self.rfile.readline(8193).decode("ascii", "strict").strip()
            parts = line.split()
            if len(parts) != 3 or parts[0] != "CONNECT":
                return
            while True:
                header = self.rfile.readline(8193)
                if header in (b"\r\n", b"\n", b""):
                    break
                if len(header) > 8192:
                    return
            if parts[1] != f"127.0.0.1:{self.server.target_port}":
                return
            with socket.create_connection(("127.0.0.1", self.server.target_port), timeout=3) as remote:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                peers = (self.connection, remote)
                while True:
                    ready, _, _ = select.select(peers, [], [], 3)
                    if not ready:
                        return
                    for src in ready:
                        chunk = src.recv(65536)
                        if not chunk:
                            return
                        (remote if src is self.connection else self.connection).sendall(chunk)
        except (OSError, UnicodeError, ValueError):
            return


class ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def sandbox_command(worker, *, deny_network=False):
    if sys.platform != "darwin":
        raise RuntimeError("macOS sandbox-exec is required for this self-test")
    if not os.path.exists("/usr/bin/sandbox-exec"):
        raise RuntimeError("sandbox-exec unavailable; refusing an unsandboxed self-test")
    profile = '''(version 1)
(allow default)
(deny file-write*)
(allow file-write* (literal "/dev/null"))
(deny network-outbound)
'''
    if not deny_network:
        profile += '(allow network-outbound (remote ip "localhost:*"))\n'
    return ["/usr/bin/sandbox-exec", "-p", profile, str(worker)]


def verify_network_denied(command, proxy_port):
    # Refuse to run unsafe fixture cases unless both TCP and UDP result in a
    # sandbox permission error, not merely a connection-refused result.
    probe = r'''
import errno, socket, sys

def denied(fn):
    try:
        fn()
    except OSError as e:
        if e.errno in (errno.EPERM, errno.EACCES):
            return
        raise
    raise RuntimeError("network deny canary failed")

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    denied(lambda: s.connect(("127.0.0.1", int(sys.argv[1]))))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    denied(lambda: s.sendto(b"x", ("127.0.0.1", int(sys.argv[1]))))
'''
    result = subprocess.run(command[:-1] + [sys.executable, "-B", "-c", probe, str(proxy_port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"HOME": "/dev/null"}, timeout=10)
    if result.returncode != 0 or result.stdout or result.stderr:
        raise RuntimeError("TCP/UDP deny-network canary failed; refusing unsafe fixtures")


def run_case(command, proxy_port, target_port, path, *, name=r"\Asynthetic-node\z", config=None):
    if config is None:
        config = ("proxies:\n"
                  "  - name: synthetic-node\n"
                  "    type: http\n"
                  "    server: 127.0.0.1\n"
                  f"    port: {proxy_port}\n"
                  "    username: synthetic-user\n"
                  f"    password: {SECRET}\n")
    args = command + ["-c", "/dev/stdin", "-f", name,
                      "-server-url", f"http://127.0.0.1:{target_port}{path}",
                      "-download-size", str(SIZE * 2), "-concurrent", "2",
                      "-timeout", "300ms", "-max-latency", "500ms",
                      "-rename=false", "-json-v1"]
    env = {"HOME": "/dev/null", "XDG_CONFIG_HOME": "/dev/null", "TMPDIR": "/dev/null"}
    p = subprocess.run(args, input=config.encode(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, timeout=12)
    assert SECRET.encode() not in p.stdout + p.stderr
    assert b"127.0.0.1" not in p.stdout + p.stderr
    return p


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 Worker/self-test.py <worker>")
    worker = Path(sys.argv[1]).resolve(strict=True)
    if not worker.is_file() or not os.access(worker, os.X_OK):
        raise RuntimeError("worker is not an executable regular file")
    command = sandbox_command(worker)
    with TargetServer(("127.0.0.1", 0), Target) as target:
        with ProxyServer(("127.0.0.1", 0), Proxy) as proxy:
            proxy.target_port = target.server_address[1]
            for server in (target, proxy):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            port = proxy.server_address[1]
            target_port = target.server_address[1]
            try:
                expected = {
                    "/good": (6, True, SIZE * 2),
                    "/good-range": (6, True, SIZE * 2),
                    "/bad-head": (0, True, SIZE * 2),
                    "/redirect": (0, False, 0),
                    "/bad-status": (6, False, 0),
                    "/bad-range": (6, False, 0),
                    "/truncated": (6, False, (SIZE - 1) * 2),
                    "/mixed": (6, False, SIZE * 2 - 1),
                    "/oversized": (6, False, (SIZE + 1) * 2),
                    "/timeout": (6, False, 2),
                    "/no-eof": (6, False, SIZE * 2),
                }
                for path, (heads, complete, received) in expected.items():
                    p = run_case(command, port, target_port, path)
                    assert p.returncode == 0, f"{path}: exit {p.returncode}; stderr={p.stderr!r}"
                    assert p.stderr == b"", f"{path}: unexpected stderr"
                    assert p.stdout.endswith(b"\n") and p.stdout.count(b"\n") == 1
                    obj = json.loads(p.stdout)
                    assert set(obj) == {"version", "node_name", "node_type", "head_attempts", "head_successes",
                                        "latency_ms", "jitter_ms", "expected_bytes", "received_bytes",
                                        "download_complete", "download_bytes_per_second"}
                    assert obj["version"] == 1 and obj["node_name"] == "synthetic-node"
                    assert obj["head_attempts"] == 6 and obj["head_successes"] == heads, (path, obj)
                    assert obj["expected_bytes"] == SIZE * 2 and obj["received_bytes"] == received, (path, obj)
                    assert obj["download_complete"] is complete, (path, obj)
                    assert (obj["download_bytes_per_second"] > 0) == complete, (path, obj)
                    print(f"PASS {path}")
                assert Target.redirected == 0, "a redirect was followed"
                for regex, config in ((r"\Amissing\z", None), ("[", None),
                                      (r"\Asynthetic-node\z", "not: [valid\n" + SECRET)):
                    p = run_case(command, port, target_port, "/good", name=regex, config=config)
                    assert p.returncode != 0 and p.stdout == b"" and p.stderr == b""
                print("PASS empty selection, invalid regex, and invalid config")
                node = ("proxies:\n  - name: synthetic-node\n    type: http\n"
                        "    server: 127.0.0.1\n    port: {}\n"
                        "    username: synthetic-user\n    password: " + SECRET + "\n")
                for invalid in ("-1", "0", "65536", "1.5", '"-1"', "null"):
                    p = run_case(command, port, target_port, "/good", config=node.format(invalid))
                    assert p.returncode != 0 and p.stdout == b"" and p.stderr == b"", invalid
                p = run_case(command, port, target_port, "/good", config=node.format(f'"{port}"'))
                assert p.returncode != 0 and p.stdout == b"" and p.stderr == b""
                print("PASS invalid node ports, including quoted integers")
                deny_command = sandbox_command(worker, deny_network=True)
                verify_network_denied(deny_command, port)
                common = {"name": "synthetic-node", "server": "127.0.0.1", "port": port}
                http = dict(common, type="http")
                unsafe = [
                    dict(common, type="ssh", username="synthetic-user", password=SECRET),
                    dict(common, type="wireguard", workers=2),
                    dict(http, headers={"X\r\nInjected": "x"}),
                    dict(http, HEADERS={"X": "x\r\nInjected: y"}),
                    dict(http, **{"headerſ": {"X": "x"}}),
                    dict(http, headers={}),
                    dict(http, headers=None),
                ]
                for item in unsafe:
                    config = json.dumps({"proxies": [item]})
                    p = run_case(deny_command, port, target_port, "/good", config=config)
                    assert p.returncode != 0 and p.stdout == b"" and p.stderr == b""
                print("PASS deny-network SSH, WireGuard, and custom HTTP header rejection")
            finally:
                proxy.shutdown()
                target.shutdown()


if __name__ == "__main__":
    main()
