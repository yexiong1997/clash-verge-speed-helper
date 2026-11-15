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
MAIN = '主分组 ü 😀'
NFC_NODE = '\u00e9'
NFD_NODE = 'e\u0301'
BYTE_CATALOGUE_REQUESTS = 15

# Distinct ASCII group keys avoid JSON object-key canonicalization, while the
# member arrays deliberately preserve both byte-distinct Unicode spellings.
BYTE_CATALOGUE = {'proxies': {
    'ByteBoth': dict(name='ByteBoth', type='Selector', now='before', all=['before', NFC_NODE, NFD_NODE]),
    'ByteNFCOnly': dict(name='ByteNFCOnly', type='Selector', now='before', all=['before', NFC_NODE]),
    'ByteNFDOnly': dict(name='ByteNFDOnly', type='Selector', now='before', all=['before', NFD_NODE]),
    'ByteNeither': dict(name='ByteNeither', type='Selector', now='before', all=['before', 'unrelated']),
    'ByteNotSelector': dict(name='ByteNotSelector', type='URLTest', now='before', all=[NFC_NODE, NFD_NODE]),
    'byte/unsafe': dict(name='byte/unsafe', type='Selector', now='before', all=[NFC_NODE, NFD_NODE]),
}}


def raw_group(name, extra=b''):
    # Extra fields are deliberately appended as raw bytes: json.dumps cannot
    # represent duplicate object members or malformed UTF-8.
    prefix = json.dumps(dict(name=name, type='Selector', now='before', all=['before', 'after']),
                        ensure_ascii=False).encode('utf-8')
    return prefix[:-1] + extra + b'}'


CATALOGUE_CASES = [
    b'{"proxies":{},"proxies":{}}',
    b'{"proxies":{"same":{},"same":{}}}',
    b'{"proxies":{"same":{},"\\u0073ame":{}}}',
    b'{"proxies":{"a":{},"\\uFEFFa":{}}}',  # Foundation strips a leading BOM from keys.
    '{"proxies":{"é":{},"e\\u0301":{}}}'.encode('utf-8'),
    b'{"proxies":{"ignored":{"type":"URLTest","extra":[{"x":1,"x":2}]}}}',
    b'{"proxies":{},"ignored":"\\xff"}'.replace(b'\\xff', bytes([255])),
    b'{"proxies":{},"ignored":"\\uD800"}',
    b'{"proxies":{},"ignored":01}',
    b'{"proxies":{},"ignored":true} trailing',
    b'{"proxies":{},"ignored":' + b'[' * 64 + b'0' + b']' * 64 + b'}',
]

GROUP_CASES = {
    'DuplicateName': raw_group('DuplicateName', b',"name":"DuplicateName"'),
    'DuplicateType': raw_group('DuplicateType', b',"type":"Selector"'),
    'DuplicateNow': raw_group('DuplicateNow', b',"now":"before"'),
    'DuplicateAll': raw_group('DuplicateAll', b',"all":["before","after"]'),
    'DuplicateEscaped': raw_group('DuplicateEscaped', b',"n\\u0061me":"DuplicateEscaped"'),
    'DuplicateNested': raw_group('DuplicateNested', b',"extra":[{"same":1,"same":2}]'),
    'DuplicateCanonical': raw_group('DuplicateCanonical', ',"extra":{"é":1,"e\\u0301":2}'.encode()),
    'DuplicateSurrogate': raw_group('DuplicateSurrogate', ',"extra":{"😀":1,"\\ud83d\\ude00":2}'.encode()),
    'MalformedUTF8': raw_group('MalformedUTF8', b',"extra":"' + bytes([255]) + b'"'),
    'MalformedSurrogate': raw_group('MalformedSurrogate', b',"extra":"\\udc00"'),
    'MalformedNumber': raw_group('MalformedNumber', b',"extra":1e+'),
    'TooDeep': raw_group('TooDeep', b',"extra":' + b'[' * 64 + b'0' + b']' * 64),
}

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
        for _ in 0..<Int(data["catalogueBadCount"]!)! {
            let rejected: Result<[ProxyGroup], SwitchFailure> = wait { ctl.eligibleGroups(nodeNames: [node], completion: $0) }
            guard case .failure(.invalidResponse) = rejected else { fatalError("ambiguous catalogue accepted") }
        }
        // Swift's String/Set equality is canonically equivalent, so compare
        // returned names and members as byte arrays rather than String values.
        let nfc = "\u{00E9}", nfd = "e\u{0301}"
        precondition(nfc == nfd && !nfc.utf8.elementsEqual(nfd.utf8))
        let nfcGroups = ["ByteBoth", "ByteNFCOnly"]
        let nfdGroups = ["ByteBoth", "ByteNFDOnly"]
        let bothGroups = ["ByteBoth", "ByteNFCOnly", "ByteNFDOnly"]
        func checkGroups(_ result: Result<[ProxyGroup], SwitchFailure>, _ expected: [String], _ label: String) {
            guard case .success(let groups) = result else { fatalError("catalogue failed: \(label)") }
            precondition(groups.map { Array($0.name.utf8) } == expected.map { Array($0.utf8) },
                         "wrong byte-exact catalogue groups: \(label)")
            if let both = groups.first(where: { $0.name.utf8.elementsEqual("ByteBoth".utf8) }) {
                precondition(both.all.map { Array($0.utf8) } == ["before", nfc, nfd].map { Array($0.utf8) },
                             "catalogue changed byte-distinct members: \(label)")
            }
        }
        func checkArray(_ nodes: [String], _ expected: [String], _ label: String) {
            let result: Result<[ProxyGroup], SwitchFailure> = wait {
                ctl.eligibleGroupsPreservingNodeBytes(nodeNames: nodes, completion: $0)
            }
            checkGroups(result, expected, label)
        }
        func checkLegacy(_ nodes: Set<String>, _ expected: [String], _ label: String) {
            let result: Result<[ProxyGroup], SwitchFailure> = wait {
                ctl.eligibleGroups(nodeNames: nodes, completion: $0)
            }
            checkGroups(result, expected, label)
        }
        checkArray([nfc], nfcGroups, "NFC only")
        checkArray([nfd], nfdGroups, "NFD only")
        checkArray([nfc, nfd], bothGroups, "NFC then NFD")
        checkArray([nfd, nfc], bothGroups, "NFD then NFC")
        checkArray([nfc, nfd, nfc, nfd], bothGroups, "both with duplicates")
        checkArray([nfc, nfc], nfcGroups, "duplicate NFC")
        checkArray([nfd, nfd], nfdGroups, "duplicate NFD")
        checkArray([], [], "empty array")
        checkArray([""], [], "empty node")
        checkArray(["", ""], [], "duplicate empty node")
        checkArray(["missing"], [], "no match")
        checkLegacy([nfc], nfcGroups, "legacy NFC")
        checkLegacy([nfd], nfdGroups, "legacy NFD")
        let legacyBoth = Set([nfc, nfd])
        precondition(legacyBoth.count == 1, "Swift Set must collapse canonical equivalents")
        let retained = legacyBoth.first!
        let retainedNFC = retained.utf8.elementsEqual(nfc.utf8)
        precondition(retainedNFC || retained.utf8.elementsEqual(nfd.utf8))
        checkLegacy(legacyBoth, retainedNFC ? nfcGroups : nfdGroups, "legacy retained spelling")
        checkLegacy([], [], "legacy empty set")
        for group in data["badGroups"]!.split(separator: ",").map(String.init) {
            precondition(switched(group) == .notAttempted(.invalidResponse), "unsafe group JSON accepted: \(group)")
        }
        precondition(switched("Oversized") == .notAttempted(.transportError))
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
        precondition(switched("VerificationAmbiguous") == .outcomeUnknown(.verificationFailed))
        for name in ["DepthLimit", "ValidScalars"] {
            let valid: Result<ProxyGroup, SwitchFailure> = wait { ctl.group(name, completion: $0) }
            guard case .success = valid else { fatalError("valid JSON rejected: \(name)") }
        }
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
        func probe(_ target: String, timeoutMs: Int = 3500, testURL: String = "http://www.gstatic.com/generate_204") -> Result<Double, SwitchFailure> {
            wait { ctl.probeNodeDelay(node: target, timeoutMs: timeoutMs, testURL: testURL, completion: $0) }
        }
        guard case .success(let dInt) = probe("ProbeOK"), abs(dInt - 123.0) < 1e-9 else {
            fatalError("probe integer delay failed")
        }
        guard case .success(let dFloat) = probe(node, timeoutMs: 1800, testURL: "https://cp.cloudflare.com/generate_204"),
              abs(dFloat - 45.5) < 1e-9 else {
            fatalError("probe float/encoded node delay failed")
        }
        guard case .success(let dUtf8) = probe("Probe ü/é"), abs(dUtf8 - 88.25) < 1e-9 else {
            fatalError("probe utf8/slash node delay failed")
        }
        for statusName in ["Probe408", "Probe500", "Probe502", "Probe503", "Probe504", "ProbeRedirect"] {
            guard case .failure(.unexpectedHTTPStatus) = probe(statusName) else {
                fatalError("probe unexpected status not rejected: \(statusName)")
            }
        }
        for badDelayName in ["ProbeDup", "ProbeDupEsc", "ProbeMissing", "ProbeZero", "ProbeNegative", "ProbeTooLarge", "ProbeOversized", "ProbeBadJSON"] {
            guard case .failure(.invalidResponse) = probe(badDelayName) else {
                fatalError("probe invalid response accepted: \(badDelayName)")
            }
        }
        guard case .failure(.transportError) = probe("ProbeDrop") else {
            fatalError("probe transport drop not reported as transportError")
        }
        for badNode in ["", ".", "..", "bad\nnode"] {
            guard case .failure(.invalidNode) = probe(badNode) else {
                fatalError("invalid probe node accepted")
            }
        }
        for badTimeout in [99, 30_001] {
            guard case .failure(.invalidNode) = probe("ProbeOK", timeoutMs: badTimeout) else {
                fatalError("invalid probe timeout accepted")
            }
        }
        for badURL in ["", "ftp://example.com", "http://bad\nurl"] {
            guard case .failure(.invalidNode) = probe("ProbeOK", testURL: badURL) else {
                fatalError("invalid probe URL accepted")
            }
        }
        let badSettingsProbe: Result<Double, SwitchFailure> = wait { invalid.probeNodeDelay(node: "ProbeOK", completion: $0) }
        guard case .failure(.invalidSettings) = badSettingsProbe else {
            fatalError("invalid probe settings accepted")
        }
        print("Swift callbacks, guards, probeNodeDelay, verified and uncertain outcomes: OK")
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
        self.catalogue_index = 0
        for name in ['Mismatch', 'Nameé', 'NotSelector', 'NoMember', 'MemberBytes', 'CurrentBytes',
                     'Concurrent', 'NoOp', 'GuardReject', 'GuardAccept', MAIN, 'UnknownStatus', 'UnknownTransport',
                     'VerificationMismatch', 'VerificationFailure', 'VerificationAmbiguous']:
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
        if data:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # curl rejects the declared oversized response without reading it.
                if len(data) <= 8 * 1024 * 1024:
                    raise
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
        path_only, sep, query = self.path.partition('?')
        if sep and path_only.startswith('/proxies/') and path_only.endswith('/delay'):
            _ = self.checked_name()
            raw_node = path_only[len('/proxies/'):-len('/delay')]
            if '/' in raw_node or not raw_node:
                self.server.errors.append('malformed probe route')
                return self.response(400)
            try:
                node_name = urllib.parse.unquote(raw_node, encoding='utf-8', errors='strict')
            except ValueError:
                self.server.errors.append('bad UTF-8 probe node')
                return self.response(400)
            with self.server.lock:
                self.server.events.append(('PROBE', node_name, self.path))
            if node_name == 'ProbeOK':
                if query != 'timeout=3500&url=http%3A%2F%2Fwww.gstatic.com%2Fgenerate_204':
                    self.server.errors.append('unexpected default probe query')
                return self.response(200, {'delay': 123})
            if node_name == NODE:
                if query != 'timeout=1800&url=https%3A%2F%2Fcp.cloudflare.com%2Fgenerate_204':
                    self.server.errors.append('unexpected custom probe query')
                return self.response(200, {'delay': 45.5})
            if node_name == 'Probe ü/é':
                return self.response(200, {'delay': 88.25})
            status_map = {'Probe408': 408, 'Probe500': 500, 'Probe502': 502, 'Probe503': 503, 'Probe504': 504}
            if node_name in status_map:
                return self.response(status_map[node_name])
            if node_name == 'ProbeRedirect':
                return self.response(302, location='http://localhost/proxies/RedirectTarget')
            if node_name == 'ProbeDup':
                return self.response(200, b'{"delay":100,"delay":200}')
            if node_name == 'ProbeDupEsc':
                return self.response(200, b'{"delay":100,"d\u0065lay":200}')
            if node_name == 'ProbeMissing':
                return self.response(200, b'{}')
            if node_name == 'ProbeZero':
                return self.response(200, b'{"delay":0}')
            if node_name == 'ProbeNegative':
                return self.response(200, b'{"delay":-5}')
            if node_name == 'ProbeTooLarge':
                return self.response(200, b'{"delay":60001}')
            if node_name == 'ProbeOversized':
                return self.response(200, b'{"delay":10,"pad":"' + b'x' * 4100 + b'"}')
            if node_name == 'ProbeBadJSON':
                return self.response(200, b'{not json')
            if node_name == 'ProbeDrop':
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.server.errors.append('unexpected probe target: ' + node_name)
            return self.response(404)
        name = self.checked_name()
        with self.server.lock:
            self.server.events.append(('GET', name, self.path))
            prior_put = any(x[0] == 'PUT' and x[1] == name for x in self.server.events)
        if self.path == '/proxies':
            with self.server.lock:
                index = self.server.catalogue_index
                self.server.catalogue_index += 1
            if 0 < index <= len(CATALOGUE_CASES):
                return self.response(200, CATALOGUE_CASES[index - 1])
            if index > len(CATALOGUE_CASES):
                if index > len(CATALOGUE_CASES) + BYTE_CATALOGUE_REQUESTS:
                    self.server.errors.append('unexpected byte-catalogue request')
                    return self.response(500)
                return self.response(200, BYTE_CATALOGUE)
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
        if name == 'VerificationAmbiguous' and prior_put:
            return self.response(200, raw_group(name, b',"now":"after"'))
        if name in GROUP_CASES:
            return self.response(200, GROUP_CASES[name])
        if name == 'Oversized':
            return self.response(200, raw_group(name, b',"padding":"' + b'x' * (8 * 1024 * 1024) + b'"'))
        if name == 'DepthLimit':
            return self.response(200, raw_group(name, b',"extra":' + b'[' * 63 + b'0' + b']' * 63))
        if name == 'ValidScalars':
            return self.response(200, raw_group(name, b',"extra":[-1.25e+2,0,-0,1E-2,true,false,null,{"x":1},{"x":2},"\\b\\f\\n\\r\\t\\\"\\\\\\/\\u0000"]'))
        group = self.server.groups.get(name)
        if group is None:
            return self.response(404)
        group = dict(group)
        if name == 'Mismatch':
            group['name'] = 'different'
        elif name == 'Nameé':
            group['name'] = 'Namee\u0301'
        if name == MAIN:
            # Exercise valid surrogate-pair escapes as well as raw UTF-8 in the catalogue.
            return self.response(200, json.dumps(group, ensure_ascii=True).encode('utf-8'))
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
        subprocess.run(['swiftc', '-swift-version', '5', str(PROJECT / 'Sources' / 'SwitchController.swift'),
                        str(source), '-o', str(binary)], check=True)
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
            payload = json.dumps(dict(socket=sock, secret=SECRET, node=NODE, main=MAIN,
                                      catalogueBadCount=str(len(CATALOGUE_CASES)),
                                      badGroups=','.join(GROUP_CASES))).encode()
            result = subprocess.run([str(binary)], input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=100, env=env)
            if result.returncode:
                print('Swift synthetic driver failed (diagnostics suppressed).')
                raise SystemExit(1)
            print(result.stdout.decode().strip())
            assert not server.errors, 'synthetic server detected an invariant violation'
            assert server.inspected == {'GET', 'PUT'}, 'argv privacy checks did not run'
            puts = [event[1] for event in server.events if event[0] == 'PUT']
            assert puts == ['GuardAccept', MAIN, 'UnknownStatus', 'UnknownTransport',
                            'VerificationMismatch', 'VerificationFailure', 'VerificationAmbiguous']
            assert not set(puts).intersection(GROUP_CASES), 'ambiguous group response caused a PUT'
            assert server.catalogue_index == len(CATALOGUE_CASES) + 1 + BYTE_CATALOGUE_REQUESTS, \
                'not all catalogue and byte-identity cases were exercised'
            assert all(event[1] != 'RedirectTarget' for event in server.events), 'redirect was followed'
            main_paths = [p for method, name, p in server.events if name == MAIN]
            assert main_paths and all(p == '/proxies/' + urllib.parse.quote(MAIN, safe='-._~') for p in main_paths)
            probe_paths = [(name, p) for method, name, p in server.events if method == 'PROBE']
            assert len(probe_paths) == 18, f'unexpected probe request count: {len(probe_paths)}'
            assert ('Probe ü/é', '/proxies/Probe%20%C3%BC%2F%C3%A9/delay?timeout=3500&url=http%3A%2F%2Fwww.gstatic.com%2Fgenerate_204') in probe_paths
            assert (NODE, '/proxies/' + urllib.parse.quote(NODE, safe='-._~') + '/delay?timeout=1800&url=https%3A%2F%2Fcp.cloudflare.com%2Fgenerate_204') in probe_paths
            print('Private synthetic socket, no unsafe PUT/retry, exact route, probeNodeDelay, argv secrecy and no redirects: OK')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    main()
