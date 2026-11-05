import Foundation
import Darwin

// Shared by ranking and automatic application: an informational pseudo-node
// may still be chosen explicitly by the user but must never win automatically.
func multiAutomaticNodeIsCandidate(_ name: String) -> Bool {
    let lowered = name.lowercased()
    let metadata = ["剩余流量", "到期", "流量重置", "重置时间", "过期时间", "订阅信息",
                    "traffic remaining", "remaining traffic", "subscription expires", "expiration date"]
    return !metadata.contains(where: { lowered.contains($0) })
}

/// Failures contain no paths, YAML, URLs, controller secrets, or OS diagnostics.
public enum AssistedSwitchFailure: Error, Equatable, Sendable {
    case invalidInput
    case profileUnavailable
    case profileChanged
    case snapshotChanged
    case controllerUnavailable
    case controllerChanged
    case groupNotPrepared
    case preparationRequired
    case busy
    case previousOutcomeUnknown
    case cancelled
    case controller(SwitchFailure)
}

/// All groups are addressable Selectors with the winning node as a direct
/// member. This is a catalogue, not consent; the caller must ask the user.
/// GLOBAL can appear, but is never suggested or implicitly selected. No group
/// suggestion is made: one matching node cannot prove a full-profile match.
public struct AssistedSwitchGroups: Sendable {
    public let groups: [ProxyGroup]
}

public enum AssistedSwitchPreparation: Sendable {
    case manualProfileRequired
    case ready(AssistedSwitchGroups)
    case notAttempted(AssistedSwitchFailure)
}

public enum AssistedSwitchResult: Equatable, Sendable {
    case manualProfileRequired
    case confirmedNoOp
    case switched
    case notAttempted(AssistedSwitchFailure)
    case outcomeUnknown(SwitchUncertainty)
}

private final class AssistedMainCallback<Value: Sendable>: @unchecked Sendable {
    private let callback: (Value) -> Void
    init(_ callback: @escaping (Value) -> Void) { self.callback = callback }
    func deliver(_ value: Value) { DispatchQueue.main.async { self.callback(value) } }
}

/// Coordinates either a *single globally selected winner* or an explicitly
/// selected manual node, not a ranking of nodes. For automatic switching the
/// caller must supply the exact job name, profile UID/path, and immutable YAML
/// bytes used to obtain `winner`, after choosing the winner across subscriptions.
/// For manual switching the caller supplies the selected node and profile
/// snapshot without asserting that any measurement exists or was successful.
/// Both paths require explicit consent to a group from the returned catalogue.
/// This API cannot establish the provenance of caller-supplied inputs.
///
/// `settings.socket` must already have been selected by the caller's unique,
/// trusted same-user socket locator. This class rejects non-sockets, symlinks,
/// wrong owners, and identity changes; it does not discover or authenticate a
/// controller process or prove uniqueness. The controller path is never logged.
///
/// Preparation and application are asynchronous from any thread. Their
/// completions are always dispatched asynchronously on the main queue. After
/// `manualProfileRequired`, the user must switch profiles in Clash Verge and
/// explicitly call prepare again. There is no timer or GUI automation. One apply
/// consumes a preparation, even if it fails; prepare and consent again before
/// another attempt. Following outcomeUnknown this instance refuses all further
/// preparations/attempts. The caller must not automatically create a new
/// instance and retry an uncertain PUT; first reconcile the actual state.
///
/// Checks reduce races, but cannot be atomic with external clients, filesystem
/// writers, profile reloads, or replacement of a socket between check and use.
/// An asynchronous profile restore may later override even verified selection.
/// The raw measured profile is not proof of a runtime endpoint or its identity.
/// This coordinator does not modify files, inject Tauri, or manipulate the GUI.
public final class AssistedSwitchCoordinator: @unchecked Sendable {
    private static let maximumSnapshotBytes = 1 * 1024 * 1024
    private static let maximumTextBytes = 8 * 1024

    private let root: URL
    private let settings: ControllerSettings
    private enum Intent {
        case automatic(VerifiedMeasurement)
        case manual
    }
    private let intent: Intent
    private let nodeName: String
    private let profileUID: String
    private let profilePath: String
    private let snapshot: Data
    private let snapshotSizeValid: Bool
    private let controller: ClashController
    private let queue = DispatchQueue(label: "AssistedSwitchCoordinator", qos: .userInitiated)

    // Only the coordinator queue mutates this state. The pin itself is immutable
    // once created and may also be used by the controller's pre-PUT guard.
    private var pin: AssistedDirectoryPin?
    private var socketIdentity: AssistedSocketIdentity?
    private var prepared: [ProxyGroup]?
    private var busy = false
    private var outcomeUnknown = false

    public init(root: URL, settings: ControllerSettings, winner: VerifiedMeasurement,
                nodeName: String, profileUID: String, profilePath: String,
                measuredYAML: Data) {
        self.root = root
        self.settings = settings
        self.intent = .automatic(winner)
        self.nodeName = nodeName
        self.profileUID = profileUID
        self.profilePath = profilePath
        self.snapshotSizeValid = !measuredYAML.isEmpty && measuredYAML.count <= Self.maximumSnapshotBytes
        // Detach even Data created with bytesNoCopy from externally mutable memory.
        self.snapshot = self.snapshotSizeValid ? measuredYAML.withUnsafeBytes { Data($0) } : Data()
        self.controller = ClashController(settings: settings)
    }

    /// Select an explicitly chosen node without asserting a measurement.
    /// The caller must supply the exact selected profile's snapshot and obtain
    /// consent for a returned direct-member group, as in the automatic path.
    public init(root: URL, settings: ControllerSettings, manualNodeName: String,
                profileUID: String, profilePath: String, measuredYAML: Data) {
        self.root = root
        self.settings = settings
        self.intent = .manual
        self.nodeName = manualNodeName
        self.profileUID = profileUID
        self.profilePath = profilePath
        self.snapshotSizeValid = !measuredYAML.isEmpty && measuredYAML.count <= Self.maximumSnapshotBytes
        // Detach even Data created with bytesNoCopy from externally mutable memory.
        self.snapshot = self.snapshotSizeValid ? measuredYAML.withUnsafeBytes { Data($0) } : Data()
        self.controller = ClashController(settings: settings)
    }

    public func prepare(completion: @escaping (AssistedSwitchPreparation) -> Void) {
        let callback = AssistedMainCallback(completion)
        queue.async {
            guard !self.busy else { callback.deliver(.notAttempted(.busy)); return }
            guard !self.outcomeUnknown else { callback.deliver(.notAttempted(.previousOutcomeUnknown)); return }
            self.prepared = nil
            guard self.validInput() else { callback.deliver(.notAttempted(.invalidInput)); return }
            self.busy = true
            guard let directory = self.getOrCreatePin() else {
                self.busy = false; callback.deliver(.notAttempted(.profileUnavailable)); return
            }
            let first = self.checkProfile(directory)
            guard case .valid = first else {
                self.busy = false; callback.deliver(Self.preparation(first)); return
            }
            guard let socket = self.getOrCreateSocket() else {
                self.busy = false
                callback.deliver(.notAttempted(self.socketIdentity == nil ? .controllerUnavailable : .controllerChanged))
                return
            }
            self.controller.eligibleGroups(nodeNames: [self.nodeName]) { result in
                self.queue.async {
                    self.busy = false
                    let check = self.checkEnvironment(directory, socket)
                    guard case .valid = check else { callback.deliver(Self.preparation(check)); return }
                    switch result {
                    case .failure(let error): callback.deliver(.notAttempted(.controller(error)))
                    case .success(let groups):
                        // ClashController filters Selector/addressability/direct membership.
                        // Preserve the exact returned `now` for the consented group.
                        self.prepared = groups
                        callback.deliver(.ready(AssistedSwitchGroups(groups: groups)))
                    }
                }
            }
        }
    }

    /// `group` must exactly match a returned name (UTF-8 bytes). The caller is
    /// responsible for obtaining user confirmation before every apply call.
    /// Production callers must explicitly provide `authorization`; its default
    /// always returns true and exists only for source compatibility. The closure
    /// can run on different background queues, so it and any captured generation
    /// or cancellation state must be thread-safe. A revoked generation must not
    /// become authorized again; synchronize shared state used by completions too.
    /// Authorization is checked before application work and again after profile
    /// and socket validation in the controller's pre-PUT guard. This cannot revoke
    /// an already-sent PUT or eliminate the race between the last check and send.
    public func apply(group: String, authorization: @escaping @Sendable () -> Bool = { true },
                      completion: @escaping (AssistedSwitchResult) -> Void) {
        let callback = AssistedMainCallback(completion)
        queue.async {
            guard authorization() else {
                self.prepared = nil
                callback.deliver(.notAttempted(.cancelled)); return
            }
            guard !self.busy else { callback.deliver(.notAttempted(.busy)); return }
            guard !self.outcomeUnknown else { callback.deliver(.notAttempted(.previousOutcomeUnknown)); return }
            guard let groups = self.prepared else { callback.deliver(.notAttempted(.preparationRequired)); return }
            self.prepared = nil
            guard let selected = groups.first(where: { Self.sameBytes($0.name, group) }) else {
                callback.deliver(.notAttempted(.groupNotPrepared)); return
            }
            guard let directory = self.pin, let socket = self.socketIdentity else {
                callback.deliver(.notAttempted(.preparationRequired)); return
            }
            self.busy = true
            let check = self.checkEnvironment(directory, socket)
            guard case .valid = check else {
                self.busy = false; callback.deliver(Self.application(check)); return
            }
            let guardResult = AssistedGuardResult()
            self.controller.switchAndVerify(group: selected.name, node: self.nodeName,
                                            expectedCurrent: selected.now,
                                            prePutGuard: {
                // This executes inside ClashController immediately before the
                // possible PUT, after it re-fetches the Selector and verifies
                // exact direct membership and captured expectedCurrent.
                let check = self.checkEnvironment(directory, socket)
                guard case .valid = check else {
                    guardResult.store(check)
                    return false
                }
                // This is deliberately the final check after environment
                // validation and immediately before the possible controller PUT.
                guard authorization() else {
                    guardResult.store(.failure(.cancelled))
                    return false
                }
                return true
            }) { result in
                self.queue.async {
                    self.busy = false
                    switch result {
                    case .confirmedNoOp: callback.deliver(.confirmedNoOp)
                    case .switched: callback.deliver(.switched)
                    case .outcomeUnknown(let uncertainty):
                        self.outcomeUnknown = true
                        callback.deliver(.outcomeUnknown(uncertainty))
                    case .notAttempted(.environmentChanged):
                        callback.deliver(Self.application(guardResult.load()))
                    case .notAttempted(let failure): callback.deliver(.notAttempted(.controller(failure)))
                    }
                }
            }
        }
    }

    private static func sameBytes(_ a: String, _ b: String) -> Bool { a.utf8.elementsEqual(b.utf8) }
    private static func validText(_ text: String) -> Bool {
        !text.isEmpty && text.utf8.count <= maximumTextBytes &&
            !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }
    private func validInput() -> Bool {
        guard snapshotSizeValid, Self.validText(nodeName), Self.validText(profileUID),
              Self.validText(profilePath) else { return false }
        switch intent {
        case .manual:
            // Explicit selection is not an assertion of successful measurement.
            return true
        case .automatic(let winner):
            guard Self.sameBytes(winner.nodeName, nodeName),
                  winner.downloadComplete, winner.expectedBytes > 0,
                  winner.receivedBytes == winner.expectedBytes,
                  winner.downloadBytesPerSecond.isFinite, winner.downloadBytesPerSecond > 0,
                  let score = winner.score, score.isFinite else { return false }
            return multiAutomaticNodeIsCandidate(nodeName)
        }
    }
    private func getOrCreatePin() -> AssistedDirectoryPin? {
        if let pin = pin { return pin }
        guard let created = AssistedDirectoryPin(root: root) else { return nil }
        pin = created
        return created
    }
    private func getOrCreateSocket() -> AssistedSocketIdentity? {
        guard let current = AssistedSocketIdentity.read(settings.socket) else { return nil }
        if let expected = socketIdentity { return current == expected ? expected : nil }
        socketIdentity = current
        return current
    }
    private func checkProfile(_ directory: AssistedDirectoryPin) -> AssistedCheck {
        guard directory.pathsStillPinned() else { return .failure(.profileChanged) }
        let options: [ProfileOption]
        do { options = try ProfileRegistry.options(root: root) }
        catch { return .failure(.profileUnavailable) }
        guard let target = options.first(where: { Self.sameBytes($0.uid, profileUID) }),
              Self.sameBytes(target.path, profilePath) else { return .failure(.profileChanged) }
        guard target.isCurrent else { return .manual }
        guard directory.matches(path: profilePath, snapshot: snapshot) else { return .failure(.snapshotChanged) }
        return .valid
    }
    private func checkEnvironment(_ directory: AssistedDirectoryPin, _ socket: AssistedSocketIdentity) -> AssistedCheck {
        let profile = checkProfile(directory)
        guard case .valid = profile else { return profile }
        // Socket validation follows profile validation; application can then
        // perform additional authorization checks before a possible PUT.
        guard AssistedSocketIdentity.read(settings.socket) == socket else { return .failure(.controllerChanged) }
        return .valid
    }
    private static func preparation(_ check: AssistedCheck) -> AssistedSwitchPreparation {
        switch check {
        case .valid: return .notAttempted(.profileUnavailable) // unreachable, fail closed
        case .manual: return .manualProfileRequired
        case .failure(let failure): return .notAttempted(failure)
        }
    }
    private static func application(_ check: AssistedCheck) -> AssistedSwitchResult {
        switch check {
        case .valid: return .notAttempted(.controller(.environmentChanged)) // unreachable
        case .manual: return .manualProfileRequired
        case .failure(let failure): return .notAttempted(failure)
        }
    }
}

private enum AssistedCheck: Sendable {
    case valid
    case manual
    case failure(AssistedSwitchFailure)
}

private final class AssistedGuardResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value: AssistedCheck = .failure(.controller(.environmentChanged))
    func store(_ result: AssistedCheck) { lock.lock(); value = result; lock.unlock() }
    func load() -> AssistedCheck { lock.lock(); defer { lock.unlock() }; return value }
}

private struct AssistedSocketIdentity: Equatable, Sendable {
    let device: dev_t
    let inode: ino_t
    static func read(_ path: String) -> Self? {
        let bytes = Array(path.utf8)
        guard bytes.first == 47, bytes.count > 1, bytes.count < 104,
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
        // Forbid ambiguous lexical paths and embedded symlink final components.
        let components = path.split(separator: "/", omittingEmptySubsequences: false)
        guard components.first == "", components.dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else { return nil }
        var info = stat()
        guard lstat(path, &info) == 0, (info.st_mode & mode_t(S_IFMT)) == mode_t(S_IFSOCK),
              info.st_uid == geteuid() else { return nil }
        return Self(device: info.st_dev, inode: info.st_ino)
    }
}

/// Pins both selected directories. The physical root is reopened component by
/// component without following symlinks, and the logical registry paths are
/// checked against the pins. This accommodates macOS aliases such as /tmp.
private final class AssistedDirectoryPin: @unchecked Sendable {
    private let rootFD: Int32
    private let profilesFD: Int32
    private let rootPath: String
    private let profilesPath: String
    private let rootIdentity: AssistedFileIdentity
    private let profilesIdentity: AssistedFileIdentity

    init?(root: URL) {
        guard root.isFileURL, root.host == nil || root.host == "", root.query == nil, root.fragment == nil else { return nil }
        let rootPath = root.standardizedFileURL.path
        guard rootPath.utf8.first == 47, !rootPath.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
        // Foundation's standardizedFileURL can spell an otherwise legitimate
        // physical root through a system alias (e.g. /tmp -> /private/tmp).
        // Resolve that spelling once, then reopen every physical component with
        // O_NOFOLLOW; the logical root and profiles paths must still lstat to
        // the pinned directory identities, here and at every later check.
        var namedRoot = stat()
        guard lstat(rootPath, &namedRoot) == 0, Self.isDirectory(namedRoot),
              let resolved = rootPath.withCString({ Darwin.realpath($0, nil) }) else { return nil }
        let physicalRoot = String(cString: resolved)
        free(resolved)
        var rootFD = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard rootFD >= 0 else { return nil }
        for component in physicalRoot.split(separator: "/").map(String.init) {
            guard component != ".", component != ".." else { close(rootFD); return nil }
            let next = component.withCString { openat(rootFD, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC) }
            close(rootFD)
            guard next >= 0 else { return nil }
            rootFD = next
        }
        let profilesFD = openat(rootFD, "profiles", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard profilesFD >= 0 else { close(rootFD); return nil }
        var rootInfo = stat(), profilesInfo = stat()
        guard fstat(rootFD, &rootInfo) == 0, fstat(profilesFD, &profilesInfo) == 0,
              Self.isDirectory(rootInfo), Self.isDirectory(profilesInfo) else {
            close(profilesFD); close(rootFD); return nil
        }
        self.rootFD = rootFD; self.profilesFD = profilesFD
        self.rootPath = rootPath
        self.profilesPath = (rootPath == "/" ? "" : rootPath) + "/profiles"
        self.rootIdentity = AssistedFileIdentity(rootInfo)
        self.profilesIdentity = AssistedFileIdentity(profilesInfo)
        guard pathsStillPinned() else { return nil }
    }
    deinit { close(profilesFD); close(rootFD) }

    private static func isDirectory(_ s: stat) -> Bool { (s.st_mode & mode_t(S_IFMT)) == mode_t(S_IFDIR) }
    private static func isRegular(_ s: stat) -> Bool { (s.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG) }
    func pathsStillPinned() -> Bool {
        var openedRoot = stat(), openedProfiles = stat(), namedRoot = stat(), namedProfiles = stat(), relativeProfiles = stat()
        return fstat(rootFD, &openedRoot) == 0 && fstat(profilesFD, &openedProfiles) == 0 &&
            lstat(rootPath, &namedRoot) == 0 && lstat(profilesPath, &namedProfiles) == 0 &&
            fstatat(rootFD, "profiles", &relativeProfiles, AT_SYMLINK_NOFOLLOW) == 0 &&
            Self.isDirectory(openedRoot) && Self.isDirectory(openedProfiles) && Self.isDirectory(namedRoot) &&
            Self.isDirectory(namedProfiles) && Self.isDirectory(relativeProfiles) &&
            AssistedFileIdentity(openedRoot) == rootIdentity && AssistedFileIdentity(namedRoot) == rootIdentity &&
            AssistedFileIdentity(openedProfiles) == profilesIdentity && AssistedFileIdentity(namedProfiles) == profilesIdentity &&
            AssistedFileIdentity(relativeProfiles) == profilesIdentity
    }
    func matches(path: String, snapshot: Data) -> Bool {
        let prefix = Array((profilesPath + "/").utf8), bytes = Array(path.utf8)
        guard bytes.count > prefix.count, bytes.starts(with: prefix) else { return false }
        let filenameBytes = Array(bytes.dropFirst(prefix.count))
        let filename = String(decoding: filenameBytes, as: UTF8.self)
        guard Array(filename.utf8) == filenameBytes,
              !filename.isEmpty, filename != ".", filename != "..", !filename.utf8.contains(47),
              !filename.utf8.contains(92), !filename.utf8.contains(0), pathsStillPinned() else { return false }
        var before = stat()
        guard filename.withCString({ fstatat(profilesFD, $0, &before, AT_SYMLINK_NOFOLLOW) }) == 0,
              Self.isRegular(before), before.st_size > 0, before.st_size <= 1 * 1024 * 1024,
              before.st_size == snapshot.count else { return false }
        let fd = filename.withCString { openat(profilesFD, $0, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC) }
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var opened = stat()
        guard fstat(fd, &opened) == 0, Self.isRegular(opened), AssistedFileVersion(opened) == AssistedFileVersion(before) else { return false }
        var content = Data(), buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n == 0 { break }
            if n < 0 { if errno == EINTR { continue }; return false }
            guard content.count + n <= 1 * 1024 * 1024 else { return false }
            content.append(contentsOf: buffer[0..<n])
        }
        var after = stat(), relative = stat(), named = stat()
        guard fstat(fd, &after) == 0, Self.isRegular(after),
              filename.withCString({ fstatat(profilesFD, $0, &relative, AT_SYMLINK_NOFOLLOW) }) == 0,
              lstat(path, &named) == 0, Self.isRegular(relative), Self.isRegular(named),
              AssistedFileVersion(opened) == AssistedFileVersion(after),
              AssistedFileVersion(after) == AssistedFileVersion(relative),
              AssistedFileVersion(after) == AssistedFileVersion(named),
              content.count == Int(after.st_size), content == snapshot, pathsStillPinned() else { return false }
        return true
    }
}

private struct AssistedFileIdentity: Equatable {
    let device: dev_t
    let inode: ino_t
    init(_ s: stat) { device = s.st_dev; inode = s.st_ino }
}
private struct AssistedFileVersion: Equatable {
    let identity: AssistedFileIdentity
    let size: off_t
    let mtimeSeconds: Int
    let mtimeNanos: Int
    let ctimeSeconds: Int
    let ctimeNanos: Int
    init(_ s: stat) {
        identity = AssistedFileIdentity(s); size = s.st_size
        mtimeSeconds = s.st_mtimespec.tv_sec; mtimeNanos = s.st_mtimespec.tv_nsec
        ctimeSeconds = s.st_ctimespec.tv_sec; ctimeNanos = s.st_ctimespec.tv_nsec
    }
}
