import Foundation
import Darwin

/// One available profile in a particular registry. `id` is the UTF-8 bytes of
/// `uid`, not Swift's canonically-equivalent String identity. Combine it with
/// the UTF-8 bytes of a node name when identifying a result: node names and
/// display names need not be unique, even within different subscriptions.
public struct ProfileOption: Identifiable, Equatable, Sendable {
    public let uid: String
    public let name: String
    public let path: String
    public let isCurrent: Bool
    public var id: Data { Data(uid.utf8) }

    public static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.name.utf8.elementsEqual(rhs.name.utf8) &&
        lhs.path.utf8.elementsEqual(rhs.path.utf8) && lhs.isCurrent == rhs.isCurrent
    }
}

/// Deliberately contains no associated data: paths, YAML, URLs, credentials,
/// parser diagnostics and underlying OS errors must never escape in errors.
public enum ProfileRegistryError: Error, Equatable, LocalizedError, Sendable {
    case unavailable
    case invalidRegistry

    public var errorDescription: String? {
        switch self {
        case .unavailable: return "The profile registry is unavailable."
        case .invalidRegistry: return "The profile registry is invalid."
        }
    }
}

/// Synchronous, read-only registry enumeration. Call it off the UI thread.
/// It never reads the profile contents or contacts a network/controller.
public enum ProfileRegistry {
    /// A convenience only: no implicit lookup is performed by `options(root:)`.
    public static var defaultRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/io.github.clash-verge-rev.clash-verge-rev", isDirectory: true)
    }

    /// Reads only `profiles.yaml` below the caller-supplied root and enumerates
    /// regular, non-symlink files in its `profiles` directory. Invalid or
    /// ambiguous eligible entries fail the whole operation, never a partial
    /// list. A missing/null `current` means no selected option; a non-null
    /// current must byte-match exactly one eligible profile. Script/merge
    /// entries and known rules/proxies/groups resources are ignored except when
    /// their IDs or filenames collide with an eligible entry. Unknown types are
    /// rejected rather than guessed.
    ///
    /// This is a validation snapshot, NOT an authorization to follow `path`
    /// later: another process can replace a file or an ancestor after return.
    /// Consumers must reopen relative to trusted directory descriptors with
    /// no-follow and revalidate their own snapshot before reading or acting.
    public static func options(root: URL) throws -> [ProfileOption] {
        // Never reinterpret a remote/hosted file URL or discard query/fragment
        // components when converting the explicitly supplied root to a path.
        guard root.isFileURL, root.host == nil || root.host == "",
              root.query == nil, root.fragment == nil else { throw ProfileRegistryError.unavailable }
        let rootPath = root.standardizedFileURL.path
        guard rootPath.utf8.first == 47, validText(rootPath) else { throw ProfileRegistryError.unavailable }
        let profilesPath = (rootPath == "/" ? "" : rootPath) + "/profiles"

        // Pin each directory while examining children. O_NOFOLLOW rejects the
        // final component if it is a symlink; O_CLOEXEC keeps descriptors out
        // of the Ruby child. The root is explicitly chosen by the caller.
        let rootFD = rootPath.withCString { Darwin.open($0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC) }
        guard rootFD >= 0 else { throw ProfileRegistryError.unavailable }
        defer { _ = Darwin.close(rootFD) }
        let profilesFD = "profiles".withCString {
            Darwin.openat(rootFD, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        }
        guard profilesFD >= 0 else { throw ProfileRegistryError.unavailable }
        defer { _ = Darwin.close(profilesFD) }
        var rootStat = stat(), profilesStat = stat()
        guard fstat(rootFD, &rootStat) == 0, isDirectory(rootStat),
              fstat(profilesFD, &profilesStat) == 0, isDirectory(profilesStat) else {
            throw ProfileRegistryError.unavailable
        }

        // Check type before open, and compare identity afterwards. O_NONBLOCK
        // avoids hanging on a substituted FIFO; O_NOFOLLOW rejects symlinks.
        var registryBefore = stat()
        guard statAt(rootFD, "profiles.yaml", &registryBefore), isRegular(registryBefore),
              registryBefore.st_size >= 0, registryBefore.st_size <= maximumYAMLBytes else {
            throw ProfileRegistryError.unavailable
        }
        let registryFD = "profiles.yaml".withCString {
            Darwin.openat(rootFD, $0, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        }
        guard registryFD >= 0 else { throw ProfileRegistryError.unavailable }
        defer { _ = Darwin.close(registryFD) }
        var openedRegistry = stat()
        guard fstat(registryFD, &openedRegistry) == 0, isRegular(openedRegistry),
              sameVersion(registryBefore, openedRegistry) else { throw ProfileRegistryError.unavailable }
        let yaml = try readBounded(registryFD)
        var registryAfter = stat()
        guard fstat(registryFD, &registryAfter) == 0, sameVersion(openedRegistry, registryAfter)
        else { throw ProfileRegistryError.unavailable }
        let document = try parse(yaml)

        if let current = document.current, !validText(current) { throw ProfileRegistryError.invalidRegistry }
        let currentID = document.current.map { Data($0.utf8) }
        var eligibleIDs = Set<Data>(), eligibleFiles = Set<Data>(), eligibleInodes = Set<FileIdentity>()
        var ignoredIDs = Set<Data>(), ignoredFiles = Set<Data>()
        var checkedFiles: [(String, FileIdentity)] = []
        var result: [ProfileOption] = []
        var currentMatches = 0

        for item in document.items {
            if ["script", "merge", "rules", "proxies", "groups"].contains(item.type) {
                // Ignore malformed auxiliary fields, but remember usable keys
                // so an eligible entry cannot alias an ignored entry.
                if let uid = item.uid, validText(uid) { ignoredIDs.insert(Data(uid.utf8)) }
                if let file = item.file, validFilename(file) { ignoredFiles.insert(Data(file.utf8)) }
                continue
            }
            guard item.type == "remote" || item.type == "local",
                  let uid = item.uid, validText(uid), let name = item.name, validText(name),
                  let file = item.file, validFilename(file) else { throw ProfileRegistryError.invalidRegistry }
            let id = Data(uid.utf8), fileBytes = Data(file.utf8)
            guard eligibleIDs.insert(id).inserted, eligibleFiles.insert(fileBytes).inserted
            else { throw ProfileRegistryError.invalidRegistry }

            var before = stat()
            guard statAt(profilesFD, file, &before), isRegular(before) else { throw ProfileRegistryError.unavailable }
            let fd = file.withCString { Darwin.openat(profilesFD, $0, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC) }
            guard fd >= 0 else { throw ProfileRegistryError.unavailable }
            var opened = stat()
            let ok = fstat(fd, &opened) == 0 && isRegular(opened) && sameIdentity(before, opened)
            _ = Darwin.close(fd)
            guard ok else { throw ProfileRegistryError.unavailable }
            let identity = FileIdentity(opened)
            // Case-insensitive/normalizing filesystems and hardlinks may map
            // byte-distinct filenames to the same file: that is ambiguous too.
            guard eligibleInodes.insert(identity).inserted else { throw ProfileRegistryError.invalidRegistry }
            checkedFiles.append((file, identity))
            let isCurrent = currentID == id
            if isCurrent { currentMatches += 1 }
            result.append(ProfileOption(uid: uid, name: name, path: profilesPath + "/" + file, isCurrent: isCurrent))
        }
        guard eligibleIDs.isDisjoint(with: ignoredIDs), eligibleFiles.isDisjoint(with: ignoredFiles),
              currentID == nil || currentMatches == 1 else { throw ProfileRegistryError.invalidRegistry }

        // Recheck that all returned paths still resolve to the pinned objects.
        // These checks reduce substitution windows but cannot eliminate a race
        // after this method returns (or replace a safe downstream reopen).
        var pathRoot = stat(), pathProfiles = stat(), pathRegistry = stat()
        guard lstat(rootPath, &pathRoot) == 0, isDirectory(pathRoot), sameIdentity(rootStat, pathRoot),
              lstat(profilesPath, &pathProfiles) == 0, isDirectory(pathProfiles), sameIdentity(profilesStat, pathProfiles),
              statAt(rootFD, "profiles.yaml", &pathRegistry), isRegular(pathRegistry),
              sameVersion(registryAfter, pathRegistry) else { throw ProfileRegistryError.unavailable }
        for (file, identity) in checkedFiles {
            var byDirectory = stat(), byPath = stat()
            guard statAt(profilesFD, file, &byDirectory), isRegular(byDirectory), FileIdentity(byDirectory) == identity,
                  lstat(profilesPath + "/" + file, &byPath) == 0, isRegular(byPath), FileIdentity(byPath) == identity
            else { throw ProfileRegistryError.unavailable }
        }
        return result
    }

    private static let maximumYAMLBytes: off_t = 4 * 1024 * 1024
    private static let maximumJSONBytes = 8 * 1024 * 1024
    private static let maximumFieldBytes = 8 * 1024

    private static func validText(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= maximumFieldBytes &&
        !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }
    private static func validFilename(_ value: String) -> Bool {
        let bytes = Array(value.utf8)
        return validText(value) && bytes != [46] && bytes != [46, 46] &&
            !bytes.contains(47) && !bytes.contains(92) // slash, backslash
    }
    private static func isRegular(_ value: stat) -> Bool { (value.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG) }
    private static func isDirectory(_ value: stat) -> Bool { (value.st_mode & mode_t(S_IFMT)) == mode_t(S_IFDIR) }
    private struct FileIdentity: Hashable {
        let device: dev_t
        let inode: ino_t
        init(_ value: stat) { device = value.st_dev; inode = value.st_ino }
    }
    private static func sameIdentity(_ a: stat, _ b: stat) -> Bool { FileIdentity(a) == FileIdentity(b) }
    private static func sameVersion(_ a: stat, _ b: stat) -> Bool {
        sameIdentity(a, b) && a.st_size == b.st_size &&
        a.st_mtimespec.tv_sec == b.st_mtimespec.tv_sec && a.st_mtimespec.tv_nsec == b.st_mtimespec.tv_nsec &&
        a.st_ctimespec.tv_sec == b.st_ctimespec.tv_sec && a.st_ctimespec.tv_nsec == b.st_ctimespec.tv_nsec
    }
    private static func statAt(_ fd: Int32, _ name: String, _ result: inout stat) -> Bool {
        name.withCString { fstatat(fd, $0, &result, AT_SYMLINK_NOFOLLOW) == 0 }
    }
    private static func readBounded(_ fd: Int32) throws -> Data {
        var result = Data(), buffer = [UInt8](repeating: 0, count: 16384)
        while true {
            let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n == 0 { return result }
            if n < 0 {
                if errno == EINTR { continue }
                throw ProfileRegistryError.unavailable
            }
            guard result.count + n <= Int(maximumYAMLBytes) else { throw ProfileRegistryError.unavailable }
            result.append(contentsOf: buffer[0..<n])
        }
    }

    private struct WireItem: Decodable { let type: String; let uid: String?; let name: String?; let file: String? }
    private struct WireRegistry: Decodable { let current: String?; let items: [WireItem] }

    // Fixed script in argv; the ONLY copy of registry bytes passed to Ruby is
    // stdin. Explicitly whitelist JSON fields, so URL/token/other metadata is
    // never emitted. Disable aliases, multiple documents and duplicate mapping
    // keys rather than letting a parser silently choose between interpretations.
    private static let rubyScript = #"""
require 'yaml'
require 'json'
begin
  text = STDIN.read.force_encoding(Encoding::UTF_8)
  raise unless text.valid_encoding?
  stream = Psych.parse_stream(text)
  raise unless stream.children.length == 1
  stack = [stream]
  until stack.empty?
    node = stack.pop
    if node.is_a?(Psych::Nodes::Mapping)
      seen = {}
      node.children.each_slice(2) do |key, _value|
        raise unless key.is_a?(Psych::Nodes::Scalar)
        bytes = key.value.b
        raise if seen.key?(bytes)
        seen[bytes] = true
      end
    end
    stack.concat(node.children) if node.respond_to?(:children) && node.children
  end
  data = YAML.safe_load(text, [], [], false)
  raise unless data.is_a?(Hash) && data['items'].is_a?(Array)
  current = data['current']
  raise unless current.nil? || current.is_a?(String)
  items = data['items'].map do |item|
    raise unless item.is_a?(Hash) && item['type'].is_a?(String)
    type = item['type']
    raise unless ['remote', 'local', 'script', 'merge', 'rules', 'proxies', 'groups'].include?(type)
    { 'type' => type,
      'uid' => (item['uid'].is_a?(String) ? item['uid'] : nil),
      'name' => (item['name'].is_a?(String) ? item['name'] : nil),
      'file' => (item['file'].is_a?(String) ? item['file'] : nil) }
  end
  STDOUT.write(JSON.generate({ 'current' => current, 'items' => items }))
rescue Exception
  exit 1
end
"""#

    private static func parse(_ yaml: Data) throws -> WireRegistry {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ruby")
        process.arguments = ["--disable-gems", "-e", rubyScript]
        // Do not let RUBYOPT/RUBYLIB/gem settings execute caller-provided code.
        process.environment = ["PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"]
        let input = Pipe(), output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1
        else { throw ProfileRegistryError.unavailable }
        do { try process.run() } catch { throw ProfileRegistryError.unavailable }
        // The parent must not retain the child's pipe ends: otherwise a Ruby
        // startup failure could keep a blocked write alive or suppress EOF.
        try? input.fileHandleForReading.close()
        try? output.fileHandleForWriting.close()
        // Bound a malformed/expensive parse as well as its input and output.
        let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 10, execute: watchdog)
        var inputOK = true
        do { try input.fileHandleForWriting.write(contentsOf: yaml) } catch { inputOK = false }
        do { try input.fileHandleForWriting.close() } catch { inputOK = false }
        var collected = Data(), outputOK = true
        do {
            while let chunk = try output.fileHandleForReading.read(upToCount: 16384), !chunk.isEmpty {
                if outputOK && collected.count + chunk.count <= maximumJSONBytes { collected.append(chunk) }
                else { outputOK = false } // Continue draining to prevent a pipe deadlock.
            }
        } catch { outputOK = false }
        try? output.fileHandleForReading.close()
        process.waitUntilExit()
        watchdog.cancel()
        guard inputOK, outputOK, process.terminationReason == .exit, process.terminationStatus == 0,
              let result = try? JSONDecoder().decode(WireRegistry.self, from: collected) else {
            throw ProfileRegistryError.invalidRegistry
        }
        return result
    }
}
