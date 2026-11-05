#!/usr/bin/env python3
"""Isolated assisted-switch tests using only private synthetic files/sockets.

Run on macOS: python3 -B Tests/test_assisted_switch.py
No user profile, real controller, remote server, or user node traffic is used.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import unittest
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler

PROJECT = Path(__file__).resolve().parent.parent
NODE = 'Synthetic node é'
SECRET = 'synthetic-secret-for-fixture'

DRIVER = r'''
import Foundation

private final class AuthorizationGate: @unchecked Sendable {
    private let lock = NSLock()
    private var authorized = true

    func isAuthorized() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return authorized
    }
    func revoke() {
        lock.lock(); defer { lock.unlock() }
        authorized = false
    }
}

@main struct AssistedDriver {
    static func wait<T>(_ action: (@escaping (T) -> Void) -> Void) -> T {
        var result: T? = nil
        var returned = false
        action { value in
            precondition(Thread.isMainThread && returned, "completion must be asynchronously on main")
            result = value
        }
        returned = true
        let deadline = Date().addingTimeInterval(30)
        while result == nil && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.005)) }
        guard let value = result else { fatalError("completion timeout") }
        return value
    }
    static func reply(_ object: [String: Any]) {
        let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        FileHandle.standardOutput.write(data + Data([10]))
    }
    static func prep(_ value: AssistedSwitchPreparation) -> [String: Any] {
        switch value {
        case .manualProfileRequired: return ["status": "manual"]
        case .ready(let choices):
            return ["status": "ready", "groups": choices.groups.map { ["name": $0.name, "now": $0.now, "all": $0.all] as [String: Any] }]
        case .notAttempted(let error): return ["status": "notAttempted", "failure": String(describing: error)]
        }
    }
    static func applied(_ value: AssistedSwitchResult) -> [String: Any] {
        switch value {
        case .manualProfileRequired: return ["status": "manual"]
        case .confirmedNoOp: return ["status": "noOp"]
        case .switched: return ["status": "switched"]
        case .notAttempted(let error): return ["status": "notAttempted", "failure": String(describing: error)]
        case .outcomeUnknown(let uncertainty): return ["status": "unknown", "reason": String(describing: uncertainty)]
        }
    }
    static func main() throws {
        guard let first = readLine(), let bytes = first.data(using: .utf8),
              let input = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { fatalError("bad fixture input") }
        let snapshot = Data(base64Encoded: input["snapshot"] as! String)!
        // Pass a Data value backed by mutable caller memory, then mutate it.
        // The coordinator must keep the exact original snapshot regardless.
        let storage = UnsafeMutableRawPointer.allocate(byteCount: snapshot.count, alignment: 1)
        snapshot.copyBytes(to: storage.assumingMemoryBound(to: UInt8.self), count: snapshot.count)
        let borrowed = Data(bytesNoCopy: storage, count: snapshot.count, deallocator: .none)
        let settings = ControllerSettings(socket: input["socket"] as! String, secret: input["secret"] as! String)
        let root = URL(fileURLWithPath: input["root"] as! String, isDirectory: true)
        let coordinator: AssistedSwitchCoordinator
        if input["manual"] as? Bool == true {
            coordinator = AssistedSwitchCoordinator(root: root, settings: settings,
                manualNodeName: input["node"] as! String, profileUID: input["uid"] as! String,
                profilePath: input["path"] as! String, measuredYAML: borrowed)
        } else {
            guard let worker = input["worker"] as? [String: Any] else { fatalError("missing worker") }
            let raw = try JSONSerialization.data(withJSONObject: worker, options: [.sortedKeys])
            guard let measured = VerifiedMeasurement.decode(raw, expectedName: worker["node_name"] as! String,
                requestedBytes: 1024, streams: 1) else { fatalError("bad worker fixture") }
            coordinator = AssistedSwitchCoordinator(root: root, settings: settings,
                winner: measured, nodeName: input["node"] as! String,
                profileUID: input["uid"] as! String, profilePath: input["path"] as! String, measuredYAML: borrowed)
        }
        if !snapshot.isEmpty { storage.storeBytes(of: UInt8(0x7f), as: UInt8.self) }
        reply(["status": "initialized"])
        while let line = readLine() {
            guard let command = line.data(using: .utf8),
                  let request = try JSONSerialization.jsonObject(with: command) as? [String: String] else { fatalError("bad command") }
            switch request["command"] {
            case "prepare":
                let result: AssistedSwitchPreparation = wait { coordinator.prepare(completion: $0) }
                reply(prep(result))
            case "apply":
                // Exercise the source-compatible default authorization.
                let result: AssistedSwitchResult = wait { coordinator.apply(group: request["group"]!, completion: $0) }
                reply(applied(result))
            case "applyAuthorized":
                let result: AssistedSwitchResult = wait {
                    coordinator.apply(group: request["group"]!, authorization: { true }, completion: $0)
                }
                reply(applied(result))
            case "applyUnauthorized":
                let result: AssistedSwitchResult = wait {
                    coordinator.apply(group: request["group"]!, authorization: { false }, completion: $0)
                }
                reply(applied(result))
            case "applyRevocable":
                let gate = AuthorizationGate()
                // The fixture sends this line only after the Unix-socket server
                // has observed and held the apply-time GET open.
                DispatchQueue.global().async {
                    guard readLine() == "revoke" else { fatalError("expected revocation") }
                    gate.revoke()
                    reply(["status": "revoked"])
                }
                let result: AssistedSwitchResult = wait {
                    coordinator.apply(group: request["group"]!, authorization: { gate.isAuthorized() }, completion: $0)
                }
                reply(applied(result))
            case "quit":
                storage.deallocate()
                return
            default: fatalError("bad command")
            }
        }
        storage.deallocate()
    }
}
'''


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.events = []
        self.errors = []
        self.lock = threading.Lock()
        self.hook = None
        self.hook_catalog = None
        self.put_mode = 'success'
        self.expected_node = NODE
        self.groups = {
            'Main': dict(name='Main', type='Selector', now='before', all=['before', NODE]),
            'GLOBAL': dict(name='GLOBAL', type='Selector', now='before', all=['before', NODE]),
            'bad/name': dict(name='bad/name', type='Selector', now='before', all=[NODE]),
            'Automatic': dict(name='Automatic', type='URLTest', now='before', all=[NODE]),
            'Nested only': dict(name='Nested only', type='Selector', now='Main', all=['Main']),
            'Other bytes': dict(name='Other bytes', type='Selector', now='before', all=['Synthetic node e\u0301']),
        }
        super().__init__(path, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status, data=None):
        payload = b'' if data is None else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def record(self):
        if self.headers.get('Authorization') != 'Bearer ' + SECRET:
            self.server.errors.append('wrong authorization')
        with self.server.lock:
            self.server.events.append((self.command, self.path))

    def do_GET(self):
        self.record()
        if self.path == '/proxies':
            hook, self.server.hook_catalog = self.server.hook_catalog, None
            if hook:
                hook()
            return self.respond(200, {'proxies': self.server.groups})
        if not self.path.startswith('/proxies/'):
            return self.respond(404)
        name = urllib.parse.unquote(self.path[len('/proxies/'):])
        hook, self.server.hook = self.server.hook, None
        if hook:
            hook()
        group = self.server.groups.get(name)
        if group is None:
            return self.respond(404)
        self.respond(200, group)

    def do_PUT(self):
        self.record()
        name = urllib.parse.unquote(self.path[len('/proxies/'):])
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        except Exception:
            self.server.errors.append('bad JSON')
            return self.respond(400)
        group = self.server.groups.get(name)
        node = self.server.expected_node
        if body != {'name': node} or group is None or node not in group['all']:
            self.server.errors.append('wrong target')
            return self.respond(400)
        if self.server.put_mode != 'mismatch':
            group['now'] = node
        if self.server.put_mode == 'unknown':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.respond(204)


def worker(name=NODE, *, complete=True, speed=12345, successes=5, latency=50):
    return dict(version=1, node_name=name, node_type='Synthetic', head_attempts=6,
                head_successes=successes, latency_ms=latency, jitter_ms=4,
                expected_bytes=1024, received_bytes=1024 if complete else 512,
                download_complete=complete, download_bytes_per_second=speed)


class AssistedSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This is the only HOME, root, build and scratch area used by tests.
        cls.temp = tempfile.TemporaryDirectory(prefix='asw-', dir='/private/tmp')
        cls.work = Path(cls.temp.name)
        assert cls.work.stat().st_mode & 0o077 == 0
        cls.home = cls.work / 'home'
        cls.home.mkdir(mode=0o700)
        cls.env = os.environ.copy()
        cls.env.update(HOME=str(cls.home), CURL_HOME=str(cls.home), TMPDIR=str(cls.work),
                       CLANG_MODULE_CACHE_PATH=str(cls.work / 'clang-cache'),
                       SWIFT_MODULECACHE_PATH=str(cls.work / 'swift-cache'),
                       http_proxy='http://127.0.0.1:1', HTTP_PROXY='http://127.0.0.1:1',
                       ALL_PROXY='http://127.0.0.1:1', no_proxy='', NO_PROXY='')
        driver = cls.work / 'Driver.swift'
        driver.write_text(DRIVER, encoding='utf-8')
        cls.binary = cls.work / 'driver'
        sources = ['CompositeScore.swift', 'VerifiedMeasurement.swift', 'ProfileRegistry.swift',
                   'SwitchController.swift', 'AssistedSwitch.swift']
        command = ['/usr/bin/swiftc', '-swift-version', '6', '-target',
                   f'{os.uname().machine}-apple-macos12.0'] + [str(PROJECT / 'Sources' / name) for name in sources]
        subprocess.run(command + [str(driver), '-o', str(cls.binary)], check=True, env=cls.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory(prefix='c-', dir=self.work)
        self.case = Path(self.temp_case.name)
        self.root = self.case / 'cfg'
        self.root.mkdir(mode=0o700)
        self.profiles = self.root / 'profiles'
        self.profiles.mkdir(mode=0o700)
        self.file = self.profiles / 'target.yaml'
        self.snapshot = b'proxies:\n  - name: synthetic\n# SYNTH_PRIVATE_YAML\n'
        self.file.write_bytes(self.snapshot)
        (self.profiles / 'other.yaml').write_bytes(b'proxies: []\n')
        self.registry('target')
        self.sock = self.case / 's'
        assert len(os.fsencode(self.sock)) < 104
        self.server = Server(str(self.sock))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.process = None
        self.extra_sockets = []

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.stdin.write(b'{"command":"quit"}\n')
                self.process.stdin.flush()
            stdout, stderr = self.process.communicate(timeout=10)
            self.assertEqual(self.process.returncode, 0, 'synthetic driver failed')
            self.assertEqual(stdout, b'')
            self.assertEqual(stderr, b'')
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for extra in self.extra_sockets:
            extra.close()
        self.assertFalse(self.server.errors, 'fake server invariant failed')
        self.temp_case.cleanup()

    def registry(self, current):
        entries = [dict(uid='target', name='Same display', file='target.yaml', type='remote'),
                   dict(uid='other', name='Same display', file='other.yaml', type='remote')]
        (self.root / 'profiles.yaml').write_text(json.dumps(dict(current=current, items=entries)), encoding='utf-8')

    def start(self, **overrides):
        # Foundation's standardizedFileURL uses /tmp for this private fixture;
        # callers must pass the exact path returned by ProfileRegistry.options.
        registry_path = str(self.file).replace('/private/tmp/', '/tmp/', 1)
        payload = dict(root=str(self.root), path=registry_path, uid='target', socket=str(self.sock),
                       secret=SECRET, node=NODE, snapshot=base64.b64encode(self.snapshot).decode(), worker=worker())
        payload.update(overrides)
        self.process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=self.env)
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False).encode() + b'\n')
        self.process.stdin.flush()
        self.assertEqual(json.loads(self.process.stdout.readline()), {'status': 'initialized'})

    def command(self, name, **values):
        self.process.stdin.write(json.dumps(dict(command=name, **values), ensure_ascii=False).encode() + b'\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def prepare(self):
        return self.command('prepare')

    def apply(self, group='Main'):
        return self.command('apply', group=group)

    def puts(self):
        with self.server.lock:
            return [x for x in self.server.events if x[0] == 'PUT']

    def ready(self):
        result = self.prepare()
        self.assertEqual(result['status'], 'ready', result)
        return result

    def assert_refused(self, result, failure=None):
        self.assertEqual(result['status'], 'notAttempted', result)
        if failure is not None:
            self.assertIn(failure, result['failure'])
        self.assertEqual(self.puts(), [])

    def replace_socket(self):
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        alternate = self.case / 'replacement'
        other.bind(str(alternate))
        other.listen(1)
        self.extra_sockets.append(other)
        os.replace(alternate, self.sock)

    def test_manual_unmeasured_node_switches_and_is_read_back(self):
        # No VerifiedMeasurement, worker output, or invented success is needed.
        self.start(manual=True, worker=None)
        result = self.ready()
        self.assertEqual([g['name'] for g in result['groups']], ['GLOBAL', 'Main'])
        self.assertEqual(self.command('applyAuthorized', group='Main'), {'status': 'switched'})
        self.assertEqual([method for method, _ in self.server.events], ['GET', 'GET', 'PUT', 'GET'])
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.server.groups['Main']['now'], NODE)

    def test_manual_failed_or_metadata_named_node_does_not_require_score(self):
        name = '剩余流量: 10 GB'
        self.server.expected_node = name
        self.server.groups['Main']['all'] = ['before', name]
        self.start(manual=True, node=name, worker=worker(name=name, complete=False, speed=0))
        self.ready()
        self.assertEqual(self.apply()['status'], 'switched')
        self.assertEqual(len(self.puts()), 1)

    def test_manual_profile_identity_and_snapshot_validation(self):
        self.start(manual=True, worker=None, uid='other')
        self.assert_refused(self.prepare(), 'profileChanged')
        self.assertEqual(self.server.events, [])

    def test_manual_current_profile_and_preput_snapshot_guard(self):
        self.registry('other')
        self.start(manual=True, worker=None)
        self.assertEqual(self.prepare()['status'], 'manual')
        self.assertEqual(self.server.events, [])
        self.registry('target')
        self.ready()
        self.server.hook = lambda: self.file.write_bytes(b'changed synthetic profile')
        self.assert_refused(self.apply(), 'snapshotChanged')

    def test_manual_direct_membership_and_node_bytes(self):
        equivalent = unicodedata.normalize('NFD', NODE)
        self.assertNotEqual(equivalent.encode(), NODE.encode())
        self.server.groups['Main']['all'] = ['before', equivalent]
        self.server.groups['GLOBAL']['all'] = ['before', equivalent]
        self.start(manual=True, worker=None)
        result = self.ready()
        self.assertEqual(result['groups'], [])
        self.assert_refused(self.apply(), 'groupNotPrepared')

    def test_manual_stale_now_and_membership_refused(self):
        self.start(manual=True, worker=None)
        self.ready()
        self.server.groups['Main']['now'] = 'changed'
        self.assert_refused(self.apply(), 'currentChanged')
        self.server.groups['Main']['now'] = 'before'
        self.ready()
        self.server.groups['Main']['all'] = ['before']
        self.assert_refused(self.apply(), 'nodeNotMember')

    def test_manual_authorization_revocation_blocks_last_moment_put(self):
        self.start(manual=True, worker=None)
        self.ready()
        self.assertEqual(self.apply_revoked_during_delayed_get(),
                         {'status': 'notAttempted', 'failure': 'cancelled'})
        self.assertEqual(self.puts(), [])

    def test_manual_socket_replacement_blocks_put(self):
        self.start(manual=True, worker=None)
        self.ready()
        self.server.hook = self.replace_socket
        self.assert_refused(self.apply(), 'controllerChanged')

    def test_manual_unknown_outcome_never_retries(self):
        self.start(manual=True, worker=None)
        self.ready()
        self.server.put_mode = 'unknown'
        self.assertEqual(self.apply(), {'status': 'unknown', 'reason': 'putTransport'})
        self.assertEqual(self.prepare()['failure'], 'previousOutcomeUnknown')
        self.assertEqual(self.command('applyAuthorized', group='Main')['failure'], 'previousOutcomeUnknown')
        self.assertEqual(len(self.puts()), 1)

    def test_unicode_profile_filename_round_trips(self):
        self.file = self.profiles / '订阅-😀.yaml'
        self.file.write_bytes(self.snapshot)
        entries = [dict(uid='target', name='Synthetic', file=self.file.name, type='remote'),
                   dict(uid='other', name='Other', file='other.yaml', type='remote')]
        (self.root / 'profiles.yaml').write_text(
            json.dumps(dict(current='target', items=entries), ensure_ascii=False), encoding='utf-8')
        self.start()
        self.ready()
        self.assertEqual(self.apply()['status'], 'switched')
        self.assertEqual(len(self.puts()), 1)

    def test_manual_profile_retry_retains_original_global_winner(self):
        self.registry('other')
        self.start()
        self.assertEqual(self.prepare()['status'], 'manual')
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.puts(), [])
        self.registry('target')
        result = self.ready()
        groups = [group['name'] for group in result['groups']]
        self.assertEqual(groups, ['GLOBAL', 'Main'])
        self.assertEqual(self.apply()['status'], 'switched')
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.server.groups['Main']['now'], NODE)
        methods = [event[0] for event in self.server.events]
        self.assertEqual(methods, ['GET', 'GET', 'PUT', 'GET'])

    def test_explicit_group_and_noop(self):
        self.start()
        self.ready()
        self.assert_refused(self.apply('bad/name'), 'groupNotPrepared')
        self.ready()
        self.server.groups['Main']['now'] = NODE
        # Captured now must agree; refresh before confirming the no-op.
        self.ready()
        self.assertEqual(self.apply()['status'], 'noOp')
        self.assertEqual(self.puts(), [])

    def test_unauthorized_before_controller_dispatch_consumes_preparation(self):
        self.start()
        self.ready()
        events_before = list(self.server.events)
        result = self.command('applyUnauthorized', group='Main')
        self.assertEqual(result, {'status': 'notAttempted', 'failure': 'cancelled'})
        self.assertEqual(self.server.events, events_before)  # no additional GET or PUT
        self.assertEqual(self.puts(), [])
        self.assert_refused(self.command('applyAuthorized', group='Main'), 'preparationRequired')
        self.assertEqual(self.server.events, events_before)

    def test_explicit_authorization_allows_verified_switch(self):
        self.start()
        self.ready()
        self.assertEqual(self.command('applyAuthorized', group='Main'), {'status': 'switched'})
        self.assertEqual([method for method, _ in self.server.events], ['GET', 'GET', 'PUT', 'GET'])
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.server.groups['Main']['now'], NODE)

    def apply_revoked_during_delayed_get(self, before_release=None):
        arrived = threading.Event()
        release = threading.Event()

        def delay_get():
            arrived.set()
            if not release.wait(timeout=4):
                self.server.errors.append('synthetic GET was not released')

        self.server.hook = delay_get
        payload = json.dumps(dict(command='applyRevocable', group='Main')).encode() + b'\n'
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        try:
            self.assertTrue(arrived.wait(timeout=3), 'apply-time GET did not reach the mock server')
            # The Swift fixture changes a lock-protected gate and acknowledges
            # the revocation while this GET is still held by the mock server.
            self.process.stdin.write(b'revoke\n')
            self.process.stdin.flush()
            self.assertEqual(json.loads(self.process.stdout.readline()), {'status': 'revoked'})
            if before_release:
                before_release()
        finally:
            release.set()
        return json.loads(self.process.stdout.readline())

    def test_authorization_revoked_while_group_get_delayed_prevents_put(self):
        self.start()
        self.ready()
        result = self.apply_revoked_during_delayed_get()
        self.assertEqual(result, {'status': 'notAttempted', 'failure': 'cancelled'})
        self.assertEqual([method for method, _ in self.server.events], ['GET', 'GET'])
        self.assertEqual(self.puts(), [])
        self.assert_refused(self.command('applyAuthorized', group='Main'), 'preparationRequired')

    def test_environment_validation_precedes_final_authorization(self):
        self.start()
        self.ready()
        # Both revocation and a socket replacement happen during the GET. The
        # changed socket takes precedence because authorization is checked last.
        result = self.apply_revoked_during_delayed_get(before_release=self.replace_socket)
        self.assertEqual(result, {'status': 'notAttempted', 'failure': 'controllerChanged'})
        self.assertEqual([method for method, _ in self.server.events], ['GET', 'GET'])
        self.assertEqual(self.puts(), [])

    def test_stale_snapshot_or_registry_rejected(self):
        self.start()
        self.ready()
        self.file.write_bytes(self.snapshot.replace(b'synthetic', b'tampered!'))
        self.assert_refused(self.apply(), 'snapshotChanged')

    def test_target_path_and_uid_are_byte_exact(self):
        self.start(path=str(self.profiles / 'other.yaml'))
        self.assert_refused(self.prepare(), 'profileChanged')
        self.assertEqual(self.server.events, [])

    def test_current_selection_and_membership_change_refused(self):
        self.start()
        self.ready()
        self.server.groups['Main']['now'] = 'somebody-else'
        self.assert_refused(self.apply(), 'currentChanged')
        self.server.groups['Main']['now'] = 'before'
        self.ready()
        self.server.groups['Main']['all'] = ['before']
        self.assert_refused(self.apply(), 'nodeNotMember')

    def test_preput_guard_catches_profile_change_during_group_fetch(self):
        self.start()
        self.ready()
        self.server.hook = lambda: self.registry('other')
        self.assertEqual(self.apply()['status'], 'manual')
        self.assertEqual(self.puts(), [])

    def test_preput_guard_catches_bytes_change_during_group_fetch(self):
        self.start()
        self.ready()
        self.server.hook = lambda: self.file.write_bytes(b'changed synthetic profile')
        self.assert_refused(self.apply(), 'snapshotChanged')

    def test_preput_guard_catches_socket_change_during_group_fetch(self):
        self.start()
        self.ready()
        self.server.hook = self.replace_socket
        self.assert_refused(self.apply(), 'controllerChanged')

    def test_catalogue_change_is_revalidated_before_return(self):
        self.start()
        self.server.hook_catalog = lambda: self.file.write_bytes(b'different synthetic profile')
        self.assert_refused(self.prepare(), 'snapshotChanged')

    def test_replaced_or_non_socket_rejected(self):
        self.start()
        self.ready()
        self.replace_socket()
        self.assert_refused(self.apply(), 'controllerChanged')
        # The identity is not silently reset by preparing again.
        self.assert_refused(self.prepare(), 'controllerChanged')

    def test_unknown_put_is_not_retried_or_reprepared(self):
        self.start()
        self.ready()
        self.server.put_mode = 'unknown'
        result = self.command('applyAuthorized', group='Main')
        self.assertEqual(result, {'status': 'unknown', 'reason': 'putTransport'})
        self.assertEqual(len(self.puts()), 1)
        # Revocation must not clear the sticky unknown-outcome state, even
        # though a subsequently unauthorized call itself reports cancelled.
        self.assertEqual(self.command('applyUnauthorized', group='Main')['failure'], 'cancelled')
        self.assertEqual(self.command('applyAuthorized', group='Main')['failure'], 'previousOutcomeUnknown')
        self.assertEqual(self.prepare()['failure'], 'previousOutcomeUnknown')
        self.assertEqual(len(self.puts()), 1)

    def test_input_validation_uses_worker_node_bytes_and_score(self):
        for overrides in [dict(node='Synthetic node e\u0301'),
                          dict(worker=worker(successes=2)),
                          dict(worker=worker(latency=0)),
                          dict(worker=worker(complete=False, speed=0)),
                          dict(node='剩余流量: 10 GB', worker=worker(name='剩余流量: 10 GB')),
                          dict(snapshot=base64.b64encode(b'').decode()),
                          dict(snapshot=base64.b64encode(b'x' * (1024 * 1024 + 1)).decode())]:
            with self.subTest(case=list(overrides)):
                self.start(**overrides)
                self.assert_refused(self.prepare(), 'invalidInput')
                self.process.stdin.write(b'{"command":"quit"}\n')
                self.process.stdin.flush()
                out, err = self.process.communicate(timeout=10)
                self.assertEqual((self.process.returncode, out, err), (0, b'', b''))
                self.process = None
        self.assertEqual(self.server.events, [])

    def test_socket_symlink_or_missing_rejected_before_get(self):
        self.start(socket=str(self.case / 'missing'))
        self.assert_refused(self.prepare(), 'controllerUnavailable')
        self.assertEqual(self.server.events, [])

    def test_socket_symlink_and_regular_file_refused(self):
        link = self.case / 'link'
        link.symlink_to(self.sock)
        self.start(socket=str(link))
        self.assert_refused(self.prepare(), 'controllerUnavailable')
        self.assertEqual(self.server.events, [])
        self.process.stdin.write(b'{"command":"quit"}\n')
        self.process.stdin.flush()
        out, err = self.process.communicate(timeout=10)
        self.assertEqual((self.process.returncode, out, err), (0, b'', b''))
        self.process = None
        regular = self.case / 'not-socket'
        regular.write_bytes(b'not a socket')
        self.start(socket=str(regular))
        self.assert_refused(self.prepare(), 'controllerUnavailable')
        self.assertEqual(self.server.events, [])

    def test_changed_profiles_directory_is_refused(self):
        self.start()
        self.ready()
        # Rename only the exact synthetic profiles directory within this case.
        self.assertEqual(self.profiles.parent, self.root)
        self.assertEqual(self.root.parent, self.case)
        self.profiles.rename(self.root / 'old-profiles')
        self.profiles.mkdir(mode=0o700)
        self.assert_refused(self.apply(), 'profileChanged')

    def test_registry_uid_unicode_equivalence_does_not_authorize(self):
        uid = 'uid-é'
        entries = [dict(uid=uid, name='target', file='target.yaml', type='remote')]
        (self.root / 'profiles.yaml').write_text(json.dumps(dict(current=uid, items=entries)), encoding='utf-8')
        self.start(uid='uid-e\u0301')
        self.assert_refused(self.prepare(), 'profileChanged')
        self.assertEqual(self.server.events, [])

    def test_changed_group_kind_and_name_refused(self):
        self.start()
        self.ready()
        self.server.groups['Main']['type'] = 'URLTest'
        self.assert_refused(self.apply(), 'notSelector')
        self.server.groups['Main']['type'] = 'Selector'
        self.ready()
        self.server.groups['Main']['name'] = 'Different'
        self.assert_refused(self.apply(), 'invalidResponse')

    def test_readback_mismatch_is_unknown_without_retry(self):
        self.start()
        self.ready()
        self.server.put_mode = 'mismatch'
        self.assertEqual(self.apply(), {'status': 'unknown', 'reason': 'verificationMismatch'})
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.apply()['failure'], 'previousOutcomeUnknown')
        self.assertEqual(self.prepare()['failure'], 'previousOutcomeUnknown')
        self.assertEqual(len(self.puts()), 1)

    def test_profile_symlink_rejected_without_reading_target(self):
        self.file.unlink()
        self.file.symlink_to('other.yaml')
        self.start()
        self.assert_refused(self.prepare())
        self.assertEqual(self.server.events, [])

    def test_success_does_not_modify_synthetic_profile_files(self):
        before = [hashlib.sha256(p.read_bytes()).digest() for p in [self.file, self.root / 'profiles.yaml']]
        self.start()
        self.ready()
        self.assertEqual(self.apply()['status'], 'switched')
        after = [hashlib.sha256(p.read_bytes()).digest() for p in [self.file, self.root / 'profiles.yaml']]
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main(verbosity=2)
