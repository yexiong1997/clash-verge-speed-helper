import Foundation
import Darwin

// This file is independent of FastHelper.swift. Public operations run off the
// main thread and always dispatch their completions asynchronously to main.

public struct ControllerSettings: Equatable, Sendable {
    public let socket: String
    public let secret: String
    public init(socket: String, secret: String) { self.socket = socket; self.secret = secret }
    // Both socket paths and bearer tokens must be compared as bytes, not with
    // Swift String's Unicode canonical-equivalence semantics.
    public static func == (lhs: ControllerSettings, rhs: ControllerSettings) -> Bool {
        lhs.socket.utf8.elementsEqual(rhs.socket.utf8) && lhs.secret.utf8.elementsEqual(rhs.secret.utf8)
    }
}

public struct ProxyGroup: Sendable {
    public let name: String
    public let now: String
    public let all: [String]
    public init(name: String, now: String, all: [String]) { self.name = name; self.now = now; self.all = all }
}

// Intentionally no associated data: errors never include paths, input details,
// underlying errors, raw HTTP responses, or secrets.
public enum SwitchFailure: Error, Equatable, LocalizedError {
    case invalidSettings, invalidGroupName, invalidNode, transportError, unexpectedHTTPStatus
    case invalidResponse, notSelector, nodeNotMember, currentChanged, environmentChanged
    public var errorDescription: String? {
        switch self {
        case .invalidSettings: return "The local controller settings are invalid."
        case .invalidGroupName: return "The group name is not safe for a controller request."
        case .invalidNode: return "The requested selection is invalid."
        case .transportError: return "The local controller request failed."
        case .unexpectedHTTPStatus: return "The local controller returned an unexpected status."
        case .invalidResponse: return "The local controller response was invalid."
        case .notSelector: return "The group is not a selectable group."
        case .nodeNotMember: return "The requested selection is not a direct member."
        case .currentChanged: return "The current selection has changed."
        case .environmentChanged: return "The active profile or local controller has changed."
        }
    }
}

public enum SwitchUncertainty: Equatable, Sendable {
    case putTransport, putUnexpectedStatus, verificationFailed, verificationMismatch
}
public enum SwitchOutcome: Equatable, Sendable {
    case confirmedNoOp                       // Already current; no PUT.
    case switched                            // PUT 204 followed by confirming GET.
    case notAttempted(SwitchFailure)         // Definitely no PUT.
    case outcomeUnknown(SwitchUncertainty)   // PUT may have occurred; do not retry automatically.
}

// The callback is only invoked on the main queue; it is never accessed by the
// worker queue. This wrapper allows the public API to accept ordinary UI
// callbacks while confining their execution to main.
private final class MainCompletion<Value: Sendable>: @unchecked Sendable {
    private let callback: (Value) -> Void
    init(_ callback: @escaping (Value) -> Void) { self.callback = callback }
    func deliver(_ value: Value) { DispatchQueue.main.async { self.callback(value) } }
}

public final class ClashController: Sendable {
    private let settings: ControllerSettings
    // Serialize each instance's whole preflight/PUT/verify sequence. This is
    // not a compare-and-swap against users or other controller clients.
    private let queue = DispatchQueue(label: "ClashController.requests", qos: .userInitiated)
    private static let maximumInputBytes = 8 * 1024
    private static let maximumResponseBytes = 8 * 1024 * 1024

    public init(settings: ControllerSettings) { self.settings = settings }

    // Selector groups with at least one direct member matching nodeNames by
    // UTF-8 bytes. Unsafe-to-address names are omitted; order is UTF-8 order.
    public func eligibleGroups(nodeNames: Set<String>,
                               completion: @escaping (Result<[ProxyGroup], SwitchFailure>) -> Void) {
        let callback = MainCompletion(completion)
        queue.async { callback.deliver(self.fetchEligibleGroups(nodeNames: nodeNames)) }
    }
    // Requires a Selector and an exact UTF-8-byte-identical returned name.
    public func group(_ name: String,
                      completion: @escaping (Result<ProxyGroup, SwitchFailure>) -> Void) {
        let callback = MainCompletion(completion)
        queue.async { callback.deliver(self.fetchGroup(name)) }
    }
    // A different client can change the group between preflight and PUT;
    // this API must never be described as atomic or race-free.
    public func switchAndVerify(group: String, node: String, expectedCurrent: String,
                                prePutGuard: @escaping @Sendable () -> Bool,
                                completion: @escaping (SwitchOutcome) -> Void) {
        let callback = MainCompletion(completion)
        queue.async {
            callback.deliver(self.performSwitch(group: group, node: node,
                                                expectedCurrent: expectedCurrent, prePutGuard: prePutGuard))
        }
    }

    private static func sameBytes(_ a: String, _ b: String) -> Bool { a.utf8.elementsEqual(b.utf8) }
    private static func hasControls(_ value: String) -> Bool {
        value.rangeOfCharacter(from: .controlCharacters) != nil
    }
    private func validSettings() -> Bool {
        settings.socket.utf8.first == 47 && settings.socket.utf8.count <= Self.maximumInputBytes &&
        !Self.hasControls(settings.socket) && settings.secret.utf8.count <= Self.maximumInputBytes &&
        !Self.hasControls(settings.secret)
    }
    private static func validGroupName(_ name: String) -> Bool {
        let bytes = Array(name.utf8)
        guard !bytes.isEmpty, bytes.count <= maximumInputBytes, bytes != [46], bytes != [46, 46],
              !hasControls(name) else { return false }
        // Reject ambiguous routing characters even when percent-encoded.
        let forbidden: Set<UInt8> = [47, 92, 37, 63, 35, 59] // / \\ % ? # ;
        return !bytes.contains { forbidden.contains($0) }
    }
    private static func validNode(_ node: String) -> Bool {
        !node.isEmpty && node.utf8.count <= maximumInputBytes && !hasControls(node)
    }
    // Encode exactly one UTF-8 segment without URL normalization or globbing.
    private static func segment(_ name: String) -> String {
        let hex = Array("0123456789ABCDEF".utf8)
        var result: [UInt8] = []
        result.reserveCapacity(name.utf8.count * 3)
        for b in name.utf8 {
            if (65...90).contains(b) || (97...122).contains(b) || (48...57).contains(b) ||
                b == 45 || b == 46 || b == 95 || b == 126 { result.append(b) }
            else { result.append(37); result.append(hex[Int(b >> 4)]); result.append(hex[Int(b & 15)]) }
        }
        return String(decoding: result, as: UTF8.self)
    }
    private struct WireGroup: Decodable { let name: String; let type: String; let now: String; let all: [String] }
    private func fetchGroup(_ name: String) -> Result<ProxyGroup, SwitchFailure> {
        guard validSettings() else { return .failure(.invalidSettings) }
        guard Self.validGroupName(name) else { return .failure(.invalidGroupName) }
        let response: Response
        switch request(method: .get, path: "/proxies/" + Self.segment(name), body: nil) {
        case .failure(let error): return .failure(error)
        case .success(let value): response = value
        }
        guard response.status == 200 else { return .failure(.unexpectedHTTPStatus) }
        guard let parsed = try? JSONDecoder().decode(WireGroup.self, from: response.body),
              Self.sameBytes(parsed.name, name) else { return .failure(.invalidResponse) }
        guard Self.sameBytes(parsed.type, "Selector") else { return .failure(.notSelector) }
        return .success(ProxyGroup(name: parsed.name, now: parsed.now, all: parsed.all))
    }
    private func fetchEligibleGroups(nodeNames: Set<String>) -> Result<[ProxyGroup], SwitchFailure> {
        guard validSettings() else { return .failure(.invalidSettings) }
        let response: Response
        switch request(method: .get, path: "/proxies", body: nil) {
        case .failure(let error): return .failure(error)
        case .success(let value): response = value
        }
        guard response.status == 200 else { return .failure(.unexpectedHTTPStatus) }
        guard let object = try? JSONSerialization.jsonObject(with: response.body),
              let root = object as? [String: Any], let proxies = root["proxies"] as? [String: Any]
        else { return .failure(.invalidResponse) }
        let wanted = Set(nodeNames.map { Array($0.utf8) })
        var result: [ProxyGroup] = []
        for (key, raw) in proxies {
            guard let proxy = raw as? [String: Any], let type = proxy["type"] as? String else { continue }
            guard Self.sameBytes(type, "Selector") else { continue }
            guard let name = proxy["name"] as? String, let now = proxy["now"] as? String,
                  let all = proxy["all"] as? [String], Self.sameBytes(key, name)
            else { return .failure(.invalidResponse) }
            guard Self.validGroupName(name), all.contains(where: { wanted.contains(Array($0.utf8)) })
            else { continue }
            result.append(ProxyGroup(name: name, now: now, all: all))
        }
        result.sort { $0.name.utf8.lexicographicallyPrecedes($1.name.utf8) }
        return .success(result)
    }
    private struct PutBody: Encodable { let name: String }
    private func performSwitch(group: String, node: String, expectedCurrent: String,
                               prePutGuard: @Sendable () -> Bool) -> SwitchOutcome {
        guard validSettings() else { return .notAttempted(.invalidSettings) }
        guard Self.validGroupName(group) else { return .notAttempted(.invalidGroupName) }
        guard Self.validNode(node), expectedCurrent.utf8.count <= Self.maximumInputBytes
        else { return .notAttempted(.invalidNode) }
        let before: ProxyGroup
        switch fetchGroup(group) {
        case .failure(let error): return .notAttempted(error)
        case .success(let value): before = value
        }
        guard before.all.contains(where: { Self.sameBytes($0, node) }) else { return .notAttempted(.nodeNotMember) }
        guard Self.sameBytes(before.now, expectedCurrent) else { return .notAttempted(.currentChanged) }
        guard prePutGuard() else { return .notAttempted(.environmentChanged) }
        guard !Self.sameBytes(before.now, node) else { return .confirmedNoOp }
        guard let body = try? JSONEncoder().encode(PutBody(name: node)) else { return .notAttempted(.invalidNode) }
        // Conservatively treat even a process-launch error as unknown from here.
        let put: Response
        switch request(method: .put, path: "/proxies/" + Self.segment(group), body: body) {
        case .failure: return .outcomeUnknown(.putTransport)
        case .success(let response): put = response
        }
        guard put.status == 204 else { return .outcomeUnknown(.putUnexpectedStatus) }
        switch fetchGroup(group) {
        case .failure: return .outcomeUnknown(.verificationFailed)
        case .success(let after):
            return Self.sameBytes(after.now, node) ? .switched : .outcomeUnknown(.verificationMismatch)
        }
    }

    private enum Method: String { case get = "GET", put = "PUT" }
    private struct Response { let status: Int; let body: Data }
    // curl config double-quoted strings interpret backslash escapes. Double
    // backslashes before escaping quotes to preserve arbitrary safe values.
    private static func configQuote(_ value: String) -> String {
        "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
    private func request(method: Method, path: String, body: Data?) -> Result<Response, SwitchFailure> {
        // path is constructed only from fixed strings and segment().
        guard validSettings(), path.hasPrefix("/proxies") else { return .failure(.invalidSettings) }
        var config = "url = \(Self.configQuote("http://localhost" + path))\n"
        if !settings.secret.isEmpty {
            config += "header = \(Self.configQuote("Authorization: Bearer " + settings.secret))\n"
        }
        if let body = body {
            guard let json = String(data: body, encoding: .utf8) else { return .failure(.invalidNode) }
            config += "header = \(Self.configQuote("Content-Type: application/json"))\n"
            config += "data-raw = \(Self.configQuote(json))\n"
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        // -q must be first. No shell, TCP proxy, redirects, or URL globbing.
        process.arguments = ["-q", "--noproxy", "*", "--proxy", "", "--unix-socket", settings.socket,
                             "--proto", "=http", "--no-location", "--globoff", "--path-as-is",
                             "--connect-timeout", "2", "--max-time", "5", "--silent",
                             "--max-filesize", String(Self.maximumResponseBytes), "--request", method.rawValue,
                             "--write-out", "\n%{http_code}", "-K", "-"]
        let input = Pipe(), output = Pipe()
        process.standardInput = input; process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        // Prevent an early curl exit killing the GUI via SIGPIPE, without
        // modifying the whole application's signal handling.
        guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1
        else { return .failure(.transportError) }
        do { try process.run() } catch { return .failure(.transportError) }
        var inputOK = true
        do {
            try input.fileHandleForWriting.write(contentsOf: Data(config.utf8))
            try input.fileHandleForWriting.close()
        } catch { inputOK = false; try? input.fileHandleForWriting.close() }
        var collected = Data(), readOK = true
        do {
            while let chunk = try output.fileHandleForReading.read(upToCount: 16 * 1024), !chunk.isEmpty {
                if !readOK || collected.count + chunk.count > Self.maximumResponseBytes + 4 {
                    // Discard beyond the limit while draining the pipe. curl's
                    // max-time still bounds how long it can produce output.
                    readOK = false
                } else {
                    collected.append(chunk)
                }
            }
        } catch {
            readOK = false
            try? output.fileHandleForReading.close()
        }
        process.waitUntilExit()
        guard inputOK, readOK, process.terminationReason == .exit, process.terminationStatus == 0,
              collected.count >= 4 else { return .failure(.transportError) }
        let suffix = Array(collected.suffix(4))
        guard suffix[0] == 10, (48...57).contains(suffix[1]), (48...57).contains(suffix[2]),
              (48...57).contains(suffix[3]) else { return .failure(.transportError) }
        let status = Int(suffix[1] - 48) * 100 + Int(suffix[2] - 48) * 10 + Int(suffix[3] - 48)
        collected.removeLast(4)
        return .success(Response(status: status, body: collected))
    }
}
