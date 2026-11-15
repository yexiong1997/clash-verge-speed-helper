#!/usr/bin/env python3
"""Isolated lifecycle tests: passive reconnect must never bootstrap the real app."""
import http.client
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TOKEN = "a" * 64

FIXTURES = r'''
// Test-only same-file fixtures; no production initializer or test hooks.
extension ClashRuntimeInjector {
    static func passiveFixture(owned: RuntimeBridge?) -> ClashRuntimeInjector {
        let injector = ClashRuntimeInjector()
        injector.bridge = owned
        if owned != nil { injector.state = .attached }
        return injector
    }
}
extension RuntimeBridge {
    func expireFixture() {
        lock.lock(); lastActivityAt = Date.distantPast; lock.unlock()
    }
}
'''

HARNESS = r'''
import AppKit
import Foundation
private let outputLock = NSLock()
private func output(_ line: String) {
    outputLock.lock(); defer { outputLock.unlock() }
    print(line); fflush(stdout)
}
@main struct PassiveHarness {
    static func main() throws {
        let owned = CommandLine.arguments.contains("--owned") ? try RuntimeBridge() : nil
        defer { owned?.stop() }
        let injector = ClashRuntimeInjector.passiveFixture(owned: owned)
        if let owned { output("OWNED \(owned.port) \(owned.token)") }
        else { output("READY") }
        while let line = readLine() {
            let parts = line.split(separator: " ").map(String.init)
            switch parts.first {
            case "start":
                let id = parts[1]
                injector.start { result in
                    switch result {
                    case .success: output("START \(id) true")
                    case .failure(let error):
                        if case ClashRuntimeError.cancelled = error { output("START \(id) cancelled") }
                        else { output("START \(id) error") }
                    }
                }
            case "passive":
                let id = parts[1]
                injector.reconnectSilently { result in
                    switch result {
                    case .success(let attached): output("DONE \(id) \(attached)")
                    case .failure(let error):
                        if case ClashRuntimeError.cancelled = error { output("DONE \(id) cancelled") }
                        else { output("DONE \(id) error") }
                    }
                }
            case "state": output("STATE \(injector.isReady) \(injector.currentState)")
            case "stop": injector.stop(); output("STOPPED")
            case "expire": owned?.expireFixture(); output("EXPIRED")
            case "quit": injector.stop(); return
            default: fatalError("unexpected test command")
            }
        }
    }
}
'''


class FakeBridge:
    def __enter__(self):
        self.attached = True
        self.status = 200
        self.raw = None
        self.block = False
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers.get("Authorization")))
                owner.arrived.set()
                if owner.block:
                    owner.release.wait(timeout=8)
                data = owner.raw if owner.raw is not None else json.dumps({"attached": owner.attached}).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class RuntimePassiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="clash-passive-test-", dir="/private/tmp"))
        source = (PROJECT / "Sources/ClashRuntimeInjector.swift").read_text()
        # An environment override alone still falls back to the real manifest.
        source = source.replace("/private/tmp/clash-verge-runtime-state.json", str(cls.tmp / "absent-fallback"))
        forbidden = [
            "private func verifyOfficialBundle() throws {",
            "private func ensureOfficialAppRunning() throws {",
            "private func readSingletonRecord() throws -> SingletonRecord {",
            "private func showOfficialWindow(_ record: SingletonRecord) throws {",
            "private func injectLoader(bridge: RuntimeBridge, ui: BootstrapUIContext) throws {",
            "private func injectLoader(loader: String, ui: BootstrapUIContext) throws {",
            "private func captureBootstrapUI(generation: UInt64) -> BootstrapUIContext {",
            "private func restoreBootstrapUI(_ ui: BootstrapUIContext) {",
            "private func promptAccessibility(openSettings: Bool, alwaysOpenSettings: Bool = false) {",
        ]
        for signature in forbidden:
            assert source.count(signature) == 1, signature
            # Only explicit-start tests may stub signature validation. All
            # launch, AX and configuration-reading methods still fail closed.
            stub = '\n if CommandLine.arguments.contains("--start-fixture") { return }' if "verifyOfficialBundle" in signature else ""
            source = source.replace(signature, signature + stub + '\n fatalError("forbidden passive side effect")')
        copied = cls.tmp / "ClashRuntimeInjector.swift"
        copied.write_text(source + FIXTURES)
        harness = cls.tmp / "Harness.swift"
        harness.write_text(HARNESS)
        cls.binary = cls.tmp / "PassiveHarness"
        result = subprocess.run([
            "/usr/bin/xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
            "-target", f"{os.uname().machine}-apple-macos12.0",
            "-module-cache-path", str(cls.tmp / "cache"),
            "-framework", "AppKit", "-framework", "ApplicationServices", "-framework", "CoreGraphics",
            str(copied), str(harness), "-o", str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @contextmanager
    def harness(self, server=None, owned=False, explicit=False):
        with tempfile.TemporaryDirectory(dir=self.tmp) as directory:
            manifest = Path(directory) / "state.json"
            if server:
                manifest.write_text(json.dumps({"port": server.port, "token": TOKEN}))
            env = {**os.environ, "CLASH_RUNTIME_STATE": str(manifest), "CLASH_DISABLE_ACCESSIBILITY_PROMPT": "1", "CLASH_RUNTIME_BROKER_DISABLED": "1"}
            arguments = (["--owned"] if owned else []) + (["--start-fixture"] if explicit else [])
            process = subprocess.Popen([str(self.binary)] + arguments,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, text=True, bufsize=1)
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line.strip()) for line in process.stdout], daemon=True)
            reader.start()

            def send(command):
                process.stdin.write(command + "\n")
                process.stdin.flush()

            def read():
                try:
                    return lines.get(timeout=7)
                except queue.Empty:
                    self.fail(f"harness did not respond; returncode={process.poll()}")

            first = read()
            try:
                yield send, read, first
                send("quit")
                self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
                reader.join(timeout=2)
                self.assertTrue(lines.empty(), "unexpected or duplicate callback")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def assert_status_requests(self, server):
        self.assertTrue(server.requests)
        self.assertTrue(all(item == ("GET", "/status", f"Bearer {TOKEN}") for item in server.requests))

    def test_missing_does_not_bootstrap(self):
        with self.harness() as (send, read, first):
            self.assertEqual(first, "READY")
            send("passive missing")
            self.assertEqual(read(), "DONE missing false")
            send("state")
            self.assertEqual(read(), "STATE false idle")

    def test_external_reuse_then_stale_revalidation(self):
        with FakeBridge() as server, self.harness(server) as (send, read, _):
            send("passive reuse")
            self.assertEqual(read(), "DONE reuse true")
            send("state")
            self.assertEqual(read(), "STATE true attached")
            server.attached = False
            send("passive stale")
            self.assertEqual(read(), "DONE stale false")
            send("state")
            self.assertEqual(read(), "STATE false idle")
            self.assert_status_requests(server)

    def test_invalid_external_responses_do_not_bootstrap(self):
        for status, raw in [(401, b"{}"), (200, b"broken-json")]:
            with self.subTest(status=status), FakeBridge() as server:
                server.status, server.raw = status, raw
                with self.harness(server) as (send, read, _):
                    send("passive invalid")
                    self.assertEqual(read(), "DONE invalid false")
                    send("state")
                    self.assertEqual(read(), "STATE false idle")
                    self.assert_status_requests(server)

    def test_owned_bridge_reuse_and_expiry(self):
        with self.harness(owned=True) as (send, read, first):
            _, port, token = first.split()
            connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=3)
            try:
                connection.request("POST", "/hello", json.dumps({"client": "test-owned", "origin": "tauri://localhost"}),
                                   {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()
            send("passive owned")
            self.assertEqual(read(), "DONE owned true")
            send("expire")
            self.assertEqual(read(), "EXPIRED")
            send("passive expired")
            self.assertEqual(read(), "DONE expired false")
            send("state")
            self.assertEqual(read(), "STATE false idle")

    def test_stop_rejects_late_discovery_and_is_idempotent(self):
        with FakeBridge() as server, self.harness(server) as (send, read, _):
            server.block = True
            send("passive race")
            self.assertTrue(server.arrived.wait(timeout=3))
            send("stop")
            self.assertEqual(read(), "STOPPED")
            server.release.set()
            self.assertEqual(read(), "DONE race cancelled")
            send("stop")
            self.assertEqual(read(), "STOPPED")
            send("state")
            self.assertEqual(read(), "STATE false idle")
            send("passive stopped")
            self.assertEqual(read(), "DONE stopped cancelled")

    def test_explicit_start_reuses_external_before_any_app_launch(self):
        with FakeBridge() as server, self.harness(server, explicit=True) as (send, read, _):
            send("start reuse")
            self.assertEqual(read(), "START reuse true")
            send("state")
            self.assertEqual(read(), "STATE true attached")
            self.assert_status_requests(server)

    def test_stop_then_start_rejects_previous_generation(self):
        with FakeBridge() as server, self.harness(server, explicit=True) as (send, read, _):
            server.block = True
            send("start old")
            self.assertTrue(server.arrived.wait(timeout=3))
            send("stop")
            self.assertEqual(read(), "START old cancelled")
            self.assertEqual(read(), "STOPPED")
            send("state")
            self.assertEqual(read(), "STATE false idle")
            send("start new")
            server.release.set()
            self.assertEqual(read(), "START new true")
            send("state")
            self.assertEqual(read(), "STATE true attached")
            self.assert_status_requests(server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
