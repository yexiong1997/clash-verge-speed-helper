import AppKit
import ApplicationServices
import Foundation
import Darwin

/// Runtime-only bridge for the official Clash Verge Rev app.
///
/// The installed app bundle is never rewritten or re-signed. The bridge only
/// talks to the official Tauri page through its Web Inspector Console and then
/// calls the command that the official UI already uses for profile switching.
final class ClashRuntimeInjector {
    static let shared = ClashRuntimeInjector()

    enum State: Equatable {
        case idle
        case injecting
        case attached
        case unavailable(String)
    }

    private let work = DispatchQueue(label: "clash.runtime.injector", qos: .utility)
    private let stateLock = NSLock()
    private var state: State = .idle
    private var bridge: RuntimeBridge?
    // Reuse the already-attached runtime bridge when the standalone injector
    // was used earlier. This keeps the UI functional while macOS TCC is
    // waiting for the newly rebuilt helper bundle to be approved.
    private var externalBridge: ExternalRuntimeBridge?
    private var startCallbacks: [(Result<Void, Error>) -> Void] = []
    private var startInFlight = false

    private let appPath = "/Applications/Clash Verge.app"
    private let bundleID = "io.github.clash-verge-rev.clash-verge-rev"
    private let teamID = "JPH3Z7PPBB"
    private let appData = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/io.github.clash-verge-rev.clash-verge-rev", isDirectory: true)

    private init() {}

    var currentState: State {
        stateLock.lock(); defer { stateLock.unlock() }
        return state
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void = { _ in }) {
        stateLock.lock()
        if state == .attached {
            stateLock.unlock()
            completion(.success(()))
            return
        }
        startCallbacks.append(completion)
        guard !startInFlight else { stateLock.unlock(); return }
        startInFlight = true
        state = .injecting
        stateLock.unlock()

        work.async { [weak self] in
            guard let self else { return }
            do {
                try self.verifyOfficialBundle()
                try self.ensureOfficialAppRunning()
                if let external = self.readAttachedExternalBridge() {
                    self.stateLock.lock(); self.externalBridge = external; self.stateLock.unlock()
                    self.finishStart(.success(()))
                    return
                }
                let record = try self.readSingletonRecord()
                let bridge = try RuntimeBridge()
                self.stateLock.lock(); self.bridge = bridge; self.stateLock.unlock()
                try self.showOfficialWindow(record)
                try self.injectLoader(bridge: bridge)
                try bridge.waitUntilAttached(timeout: 20)
                self.finishStart(.success(()))
            } catch {
                self.finishStart(.failure(error))
            }
        }
    }

    func switchProfile(uid: String, completion: @escaping (Result<RuntimeProfileSwitch, Error>) -> Void) {
        guard !uid.isEmpty, uid.utf8.count <= 256,
              !uid.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            completion(.failure(ClashRuntimeError.invalidProfileUID)); return
        }
        work.async { [weak self] in
            guard let self else { return }
            self.stateLock.lock(); var external = self.externalBridge; self.stateLock.unlock()
            // The standalone bridge may have attached just after app launch,
            // or the launch status callback may have been overwritten by a
            // profile refresh. Re-discover it at the point of use before
            // declaring the runtime unavailable.
            if external == nil, let discovered = self.readAttachedExternalBridge() {
                self.stateLock.lock(); self.externalBridge = discovered; self.state = .attached; self.stateLock.unlock()
                external = discovered
            }
            if let external {
                external.switchProfile(uid: uid) { result in
                    switch result {
                    case .failure(let error): completion(.failure(error))
                    case .success(let value):
                        guard let previous = value["previous"] as? String,
                              let current = value["current"] as? String,
                              let name = value["name"] as? String else {
                            completion(.failure(ClashRuntimeError.invalidBridgeResult)); return
                        }
                        completion(.success(RuntimeProfileSwitch(previous: previous, current: current, name: name)))
                    }
                }
                return
            }
            guard let bridge = self.currentBridge() else {
                completion(.failure(ClashRuntimeError.notAttached)); return
            }
            bridge.enqueue(command: ["op": "switch", "uid": uid]) { result in
                switch result {
                case .failure(let error): completion(.failure(error))
                case .success(let value):
                    guard let previous = value["previous"] as? String,
                          let current = value["current"] as? String,
                          let name = value["name"] as? String else {
                        completion(.failure(ClashRuntimeError.invalidBridgeResult)); return
                    }
                    completion(.success(RuntimeProfileSwitch(previous: previous, current: current, name: name)))
                }
            }
        }
    }

    func stop() {
        work.async { [weak self] in
            guard let self else { return }
            self.stateLock.lock(); let bridge = self.bridge; self.bridge = nil; self.externalBridge = nil; self.state = .idle; self.stateLock.unlock()
            bridge?.stop()
        }
    }

    private func currentBridge() -> RuntimeBridge? {
        stateLock.lock(); defer { stateLock.unlock() }
        return bridge
    }

    private func finishStart(_ result: Result<Void, Error>) {
        stateLock.lock()
        startInFlight = false
        if case .success = result { state = .attached }
        else if case .failure(let error) = result { state = .unavailable(error.localizedDescription) }
        let callbacks = startCallbacks; startCallbacks.removeAll()
        stateLock.unlock()
        callbacks.forEach { $0(result) }
    }

    private struct SingletonRecord: Decodable {
        let port: Int
        let token: String
    }

    private func verifyOfficialBundle() throws {
        let bundleURL = URL(fileURLWithPath: appPath, isDirectory: true)
        guard FileManager.default.fileExists(atPath: bundleURL.path),
              let bundle = Bundle(url: bundleURL),
              bundle.bundleIdentifier == bundleID else { throw ClashRuntimeError.officialAppMissing }
        let inspection = try run("/usr/bin/codesign", ["-dv", appPath])
        guard inspection.contains("TeamIdentifier=\(teamID)") else { throw ClashRuntimeError.officialSignatureMismatch }
        _ = try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", appPath])
    }

    private func ensureOfficialAppRunning() throws {
        if runningOfficialApp() != nil { return }
        let bundleURL = URL(fileURLWithPath: appPath, isDirectory: true)
        let semaphore = DispatchSemaphore(value: 0)
        var launchError: Error?
        NSWorkspace.shared.openApplication(at: bundleURL, configuration: NSWorkspace.OpenConfiguration()) { _, error in
            launchError = error; semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 10)
        if let launchError { throw launchError }
        for _ in 0..<50 {
            if runningOfficialApp() != nil { return }
            Thread.sleep(forTimeInterval: 0.2)
        }
        throw ClashRuntimeError.officialAppNotRunning
    }

    private func runningOfficialApp() -> NSRunningApplication? {
        NSWorkspace.shared.runningApplications.first {
            $0.bundleIdentifier == bundleID &&
            $0.bundleURL?.standardizedFileURL.path == appPath
        }
    }

    private func readSingletonRecord() throws -> SingletonRecord {
        let path = appData.appendingPathComponent("singleton-instance.json")
        let data = try Data(contentsOf: path, options: [.mappedIfSafe])
        let record = try JSONDecoder().decode(SingletonRecord.self, from: data)
        let hex = CharacterSet(charactersIn: "0123456789abcdefABCDEF")
        guard (1...65535).contains(record.port), record.token.count >= 32,
              record.token.unicodeScalars.allSatisfy({ hex.contains($0) }) else {
            throw ClashRuntimeError.invalidSingletonRecord
        }
        return record
    }

    private func readAttachedExternalBridge() -> ExternalRuntimeBridge? {
        var urls: [URL] = []
        if let override = ProcessInfo.processInfo.environment["CLASH_RUNTIME_STATE"], !override.isEmpty {
            urls.append(URL(fileURLWithPath: override))
        }
        // Keep this outside Documents: TCC can block a synchronous read there
        // and leave the launch queue stuck before the UI can report a result.
        urls.append(URL(fileURLWithPath: "/private/tmp/clash-verge-runtime-state.json"))
        for url in urls {
            guard let data = try? Data(contentsOf: url),
                  let state = try? JSONDecoder().decode(ExternalRuntimeState.self, from: data),
                  (1...65535).contains(state.port), state.token.count >= 32 else { continue }
            let bridge = ExternalRuntimeBridge(port: state.port, token: state.token)
            if bridge.isAttached() { return bridge }
        }
        return nil
    }

    private func showOfficialWindow(_ record: SingletonRecord) throws {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(record.port)/commands/visible")!)
        request.httpMethod = "GET"
        request.setValue(record.token, forHTTPHeaderField: "x-instance-token")
        let semaphore = DispatchSemaphore(value: 0)
        var status = 0
        URLSession.shared.dataTask(with: request) { _, response, _ in
            status = (response as? HTTPURLResponse)?.statusCode ?? 0
            semaphore.signal()
        }.resume()
        guard semaphore.wait(timeout: .now() + 5) == .success, status == 200 else {
            throw ClashRuntimeError.officialWindowUnavailable
        }
    }

    private func injectLoader(bridge: RuntimeBridge) throws {
        guard AXIsProcessTrusted() else { throw ClashRuntimeError.accessibilityRequired }
        guard let app = runningOfficialApp() else { throw ClashRuntimeError.officialAppNotRunning }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(axApp, 3)
        if let prompt = findConsolePrompt(axApp) {
            try submit(prompt: prompt, app: app, loader: bridge.loaderScript)
            return
        }
        var window: AXUIElement?
        for _ in 0..<40 {
            window = firstWindow(axApp)
            if window != nil { break }
            waitForAXChange()
        }
        guard let window else { throw ClashRuntimeError.officialWindowUnavailable }
        _ = app.activate(options: [.activateIgnoringOtherApps])
        var developer: AXUIElement?
        for _ in 0..<20 {
            if let settings = findElement(window, predicate: { role, title in
                role == kAXButtonRole && title.contains("设")
            }) { _ = AXUIElementPerformAction(settings, kAXPressAction as CFString) }
            waitForAXChange()
            if let refreshedWindow = firstWindow(axApp),
               let candidate = findElement(refreshedWindow, predicate: { role, title in
                   role == kAXButtonRole && title.contains("开发者工具")
               }) {
                developer = candidate
                break
            }
        }
        guard let developer else { throw ClashRuntimeError.developerToolsUnavailable }
        _ = AXUIElementPerformAction(developer, kAXShowMenuAction as CFString)
        _ = AXUIElementPerformAction(developer, kAXPressAction as CFString)
        for _ in 0..<50 {
            waitForAXChange()
            if let prompt = findConsolePrompt(axApp) {
                try submit(prompt: prompt, app: app, loader: bridge.loaderScript)
                return
            }
        }
        throw ClashRuntimeError.developerToolsUnavailable
    }

    private func submit(prompt: AXUIElement, app: NSRunningApplication, loader: String) throws {
        guard let source = CGEventSource(stateID: .privateState) else { throw ClashRuntimeError.consoleInputFailed }
        guard let frame = frame(prompt) else { throw ClashRuntimeError.consoleInputFailed }
        let point = CGPoint(x: frame.midX, y: frame.midY)
        _ = app.activate(options: [.activateIgnoringOtherApps])
        for mouseType in [CGEventType.leftMouseDown, CGEventType.leftMouseUp] {
            guard let event = CGEvent(mouseEventSource: source, mouseType: mouseType,
                                      mouseCursorPosition: point, mouseButton: .left) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.post(tap: .cghidEventTap)
            usleep(30_000)
        }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.flags = [.maskCommand]
            event.postToPid(app.processIdentifier)
            usleep(20_000)
        }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 51, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.postToPid(app.processIdentifier)
            usleep(20_000)
        }
        let units = Array(loader.utf16)
        for offset in stride(from: 0, to: units.count, by: 20) {
            let chunk = Array(units[offset..<min(offset + 20, units.count)])
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            chunk.withUnsafeBufferPointer { buffer in
                event.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
            }
            event.postToPid(app.processIdentifier)
            usleep(20_000)
            guard let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            keyUp.postToPid(app.processIdentifier)
        }
        Thread.sleep(forTimeInterval: 1.0)
        let entered = attribute(prompt, kAXValueAttribute) as? String ?? ""
        guard entered == loader else {
            throw ClashRuntimeError.consoleInputFailed
        }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.postToPid(app.processIdentifier)
            usleep(30_000)
        }
    }

    private func waitForAXChange() { Thread.sleep(forTimeInterval: 0.25) }

    private func firstWindow(_ app: AXUIElement) -> AXUIElement? {
        (attribute(app, kAXWindowsAttribute) as? [AXUIElement])?.first
    }

    private func findConsolePrompt(_ app: AXUIElement) -> AXUIElement? {
        for window in (attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []) {
            if let prompt = findElement(window, predicate: { role, title in
                (role == kAXTextAreaRole || role == kAXTextFieldRole) && title == "Console prompt"
            }) { return prompt }
            if let prompt = findConsolePrompt(window, inConsole: false) { return prompt }
        }
        return nil
    }

    private func findConsolePrompt(_ root: AXUIElement, inConsole: Bool, depth: Int = 0) -> AXUIElement? {
        guard depth < 24 else { return nil }
        let role = attribute(root, kAXRoleAttribute) as? String ?? ""
        let title = attribute(root, kAXTitleAttribute) as? String ?? ""
        let description = attribute(root, kAXDescriptionAttribute) as? String ?? ""
        let console = inConsole || title.localizedCaseInsensitiveContains("console") ||
            description.localizedCaseInsensitiveContains("console")
        if console && (role == kAXTextAreaRole || role == kAXTextFieldRole) {
            return root
        }
        for child in (attribute(root, kAXChildrenAttribute) as? [AXUIElement] ?? []) {
            if let result = findConsolePrompt(child, inConsole: console, depth: depth + 1) { return result }
        }
        return nil
    }

    private func findElement(_ root: AXUIElement,
                             predicate: (String, String) -> Bool,
                             requireConsoleHint: Bool = false,
                             depth: Int = 0) -> AXUIElement? {
        guard depth < 20 else { return nil }
        let role = attribute(root, kAXRoleAttribute) as? String ?? ""
        let title = attribute(root, kAXTitleAttribute) as? String ?? ""
        if predicate(role, title) {
            if !requireConsoleHint || title == "Console prompt" { return root }
        }
        for child in (attribute(root, kAXChildrenAttribute) as? [AXUIElement] ?? []) {
            if let result = findElement(child, predicate: predicate, requireConsoleHint: requireConsoleHint, depth: depth + 1) {
                return result
            }
        }
        return nil
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    private func frame(_ element: AXUIElement) -> CGRect? {
        guard let value = attribute(element, "AXFrame") else { return nil }
        var result = CGRect.zero
        guard AXValueGetValue(value as! AXValue, .cgRect, &result) else { return nil }
        return result
    }

    private func run(_ executable: String, _ arguments: [String]) throws -> String {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
        let output = Pipe(); process.standardOutput = output; process.standardError = output
        try process.run(); let data = output.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw ClashRuntimeError.officialSignatureMismatch }
        return String(data: data, encoding: .utf8) ?? ""
    }
}

struct RuntimeProfileSwitch: Sendable {
    let previous: String
    let current: String
    let name: String
}

private struct ExternalRuntimeState: Decodable {
    let port: Int
    let token: String
}

/// Client for the standalone bridge produced by outputs/clash-verge-runtime.mjs.
/// It is intentionally limited to status and exact-profile switch requests;
/// it never exposes a general JavaScript or filesystem interface.
private final class ExternalRuntimeBridge {
    private let port: Int
    private let token: String

    init(port: Int, token: String) {
        self.port = port
        self.token = token
    }

    func isAttached() -> Bool {
        guard let value = try? request(path: "/status", method: "GET", body: nil) else { return false }
        return value["attached"] as? Bool == true
    }

    func switchProfile(uid: String, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            do {
                let body: [String: Any] = ["op": "switch", "target": uid]
                completion(.success(try self.request(path: "/command", method: "POST", body: body)))
            } catch {
                completion(.failure(ClashRuntimeError.notAttached))
            }
        }
    }

    private func request(path: String, method: String, body: [String: Any]?) throws -> [String: Any] {
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else {
            throw ClashRuntimeError.notAttached
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }

        let semaphore = DispatchSemaphore(value: 0)
        var result: Result<(Data, HTTPURLResponse), Error>?
        URLSession.shared.dataTask(with: request) { data, response, error in
            defer { semaphore.signal() }
            if let error { result = .failure(error); return }
            guard let data, let response = response as? HTTPURLResponse else {
                result = .failure(ClashRuntimeError.notAttached); return
            }
            result = .success((data, response))
        }.resume()
        guard semaphore.wait(timeout: .now() + 45) == .success,
              let result else { throw ClashRuntimeError.notAttached }
        let (data, response) = try result.get()
        guard (200...299).contains(response.statusCode),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ClashRuntimeError.notAttached
        }
        if let error = object["error"] as? String, !error.isEmpty {
            throw ClashRuntimeError.notAttached
        }
        return object
    }
}

private enum ClashRuntimeError: LocalizedError {
    case officialAppMissing, officialSignatureMismatch, officialAppNotRunning
    case invalidSingletonRecord, officialWindowUnavailable, accessibilityRequired
    case developerToolsUnavailable, consoleInputFailed, notAttached
    case invalidProfileUID, invalidBridgeResult

    var errorDescription: String? {
        switch self {
        case .officialAppMissing: return "未找到官方 Clash Verge Rev（/Applications/Clash Verge.app）。"
        case .officialSignatureMismatch: return "已安装的 Clash Verge 不是预期的官方 Developer ID 签名。"
        case .officialAppNotRunning: return "Clash Verge 未能启动。"
        case .invalidSingletonRecord: return "Clash Verge 的本机实例记录无效。"
        case .officialWindowUnavailable: return "无法显示 Clash Verge 主窗口。"
        case .accessibilityRequired: return "需要在系统设置中授予本助手辅助功能权限。"
        case .developerToolsUnavailable: return "无法打开 Clash Verge Web Inspector。"
        case .consoleInputFailed: return "无法向 Clash Verge Console 注入加载器。"
        case .notAttached: return "运行时补丁尚未连接。"
        case .invalidProfileUID: return "订阅 UID 无效。"
        case .invalidBridgeResult: return "Clash Verge 返回了无法验证的切换结果。"
        }
    }
}

private final class RuntimeBridge {
    private let listenerFD: Int32
    // Keep accepting sockets on a different queue from request handling. The
    // accept loop blocks in accept(2); putting it on the same serial queue as
    // handle(_:) starves every HTTP request (including the CORS preflight), so
    // the WebView reports that /patch is unreachable even though the listener
    // was bound successfully.
    private let acceptWork = DispatchQueue(label: "clash.runtime.bridge.accept", qos: .utility)
    private let work = DispatchQueue(label: "clash.runtime.bridge.requests", qos: .utility)
    private let lock = NSLock()
    private var stopped = false
    private var attachedClient: String?
    private var pending: [String: (Result<[String: Any], Error>) -> Void] = [:]
    private var commands: [[String: Any]] = []
    private var attachSemaphore = DispatchSemaphore(value: 0)
    let port: Int
    let token: String
    let loaderScript: String

    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ClashRuntimeError.consoleInputFailed }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }}
        guard bound == 0, listen(fd, 8) == 0 else { close(fd); throw ClashRuntimeError.consoleInputFailed }
        var actual = sockaddr_in(); var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        guard getsockname(fd, withUnsafeMutablePointer(to: &actual) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { $0 } }, &length) == 0 else {
            close(fd); throw ClashRuntimeError.consoleInputFailed
        }
        listenerFD = fd; port = Int(UInt16(bigEndian: actual.sin_port)); token = UUID().uuidString.replacingOccurrences(of: "-", with: "") + UUID().uuidString.replacingOccurrences(of: "-", with: "")
        loaderScript = RuntimeBridge.makeLoader(port: port, token: token)
        acceptWork.async { [weak self] in self?.acceptLoop() }
    }

    func waitUntilAttached(timeout: TimeInterval) throws {
        guard attachSemaphore.wait(timeout: .now() + timeout) == .success else { throw ClashRuntimeError.developerToolsUnavailable }
    }

    func enqueue(command: [String: Any], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let id = UUID().uuidString
        var item = command; item["id"] = id
        lock.lock(); pending[id] = completion; commands.append(item); lock.unlock()
    }

    func stop() {
        lock.lock(); stopped = true; let fd = listenerFD; let callbacks = pending.values; pending.removeAll(); lock.unlock()
        close(fd); callbacks.forEach { $0(.failure(ClashRuntimeError.notAttached)) }
    }

    private func acceptLoop() {
        while true {
            lock.lock(); let done = stopped; lock.unlock(); if done { return }
            let connection = accept(listenerFD, nil, nil)
            if connection < 0 { continue }
            work.async { [weak self] in self?.handle(connection) }
        }
    }

    private func handle(_ fd: Int32) {
        defer { close(fd) }
        guard let request = readRequest(fd) else { respond(fd, status: 400, body: ["error": "bad request"]); return }
        if request.method == "OPTIONS" { respond(fd, status: 204, body: nil); return }
        guard authorized(request.headers) else { respond(fd, status: 401, body: ["error": "unauthorized"]); return }
        if request.method == "GET", request.path == "/patch" { respondRaw(fd, status: 200, contentType: "application/javascript", body: patchScript()); return }
        if request.method == "POST", request.path == "/hello" {
            let client = (request.json["client"] as? String) ?? ""
            guard !client.isEmpty else { respond(fd, status: 400, body: ["error": "client"]); return }
            lock.lock(); attachedClient = client; lock.unlock(); attachSemaphore.signal(); respond(fd, status: 200, body: ["ok": true]); return
        }
        if request.method == "GET", request.path == "/poll" {
            lock.lock(); let command = commands.isEmpty ? nil : commands.removeFirst(); lock.unlock()
            guard let command else { respond(fd, status: 204, body: nil); return }
            respond(fd, status: 200, body: ["command": command]); return
        }
        if request.method == "POST", request.path == "/result" {
            guard let id = request.json["id"] as? String else { respond(fd, status: 400, body: ["error": "id"]); return }
            lock.lock(); let callback = pending.removeValue(forKey: id); lock.unlock()
            if let callback {
                if (request.json["ok"] as? Bool) == true, let value = request.json["value"] as? [String: Any] {
                    callback(.success(value))
                } else { callback(.failure(ClashRuntimeError.invalidBridgeResult)) }
            }
            respond(fd, status: 200, body: ["ok": true]); return
        }
        if request.method == "GET", request.path == "/status" {
            lock.lock(); let attached = attachedClient != nil; lock.unlock(); respond(fd, status: 200, body: ["attached": attached]); return
        }
        respond(fd, status: 404, body: ["error": "not found"])
    }

    private struct Request { let method: String; let path: String; let headers: [String: String]; let json: [String: Any] }

    private func readRequest(_ fd: Int32) -> Request? {
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
        while data.range(of: Data("\r\n\r\n".utf8)) == nil && data.count < 65536 {
            let count = read(fd, &buffer, buffer.count); if count <= 0 { return nil }; data.append(buffer, count: count)
        }
        guard let marker = data.range(of: Data("\r\n\r\n".utf8)), let head = String(data: data[..<marker.lowerBound], encoding: .utf8) else { return nil }
        let lines = head.split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        guard let first = lines.first else { return nil }; let parts = first.split(separator: " "); guard parts.count >= 2 else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() { let p = line.split(separator: ":", maxSplits: 1); if p.count == 2 { headers[String(p[0]).lowercased()] = String(p[1]).trimmingCharacters(in: .whitespaces) } }
        let length = Int(headers["content-length"] ?? "0") ?? 0; var body = Data(data[marker.upperBound...])
        while body.count < length { let count = read(fd, &buffer, buffer.count); if count <= 0 { return nil }; body.append(buffer, count: count) }
        let json = (try? JSONSerialization.jsonObject(with: body.prefix(length))) as? [String: Any] ?? [:]
        return Request(method: String(parts[0]), path: String(parts[1].split(separator: "?").first ?? ""), headers: headers, json: json)
    }

    private func authorized(_ headers: [String: String]) -> Bool { headers["authorization"] == "Bearer \(token)" }

    private func respond(_ fd: Int32, status: Int, body: [String: Any]?) {
        if status == 204 { respondRaw(fd, status: status, contentType: "application/json", body: ""); return }
        let data = (try? JSONSerialization.data(withJSONObject: body ?? [:])) ?? Data("{}".utf8)
        respondRaw(fd, status: status, contentType: "application/json", body: String(data: data, encoding: .utf8) ?? "{}")
    }

    private func respondRaw(_ fd: Int32, status: Int, contentType: String, body: String) {
        let text = "HTTP/1.1 \(status) \(status == 200 ? "OK" : status == 204 ? "No Content" : "Error")\r\nContent-Type: \(contentType)\r\nAccess-Control-Allow-Origin: tauri://localhost\r\nAccess-Control-Allow-Headers: Authorization, Content-Type\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nCache-Control: no-store\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
        _ = text.withCString { write(fd, $0, strlen($0)) }
    }

    private func patchScript() -> String {
        let base = "http://127.0.0.1:\(port)"
        return RuntimeBridge.makePatch(base: base, token: token)
    }

    private static func makeLoader(port: Int, token: String) -> String {
        let base = "http://127.0.0.1:\(port)/patch"
        return "fetch(\(json(base)),{headers:{Authorization:\(json("Bearer \(token)"))}}).then(r=>{if(!r.ok)throw Error(r.status);return r.text()}).then(eval);"
    }

    private static func makePatch(base: String, token: String) -> String {
        let template = #"""
        (()=>{const B=__BASE__,A=__AUTH__,S='__SLOT__',W=ms=>new Promise(r=>setTimeout(r,ms));if(!window.__TAURI_INTERNALS__?.invoke||location.origin!=='tauri://localhost')throw Error('Tauri page required');window[S]?.stop?.();let live=true;const C=crypto.randomUUID(),I=window.__TAURI_INTERNALS__.invoke;const J=(p,m='GET',b)=>fetch(B+p,{method:m,headers:{Authorization:A,'Content-Type':'application/json','Cache-Control':'no-store'},body:b===undefined?undefined:JSON.stringify(b)});const stop=()=>{live=false;delete window[S]};window[S]={stop,client:C};(async()=>{try{await J('/hello','POST',{client:C,origin:location.origin});console.info('[Clash runtime] official bridge attached');while(live){const r=await J('/poll?client='+encodeURIComponent(C));if(r.status===204){await W(220);continue}const q=await r.json();if(!q.command){await W(220);continue}let out;try{if(q.command.op==='switch'){const before=await I('get_profiles');const target=q.command.uid;const matches=(before.items||[]).filter(x=>x&&x.uid===target);if(matches.length!==1)throw Error('profile not found');const outcome=await I('patch_profiles_config',{profiles:{current:target}});const after=await I('get_profiles');if(outcome?.status!=='valid'||after.current!==target)throw Error('official transaction not verified');out={ok:true,value:{previous:before.current||'',current:after.current,name:matches[0].name||target}}}else if(q.command.op==='detach'){out={ok:true,value:{detached:true}}}else throw Error('unsupported operation')}catch(e){out={ok:false,error:String(e?.message||e)}}await J('/result','POST',{client:C,id:q.command.id,...out});if(q.command.op==='detach')stop()}}catch(e){console.error('[Clash runtime] stopped',String(e?.message||e));stop()}})()})()
        """#
        return template.replacingOccurrences(of: "__BASE__", with: json(base))
            .replacingOccurrences(of: "__AUTH__", with: json("Bearer \(token)"))
            .replacingOccurrences(of: "__SLOT__", with: "__CLASH_VERGE_RUNTIME_BRIDGE__")
            .replacingOccurrences(of: "\n", with: " ")
    }

    private static func json(_ value: String) -> String {
        let data = try! JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed])
        return String(data: data, encoding: .utf8)!
    }
}
