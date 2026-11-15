import AppKit
import ApplicationServices
import CoreGraphics
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
    private let operationWork = DispatchQueue(label: "clash.runtime.injector.ops", qos: .utility, attributes: .concurrent)
    private let stateLock = NSLock()
    private var state: State = .idle
    private var bridge: RuntimeBridge?
    private var pendingStartBridge: RuntimeBridge?
    // Reuse the already-attached runtime bridge when the standalone injector
    // was used earlier. This keeps the UI functional while macOS TCC is
    // waiting for the newly rebuilt helper bundle to be approved.
    private var externalBridge: ExternalRuntimeBridge?
    private var startCallbacks: [(Result<Void, Error>) -> Void] = []
    private var startInFlight = false
    private var lifecycleGeneration: UInt64 = 0
    // Only the serial work queue sets this. Checks in the AX bootstrap loops
    // also reject a stopped generation after a newer start resets the flag.
    private var executingStartGeneration: UInt64?
    private final class SwitchRequestToken {
        let id = UUID()
        private let lock = NSLock()
        private var cancelled = false
        var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
        func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    }

    private var stopRequested = false
    private var promptedAccessibility = false
    private var lastAccessibilitySettingsPromptAt: Date?
    private let accessibilitySettingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
    private var activeSwitchTokens: [UUID: SwitchRequestToken] = [:]
    private var activeSwitchCancellations: [UUID: () -> Void] = [:]

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

    var hasAccessibilityPermission: Bool {
        AXIsProcessTrusted()
    }

    var isReady: Bool {
        stateLock.lock()
        let external = externalBridge
        let current = bridge
        let st = state
        stateLock.unlock()
        if external != nil { return true }
        return st == .attached && (current?.isAlive() == true)
    }

    /// Reuse an existing connection without launching or activating Clash,
    /// showing permission UI, or opening its Inspector. A missing bridge is a
    /// normal false result; only an explicit switch may bootstrap a new one.
    func reconnectSilently(completion: @escaping (Result<Bool, Error>) -> Void = { _ in }) {
        stateLock.lock()
        let generation = lifecycleGeneration
        let stopped = stopRequested
        let busy = startInFlight || !activeSwitchTokens.isEmpty
        stateLock.unlock()
        guard !stopped else { completion(.failure(ClashRuntimeError.cancelled)); return }
        guard !busy else { completion(.success(false)); return }

        work.async { [weak self] in
            guard let self else { completion(.failure(ClashRuntimeError.cancelled)); return }
            self.stateLock.lock()
            guard self.lifecycleGeneration == generation, !self.stopRequested else {
                self.stateLock.unlock()
                completion(.failure(ClashRuntimeError.cancelled))
                return
            }
            guard !self.startInFlight, self.activeSwitchTokens.isEmpty else {
                self.stateLock.unlock()
                completion(.success(false))
                return
            }
            let owned = self.bridge
            let cachedExternal = self.externalBridge
            self.stateLock.unlock()

            RuntimeBrokerSupport.startIfInstalled()
            let ownedAlive = owned?.isAlive() == true
            var attachedExternal: ExternalRuntimeBridge?
            if !ownedAlive {
                if let cachedExternal, cachedExternal.isAttached() {
                    attachedExternal = cachedExternal
                } else {
                    attachedExternal = self.readAttachedExternalBridge()
                }
            }

            self.stateLock.lock()
            guard self.lifecycleGeneration == generation, !self.stopRequested else {
                self.stateLock.unlock()
                completion(.failure(ClashRuntimeError.cancelled))
                return
            }
            guard !self.startInFlight, self.activeSwitchTokens.isEmpty,
                  self.bridge === owned, self.externalBridge === cachedExternal else {
                self.stateLock.unlock()
                completion(.success(false))
                return
            }
            let attached = ownedAlive || attachedExternal != nil
            if !ownedAlive { self.bridge = nil }
            self.externalBridge = attachedExternal
            if attached { self.state = .attached }
            else if self.state == .attached { self.state = .idle }
            self.stateLock.unlock()
            if !ownedAlive { owned?.stop() }
            completion(.success(attached))
        }
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void = { _ in }) {
        stateLock.lock()
        if state == .attached, externalBridge == nil, let existing = bridge, existing.isAlive() {
            stateLock.unlock()
            completion(.success(()))
            return
        }
        startCallbacks.append(completion)
        guard !startInFlight else { stateLock.unlock(); return }
        startInFlight = true
        lifecycleGeneration &+= 1
        let generation = lifecycleGeneration
        stopRequested = false
        state = .injecting
        let stale = bridge
        let cachedExternal = externalBridge
        bridge = nil
        externalBridge = nil
        stateLock.unlock()
        stale?.stop()

        work.async { [weak self] in
            guard let self else { return }
            var bootstrapUI: BootstrapUIContext?
            self.stateLock.lock()
            self.executingStartGeneration = generation
            self.stateLock.unlock()
            defer {
                if let ui = bootstrapUI { self.restoreBootstrapUI(ui) }
                self.stateLock.lock()
                self.executingStartGeneration = nil
                self.stateLock.unlock()
            }
            let finish: (Result<Void, Error>) -> Void = { result in
                if let ui = bootstrapUI {
                    self.restoreBootstrapUI(ui)
                    bootstrapUI = nil
                }
                self.finishStart(result, generation: generation)
            }
            var createdBridge: RuntimeBridge?
            do {
                try self.checkNotStopped()
                try self.verifyOfficialBundle()
                try self.checkNotStopped()
                let external = cachedExternal?.isAttached() == true ? cachedExternal : self.readAttachedExternalBridge()
                try self.checkNotStopped()
                if let external {
                    self.stateLock.lock()
                    let active = self.lifecycleGeneration == generation && !self.stopRequested
                    if active { self.externalBridge = external }
                    self.stateLock.unlock()
                    finish(active ? .success(()) : .failure(ClashRuntimeError.cancelled))
                    return
                }
                RuntimeBrokerSupport.startIfInstalled()
                let broker = self.readAvailableBroker()
                let ui = self.captureBootstrapUI(generation: generation)
                bootstrapUI = ui
                ui.touchedUI = true
                try self.ensureOfficialAppRunning()
                ui.app = self.runningOfficialApp()
                try self.checkNotStopped()
                let record = try self.readSingletonRecord()
                guard AXIsProcessTrusted() else {
                    self.promptAccessibility(openSettings: true, alwaysOpenSettings: false)
                    throw ClashRuntimeError.accessibilityRequired
                }
                if Self.isScreenLocked() {
                    throw ClashRuntimeError.screenLocked
                }
                if let broker {
                    try self.showOfficialWindow(record)
                    try self.checkNotStopped()
                    try self.injectLoader(loader: broker.loaderScript, ui: ui)
                    let deadline = Date().addingTimeInterval(20)
                    var attachPoll = 0
                    while !broker.isAttached() {
                        try self.checkNotStopped()
                        guard Date() < deadline else { throw ClashRuntimeError.developerToolsUnavailable }
                        Thread.sleep(forTimeInterval: attachPoll < 50 ? 0.02 : 0.1)
                        attachPoll += 1
                    }
                    self.captureOwnedInspector(ui)
                    try self.checkNotStopped()
                    self.stateLock.lock()
                    let active = self.lifecycleGeneration == generation && !self.stopRequested
                    if active { self.externalBridge = broker }
                    self.stateLock.unlock()
                    finish(active ? .success(()) : .failure(ClashRuntimeError.cancelled))
                    return
                }
                let bridge = try RuntimeBridge()
                createdBridge = bridge
                self.stateLock.lock()
                let stoppedBeforeAttach = self.stopRequested || self.lifecycleGeneration != generation
                if !stoppedBeforeAttach { self.pendingStartBridge = bridge }
                self.stateLock.unlock()
                if stoppedBeforeAttach {
                    bridge.stop()
                    finish(.failure(ClashRuntimeError.cancelled))
                    return
                }
                try self.showOfficialWindow(record)
                try self.checkNotStopped()
                try self.injectLoader(bridge: bridge, ui: ui)
                try self.checkNotStopped()
                try bridge.waitUntilAttached(timeout: 20)
                self.captureOwnedInspector(ui)
                self.stateLock.lock()
                let stillActive = !self.stopRequested && self.lifecycleGeneration == generation && self.state == .injecting
                if self.pendingStartBridge === bridge { self.pendingStartBridge = nil }
                if stillActive { self.bridge = bridge }
                self.stateLock.unlock()
                if !stillActive {
                    bridge.stop()
                    finish(.failure(ClashRuntimeError.cancelled))
                    return
                }
                finish(.success(()))
            } catch {
                self.stateLock.lock()
                if let createdBridge {
                    if self.pendingStartBridge === createdBridge { self.pendingStartBridge = nil }
                    if self.bridge === createdBridge { self.bridge = nil }
                }
                self.stateLock.unlock()
                createdBridge?.stop()
                finish(.failure(error))
            }
        }
    }

    @discardableResult
    func switchProfile(uid: String, completion: @escaping (Result<RuntimeProfileSwitch, Error>) -> Void) -> UUID {
        let token = SwitchRequestToken()
        let requestID = token.id
        guard !uid.isEmpty, uid.utf8.count <= 256,
              !uid.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            completion(.failure(ClashRuntimeError.invalidProfileUID))
            return requestID
        }

        let finishLock = NSLock()
        var delivered = false
        let deliver: (Result<RuntimeProfileSwitch, Error>) -> Void = { [weak self] result in
            finishLock.lock()
            guard !delivered else { finishLock.unlock(); return }
            delivered = true
            finishLock.unlock()
            if let self {
                self.stateLock.lock()
                self.activeSwitchCancellations.removeValue(forKey: requestID)
                self.activeSwitchTokens.removeValue(forKey: requestID)
                self.stateLock.unlock()
            }
            completion(result)
        }

        stateLock.lock()
        activeSwitchTokens[requestID] = token
        activeSwitchCancellations[requestID] = {
            token.cancel()
            deliver(.failure(ClashRuntimeError.cancelled))
        }
        stateLock.unlock()

        operationWork.async { [weak self] in
            guard let self else {
                deliver(.failure(ClashRuntimeError.notAttached))
                return
            }
            if self.isSwitchCancelled(token) {
                deliver(.failure(ClashRuntimeError.cancelled))
                return
            }

            self.runBridgeSwitch(uid: uid, token: token, allowReattachRetry: true, deliver: deliver)
        }
        return requestID
    }

    func cancelSwitch(_ requestID: UUID) {
        stateLock.lock()
        activeSwitchTokens[requestID]?.cancel()
        let cancelAction = activeSwitchCancellations.removeValue(forKey: requestID)
        activeSwitchTokens.removeValue(forKey: requestID)
        stateLock.unlock()
        cancelAction?()
    }

    func stop() {
        stateLock.lock()
        stopRequested = true
        lifecycleGeneration &+= 1
        startInFlight = false
        let callbacks = startCallbacks
        startCallbacks.removeAll()
        let bridge = self.bridge
        let pendingBridge = self.pendingStartBridge
        self.bridge = nil
        self.pendingStartBridge = nil
        self.externalBridge = nil
        self.state = .idle
        for token in activeSwitchTokens.values { token.cancel() }
        let cancels = Array(activeSwitchCancellations.values)
        activeSwitchTokens.removeAll()
        activeSwitchCancellations.removeAll()
        stateLock.unlock()
        bridge?.stop()
        if let pendingBridge, pendingBridge !== bridge { pendingBridge.stop() }
        cancels.forEach { $0() }
        callbacks.forEach { $0(.failure(ClashRuntimeError.cancelled)) }
    }

    private func runBridgeSwitch(uid: String,
                                 token: SwitchRequestToken,
                                 allowReattachRetry: Bool,
                                 deliver: @escaping (Result<RuntimeProfileSwitch, Error>) -> Void) {
        let requestID = token.id
        if isSwitchCancelled(token) {
            deliver(.failure(ClashRuntimeError.cancelled))
            return
        }
        switch ensureAliveTransport(token: token) {
        case .failure(let error):
            deliver(.failure(error))
        case .success(.remote(let external)):
            stateLock.lock()
            activeSwitchCancellations[requestID] = { token.cancel() }
            stateLock.unlock()
            external.switchProfile(uid: uid, requestID: requestID, isCancelled: { [weak self] in
                self?.isSwitchCancelled(token) ?? true
            }) { result in
                switch result {
                case .failure(let error): deliver(.failure(error))
                case .success(let value):
                    guard let previous = value["previous"] as? String,
                          let current = value["current"] as? String, current == uid,
                          let name = value["name"] as? String else {
                        deliver(.failure(ClashRuntimeError.invalidBridgeResult)); return
                    }
                    deliver(.success(RuntimeProfileSwitch(previous: previous, current: current, name: name)))
                }
            }
        case .success(.owned(let bridge)):
            if isSwitchCancelled(token) {
                deliver(.failure(ClashRuntimeError.cancelled))
                return
            }
            stateLock.lock()
            activeSwitchCancellations[requestID] = { [weak bridge] in
                token.cancel()
                bridge?.cancel(id: requestID)
                deliver(.failure(ClashRuntimeError.cancelled))
            }
            stateLock.unlock()
            if isSwitchCancelled(token) {
                bridge.cancel(id: requestID)
                deliver(.failure(ClashRuntimeError.cancelled))
                return
            }
            bridge.enqueue(id: requestID, command: ["op": "switch", "uid": uid],
                           pickupTimeout: 3.0, executionTimeout: 25.0) { [weak self] result in
                switch result {
                case .failure(let error):
                    if let self, allowReattachRetry, !self.isSwitchCancelled(token),
                       case ClashRuntimeError.notAttached = error {
                        self.invalidateDeadBridge(bridge, reason: error.localizedDescription)
                        self.runBridgeSwitch(uid: uid, token: token, allowReattachRetry: false, deliver: deliver)
                        return
                    }
                    deliver(.failure(error))
                case .success(let value):
                    guard let previous = value["previous"] as? String,
                          let current = value["current"] as? String,
                          let name = value["name"] as? String else {
                        deliver(.failure(ClashRuntimeError.invalidBridgeResult)); return
                    }
                    deliver(.success(RuntimeProfileSwitch(previous: previous, current: current, name: name)))
                }
            }
        }
    }

    private enum Transport {
        case owned(RuntimeBridge)
        case remote(ExternalRuntimeBridge)
    }

    private func ensureAliveTransport(token: SwitchRequestToken) -> Result<Transport, Error> {
        stateLock.lock()
        let current = bridge
        let remote = externalBridge
        let inFlight = startInFlight
        stateLock.unlock()

        if let current, current.isAlive() {
            return .success(.owned(current))
        }
        if let available = remote?.isAttached() == true ? remote : readAttachedExternalBridge() {
            stateLock.lock()
            let active = !stopRequested && !token.isCancelled
            if active { externalBridge = available; state = .attached }
            stateLock.unlock()
            return active ? .success(.remote(available)) : .failure(ClashRuntimeError.cancelled)
        }
        if let current {
            invalidateDeadBridge(current, reason: ClashRuntimeError.notAttached.localizedDescription)
        }

        if !inFlight {
            if !AXIsProcessTrusted() {
                promptAccessibility(openSettings: true, alwaysOpenSettings: true)
                return .failure(ClashRuntimeError.accessibilityRequired)
            }
            if Self.isScreenLocked() {
                return .failure(ClashRuntimeError.screenLocked)
            }
        }

        let semaphore = DispatchSemaphore(value: 0)
        var startResult: Result<Void, Error>?
        start { result in
            startResult = result
            semaphore.signal()
        }
        let deadline = Date().addingTimeInterval(25)
        while Date() < deadline {
            if isSwitchCancelled(token) {
                return .failure(ClashRuntimeError.cancelled)
            }
            if semaphore.wait(timeout: .now() + 0.1) == .success {
                break
            }
        }
        if isSwitchCancelled(token) {
            return .failure(ClashRuntimeError.cancelled)
        }
        guard let startResult else {
            return .failure(ClashRuntimeError.developerToolsUnavailable)
        }
        switch startResult {
        case .failure(let error):
            if case ClashRuntimeError.accessibilityRequired = error {
                promptAccessibility(openSettings: true, alwaysOpenSettings: true)
            }
            return .failure(error)
        case .success:
            stateLock.lock()
            let attachedBridge = bridge
            let attachedRemote = externalBridge
            stateLock.unlock()
            if let attachedRemote, attachedRemote.isAttached() { return .success(.remote(attachedRemote)) }
            guard let attachedBridge, attachedBridge.isAlive() else {
                return .failure(ClashRuntimeError.notAttached)
            }
            return .success(.owned(attachedBridge))
        }
    }

    private func invalidateDeadBridge(_ dead: RuntimeBridge, reason: String) {
        stateLock.lock()
        let matched = (bridge === dead)
        if matched {
            bridge = nil
            if state == .attached { state = .unavailable(reason) }
        }
        stateLock.unlock()
        if matched { dead.stop() }
    }

    private func isSwitchCancelled(_ token: SwitchRequestToken) -> Bool {
        stateLock.lock(); let stopped = stopRequested; stateLock.unlock()
        return stopped || token.isCancelled
    }

    private func checkNotStopped() throws {
        stateLock.lock()
        let stopped = stopRequested || executingStartGeneration.map { $0 != lifecycleGeneration } == true
        stateLock.unlock()
        if stopped { throw ClashRuntimeError.cancelled }
    }

    private func promptAccessibility(openSettings: Bool, alwaysOpenSettings: Bool = false) {
        guard ProcessInfo.processInfo.environment["CLASH_DISABLE_ACCESSIBILITY_PROMPT"] != "1" else { return }
        stateLock.lock()
        if !alwaysOpenSettings && promptedAccessibility {
            stateLock.unlock()
            return
        }
        if alwaysOpenSettings, let last = lastAccessibilitySettingsPromptAt,
           Date().timeIntervalSince(last) < 1.5 {
            stateLock.unlock()
            return
        }
        promptedAccessibility = true
        lastAccessibilitySettingsPromptAt = Date()
        stateLock.unlock()
        DispatchQueue.main.async {
            guard NSApp.activationPolicy() == .regular else { return }
            let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
            if openSettings, let url = self.accessibilitySettingsURL {
                let config = NSWorkspace.OpenConfiguration()
                config.activates = true
                NSWorkspace.shared.open(url, configuration: config, completionHandler: nil)
            }
        }
    }

    private static func isScreenLocked() -> Bool {
        guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
        return (dict["CGSSessionScreenIsLocked"] as? Bool) == true
    }

    private func currentBridge() -> RuntimeBridge? {
        stateLock.lock(); defer { stateLock.unlock() }
        return bridge
    }

    private func finishStart(_ result: Result<Void, Error>, generation: UInt64) {
        stateLock.lock()
        guard lifecycleGeneration == generation, startInFlight, !stopRequested else { stateLock.unlock(); return }
        startInFlight = false
        if case .success = result {
            state = .attached
        } else if case .failure(let error) = result {
            state = stopRequested ? .idle : .unavailable(error.localizedDescription)
        }
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
            try checkNotStopped()
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
        if let broker = readAvailableBroker(), broker.isAttached() { return broker }
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

    private func readAvailableBroker() -> ExternalRuntimeBridge? {
        guard let descriptor = RuntimeBrokerSupport.readDescriptor() else { return nil }
        let broker = ExternalRuntimeBridge(port: descriptor.port, token: descriptor.token, sessionID: descriptor.sessionID)
        return broker.isAvailable() ? broker : nil
    }

    fileprivate static let loopbackSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 5
        config.timeoutIntervalForResource = 30
        return URLSession(configuration: config)
    }()

    private func showOfficialWindow(_ record: SingletonRecord) throws {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(record.port)/commands/visible")!)
        request.httpMethod = "GET"
        request.timeoutInterval = 5
        request.setValue(record.token, forHTTPHeaderField: "x-instance-token")
        let semaphore = DispatchSemaphore(value: 0)
        var status = 0
        Self.loopbackSession.dataTask(with: request) { _, response, _ in
            status = (response as? HTTPURLResponse)?.statusCode ?? 0
            semaphore.signal()
        }.resume()
        guard semaphore.wait(timeout: .now() + 5) == .success, status == 200 else {
            throw ClashRuntimeError.officialWindowUnavailable
        }
    }

    private struct InspectorUI {
        let window: AXUIElement
        let area: AXUIElement
        let closeButton: AXUIElement
    }

    private static let proxiesNavigationLabels: Set<String> = [
        "代理", "proxies", "proxy", "proksi", "プロキシ", "프록시", "прокси", "الوكلاء", "پراکسی‌ها", "vekil'ler"
    ]

    private static let settingsNavigationLabels: Set<String> = [
        "设置", "設置", "設定", "设定", "settings", "einstellungen", "ajustes", "pengaturan",
        "설정", "настройки", "ayarlar", "көйләүләр", "الإعدادات", "تنظیمات"
    ]

    private final class BootstrapUIContext {
        let generation: UInt64
        let startedAt = Date()
        let previousFrontApp: NSRunningApplication?
        var app: NSRunningApplication?
        var initiallyRunning = false
        var initiallyHidden = false
        var capturedWindowState = false
        var initialWindows: [(element: AXUIElement, minimized: Bool)] = []
        var initialInspector: InspectorUI?
        var initialInspectorTab: AXUIElement?
        var ownedInspector: InspectorUI?
        var openedInspector = false
        var changedInspectorTab = false
        var initialNavigation: AXUIElement?
        var changedNavigation = false
        var didInject = false
        var touchedUI = false

        init(generation: UInt64, previousFrontApp: NSRunningApplication?) {
            self.generation = generation
            self.previousFrontApp = previousFrontApp
        }
    }

    private func captureBootstrapUI(generation: UInt64) -> BootstrapUIContext {
        let ui = BootstrapUIContext(generation: generation, previousFrontApp: NSWorkspace.shared.frontmostApplication)
        guard let app = runningOfficialApp() else { return ui }
        ui.app = app
        ui.initiallyRunning = true
        ui.initiallyHidden = app.isHidden
        guard AXIsProcessTrusted(), !Self.isScreenLocked() else { return ui }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(axApp, 1)
        ui.initialWindows = allWindows(axApp).map { ($0, attribute($0, kAXMinimizedAttribute) as? Bool == true) }
        ui.capturedWindowState = true
        ui.initialInspector = findInspector(axApp)
        if let inspector = ui.initialInspector {
            ui.initialInspectorTab = findElement(
                inspector.area,
                roles: [kAXRadioButtonRole],
                predicate: { role, _ in role == kAXRadioButtonRole },
                matching: {
                    (self.attribute($0, kAXValueAttribute) as? NSNumber)?.boolValue == true ||
                    self.attribute($0, kAXSelectedAttribute) as? Bool == true
                },
                maxDepth: 8
            )
        }
        if let official = findOfficialPage(axApp),
           let currentPath = Self.normalizedTauriPath(official.url) {
            let labels: [String: Set<String>] = [
                "/": ["首页", "首頁", "home"],
                "/home": ["首页", "首頁", "home"],
                "/proxies": Self.proxiesNavigationLabels,
                "/profile": ["订阅", "訂閱", "profiles"],
                "/connections": ["连接", "連接", "連線", "connections"],
                "/rules": ["规则", "規則", "rules"],
                "/logs": ["日志", "日誌", "logs"],
                "/unlock": ["测试", "測試", "解鎖", "test"],
                "/settings": Self.settingsNavigationLabels
            ]
            if let candidates = labels[currentPath] {
                ui.initialNavigation = findNavigationButton(in: official.page, labels: candidates)
            }
        }
        return ui
    }

    private func restoreBootstrapUI(_ ui: BootstrapUIContext) {
        stateLock.lock()
        let ownsTurn = executingStartGeneration == ui.generation
        stateLock.unlock()
        guard ownsTurn, ui.touchedUI, AXIsProcessTrusted(), !Self.isScreenLocked(),
              let app = ui.app ?? runningOfficialApp(), !app.isTerminated,
              runningOfficialApp()?.processIdentifier == app.processIdentifier else { return }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(axApp, 1)
        let currentInspector = findInspector(axApp)
        var diagnostic: [String: Any] = [
            "pid": app.processIdentifier, "initialInspector": ui.initialInspector != nil,
            "openedInspector": ui.openedInspector, "ownedInspector": ui.ownedInspector != nil,
            "currentInspector": currentInspector != nil, "didInject": ui.didInject,
            "originalNavigation": ui.initialNavigation != nil, "changedNavigation": ui.changedNavigation
        ]
        if let owned = ui.ownedInspector, let current = currentInspector {
            diagnostic["sameArea"] = CFEqual(owned.area, current.area)
            diagnostic["sameWindow"] = CFEqual(owned.window, current.window)
        }
        if let current = currentInspector, Self.shouldCloseInspector(
            openedByBootstrap: ui.openedInspector, existedBefore: ui.initialInspector != nil,
            owned: ui.ownedInspector?.window, current: current.window
        ) {
            let result = AXUIElementPerformAction(current.closeButton, kAXPressAction as CFString)
            diagnostic["closeResult"] = result.rawValue
            if result == .success {
                Thread.sleep(forTimeInterval: 0.01)
                for _ in 0..<20 {
                    guard let remaining = findInspector(axApp), CFEqual(current.window, remaining.window) else { break }
                    Thread.sleep(forTimeInterval: 0.015)
                }
            }
        } else if ui.changedInspectorTab, let initial = ui.initialInspector,
                  let current = findInspector(axApp), CFEqual(initial.area, current.area),
                  let tab = ui.initialInspectorTab {
            _ = AXUIElementPerformAction(tab, kAXPressAction as CFString)
        }
        let currentPath = Self.normalizedTauriPath(findOfficialPage(axApp)?.url)
        var navigatedToProxies = currentPath == "/proxies"
        if Self.shouldNavigateToProxies(didInject: ui.didInject, currentPath: currentPath) {
            navigatedToProxies = navigateToProxiesTab(axApp)
        } else if !ui.didInject, ui.changedNavigation, currentPath == "/settings" {
            if let navigation = ui.initialNavigation {
                _ = AXUIElementPerformAction(navigation, kAXPressAction as CFString)
            } else {
                _ = navigateToProxiesTab(axApp)
            }
        }
        for window in ui.initialWindows where window.minimized {
            _ = AXUIElementSetAttributeValue(window.element, kAXMinimizedAttribute as CFString, kCFBooleanTrue)
        }
        let stillFrontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
        if ui.initiallyHidden || !ui.initiallyRunning || (ui.capturedWindowState && ui.initialWindows.isEmpty) {
            _ = app.hide()
        }
        if stillFrontmost, let previous = ui.previousFrontApp, !previous.isTerminated,
           previous.processIdentifier != app.processIdentifier {
            _ = previous.activate(options: [])
        }
        diagnostic["navigatedToProxies"] = navigatedToProxies
        diagnostic["finalPath"] = Self.normalizedTauriPath(findOfficialPage(axApp)?.url) ?? ""
        diagnostic["elapsedMs"] = Int((Date().timeIntervalSince(ui.startedAt) * 1000).rounded())
        diagnostic["inspectorRemaining"] = findInspector(axApp) != nil
        recordBootstrapDiagnostic(diagnostic)
    }

    private func recordBootstrapDiagnostic(_ value: [String: Any]) {
        guard !RuntimeBrokerSupport.disabled else { return }
        let root = RuntimeBrokerSupport.root
        var info = stat()
        guard lstat(root.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0,
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        let path = root.appendingPathComponent("bootstrap-status.json")
        try? data.write(to: path, options: [.atomic])
        _ = chmod(path.path, 0o600)
    }

    private static func normalizedLabel(_ value: String) -> String {
        value.filter { !$0.isWhitespace }.lowercased()
    }

    private static func normalizedTauriPath(_ url: URL?) -> String? {
        guard let url, url.scheme == "tauri", url.host == "localhost" else { return nil }
        return url.path.isEmpty ? "/" : url.path
    }

    private static func shouldNavigateToProxies(didInject: Bool, currentPath: String?) -> Bool {
        guard didInject else { return false }
        return currentPath != "/proxies"
    }

    private static func shouldCloseInspector(openedByBootstrap: Bool, existedBefore: Bool,
                                             owned: AXUIElement?, current: AXUIElement?) -> Bool {
        guard openedByBootstrap, !existedBefore, let owned, let current else { return false }
        return CFEqual(owned, current)
    }

    private func isOfficialMainWebArea(_ element: AXUIElement, role: String) -> Bool {
        guard role == "AXWebArea" else { return false }
        if let url = attribute(element, kAXURLAttribute) as? URL {
            return url.scheme == "tauri" && url.host == "localhost"
        }
        let title = attribute(element, kAXTitleAttribute) as? String ?? ""
        if title == "Clash Verge" { return true }
        let description = attribute(element, kAXDescriptionAttribute) as? String ?? ""
        return description == "Clash Verge"
    }

    private func findOfficialPage(_ app: AXUIElement) -> (window: AXUIElement, page: AXUIElement, url: URL?)? {
        for window in allWindows(app) {
            if let page = findElement(
                window,
                roles: ["AXWebArea"],
                predicate: { role, _ in role == "AXWebArea" },
                matching: { self.isOfficialMainWebArea($0, role: "AXWebArea") },
                skipSubtree: { role, _ in role == "AXWebArea" },
                maxDepth: 6
            ) {
                let url = attribute(page, kAXURLAttribute) as? URL
                return (window, page, url)
            }
        }
        return nil
    }

    private func findNavigationButton(in root: AXUIElement, labels: Set<String>, maxDepth: Int = 8) -> AXUIElement? {
        findElement(
            root,
            roles: [kAXButtonRole],
            predicate: { role, title in
                role == kAXButtonRole && labels.contains(Self.normalizedLabel(title))
            },
            maxDepth: maxDepth
        )
    }

    private func findProxiesNavigationButton(_ app: AXUIElement) -> AXUIElement? {
        if let official = findOfficialPage(app),
           let button = findNavigationButton(in: official.page, labels: Self.proxiesNavigationLabels) {
            return button
        }
        for window in allWindows(app) {
            if let button = findNavigationButton(in: window, labels: Self.proxiesNavigationLabels, maxDepth: 12) {
                return button
            }
        }
        return nil
    }

    private func navigateToProxiesTab(_ axApp: AXUIElement) -> Bool {
        if Self.normalizedTauriPath(findOfficialPage(axApp)?.url) == "/proxies" {
            return true
        }
        guard let button = findProxiesNavigationButton(axApp),
              AXUIElementPerformAction(button, kAXPressAction as CFString) == .success else {
            return false
        }
        for poll in 0..<24 {
            Thread.sleep(forTimeInterval: 0.012)
            if Self.normalizedTauriPath(findOfficialPage(axApp)?.url) == "/proxies" {
                return true
            }
            if poll == 8, let retryButton = findProxiesNavigationButton(axApp) {
                _ = AXUIElementPerformAction(retryButton, kAXPressAction as CFString)
            }
        }
        return Self.normalizedTauriPath(findOfficialPage(axApp)?.url) == "/proxies"
    }

    private func findDeveloperToolsButton(_ app: AXUIElement) -> AXUIElement? {
        let isDevTools: (String, String) -> Bool = { role, title in
            guard role == kAXButtonRole else { return false }
            return title.contains("开发者工具") ||
                title.contains("開發人員工具") ||
                title.contains("開発者ツール") ||
                title.contains("개발자 도구") ||
                title.localizedCaseInsensitiveContains("developer tools") ||
                title.localizedCaseInsensitiveContains("dev tools") ||
                title.localizedCaseInsensitiveContains("devtools")
        }
        if let official = findOfficialPage(app) {
            let path = Self.normalizedTauriPath(official.url)
            if path == nil || path == "/settings" {
                return findElement(
                    official.page,
                    roles: [kAXButtonRole],
                    predicate: isDevTools,
                    reverseChildren: true,
                    maxDepth: 22
                )
            }
            return nil
        }
        for window in allWindows(app) {
            if let candidate = findElement(
                window,
                roles: [kAXButtonRole],
                predicate: isDevTools,
                reverseChildren: true,
                maxDepth: 22
            ) {
                return candidate
            }
        }
        return nil
    }

    private func findInspector(_ app: AXUIElement) -> InspectorUI? {
        for window in allWindows(app) {
            if let area = findElement(
                window,
                roles: ["AXWebArea"],
                predicate: { role, _ in role == "AXWebArea" },
                matching: { candidate in
                    !self.isOfficialMainWebArea(candidate, role: "AXWebArea") &&
                    self.findElement(
                        candidate,
                        roles: [kAXButtonRole],
                        predicate: { role, title in
                            role == kAXButtonRole && (
                                title.localizedCaseInsensitiveContains("dock to") ||
                                title.localizedCaseInsensitiveContains("detach into") ||
                                title.contains("停靠")
                            )
                        },
                        maxDepth: 8
                    ) != nil
                },
                skipSubtree: { role, _ in role == "AXWebArea" },
                maxDepth: 6
            ), let close = findElement(
                area,
                roles: [kAXButtonRole],
                predicate: { role, title in
                    role == kAXButtonRole && ["close", "关闭", "關閉"].contains(Self.normalizedLabel(title))
                },
                maxDepth: 8
            ) {
                return InspectorUI(window: window, area: area, closeButton: close)
            }
        }
        return nil
    }

    private func injectLoader(bridge: RuntimeBridge, ui: BootstrapUIContext) throws {
        try injectLoader(loader: bridge.loaderScript, ui: ui)
    }

    private func captureOwnedInspector(_ ui: BootstrapUIContext) {
        guard ui.openedInspector, ui.initialInspector == nil, ui.ownedInspector == nil, let app = ui.app,
              !app.isTerminated, AXIsProcessTrusted() else { return }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(axApp, 1)
        for poll in 0..<10 {
            if let inspector = findInspector(axApp) {
                ui.ownedInspector = inspector
                return
            }
            if poll < 9 { Thread.sleep(forTimeInterval: 0.015) }
        }
    }

    private func injectLoader(loader: String, ui: BootstrapUIContext) throws {
        guard AXIsProcessTrusted() else { throw ClashRuntimeError.accessibilityRequired }
        guard !Self.isScreenLocked() else { throw ClashRuntimeError.screenLocked }
        guard let app = runningOfficialApp() else { throw ClashRuntimeError.officialAppNotRunning }
        ui.app = app
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(axApp, 1)
        if let found = findConsolePrompt(axApp) {
            try submit(prompt: found.prompt, window: found.window, app: app, loader: loader, ui: ui)
            return
        }
        var windows: [AXUIElement] = []
        for _ in 0..<300 {
            try checkNotStopped()
            windows = allWindows(axApp)
            if !windows.isEmpty { break }
            Thread.sleep(forTimeInterval: 0.025)
        }
        guard !windows.isEmpty else { throw ClashRuntimeError.officialWindowUnavailable }
        _ = AXUIElementSetAttributeValue(axApp, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        _ = app.activate(options: [.activateIgnoringOtherApps])
        var developer: AXUIElement?
        if findInspector(axApp) == nil {
            if let official = findOfficialPage(axApp),
               let currentPath = Self.normalizedTauriPath(official.url),
               currentPath != "/settings",
               let settings = findNavigationButton(in: official.page, labels: Self.settingsNavigationLabels) {
                if AXUIElementPerformAction(settings, kAXPressAction as CFString) == .success {
                    ui.changedNavigation = true
                    Thread.sleep(forTimeInterval: 0.015)
                }
            }
            for attempt in 0..<200 {
                try checkNotStopped()
                if let candidate = findDeveloperToolsButton(axApp) {
                    developer = candidate
                    break
                }
                if attempt > 0 && attempt % 15 == 0 {
                    let official = findOfficialPage(axApp)
                    if Self.normalizedTauriPath(official?.url) != "/settings" {
                        let settingsButton = official.flatMap {
                            findNavigationButton(in: $0.page, labels: Self.settingsNavigationLabels)
                        } ?? allWindows(axApp).compactMap {
                            findNavigationButton(in: $0, labels: Self.settingsNavigationLabels, maxDepth: 12)
                        }.first
                        if let settingsButton,
                           AXUIElementPerformAction(settingsButton, kAXPressAction as CFString) == .success {
                            ui.changedNavigation = true
                        }
                    }
                }
                Thread.sleep(forTimeInterval: 0.015)
            }
        }
        if findInspector(axApp) == nil {
            guard let developer, AXUIElementPerformAction(developer, kAXPressAction as CFString) == .success else {
                throw ClashRuntimeError.developerToolsUnavailable
            }
            ui.openedInspector = true
            ui.ownedInspector = findInspector(axApp)
        }
        var inspectorReadyPolls = 0
        for attempt in 0..<400 {
            try checkNotStopped()
            Thread.sleep(forTimeInterval: attempt == 0 ? 0.012 : 0.015)
            let inspector = ui.ownedInspector ?? findInspector(axApp)
            if ui.openedInspector, ui.ownedInspector == nil, let inspector {
                ui.ownedInspector = inspector
            }
            if let found = findConsolePrompt(axApp, inspectorHint: inspector) {
                try submit(prompt: found.prompt, window: found.window, app: app, loader: loader, ui: ui)
                return
            }
            if let inspector {
                inspectorReadyPolls += 1
                if inspectorReadyPolls == 1 || (inspectorReadyPolls > 1 && inspectorReadyPolls % 12 == 0) {
                    if let consoleTab = findElement(
                        inspector.area,
                        roles: [kAXRadioButtonRole, kAXButtonRole],
                        predicate: { role, title in
                            (role == kAXRadioButtonRole || role == kAXButtonRole) &&
                            (title.localizedCaseInsensitiveContains("console") || title.contains("控制台"))
                        },
                        maxDepth: 8
                    ) {
                        if AXUIElementPerformAction(consoleTab, kAXPressAction as CFString) == .success {
                            ui.changedInspectorTab = true
                        }
                    }
                }
            }
        }
        throw ClashRuntimeError.developerToolsUnavailable
    }

    private func clearPromptInput(source: CGEventSource, pid: pid_t) throws {
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.flags = [.maskCommand]
            event.postToPid(pid)
            usleep(8_000)
        }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 51, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.flags = []
            event.postToPid(pid)
            usleep(8_000)
        }
    }

    private func typeLoaderChunks(_ units: [UInt16],
                                  source: CGEventSource,
                                  pid: pid_t,
                                  chunkDelayUs: useconds_t) throws {
        for offset in stride(from: 0, to: units.count, by: 20) {
            try checkNotStopped()
            let chunk = Array(units[offset..<min(offset + 20, units.count)])
            for down in [true, false] {
                guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else {
                    throw ClashRuntimeError.consoleInputFailed
                }
                event.flags = []
                chunk.withUnsafeBufferPointer { buffer in
                    event.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
                }
                event.postToPid(pid)
            }
            if chunkDelayUs > 0 { usleep(chunkDelayUs) }
        }
    }

    private func submit(prompt: AXUIElement,
                        window: AXUIElement,
                        app: NSRunningApplication,
                        loader: String,
                        ui: BootstrapUIContext) throws {
        guard let source = CGEventSource(stateID: .privateState) else { throw ClashRuntimeError.consoleInputFailed }
        _ = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        if !app.isActive {
            _ = AXUIElementSetAttributeValue(AXUIElementCreateApplication(app.processIdentifier), kAXFrontmostAttribute as CFString, kCFBooleanTrue)
            _ = app.activate(options: [.activateIgnoringOtherApps])
            for _ in 0..<25 {
                if app.isActive { break }
                usleep(6_000)
            }
        }
        let focused = AXUIElementSetAttributeValue(prompt, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        if focused != .success {
            guard let frame = frame(prompt), frame.width > 1, frame.height > 1 else {
                throw ClashRuntimeError.consoleInputFailed
            }
            let point = CGPoint(x: frame.midX, y: frame.midY)
            for mouseType in [CGEventType.leftMouseDown, CGEventType.leftMouseUp] {
                guard let event = CGEvent(mouseEventSource: source, mouseType: mouseType,
                                          mouseCursorPosition: point, mouseButton: .left) else {
                    throw ClashRuntimeError.consoleInputFailed
                }
                event.post(tap: .cghidEventTap)
                usleep(10_000)
            }
        }
        usleep(5_000)
        var entered = ""
        if AXUIElementSetAttributeValue(prompt, kAXValueAttribute as CFString, loader as CFTypeRef) == .success {
            entered = attribute(prompt, kAXValueAttribute) as? String ?? ""
        }
        if entered != loader {
            let existing = attribute(prompt, kAXValueAttribute) as? String ?? ""
            if !existing.isEmpty {
                try clearPromptInput(source: source, pid: app.processIdentifier)
            }
            let units = Array(loader.utf16)
            try typeLoaderChunks(units, source: source, pid: app.processIdentifier, chunkDelayUs: 6_000)
            for check in 0..<24 {
                usleep(12_000)
                entered = attribute(prompt, kAXValueAttribute) as? String ?? ""
                if entered == loader { break }
                if check == 23 { break }
            }
            if entered != loader {
                try clearPromptInput(source: source, pid: app.processIdentifier)
                usleep(20_000)
                try typeLoaderChunks(units, source: source, pid: app.processIdentifier, chunkDelayUs: 18_000)
                for _ in 0..<32 {
                    usleep(25_000)
                    entered = attribute(prompt, kAXValueAttribute) as? String ?? ""
                    if entered == loader { break }
                }
            }
        }
        guard entered == loader else {
            throw ClashRuntimeError.consoleInputFailed
        }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: down) else {
                throw ClashRuntimeError.consoleInputFailed
            }
            event.flags = []
            event.postToPid(app.processIdentifier)
            usleep(5_000)
        }
        ui.didInject = true
    }

    private func allWindows(_ app: AXUIElement) -> [AXUIElement] {
        attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    }

    private func findInspectorWebAreas(_ app: AXUIElement) -> [(window: AXUIElement, area: AXUIElement)] {
        var results: [(window: AXUIElement, area: AXUIElement)] = []
        for window in allWindows(app) {
            func collect(_ element: AXUIElement, depth: Int) {
                guard depth <= 6 else { return }
                let role = attribute(element, kAXRoleAttribute) as? String ?? ""
                if role == "AXWebArea" {
                    if !isOfficialMainWebArea(element, role: role) {
                        results.append((window, element))
                    }
                    return
                }
                for child in (attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []) {
                    collect(child, depth: depth + 1)
                }
            }
            collect(window, depth: 0)
        }
        return results
    }

    private func findConsolePrompt(_ app: AXUIElement,
                                   inspectorHint: InspectorUI? = nil) -> (window: AXUIElement, prompt: AXUIElement)? {
        let candidates: [(window: AXUIElement, area: AXUIElement)]
        if let inspectorHint {
            candidates = [(inspectorHint.window, inspectorHint.area)]
        } else {
            candidates = findInspectorWebAreas(app)
        }
        guard !candidates.isEmpty else { return nil }
        for candidate in candidates {
            var fallback: AXUIElement?
            if let exact = scanConsolePrompt(candidate.area, inConsole: false, maxDepth: 6, fallback: &fallback) {
                return (candidate.window, exact)
            }
            if let fallback {
                return (candidate.window, fallback)
            }
        }
        for candidate in candidates {
            var fallback: AXUIElement?
            if let exact = scanConsolePrompt(candidate.area, inConsole: false, maxDepth: 12, fallback: &fallback) {
                return (candidate.window, exact)
            }
            if let fallback {
                return (candidate.window, fallback)
            }
        }
        return nil
    }

    private func scanConsolePrompt(_ root: AXUIElement,
                                   inConsole: Bool,
                                   maxDepth: Int,
                                   fallback: inout AXUIElement?,
                                   depth: Int = 0) -> AXUIElement? {
        guard depth < maxDepth else { return nil }
        let role = attribute(root, kAXRoleAttribute) as? String ?? ""
        if depth > 0 && isOfficialMainWebArea(root, role: role) {
            return nil
        }
        let isInput = role == kAXTextAreaRole || role == kAXTextFieldRole
        var console = inConsole
        if isInput || !console {
            let title = attribute(root, kAXTitleAttribute) as? String ?? ""
            let description = attribute(root, kAXDescriptionAttribute) as? String ?? ""
            if isInput {
                let help = attribute(root, kAXHelpAttribute) as? String ?? ""
                let isExactPrompt = title.caseInsensitiveCompare("Console prompt") == .orderedSame ||
                    description.caseInsensitiveCompare("Console prompt") == .orderedSame ||
                    help.caseInsensitiveCompare("Console prompt") == .orderedSame ||
                    title == "控制台提示符" || description == "控制台提示符" || help == "控制台提示符"
                if isExactPrompt { return root }
            }
            if !console {
                console = title.localizedCaseInsensitiveContains("console") ||
                    description.localizedCaseInsensitiveContains("console") ||
                    title.contains("控制台") || description.contains("控制台")
            }
        }
        if isInput && console {
            let subrole = attribute(root, kAXSubroleAttribute) as? String ?? ""
            if subrole != kAXSearchFieldSubrole {
                if fallback == nil || role == kAXTextAreaRole {
                    fallback = root
                }
            }
        }
        for child in (attribute(root, kAXChildrenAttribute) as? [AXUIElement] ?? []) {
            if let exact = scanConsolePrompt(child, inConsole: console, maxDepth: maxDepth, fallback: &fallback, depth: depth + 1) {
                return exact
            }
        }
        return nil
    }

    private func findElement(_ root: AXUIElement,
                             roles: Set<String>? = nil,
                             predicate: (String, String) -> Bool,
                             matching: (AXUIElement) -> Bool = { _ in true },
                             skipSubtree: (String, AXUIElement) -> Bool = { _, _ in false },
                             reverseChildren: Bool = false,
                             maxDepth: Int = 28,
                             depth: Int = 0) -> AXUIElement? {
        guard depth < maxDepth else { return nil }
        let role = attribute(root, kAXRoleAttribute) as? String ?? ""
        if roles == nil || roles?.contains(role) == true {
            let title = attribute(root, kAXTitleAttribute) as? String ?? ""
            var matched = !title.isEmpty && predicate(role, title)
            if !matched {
                let description = attribute(root, kAXDescriptionAttribute) as? String ?? ""
                let label = title.isEmpty ? description : title
                if predicate(role, label) || (!description.isEmpty && description != label && predicate(role, description)) {
                    matched = true
                }
            }
            if !matched {
                let help = attribute(root, kAXHelpAttribute) as? String ?? ""
                if !help.isEmpty && predicate(role, help) {
                    matched = true
                }
            }
            if matched && matching(root) { return root }
        }
        if depth > 0 && skipSubtree(role, root) { return nil }
        let children = attribute(root, kAXChildrenAttribute) as? [AXUIElement] ?? []
        for child in (reverseChildren ? Array(children.reversed()) : children) {
            if let result = findElement(
                child,
                roles: roles,
                predicate: predicate,
                matching: matching,
                skipSubtree: skipSubtree,
                reverseChildren: reverseChildren,
                maxDepth: maxDepth,
                depth: depth + 1
            ) {
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

    private func run(_ executable: String, _ arguments: [String], timeout: TimeInterval = 15) throws -> String {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
        let output = Pipe(); process.standardOutput = output; process.standardError = output
        try process.run()
        let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: watchdog)
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            throw ClashRuntimeError.officialSignatureMismatch
        }
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

struct RuntimeBrokerDescriptor: Codable {
    let protocolVersion: Int
    let sessionID: String
    let port: Int
    let token: String
    let pid: Int32
}

enum RuntimeBrokerSupport {
    static var root: URL {
        if let override = ProcessInfo.processInfo.environment["CLASH_RUNTIME_BROKER_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ClashVergeSpeedHelper/Runtime", isDirectory: true)
    }

    static var disabled: Bool { ProcessInfo.processInfo.environment["CLASH_RUNTIME_BROKER_DISABLED"] == "1" }
    static var executable: URL { root.appendingPathComponent("ClashRuntimeBroker") }
    static var manifest: URL { root.appendingPathComponent("state.json") }

    static func readDescriptor() -> RuntimeBrokerDescriptor? {
        guard !disabled else { return nil }
        var info = stat()
        guard lstat(root.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0 else { return nil }
        let descriptorFD = open(manifest.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptorFD >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: descriptorFD, closeOnDealloc: true)
        guard fstat(descriptorFD, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0, info.st_size <= 4096,
              let data = try? handle.readToEnd(),
              let descriptor = try? JSONDecoder().decode(RuntimeBrokerDescriptor.self, from: data),
              descriptor.protocolVersion == 1, UUID(uuidString: descriptor.sessionID) != nil,
              (1...65535).contains(descriptor.port), descriptor.token.utf8.count == 64,
              descriptor.token.allSatisfy({ $0.isHexDigit }), descriptor.pid > 0 else { return nil }
        return descriptor
    }

    static func startIfInstalled() {
        guard !disabled else { return }
        if let descriptor = readDescriptor(),
           ExternalRuntimeBridge(port: descriptor.port, token: descriptor.token, sessionID: descriptor.sessionID).isAvailable() { return }
        var info = stat()
        guard lstat(executable.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o022 == 0,
              FileManager.default.isExecutableFile(atPath: executable.path) else { return }
        let process = Process()
        process.executableURL = executable
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return }
        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline {
            if let descriptor = readDescriptor(),
               ExternalRuntimeBridge(port: descriptor.port, token: descriptor.token, sessionID: descriptor.sessionID).isAvailable() { return }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }
}

/// Client for the standalone bridge produced by outputs/clash-verge-runtime.mjs.
/// It is intentionally limited to status and exact-profile switch requests;
/// it never exposes a general JavaScript or filesystem interface.
private final class ExternalRuntimeBridge {
    private let port: Int
    private let token: String
    private let sessionID: String?

    init(port: Int, token: String, sessionID: String? = nil) {
        self.port = port
        self.token = token
        self.sessionID = sessionID
    }

    var loaderScript: String { RuntimeBridge.makeLoader(port: port, token: token) }

    func isAvailable() -> Bool {
        guard let value = try? request(path: "/status", method: "GET", body: nil, timeout: 1) else { return false }
        guard let sessionID else { return true }
        return value["sessionID"] as? String == sessionID && value["protocolVersion"] as? Int == 1
    }

    func isAttached() -> Bool {
        guard let value = try? request(path: "/status", method: "GET", body: nil, timeout: 3) else { return false }
        if let sessionID, value["sessionID"] as? String != sessionID || value["protocolVersion"] as? Int != 1 { return false }
        return value["attached"] as? Bool == true
    }

    func switchProfile(uid: String,
                       requestID: UUID,
                       isCancelled: @escaping () -> Bool = { false },
                       completion: @escaping (Result<[String: Any], Error>) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            do {
                let body: [String: Any] = ["op": "switch", "target": uid, "id": requestID.uuidString]
                let value = try self.request(path: "/command", method: "POST", body: body, timeout: 30,
                                            isCancelled: isCancelled, requestID: requestID)
                completion(.success(value))
            } catch let error as ClashRuntimeError {
                completion(.failure(error))
            } catch {
                completion(.failure(ClashRuntimeError.switchTimeout))
            }
        }
    }

    private func request(path: String,
                         method: String,
                         body: [String: Any]?,
                         timeout: TimeInterval,
                         isCancelled: () -> Bool = { false }, requestID: UUID? = nil) throws -> [String: Any] {
        if isCancelled() { throw ClashRuntimeError.cancelled }
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else {
            throw ClashRuntimeError.notAttached
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }

        let semaphore = DispatchSemaphore(value: 0)
        var result: Result<(Data, HTTPURLResponse), Error>?
        let task = ClashRuntimeInjector.loopbackSession.dataTask(with: request) { data, response, error in
            defer { semaphore.signal() }
            if let error { result = .failure(error); return }
            guard let data, let response = response as? HTTPURLResponse else {
                result = .failure(ClashRuntimeError.notAttached); return
            }
            result = .success((data, response))
        }
        task.resume()
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if isCancelled() {
                let cancellation = requestID.flatMap { requestID -> [String: Any]? in
                    guard sessionID != nil else { return nil }
                    return try? self.request(path: "/cancel", method: "POST", body: ["id": requestID.uuidString], timeout: 2)
                }
                task.cancel()
                throw cancellation?["cancelledBeforePickup"] as? Bool == true ? ClashRuntimeError.cancelled : ClashRuntimeError.switchTimeout
            }
            if semaphore.wait(timeout: .now() + 0.1) == .success {
                break
            }
        }
        guard let result else {
            task.cancel()
            throw timeout >= 20 ? ClashRuntimeError.switchTimeout : ClashRuntimeError.notAttached
        }
        let (data, response) = try result.get()
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw requestID == nil ? ClashRuntimeError.notAttached : ClashRuntimeError.switchTimeout
        }
        if let code = object["errorCode"] as? String {
            switch code {
            case "not_attached": throw ClashRuntimeError.notAttached
            case "cancelled": throw ClashRuntimeError.cancelled
            case "outcome_unknown", "duplicate": throw ClashRuntimeError.switchTimeout
            default: throw ClashRuntimeError.bridgeFailure((object["error"] as? String) ?? "桥接请求失败。")
            }
        }
        guard (200...299).contains(response.statusCode) else {
            throw requestID == nil ? ClashRuntimeError.notAttached : ClashRuntimeError.switchTimeout
        }
        if let error = object["error"] as? String, !error.isEmpty {
            throw requestID == nil ? ClashRuntimeError.notAttached : ClashRuntimeError.switchTimeout
        }
        return object
    }
}

enum ClashRuntimeError: LocalizedError {
    case officialAppMissing, officialSignatureMismatch, officialAppNotRunning
    case invalidSingletonRecord, officialWindowUnavailable, accessibilityRequired
    case developerToolsUnavailable, consoleInputFailed, notAttached
    case invalidProfileUID, invalidBridgeResult
    case switchTimeout, cancelled, screenLocked, bridgeFailure(String)

    var errorDescription: String? {
        switch self {
        case .officialAppMissing: return "未找到官方 Clash Verge Rev（/Applications/Clash Verge.app）。"
        case .officialSignatureMismatch: return "已安装的 Clash Verge 不是预期的官方 Developer ID 签名。"
        case .officialAppNotRunning: return "Clash Verge 未能启动。"
        case .invalidSingletonRecord: return "Clash Verge 的本机实例记录无效。"
        case .officialWindowUnavailable: return "无法显示 Clash Verge 主窗口。"
        case .accessibilityRequired: return "需要在「系统设置 → 隐私与安全性 → 辅助功能」中授予本助手权限（已尝试自动打开对应设置，授予后返回即可使用；若重装后开关仍显示已开启却无效，请先选中本助手点「-」移除旧记录后再「+」添加并开启）。"
        case .developerToolsUnavailable: return "无法打开 Clash Verge Web Inspector。"
        case .consoleInputFailed: return "无法向 Clash Verge Console 注入加载器。"
        case .notAttached: return "运行时补丁尚未连接或已断开。"
        case .invalidProfileUID: return "订阅 UID 无效。"
        case .invalidBridgeResult: return "Clash Verge 返回了无法验证的切换结果。"
        case .switchTimeout: return "Clash Verge 未返回可确认的切换结果；请求可能已执行，请核对当前订阅。"
        case .cancelled: return "已取消切换订阅。"
        case .screenLocked: return "当前 macOS 处于锁屏状态，无法向 Clash Verge 注入运行时补丁；请解锁后重试。"
        case .bridgeFailure(let message): return message
        }
    }
}

final class RuntimeBridge {
    private struct PendingCommand {
        let completion: (Result<[String: Any], Error>) -> Void
        var pickedUp: Bool
        var client: String?
    }

    private let listenerFD: Int32
    // Keep accepting sockets on a different queue from request handling. The
    // accept loop blocks in accept(2); putting it on the same serial queue as
    // handle(_:) starves every HTTP request (including the CORS preflight), so
    // the WebView reports that /patch is unreachable even though the listener
    // was bound successfully.
    private let acceptWork = DispatchQueue(label: "clash.runtime.bridge.accept", qos: .utility)
    private let work = DispatchQueue(label: "clash.runtime.bridge.requests", qos: .utility, attributes: .concurrent)
    private let lock = NSLock()
    private var stopped = false
    private var shuttingDown = false
    private var attachedClient: String?
    private var lastActivityAt: Date?
    private var inFlightIDs: Set<String> = []
    private var pending: [String: PendingCommand] = [:]
    private var commands: [[String: Any]] = []
    private var attachSemaphore = DispatchSemaphore(value: 0)
    private var hostCommandIDs: Set<String> = []
    private var cancelledHostIDs: Set<String> = []
    private var hostCommandOrder: [String] = []
    private let sessionID: String?
    private let shutdownHandler: (() -> Void)?
    let port: Int
    let token: String
    let loaderScript: String

    init(sessionID: String? = nil, shutdown: (() -> Void)? = nil) throws {
        self.sessionID = sessionID
        self.shutdownHandler = shutdown
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ClashRuntimeError.consoleInputFailed }
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
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
        guard bound == 0, listen(fd, 16) == 0 else { close(fd); throw ClashRuntimeError.consoleInputFailed }
        var actual = sockaddr_in(); var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        guard getsockname(fd, withUnsafeMutablePointer(to: &actual) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { $0 } }, &length) == 0 else {
            close(fd); throw ClashRuntimeError.consoleInputFailed
        }
        listenerFD = fd; port = Int(UInt16(bigEndian: actual.sin_port)); token = UUID().uuidString.replacingOccurrences(of: "-", with: "") + UUID().uuidString.replacingOccurrences(of: "-", with: "")
        loaderScript = RuntimeBridge.makeLoader(port: port, token: token)
        acceptWork.async { [weak self] in self?.acceptLoop() }
    }

    func waitUntilAttached(timeout: TimeInterval) throws {
        guard attachSemaphore.wait(timeout: .now() + timeout) == .success else {
            throw ClashRuntimeError.developerToolsUnavailable
        }
        lock.lock(); let active = !stopped && attachedClient != nil; lock.unlock()
        guard active else { throw ClashRuntimeError.cancelled }
    }

    func isAlive(idleThreshold: TimeInterval = 3.5) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return isAliveLocked(idleThreshold: idleThreshold)
    }

    private func isAliveLocked(idleThreshold: TimeInterval = 3.5) -> Bool {
        guard !stopped, !shuttingDown, attachedClient != nil else { return false }
        if !inFlightIDs.isEmpty { return true }
        guard let lastActivityAt else { return false }
        return Date().timeIntervalSince(lastActivityAt) <= idleThreshold
    }

    func enqueue(id: UUID = UUID(),
                 command: [String: Any],
                 pickupTimeout: TimeInterval = 3.0,
                 executionTimeout: TimeInterval = 25.0,
                 completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let cmdID = id.uuidString
        var item = command; item["id"] = cmdID
        lock.lock()
        guard !cancelledHostIDs.contains(cmdID) else {
            lock.unlock(); completion(.failure(ClashRuntimeError.cancelled)); return
        }
        guard isAliveLocked() else {
            lock.unlock()
            completion(.failure(ClashRuntimeError.notAttached))
            return
        }
        pending[cmdID] = PendingCommand(completion: completion, pickedUp: false, client: nil)
        commands.append(item)
        lock.unlock()

        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + pickupTimeout) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard let entry = self.pending[cmdID], !entry.pickedUp else {
                self.lock.unlock()
                return
            }
            self.pending.removeValue(forKey: cmdID)
            self.commands.removeAll { ($0["id"] as? String) == cmdID }
            // The JS loop polls every ~220ms; failing to pick up within pickupTimeout
            // indicates the injected WebView page is no longer polling.
            self.attachedClient = nil
            self.lock.unlock()
            entry.completion(.failure(ClashRuntimeError.notAttached))
        }

        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + executionTimeout) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard let entry = self.pending.removeValue(forKey: cmdID) else {
                self.lock.unlock()
                return
            }
            self.commands.removeAll { ($0["id"] as? String) == cmdID }
            self.inFlightIDs.remove(cmdID)
            self.lock.unlock()
            entry.completion(.failure(ClashRuntimeError.switchTimeout))
        }
    }

    @discardableResult
    func cancel(id: UUID) -> Bool {
        let cmdID = id.uuidString
        lock.lock()
        commands.removeAll { ($0["id"] as? String) == cmdID }
        inFlightIDs.remove(cmdID)
        let entry = pending.removeValue(forKey: cmdID)
        lock.unlock()
        let beforePickup = entry?.pickedUp == false
        entry?.completion(.failure(beforePickup ? ClashRuntimeError.cancelled : ClashRuntimeError.switchTimeout))
        return beforePickup
    }

    func stop() {
        lock.lock()
        guard !stopped else { lock.unlock(); return }
        stopped = true
        attachedClient = nil
        lastActivityAt = nil
        inFlightIDs.removeAll()
        commands.removeAll()
        let fd = listenerFD
        let entries = Array(pending.values)
        pending.removeAll()
        lock.unlock()
        close(fd)
        attachSemaphore.signal()
        entries.forEach { $0.completion(.failure($0.pickedUp ? ClashRuntimeError.switchTimeout : ClashRuntimeError.notAttached)) }
    }

    private func acceptLoop() {
        while true {
            lock.lock(); let done = stopped; lock.unlock(); if done { return }
            let connection = accept(listenerFD, nil, nil)
            if connection < 0 {
                if errno == EINTR { continue }
                lock.lock(); let finished = stopped; lock.unlock()
                if finished || errno == EBADF || errno == EINVAL { return }
                continue
            }
            work.async { [weak self] in self?.handle(connection) }
        }
    }

    private func handle(_ fd: Int32) {
        defer { close(fd) }
        var nosig: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &nosig, socklen_t(MemoryLayout<Int32>.size))
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

        guard let request = readRequest(fd) else { respond(fd, status: 400, body: ["error": "bad request"]); return }
        if request.method == "OPTIONS" { respond(fd, status: 204, body: nil); return }
        guard authorized(request.headers) else { respond(fd, status: 401, body: ["error": "unauthorized"]); return }
        if sessionID != nil, request.method == "POST", request.path == "/command" {
            guard request.headers["origin"] == nil,
                  request.json["op"] as? String == "switch",
                  let rawID = request.json["id"] as? String, let identifier = UUID(uuidString: rawID),
                  let target = request.json["target"] as? String, !target.isEmpty, target.utf8.count <= 256,
                  !target.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
                respond(fd, status: 400, body: ["errorCode": "invalid_request", "error": "无效的切换请求。"]); return
            }
            let commandID = identifier.uuidString
            lock.lock()
            guard !shuttingDown, pending.count < 32 else { lock.unlock(); respond(fd, status: 429, body: ["errorCode": "busy"]); return }
            guard !hostCommandIDs.contains(commandID) else {
                lock.unlock(); respond(fd, status: 409, body: ["errorCode": "duplicate"]); return
            }
            hostCommandIDs.insert(commandID)
            hostCommandOrder.append(commandID)
            if hostCommandOrder.count > 512 {
                let oldest = hostCommandOrder.removeFirst()
                if pending[oldest] == nil { hostCommandIDs.remove(oldest); cancelledHostIDs.remove(oldest) }
            }
            lock.unlock()
            let completed = DispatchSemaphore(value: 0)
            let resultLock = NSLock()
            var outcome: Result<[String: Any], Error>?
            enqueue(id: identifier, command: ["op": "switch", "uid": target]) { result in
                resultLock.lock(); outcome = result; resultLock.unlock(); completed.signal()
            }
            _ = completed.wait(timeout: .now() + 27)
            resultLock.lock(); let result = outcome; resultLock.unlock()
            switch result {
            case .success(let value): respond(fd, status: 200, body: value)
            case .failure(let error):
                let code: String
                switch error {
                case ClashRuntimeError.notAttached: code = "not_attached"
                case ClashRuntimeError.cancelled: code = "cancelled"
                case ClashRuntimeError.switchTimeout: code = "outcome_unknown"
                default: code = "bridge_failure"
                }
                respond(fd, status: 409, body: ["errorCode": code, "error": error.localizedDescription])
            case nil:
                _ = cancel(id: identifier)
                respond(fd, status: 504, body: ["errorCode": "outcome_unknown"])
            }
            return
        }
        if sessionID != nil, request.method == "POST", request.path == "/cancel" {
            guard request.headers["origin"] == nil,
                  let rawID = request.json["id"] as? String, let identifier = UUID(uuidString: rawID) else {
                respond(fd, status: 400, body: ["error": "id"]); return
            }
            lock.lock()
            let unseen = !hostCommandIDs.contains(identifier.uuidString)
            if unseen { hostCommandIDs.insert(identifier.uuidString); hostCommandOrder.append(identifier.uuidString) }
            cancelledHostIDs.insert(identifier.uuidString)
            lock.unlock()
            let cancelledBeforePickup = cancel(id: identifier) || unseen
            respond(fd, status: 200, body: ["cancelledBeforePickup": cancelledBeforePickup])
            return
        }
        if let shutdownHandler, request.method == "POST", request.path == "/shutdown" {
            lock.lock()
            guard request.headers["origin"] == nil, pending.isEmpty else {
                lock.unlock(); respond(fd, status: 409, body: ["errorCode": "busy"]); return
            }
            shuttingDown = true
            lock.unlock()
            respond(fd, status: 200, body: ["ok": true])
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.1, execute: shutdownHandler)
            return
        }
        if request.method == "GET", request.path == "/patch" { respondRaw(fd, status: 200, contentType: "application/javascript", body: patchScript()); return }
        if request.method == "POST", request.path == "/hello" {
            let client = (request.json["client"] as? String) ?? ""
            guard !client.isEmpty else { respond(fd, status: 400, body: ["error": "client"]); return }
            lock.lock()
            attachedClient = client
            lastActivityAt = Date()
            lock.unlock()
            attachSemaphore.signal()
            respond(fd, status: 200, body: ["ok": true])
            return
        }
        if request.method == "GET", request.path == "/poll" {
            let queryClient = request.query["client"]
            lock.lock()
            guard !stopped, !shuttingDown, let attached = attachedClient, queryClient == nil || queryClient == attached else {
                lock.unlock()
                respond(fd, status: 409, body: ["error": "unknown client"])
                return
            }
            lastActivityAt = Date()
            var picked: [String: Any]?
            while !commands.isEmpty {
                let candidate = commands.removeFirst()
                if let id = candidate["id"] as? String, var entry = pending[id] {
                    entry.pickedUp = true
                    entry.client = attached
                    pending[id] = entry
                    inFlightIDs.insert(id)
                    picked = candidate
                    break
                }
            }
            lock.unlock()
            guard let command = picked else { respond(fd, status: 204, body: nil); return }
            respond(fd, status: 200, body: ["command": command])
            return
        }
        if request.method == "POST", request.path == "/result" {
            guard let id = request.json["id"] as? String else { respond(fd, status: 400, body: ["error": "id"]); return }
            let client = request.json["client"] as? String
            lock.lock()
            if let expected = pending[id]?.client, client != expected {
                lock.unlock(); respond(fd, status: 409, body: ["error": "unknown client"]); return
            }
            if client == nil || client == attachedClient {
                lastActivityAt = Date()
            }
            inFlightIDs.remove(id)
            let entry = pending.removeValue(forKey: id)
            lock.unlock()
            if let entry {
                if (request.json["ok"] as? Bool) == true, let value = request.json["value"] as? [String: Any] {
                    entry.completion(.success(value))
                } else {
                    let raw = (request.json["error"] as? String) ?? ""
                    let sanitized = Self.sanitizeBridgeError(raw)
                    entry.completion(.failure(ClashRuntimeError.bridgeFailure(sanitized)))
                }
            }
            respond(fd, status: 200, body: ["ok": true])
            return
        }
        if request.method == "GET", request.path == "/status" {
            let attached = isAlive()
            var status: [String: Any] = ["attached": attached]
            if let sessionID { status["sessionID"] = sessionID; status["protocolVersion"] = 1; status["pid"] = getpid() }
            respond(fd, status: 200, body: status)
            return
        }
        respond(fd, status: 404, body: ["error": "not found"])
    }

    private static func sanitizeBridgeError(_ raw: String) -> String {
        switch raw {
        case "profile not found":
            return "未在 Clash Verge 中找到目标订阅。"
        case "official transaction not verified":
            return "Clash Verge 未确认订阅事务完成。"
        case "unsupported operation":
            return "Clash Verge 运行时不支持该操作。"
        default:
            return "Clash Verge 返回了无法验证的切换结果。"
        }
    }

    private struct Request {
        let method: String
        let path: String
        let query: [String: String]
        let headers: [String: String]
        let json: [String: Any]
    }

    private func readRequest(_ fd: Int32) -> Request? {
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
        while data.range(of: Data("\r\n\r\n".utf8)) == nil && data.count < 65536 {
            let count = read(fd, &buffer, buffer.count)
            if count < 0 { if errno == EINTR { continue }; return nil }
            if count == 0 { return nil }
            data.append(buffer, count: count)
        }
        guard let marker = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let headBytes = data[..<marker.lowerBound]
        let lines = headBytes.split(separator: 0x0A, omittingEmptySubsequences: false).map { rawLine -> String in
            let trimmed = rawLine.last == 0x0D ? rawLine.dropLast() : rawLine[...]
            return String(decoding: trimmed, as: UTF8.self).trimmingCharacters(in: .whitespaces)
        }
        guard let first = lines.first, !first.isEmpty else { return nil }
        let parts = first.split(separator: " ")
        guard parts.count >= 2 else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() where !line.isEmpty {
            let p = line.split(separator: ":", maxSplits: 1)
            if p.count == 2 {
                headers[String(p[0]).trimmingCharacters(in: .whitespaces).lowercased()] = String(p[1]).trimmingCharacters(in: .whitespaces)
            }
        }
        let length: Int
        if let rawLength = headers["content-length"] {
            guard let parsed = Int(rawLength), (0...65536).contains(parsed) else { return nil }
            length = parsed
        } else {
            length = 0
        }
        var body = Data(data[marker.upperBound...])
        while body.count < length {
            let count = read(fd, &buffer, buffer.count)
            if count < 0 { if errno == EINTR { continue }; return nil }
            if count == 0 { return nil }
            body.append(buffer, count: count)
        }
        let json = (try? JSONSerialization.jsonObject(with: body.prefix(length))) as? [String: Any] ?? [:]
        let rawTarget = String(parts[1])
        let components = URLComponents(string: rawTarget)
        let path = components?.path ?? String(rawTarget.split(separator: "?").first ?? "")
        var query: [String: String] = [:]
        for item in components?.queryItems ?? [] {
            if let value = item.value { query[item.name] = value }
        }
        return Request(method: String(parts[0]), path: path, query: query, headers: headers, json: json)
    }

    private func authorized(_ headers: [String: String]) -> Bool { headers["authorization"] == "Bearer \(token)" }

    private func respond(_ fd: Int32, status: Int, body: [String: Any]?) {
        if status == 204 { respondRaw(fd, status: status, contentType: "application/json", body: ""); return }
        let data = (try? JSONSerialization.data(withJSONObject: body ?? [:])) ?? Data("{}".utf8)
        respondRaw(fd, status: status, contentType: "application/json", body: String(data: data, encoding: .utf8) ?? "{}")
    }

    private func respondRaw(_ fd: Int32, status: Int, contentType: String, body: String) {
        let reason = status == 200 ? "OK" : status == 204 ? "No Content" : status == 409 ? "Conflict" : "Error"
        let lengthHeader = status == 204 ? "" : "Content-Length: \(body.utf8.count)\r\n"
        let text = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: \(contentType)\r\nAccess-Control-Allow-Origin: tauri://localhost\r\nAccess-Control-Allow-Headers: Authorization, Content-Type, Cache-Control\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nCache-Control: no-store\r\n\(lengthHeader)Connection: close\r\n\r\n\(body)"
        let bytes = Array(text.utf8)
        var sent = 0
        while sent < bytes.count {
            let written = bytes.withUnsafeBytes { raw -> Int in
                guard let base = raw.baseAddress else { return -1 }
                return write(fd, base.advanced(by: sent), bytes.count - sent)
            }
            if written > 0 { sent += written; continue }
            if written < 0 && errno == EINTR { continue }
            break
        }
    }

    private func patchScript() -> String {
        let base = "http://127.0.0.1:\(port)"
        return RuntimeBridge.makePatch(base: base, token: token)
    }

    fileprivate static func makeLoader(port: Int, token: String) -> String {
        let base = "http://127.0.0.1:\(port)/patch"
        return "fetch(\(json(base)),{cache:'no-store',headers:{Authorization:\(json("Bearer \(token)"))}}).then(r=>{if(!r.ok)throw Error(r.status);return r.text()}).then(eval);"
    }

    private static func makePatch(base: String, token: String) -> String {
        let template = #"""
        (()=>{const B=__BASE__,A=__AUTH__,S='__SLOT__',W=ms=>new Promise(r=>setTimeout(r,ms));if(!window.__TAURI_INTERNALS__?.invoke||location.origin!=='tauri://localhost')throw Error('Tauri page required');window[S]?.stop?.();let live=true,errs=0;const C=crypto.randomUUID(),I=window.__TAURI_INTERNALS__.invoke;const J=(p,m='GET',b)=>fetch(B+p,{method:m,cache:'no-store',headers:{Authorization:A,'Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)});const stop=()=>{live=false;delete window[S]};const T0=Date.now();window[S]={stop,client:C};(async()=>{try{await J('/hello','POST',{client:C,origin:location.origin});console.info('[Clash runtime] official bridge attached');while(live){let r;try{r=await J('/poll?client='+encodeURIComponent(C));if(!r.ok&&r.status!==204){if(r.status===401||r.status===409)throw Error('HTTP '+r.status);throw Error('poll '+r.status)}}catch(pe){if(!live)break;if(++errs>=8||String(pe?.message||'').startsWith('HTTP '))throw pe;await W(400);continue}errs=0;const P=Date.now()-T0<3000?35:220;if(r.status===204){await W(P);continue}const q=await r.json();if(!q.command){await W(P);continue}let out;try{if(q.command.op==='switch'){const before=await I('get_profiles');const target=q.command.uid;const matches=(before.items||[]).filter(x=>x&&x.uid===target);if(matches.length!==1)throw Error('profile not found');const outcome=await I('patch_profiles_config',{profiles:{current:target}});const after=await I('get_profiles');if(outcome?.status!=='valid'||after.current!==target)throw Error('official transaction not verified');out={ok:true,value:{previous:before.current||'',current:after.current,name:matches[0].name||target}}}else if(q.command.op==='detach'){out={ok:true,value:{detached:true}}}else throw Error('unsupported operation')}catch(e){out={ok:false,error:String(e?.message||e)}}await J('/result','POST',{client:C,id:q.command.id,...out});if(q.command.op==='detach')stop()}}catch(e){console.error('[Clash runtime] stopped',String(e?.message||e));stop()}})()})()
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
