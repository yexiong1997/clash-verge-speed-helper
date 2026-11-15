#!/usr/bin/env python3
"""Synthetic loopback integration tests for RuntimeBridge and ClashRuntimeInjector."""
import http.client
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

SWIFT_HARNESS = r'''
import AppKit
import Foundation

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

private func runBridgeDriver() throws {
    let bridge = try RuntimeBridge()
    defer { bridge.stop() }
    print("PORT=\(bridge.port)")
    print("TOKEN=\(bridge.token)")
    fflush(stdout)

    while let line = readLine(strippingNewline: true) {
        let parts = line.split(separator: " ", maxSplits: 3).map(String.init)
        guard let cmd = parts.first else { continue }
        switch cmd {
        case "wait_attach":
            let timeout = Double(parts.count > 1 ? parts[1] : "5") ?? 5
            do {
                try bridge.waitUntilAttached(timeout: timeout)
                print("ATTACHED ok")
            } catch {
                print("ATTACHED err=\(error.localizedDescription)")
            }
        case "is_alive":
            let threshold = Double(parts.count > 1 ? parts[1] : "3.5") ?? 3.5
            print("ALIVE \(bridge.isAlive(idleThreshold: threshold))")
        case "enqueue":
            // enqueue <uid> <pickupTimeout> <executionTimeout>
            guard parts.count == 4,
                  let pickup = Double(parts[2]),
                  let execTimeout = Double(parts[3]) else { fail("bad enqueue args") }
            let uid = parts[1]
            let sem = DispatchSemaphore(value: 0)
            var resultLine = ""
            bridge.enqueue(command: ["op": "switch", "uid": uid],
                           pickupTimeout: pickup,
                           executionTimeout: execTimeout) { result in
                switch result {
                case .success(let value):
                    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data()
                    resultLine = "ENQUEUE ok " + (String(data: data, encoding: .utf8) ?? "{}")
                case .failure(let error):
                    let code: String
                    if let runtimeError = error as? ClashRuntimeError {
                        switch runtimeError {
                        case .notAttached: code = "notAttached"
                        case .switchTimeout: code = "switchTimeout"
                        case .cancelled: code = "cancelled"
                        case .bridgeFailure(let msg): code = "bridgeFailure:\(msg)"
                        default: code = "other:\(runtimeError.localizedDescription)"
                        }
                    } else {
                        code = "other:\(error.localizedDescription)"
                    }
                    resultLine = "ENQUEUE err \(code)"
                }
                sem.signal()
            }
            _ = sem.wait(timeout: .now() + 15)
            print(resultLine)
        case "enqueue_and_cancel":
            let id = UUID()
            let sem = DispatchSemaphore(value: 0)
            var resultLine = ""
            bridge.enqueue(id: id, command: ["op": "switch", "uid": parts.count > 1 ? parts[1] : "u1"],
                           pickupTimeout: 3.0, executionTimeout: 10.0) { result in
                switch result {
                case .success:
                    resultLine = "CANCELLED unexpected_success"
                case .failure(let error):
                    if case ClashRuntimeError.cancelled = error {
                        resultLine = "CANCELLED ok"
                    } else {
                        resultLine = "CANCELLED wrong:\(error)"
                    }
                }
                sem.signal()
            }
            bridge.cancel(id: id)
            _ = sem.wait(timeout: .now() + 5)
            print(resultLine)
        case "injector_negative":
            let inj = ClashRuntimeInjector.shared
            let readyBefore = inj.isReady
            var cancelCode = ""
            let sem = DispatchSemaphore(value: 0)
            // Validation is synchronous, so this cannot discover a real bridge
            // or launch Clash before cancellation wins a scheduling race.
            let reqID = inj.switchProfile(uid: "") { result in
                switch result {
                case .success:
                    cancelCode = "unexpected_success"
                case .failure(let error):
                    if case ClashRuntimeError.invalidProfileUID = error {
                        cancelCode = "invalidProfileUID"
                    } else {
                        cancelCode = "error:\(error)"
                    }
                }
                sem.signal()
            }
            inj.cancelSwitch(reqID)
            _ = sem.wait(timeout: .now() + 5)
            let readyAfter = inj.isReady
            inj.stop()
            let readyStopped = inj.isReady
            print("INJECTOR readyBefore=\(readyBefore) cancel=\(cancelCode) readyAfter=\(readyAfter) readyStopped=\(readyStopped)")
        case "quit":
            return
        default:
            fail("unknown cmd: \(cmd)")
        }
        fflush(stdout)
    }
}

@main struct BridgeHarnessMain {
    static func main() throws {
        try runBridgeDriver()
    }
}
'''


class RuntimeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='clash-runtime-bridge-test-', dir='/private/tmp'))
        src = cls.tmp / 'ClashRuntimeInjector.swift'
        shutil.copyfile(PROJECT / 'Sources/ClashRuntimeInjector.swift', src)
        harness = cls.tmp / 'BridgeHarness.swift'
        harness.write_text(SWIFT_HARNESS, encoding='utf-8')
        cls.binary = cls.tmp / 'BridgeHarness'
        cache = cls.tmp / 'cache'
        res = subprocess.run(
            ['/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-target', f'{os_machine()}-apple-macos12.0',
             '-parse-as-library', '-module-cache-path', str(cache),
             '-framework', 'AppKit', '-framework', 'ApplicationServices', '-framework', 'CoreGraphics',
             str(src), str(harness), '-o', str(cls.binary)],
            capture_output=True, text=True, timeout=120
        )
        if res.returncode != 0:
            raise RuntimeError(f'swiftc failed:\n{res.stderr}')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def spawn_harness(self):
        proc = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )
        port_line = proc.stdout.readline().strip()
        token_line = proc.stdout.readline().strip()
        self.assertTrue(port_line.startswith('PORT='), f'unexpected port line: {port_line}')
        self.assertTrue(token_line.startswith('TOKEN='), f'unexpected token line: {token_line}')
        port = int(port_line.split('=', 1)[1])
        token = token_line.split('=', 1)[1]
        return proc, port, token

    def http_request(self, port, method, path, headers=None, body=None, timeout=5):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        try:
            payload = None if body is None else json.dumps(body).encode('utf-8')
            conn.request(method, path, body=payload, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read().decode('utf-8')
            return resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), raw
        finally:
            conn.close()

    def test_cors_auth_patch_and_roundtrip(self):
        proc, port, token = self.spawn_harness()
        try:
            # 1. OPTIONS preflight returns 204, allows Cache-Control, and omits Content-Length
            status, hdrs, raw = self.http_request(port, 'OPTIONS', '/hello')
            self.assertEqual(status, 204)
            self.assertEqual(raw, '')
            self.assertEqual(hdrs.get('access-control-allow-origin'), 'tauri://localhost')
            allowed = {h.strip().lower() for h in hdrs.get('access-control-allow-headers', '').split(',')}
            self.assertIn('authorization', allowed)
            self.assertIn('content-type', allowed)
            self.assertIn('cache-control', allowed)
            self.assertNotIn('content-length', hdrs)

            # 2. Unauthorized rejected with 401
            status, _, _ = self.http_request(port, 'GET', '/status')
            self.assertEqual(status, 401)

            auth = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

            # 3. GET /patch uses cache:'no-store' option and omits Cache-Control header in J()
            status, _, patch_js = self.http_request(port, 'GET', '/patch', headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(status, 200)
            self.assertIn("cache:'no-store'", patch_js)
            self.assertNotIn("'Cache-Control':'no-store'", patch_js)
            self.assertIn('patch_profiles_config', patch_js)

            # 4. Stalled half-open socket does not wedge /hello or /status
            stalled = socket.create_connection(('127.0.0.1', port), timeout=5)
            try:
                stalled.sendall(b'GET /status HTTP/1.1\r\n')  # incomplete headers, never sends \r\n\r\n
                attach_out = []
                t = threading.Thread(target=lambda: (
                    proc.stdin.write('wait_attach 5\n'),
                    proc.stdin.flush(),
                    attach_out.append(proc.stdout.readline().strip())
                ))
                t.start()
                status, _, body = self.http_request(port, 'POST', '/hello', headers=auth, body={'client': 'c1', 'origin': 'tauri://localhost'})
                self.assertEqual(status, 200)
                t.join(timeout=5)
                self.assertEqual(attach_out, ['ATTACHED ok'])
            finally:
                stalled.close()

            # 5. Mismatched client poll returns 409, matching client gets 204
            status, _, _ = self.http_request(port, 'GET', '/poll?client=wrong', headers=auth)
            self.assertEqual(status, 409)
            status, _, _ = self.http_request(port, 'GET', '/poll?client=c1', headers=auth)
            self.assertEqual(status, 204)

            # 6. Full command roundtrip: enqueue -> /poll -> /result
            enq_out = []
            t = threading.Thread(target=lambda: (
                proc.stdin.write('enqueue uid-alpha 3 5\n'),
                proc.stdin.flush(),
                enq_out.append(proc.stdout.readline().strip())
            ))
            t.start()
            cmd = None
            for _ in range(30):
                status, _, raw = self.http_request(port, 'GET', '/poll?client=c1', headers=auth)
                if status == 200:
                    cmd = json.loads(raw)['command']
                    break
                time.sleep(0.05)
            self.assertIsNotNone(cmd)
            self.assertEqual(cmd['op'], 'switch')
            self.assertEqual(cmd['uid'], 'uid-alpha')
            status, _, _ = self.http_request(port, 'POST', '/result', headers=auth, body={
                'client': 'c1',
                'id': cmd['id'],
                'ok': True,
                'value': {'previous': 'uid-old', 'current': 'uid-alpha', 'name': 'Alpha'}
            })
            self.assertEqual(status, 200)
            t.join(timeout=5)
            self.assertTrue(enq_out[0].startswith('ENQUEUE ok '), enq_out)
            self.assertEqual(json.loads(enq_out[0][len('ENQUEUE ok '):]), {'current': 'uid-alpha', 'name': 'Alpha', 'previous': 'uid-old'})

            # 7. Error sanitization from /result
            enq_err = []
            t = threading.Thread(target=lambda: (
                proc.stdin.write('enqueue uid-missing 3 5\n'),
                proc.stdin.flush(),
                enq_err.append(proc.stdout.readline().strip())
            ))
            t.start()
            cmd = None
            for _ in range(30):
                status, _, raw = self.http_request(port, 'GET', '/poll?client=c1', headers=auth)
                if status == 200:
                    cmd = json.loads(raw)['command']
                    break
                time.sleep(0.05)
            self.assertIsNotNone(cmd)
            self.http_request(port, 'POST', '/result', headers=auth, body={
                'client': 'c1', 'id': cmd['id'], 'ok': False, 'error': 'profile not found'
            })
            t.join(timeout=5)
            self.assertEqual(enq_err[0], 'ENQUEUE err bridgeFailure:未在 Clash Verge 中找到目标订阅。')

            proc.stdin.write('quit\n')
            proc.stdin.flush()
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_timeouts_cancellation_and_liveness(self):
        proc, port, token = self.spawn_harness()
        auth = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        try:
            # Attach client c2
            proc.stdin.write('wait_attach 5\n')
            proc.stdin.flush()
            status, _, _ = self.http_request(port, 'POST', '/hello', headers=auth, body={'client': 'c2'})
            self.assertEqual(status, 200)
            self.assertEqual(proc.stdout.readline().strip(), 'ATTACHED ok')

            # 1. Cancel removes queued command before poll sees it
            proc.stdin.write('enqueue_and_cancel uid-cancel\n')
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), 'CANCELLED ok')
            status, _, _ = self.http_request(port, 'GET', '/poll?client=c2', headers=auth)
            self.assertEqual(status, 204)

            # 2. Execution timeout when command is picked up but no /result arrives
            enq_exec = []
            t = threading.Thread(target=lambda: (
                proc.stdin.write('enqueue uid-hang 2.0 0.4\n'),
                proc.stdin.flush(),
                enq_exec.append(proc.stdout.readline().strip())
            ))
            t.start()
            picked = False
            for _ in range(20):
                status, _, _ = self.http_request(port, 'GET', '/poll?client=c2', headers=auth)
                if status == 200:
                    picked = True
                    break
                time.sleep(0.05)
            self.assertTrue(picked)
            t.join(timeout=5)
            self.assertEqual(enq_exec[0], 'ENQUEUE err switchTimeout')

            # 3. Idle heartbeat expiration sets isAlive false, then pickup timeout fires and clears attachedClient
            time.sleep(0.6)
            proc.stdin.write('is_alive 0.3\n')
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), 'ALIVE false')

            # Refresh via /poll -> alive again
            status, _, _ = self.http_request(port, 'GET', '/poll?client=c2', headers=auth)
            self.assertEqual(status, 204)
            proc.stdin.write('is_alive 3.5\n')
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), 'ALIVE true')

            # Pickup timeout when client stops polling
            proc.stdin.write('enqueue uid-nopoll 0.3 5.0\n')
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), 'ENQUEUE err notAttached')
            proc.stdin.write('is_alive 3.5\n')
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), 'ALIVE false')

            # 4. Invalid input never starts the real injector.
            proc.stdin.write('injector_negative\n')
            proc.stdin.flush()
            line = proc.stdout.readline().strip()
            self.assertEqual(line, 'INJECTOR readyBefore=false cancel=invalidProfileUID readyAfter=false readyStopped=false')

            proc.stdin.write('quit\n')
            proc.stdin.flush()
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def os_machine():
    import os
    return os.uname().machine


if __name__ == '__main__':
    unittest.main(verbosity=2)
