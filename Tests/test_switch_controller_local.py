#!/usr/bin/env python3
"""Synthetic Unix-socket-only tests; never read or contact a live controller."""
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
SECRET = 'SYNTH_TOKEN_a7\\b"c'
NODE = 'SYNTH_BODY_4b"c\\d'
MAIN = '主分组 ü'

# The test driver receives its synthetic socket and token on stdin, not argv.
SWIFT = r'''
import Foundation

@main struct ControllerLocalTest {
    static func wait<T>(_ action: (@escaping (T) -> Void) -> Void) -> T {
        var result: T?
        action { value in
            precondition(Thread.isMainThread, "callback must be on main")
            result = value
        }
        let deadline = Date().addingTimeInterval(20)
        while result == nil && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        guard let value = result else { fatalError("callback timeout") }
        return value
    }
    static func main() throws {
        let input = FileHandle.standardInput.readDataToEndOfFile()
        let data = try JSONSerialization.jsonObject(with: input) as! [String: String]
        let socket = data["socket"]!, secret = data["secret"]!, node = data["node"]!, main = data["main"]!
        let ctl = ClashController(settings: ControllerSettings(socket: socket, secret: secret))
        func switched(_ group: String, _ node: String = "after", _ before: String = "before") -> SwitchOutcome {
            wait { ctl.switchAndVerify(group: group, node: node, expectedCurrent: before, prePutGuard: { true }, completion: $0) }
        }
        let list: Result<[ProxyGroup], SwitchFailure> = wait { ctl.eligibleGroups(nodeNames: [node], completion: $0) }
        guard case .success(let groups) = list, groups.count == 1,
              groups[0].name.utf8.elementsEqual(main.utf8) else { fatalError("catalogue failure") }
        let get: Result<ProxyGroup, SwitchFailure> = wait { ctl.group(main, completion: $0) }
        guard case .success(let item) = get, item.name.utf8.elementsEqual(main.utf8)
        else { fatalError("exact group failure") }
        for bad in ["", ".", "..", "x/y", "x\\y", "x%2Fy", "x?y", "x#y", "x;y", "x\ny", "x\u{7f}y"] {
            guard switched(bad) == .notAttempted(.invalidGroupName) else { fatalError("unsafe group accepted") }
        }
        precondition(switched("Mismatch") == .notAttempted(.invalidResponse))
        precondition(switched("Nameé") == .notAttempted(.invalidResponse))
        precondition(switched("NotSelector") == .notAttempted(.notSelector))
        precondition(switched("NoMember") == .notAttempted(.nodeNotMember))
        precondition(switched("MemberBytes", "no\u{301}de") == .notAttempted(.nodeNotMember))
        precondition(switched("CurrentBytes", "after", "nóde") == .notAttempted(.currentChanged))
        precondition(switched("Concurrent") == .notAttempted(.currentChanged))
        precondition(switched("NoOp", "already", "already") == .confirmedNoOp)
        let refused: SwitchOutcome = wait { ctl.switchAndVerify(group: "GuardReject", node: "after", expectedCurrent: "before", prePutGuard: { false }, completion: $0) }
        precondition(refused == .notAttempted(.environmentChanged))
        let allowed: SwitchOutcome = wait { ctl.switchAndVerify(group: "GuardAccept", node: "after", expectedCurrent: "before", prePutGuard: { true }, completion: $0) }
        precondition(allowed == .switched)
        precondition(switched(main, node) == .switched)
        precondition(switched("UnknownStatus") == .outcomeUnknown(.putUnexpectedStatus))
        precondition(switched("UnknownTransport") == .outcomeUnknown(.putTransport))
        precondition(switched("VerificationMismatch") == .outcomeUnknown(.verificationMismatch))
        precondition(switched("VerificationFailure") == .outcomeUnknown(.verificationFailed))
        let redir: Result<ProxyGroup, SwitchFailure> = wait { ctl.group("Redirect", completion: $0) }
        guard case .failure(.unexpectedHTTPStatus) = redir else { fatalError("redirect followed") }
        let badJSON: Result<ProxyGroup, SwitchFailure> = wait { ctl.group("BadJSON", completion: $0) }
        guard case .failure(.invalidResponse) = badJSON else { fatalError("bad JSON accepted") }
        let invalid = ClashController(settings: ControllerSettings(socket: "relative", secret: secret))
        let rejected: SwitchOutcome = wait { invalid.switchAndVerify(group: "x", node: "y", expectedCurrent: "z", prePutGuard: { true }, completion: $0) }
        precondition(rejected == .notAttempted(.invalidSettings))
        let control = ClashController(settings: ControllerSettings(socket: socket + "\n", secret: secret))
        let rejectedControl: SwitchOutcome = wait { control.switchAndVerify(group: "x", node: "y", expectedCurrent: "z", prePutGuard: { true }, completion: $0) }
        precondition(rejectedControl == .notAttempted(.invalidSettings))
        let badSecret = ClashController(settings: ControllerSettings(socket: socket, secret: "x\ny"))
        let rejectedSecret: Result<ProxyGroup, SwitchFailure> = wait { badSecret.group("x", completion: $0) }
        guard case .failure(.invalidSettings) = rejectedSecret else { fatalError("bad secret accepted") }
        precondition(ControllerSettings(socket: "/é", secret: "x") != ControllerSettings(socket: "/e\u{301}", secret: "x"))
        for failure: SwitchFailure in [.invalidSettings, .invalidGroupName, .invalidNode, .transportError,
            .unexpectedHTTPStatus, .invalidResponse, .notSelector, .nodeNotMember, .currentChanged, .environmentChanged] {
            let text = "\(failure) \(failure.localizedDescription)"
            precondition(!text.contains(secret) && !text.contains(socket) && !text.contains(node))
        }
        print("Swift callbacks, guards, verified and uncertain outcomes: OK")
    }
}
'''

class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    def __init__(self, path):
        self.events = []
        self.lock = threading.Lock()
        self.inspected = set()
        self.errors = []
        self.groups = {}
        for name in ['Mismatch', 'Nameé', 'NotSelector', 'NoMember', 'MemberBytes', 'CurrentBytes',
                     'Concurrent', 'NoOp', 'GuardReject', 'GuardAccept', MAIN, 'UnknownStatus', 'UnknownTransport',
                     'VerificationMismatch', 'VerificationFailure']:
            self.groups[name] = dict(name=name, type='Selector', now='before', all=['before', 'after'])
        self.groups[MAIN]['all'] = ['before', NODE]
        self.groups['NotSelector']['type'] = 'URLTest'
        self.groups['NoMember']['all'] = ['before']
        self.groups['MemberBytes']['all'] = ['before', 'nóde']
        self.groups['CurrentBytes']['now'] = 'no\u0301de'
        self.groups['Concurrent']['now'] = 'someone-else'
        self.groups['NoOp']['now'] = 'already'
        self.groups['NoOp']['all'] = ['already']
        super().__init__(path, Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def response(self, status, obj=None, location=None):
        data = b'' if obj is None else (obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode())
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        if location:
            self.send_header('Location', location)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True
    def checked_name(self):
        if self.headers.get('Authorization') != 'Bearer ' + SECRET:
            self.server.errors.append('authorization mismatch')
        # Inspect curl while it is waiting for this synthetic response. Only
        # tests process argv; the token/body themselves remain synthetic.
        if self.command not in self.server.inspected:
            self.server.inspected.add(self.command)
            ps = subprocess.run(['/bin/ps', '-axww', '-o', 'command='], capture_output=True, text=True, check=True).stdout
            curl_lines = [line for line in ps.splitlines() if '/usr/bin/curl' in line]
            if not curl_lines or any('SYNTH_TOKEN_a7' in line or 'SYNTH_BODY_4b' in line for line in curl_lines):
                self.server.errors.append('curl argv privacy check failed')
        if not self.path.startswith('/proxies/'):
            return None
        try:
            return urllib.parse.unquote(self.path[len('/proxies/'):], encoding='utf-8', errors='strict')
        except ValueError:
            self.server.errors.append('bad UTF-8 route')
            return None
    def do_GET(self):
        name = self.checked_name()
        with self.server.lock:
            self.server.events.append(('GET', name, self.path))
            prior_put = any(x[0] == 'PUT' and x[1] == name for x in self.server.events)
        if self.path == '/proxies':
            safe = self.server.groups[MAIN]
            catalogue = {MAIN: safe, 'bad/name': dict(name='bad/name', type='Selector', now='before', all=[NODE]),
                         'auto': dict(name='auto', type='URLTest', now='before', all=[NODE]),
                         'other': dict(name='other', type='Selector', now='before', all=['unrelated'])}
            return self.response(200, {'proxies': catalogue})
        if name == 'Redirect':
            return self.response(302, location='http://localhost/proxies/RedirectTarget')
        if name == 'BadJSON':
            return self.response(200, b'{not json')
        if name == 'VerificationFailure' and prior_put:
            return self.response(500)
        group = self.server.groups.get(name)
        if group is None:
            return self.response(404)
        group = dict(group)
        if name == 'Mismatch':
            group['name'] = 'different'
        elif name == 'Nameé':
            group['name'] = 'Namee\u0301'
        self.response(200, group)
    def do_PUT(self):
        name = self.checked_name()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        except (ValueError, json.JSONDecodeError):
            self.server.errors.append('malformed synthetic JSON body')
            return self.response(400)
        if set(body) != {'name'}:
            self.server.errors.append('unexpected synthetic PUT body')
        with self.server.lock:
            self.server.events.append(('PUT', name, self.path))
            group = self.server.groups.get(name)
            if group is None or body.get('name') not in group['all']:
                self.server.errors.append('unexpected synthetic PUT target')
            elif name != 'VerificationMismatch':
                group['now'] = body['name']
        if name == 'UnknownStatus':
            return self.response(500)
        if name == 'UnknownTransport':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.response(204)


def main():
    # Keep the synthetic Unix socket short enough for Darwin sockaddr_un.
    with tempfile.TemporaryDirectory(prefix='clash-controller-test-', dir='/private/tmp') as folder:
        root = pathlib.Path(folder)
        assert (root.stat().st_mode & 0o077) == 0, 'synthetic temp directory must be private'
        sock = str(root / 'api.sock')
        # Darwin sockaddr_un is short: use a private /tmp directory if needed.
        assert len(os.fsencode(sock)) < 100, 'temporary socket path too long'
        source = root / 'ControllerLocalTest.swift'
        source.write_text(SWIFT, encoding='utf-8')
        binary = root / 'controller-test'
        subprocess.run(['swiftc', str(PROJECT / 'Sources' / 'SwitchController.swift'), str(source), '-o', str(binary)], check=True)
        home = root / 'home'
        home.mkdir(mode=0o700)
        (home / '.curlrc').write_text('request = "POST"\nlocation\n', encoding='utf-8')
        env = os.environ.copy()
        env.update(HOME=str(home), CURL_HOME=str(home), http_proxy='http://127.0.0.1:1', HTTP_PROXY='http://127.0.0.1:1',
                   ALL_PROXY='http://127.0.0.1:1', no_proxy='', NO_PROXY='')
        server = Server(sock)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps(dict(socket=sock, secret=SECRET, node=NODE, main=MAIN)).encode()
            result = subprocess.run([str(binary)], input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=100, env=env)
            if result.returncode:
                print('Swift synthetic driver failed (diagnostics suppressed).')
                raise SystemExit(1)
            print(result.stdout.decode().strip())
            assert not server.errors, 'synthetic server detected an invariant violation'
            assert server.inspected == {'GET', 'PUT'}, 'argv privacy checks did not run'
            puts = [event[1] for event in server.events if event[0] == 'PUT']
            assert puts == ['GuardAccept', MAIN, 'UnknownStatus', 'UnknownTransport', 'VerificationMismatch', 'VerificationFailure']
            assert all(event[1] != 'RedirectTarget' for event in server.events), 'redirect was followed'
            main_paths = [p for method, name, p in server.events if name == MAIN]
            assert main_paths and all(p == '/proxies/' + urllib.parse.quote(MAIN, safe='-._~') for p in main_paths)
            print('Private synthetic socket, no unsafe PUT/retry, exact route, argv secrecy and no redirects: OK')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    main()
