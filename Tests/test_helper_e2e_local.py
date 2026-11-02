#!/usr/bin/env python3
"""End-to-end automatic-switch test, with an instrumented *temporary* app.

Run from the project root on macOS after Scripts/fetch-worker.sh:
    python3 -B Tests/test_helper_e2e_local.py
No installed/desktop app is run. The temporary child and all its descendants
are additionally restricted by macOS sandbox-exec to this fixture's exact
loopback ports and Unix socket, synthetic HOME, and private temp directory.
The test fails closed if the source instrumentation anchors no longer match.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
FAST = PROJECT / "Sources" / "main.swift"
CONTROLLER = PROJECT / "Sources" / "SwitchController.swift"
SPEEDTEST = PROJECT / "build" / "deps" / "clash-speedtest"
NODE_SLOW = "E2E-Slow"
NODE_FAST = "E2E-Fast"
NODE_META = "E2E-剩余流量"
GROUP = "E2E-Select"
NODES = (NODE_SLOW, NODE_FAST, NODE_META)
MEMBERS = ["E2E-Before", "E2E-Changed", *NODES]

# Appended only to the throwaway copy, in the same Swift file so that Swift's
# private access control permits observation and invocation of real UI flows.
DRIVER = r'''
#if DEBUG_LOCAL_TEST
private extension AppDelegate {
    func e2eLocalTest() -> Int32 {
        let args = CommandLine.arguments
        guard args.count == 3, ["normal", "group-changed", "profile-changed", "pre-put-profile-changed", "cancel"].contains(args[2]) else {
            print("E2E invalid arguments"); return 1
        }
        let scenario = args[2]
        func waitFor(_ seconds: TimeInterval, _ condition: () -> Bool) -> Bool {
            let deadline = Date().addingTimeInterval(seconds)
            while !condition() && Date() < deadline {
                _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            return condition()
        }
        func fail(_ reason: String) -> Int32 {
            print("E2E \(scenario) FAILED: \(reason); status=\(statusLabel.stringValue); profile=\(profileLabel.stringValue); groups=\(groupPopup.titleOfSelectedItem ?? "nil")")
            return 1
        }
        buildWindow(); useCurrentProfile(nil)
        guard waitFor(15, { !self.groups.isEmpty }) else { return fail("group refresh timeout") }
        guard profileCount == 3, startButton.isEnabled, switchCheck.state == .on else { return fail("profile/UI setup") }
        guard groups.map(\.name) == ["E2E-Select", "GLOBAL"] else { return fail("metadata-only group filtering") }
        let selected = groupPopup.indexOfSelectedItem - 1
        guard selected >= 0, selected < groups.count, groups[selected].name == "E2E-Select" else {
            return fail("unique non-GLOBAL group was not suggested")
        }
        start(nil) // The real Start path, including group preflight and BatchRunner.
        guard waitFor(15, { self.runner != nil || (!self.busy && self.runToken == nil) }), runner != nil else {
            return fail("real batch did not start")
        }
        if scenario == "profile-changed" {
            guard let path = profilePath, let handle = FileHandle(forWritingAtPath: path) else {
                return fail("cannot modify synthetic profile")
            }
            handle.seekToEndOfFile(); handle.write(Data("\n# synthetic change\n".utf8)); handle.closeFile()
        }
        if scenario == "cancel" {
            // The mock sets this marker only after all workers reach local proxies.
            guard let ready = ProcessInfo.processInfo.environment["E2E_READY_MARKER"],
                  waitFor(10, { FileManager.default.fileExists(atPath: ready) }) else {
                self.runner?.cancel(); return fail("local workers did not start")
            }
            stop(nil) // Real Stop -> BatchRunner.cancel -> real finished(cancelled:).
        }
        guard waitFor(35, { !self.busy && self.runner == nil && self.runToken == nil }) else {
            self.runner?.cancel(); return fail("finish/switch timeout")
        }
        if scenario == "cancel" {
            guard statusLabel.stringValue.contains("已停止") else { return fail("cancel finish not observed") }
        } else {
            guard rows.count == 3, rows.allSatisfy({ $0.speedValue > 0 }) else { return fail("missing valid mock rows") }
            guard rows[0].name == "E2E-剩余流量",
                  let best = rows.first(where: { !isInformationalNodeName($0.name) }), best.name == "E2E-Fast" else {
                return fail("unexpected measured ranking or metadata filtering")
            }
            if scenario == "normal" && !statusLabel.stringValue.contains("已将") {
                return fail("app did not report verified switch")
            }
            if scenario == "pre-put-profile-changed" && !statusLabel.stringValue.contains("未切换") {
                return fail("late profile change was not rejected")
            }
            print("E2E \(scenario) rows: " + rows.map { "\($0.name)=\($0.speed)" }.joined(separator: ", "))
        }
        print("E2E \(scenario) AppDelegate finish: OK; " + statusLabel.stringValue)
        return 0
    }
}
#endif
'''


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise RuntimeError(f"unsafe/changed Swift instrumentation anchor: {label}")
    return text.replace(old, new, 1)


def swift_literal(text):
    # All injected paths/URLs use ASCII generated by this fixture.
    assert text.isascii() and "\\" not in text and '"' not in text
    return '"' + text + '"'


def instrument(source, config_root, controller_sock, target_url):
    source = replace_once(source,
        "root = File.join(Dir.home, 'Library', 'Application Support', 'io.github.clash-verge-rev.clash-verge-rev')",
        "root = " + json.dumps(str(config_root)), "Ruby profile root")
    source = replace_once(source,
        "rescue StandardError\n  puts JSON.generate({error: true})",
        'rescue StandardError => e\n  warn "synthetic fixture error: #{e.class}: #{e.message}"\n  puts JSON.generate({error: true})',
        "temporary synthetic Ruby diagnostics")
    source = replace_once(source,
        'let output = Pipe(); p.standardOutput = output; p.standardError = FileHandle.nullDevice',
        'let output = Pipe(); p.standardOutput = output; p.standardError = FileHandle.standardError',
        "temporary synthetic inspector diagnostics")
    old_socket = ('    var paths = ["/var/run/clash-verge-service/users/\\(getuid())/verge-mihomo.sock"]\n'
                  '    if let info = inspect(["socket"]), let path = info["socket"] as? String { paths.append(path) }')
    new_socket = ('    var paths: [String] = []\n'
                  '    if let info = inspect(["socket"]), let path = info["socket"] as? String,\n'
                  '       path == ' + swift_literal(str(controller_sock)) + ' { paths.append(path) }')
    source = replace_once(source, old_socket, new_socket, "service socket removal and exact socket allowlist")
    if "/var/run/clash-verge-service" in source or "Dir.home" in source:
        raise RuntimeError("unsafe profile/socket lookup remains in instrumented copy")
    source = replace_once(source,
        'let target = serverURL ?? "https://speed.cloudflare.com/__down?bytes=\\(size / streams)"',
        'let target = ' + swift_literal(target_url), "force loopback speed target")
    source = replace_once(source,
        'alert.addButton(withTitle: "开始测速"); alert.addButton(withTitle: "取消"); guard alert.runModal() == .alertFirstButtonReturn else { return }',
        'alert.addButton(withTitle: "开始测速"); alert.addButton(withTitle: "取消") // local fixture consent only',
        "test-only consent bypass")
    source = replace_once(source,
        '#if DEBUG_LOCAL_TEST\n// Only present in explicitly compiled developer test builds; never shipped.',
        DRIVER + '\n#if DEBUG_LOCAL_TEST\n// Only present in explicitly compiled developer test builds; never shipped.',
        "private driver placement")
    source = replace_once(source,
        'if CommandLine.arguments.contains("--local-test") { exit(localTest()) }',
        'if CommandLine.arguments.contains("--local-test") { exit(delegate.e2eLocalTest()) }',
        "debug-only local test entry")
    source = replace_once(source,
        'if CommandLine.arguments.contains("--self-test") { exit(delegate.selfTest()) }',
        '// self-test disabled in the isolated fixture bundle', "block non-fixture test entry")
    source = replace_once(source,
        'app.delegate = delegate; app.setActivationPolicy(.regular); app.run()',
        'fatalError("This temporary instrumented bundle may only run --local-test")',
        "block normal instrumented app launch")
    return source


class Case:
    def __init__(self, name, ready_marker, profile_path):
        self.name = name
        self.ready_marker = ready_marker
        self.profile_path = profile_path
        self.lock = threading.Lock()
        self.events = []
        self.errors = []
        self.tunnels = {name: 0 for name in NODES}
        self.heads = {name: 0 for name in NODES}
        self.gets = {name: 0 for name in NODES}
        self.current = "E2E-Before"
        self.changed = False

    def error(self, text):
        with self.lock:
            self.errors.append(text)


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyServer(ThreadingTCPServer):
    def __init__(self, node, delay, target_port):
        self.node, self.delay, self.target_port = node, delay, target_port
        self.case = None
        super().__init__(("127.0.0.1", 0), ProxyHandler)


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(6)
        case = self.server.case
        try:
            first = self.rfile.readline(8192)
            if not first:  # sandbox probe
                return
            while self.rfile.readline(8192).strip():
                pass
            expected = f"CONNECT 127.0.0.1:{self.server.target_port} ".encode()
            if not first.startswith(expected):
                case.error("proxy received an unexpected nonlocal CONNECT target")
                return
            with case.lock:
                case.tunnels[self.server.node] += 1
                if all(case.tunnels.values()):
                    case.ready_marker.touch()
                if case.name == "group-changed" and not case.changed:
                    case.current = "E2E-Changed"
                    case.changed = True
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            request = self.rfile.readline(8192)
            while self.rfile.readline(8192).strip():
                pass
            if case.name == "cancel":
                time.sleep(1)
            if request.startswith(b"HEAD /fixture "):
                with case.lock:
                    case.heads[self.server.node] += 1
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            elif request.startswith(b"GET /fixture "):
                with case.lock:
                    case.gets[self.server.node] += 1
                size = 512 * 1024
                self.wfile.write(f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n".encode())
                for _ in range(size // 32768):
                    self.wfile.write(b"x" * 32768)
                    self.wfile.flush()
                    if self.server.delay:
                        time.sleep(self.server.delay)
            else:
                case.error("proxy received an unexpected synthetic HTTP request")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # Cancellation deliberately closes active local connections.


class TargetHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        data = b"x" * (512 * 1024)
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


class UnixController(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    def __init__(self, path):
        self.case = None
        super().__init__(str(path), ControllerHandler)


class ControllerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, payload=None):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        case = self.server.case
        with case.lock:
            case.events.append(("GET", self.path, case.current))
            current = case.current
        group = dict(name=GROUP, type="Selector", now=current, all=MEMBERS)
        if self.path == "/proxies":
            global_group = dict(name="GLOBAL", type="Selector", now=current, all=MEMBERS)
            metadata_only = dict(name="E2E-MetadataOnly", type="Selector", now=NODE_META, all=[NODE_META])
            auto = dict(name="E2E-Auto", type="URLTest", now=current, all=MEMBERS)
            self.reply(200, {"proxies": {GROUP: group, "GLOBAL": global_group,
                                         "E2E-MetadataOnly": metadata_only, "E2E-Auto": auto}})
        elif self.path == "/proxies/" + urllib.parse.quote(GROUP, safe="-._~"):
            # The second group GET is the one issued by switchAndVerify after
            # finished() has checked the unchanged profile. Change only the
            # synthetic file before returning that GET to exercise prePutGuard.
            with case.lock:
                group_gets = sum(event[:2] == ("GET", self.path) for event in case.events)
                if case.name == "pre-put-profile-changed" and group_gets == 2 and not case.changed:
                    with case.profile_path.open("a", encoding="utf-8") as profile:
                        profile.write("\n# synthetic pre-PUT change\n")
                    case.changed = True
            self.reply(200, group)
        else:
            case.error("unexpected controller GET route")
            self.reply(404)

    def do_PUT(self):
        case = self.server.case
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, json.JSONDecodeError):
            case.error("malformed controller PUT body"); self.reply(400); return
        with case.lock:
            case.events.append(("PUT", self.path, body))
            if self.path != "/proxies/" + GROUP or body != {"name": NODE_FAST}:
                case.errors.append("unexpected controller PUT route or node")
            else:
                case.current = body["name"]
        self.reply(204)


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def quote_sandbox(path):
    value = str(path)
    if not value.isascii() or '"' in value or "\\" in value:
        raise RuntimeError("unsupported path in sandbox policy")
    return '"' + value + '"'


def sandbox_profile(root, socket_path, ports, real_homes):
    # Canonical paths matter for the Darwin Unix-socket filter.
    lines = ['(version 1)', '(allow default)', '(deny network-outbound)',
             '(deny file-write*)', f'(allow file-write* (subpath {quote_sandbox(root)}))',
             '(allow file-write* (literal "/dev/null"))',
             '(deny file-read* (subpath "/Users"))']
    for real_home in real_homes:
        if real_home == Path("/"):
            raise RuntimeError("cannot safely isolate a root-directory home")
        if str(root).startswith(str(real_home) + "/"):
            # An unusual HOME could contain /private/tmp. In that case deny the
            # actual Clash profile tree rather than denying our own temp root.
            denied = real_home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
            if root == denied or str(root).startswith(str(denied) + "/"):
                raise RuntimeError("temporary directory overlaps the real Clash profile tree")
        else:
            denied = real_home
        lines.append(f'(deny file-read* (subpath {quote_sandbox(denied)}))')
    lines.append(f'(allow network-outbound (remote unix-socket (path {quote_sandbox(socket_path)})))')
    lines += [f'(allow network-outbound (remote ip "localhost:{port}"))' for port in ports]
    return "\n".join(lines)


def verify_sandbox(root, profile, socket_path, allowed_port, env):
    # Two private canaries prove that same-host TCP and Unix endpoints not in
    # the allowlist are denied, without ever probing an external address.
    with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
        tcp.bind(("127.0.0.1", 0)); tcp.listen()
        denied_sock = root / "denied.sock"
        unix.bind(str(denied_sock)); unix.listen()
        code = r'''import errno, socket, sys
for kind, address, expect in [
    (socket.AF_INET, ("127.0.0.1", int(sys.argv[1])), True),
    (socket.AF_INET, ("127.0.0.1", int(sys.argv[2])), False),
    (socket.AF_UNIX, sys.argv[3], True), (socket.AF_UNIX, sys.argv[4], False)]:
    s = socket.socket(kind); s.settimeout(3)
    try:
        s.connect(address); allowed = True
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES): raise
        allowed = False
    finally: s.close()
    if allowed != expect: raise SystemExit("sandbox network allowlist check failed")
'''
        subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/python3", "-c", code,
                        str(allowed_port), str(tcp.getsockname()[1]), str(socket_path), str(denied_sock)],
                       cwd=root, env=env, check=True, capture_output=True, timeout=15)


def main():
    if not __debug__:
        raise RuntimeError("Python assertions must be enabled; do not use -O")
    if not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError("macOS sandbox-exec is required; refusing to run unsandboxed")
    if SPEEDTEST.is_symlink() or not SPEEDTEST.is_file():
        raise RuntimeError("worker missing or unsafe; first run Scripts/fetch-worker.sh")
    original_fast = FAST.read_bytes()
    original_controller = CONTROLLER.read_bytes()
    with tempfile.TemporaryDirectory(prefix="clash-e2e-", dir="/private/tmp") as folder:
        root = Path(folder).resolve()
        if root.stat().st_mode & 0o077:
            raise RuntimeError("temporary directory is not private")
        home = root / "home"; home.mkdir(mode=0o700)
        tmp = root / "tmp"; tmp.mkdir(mode=0o700)
        config_root = home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
        (config_root / "profiles").mkdir(parents=True, mode=0o700)
        profile_path = config_root / "profiles/e2e.yaml"
        socket_path = root / "controller.sock"
        if len(os.fsencode(socket_path)) >= 100:
            raise RuntimeError("temporary Unix socket path too long")
        target = ThreadingTCPServer(("127.0.0.1", 0), TargetHandler)
        controller = UnixController(socket_path)
        target_port = target.server_address[1]
        proxies = [ProxyServer(NODE_SLOW, 0.04, target_port),
                   ProxyServer(NODE_FAST, 0.01, target_port),
                   ProxyServer(NODE_META, 0.0, target_port)]
        servers = [target, controller, *proxies]
        threads = [serve(server) for server in servers]
        try:
            real_homes = {Path(os.environ.get("HOME", "/nonexistent")).resolve(),
                          Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}
            ports = [target_port] + [s.server_address[1] for s in proxies]
            sandbox = sandbox_profile(root, socket_path, ports, real_homes)
            env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
                   "CFFIXED_USER_HOME": str(home), "CURL_HOME": str(home), "TMPDIR": str(tmp),
                   "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "NO_PROXY": "*", "no_proxy": "*"}
            verify_sandbox(root, sandbox, socket_path, proxies[0].server_address[1], env)
            print("Sandbox allowlist: exact fixture ports and Unix socket; other local canaries denied")
            fake_yaml = "proxies:\n" + "".join(
                '  - {name: %s, type: http, server: 127.0.0.1, port: %d}\n' %
                (json.dumps(server.node, ensure_ascii=False), server.server_address[1]) for server in proxies)
            (config_root / "profiles.yaml").write_text(
                'current: e2e\nitems:\n  - {uid: e2e, file: e2e.yaml, name: E2E-Synthetic}\n', encoding="utf-8")
            (config_root / "clash-verge.yaml").write_text(
                'external-controller-unix: ' + json.dumps(str(socket_path)) + '\ntun: {enable: false}\n', encoding="utf-8")
            source = instrument(original_fast.decode("utf-8"), config_root, socket_path,
                                f"http://127.0.0.1:{target_port}/fixture")
            main_swift = root / "main.swift"; main_swift.write_text(source, encoding="utf-8")
            controller_swift = root / "SwitchController.swift"; controller_swift.write_bytes(original_controller)
            app = root / "Fixture.app/Contents"
            binary = app / "MacOS/E2EHelper"
            binary.parent.mkdir(parents=True, mode=0o700)
            resources = app / "Resources"; resources.mkdir(mode=0o700)
            shutil.copyfile(SPEEDTEST, resources / "clash-speedtest")
            (resources / "clash-speedtest").chmod(0o700)
            with (app / "Info.plist").open("wb") as output:
                plistlib.dump({"CFBundleExecutable": "E2EHelper", "CFBundleIdentifier": "local.synthetic.clash-e2e",
                               "CFBundlePackageType": "APPL", "LSUIElement": True}, output)
            cmd = ["/usr/bin/sandbox-exec", "-p", sandbox, "/usr/bin/swiftc", "-D", "DEBUG_LOCAL_TEST",
                   "-module-cache-path", str(root / "module-cache"), str(main_swift), str(controller_swift), "-o", str(binary)]
            build = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=120)
            if build.returncode:
                raise RuntimeError("temporary swiftc build failed:\n" + build.stderr[-5000:])
            for name in ("normal", "group-changed", "profile-changed", "pre-put-profile-changed", "cancel"):
                profile_path.write_text(fake_yaml, encoding="utf-8")
                ready_marker = root / ("ready-" + name)
                case = Case(name, ready_marker, profile_path)
                controller.case = case
                for proxy in proxies:
                    proxy.case = case
                run_env = {**env, "E2E_READY_MARKER": str(ready_marker)}
                run = subprocess.run(["/usr/bin/sandbox-exec", "-p", sandbox, str(binary), "--local-test", name],
                                     cwd=root, env=run_env, capture_output=True, text=True, timeout=60)
                print(run.stdout.strip())
                if run.returncode:
                    raise RuntimeError(f"instrumented app failed in {name}: {run.returncode}\n{run.stderr[-4000:]}")
                with case.lock:
                    events, errors = list(case.events), list(case.errors)
                    tunnels, heads, gets = dict(case.tunnels), dict(case.heads), dict(case.gets)
                    current, changed = case.current, case.changed
                if errors:
                    raise AssertionError(f"{name}: local fixture errors: {errors}")
                puts = [event for event in events if event[0] == "PUT"]
                if name == "normal":
                    expected = [("GET", "/proxies"), ("GET", "/proxies/" + GROUP),
                                ("GET", "/proxies/" + GROUP), ("PUT", "/proxies/" + GROUP),
                                ("GET", "/proxies/" + GROUP)]
                    assert [(e[0], e[1]) for e in events] == expected, events
                    assert puts == [("PUT", "/proxies/" + GROUP, {"name": NODE_FAST})], puts
                    assert current == NODE_FAST and events[-1][2] == NODE_FAST, events
                else:
                    assert not puts, (name, events)
                if name == "group-changed":
                    assert changed and current == "E2E-Changed", (changed, events)
                    assert len([e for e in events if e[:2] == ("GET", "/proxies/" + GROUP)]) >= 2, events
                if name == "pre-put-profile-changed":
                    assert changed and current == "E2E-Before", (changed, events)
                    assert len([e for e in events if e[:2] == ("GET", "/proxies/" + GROUP)]) == 2, events
                assert all(tunnels.values()), (name, tunnels)
                if name != "cancel":
                    assert all(heads.values()) and all(gets.values()), (name, heads, gets)
                print(f"E2E {name}: {len(puts)} PUT; local tunnels={tunnels}; "
                      f"controller events={[event[:2] for event in events]}")
            if FAST.read_bytes() != original_fast or CONTROLLER.read_bytes() != original_controller:
                raise RuntimeError("Swift sources changed during the test; rerun against the latest snapshot")
            print("Source SHA-256:", hashlib.sha256(original_fast).hexdigest(), hashlib.sha256(original_controller).hexdigest())
            print("PASS: synthetic AppDelegate end-to-end flows; no real profile/socket/node accessed")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)


if __name__ == "__main__":
    main()
