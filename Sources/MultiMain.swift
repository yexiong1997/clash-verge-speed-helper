import AppKit
import Foundation
import Darwin

// This executable is separate from main.swift. It never writes profile files;
// an explicitly requested cross-profile change goes through the runtime
// injector, while controller selection remains opt-in and guarded.

enum MultiUIError: Error {
    case invalidProfile, staleProfile, budget, unavailable
}

private let multiMaxProfileBytes = 1 * 1024 * 1024
private let multiMaxSnapshotBytes = 8 * 1024 * 1024
// MultiBatchJob makes a private copy for EVERY node, so budget the products,
// not merely the sum of unique snapshots.
private let multiMaxJobBytes = 64 * 1024 * 1024
private let multiMaxProfiles = 32
private let multiMaxNodes = 2000
private let multiMaxNameBytes = 1024

struct MultiFileVersion: Equatable {
    let device: dev_t
    let inode: ino_t
    let size: off_t
    let mtimeSeconds: Int
    let mtimeNanos: Int
    let ctimeSeconds: Int
    let ctimeNanos: Int
    init(_ s: stat) {
        device = s.st_dev; inode = s.st_ino; size = s.st_size
        mtimeSeconds = s.st_mtimespec.tv_sec; mtimeNanos = s.st_mtimespec.tv_nsec
        ctimeSeconds = s.st_ctimespec.tv_sec; ctimeNanos = s.st_ctimespec.tv_nsec
    }
}

struct MultiProfileSnapshot {
    let option: ProfileOption
    let data: Data
    let version: MultiFileVersion
    let names: [String]
}

struct MultiPreparedBatch {
    let registry: [ProfileOption]
    // Private byte-exact registry snapshot validated while preparing this batch.
    let registryData: Data
    let registryVersion: MultiFileVersion
    let profiles: [MultiProfileSnapshot]
    let options: MultiBatchOptions
    let parallel: Int
    let jobCount: Int
    let requestedBytes: Int
    // Preview snapshots are for the picker only and must never launch workers.
    let previewOnly: Bool
}

struct MultiIdentity: Hashable {
    let profile: Data
    let node: Data
    init(profile: Data, node: String) {
        self.profile = profile
        self.node = Data(node.utf8)
    }
}

private func multiRequireContinuing(_ mayContinue: () -> Bool) throws {
    guard mayContinue() else { throw MultiUIError.unavailable }
}

// Open every directory component without following links.
private func multiRootDirectory(_ root: URL, mayContinue: () -> Bool = { true }) throws -> Int32 {
    try multiRequireContinuing(mayContinue)
    guard root.isFileURL, root.host == nil || root.host == "", root.query == nil,
          root.fragment == nil else { throw MultiUIError.unavailable }
    // Keep the caller's path spelling: Foundation's standardizedFileURL can
    // rewrite an existing /private/tmp prefix to the symlink /tmp. Following
    // that rewritten prefix would defeat the component-by-component no-follow
    // check (and incorrectly reject an otherwise safe synthetic root).
    let path = root.path
    guard path.hasPrefix("/"), !path.utf8.contains(0) else { throw MultiUIError.unavailable }
    var fd = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw MultiUIError.unavailable }
    for component in path.split(separator: "/").map(String.init) {
        guard mayContinue() else { close(fd); throw MultiUIError.unavailable }
        guard component != ".", component != "..", !component.utf8.contains(0) else {
            close(fd); throw MultiUIError.unavailable
        }
        let next = component.withCString { openat(fd, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC) }
        close(fd)
        guard next >= 0 else { throw MultiUIError.unavailable }
        fd = next
    }
    guard mayContinue() else { close(fd); throw MultiUIError.unavailable }
    return fd
}

private func multiProfilesDirectory(_ root: URL, mayContinue: () -> Bool = { true }) throws -> Int32 {
    let rootFD = try multiRootDirectory(root, mayContinue: mayContinue)
    defer { close(rootFD) }
    try multiRequireContinuing(mayContinue)
    let fd = openat(rootFD, "profiles", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw MultiUIError.unavailable }
    return fd
}

private func multiIsRegular(_ s: stat) -> Bool {
    (s.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG)
}

// Recheck the exact registry bytes without spawning Ruby on the UI thread. The
// full parser validates this same version during preparation and postflight.
private func multiReadRegistrySnapshot(root: URL, mayContinue: () -> Bool = { true }) throws -> (Data, MultiFileVersion) {
    let directory = try multiRootDirectory(root, mayContinue: mayContinue)
    defer { close(directory) }
    try multiRequireContinuing(mayContinue)
    let filename = "profiles.yaml"
    let maximumBytes = 4 * 1024 * 1024
    var before = stat()
    guard fstatat(directory, filename, &before, AT_SYMLINK_NOFOLLOW) == 0,
          multiIsRegular(before), before.st_size > 0, before.st_size <= off_t(maximumBytes)
    else { throw MultiUIError.staleProfile }
    try multiRequireContinuing(mayContinue)
    let fd = openat(directory, filename, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard fd >= 0 else { throw MultiUIError.staleProfile }
    defer { close(fd) }
    var opened = stat()
    guard fstat(fd, &opened) == 0, multiIsRegular(opened),
          MultiFileVersion(before) == MultiFileVersion(opened) else { throw MultiUIError.staleProfile }
    var data = Data(), buffer = [UInt8](repeating: 0, count: 16_384)
    while true {
        try multiRequireContinuing(mayContinue)
        let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
        if n == 0 { break }
        if n < 0 {
            if errno == EINTR { continue }
            throw MultiUIError.unavailable
        }
        guard data.count + n <= maximumBytes else { throw MultiUIError.budget }
        data.append(contentsOf: buffer[0..<n])
    }
    try multiRequireContinuing(mayContinue)
    var after = stat(), pathAfter = stat()
    guard !data.isEmpty, fstat(fd, &after) == 0, multiIsRegular(after),
          fstatat(directory, filename, &pathAfter, AT_SYMLINK_NOFOLLOW) == 0,
          multiIsRegular(pathAfter), MultiFileVersion(opened) == MultiFileVersion(after),
          MultiFileVersion(after) == MultiFileVersion(pathAfter), data.count == Int(after.st_size)
    else { throw MultiUIError.staleProfile }
    return (data, MultiFileVersion(after))
}

private func multiReadSnapshot(root: URL, option: ProfileOption, mayContinue: () -> Bool = { true }) throws -> (Data, MultiFileVersion) {
    let dir = try multiProfilesDirectory(root, mayContinue: mayContinue)
    defer { close(dir) }
    try multiRequireContinuing(mayContinue)
    let filename = (option.path as NSString).lastPathComponent
    let expectedPath = root.standardizedFileURL.appendingPathComponent("profiles", isDirectory: true)
        .appendingPathComponent(filename).path
    guard !filename.isEmpty, filename != ".", filename != "..",
          !filename.utf8.contains(47), !filename.utf8.contains(92), !filename.utf8.contains(0),
          expectedPath.utf8.elementsEqual(option.path.utf8) else { throw MultiUIError.staleProfile }
    var before = stat()
    guard filename.withCString({ fstatat(dir, $0, &before, AT_SYMLINK_NOFOLLOW) }) == 0,
          multiIsRegular(before), before.st_size > 0, before.st_size <= off_t(multiMaxProfileBytes)
    else { throw MultiUIError.invalidProfile }
    try multiRequireContinuing(mayContinue)
    let fd = filename.withCString { openat(dir, $0, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC) }
    guard fd >= 0 else { throw MultiUIError.staleProfile }
    defer { close(fd) }
    var opened = stat()
    guard fstat(fd, &opened) == 0, multiIsRegular(opened),
          MultiFileVersion(before) == MultiFileVersion(opened) else { throw MultiUIError.staleProfile }
    var data = Data(), buffer = [UInt8](repeating: 0, count: 16_384)
    while true {
        try multiRequireContinuing(mayContinue)
        let n = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
        if n == 0 { break }
        if n < 0 {
            if errno == EINTR { continue }
            throw MultiUIError.unavailable
        }
        guard data.count + n <= multiMaxProfileBytes else { throw MultiUIError.budget }
        data.append(contentsOf: buffer[0..<n])
    }
    try multiRequireContinuing(mayContinue)
    var after = stat(), pathAfter = stat()
    guard !data.isEmpty, fstat(fd, &after) == 0, multiIsRegular(after),
          filename.withCString({ fstatat(dir, $0, &pathAfter, AT_SYMLINK_NOFOLLOW) }) == 0,
          multiIsRegular(pathAfter), MultiFileVersion(opened) == MultiFileVersion(after),
          MultiFileVersion(after) == MultiFileVersion(pathAfter), data.count == Int(after.st_size)
    else { throw MultiUIError.staleProfile }
    return (data, MultiFileVersion(after))
}

// Tolerate an explicit current-profile change, but reject replacement of the
// target profile file even when its bytes are identical.
func multiProfileSnapshotMatches(root: URL, profile: MultiProfileSnapshot) -> Bool {
    guard let (data, version) = try? multiReadSnapshot(root: root, option: profile.option) else { return false }
    return version == profile.version && data == profile.data
}

// Constant code is the only Ruby argv data. Config bytes are stdin only;
// stderr is discarded and stdout contains only validated node names. Aliases,
// duplicate mapping keys and providers are rejected rather than expanded.
private let multiProfileInspector = #"""
require 'yaml'
require 'json'
begin
  text = STDIN.read.force_encoding(Encoding::UTF_8)
  raise unless text.valid_encoding? && !text.empty?
  stream = Psych.parse_stream(text)
  raise unless stream.children.length == 1
  stack = [stream]
  until stack.empty?
    node = stack.pop
    raise if node.is_a?(Psych::Nodes::Alias)
    # Explicit tags could turn distinct source spellings into an identical key
    # after loading (for example, binary-encoded strings). Fail closed.
    raise if node.respond_to?(:tag) && node.tag
    if node.is_a?(Psych::Nodes::Mapping)
      seen = {}
      node.children.each_slice(2) do |key, value|
        raise unless value && key.is_a?(Psych::Nodes::Scalar)
        raw = key.value.b
        raise if seen.key?(raw)
        seen[raw] = true
      end
    end
    stack.concat(node.children) if node.respond_to?(:children) && node.children
  end
  data = YAML.safe_load(text, [], [], false)
  raise unless data.is_a?(Hash)
  # Implicit non-string keys can also collapse on load (e.g. 1 and 01).
  # Profile mappings are required to use unambiguous string keys throughout.
  stack = [data]
  until stack.empty?
    value = stack.pop
    if value.is_a?(Hash)
      raise unless value.keys.all? { |key| key.is_a?(String) }
      stack.concat(value.values)
    elsif value.is_a?(Array)
      stack.concat(value)
    end
  end
  raise if data.key?('proxy-providers') || data.key?('rule-providers')
  proxies = data['proxies']
  raise unless proxies.is_a?(Array) && proxies.length.between?(1, 2000)
  seen = {}
  names = proxies.map do |proxy|
    raise unless proxy.is_a?(Hash) && proxy['name'].is_a?(String)
    name = proxy['name']
    raise unless name.valid_encoding? && name.bytesize.between?(1, 1024)
    raise if name.match?(/[[:cntrl:]]/) || seen.key?(name.b)
    seen[name.b] = true
    name
  end
  STDOUT.write(JSON.generate({'names' => names}))
rescue Exception
  exit 1
end
"""#

private struct MultiNodeList: Decodable { let names: [String] }

func multiInspectNodes(_ data: Data) throws -> [String] {
    guard !data.isEmpty, data.count <= multiMaxProfileBytes else { throw MultiUIError.budget }
    let process = Process(), input = Pipe(), output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/ruby")
    process.arguments = ["--disable-gems", "-e", multiProfileInspector]
    process.environment = ["PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"]
    process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
    guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1
    else { throw MultiUIError.unavailable }
    do { try process.run() } catch { throw MultiUIError.unavailable }
    try? input.fileHandleForReading.close(); try? output.fileHandleForWriting.close()
    let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 10, execute: watchdog)
    var inputOK = true, outputOK = true, collected = Data()
    do { try input.fileHandleForWriting.write(contentsOf: data) } catch { inputOK = false }
    do { try input.fileHandleForWriting.close() } catch { inputOK = false }
    do {
        while let chunk = try output.fileHandleForReading.read(upToCount: 16_384), !chunk.isEmpty {
            if outputOK && collected.count + chunk.count <= 4 * 1024 * 1024 { collected.append(chunk) }
            else { outputOK = false } // Drain even on overflow; never retain raw output.
        }
    } catch { outputOK = false }
    try? output.fileHandleForReading.close()
    process.waitUntilExit(); watchdog.cancel()
    guard inputOK, outputOK, process.terminationReason == .exit, process.terminationStatus == 0,
          let parsed = try? JSONDecoder().decode(MultiNodeList.self, from: collected),
          !parsed.names.isEmpty, parsed.names.count <= multiMaxNodes else { throw MultiUIError.invalidProfile }
    var seen = Set<Data>()
    for name in parsed.names {
        guard !name.isEmpty, name.utf8.count <= multiMaxNameBytes,
              name.rangeOfCharacter(from: .controlCharacters) == nil,
              name.rangeOfCharacter(from: .newlines) == nil,
              seen.insert(Data(name.utf8)).inserted else { throw MultiUIError.invalidProfile }
    }
    return parsed.names
}

// Called only on a background queue. A refresh is a registry snapshot, not an
// authorization to follow the saved path; selected entries and content are
// checked before and after inspection and again after consent, before execution.
func multiPrepare(root: URL, registry: [ProfileOption], selected: Set<Data>,
                  options: MultiBatchOptions, parallel: Int,
                  nodeSelection: Set<MultiIdentity>? = nil, previewOnly: Bool = false) throws -> MultiPreparedBatch {
    let (registryData, registryVersion) = try multiReadRegistrySnapshot(root: root)
    guard !selected.isEmpty, selected.count <= multiMaxProfiles, (1...10).contains(parallel),
          options.downloadBytes > 0, options.streams == 2 || options.streams == 4,
          nodeSelection.map({ !$0.isEmpty }) ?? true,
          try ProfileRegistry.options(root: root) == registry else { throw MultiUIError.staleProfile }
    let chosen = registry.filter { selected.contains($0.id) }
    guard chosen.count == selected.count else { throw MultiUIError.staleProfile }
    var snapshots: [MultiProfileSnapshot] = [], uniqueBytes = 0, copiedBytes = 0, count = 0, totalNodes = 0
    var matched = Set<MultiIdentity>()
    for option in chosen {
        let (data, version) = try multiReadSnapshot(root: root, option: option)
        uniqueBytes += data.count
        guard uniqueBytes <= multiMaxSnapshotBytes else { throw MultiUIError.budget }
        let allNames = try multiInspectNodes(data)
        totalNodes += allNames.count
        guard totalNodes <= multiMaxNodes else { throw MultiUIError.budget }
        let names = allNames.filter { name in
            let identity = MultiIdentity(profile: option.id, node: name)
            guard nodeSelection == nil || nodeSelection!.contains(identity) else { return false }
            matched.insert(identity)
            return true
        }
        count += names.count
        // A preview does not create jobs or per-node YAML copies. Actual runs
        // retain the original bound, calculated only for selected nodes.
        if !previewOnly && !names.isEmpty {
            guard data.count <= (multiMaxJobBytes - copiedBytes) / names.count else { throw MultiUIError.budget }
            copiedBytes += data.count * names.count
        }
        snapshots.append(MultiProfileSnapshot(option: option, data: data, version: version, names: names))
    }
    guard count > 0, nodeSelection == nil || matched == nodeSelection!,
          try ProfileRegistry.options(root: root) == registry,
          options.downloadBytes <= Int.max / count else { throw MultiUIError.staleProfile }
    let prepared = MultiPreparedBatch(registry: registry, registryData: registryData,
                                      registryVersion: registryVersion, profiles: snapshots,
                                      options: options, parallel: parallel, jobCount: count,
                                      requestedBytes: options.downloadBytes * count, previewOnly: previewOnly)
    try multiRevalidate(root: root, batch: prepared)
    return prepared
}

// This bounded, byte-exact check avoids a synchronous Ruby subprocess for
// every queued job. It still cannot be atomic with another process or spawn.
func multiQuickRevalidate(root: URL, batch: MultiPreparedBatch, mayContinue: () -> Bool = { true }) throws {
    let (before, beforeVersion) = try multiReadRegistrySnapshot(root: root, mayContinue: mayContinue)
    guard beforeVersion == batch.registryVersion, before == batch.registryData
    else { throw MultiUIError.staleProfile }
    for profile in batch.profiles {
        try multiRequireContinuing(mayContinue)
        let (data, version) = try multiReadSnapshot(root: root, option: profile.option, mayContinue: mayContinue)
        guard version == profile.version, data == profile.data else { throw MultiUIError.staleProfile }
    }
    try multiRequireContinuing(mayContinue)
    let (after, afterVersion) = try multiReadRegistrySnapshot(root: root, mayContinue: mayContinue)
    guard afterVersion == batch.registryVersion, after == batch.registryData
    else { throw MultiUIError.staleProfile }
    try multiRequireContinuing(mayContinue)
}

func multiRevalidate(root: URL, batch: MultiPreparedBatch) throws {
    guard try ProfileRegistry.options(root: root) == batch.registry else { throw MultiUIError.staleProfile }
    try multiQuickRevalidate(root: root, batch: batch)
    guard try ProfileRegistry.options(root: root) == batch.registry else { throw MultiUIError.staleProfile }
}

private func multiSameSnapshots(_ a: MultiPreparedBatch, _ b: MultiPreparedBatch) -> Bool {
    guard a.registry == b.registry, a.registryData == b.registryData,
          a.registryVersion == b.registryVersion, a.profiles.count == b.profiles.count else { return false }
    return zip(a.profiles, b.profiles).allSatisfy { left, right in
        left.option == right.option && left.version == right.version && left.data == right.data
    }
}

private func multiBatchIncludesCurrent(_ batch: MultiPreparedBatch) -> Bool {
    guard let active = batch.registry.first(where: { $0.isCurrent }) else { return false }
    return batch.profiles.contains(where: { $0.option.id == active.id && !$0.names.isEmpty })
}

func multiJobs(_ batch: MultiPreparedBatch) -> [MultiBatchJob] {
    guard !batch.previewOnly else { return [] }
    var jobs: [MultiBatchJob] = []
    jobs.reserveCapacity(batch.jobCount)
    for profile in batch.profiles {
        for name in profile.names {
            jobs.append(MultiBatchJob(profileIdentity: profile.option.id, nodeName: name,
                                      yamlSnapshot: profile.data, index: jobs.count, options: batch.options))
        }
    }
    return jobs
}

struct MultiRow {
    let identity: MultiIdentity
    let profileName: String
    let nodeName: String
    let index: Int
    var measurement: VerifiedMeasurement?
    var state: String
    var eligibleScore: Double? {
        guard multiAutomaticNodeIsCandidate(nodeName),
              let score = measurement?.score, score.isFinite else { return nil }
        return score
    }
}

// Pasted TSV can be interpreted as spreadsheet formulas. Prefix dangerous
// cells and prevent cell text from injecting column or row delimiters.
func multiTSVSafeCell(_ value: String) -> String {
    let cell = value.replacingOccurrences(of: "\t", with: " ")
        .replacingOccurrences(of: "\r", with: " ").replacingOccurrences(of: "\n", with: " ")
    let prefix = cell.trimmingCharacters(in: .whitespacesAndNewlines.union(.controlCharacters))
    guard let first = prefix.first, "=+-@".contains(first) else { return cell }
    return "'" + cell
}

func multiRank(_ rows: [MultiRow]) -> [MultiRow] {
    rows.sorted { a, b in
        switch (a.eligibleScore, b.eligibleScore) {
        case let (x?, y?): return x == y ? a.index < b.index : x > y
        case (_?, nil): return true
        case (nil, _?): return false
        case (nil, nil): return a.index < b.index
        }
    }
}

// The visible catalogue and test jobs are separate: unchecked nodes remain
// available for manual selection, but never retain a previous run's score.
func multiPreviewRows(_ batch: MultiPreparedBatch) -> [MultiRow] {
    var rows: [MultiRow] = []
    for profile in batch.profiles {
        for name in profile.names {
            rows.append(MultiRow(identity: MultiIdentity(profile: profile.option.id, node: name),
                                 profileName: "\(profile.option.name) [\(profile.option.uid)]",
                                 nodeName: name, index: rows.count, measurement: nil, state: "待测速"))
        }
    }
    return rows
}

func multiRunRows(_ rows: [MultiRow], jobs: [MultiBatchJob]) -> [MultiRow]? {
    let identities = jobs.map { MultiIdentity(profile: $0.profileIdentity, node: $0.nodeName) }
    let catalogue = Set(rows.map(\.identity)), selected = Set(identities)
    guard catalogue.count == rows.count, selected.count == identities.count,
          !selected.isEmpty, selected.isSubset(of: catalogue) else { return nil }
    return rows.map { original in
        var row = original
        row.measurement = nil
        row.state = selected.contains(row.identity) ? "等待" : "未勾选"
        return row
    }
}

enum MultiRowStatusFilter: Int, Equatable {
    case all = 0
    case validOnly = 1
    case testingOnly = 2
    case failedOnly = 3
}

func multiVisibleRows(_ rows: [MultiRow], query: String,
                      statusFilter: MultiRowStatusFilter = .all,
                      activeIdentities: Set<MultiIdentity> = [],
                      sortColumn: String? = nil,
                      sortAscending: Bool = false) -> [Int] {
    let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
    var indices = rows.indices.filter { idx in
        let item = rows[idx]
        let matchesQuery = term.isEmpty ||
            item.nodeName.localizedCaseInsensitiveContains(term) ||
            item.profileName.localizedCaseInsensitiveContains(term) ||
            (item.measurement?.nodeType.localizedCaseInsensitiveContains(term) ?? false)
        guard matchesQuery else { return false }
        switch statusFilter {
        case .all:
            return true
        case .validOnly:
            return item.eligibleScore != nil
        case .testingOnly:
            return activeIdentities.contains(item.identity) || item.state == "等待"
        case .failedOnly:
            return item.state == "无效" || item.state == "已取消" || (item.measurement != nil && item.eligibleScore == nil)
        }
    }
    if let col = sortColumn {
        indices.sort { lhs, rhs in
            let a = rows[lhs], b = rows[rhs]
            let cmp: Bool?
            switch col {
            case "speed":
                switch (a.measurement?.downloadBytesPerSecond, b.measurement?.downloadBytesPerSecond) {
                case let (x?, y?) where x != y: cmp = sortAscending ? (x < y) : (x > y)
                case (_?, nil): cmp = true
                case (nil, _?): cmp = false
                default: cmp = nil
                }
            case "latency":
                switch (a.measurement?.latencyMilliseconds, b.measurement?.latencyMilliseconds) {
                case let (x?, y?) where x != y: cmp = sortAscending ? (x < y) : (x > y)
                case (_?, nil): cmp = true
                case (nil, _?): cmp = false
                default: cmp = nil
                }
            case "failure":
                let fa = a.measurement.map { $0.headAttempts > 0 ? Double($0.headAttempts - $0.headSuccesses) / Double($0.headAttempts) : 1.0 }
                let fb = b.measurement.map { $0.headAttempts > 0 ? Double($0.headAttempts - $0.headSuccesses) / Double($0.headAttempts) : 1.0 }
                switch (fa, fb) {
                case let (x?, y?) where x != y: cmp = sortAscending ? (x < y) : (x > y)
                case (_?, nil): cmp = true
                case (nil, _?): cmp = false
                default: cmp = nil
                }
            case "jitter":
                switch (a.measurement?.jitterMilliseconds, b.measurement?.jitterMilliseconds) {
                case let (x?, y?) where x != y: cmp = sortAscending ? (x < y) : (x > y)
                case (_?, nil): cmp = true
                case (nil, _?): cmp = false
                default: cmp = nil
                }
            case "score":
                switch (a.eligibleScore, b.eligibleScore) {
                case let (x?, y?) where x != y: cmp = sortAscending ? (x < y) : (x > y)
                case (_?, nil): cmp = true
                case (nil, _?): cmp = false
                default: cmp = nil
                }
            case "rank":
                if a.index != b.index { cmp = sortAscending ? (a.index < b.index) : (a.index > b.index) }
                else { cmp = nil }
            default:
                cmp = nil
            }
            return cmp ?? (a.index < b.index)
        }
    }
    return indices
}

func multiFunnelPromotedIdentities(rows: [MultiRow], runIdentities: Set<MultiIdentity>, topCount: Int) -> Set<MultiIdentity> {
    let limit = max(1, min(50, topCount))
    let ranked = multiRank(rows).filter { runIdentities.contains($0.identity) && $0.eligibleScore != nil }
    return Set(ranked.prefix(limit).map(\.identity))
}

// Require exactly one output string; the strict decoder itself rejects embedded
// newlines while permitting a single LF or CRLF line terminator. Never display
// a parser error or raw worker output.
func multiDecode(_ result: MultiBatchResult, job: MultiBatchJob) -> VerifiedMeasurement? {
    guard case .success(let lines) = result, lines.count == 1,
          lines[0].utf8.count <= 1_048_576,
          let measurement = VerifiedMeasurement.decode(Data(lines[0].utf8), expectedName: job.nodeName,
                  requestedBytes: job.options.downloadBytes, streams: job.options.streams),
          measurement.nodeName.utf8.elementsEqual(job.nodeName.utf8),
          !measurement.nodeType.isEmpty, measurement.nodeType.utf8.count <= 256,
          measurement.nodeType.rangeOfCharacter(from: .controlCharacters) == nil,
          measurement.headAttempts > 0, measurement.headSuccesses >= 0,
          measurement.headSuccesses <= measurement.headAttempts,
          measurement.downloadBytesPerSecond.isFinite, measurement.downloadBytesPerSecond >= 0,
          measurement.latencyMilliseconds.isFinite, measurement.latencyMilliseconds >= 0,
          measurement.jitterMilliseconds.isFinite, measurement.jitterMilliseconds >= 0,
          measurement.score.map({ $0.isFinite && (0...100).contains($0) }) ?? true
    else { return nil }
    return measurement
}

// String equality in Swift is canonically equivalent; all controller and profile
// identities here deliberately compare the actual UTF-8 bytes instead.
func multiBytesEqual(_ a: String, _ b: String) -> Bool { a.utf8.elementsEqual(b.utf8) }

struct MultiControllerIdentity: Equatable {
    let path: String
    let device: dev_t
    let inode: ino_t
    static func == (a: Self, b: Self) -> Bool {
        multiBytesEqual(a.path, b.path) && a.device == b.device && a.inode == b.inode
    }
}

// Only unique, same-effective-user sockets are candidates; aliases for the same
// device/inode count once. The controller itself repeats the identity check.
func multiUniqueController(paths: [String]) -> MultiControllerIdentity? {
    var candidates: [MultiControllerIdentity] = []
    for path in paths {
        let bytes = Array(path.utf8), parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard bytes.first == 47, bytes.count > 1, bytes.count < 104,
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              parts.first == "", parts.dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else { continue }
        var info = stat()
        guard lstat(path, &info) == 0, (info.st_mode & mode_t(S_IFMT)) == mode_t(S_IFSOCK),
              info.st_uid == geteuid() else { continue }
        let item = MultiControllerIdentity(path: path, device: info.st_dev, inode: info.st_ino)
        if !candidates.contains(where: { $0.device == item.device && $0.inode == item.inode }) { candidates.append(item) }
    }
    return candidates.count == 1 ? candidates[0] : nil
}

// A socket file can outlive the core that created it. Probe the local
// controller itself before selecting an identity so a stale generated config
// socket cannot make the live Verge service socket look ambiguous. This is a
// bounded, read-only request and stops after the HTTP headers are complete.
private func multiControllerResponds(path: String) -> Bool {
    let bytes = Array(path.utf8)
    var address = sockaddr_un()
    let pathCapacity = MemoryLayout.size(ofValue: address.sun_path)
    guard !bytes.isEmpty, bytes.count + 1 < pathCapacity else { return false }
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    address.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &address.sun_path) { raw in
        raw.initializeMemory(as: UInt8.self, repeating: 0)
        raw.copyBytes(from: bytes)
    }

    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var timeout = timeval(tv_sec: 1, tv_usec: 0)
    guard setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout,
                     socklen_t(MemoryLayout<timeval>.size)) == 0,
          setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                     socklen_t(MemoryLayout<timeval>.size)) == 0 else { return false }
    var noSignal: Int32 = 1
    _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal,
                   socklen_t(MemoryLayout<Int32>.size))
    let connected = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard connected == 0 else { return false }

    let request = Array("GET /proxies HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".utf8)
    var sent = 0
    while sent < request.count {
        let count = request.withUnsafeBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return Darwin.send(fd, base.advanced(by: sent), request.count - sent, 0)
        }
        if count > 0 { sent += count; continue }
        if count < 0 && errno == EINTR { continue }
        return false
    }

    var response = Data()
    var buffer = [UInt8](repeating: 0, count: 16 * 1024)
    let separator = Data([13, 10, 13, 10])
    while response.count <= 256 * 1024 {
        let count = buffer.withUnsafeMutableBytes {
            Darwin.recv(fd, $0.baseAddress, $0.count, 0)
        }
        if count > 0 {
            response.append(contentsOf: buffer[0..<count])
            guard let end = response.range(of: separator) else { continue }
            let header = response[..<end.lowerBound]
            guard let text = String(data: header, encoding: .utf8),
                  let first = text.split(separator: "\r\n", omittingEmptySubsequences: false).first
            else { return false }
            let fields = first.split(separator: " ", omittingEmptySubsequences: true)
            return fields.count >= 2 && fields[1] == "200"
        }
        if count < 0 && errno == EINTR { continue }
        return false
    }
    return false
}

// Return the generated Unix socket only. The config is opened no-follow and
// bounded; Ruby sees the bytes on stdin, emits no other config, and has a
// watchdog. An unreadable/ambiguous config fails closed, rather than hiding a
// second controller candidate. Missing config permits the Verge service socket.
private func multiConfiguredSocket(root: URL) throws -> String? {
    let fd = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw MultiUIError.unavailable }
    defer { close(fd) }
    var before = stat()
    if fstatat(fd, "clash-verge.yaml", &before, AT_SYMLINK_NOFOLLOW) != 0 {
        if errno == ENOENT { return nil }
        throw MultiUIError.unavailable
    }
    guard multiIsRegular(before), before.st_size >= 0, before.st_size <= 4 * 1024 * 1024 else { throw MultiUIError.unavailable }
    let inputFD = openat(fd, "clash-verge.yaml", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard inputFD >= 0 else { throw MultiUIError.unavailable }
    defer { close(inputFD) }
    var opened = stat(), after = stat(), named = stat()
    guard fstat(inputFD, &opened) == 0, MultiFileVersion(before) == MultiFileVersion(opened) else { throw MultiUIError.unavailable }
    var data = Data(), buffer = [UInt8](repeating: 0, count: 16384)
    while true {
        let n = buffer.withUnsafeMutableBytes { Darwin.read(inputFD, $0.baseAddress, $0.count) }
        if n == 0 { break }
        if n < 0 { if errno == EINTR { continue }; throw MultiUIError.unavailable }
        guard data.count + n <= 4 * 1024 * 1024 else { throw MultiUIError.unavailable }
        data.append(contentsOf: buffer[0..<n])
    }
    guard fstat(inputFD, &after) == 0, fstatat(fd, "clash-verge.yaml", &named, AT_SYMLINK_NOFOLLOW) == 0,
          multiIsRegular(after), multiIsRegular(named), MultiFileVersion(opened) == MultiFileVersion(after),
          MultiFileVersion(after) == MultiFileVersion(named), data.count == Int(after.st_size) else { throw MultiUIError.unavailable }
    let script = #"""
require 'yaml'
require 'json'
begin
  stream = Psych.parse_stream(STDIN.read)
  raise unless stream.children.length == 1
  nodes = [stream]
  until nodes.empty?
    n = nodes.pop
    if n.is_a?(Psych::Nodes::Mapping)
      keys = {}
      n.children.each_slice(2) do |k,v|
        raise unless v && k.is_a?(Psych::Nodes::Scalar) && !keys[k.value.b]
        keys[k.value.b] = true
      end
    end
    nodes.concat(n.children) if n.respond_to?(:children) && n.children
  end
  value = YAML.safe_load(stream.to_yaml, [Symbol], [], false)
  raise unless value.is_a?(Hash)
  socket = value['external-controller-unix']
  raise unless socket.nil? || socket.is_a?(String)
  raise if socket && socket.bytesize > 8192
  STDOUT.write(JSON.generate({'socket'=>socket}))
rescue Exception
  exit 1
end
"""#
    let process = Process(), input = Pipe(), output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/ruby")
    process.arguments = ["--disable-gems", "-e", script]
    process.environment = ["PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"]
    process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
    guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else { throw MultiUIError.unavailable }
    do { try process.run() } catch { throw MultiUIError.unavailable }
    try? input.fileHandleForReading.close(); try? output.fileHandleForWriting.close()
    let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 10, execute: watchdog)
    var good = true, outputData = Data()
    do { try input.fileHandleForWriting.write(contentsOf: data); try input.fileHandleForWriting.close() }
    catch { good = false; try? input.fileHandleForWriting.close() }
    do {
        while let chunk = try output.fileHandleForReading.read(upToCount: 16384), !chunk.isEmpty {
            if outputData.count + chunk.count <= 32768 { outputData.append(chunk) } else { good = false }
        }
    } catch { good = false }
    try? output.fileHandleForReading.close(); process.waitUntilExit(); watchdog.cancel()
    guard good, process.terminationReason == .exit, process.terminationStatus == 0,
          let object = try? JSONSerialization.jsonObject(with: outputData) as? [String: Any] else { throw MultiUIError.unavailable }
    return object["socket"] as? String
}

func multiLocalController(root: URL) -> MultiControllerIdentity? {
    do {
        var paths = ["/var/run/clash-verge-service/users/\(geteuid())/verge-mihomo.sock"]
        if let configured = try multiConfiguredSocket(root: root) { paths.append(configured) }
        // The service path is deliberately first. A stale config socket may
        // still pass lstat(), so only a live read-only controller response can
        // make a path eligible; if both respond, keep the service identity.
        for path in paths {
            guard let identity = multiUniqueController(paths: [path]),
                  multiControllerResponds(path: identity.path) else { continue }
            return identity
        }
        return nil
    } catch { return nil }
}

// Clash Verge may create its service socket shortly after the window appears.
// A single discovery attempt turns that normal startup race into a persistent
// error in the footer. Keep the bounded retry off the main thread and return
// the first live, same-user controller identity; callers still fail closed
// when the controller never becomes available.
func multiWaitForLocalController(root: URL, attempts: Int = 12,
                                 delay: TimeInterval = 0.25) -> MultiControllerIdentity? {
    let boundedAttempts = max(1, min(attempts, 40))
    let boundedDelay = max(0, min(delay, 2))
    for index in 0..<boundedAttempts {
        if let identity = multiLocalController(root: root) { return identity }
        if index + 1 < boundedAttempts, boundedDelay > 0 {
            Thread.sleep(forTimeInterval: boundedDelay)
        }
    }
    return nil
}

// These helpers are pure so canonical-equivalent but byte-distinct names and
// all pre-test capture requirements can be exercised without a controller.
func multiGroupCandidates(_ groups: [ProxyGroup], testedNames: [String]) -> [ProxyGroup] {
    let names = Set(testedNames.map { Data($0.utf8) })
    guard !names.isEmpty else { return [] }
    var counts: [Data: Int] = [:]
    for group in groups { counts[Data(group.name.utf8), default: 0] += 1 }
    return groups.filter { group in
        let key = Data(group.name.utf8)
        return !group.name.isEmpty && !group.now.isEmpty && group.now.utf8.count <= 8192 &&
            !group.now.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) &&
            counts[key] == 1 && group.all.contains(where: { names.contains(Data($0.utf8)) })
    }
}

func multiSuggestedGroup(_ groups: [ProxyGroup], testedNames: [String]) -> String? {
    let names = Set(testedNames.map { Data($0.utf8) })
    guard !names.isEmpty else { return nil }
    let full = multiGroupCandidates(groups, testedNames: testedNames).filter { group in
        !multiBytesEqual(group.name, "GLOBAL") && names.isSubset(of: Set(group.all.map { Data($0.utf8) }))
    }
    return full.count == 1 ? full[0].name : nil
}

struct MultiSwitchCapture {
    let controller: MultiControllerIdentity
    let profile: MultiProfileSnapshot
    let groupName: String
    let now: String
}

enum MultiGroupRefreshChoice: Equatable {
    case select(Int)
    case noSelection
    case invalidateRecurringAuthorization
}

// A background refresh may update a group's `now`, but an armed recurring
// authorization must remain bound to the same controller, profile and group.
func multiGroupRefreshChoice(previous: MultiSwitchCapture?, recurring: MultiSwitchCapture?,
                             controller: MultiControllerIdentity, profile: MultiProfileSnapshot,
                             candidates: [ProxyGroup]) -> MultiGroupRefreshChoice {
    func index(_ capture: MultiSwitchCapture) -> Int? {
        guard capture.controller == controller, capture.profile.option.id == profile.option.id,
              multiBytesEqual(capture.profile.option.path, profile.option.path),
              capture.profile.data == profile.data, capture.profile.version == profile.version else { return nil }
        let matches = candidates.indices.filter { multiBytesEqual(candidates[$0].name, capture.groupName) }
        return matches.count == 1 ? matches[0] : nil
    }
    if let recurring = recurring {
        guard let match = index(recurring) else { return .invalidateRecurringAuthorization }
        return .select(match)
    }
    if let previous = previous, let match = index(previous) { return .select(match) }
    if candidates.count == 1, !multiBytesEqual(candidates[0].name, "GLOBAL") { return .select(0) }
    return .noSelection
}

func multiCaptureMatches(_ capture: MultiSwitchCapture, controller: MultiControllerIdentity,
                         profile: MultiProfileSnapshot, group: ProxyGroup, winner: String) -> Bool {
    capture.controller == controller && capture.profile.option.id == profile.option.id &&
        multiBytesEqual(capture.profile.option.path, profile.option.path) && capture.profile.data == profile.data &&
        capture.profile.version == profile.version && multiBytesEqual(capture.groupName, group.name) && multiBytesEqual(capture.now, group.now) &&
        group.all.contains(where: { multiBytesEqual($0, winner) })
}

func multiRunPermitted(closed: Bool, cancelled: Bool, tokenMatches: Bool, scheduled: Bool, armed: Bool) -> Bool {
    !closed && !cancelled && tokenMatches && (!scheduled || armed)
}

// Shared by the main-thread UI and the controller's background pre-PUT guard.
// Revocation after the final guard or after send cannot recall a PUT.
final class MultiSwitchAuthorization: @unchecked Sendable {
    private let lock = NSLock()
    private var active = true
    func revoke() { lock.lock(); active = false; lock.unlock() }
    func permits() -> Bool { lock.lock(); defer { lock.unlock() }; return active }
}

// Snapshot I/O must never block the UI thread. A single serial worker also
// bounds stuck filesystem reads across repeated stop/restart attempts. A timeout
// resolves the logical request, but cannot interrupt a read already in the OS.
enum MultiValidationOutcome: Equatable { case passed, failed, timedOut, cancelled }

final class MultiSerialValidationQueue: @unchecked Sendable {
    private final class Request: @unchecked Sendable {
        let timeout: TimeInterval
        let work: @Sendable () -> Bool
        let completion: @Sendable (MultiValidationOutcome) -> Void
        var completed = false // Main-thread only.
        init(timeout: TimeInterval, work: @escaping @Sendable () -> Bool,
             completion: @escaping @Sendable (MultiValidationOutcome) -> Void) {
            self.timeout = max(0, timeout); self.work = work; self.completion = completion
        }
    }
    private let worker = DispatchQueue(label: "local.clashverge.snapshot-validation", qos: .utility)
    private var waiting: [Request] = [] // Main-thread only.
    private var active: Request?       // Retained until the physical read returns.

    func submit(timeout: TimeInterval, work: @escaping @Sendable () -> Bool,
                completion: @escaping @Sendable (MultiValidationOutcome) -> Void) {
        precondition(Thread.isMainThread)
        let request = Request(timeout: timeout, work: work, completion: completion)
        waiting.append(request)
        // If an earlier cancelled/timed-out call remains physically blocked,
        // bound this new request's wait without starting another OS read.
        if let current = active, current.completed {
            DispatchQueue.main.asyncAfter(deadline: .now() + request.timeout) { [weak self, weak request] in
                guard let self = self, let request = request,
                      self.waiting.contains(where: { $0 === request }) else { return }
                self.complete(request, .timedOut)
            }
        }
        pump()
    }

    func cancelAll() {
        precondition(Thread.isMainThread)
        let requests = (active.map { [$0] } ?? []) + waiting
        waiting.removeAll()
        for request in requests { complete(request, .cancelled) }
    }

    private func complete(_ request: Request, _ outcome: MultiValidationOutcome) {
        guard !request.completed else { return }
        request.completed = true
        waiting.removeAll { $0 === request }
        request.completion(outcome)
    }

    private func expire(_ request: Request) {
        guard !request.completed else { return }
        // No queued validation may bypass a stalled/overdue physical read.
        let pending = waiting
        waiting.removeAll()
        complete(request, .timedOut)
        for item in pending { complete(item, .timedOut) }
    }

    private func pump() {
        guard active == nil, !waiting.isEmpty else { return }
        let request = waiting.removeFirst()
        active = request
        // Healthy queued work gets its own full timeout only when it starts.
        // Compare on main-thread delivery too: a delayed timer must not allow
        // a late success through just because its callback happened to win.
        let deadline = DispatchTime.now() + request.timeout
        DispatchQueue.main.asyncAfter(deadline: deadline) { [weak self, weak request] in
            guard let self = self, let request = request, self.active === request else { return }
            self.expire(request)
        }
        worker.async { [weak self] in
            let passed = request.work()
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                if self.active === request { self.active = nil }
                if DispatchTime.now() >= deadline { self.expire(request) }
                else { self.complete(request, passed ? .passed : .failed) }
                self.pump()
            }
        }
    }
}

private struct MultiPendingWinner {
    let profile: MultiProfileSnapshot
    let node: String
    let measurement: VerifiedMeasurement
}

private struct MultiPickerChoice {
    let profile: MultiProfileSnapshot
    let node: String
    var identity: MultiIdentity { MultiIdentity(profile: profile.option.id, node: node) }
}

func multiSelectTopN(rows: [MultiRow], visibleRows: [Int], count: Int) -> Set<MultiIdentity>? {
    guard count >= 1, count <= 2_000, !visibleRows.isEmpty else { return nil }
    var picked = Set<MultiIdentity>()
    for rowIndex in visibleRows.prefix(count) {
        guard rows.indices.contains(rowIndex) else { return nil }
        picked.insert(rows[rowIndex].identity)
    }
    return picked.isEmpty ? nil : picked
}

private final class MultiNodeCheckbox: NSButton {
    var nodeIdentity: MultiIdentity?
}

private struct MultiGroupContext {
    let controller: MultiControllerIdentity
    let profile: MultiProfileSnapshot
    let groups: [ProxyGroup]
}

private func multiHexColor(_ hex: UInt32, alpha: CGFloat = 1.0) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

private func multiIsDarkAppearance(_ appearance: NSAppearance) -> Bool {
    appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
}

// Scheme A uses opaque surfaces. Dynamic tokens keep labels and custom
// drawing in sync when the system appearance changes.
enum MultiPalette {
    private static func color(_ name: String, dark: UInt32, light: UInt32) -> NSColor {
        NSColor(name: NSColor.Name("ClashGraphite.\(name)")) { appearance in
            multiHexColor(multiIsDarkAppearance(appearance) ? dark : light)
        }
    }
    static let background = color("background", dark: 0x14171B, light: 0xF3F5F8)
    static let bar = color("bar", dark: 0x1A1E23, light: 0xF9FAFC)
    static let panel = color("panel", dark: 0x1C2026, light: 0xFFFFFF)
    static let elevated = color("elevated", dark: 0x232932, light: 0xEDF1F6)
    static let border = color("border", dark: 0x39414C, light: 0xC8D0DA)
    static let divider = color("divider", dark: 0x2C333D, light: 0xDFE4EB)
    static let text = color("text", dark: 0xEDF1F5, light: 0x202936)
    static let muted = color("muted", dark: 0xA8B2C0, light: 0x505E70)
    static let faint = color("faint", dark: 0x9DA9B9, light: 0x5E6B7C)
    static let accent = color("accent", dark: 0xA9C7FF, light: 0x284F98)
    static let accentInk = color("accentInk", dark: 0x182538, light: 0xFFFFFF)
    static let selection = color("selection", dark: 0x28364B, light: 0xE2EBFA)
    static let success = color("success", dark: 0x84D2B1, light: 0x246A4D)
    static let successSurface = color("successSurface", dark: 0x20332C, light: 0xE6F1EB)
    static let warning = color("warning", dark: 0xEDBF80, light: 0x815414)
    static let danger = color("danger", dark: 0xF2A19C, light: 0xA83732)

    static var outline: NSColor {
        NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast ? muted : border
    }
    static var reducesMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
}

final class MultiResultRowView: NSTableRowView {
    var isCascadeHighlighted = false { didSet { needsDisplay = true } }
    var isRecentlyScored = false { didSet { needsDisplay = true } }
    var isActiveTesting = false { didSet { needsDisplay = true } }
    override var isEmphasized: Bool { get { false } set {} }

    override func drawBackground(in dirtyRect: NSRect) {
        let b = bounds.insetBy(dx: 4, dy: 1)
        guard b.width > 4, b.height > 4 else { return }
        let path = NSBezierPath(roundedRect: b, xRadius: 5, yRadius: 5)
        if isCascadeHighlighted {
            MultiPalette.selection.setFill(); path.fill()
        } else if isRecentlyScored {
            MultiPalette.successSurface.setFill(); path.fill()
        } else if isActiveTesting {
            MultiPalette.elevated.setFill(); path.fill()
        }
        MultiPalette.divider.setFill()
        NSRect(x: 6, y: bounds.maxY - 0.5, width: max(0, bounds.width - 12), height: 0.5).fill()
    }

    override func drawSelection(in dirtyRect: NSRect) {
        guard selectionHighlightStyle != .none else { return }
        let b = bounds.insetBy(dx: 4, dy: 1)
        guard b.width > 4, b.height > 4 else { return }
        let path = NSBezierPath(roundedRect: b, xRadius: 5, yRadius: 5)
        MultiPalette.selection.setFill(); path.fill()
        if NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast {
            MultiPalette.accent.setStroke(); path.lineWidth = 1; path.stroke()
        }
    }
}

enum MultiDockVisualState: Equatable {
    case idle
    case racing(progress: CGFloat)
    case crossProfilePending(ready: Bool)
    case switchedConfirmed
}

// Preserve the existing view API while replacing the old glass treatment.
enum MultiClearGlassStyle {
    case clearPrimary, clearSecondary, clearSubtle, clearDanger
    case frostedCard, frostedDock, darkEmeraldPill
}

final class MultiClearAmbientCanvas: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        MultiPalette.background.setFill(); bounds.fill()
        MultiPalette.bar.setFill()
        NSRect(x: 0, y: 0, width: bounds.width, height: 54).fill()
        MultiPalette.divider.setFill()
        NSRect(x: 0, y: 53, width: bounds.width, height: 1).fill()
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        invalidateAppearance()
    }
    func invalidateAppearance() {
        func redraw(_ view: NSView) {
            view.needsDisplay = true
            for child in view.subviews { redraw(child) }
        }
        redraw(self)
    }
}

final class MultiClearFrostedSurface: NSView {
    var style: MultiClearGlassStyle { didSet { needsDisplay = true } }
    var cornerRadius: CGFloat { didSet { needsDisplay = true } }
    var isCapsule: Bool { didSet { needsDisplay = true } }
    var dockState: MultiDockVisualState = .idle { didSet { needsDisplay = true } }
    var dockVisualState: MultiDockVisualState {
        get { dockState }
        set { dockState = newValue }
    }

    init(style: MultiClearGlassStyle, cornerRadius: CGFloat = 14, isCapsule: Bool = false) {
        self.style = style; self.cornerRadius = cornerRadius; self.isCapsule = isCapsule
        super.init(frame: .zero)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 2, bounds.height > 2 else { return }
        let r = style == .frostedDock ? 12 : (isCapsule ? min(6, bounds.height / 2) : cornerRadius)
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: r, yRadius: r)
        switch style {
        case .darkEmeraldPill: MultiPalette.successSurface.setFill()
        case .clearPrimary, .clearSecondary, .clearDanger: MultiPalette.elevated.setFill()
        default: MultiPalette.panel.setFill()
        }
        path.fill()
        MultiPalette.outline.setStroke()
        path.lineWidth = 1
        path.stroke()
    }
}

final class MultiClearGlassButton: NSButton {
    var glassStyle: MultiClearGlassStyle = .clearSecondary { didSet { needsDisplay = true } }
    var isProminent = false { didSet { needsDisplay = true } }
    var symbolName: String? { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    var pillHeight: CGFloat = 30 { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    var hPadding: CGFloat = 12 { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    var fontSize: CGFloat = 12 { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    var leadingDotColor: NSColor? { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    private var isHovered = false { didSet { needsDisplay = true } }
    private var isPressedVisual = false { didSet { needsDisplay = true } }
    private var trackingAreaRef: NSTrackingArea?
    override var title: String { didSet { invalidateIntrinsicContentSize(); needsDisplay = true } }
    override var isEnabled: Bool { didSet { needsDisplay = true } }

    convenience init(title: String, symbol: String? = nil, style: MultiClearGlassStyle = .clearSecondary,
                     height: CGFloat = 30, hPadding: CGFloat = 12, fontSize: CGFloat = 12) {
        self.init(frame: .zero)
        self.title = title; symbolName = symbol; glassStyle = style
        pillHeight = height; self.hPadding = hPadding; self.fontSize = fontSize
        isBordered = false; bezelStyle = .regularSquare
        setButtonType(.momentaryPushIn)
    }
    override var isFlipped: Bool { true }
    private var labelFont: NSFont { .systemFont(ofSize: fontSize, weight: .semibold) }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = trackingAreaRef { removeTrackingArea(existing) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area); trackingAreaRef = area
    }
    override func mouseEntered(with event: NSEvent) {
        super.mouseEntered(with: event)
        if isEnabled { isHovered = true }
    }
    override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        isHovered = false; isPressedVisual = false
    }
    override func mouseDown(with event: NSEvent) {
        guard isEnabled else { return }
        isPressedVisual = true
        super.mouseDown(with: event)
        isPressedVisual = false
    }
    override var intrinsicContentSize: NSSize {
        let textW = ceil((title as NSString).size(withAttributes: [.font: labelFont]).width)
        let iconW: CGFloat = symbolName == nil ? 0 : fontSize + 5
        return NSSize(width: hPadding * 2 + (leadingDotColor == nil ? 0 : 12) + iconW + textW + 2,
                      height: pillHeight)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 2, bounds.height > 2 else { return }
        let b = bounds
        let prominent = glassStyle == .clearPrimary && isProminent && isEnabled
        let selected = glassStyle == .clearPrimary && isEnabled
        var fill: NSColor = prominent ? MultiPalette.accent :
            (selected ? MultiPalette.selection : MultiPalette.elevated)
        var ink: NSColor = MultiPalette.text
        if !isEnabled { ink = MultiPalette.faint }
        else if prominent { ink = MultiPalette.accentInk }
        else if selected { ink = MultiPalette.accent }
        else if glassStyle == .clearDanger { ink = MultiPalette.danger }
        else if glassStyle == .darkEmeraldPill { ink = MultiPalette.success; fill = MultiPalette.successSurface }
        else if glassStyle == .clearSubtle { ink = MultiPalette.muted; fill = MultiPalette.panel }
        if isPressedVisual && isEnabled {
            fill = fill.blended(withFraction: 0.10, of: ink) ?? fill
        } else if isHovered && isEnabled && !prominent {
            fill = selected ? MultiPalette.selection : MultiPalette.elevated
        }
        let path = NSBezierPath(roundedRect: b.insetBy(dx: 1, dy: 1), xRadius: 6, yRadius: 6)
        fill.setFill(); path.fill()
        let focused = window?.firstResponder === self
        (focused ? MultiPalette.accent : (isHovered && isEnabled ? MultiPalette.muted : MultiPalette.outline)).setStroke()
        path.lineWidth = focused ? 2 : 1
        path.stroke()

        let attr: [NSAttributedString.Key: Any] = [.font: labelFont, .foregroundColor: ink]
        let textSize = (title as NSString).size(withAttributes: attr)
        let iconW: CGFloat = symbolName == nil ? 0 : fontSize + 4
        let dotW: CGFloat = leadingDotColor == nil ? 0 : 11
        let textW = min(textSize.width, max(0, b.width - hPadding * 2 - iconW - dotW))
        var cursorX = max(hPadding, (b.width - dotW - iconW - textW) / 2)
        if let dot = leadingDotColor {
            (isEnabled ? dot : MultiPalette.faint).setFill()
            NSBezierPath(ovalIn: NSRect(x: cursorX, y: (b.height - 6) / 2, width: 6, height: 6)).fill()
            cursorX += 11
        }
        if let symbol = symbolName, let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil),
           let configured = image.withSymbolConfiguration(.init(pointSize: fontSize - 1, weight: .semibold)),
           let tinted = configured.copy() as? NSImage {
            tinted.lockFocus(); ink.set()
            NSRect(origin: .zero, size: tinted.size).fill(using: .sourceAtop)
            tinted.unlockFocus()
            tinted.draw(in: NSRect(x: cursorX, y: (b.height - tinted.size.height) / 2,
                                  width: tinted.size.width, height: tinted.size.height))
            cursorX += iconW
        }
        let paragraph = NSMutableParagraphStyle(); paragraph.lineBreakMode = .byTruncatingTail
        var textAttr = attr; textAttr[.paragraphStyle] = paragraph
        (title as NSString).draw(in: NSRect(x: cursorX, y: (b.height - textSize.height) / 2 - 0.5,
                                          width: textW + 2, height: textSize.height), withAttributes: textAttr)
    }
}

final class MultiChampionCardView: NSView {
    var crownPulseIntensity: CGFloat = 0 { didSet { needsDisplay = true } }
    var isCrossProfileReady = false { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 14, yRadius: 14)
        let feedback = MultiPalette.reducesMotion ? 0 : min(1, max(0, crownPulseIntensity)) * 0.35
        (MultiPalette.panel.blended(withFraction: feedback, of: MultiPalette.selection) ?? MultiPalette.panel).setFill()
        path.fill()
        MultiPalette.outline.setStroke(); path.lineWidth = 1; path.stroke()
    }
}

final class MultiSpeedBarCellView: NSTableCellView {
    var speedText = "—" { didSet { needsDisplay = true; setAccessibilityValue(speedText) } }
    var fraction: CGFloat = 0 { didSet { needsDisplay = true } }
    var isTesting = false { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }

    func configure(text: String, fraction: CGFloat, isTesting: Bool) {
        speedText = text; self.fraction = max(0, min(1, fraction)); self.isTesting = isTesting
        setAccessibilityElement(true); setAccessibilityRole(.staticText)
        setAccessibilityLabel("验证吞吐")
        setAccessibilityValue(isTesting ? "测速中" : text)
    }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let b = bounds
        guard b.width > 12, b.height > 10 else { return }
        let measured = speedText != "—"
        let text = isTesting ? "测速中…" : speedText
        let attr: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: measured ? .medium : .regular),
            .foregroundColor: isTesting ? MultiPalette.accent : (measured ? MultiPalette.text : MultiPalette.faint)
        ]
        let size = (text as NSString).size(withAttributes: attr)
        let y: CGFloat = measured || isTesting ? 3.5 : (b.height - size.height) / 2
        (text as NSString).draw(in: NSRect(x: max(4, b.width - size.width - 8), y: y,
                                         width: size.width + 2, height: size.height), withAttributes: attr)
        guard isTesting || (measured && fraction > 0.005) else { return }
        let track = NSRect(x: 8, y: b.height - 6, width: b.width - 16, height: 2)
        MultiPalette.border.setFill()
        NSBezierPath(roundedRect: track, xRadius: 1, yRadius: 1).fill()
        let width = track.width * (isTesting ? 0.32 : fraction)
        MultiPalette.accent.setFill()
        NSBezierPath(roundedRect: NSRect(x: track.maxX - width, y: track.minY, width: width, height: 2),
                     xRadius: 1, yRadius: 1).fill()
    }
}

final class MultiSparklineView: NSView {
    var points: [CGFloat] = [] { didSet { needsDisplay = true } }
    var strokeColor: NSColor = MultiPalette.accent { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        guard points.count > 1 else { return }
        let path = NSBezierPath()
        for (i, point) in points.enumerated() {
            let p = NSPoint(x: CGFloat(i) * bounds.width / CGFloat(points.count - 1),
                            y: bounds.height - point * (bounds.height - 6) - 3)
            if i == 0 { path.move(to: p) } else { path.line(to: p) }
        }
        strokeColor.setStroke()
        path.lineWidth = 1.4; path.lineCapStyle = .round; path.lineJoinStyle = .round
        path.stroke()
    }
}

// A single finite transition carries progress; no shimmer or looping layer.
final class MultiProgressView: NSView {
    private let track = CALayer()
    private let fill = CALayer()
    private(set) var fraction: CGFloat = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.addSublayer(track); track.addSublayer(fill)
        track.cornerRadius = 1.5; fill.cornerRadius = 1.5
        fill.anchorPoint = .zero
        setAccessibilityElement(true); setAccessibilityRole(.progressIndicator)
        setAccessibilityLabel("测速进度")
    }
    required init?(coder: NSCoder) { nil }
    override func layout() {
        super.layout()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        track.frame = NSRect(x: 0, y: (bounds.height - 3) / 2, width: bounds.width, height: 3)
        fill.bounds = NSRect(x: 0, y: 0, width: bounds.width, height: 3)
        fill.position = .zero
        updateColors()
        CATransaction.commit()
    }
    private func updateColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            track.backgroundColor = MultiPalette.border.cgColor
            fill.backgroundColor = MultiPalette.accent.cgColor
        }
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }
    func setFraction(_ value: CGFloat, animated: Bool = true) {
        let value = max(0, min(1, value))
        let previous = fill.presentation()?.transform.m11 ?? fill.transform.m11
        let changed = abs(value - fraction) > 0.0001
        fraction = value
        setAccessibilityValue(NSNumber(value: Double(value) * 100))
        CATransaction.begin(); CATransaction.setDisableActions(true)
        fill.transform = CATransform3DMakeScale(value, 1, 1)
        CATransaction.commit()
        if changed && animated && !MultiPalette.reducesMotion && value > 0 {
            let animation = CABasicAnimation(keyPath: "transform.scale.x")
            animation.fromValue = previous; animation.toValue = value
            animation.duration = 0.45; animation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            fill.add(animation, forKey: "progress")
        } else if MultiPalette.reducesMotion || !animated || value == 0 {
            fill.removeAnimation(forKey: "progress")
        }
    }
    func finishMotion() { fill.removeAllAnimations(); updateColors() }
}

final class MultiTableHeaderCell: NSTableHeaderCell {
    override func draw(withFrame cellFrame: NSRect, in controlView: NSView) {
        MultiPalette.panel.setFill(); cellFrame.fill()
        MultiPalette.divider.setFill()
        NSRect(x: cellFrame.minX, y: cellFrame.maxY - 1, width: cellFrame.width, height: 1).fill()
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment; paragraph.lineBreakMode = .byTruncatingTail
        let font = NSFont.systemFont(ofSize: 11, weight: .medium)
        let height = (stringValue as NSString).size(withAttributes: [.font: font]).height
        (stringValue as NSString).draw(in: NSRect(x: cellFrame.minX + 6, y: cellFrame.midY - height / 2,
                                                width: max(0, cellFrame.width - 12), height: height),
                                      withAttributes: [.font: font, .foregroundColor: MultiPalette.muted,
                                                       .paragraphStyle: paragraph])
    }
}

final class MultiSurfaceView: NSView {
    private let baseFill: NSColor
    private let fillAlpha: CGFloat
    private let baseBorder: NSColor?
    private let borderAlpha: CGFloat
    private let radius: CGFloat
    private let lineWidth: CGFloat

    init(fill: NSColor = MultiPalette.panel, fillAlpha: CGFloat = 1.0,
         border: NSColor? = MultiPalette.divider, borderAlpha: CGFloat = 0.65,
         cornerRadius: CGFloat = 10, borderWidth: CGFloat = 1) {
        self.baseFill = fill
        self.fillAlpha = fillAlpha
        self.baseBorder = border
        self.borderAlpha = borderAlpha
        self.radius = cornerRadius
        self.lineWidth = borderWidth
        super.init(frame: .zero)
        self.wantsLayer = true
    }
    required init?(coder: NSCoder) { nil }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        guard let layer = self.layer else { return }
        layer.cornerRadius = radius
        if #available(macOS 10.15, *) { layer.cornerCurve = .continuous }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let resolvedFill = fillAlpha < 1.0 ? baseFill.withAlphaComponent(fillAlpha) : baseFill
            layer.backgroundColor = resolvedFill.cgColor
            if let baseBorder = baseBorder, lineWidth > 0 {
                let resolvedBorder = borderAlpha < 1.0 ? baseBorder.withAlphaComponent(borderAlpha) : baseBorder
                layer.borderColor = resolvedBorder.cgColor
                layer.borderWidth = lineWidth
            } else {
                layer.borderWidth = 0
                layer.borderColor = nil
            }
        }
    }
}

final class MultiAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                              NSTableViewDataSource, NSTableViewDelegate, NSComboBoxDelegate {
    private let root = ProfileRegistry.defaultRoot
    private let schedule = TestSchedule() // Always disarmed at launch.
    private var timer: Timer?
    private var window: NSWindow!
    private let profileTable = NSTableView(), resultTable = NSTableView()
    private let selectAllProfilesButton = MultiClearGlassButton(title: "全选", style: .clearSecondary, height: 24, hPadding: 10, fontSize: 11)
    private let clearProfilesButton = MultiClearGlassButton(title: "清空", style: .clearSubtle, height: 24, hPadding: 10, fontSize: 11)
    private let refreshButton = MultiClearGlassButton(title: "刷新配置", symbol: "arrow.clockwise", style: .clearSecondary, height: 24, hPadding: 10, fontSize: 11)
    private let funnelButton = MultiClearGlassButton(title: "智能两段优选", symbol: "sparkles", style: .clearSecondary, height: 32, hPadding: 13, fontSize: 12)
    private let loadTestButton = MultiClearGlassButton(title: "加压验证 · 20M", symbol: "bolt.fill", style: .clearSecondary, height: 32, hPadding: 14, fontSize: 12)
    private let switchResultButton = MultiClearGlassButton(title: "切换至选中节点", symbol: "arrow.right", style: .clearSecondary, height: 34, hPadding: 16, fontSize: 12.5)
    private let undoSwitchButton = MultiClearGlassButton(title: "切回原节点", symbol: "arrow.uturn.backward", style: .clearSecondary, height: 34, hPadding: 12, fontSize: 11.5)
    private let champQuickSwitchButton = MultiClearGlassButton(title: "切换最优", symbol: "bolt.fill", style: .darkEmeraldPill, height: 22, hPadding: 8, fontSize: 10.5)
    private let champRetestButton = MultiClearGlassButton(title: "20M 复测", symbol: "arrow.clockwise", style: .darkEmeraldPill, height: 22, hPadding: 8, fontSize: 10.5)
    private let filterAllButton = MultiClearGlassButton(title: "全部", style: .clearPrimary, height: 24, hPadding: 9, fontSize: 11)
    private let filterValidButton = MultiClearGlassButton(title: "仅有效", style: .clearSubtle, height: 24, hPadding: 9, fontSize: 11)
    private let filterTestingButton = MultiClearGlassButton(title: "测速中", style: .clearSubtle, height: 24, hPadding: 9, fontSize: 11)
    private let filterFailedButton = MultiClearGlassButton(title: "失败", style: .clearSubtle, height: 24, hPadding: 9, fontSize: 11)
    private let nodeSearch = NSSearchField()
    private let topNBox = NSComboBox()
    private let selectTopNButton = MultiClearGlassButton(title: "选择前 N 项", style: .clearSecondary, height: 28, hPadding: 8, fontSize: 11)
    private let selectShownButton = MultiClearGlassButton(title: "全选当前", style: .clearSecondary, height: 28, hPadding: 8, fontSize: 11)
    private let clearShownButton = MultiClearGlassButton(title: "清空勾选", style: .clearSubtle, height: 28, hPadding: 8, fontSize: 11)
    private let nodeCount = NSTextField(labelWithString: "已勾选 0 / 0")
    private let emptyStateLabel = NSTextField(labelWithString: "请先勾选订阅源")
    private let trafficEstimate = NSTextField(labelWithString: "预计最大下载：—（不含 HEAD 请求）")
    private let groupPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let refreshGroupsButton = MultiClearGlassButton(title: "刷新", symbol: "arrow.clockwise", style: .clearSecondary, height: 28, hPadding: 10, fontSize: 11.5)
    private let parallelBox = NSComboBox()
    private let sizePopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let modePopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let intervalPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let scheduleButton = MultiClearGlassButton(title: "启用定时", style: .clearSecondary, height: 24, hPadding: 10, fontSize: 11)
    private let startButton = MultiClearGlassButton(title: "开始测速", symbol: "play.fill", style: .clearPrimary, height: 32, hPadding: 16, fontSize: 12.5)
    private let stopButton = MultiClearGlassButton(title: "停止", symbol: "stop.fill", style: .clearDanger, height: 32, hPadding: 12, fontSize: 12)
    private let copyButton = MultiClearGlassButton(title: "导出 TSV", symbol: "square.and.arrow.up", style: .clearSecondary, height: 32, hPadding: 13, fontSize: 12)
    private let autoSwitch = NSButton(checkboxWithTitle: "测速后自动切换最高分（当前订阅）", target: nil, action: nil)
    private let status = NSTextField(wrappingLabelWithString: "请选择订阅。启动应用不会自动测速。")
    private let scheduleStatus = NSTextField(labelWithString: "定时未启用（仅窗口打开时运行）")
    private let subPillsStack = NSStackView()
    private let champCardView = MultiChampionCardView()
    private let bottomDockSurface = MultiClearFrostedSurface(style: .frostedDock, cornerRadius: 27, isCapsule: true)
    private let progressView = MultiProgressView()
    private let champScoreLabel = NSTextField(labelWithString: "—")
    private let champNodeLabel = NSTextField(labelWithString: "等待测速或选择节点")
    private let champSubLabel = NSTextField(labelWithString: "请选择订阅并运行多订阅基准测速")
    private let champSpeedLabel = NSTextField(labelWithString: "—")
    private let champMetaLabel = NSTextField(labelWithString: "延迟 —  ·  抖动 —  ·  丢包 —")
    private let champReadyLabel = NSTextField(labelWithString: "● 待命就绪")
    private let champSparkline = MultiSparklineView()
    private var uiMotionTimer: Timer?
    private var feedbackTimer: Timer?
    private var displayOptionsObserver: NSObjectProtocol?
    private var statusFilter: MultiRowStatusFilter = .all
    private var sortColumnIdentifier: String?
    private var sortColumnAscending: Bool = false
    private var cascadeHighlightedIdentities = Set<MultiIdentity>()
    private var recentlyScoredIdentities = Set<MultiIdentity>()
    private var funnelStage: Int = 0 // 0 = normal, 1 = quick screening, 2 = Top-N pressure test
    private var lastUndoSwitchChoice: MultiPickerChoice?
    private var lastUndoTargetNodeName: String?
    private var crossProfileReadyDetected = false
    private var targetChampSpeed: Double = 0
    private var targetChampScore: Double = 0
    private var dockConfirmedUntil: Date?
    private var registry: [ProfileOption] = []
    private var selected = Set<Data>()
    // The checked identities are bound to their immutable, revalidated preview.
    private var nodeSelection: Set<MultiIdentity>?
    private var selectionPreview: MultiPreparedBatch?
    private var rows: [MultiRow] = []
    private var visibleRows: [Int] = []
    private var runIdentities = Set<MultiIdentity>()
    private var runJobIdentity: [Int: MultiIdentity] = [:]
    private var groupContext: MultiGroupContext?
    private var groupGeneration: UInt64 = 0
    private var groupRefreshInFlight = false
    private var requestedSwitchCapture: MultiSwitchCapture?
    private var currentBatch: MultiPreparedBatch?
    private var armedBatch: MultiPreparedBatch?
    private var runner: MultiBatchRunner?
    private let launchChecks = MultiSerialValidationQueue()
    private var launchCheckAuthorization: MultiSwitchAuthorization?
    private var launchCheckFailure: String?
    private var busy = false
    private var displayedBusyState = false
    private var closed = false
    private var activeToken: TestSchedule.RunToken?
    private var armedSwitch: MultiSwitchCapture?
    private var runCapture: MultiSwitchCapture?
    private var runScheduled = false
    private var runSwitchRequested = false
    private var cancelled = false
    private var runFailure = false
    private var applying = false
    private var manualSwitchDisarmedSchedule = false
    private var switchOutcomeUnknown = false
    private var pendingWinner: MultiPendingWinner?
    private var pendingManual: MultiPickerChoice?
    private var switchCoordinator: AssistedSwitchCoordinator?
    private var switchAuthorization: MultiSwitchAuthorization?
#if !MULTI_UI_TEST
    private let runtimeInjector = ClashRuntimeInjector.shared
#endif
    private let sizeValues = [256 * 1024, 512 * 1024, 1 * 1_048_576, 2 * 1_048_576,
                              3 * 1_048_576, 5 * 1_048_576, 20 * 1_048_576, 50 * 1_048_576]
    private let sizeLabels = ["256 KiB", "512 KiB", "1 MiB", "2 MiB", "3 MiB", "5 MiB", "20 MiB", "50 MiB"]
    private let intervals: [TimeInterval] = [900, 1800, 3600, 7200, 21600, 43200, 86400]
    private let minimumWindowContentSize = NSSize(width: 1080, height: 700)
    private let preferredWindowContentSize = NSSize(width: 1240, height: 800)

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
#if !MULTI_UI_TEST
        runtimeInjector.start { [weak self] result in
            DispatchQueue.main.async {
                guard let self, !self.closed else { return }
                switch result {
                case .success:
                    self.runtimeBridgeDidAttach()
                case .failure(let error):
                    self.status.stringValue = "Clash Verge 运行时补丁未连接：\(error.localizedDescription)"
                }
                self.updateControls()
            }
        }
#endif
        refresh(nil)
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.pollSchedule() }
        // Countdown and bridge state need only a one-second cadence. Animation
        // is finite and runs only while an actual value is changing.
        uiMotionTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tickUIMotion() }
        displayOptionsObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            (self.window.contentView as? MultiClearAmbientCanvas)?.invalidateAppearance()
            if MultiPalette.reducesMotion {
                self.feedbackTimer?.invalidate(); self.feedbackTimer = nil
                self.champCardView.crownPulseIntensity = 0
                self.progressView.finishMotion()
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if busy { requestStopBeforeClose(); return .terminateCancel }
        return .terminateNow
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if busy { requestStopBeforeClose(); return false }
        return true
    }
    private func requestStopBeforeClose() {
        // Revocation affects checks not yet passed; it cannot retract a commit.
        switchAuthorization?.revoke(); cancelled = true; pendingWinner = nil; pendingManual = nil
        disarm(); cancelLaunchChecks(); runner?.cancel(); stopButton.isEnabled = false
        // Cancelling a scheduled batch can synchronously invoke finished/abort.
        // In that case the window stays open but is already ready to close.
        status.stringValue = !busy ? "本次操作已停止；可再次关闭窗口。" : applying ? "正在撤销切换授权；已通过最终检查的请求仍可能发送或已发出，请等待结果后关闭。" : "正在停止操作；请等待结束后关闭窗口。"
    }
    func windowWillClose(_ notification: Notification) { closeApp() }
    func windowDidResize(_ notification: Notification) {
        guard let window else { return }
        defer { updateCompactLayout() }
        let minimumFrame = window.frameRect(forContentRect: NSRect(origin: .zero, size: minimumWindowContentSize)).size
        guard window.frame.width < minimumFrame.width || window.frame.height < minimumFrame.height else { return }
        window.setContentSize(minimumWindowContentSize)
    }
    private func updateCompactLayout() {
        // The optional explanation moves to the timer button's help at narrow
        // widths, rather than leaving a clipped character beside the controls.
        scheduleStatus.isHidden = (window.contentView?.bounds.width ?? 1240) < 1180
        scheduleButton.toolTip = scheduleStatus.stringValue
    }
    func applicationWillTerminate(_ notification: Notification) { closeApp() }
    private func closeApp() {
        guard !closed else { return }
        closed = true; timer?.invalidate(); timer = nil; uiMotionTimer?.invalidate(); uiMotionTimer = nil
        feedbackTimer?.invalidate(); feedbackTimer = nil
        if let observer = displayOptionsObserver { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
        displayOptionsObserver = nil
#if !MULTI_UI_TEST
        runtimeInjector.stop()
#endif
        schedule.disarm(); armedBatch = nil; switchAuthorization?.revoke()
        cancelLaunchChecks(); runner?.cancel()
    }
#if !MULTI_UI_TEST
    private func runtimeBridgeDidAttach() {
        guard !closed else { return }
        // Do not overwrite an active measurement or switch status. Once the
        // catalogue is loaded, however, a bridge attach is also the signal to
        // recover a controller refresh that may have raced Clash startup.
        guard !busy else { return }
        guard selectionPreview != nil else {
            status.stringValue = "Clash Verge 运行时补丁已连接；请选择订阅。"
            return
        }
        guard !groupRefreshInFlight, groupContext == nil else { return }
        status.stringValue = "Clash Verge 运行时补丁已连接；正在恢复策略组连接…"
        refreshGroups(nil)
    }
#endif
    private func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
                       color: NSColor = MultiPalette.text, monospacedDigits: Bool = false) -> NSTextField {
        let v = NSTextField(labelWithString: text)
        v.font = monospacedDigits ? .monospacedDigitSystemFont(ofSize: size, weight: weight) : .systemFont(ofSize: size, weight: weight)
        v.textColor = color
        return v
    }
    private func symbolIcon(_ name: String, color: NSColor = MultiPalette.muted, size: CGFloat = 13) -> NSImageView {
        let view = NSImageView()
        if let image = NSImage(systemSymbolName: name, accessibilityDescription: nil) {
            let config = NSImage.SymbolConfiguration(pointSize: size, weight: .semibold)
            view.image = image.withSymbolConfiguration(config)
        }
        view.contentTintColor = color
        view.setAccessibilityElement(false)
        view.setContentHuggingPriority(.required, for: .horizontal)
        return view
    }
    private func styleButton(_ button: NSButton, symbol: String? = nil, prominent: Bool = false,
                             tint: NSColor? = nil, small: Bool = false, recessed: Bool = false) {
        button.bezelStyle = recessed ? .recessed : .rounded
        if recessed { button.showsBorderOnlyWhileMouseInside = true }
        button.controlSize = small ? .small : .regular
        button.font = .systemFont(ofSize: small ? 11.5 : 12.5, weight: prominent ? .semibold : .medium)
        if let symbol = symbol, let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            let config = NSImage.SymbolConfiguration(pointSize: small ? 10 : 11.5, weight: .semibold)
            button.image = image.withSymbolConfiguration(config)
            button.imagePosition = .imageLeading
        }
        if let tint = tint {
            button.contentTintColor = tint
        }
    }
    private func cardBox(padding: NSEdgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12),
                         fill: NSColor = MultiPalette.panel,
                         fillAlpha: CGFloat = 0.78,
                         spacing: CGFloat = 8) -> (NSView, NSStackView) {
        let box = MultiSurfaceView(fill: fill, fillAlpha: fillAlpha, border: MultiPalette.divider,
                                   borderAlpha: 1.0, cornerRadius: 10, borderWidth: 1)
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        stack.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: padding.left),
            stack.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -padding.right),
            stack.topAnchor.constraint(equalTo: box.topAnchor, constant: padding.top),
            stack.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -padding.bottom)
        ])
        return (box, stack)
    }
    private func pillBadge(_ view: NSView, fill: NSColor = MultiPalette.elevated,
                           fillAlpha: CGFloat = 1.0,
                           cornerRadius: CGFloat = 8, hPad: CGFloat = 8, vPad: CGFloat = 3) -> NSView {
        let box = MultiSurfaceView(fill: fill, fillAlpha: fillAlpha, border: nil,
                                   borderAlpha: 0, cornerRadius: cornerRadius, borderWidth: 0)
        view.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: hPad),
            view.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -hPad),
            view.topAnchor.constraint(equalTo: box.topAnchor, constant: vPad),
            view.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -vPad)
        ])
        return box
    }
    private func divider(vertical: Bool = false) -> NSView {
        let line = MultiSurfaceView(fill: MultiPalette.divider, fillAlpha: 1.0, border: nil,
                                    borderAlpha: 0, cornerRadius: 0, borderWidth: 0)
        if vertical {
            line.widthAnchor.constraint(equalToConstant: 1).isActive = true
        } else {
            line.heightAnchor.constraint(equalToConstant: 1).isActive = true
        }
        return line
    }
    private func spacer() -> NSView {
        let v = NSView()
        v.setContentHuggingPriority(.init(1), for: .horizontal)
        v.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        return v
    }
    private func row(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
        let s = NSStackView(views: views); s.orientation = .horizontal; s.alignment = .centerY; s.spacing = spacing; return s
    }
    private func scroller(_ table: NSTableView, height: CGFloat, drawsBackground: Bool = true) -> NSScrollView {
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false; scroll.autohidesScrollers = true
        scroll.drawsBackground = drawsBackground
        if drawsBackground { scroll.backgroundColor = MultiPalette.panel }
        scroll.documentView = table
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: height).isActive = true
        return scroll
    }
    private func addColumn(_ table: NSTableView, _ id: String, _ title: String, _ width: CGFloat,
                           minWidth: CGFloat = 44, maxWidth: CGFloat? = nil, align: NSTextAlignment = .left) {
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
        col.headerCell = MultiTableHeaderCell(textCell: title)
        col.title = title; col.width = width; col.minWidth = minWidth
        if let maxWidth = maxWidth { col.maxWidth = maxWidth }
        col.headerCell.alignment = align
        table.addTableColumn(col)
    }
    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Clash 多订阅测速助手"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = MultiPalette.background
        window.minSize = minimumWindowContentSize
        window.contentMinSize = minimumWindowContentSize
        window.center(); window.delegate = self
        // A persisted frame can be narrower than this fixed desktop layout.
        window.setContentSize(preferredWindowContentSize)
        window.center()

        let rootCanvas = MultiClearAmbientCanvas(frame: NSRect(x: 0, y: 0, width: 1240, height: 800))
        window.contentView = rootCanvas
        startButton.isProminent = true
        startButton.widthAnchor.constraint(equalToConstant: 112).isActive = true

        // Wire all actions (preserving exact single-occurrence E2E action anchors)
        selectAllProfilesButton.target = self; selectAllProfilesButton.action = #selector(selectAllProfiles(_:))
        clearProfilesButton.target = self; clearProfilesButton.action = #selector(clearAllProfiles(_:))
        refreshButton.target = self; refreshButton.action = #selector(refresh(_:))
        funnelButton.target = self; funnelButton.action = #selector(startFunnelTest(_:))
        loadTestButton.target = self; loadTestButton.action = #selector(startLoadTest(_:))
        switchResultButton.target = self; switchResultButton.action = #selector(switchSelectedResult(_:))
        undoSwitchButton.target = self; undoSwitchButton.action = #selector(undoLastSwitch(_:)); undoSwitchButton.isHidden = true
        champQuickSwitchButton.target = self; champQuickSwitchButton.action = #selector(switchChampionRoute(_:))
        champRetestButton.target = self; champRetestButton.action = #selector(retestChampionRoute(_:))
        filterAllButton.tag = 0; filterAllButton.target = self; filterAllButton.action = #selector(statusFilterChanged(_:))
        filterValidButton.tag = 1; filterValidButton.target = self; filterValidButton.action = #selector(statusFilterChanged(_:))
        filterTestingButton.tag = 2; filterTestingButton.target = self; filterTestingButton.action = #selector(statusFilterChanged(_:))
        filterFailedButton.tag = 3; filterFailedButton.target = self; filterFailedButton.action = #selector(statusFilterChanged(_:))
        startButton.target = self; startButton.action = #selector(startManual(_:)); startButton.keyEquivalent = "\r"
        stopButton.target = self; stopButton.action = #selector(stop(_:))
        copyButton.target = self; copyButton.action = #selector(copyResults(_:))
        selectTopNButton.target = self; selectTopNButton.action = #selector(selectTopNNodes(_:))
        selectShownButton.target = self; selectShownButton.action = #selector(selectShownNodes(_:))
        clearShownButton.target = self; clearShownButton.action = #selector(clearShownNodes(_:))
        refreshGroupsButton.target = self; refreshGroupsButton.action = #selector(refreshGroups(_:))
        scheduleButton.target = self; scheduleButton.action = #selector(toggleSchedule(_:))

        // =========================================================================
        // 1. TOP COMMAND BAR (Height: 54 pt)
        // =========================================================================
        let topBar = NSView()
        topBar.translatesAutoresizingMaskIntoConstraints = false

        let brandOrb = MultiClearFrostedSurface(style: .clearPrimary, cornerRadius: 13, isCapsule: true)
        brandOrb.translatesAutoresizingMaskIntoConstraints = false
        brandOrb.widthAnchor.constraint(equalToConstant: 26).isActive = true
        brandOrb.heightAnchor.constraint(equalToConstant: 26).isActive = true
        let brandIcon = symbolIcon("waveform.path", color: MultiPalette.accent, size: 12.5)
        brandIcon.translatesAutoresizingMaskIntoConstraints = false
        brandOrb.addSubview(brandIcon)
        NSLayoutConstraint.activate([
            brandIcon.centerXAnchor.constraint(equalTo: brandOrb.centerXAnchor),
            brandIcon.centerYAnchor.constraint(equalTo: brandOrb.centerYAnchor)
        ])

        let appTitle = label("Clash 测速工作台", size: 14, weight: .semibold)
        let verPillSurface = MultiClearFrostedSurface(style: .clearSubtle, cornerRadius: 10, isCapsule: true)
        let verLabel = label("Mihomo", size: 10.5, weight: .medium, color: MultiPalette.muted)
        verLabel.translatesAutoresizingMaskIntoConstraints = false
        verPillSurface.addSubview(verLabel)
        NSLayoutConstraint.activate([
            verLabel.leadingAnchor.constraint(equalTo: verPillSurface.leadingAnchor, constant: 9),
            verLabel.trailingAnchor.constraint(equalTo: verPillSurface.trailingAnchor, constant: -9),
            verLabel.centerYAnchor.constraint(equalTo: verPillSurface.centerYAnchor),
            verPillSurface.heightAnchor.constraint(equalToConstant: 20)
        ])

        let topBarRow = row([
            brandOrb, appTitle, verPillSurface,
            spacer(),
            copyButton, funnelButton, loadTestButton, stopButton, startButton
        ], spacing: 8)
        topBarRow.translatesAutoresizingMaskIntoConstraints = false
        topBar.addSubview(topBarRow)
        NSLayoutConstraint.activate([
            topBarRow.leadingAnchor.constraint(equalTo: topBar.leadingAnchor, constant: 86),
            topBarRow.trailingAnchor.constraint(equalTo: topBar.trailingAnchor, constant: -24),
            topBarRow.centerYAnchor.constraint(equalTo: topBar.centerYAnchor, constant: 2),
            topBar.heightAnchor.constraint(equalToConstant: 54)
        ])

        // =========================================================================
        // 2. ORIGINAL SPLIT DECK: champion and subscription matrix.
        // =========================================================================
        let topDeck = NSView()
        topDeck.translatesAutoresizingMaskIntoConstraints = false

        let champCard = champCardView
        champCard.translatesAutoresizingMaskIntoConstraints = false

        let champTag = label("OPTIMAL ROUTE", size: 10, weight: .medium, color: MultiPalette.muted, monospacedDigits: true)
        champScoreLabel.font = .monospacedDigitSystemFont(ofSize: 12.5, weight: .bold)
        champScoreLabel.textColor = MultiPalette.accent
        let champScoreBadge = MultiClearFrostedSurface(style: .clearPrimary, cornerRadius: 12, isCapsule: true)
        champScoreLabel.translatesAutoresizingMaskIntoConstraints = false
        champScoreBadge.addSubview(champScoreLabel)
        NSLayoutConstraint.activate([
            champScoreLabel.leadingAnchor.constraint(equalTo: champScoreBadge.leadingAnchor, constant: 10),
            champScoreLabel.trailingAnchor.constraint(equalTo: champScoreBadge.trailingAnchor, constant: -10),
            champScoreLabel.centerYAnchor.constraint(equalTo: champScoreBadge.centerYAnchor),
            champScoreBadge.heightAnchor.constraint(equalToConstant: 24)
        ])
        let champTopRow = row([champTag, spacer(), champRetestButton, champQuickSwitchButton, champScoreBadge], spacing: 6)

        champNodeLabel.font = .systemFont(ofSize: 16.5, weight: .bold)
        champNodeLabel.textColor = MultiPalette.text
        champNodeLabel.lineBreakMode = .byTruncatingTail

        champSubLabel.font = .systemFont(ofSize: 11.5, weight: .medium)
        champSubLabel.textColor = MultiPalette.muted
        champSubLabel.lineBreakMode = .byTruncatingTail

        champSpeedLabel.font = .monospacedDigitSystemFont(ofSize: 24, weight: .medium)
        champSpeedLabel.textColor = MultiPalette.accent
        let champSpeedUnit = label("MiB/s", size: 11.5, weight: .medium, color: MultiPalette.muted)
        champMetaLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        champMetaLabel.textColor = MultiPalette.muted
        champMetaLabel.lineBreakMode = .byTruncatingTail

        champSparkline.translatesAutoresizingMaskIntoConstraints = false
        champSparkline.setAccessibilityElement(false)
        champSparkline.toolTip = "当前吞吐量示意，不代表历史曲线"
        champSparkline.widthAnchor.constraint(equalToConstant: 82).isActive = true
        champSparkline.heightAnchor.constraint(equalToConstant: 24).isActive = true

        champReadyLabel.font = .systemFont(ofSize: 10.5, weight: .bold)
        champReadyLabel.textColor = MultiPalette.success
        let champReadyPill = MultiClearFrostedSurface(style: .darkEmeraldPill, cornerRadius: 11, isCapsule: true)
        champReadyLabel.translatesAutoresizingMaskIntoConstraints = false
        champReadyPill.addSubview(champReadyLabel)
        NSLayoutConstraint.activate([
            champReadyLabel.leadingAnchor.constraint(equalTo: champReadyPill.leadingAnchor, constant: 9),
            champReadyLabel.trailingAnchor.constraint(equalTo: champReadyPill.trailingAnchor, constant: -9),
            champReadyLabel.centerYAnchor.constraint(equalTo: champReadyPill.centerYAnchor),
            champReadyPill.heightAnchor.constraint(equalToConstant: 22)
        ])

        let champBottomRow = row([
            champSpeedLabel, champSpeedUnit, spacer(),
            champSparkline, champReadyPill
        ], spacing: 8)
        let champStack = NSStackView(views: [champTopRow, champNodeLabel, champSubLabel, spacer(), champMetaLabel, champBottomRow])
        champStack.orientation = .vertical; champStack.alignment = .leading; champStack.spacing = 4
        champStack.translatesAutoresizingMaskIntoConstraints = false
        champCard.addSubview(champStack)
        NSLayoutConstraint.activate([
            champTopRow.widthAnchor.constraint(equalTo: champStack.widthAnchor),
            champBottomRow.widthAnchor.constraint(equalTo: champStack.widthAnchor),
            champStack.leadingAnchor.constraint(equalTo: champCard.leadingAnchor, constant: 18),
            champStack.trailingAnchor.constraint(equalTo: champCard.trailingAnchor, constant: -18),
            champStack.topAnchor.constraint(equalTo: champCard.topAnchor, constant: 14),
            champStack.bottomAnchor.constraint(equalTo: champCard.bottomAnchor, constant: -14)
        ])

        // Right Matrix Card: Subscription Pills + Compact Table + Capsule Parameter Strip
        let matrixCard = MultiClearFrostedSurface(style: .frostedCard, cornerRadius: 14, isCapsule: false)
        matrixCard.translatesAutoresizingMaskIntoConstraints = false

        trafficEstimate.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        trafficEstimate.textColor = MultiPalette.muted
        trafficEstimate.lineBreakMode = .byTruncatingTail

        let matrixHeaderRow = row([
            label("订阅源矩阵", size: 12.5, weight: .semibold),
            trafficEstimate,
            spacer(),
            selectAllProfilesButton,
            clearProfilesButton,
            refreshButton
        ], spacing: 8)

        subPillsStack.orientation = .horizontal
        subPillsStack.alignment = .centerY
        subPillsStack.spacing = 8
        subPillsStack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        // Keep profileTable in the view hierarchy (compact alongside pills) so programmatic E2E & accessibility work seamlessly
        profileTable.delegate = self; profileTable.dataSource = self; profileTable.rowHeight = 24
        profileTable.intercellSpacing = NSSize(width: 4, height: 1)
        profileTable.headerView = nil
        profileTable.backgroundColor = .clear
        profileTable.usesAlternatingRowBackgroundColors = false
        if #available(macOS 11.0, *) { profileTable.style = .plain }
        profileTable.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        addColumn(profileTable, "select", "勾选", 24, minWidth: 22, maxWidth: 28, align: .center)
        addColumn(profileTable, "profile", "订阅名称", 160, minWidth: 100)
        let profilesScroll = scroller(profileTable, height: 28, drawsBackground: false)
        profilesScroll.isHidden = true
        profilesScroll.widthAnchor.constraint(equalToConstant: 1).isActive = true
        profilesScroll.heightAnchor.constraint(equalToConstant: 28).isActive = true

        let subMidRow = row([subPillsStack, spacer(), profilesScroll], spacing: 8)

        groupPopup.addItem(withTitle: "请选择当前订阅策略组")
        groupPopup.controlSize = .small; groupPopup.font = .systemFont(ofSize: 11.5, weight: .semibold)
        groupPopup.target = self; groupPopup.action = #selector(groupChanged(_:))
        groupPopup.widthAnchor.constraint(equalToConstant: 210).isActive = true

        let groupSelectorPill = MultiClearFrostedSurface(style: .clearSecondary, cornerRadius: 15, isCapsule: true)
        groupSelectorPill.translatesAutoresizingMaskIntoConstraints = false
        groupSelectorPill.heightAnchor.constraint(equalToConstant: 30).isActive = true
        let groupSelectorRow = row([
            symbolIcon("arrow.triangle.branch", color: MultiPalette.accent, size: 11.5),
            label("策略组", size: 11, weight: .medium, color: MultiPalette.muted),
            groupPopup
        ], spacing: 5)
        groupSelectorRow.translatesAutoresizingMaskIntoConstraints = false
        groupSelectorPill.addSubview(groupSelectorRow)
        NSLayoutConstraint.activate([
            groupSelectorRow.leadingAnchor.constraint(equalTo: groupSelectorPill.leadingAnchor, constant: 11),
            groupSelectorRow.trailingAnchor.constraint(equalTo: groupSelectorPill.trailingAnchor, constant: -8),
            groupSelectorRow.centerYAnchor.constraint(equalTo: groupSelectorPill.centerYAnchor)
        ])

        autoSwitch.state = .on; autoSwitch.controlSize = .small
        autoSwitch.target = self; autoSwitch.action = #selector(configurationChanged(_:))
        autoSwitch.font = .systemFont(ofSize: 11.5, weight: .medium)

        let strategyRouteRow = row([
            groupSelectorPill,
            refreshGroupsButton,
            autoSwitch,
            spacer()
        ], spacing: 8)

        let matrixDiv = divider()

        // Configure parameter controls inside 32pt Clear Frosted Capsules
        parallelBox.addItems(withObjectValues: (1...10).map(String.init)); parallelBox.stringValue = "10"
        parallelBox.controlSize = .small
        parallelBox.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .bold)
        parallelBox.numberOfVisibleItems = 10; parallelBox.delegate = self; parallelBox.target = self; parallelBox.action = #selector(configurationChanged(_:))
        parallelBox.widthAnchor.constraint(equalToConstant: 44).isActive = true

        sizePopup.addItems(withTitles: sizeLabels); sizePopup.selectItem(at: 4)
        sizePopup.controlSize = .small; sizePopup.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold)
        sizePopup.widthAnchor.constraint(equalToConstant: 76).isActive = true

        modePopup.addItems(withTitles: ["快速 (2流)", "精确 (4流)"])
        modePopup.controlSize = .small; modePopup.font = .systemFont(ofSize: 11.5, weight: .semibold)
        modePopup.widthAnchor.constraint(equalToConstant: 104).isActive = true
        for popup in [sizePopup, modePopup] { popup.target = self; popup.action = #selector(configurationChanged(_:)) }

        intervalPopup.addItems(withTitles: ["15 分钟", "30 分钟", "1 小时", "2 小时", "6 小时", "12 小时", "24 小时"])
        intervalPopup.controlSize = .small; intervalPopup.font = .systemFont(ofSize: 11.5, weight: .semibold)
        intervalPopup.target = self; intervalPopup.action = #selector(configurationChanged(_:))
        intervalPopup.widthAnchor.constraint(equalToConstant: 70).isActive = true

        scheduleStatus.font = .systemFont(ofSize: 10.5, weight: .medium)
        scheduleStatus.textColor = MultiPalette.muted
        scheduleStatus.lineBreakMode = .byTruncatingTail
        scheduleStatus.setContentCompressionResistancePriority(.init(1), for: .horizontal)

        func wrapParamPill(_ titleText: String, control: NSView, trailing: NSView? = nil) -> NSView {
            let pill = MultiClearFrostedSurface(style: .clearSecondary, cornerRadius: 16, isCapsule: true)
            pill.translatesAutoresizingMaskIntoConstraints = false
            pill.heightAnchor.constraint(equalToConstant: 32).isActive = true
            var items: [NSView] = [
                label(titleText, size: 11, weight: .medium, color: MultiPalette.muted),
                control
            ]
            if let trailing = trailing { items.append(trailing) }
            let r = row(items, spacing: 5)
            r.translatesAutoresizingMaskIntoConstraints = false
            pill.addSubview(r)
            NSLayoutConstraint.activate([
                r.leadingAnchor.constraint(equalTo: pill.leadingAnchor, constant: 9),
                r.trailingAnchor.constraint(equalTo: pill.trailingAnchor, constant: -7),
                r.centerYAnchor.constraint(equalTo: pill.centerYAnchor)
            ])
            return pill
        }

        let pPill1 = wrapParamPill("并发", control: parallelBox)
        let pPill2 = wrapParamPill("包大小", control: sizePopup)
        let pPill3 = wrapParamPill("模式", control: modePopup)
        let pPill4 = wrapParamPill("定时", control: intervalPopup, trailing: scheduleButton)

        let paramStripRow = row([pPill1, pPill2, pPill3, pPill4, spacer(), scheduleStatus], spacing: 8)
        paramStripRow.setClippingResistancePriority(.defaultLow, for: .horizontal)

        let matrixStack = NSStackView(views: [matrixHeaderRow, subMidRow, strategyRouteRow, matrixDiv, paramStripRow])
        matrixStack.orientation = .vertical; matrixStack.alignment = .leading; matrixStack.spacing = 8
        matrixStack.translatesAutoresizingMaskIntoConstraints = false
        matrixCard.addSubview(matrixStack)
        NSLayoutConstraint.activate([
            matrixHeaderRow.widthAnchor.constraint(equalTo: matrixStack.widthAnchor),
            subMidRow.widthAnchor.constraint(equalTo: matrixStack.widthAnchor),
            strategyRouteRow.widthAnchor.constraint(equalTo: matrixStack.widthAnchor),
            matrixDiv.widthAnchor.constraint(equalTo: matrixStack.widthAnchor),
            paramStripRow.widthAnchor.constraint(equalTo: matrixStack.widthAnchor),
            matrixStack.leadingAnchor.constraint(equalTo: matrixCard.leadingAnchor, constant: 16),
            matrixStack.trailingAnchor.constraint(equalTo: matrixCard.trailingAnchor, constant: -16),
            matrixStack.topAnchor.constraint(equalTo: matrixCard.topAnchor, constant: 11),
            matrixStack.bottomAnchor.constraint(equalTo: matrixCard.bottomAnchor, constant: -11)
        ])

        topDeck.addSubview(champCard)
        topDeck.addSubview(matrixCard)
        NSLayoutConstraint.activate([
            champCard.leadingAnchor.constraint(equalTo: topDeck.leadingAnchor),
            champCard.topAnchor.constraint(equalTo: topDeck.topAnchor),
            champCard.bottomAnchor.constraint(equalTo: topDeck.bottomAnchor),
            champCard.widthAnchor.constraint(equalToConstant: 392),

            matrixCard.leadingAnchor.constraint(equalTo: champCard.trailingAnchor, constant: 14),
            matrixCard.trailingAnchor.constraint(equalTo: topDeck.trailingAnchor),
            matrixCard.topAnchor.constraint(equalTo: topDeck.topAnchor),
            matrixCard.bottomAnchor.constraint(equalTo: topDeck.bottomAnchor),
            topDeck.heightAnchor.constraint(equalToConstant: 184)
        ])

        // =========================================================================
        // 3. CENTER TELEMETRY TABLE CARD (Frosted Glass + Top-N Selection)
        // =========================================================================
        let tableCard = MultiClearFrostedSurface(style: .frostedCard, cornerRadius: 14, isCapsule: false)
        tableCard.translatesAutoresizingMaskIntoConstraints = false

        nodeCount.font = .monospacedDigitSystemFont(ofSize: 11, weight: .bold)
        nodeCount.textColor = MultiPalette.accent
        let countPill = MultiClearFrostedSurface(style: .clearPrimary, cornerRadius: 11, isCapsule: true)
        nodeCount.translatesAutoresizingMaskIntoConstraints = false
        countPill.addSubview(nodeCount)
        NSLayoutConstraint.activate([
            nodeCount.leadingAnchor.constraint(equalTo: countPill.leadingAnchor, constant: 10),
            nodeCount.trailingAnchor.constraint(equalTo: countPill.trailingAnchor, constant: -10),
            nodeCount.centerYAnchor.constraint(equalTo: countPill.centerYAnchor),
            countPill.heightAnchor.constraint(equalToConstant: 22)
        ])

        nodeSearch.placeholderString = "搜索订阅、节点或协议 (⌘F)"
        nodeSearch.target = self; nodeSearch.action = #selector(filterNodes(_:))
        nodeSearch.sendsSearchStringImmediately = true
        nodeSearch.controlSize = .small
        nodeSearch.font = .systemFont(ofSize: 11.5)
        nodeSearch.widthAnchor.constraint(lessThanOrEqualToConstant: 196).isActive = true
        nodeSearch.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        let preferredSearchWidth = nodeSearch.widthAnchor.constraint(equalToConstant: 196)
        preferredSearchWidth.priority = .defaultLow; preferredSearchWidth.isActive = true

        topNBox.addItems(withObjectValues: ["3", "5", "10", "15", "20", "30", "50"])
        topNBox.stringValue = "10"
        topNBox.controlSize = .small
        topNBox.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .bold)
        topNBox.numberOfVisibleItems = 7
        topNBox.widthAnchor.constraint(equalToConstant: 44).isActive = true

        let topNPill = MultiClearFrostedSurface(style: .clearSecondary, cornerRadius: 14, isCapsule: true)
        topNPill.translatesAutoresizingMaskIntoConstraints = false
        topNPill.heightAnchor.constraint(equalToConstant: 28).isActive = true
        let topNPillRow = row([
            label("前", size: 11, weight: .medium, color: MultiPalette.muted),
            topNBox,
            label("项", size: 11, weight: .medium, color: MultiPalette.muted)
        ], spacing: 4)
        topNPillRow.translatesAutoresizingMaskIntoConstraints = false
        topNPill.addSubview(topNPillRow)
        NSLayoutConstraint.activate([
            topNPillRow.leadingAnchor.constraint(equalTo: topNPill.leadingAnchor, constant: 7),
            topNPillRow.trailingAnchor.constraint(equalTo: topNPill.trailingAnchor, constant: -7),
            topNPillRow.centerYAnchor.constraint(equalTo: topNPill.centerYAnchor)
        ])

        let tableToolbar = row([
            label("节点实时遥测队列", size: 13, weight: .semibold),
            countPill,
            filterAllButton,
            filterValidButton,
            filterTestingButton,
            filterFailedButton,
            spacer(),
            nodeSearch,
            topNPill,
            selectTopNButton,
            selectShownButton,
            clearShownButton
        ], spacing: 6)
        tableToolbar.translatesAutoresizingMaskIntoConstraints = false
        tableToolbar.setClippingResistancePriority(.defaultLow, for: .horizontal)

        let tableHairline = divider()
        tableHairline.translatesAutoresizingMaskIntoConstraints = false

        resultTable.delegate = self; resultTable.dataSource = self
        resultTable.target = self; resultTable.doubleAction = #selector(doubleClickResultRow(_:))
        let rowMenu = NSMenu(title: "节点操作")
        let menuSwitch = NSMenuItem(title: "立即切换至此节点", action: #selector(contextSwitchRow(_:)), keyEquivalent: "")
        menuSwitch.target = self; rowMenu.addItem(menuSwitch)
        let menuRetest = NSMenuItem(title: "单独 20MiB 加压复测此节点", action: #selector(contextRetestRow(_:)), keyEquivalent: "")
        menuRetest.target = self; rowMenu.addItem(menuRetest)
        rowMenu.addItem(NSMenuItem.separator())
        let menuCopyName = NSMenuItem(title: "复制节点名称", action: #selector(contextCopyNodeName(_:)), keyEquivalent: "")
        menuCopyName.target = self; rowMenu.addItem(menuCopyName)
        resultTable.menu = rowMenu
        resultTable.usesAlternatingRowBackgroundColors = false
        resultTable.backgroundColor = .clear
        resultTable.rowHeight = 32
        resultTable.intercellSpacing = NSSize(width: 6, height: 2)
        resultTable.allowsColumnReordering = false
        if #available(macOS 11.0, *) { resultTable.style = .fullWidth }
        resultTable.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        addColumn(resultTable, "check", "测速", 36, minWidth: 32, maxWidth: 42, align: .center)
        addColumn(resultTable, "rank", "#", 34, minWidth: 30, maxWidth: 44, align: .center)
        addColumn(resultTable, "profile", "订阅", 116, minWidth: 84)
        addColumn(resultTable, "node", "节点名称", 210, minWidth: 140)
        addColumn(resultTable, "type", "协议", 66, minWidth: 54, maxWidth: 80, align: .center)
        addColumn(resultTable, "speed", "验证吞吐", 96, minWidth: 76, align: .right)
        addColumn(resultTable, "latency", "延迟", 68, minWidth: 54, align: .right)
        addColumn(resultTable, "failure", "丢包率", 58, minWidth: 48, align: .right)
        addColumn(resultTable, "jitter", "抖动", 64, minWidth: 54, align: .right)
        addColumn(resultTable, "score", "综合评分", 98, minWidth: 84, align: .right)
        let resultsScroll = scroller(resultTable, height: 260, drawsBackground: false)
        resultsScroll.translatesAutoresizingMaskIntoConstraints = false

        emptyStateLabel.font = .systemFont(ofSize: 14, weight: .semibold)
        emptyStateLabel.textColor = MultiPalette.text
        emptyStateLabel.alignment = .center
        emptyStateLabel.maximumNumberOfLines = 2
        emptyStateLabel.lineBreakMode = .byWordWrapping
        emptyStateLabel.translatesAutoresizingMaskIntoConstraints = false

        tableCard.addSubview(tableToolbar)
        tableCard.addSubview(tableHairline)
        tableCard.addSubview(resultsScroll)
        tableCard.addSubview(emptyStateLabel)
        NSLayoutConstraint.activate([
            tableToolbar.leadingAnchor.constraint(equalTo: tableCard.leadingAnchor, constant: 16),
            tableToolbar.trailingAnchor.constraint(equalTo: tableCard.trailingAnchor, constant: -16),
            tableToolbar.topAnchor.constraint(equalTo: tableCard.topAnchor, constant: 10),
            tableToolbar.heightAnchor.constraint(equalToConstant: 32),

            tableHairline.leadingAnchor.constraint(equalTo: tableCard.leadingAnchor, constant: 2),
            tableHairline.trailingAnchor.constraint(equalTo: tableCard.trailingAnchor, constant: -2),
            tableHairline.topAnchor.constraint(equalTo: tableToolbar.bottomAnchor, constant: 8),

            resultsScroll.leadingAnchor.constraint(equalTo: tableCard.leadingAnchor, constant: 6),
            resultsScroll.trailingAnchor.constraint(equalTo: tableCard.trailingAnchor, constant: -6),
            resultsScroll.topAnchor.constraint(equalTo: tableHairline.bottomAnchor, constant: 2),
            resultsScroll.bottomAnchor.constraint(equalTo: tableCard.bottomAnchor, constant: -6),
            emptyStateLabel.centerXAnchor.constraint(equalTo: resultsScroll.centerXAnchor),
            emptyStateLabel.centerYAnchor.constraint(equalTo: resultsScroll.centerYAnchor),
            emptyStateLabel.leadingAnchor.constraint(greaterThanOrEqualTo: resultsScroll.leadingAnchor, constant: 24),
            emptyStateLabel.trailingAnchor.constraint(lessThanOrEqualTo: resultsScroll.trailingAnchor, constant: -24)
        ])

        // =========================================================================
        // 4. ORIGINAL BOTTOM STATUS AND ACTION BAR (Height: 54 pt)
        // =========================================================================
        let bottomDock = bottomDockSurface
        bottomDock.translatesAutoresizingMaskIntoConstraints = false

        status.font = .systemFont(ofSize: 11.5, weight: .medium)
        status.textColor = MultiPalette.text
        status.maximumNumberOfLines = 1
        status.lineBreakMode = .byTruncatingTail
        status.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)

        progressView.widthAnchor.constraint(equalToConstant: 150).isActive = true
        progressView.heightAnchor.constraint(equalToConstant: 16).isActive = true
        let bottomDockRow = row([
            symbolIcon("shield.lefthalf.filled", color: MultiPalette.success, size: 12),
            status,
            spacer(),
            progressView,
            undoSwitchButton,
            switchResultButton
        ], spacing: 8)
        bottomDockRow.translatesAutoresizingMaskIntoConstraints = false
        bottomDock.addSubview(bottomDockRow)
        NSLayoutConstraint.activate([
            bottomDockRow.leadingAnchor.constraint(equalTo: bottomDock.leadingAnchor, constant: 14),
            bottomDockRow.trailingAnchor.constraint(equalTo: bottomDock.trailingAnchor, constant: -12),
            bottomDockRow.centerYAnchor.constraint(equalTo: bottomDock.centerYAnchor),
            bottomDock.heightAnchor.constraint(equalToConstant: 54)
        ])

        for v in [topBar, topDeck, tableCard, bottomDock] {
            rootCanvas.addSubview(v)
        }
        NSLayoutConstraint.activate([
            topBar.leadingAnchor.constraint(equalTo: rootCanvas.leadingAnchor),
            topBar.trailingAnchor.constraint(equalTo: rootCanvas.trailingAnchor),
            topBar.topAnchor.constraint(equalTo: rootCanvas.topAnchor),

            topDeck.leadingAnchor.constraint(equalTo: rootCanvas.leadingAnchor, constant: 24),
            topDeck.trailingAnchor.constraint(equalTo: rootCanvas.trailingAnchor, constant: -24),
            topDeck.topAnchor.constraint(equalTo: topBar.bottomAnchor, constant: 4),

            tableCard.leadingAnchor.constraint(equalTo: rootCanvas.leadingAnchor, constant: 24),
            tableCard.trailingAnchor.constraint(equalTo: rootCanvas.trailingAnchor, constant: -24),
            tableCard.topAnchor.constraint(equalTo: topDeck.bottomAnchor, constant: 12),
            tableCard.bottomAnchor.constraint(equalTo: bottomDock.topAnchor, constant: -12),

            bottomDock.leadingAnchor.constraint(equalTo: rootCanvas.leadingAnchor, constant: 24),
            bottomDock.trailingAnchor.constraint(equalTo: rootCanvas.trailingAnchor, constant: -24),
            bottomDock.bottomAnchor.constraint(equalTo: rootCanvas.bottomAnchor, constant: -14)
        ])
        updateControls()
        updateCompactLayout()
    }
    private func cancelLaunchChecks() {
        launchCheckAuthorization?.revoke(); launchCheckAuthorization = nil
        launchChecks.cancelAll()
    }
    private func disarm(_ reason: String? = nil) {
        let wasArmed = schedule.isArmed
        schedule.disarm(); armedBatch = nil; armedSwitch = nil; switchAuthorization?.revoke()
        if runScheduled {
            cancelLaunchChecks()
            // A passed check may already be queued at the runner or worker.
            // Revoke it now; an OS spawn past its final guard is still non-atomic.
            runner?.cancel()
        }
        scheduleButton.title = "启用定时"; scheduleStatus.stringValue = "定时未启用（仅应用打开时运行；每次消耗上方流量）"
        if wasArmed, let reason = reason { status.stringValue = reason }
    }
    @objc private func configurationChanged(_ sender: Any?) {
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
        disarm("选项已变化，定时已停止。")
        updateControls()
    }
    func controlTextDidChange(_ obj: Notification) { configurationChanged(nil) }
    func comboBoxSelectionDidChange(_ notification: Notification) { configurationChanged(nil) }
    @objc private func refresh(_ sender: Any?) { refresh(preservingPending: false) }
    private func refresh(preservingPending: Bool) {
        guard !busy, !closed else { return }
        if !preservingPending {
            pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
        }
        nodeSelection = nil; selectionPreview = nil
        rows = []; visibleRows = []; resultTable.deselectAll(nil); resultTable.reloadData(); invalidateGroups()
        disarm("订阅已刷新，定时已停止。")
        busy = true; updateControls(); status.stringValue = "正在读取订阅列表…"
        let root = self.root
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = try? ProfileRegistry.options(root: root)
            DispatchQueue.main.async { [weak self] in
                guard let self = self, !self.closed else { return }
                self.busy = false
                if let result = result {
                    self.registry = result; self.selected = self.selected.intersection(Set(result.map(\.id)))
                    self.status.stringValue = result.isEmpty ? "没有可用的本地订阅。" : "请选择订阅。测试前会重新验证内容。"
                } else {
                    self.registry = []; self.selected = []; self.status.stringValue = "无法安全读取订阅列表，请检查后刷新。"
                }
                self.profileTable.reloadData(); self.updateControls()
                if !self.selected.isEmpty { self.loadNodePreview() }
            }
        }
    }
    @objc private func selectAllProfiles(_ sender: Any?) {
        guard !busy, !registry.isEmpty else { return }
        let all = Set(registry.map(\.id))
        guard selected != all else { return }
        selected = all
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false; nodeSelection = nil; selectionPreview = nil
        rows = []; visibleRows = []; resultTable.deselectAll(nil); resultTable.reloadData(); invalidateGroups()
        disarm("订阅选择已变化，定时已停止。")
        updateControls(); if !selected.isEmpty { loadNodePreview() }
    }
    @objc private func clearAllProfiles(_ sender: Any?) {
        guard !busy, !selected.isEmpty else { return }
        selected.removeAll()
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false; nodeSelection = nil; selectionPreview = nil
        rows = []; visibleRows = []; resultTable.deselectAll(nil); resultTable.reloadData(); invalidateGroups()
        disarm("订阅选择已变化，定时已停止。")
        status.stringValue = "已清空订阅选择。"
        updateControls()
    }
    @objc private func toggleProfile(_ sender: NSButton) {
        guard !busy, registry.indices.contains(sender.tag) else { return }
        let id = registry[sender.tag].id
        if sender.state == .on { selected.insert(id) } else { selected.remove(id) }
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false; nodeSelection = nil; selectionPreview = nil
        rows = []; visibleRows = []; resultTable.deselectAll(nil); resultTable.reloadData(); invalidateGroups()
        disarm("订阅选择已变化，定时已停止。")
        updateControls(); if !selected.isEmpty { loadNodePreview() }
    }
    @objc private func toggleProfilePill(_ sender: NSButton) {
        guard !busy, registry.indices.contains(sender.tag) else { return }
        let id = registry[sender.tag].id
        if selected.contains(id) { selected.remove(id) } else { selected.insert(id) }
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false; nodeSelection = nil; selectionPreview = nil
        rows = []; visibleRows = []; resultTable.deselectAll(nil); resultTable.reloadData(); invalidateGroups()
        disarm("订阅选择已变化，定时已停止。")
        updateControls(); if !selected.isEmpty { loadNodePreview() }
    }
    private func rebuildSubscriptionPills() {
        for view in subPillsStack.arrangedSubviews {
            subPillsStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        if registry.isEmpty {
            let emptyLbl = label("未检测到订阅源", size: 11.5, weight: .medium, color: MultiPalette.muted)
            subPillsStack.addArrangedSubview(emptyLbl)
            return
        }
        for (idx, option) in registry.prefix(4).enumerated() {
            let isChecked = selected.contains(option.id)
            let suffix = option.isCurrent ? " · 当前" : ""
            let prefix = isChecked ? "✓ " : ""
            let pill = MultiClearGlassButton(
                title: "\(prefix)\(option.name)\(suffix)",
                style: isChecked ? .clearPrimary : .clearSecondary,
                height: 28,
                hPadding: 12,
                fontSize: 11.5
            )
            pill.leadingDotColor = option.isCurrent ? MultiPalette.success : (isChecked ? MultiPalette.accent : MultiPalette.faint)
            pill.tag = idx
            pill.toolTip = option.name + suffix
            pill.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            pill.widthAnchor.constraint(lessThanOrEqualToConstant: 180).isActive = true
            pill.widthAnchor.constraint(greaterThanOrEqualToConstant: 70).isActive = true
            pill.target = self
            pill.action = #selector(toggleProfilePill(_:))
            pill.isEnabled = !busy
            subPillsStack.addArrangedSubview(pill)
        }
    }
    private func pulseChampionFeedback() {
        feedbackTimer?.invalidate(); feedbackTimer = nil
        guard !MultiPalette.reducesMotion else { champCardView.crownPulseIntensity = 0; return }
        champCardView.crownPulseIntensity = 1
        let start = ProcessInfo.processInfo.systemUptime
        feedbackTimer = Timer.scheduledTimer(withTimeInterval: 0.025, repeats: true) { [weak self] timer in
            guard let self, !self.closed else { timer.invalidate(); return }
            let elapsed = (ProcessInfo.processInfo.systemUptime - start) / 0.3
            if MultiPalette.reducesMotion || elapsed >= 1 {
                self.champCardView.crownPulseIntensity = 0
                timer.invalidate(); self.feedbackTimer = nil
            } else {
                self.champCardView.crownPulseIntensity = CGFloat(1 - elapsed)
            }
        }
    }
    private func updateChampionCard() {
        // The table stays still during a run, so find the best measured row
        // independently of its current screen position.
        let bestScored = multiRank(rows).first { $0.eligibleScore != nil }
        let highlighted: MultiRow? = visibleRows.indices.contains(resultTable.selectedRow) ? rows[visibleRows[resultTable.selectedRow]] : nil
        let hero = bestScored ?? highlighted ?? rows.first
        let hasPendingCrossProfile = (pendingWinner != nil || pendingManual != nil)
        champCardView.isCrossProfileReady = hasPendingCrossProfile && crossProfileReadyDetected
        guard let item = hero else {
            targetChampScore = 0; targetChampSpeed = 0
            champScoreLabel.stringValue = "—"
            champNodeLabel.stringValue = selected.isEmpty ? "请先勾选订阅源" : "等待测速或选择节点"
            champSubLabel.stringValue = selected.isEmpty ? "支持多订阅并发基准测速与安全切换" : "已载入 \(rows.count) 个节点 · 点击右上角开始测速"
            champSpeedLabel.stringValue = "—"
            champMetaLabel.stringValue = "延迟 —  ·  抖动 —  ·  丢包 —"
            champReadyLabel.stringValue = busy ? "● 测速运行中" : "● 待命就绪"
            champQuickSwitchButton.isHidden = true; champRetestButton.isHidden = true
            champSparkline.isHidden = true
            return
        }
        let isCurrentProfile = registry.first(where: { $0.id == item.identity.profile })?.isCurrent ?? false
        champNodeLabel.stringValue = item.nodeName
        champQuickSwitchButton.isHidden = false; champRetestButton.isHidden = false
        champQuickSwitchButton.isEnabled = !busy && !groupRefreshInFlight && !switchOutcomeUnknown && selectionPreview != nil
        champRetestButton.isEnabled = !busy && !groupRefreshInFlight && selectionPreview != nil
        champSparkline.isHidden = item.measurement == nil
        if let m = item.measurement {
            let newScore = item.eligibleScore ?? m.score ?? 0
            let newSpeed = m.downloadBytesPerSecond / 1_048_576
            if abs(newSpeed - targetChampSpeed) > 0.005 || abs(newScore - targetChampScore) > 0.05 {
                targetChampScore = newScore; targetChampSpeed = newSpeed
                pulseChampionFeedback()
            }
            let scoreText = item.eligibleScore.map { String(format: "%.1f", $0) } ?? (m.score.map { String(format: "%.1f", $0) } ?? "—")
            champScoreLabel.stringValue = scoreText
            champSpeedLabel.stringValue = String(format: "%.2f", newSpeed)
            champSubLabel.stringValue = "订阅源：\(item.profileName)  ·  \(m.nodeType.uppercased())"
            let failures = m.headAttempts > 0 ? 100.0 * Double(m.headAttempts - m.headSuccesses) / Double(m.headAttempts) : 100
            champMetaLabel.stringValue = String(format: "延迟 %.1f ms  ·  抖动 %.1f ms  ·  丢包 %.1f%%",
                                                m.latencyMilliseconds, m.jitterMilliseconds, failures)
            let norm = max(0.15, min(0.96, CGFloat(newSpeed / 25)))
            champSparkline.points = [max(0.12, norm * 0.68), max(0.15, norm * 0.82), max(0.14, norm * 0.76),
                                     max(0.18, norm * 0.91), norm, max(0.16, norm * 0.95)]
        } else {
            targetChampScore = 0; targetChampSpeed = 0
            champScoreLabel.stringValue = "待测"
            champSubLabel.stringValue = "订阅源：\(item.profileName)  ·  状态：\(item.state)"
            champSpeedLabel.stringValue = "—"
            champMetaLabel.stringValue = "延迟 —  ·  抖动 —  ·  丢包 —"
        }
        if busy {
            champReadyLabel.stringValue = funnelStage == 1 ? "● 初筛竞速中" : (funnelStage == 2 ? "● 晋级复测中" : "● 正在基准测速")
        } else if isCurrentProfile {
            champReadyLabel.stringValue = "● 同订阅就绪"
        } else {
            champReadyLabel.stringValue = crossProfileReadyDetected ? "● 跨订阅已就绪" : "● 跨订阅需激活"
        }
    }
    private func updateDockAndProgressState() {
        let totalJobs = max(1, runIdentities.count)
        let doneJobs = rows.filter {
            runIdentities.contains($0.identity) && !["等待", "待测速", "未勾选"].contains($0.state)
        }.count
        let measuring = busy && currentBatch != nil && !runIdentities.isEmpty
        let hasProgress = measuring || doneJobs > 0
        let frac = hasProgress ? CGFloat(doneJobs) / CGFloat(totalJobs) : 0
        progressView.isHidden = !hasProgress
        progressView.setFraction(frac)
        progressView.toolTip = "已处理 \(doneJobs) / \(runIdentities.count) 个节点"
        if measuring {
            let pct = Int(round(frac * 100))
            let stageTag = funnelStage == 1 ? "初筛" : (funnelStage == 2 ? "决选" : "测速")
            startButton.title = "\(stageTag) · \(pct)%"
            bottomDockSurface.dockVisualState = .racing(progress: frac)
        } else {
            startButton.title = "开始测速"
            if pendingWinner != nil || pendingManual != nil {
                bottomDockSurface.dockVisualState = .crossProfilePending(ready: crossProfileReadyDetected)
            } else if let until = dockConfirmedUntil, Date() < until {
                bottomDockSurface.dockVisualState = .switchedConfirmed
            } else {
                bottomDockSurface.dockVisualState = .idle
            }
        }
        for (btn, f) in [(filterAllButton, MultiRowStatusFilter.all),
                         (filterValidButton, .validOnly),
                         (filterTestingButton, .testingOnly),
                         (filterFailedButton, .failedOnly)] {
            btn.glassStyle = (statusFilter == f) ? .clearPrimary : .clearSubtle
        }
        if let undo = lastUndoSwitchChoice {
            undoSwitchButton.isHidden = false
            let shortUndo = undo.node.count > 14 ? String(undo.node.prefix(14)) + "…" : undo.node
            undoSwitchButton.title = "切回 \(shortUndo)"
            undoSwitchButton.isEnabled = !busy && !groupRefreshInFlight && !switchOutcomeUnknown
        } else {
            undoSwitchButton.isHidden = true
            undoSwitchButton.isEnabled = false
        }
    }
    private func tickUIMotion() {
        guard !closed else { return }
        if schedule.isArmed, let nextFire = schedule.nextFire {
            let remaining = max(0, Int(ceil(nextFire.timeIntervalSince(TestSchedule.monotonicNow()))))
            let mins = remaining / 60
            let secs = remaining % 60
            let title = intervalPopup.titleOfSelectedItem ?? ""
            scheduleStatus.stringValue = String(format: "下次 %02d:%02d（每 %@）", mins, secs, title)
            scheduleButton.toolTip = scheduleStatus.stringValue
        }
        if !busy && !groupRefreshInFlight, (pendingWinner != nil || pendingManual != nil) {
            let targetUID = pendingWinner?.profile.option.id ?? pendingManual?.profile.option.id
            if let targetUID = targetUID {
                let currentUID = (try? ProfileRegistry.options(root: root))?.first(where: { $0.isCurrent })?.id
                let nowReady = (currentUID == targetUID)
                if nowReady != crossProfileReadyDetected {
                    crossProfileReadyDetected = nowReady
                    bottomDockSurface.dockVisualState = .crossProfilePending(ready: nowReady)
                    champCardView.isCrossProfileReady = nowReady
                }
            }
        } else if crossProfileReadyDetected {
            crossProfileReadyDetected = false
        }
        if let until = dockConfirmedUntil, Date() >= until {
            dockConfirmedUntil = nil
            updateDockAndProgressState()
        }
    }
    private func updateControls() {
        let count = nodeSelection?.count ?? 0
        let hasNodes = selectionPreview != nil && count > 0
        emptyStateLabel.stringValue = busy ? "正在读取订阅和节点…" :
            (selectionPreview == nil ? "请先勾选订阅源" : "当前筛选没有节点")
        emptyStateLabel.isHidden = !visibleRows.isEmpty
        let readyForAction = !busy && !groupRefreshInFlight
        selectAllProfilesButton.isEnabled = !busy && selected.count < registry.count
        clearProfilesButton.isEnabled = !busy && !selected.isEmpty
        refreshButton.isEnabled = !busy
        funnelButton.isEnabled = readyForAction && hasNodes
        loadTestButton.isEnabled = readyForAction && hasNodes
        switchResultButton.isEnabled = readyForAction && !switchOutcomeUnknown && selectionPreview != nil && visibleRows.indices.contains(resultTable.selectedRow)
        topNBox.isEnabled = !busy && !visibleRows.isEmpty
        selectTopNButton.isEnabled = !busy && !visibleRows.isEmpty
        selectShownButton.isEnabled = !busy && !visibleRows.isEmpty; clearShownButton.isEnabled = !busy && !visibleRows.isEmpty
        nodeSearch.isEnabled = !busy && selectionPreview != nil
        for btn in [filterAllButton, filterValidButton, filterTestingButton, filterFailedButton] {
            btn.isEnabled = selectionPreview != nil
        }
        groupPopup.isEnabled = readyForAction && groupContext != nil; refreshGroupsButton.isEnabled = readyForAction && selectionPreview != nil
        nodeCount.stringValue = "已勾选 \(count) / \(rows.count)"
        if let (options, _) = chosenOptions() {
            let total = Double(count) * Double(options.downloadBytes) / 1_048_576
            trafficEstimate.stringValue = "预计最大下载：\(count) 节点 ≈ \(String(format: "%.2f", total)) MiB（不含 HEAD 请求）"
        }
        if busy { profileTable.deselectAll(nil) }
        if busy != displayedBusyState {
            displayedBusyState = busy
            resultTable.reloadData()
        }
        parallelBox.isEnabled = !busy; sizePopup.isEnabled = !busy; modePopup.isEnabled = !busy
        intervalPopup.isEnabled = !busy
        scheduleButton.isEnabled = schedule.isArmed || (readyForAction && hasNodes)
        startButton.isEnabled = readyForAction && hasNodes; stopButton.isEnabled = busy && !cancelled
        autoSwitch.isEnabled = !busy && !switchOutcomeUnknown
        copyButton.isEnabled = !rows.isEmpty
        scheduleStatus.textColor = schedule.isArmed ? MultiPalette.accent : MultiPalette.muted
        updateDockAndProgressState()
        rebuildSubscriptionPills()
        updateChampionCard()
        profileTable.reloadData()
    }
    private func chosenOptions() -> (MultiBatchOptions, Int)? {
        guard let parallel = Int(parallelBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1...10).contains(parallel), sizeValues.indices.contains(sizePopup.indexOfSelectedItem) else { return nil }
        let quick = modePopup.indexOfSelectedItem == 0
        return (MultiBatchOptions(downloadBytes: sizeValues[sizePopup.indexOfSelectedItem], streams: quick ? 2 : 4,
                                  latencyTimeout: quick ? "1s" : "2s", downloadTimeout: "3s"), parallel)
    }
    private func describe(_ batch: MultiPreparedBatch) -> String? {
        let profiles = batch.profiles.map { "• \($0.option.name)（UID：\($0.option.uid)，\($0.names.count) 个节点）" }.joined(separator: "\n")
        guard profiles.utf8.count <= 12_000 else { return nil }
        let perNode = sizeLabels[sizeValues.firstIndex(of: batch.options.downloadBytes)!]
        let totalMiB = Double(batch.requestedBytes) / 1_048_576
        return "订阅：\n\(profiles)\n共 \(batch.jobCount) 个节点，每节点 \(perNode)，目标总下载约 \(String(format: "%.2f", totalMiB)) MiB；另有 HEAD 请求。此估算不是流量硬上限。\n全局并行上限 \(batch.parallel)，每节点 \(batch.options.streams) 条下载流。"
    }
    private func showWarning(_ title: String, _ message: String) {
        let alert = NSAlert(); alert.alertStyle = .warning; alert.messageText = title; alert.informativeText = message; alert.runModal()
    }
    private func prepare(completion: @escaping (MultiPreparedBatch?) -> Void) {
        guard let (options, parallel) = chosenOptions() else {
            status.stringValue = "并行数无效，请输入 1 到 10 的整数。"; completion(nil); return
        }
        guard let nodeSelection = self.nodeSelection, !nodeSelection.isEmpty, let preview = self.selectionPreview else {
            status.stringValue = "请先在测速列表中勾选至少一个节点。"; completion(nil); return
        }
        let root = self.root, registry = self.registry, selected = self.selected
        busy = true; updateControls(); status.stringValue = "正在本地检查订阅快照…"
        DispatchQueue.global(qos: .userInitiated).async {
            let prepared = try? multiPrepare(root: root, registry: registry, selected: selected, options: options,
                                             parallel: parallel, nodeSelection: nodeSelection)
            let batch: MultiPreparedBatch?
            if let prepared = prepared, multiSameSnapshots(preview, prepared) {
                batch = prepared
            } else { batch = nil }
            DispatchQueue.main.async { completion(batch) }
        }
    }
    private func loadNodePreview() {
        guard !busy, !closed, !selected.isEmpty else { return }
        cancelled = false; busy = true; updateControls(); status.stringValue = "正在本地读取节点列表，不会测速…"
        let root = self.root, registry = self.registry, selected = self.selected
        let previewOptions = MultiBatchOptions(downloadBytes: 256 * 1024, streams: 2,
                                               latencyTimeout: "1s", downloadTimeout: "3s")
        DispatchQueue.global(qos: .userInitiated).async {
            let batch = try? multiPrepare(root: root, registry: registry, selected: selected,
                                          options: previewOptions, parallel: 1, previewOnly: true)
            DispatchQueue.main.async {
                guard !self.closed else { return }
                self.busy = false
                guard !self.cancelled, self.selected == selected, let batch = batch else {
                    self.rows = []; self.visibleRows = []; self.nodeSelection = nil; self.selectionPreview = nil
                    self.resultTable.reloadData(); self.updateControls()
                    self.status.stringValue = "节点列表不可用或操作已停止；未测速。"; return
                }
                self.selectionPreview = batch; self.rows = multiPreviewRows(batch)
                self.nodeSelection = Set(self.rows.map(\.identity)); self.reloadVisibleRows()
                self.status.stringValue = "已载入 \(self.rows.count) 个节点，默认全勾选；可直接在列表中修改。"
                self.updateControls(); self.refreshGroups(nil)
            }
        }
    }

    private func highlightedIdentity() -> MultiIdentity? {
        guard visibleRows.indices.contains(resultTable.selectedRow) else { return nil }
        return rows[visibleRows[resultTable.selectedRow]].identity
    }
    private func reloadVisibleRows(preserving desired: MultiIdentity? = nil) {
        let highlighted = desired ?? highlightedIdentity()
        let activeTesting: Set<MultiIdentity> = busy ? Set(rows.filter { runIdentities.contains($0.identity) && $0.state == "等待" }.map(\.identity)) : []
        visibleRows = multiVisibleRows(rows, query: nodeSearch.stringValue, statusFilter: statusFilter,
                                       activeIdentities: activeTesting, sortColumn: busy ? nil : sortColumnIdentifier,
                                       sortAscending: sortColumnAscending)
        resultTable.reloadData()
        if let highlighted = highlighted, let row = visibleRows.firstIndex(where: { rows[$0].identity == highlighted }) {
            resultTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        } else { resultTable.deselectAll(nil) }
        nodeCount.stringValue = "已勾选 \(nodeSelection?.count ?? 0) / \(rows.count)"
        updateChampionCard()
    }
    @objc private func statusFilterChanged(_ sender: NSButton) {
        guard let filter = MultiRowStatusFilter(rawValue: sender.tag) else { return }
        statusFilter = filter
        reloadVisibleRows()
        updateControls()
    }
    func tableView(_ tableView: NSTableView, didClick tableColumn: NSTableColumn) {
        guard tableView === resultTable, !busy else { return }
        let id = tableColumn.identifier.rawValue
        guard ["rank", "profile", "node", "type", "speed", "latency", "failure", "jitter", "score"].contains(id) else { return }
        if sortColumnIdentifier == id {
            sortColumnAscending.toggle()
        } else {
            sortColumnIdentifier = id
            sortColumnAscending = ["rank", "profile", "node", "type", "latency", "failure", "jitter"].contains(id)
        }
        reloadVisibleRows()
    }
    @objc private func filterNodes(_ sender: Any?) {
        guard !busy else { return }; reloadVisibleRows(); updateControls()
    }
    private func changeNodeSelection(_ identities: [MultiIdentity], checked: Bool) {
        guard !busy, var selection = nodeSelection else { return }
        for identity in identities {
            if checked { selection.insert(identity) } else { selection.remove(identity) }
        }
        guard selection != nodeSelection else { return }
        nodeSelection = selection; pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
        disarm("节点勾选已变化，定时已停止。")
        reloadVisibleRows(); updateControls()
    }
    @objc private func toggleNode(_ sender: MultiNodeCheckbox) {
        guard let identity = sender.nodeIdentity, rows.contains(where: { $0.identity == identity }) else { return }
        changeNodeSelection([identity], checked: sender.state == .on)
    }
    @objc private func selectTopNNodes(_ sender: Any?) {
        guard !busy, selectionPreview != nil, !visibleRows.isEmpty else { return }
        guard let count = Int(topNBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)),
              let updated = multiSelectTopN(rows: rows, visibleRows: visibleRows, count: count) else {
            status.stringValue = "前 N 项数量无效，请输入 1 到 2000 的整数。"
            return
        }
        if updated != nodeSelection {
            nodeSelection = updated; pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
            disarm("节点勾选已变化，定时已停止。")
        }
        cascadeHighlightedIdentities = updated
        reloadVisibleRows(); updateControls()
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(650)) { [weak self] in
            guard let self = self, !self.closed else { return }
            self.cascadeHighlightedIdentities.removeAll()
            self.resultTable.reloadData()
        }
        status.stringValue = "已勾选当前列表前 \(updated.count) 个节点。"
    }
    @objc private func selectShownNodes(_ sender: Any?) {
        changeNodeSelection(visibleRows.map { rows[$0].identity }, checked: true)
    }
    @objc private func clearShownNodes(_ sender: Any?) {
        changeNodeSelection(visibleRows.map { rows[$0].identity }, checked: false)
    }
    private func invalidateGroups() {
        groupGeneration &+= 1; groupRefreshInFlight = false; groupContext = nil
        groupPopup.removeAllItems(); groupPopup.addItem(withTitle: "请选择当前订阅策略组")
    }
    @objc private func groupChanged(_ sender: Any?) {
        guard !busy else { return }
        disarm("策略组已变化，定时已停止。")
        if let capture = selectedGroupCapture() {
            status.stringValue = "策略组：\(capture.groupName)；当前：\(capture.now)。点击切换即执行。"
        }
        updateControls()
#if !MULTI_UI_TEST
        continuePendingSwitchIfNeeded()
#endif
    }
    private func selectedGroupCapture() -> MultiSwitchCapture? {
        guard let context = groupContext, groupPopup.indexOfSelectedItem > 0 else { return nil }
        let index = groupPopup.indexOfSelectedItem - 1
        guard context.groups.indices.contains(index) else { return nil }
        let group = context.groups[index]
        return MultiSwitchCapture(controller: context.controller, profile: context.profile, groupName: group.name, now: group.now)
    }
    @objc private func refreshGroups(_ sender: Any?) {
        guard !busy, !closed, let preview = selectionPreview else { return }
        // Only a post-switch refresh may retain an existing recurring grant.
        let postSwitch = (sender as? Bool) == true
        if groupRefreshInFlight { return }
        let previous = selectedGroupCapture()
        let previousStatus = postSwitch ? status.stringValue : nil
        if schedule.isArmed && !postSwitch { disarm("策略组已刷新，定时已停止。") }
        func report(_ message: String) {
            if let previousStatus = previousStatus {
                if self.status.stringValue == previousStatus { self.status.stringValue = previousStatus + " " + message }
            } else { self.status.stringValue = message }
        }
        invalidateGroups()
        groupRefreshInFlight = true
        let generation = groupGeneration, root = self.root
        updateControls()
        // Every failure, including a timeout, invalidates this generation. Never
        // leave a recurring authorization active with an unknown displayed group.
        func fail(_ message: String) {
            guard !self.closed, self.groupRefreshInFlight, self.groupGeneration == generation else { return }
            self.groupRefreshInFlight = false
            self.invalidateGroups()
            if postSwitch && self.schedule.isArmed {
                self.disarm()
                self.status.stringValue = message + " 为安全起见定时已停止，请刷新策略组后重新启用。"
            } else { report(message) }
            self.updateControls()
        }
        if postSwitch && schedule.isArmed && armedSwitch == nil {
            fail("定时策略组授权无效。")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(15)) {
            fail("策略组刷新超时。")
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let active = (try? ProfileRegistry.options(root: root))?.first(where: { $0.isCurrent })
            let profile = active.flatMap { current in
                preview.profiles.first { $0.option.id == current.id && multiBytesEqual($0.option.path, current.path) }
            }
            let snapshotMatches = profile.map { multiProfileSnapshotMatches(root: root, profile: $0) } ?? false
            let identity = snapshotMatches ? multiWaitForLocalController(root: root) : nil
            DispatchQueue.main.async {
                guard !self.closed, !self.busy, self.groupRefreshInFlight, self.groupGeneration == generation else { return }
                guard active != nil else {
                    fail("无法读取 Clash Verge 当前订阅；请刷新配置。")
                    return
                }
                guard let profile = profile else {
                    self.groupRefreshInFlight = false
                    self.invalidateGroups()
                    self.status.stringValue = "当前订阅未勾选；跨订阅切换时会自动激活目标订阅。"
                    self.updateControls()
                    return
                }
                guard snapshotMatches else {
                    self.groupRefreshInFlight = false
                    self.invalidateGroups()
                    self.status.stringValue = "当前订阅内容已更新，正在重新读取节点…"
                    self.loadNodePreview()
                    return
                }
                guard let identity = identity else {
                    fail("无法连接 Mihomo 控制器；请确认 Clash Verge 正在运行。")
                    return
                }
                let controller = ClashController(settings: ControllerSettings(socket: identity.path, secret: ""))
                controller.eligibleGroupsPreservingNodeBytes(nodeNames: profile.names) { result in
                    guard !self.closed, !self.busy, self.groupRefreshInFlight, self.groupGeneration == generation else { return }
                    guard case .success(let groups) = result else { fail("读取策略组失败。"); return }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let current = (try? ProfileRegistry.options(root: root))?.first(where: { $0.isCurrent })
                        let valid = current.map { $0.id == profile.option.id && multiBytesEqual($0.path, profile.option.path) } == true &&
                            multiProfileSnapshotMatches(root: root, profile: profile) && multiLocalController(root: root) == identity
                        DispatchQueue.main.async {
                            guard !self.closed, !self.busy, self.groupRefreshInFlight, self.groupGeneration == generation else { return }
                            guard valid else { fail("订阅或控制器已变化，请刷新策略组。"); return }
                            let candidates = multiGroupCandidates(groups, testedNames: profile.names)
                            let required = postSwitch && self.schedule.isArmed ? self.armedSwitch : nil
                            let choice = multiGroupRefreshChoice(previous: previous, recurring: required,
                                controller: identity, profile: profile, candidates: candidates)
                            if choice == .invalidateRecurringAuthorization {
                                fail("已授权的订阅、控制器或策略组已变化。")
                                return
                            }
                            self.groupContext = MultiGroupContext(controller: identity, profile: profile, groups: candidates)
                            for group in candidates { self.groupPopup.addItem(withTitle: "\(group.name)  [当前: \(group.now)]") }
                            if case .select(let index) = choice { self.groupPopup.selectItem(at: index + 1) }
                            self.groupRefreshInFlight = false
                            report(candidates.isEmpty ? "当前订阅没有可用的直接成员 Selector。" :
                                "策略组已刷新，对应当前订阅 ‘\(profile.option.name)’；多组时请先选择。")
                            self.updateControls()
#if !MULTI_UI_TEST
                            self.continuePendingSwitchIfNeeded()
#endif
                        }
                    }
                }
            }
        }
    }
    func applicationDidBecomeActive(_ notification: Notification) {
        // Merely returning focus must not silently disable a recurring test.
        if window != nil && !busy && !schedule.isArmed && selectionPreview != nil { refreshGroups(nil) }
    }

    @objc private func startLoadTest(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, !selected.isEmpty, let sizeIndex = sizeValues.firstIndex(of: 20 * 1_048_576) else { return }
        // A preset change must never preserve authorization for a recurring run.
        disarm(); pendingWinner = nil; pendingManual = nil; funnelStage = 0
        parallelBox.stringValue = "2"; modePopup.selectItem(at: 1); sizePopup.selectItem(at: sizeIndex)
        updateControls()
        // Reuse the ordinary one-click flow; the visible preset is the user's action.
        startManual(sender)
    }

    @objc private func startFunnelTest(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, !selected.isEmpty,
              let quickIndex = sizeValues.firstIndex(of: 1 * 1_048_576) else { return }
        disarm(); pendingWinner = nil; pendingManual = nil
        parallelBox.stringValue = "10"; modePopup.selectItem(at: 0); sizePopup.selectItem(at: quickIndex)
        updateControls()
        funnelStage = 1
        startManual(funnelButton)
    }

    private func heroOrHighlightedRow() -> MultiRow? {
        let bestScored = rows.first { $0.eligibleScore != nil }
        let highlighted: MultiRow? = visibleRows.indices.contains(resultTable.selectedRow) ? rows[visibleRows[resultTable.selectedRow]] : nil
        return bestScored ?? highlighted ?? rows.first
    }

    @objc private func switchChampionRoute(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown,
              let batch = selectionPreview, let hero = heroOrHighlightedRow(),
              let profile = batch.profiles.first(where: { $0.option.id == hero.identity.profile }),
              hero.identity == MultiIdentity(profile: profile.option.id, node: hero.nodeName),
              profile.names.contains(where: { multiBytesEqual($0, hero.nodeName) }) else { return }
        manualSwitchDisarmedSchedule = false
        beginManualSwitch(MultiPickerChoice(profile: profile, node: hero.nodeName))
    }

    @objc private func retestChampionRoute(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, selectionPreview != nil,
              let hero = heroOrHighlightedRow(),
              let sizeIndex = sizeValues.firstIndex(of: 20 * 1_048_576) else { return }
        disarm(); pendingWinner = nil; pendingManual = nil; funnelStage = 0
        nodeSelection = [hero.identity]
        parallelBox.stringValue = "1"; modePopup.selectItem(at: 1); sizePopup.selectItem(at: sizeIndex)
        reloadVisibleRows(preserving: hero.identity); updateControls()
        startManual(sender)
    }

    @objc private func doubleClickResultRow(_ sender: Any?) {
        let clicked = resultTable.clickedRow
        guard clicked >= 0, visibleRows.indices.contains(clicked) else { return }
        resultTable.selectRowIndexes(IndexSet(integer: clicked), byExtendingSelection: false)
        switchSelectedResult(sender)
    }

    @objc private func contextSwitchRow(_ sender: Any?) {
        let targetRow = resultTable.clickedRow >= 0 ? resultTable.clickedRow : resultTable.selectedRow
        guard targetRow >= 0, visibleRows.indices.contains(targetRow) else { return }
        resultTable.selectRowIndexes(IndexSet(integer: targetRow), byExtendingSelection: false)
        switchSelectedResult(sender)
    }

    @objc private func contextRetestRow(_ sender: Any?) {
        let targetRow = resultTable.clickedRow >= 0 ? resultTable.clickedRow : resultTable.selectedRow
        guard !busy, !groupRefreshInFlight, !closed, selectionPreview != nil,
              targetRow >= 0, visibleRows.indices.contains(targetRow),
              let sizeIndex = sizeValues.firstIndex(of: 20 * 1_048_576) else { return }
        let row = rows[visibleRows[targetRow]]
        disarm(); pendingWinner = nil; pendingManual = nil; funnelStage = 0
        nodeSelection = [row.identity]
        parallelBox.stringValue = "1"; modePopup.selectItem(at: 1); sizePopup.selectItem(at: sizeIndex)
        reloadVisibleRows(preserving: row.identity); updateControls()
        startManual(sender)
    }

    @objc private func contextCopyNodeName(_ sender: Any?) {
        let targetRow = resultTable.clickedRow >= 0 ? resultTable.clickedRow : resultTable.selectedRow
        guard targetRow >= 0, visibleRows.indices.contains(targetRow) else { return }
        let row = rows[visibleRows[targetRow]]
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(row.nodeName, forType: .string)
        status.stringValue = "已复制节点名称：\(row.nodeName)"
    }

    @objc private func undoLastSwitch(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown,
              let undoChoice = lastUndoSwitchChoice else { return }
        manualSwitchDisarmedSchedule = false
        beginManualSwitch(undoChoice)
    }

    @objc private func switchSelectedResult(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown, let batch = selectionPreview,
              visibleRows.indices.contains(resultTable.selectedRow) else { return }
        let row = rows[visibleRows[resultTable.selectedRow]]
        guard let profile = batch.profiles.first(where: { $0.option.id == row.identity.profile }),
              row.identity == MultiIdentity(profile: profile.option.id, node: row.nodeName),
              profile.names.contains(where: { multiBytesEqual($0, row.nodeName) }) else { return }
        manualSwitchDisarmedSchedule = false
        beginManualSwitch(MultiPickerChoice(profile: profile, node: row.nodeName))
    }

    private func allowed(_ token: TestSchedule.RunToken?, scheduled: Bool) -> Bool {
        multiRunPermitted(closed: closed, cancelled: cancelled, tokenMatches: token == nil || activeToken == token,
                          scheduled: scheduled, armed: schedule.isArmed)
    }

    // Freshly discover the unique same-user socket and revalidate profile bytes
    // on both sides of the controller GET. For a recurring run, authorization
    // fixes the profile, socket and group but *not* the old `now`: it is fetched
    // afresh for each run. No modal is shown on timer ticks.
    private func preflight(_ batch: MultiPreparedBatch, authorized: MultiSwitchCapture?,
                           token: TestSchedule.RunToken?, scheduled: Bool,
                           completion: @escaping (MultiSwitchCapture?) -> Void) {
        guard allowed(token, scheduled: scheduled), !switchOutcomeUnknown, let prior = authorized,
              let active = batch.registry.first(where: { $0.isCurrent }),
              let profile = batch.profiles.first(where: { $0.option.id == active.id }) else { completion(nil); return }
        let root = self.root
        status.stringValue = "正在确认当前订阅和本地 Selector…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let valid = (try? multiRevalidate(root: root, batch: batch)) != nil
            let identity = valid ? multiWaitForLocalController(root: root) : nil
            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.allowed(token, scheduled: scheduled), let identity = identity else { completion(nil); return }
                guard identity == prior.controller, profile.option.id == prior.profile.option.id,
                      multiBytesEqual(profile.option.path, prior.profile.option.path), profile.data == prior.profile.data,
                      profile.version == prior.profile.version else { completion(nil); return }
                let controller = ClashController(settings: ControllerSettings(socket: identity.path, secret: ""))
                controller.eligibleGroupsPreservingNodeBytes(nodeNames: profile.names) { result in
                    guard self.allowed(token, scheduled: scheduled), case .success(let groups) = result else { completion(nil); return }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let stillValid = (try? multiRevalidate(root: root, batch: batch)) != nil && multiLocalController(root: root) == identity
                        DispatchQueue.main.async {
                            guard self.allowed(token, scheduled: scheduled), stillValid else { completion(nil); return }
                            let candidates = multiGroupCandidates(groups, testedNames: profile.names)
                            let matches = candidates.filter { multiBytesEqual($0.name, prior.groupName) }
                            guard self.allowed(token, scheduled: scheduled), matches.count == 1,
                                  scheduled || multiBytesEqual(matches[0].now, prior.now) else { completion(nil); return }
                            let group = matches[0]
                            completion(MultiSwitchCapture(controller: identity, profile: profile, groupName: group.name, now: group.now))
                        }
                    }
                }
            }
        }
    }

    @objc private func startManual(_ sender: Any?) {
        guard !busy, !groupRefreshInFlight, !closed, let token = schedule.beginManualRun() else { return }
        if (sender as AnyObject?) !== funnelButton { funnelStage = 0 }
        cancelled = false; runFailure = false; runScheduled = false; runCapture = nil
        pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false; activeToken = token
        let switching = autoSwitch.state == .on
        requestedSwitchCapture = switching ? selectedGroupCapture() : nil
        prepare { [weak self] batch in
            guard let self = self, !self.closed else { return }
            guard self.allowed(token, scheduled: false), let batch = batch, self.describe(batch) != nil,
                  !switching || !self.switchOutcomeUnknown else {
                self.disarm(); self.abort(token, "已停止或订阅无效、已变化、超出预算；定时已停止。")
                return
            }
            if let armed = self.armedBatch, !multiSameSnapshots(armed, batch) {
                self.disarm("订阅内容已变化，定时已停止。")
            }
            guard self.allowed(token, scheduled: false),
                  !switching || !multiBatchIncludesCurrent(batch) || self.requestedSwitchCapture != nil else {
                self.disarm(); self.abort(token, "请先在主界面选择当前订阅的策略组；未开始测速。"); return
            }
            self.verifyAndLaunch(batch, token: token, scheduled: false, switching: switching)
        }
    }
    @objc private func toggleSchedule(_ sender: Any?) {
        guard !closed else { return }
        if schedule.isArmed {
            disarm("定时授权已撤销；已通过最终检查的切换请求仍可能发送或已发出，请在 Verge 核实。")
            updateControls(); return
        }
        guard !busy, !groupRefreshInFlight, intervals.indices.contains(intervalPopup.indexOfSelectedItem) else { return }
        cancelled = false; pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
        let switching = autoSwitch.state == .on
        let selectedCapture = switching ? selectedGroupCapture() : nil
        let interval = intervals[intervalPopup.indexOfSelectedItem], title = intervalPopup.titleOfSelectedItem ?? ""
        prepare { [weak self] batch in
            guard let self = self, !self.closed else { return }
            guard !self.cancelled, let batch = batch, self.describe(batch) != nil,
                  !switching || !self.switchOutcomeUnknown else {
                self.busy = false; self.updateControls(); self.status.stringValue = "已停止或订阅无效；未启用定时。"; return
            }
            // The main window shows the interval, target traffic and switch option;
            // pressing Enable is the user's recurring-run authorization.
            let finishArming: (MultiSwitchCapture?) -> Void = { capture in
                guard !self.closed else { return }
                guard !self.cancelled, !switching || !multiBatchIncludesCurrent(batch) || capture != nil else {
                    self.disarm(); self.busy = false; self.updateControls(); self.status.stringValue = "未授权或无法确认策略组，未启用定时。"; return
                }
                let root = self.root
                DispatchQueue.global(qos: .userInitiated).async {
                    let valid = (try? multiRevalidate(root: root, batch: batch)) != nil
                    DispatchQueue.main.async {
                        guard !self.closed else { return }
                        self.busy = false
                        guard valid, !self.cancelled, (try? self.schedule.arm(interval: interval, now: TestSchedule.monotonicNow())) != nil else {
                            self.disarm(); self.updateControls(); self.status.stringValue = "订阅已变化或定时无效，未启用。"; return
                        }
                        self.armedBatch = batch; self.armedSwitch = capture; self.scheduleButton.title = "停止定时"
                        self.scheduleStatus.stringValue = "定时：每 \(title) 运行（仅应用打开时，每次消耗流量）"
                        if let capture = capture {
                            self.status.stringValue = "定时已启用，首次在一个间隔后运行；持续授权策略组 ‘\(capture.groupName)’ 自动切换，可能覆盖其后的手动选点。"
                        } else {
                            self.status.stringValue = "定时已启用，首次在一个间隔后运行；未授权当前策略组自动切换。"
                        }
                        self.updateControls()
                    }
                }
            }
            if switching && multiBatchIncludesCurrent(batch) { self.preflight(batch, authorized: selectedCapture, token: nil, scheduled: false, completion: finishArming) }
            else { finishArming(nil) }
        }
    }
    private func pollSchedule() {
        guard !closed else { return }
        switch schedule.pollDue(now: TestSchedule.monotonicNow(), externalBusy: busy || groupRefreshInFlight) {
        case .started(let token):
            guard let batch = armedBatch else { disarm(); _ = schedule.finishRun(token, now: TestSchedule.monotonicNow()); return }
            cancelled = false; runFailure = false; runScheduled = true; runCapture = nil; pendingWinner = nil; pendingManual = nil; manualSwitchDisarmedSchedule = false
            activeToken = token; busy = true; updateControls()
            status.stringValue = "定时到期，正在核对已确认的订阅快照…"
            verifyAndLaunch(batch, token: token, scheduled: true, switching: autoSwitch.state == .on)
        case .skippedBusy: status.stringValue = "操作或策略组刷新尚未结束，本次定时已跳过，不会补跑。"
        case .invalidTime: disarm("定时时间无效，已停止。")
        default: break
        }
    }
    private func verifyAndLaunch(_ batch: MultiPreparedBatch, token: TestSchedule.RunToken, scheduled: Bool, switching: Bool) {
        runSwitchRequested = switching
        let needsCapture = switching && multiBatchIncludesCurrent(batch)
        let root = self.root
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let valid = (try? multiRevalidate(root: root, batch: batch)) != nil
            DispatchQueue.main.async { [weak self] in
                guard let self = self, !self.closed, self.activeToken == token else { return }
                guard valid, self.allowed(token, scheduled: scheduled), !switching || !self.switchOutcomeUnknown,
                      !scheduled || !needsCapture || self.armedSwitch != nil else {
                    self.disarm(); self.abort(token, "已停止，或订阅、配置、控制器状态失效；未开始测速。")
                    return
                }
                guard needsCapture else { self.launch(batch, token: token); return }
                let authorized = scheduled ? self.armedSwitch : self.requestedSwitchCapture
                self.preflight(batch, authorized: authorized, token: token, scheduled: scheduled) { capture in
                    guard self.activeToken == token else { return }
                    guard self.allowed(token, scheduled: scheduled), let capture = capture else {
                        self.disarm(); self.abort(token, "未授权或无法确认当前订阅、控制器和策略组；未开始测速。")
                        return
                    }
                    // Re-fetch the group displayed before the button press.
                    // The displayed `now` must not be silently rebased if
                    // another client changes it before this one-off run.
                    if !scheduled {
                        self.preflight(batch, authorized: capture, token: token, scheduled: false) { fresh in
                            guard self.activeToken == token else { return }
                            guard self.allowed(token, scheduled: false), let fresh = fresh,
                                  multiBytesEqual(fresh.now, capture.now) else {
                                self.disarm(); self.abort(token, "授权后环境已变化；未开始测速。")
                                return
                            }
                            self.runCapture = fresh; self.launch(batch, token: token)
                        }
                    } else {
                        self.runCapture = capture; self.launch(batch, token: token)
                    }
                }
            }
        }
    }
    private func workerURL() -> URL? {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return nil }
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/clash-speedtest")
        var info = stat()
        guard lstat(url.path, &info) == 0, multiIsRegular(info), FileManager.default.isExecutableFile(atPath: url.path) else { return nil }
        return url
    }
    private func launch(_ batch: MultiPreparedBatch, token: TestSchedule.RunToken) {
        guard let executable = workerURL() else {
            disarm(); abort(token, "测速组件缺失或不可执行；未启动测速，定时已停止。"); return
        }
        let jobs = multiJobs(batch)
        guard let newRows = multiRunRows(rows, jobs: jobs), jobs.count == batch.jobCount else {
            disarm(); abort(token, "节点列表与本次任务不一致；未启动测速。"); return
        }
        let highlighted = highlightedIdentity()
        currentBatch = batch
        runIdentities = Set(jobs.map { MultiIdentity(profile: $0.profileIdentity, node: $0.nodeName) })
        runJobIdentity = Dictionary(uniqueKeysWithValues: jobs.map { ($0.index, MultiIdentity(profile: $0.profileIdentity, node: $0.nodeName)) })
        rows = newRows; reloadVisibleRows(preserving: highlighted)
        cancelLaunchChecks()
        let authorization = MultiSwitchAuthorization()
        launchCheckAuthorization = authorization; launchCheckFailure = nil
        let root = self.root, scheduled = runScheduled
        let runner = MultiBatchRunner(executable: executable, jobs: jobs, parallel: batch.parallel,
            asyncPreLaunch: { [weak self] _, completion in
                guard let self = self, self.allowed(token, scheduled: scheduled), authorization.permits() else {
                    completion(false); return
                }
                self.launchChecks.submit(timeout: 15, work: {
                    guard authorization.permits() else { return false }
                    let valid = (try? multiQuickRevalidate(root: root, batch: batch,
                                                           mayContinue: { authorization.permits() })) != nil
                    return valid && authorization.permits()
                }, completion: { [weak self] outcome in
                    guard let self = self, self.allowed(token, scheduled: scheduled), authorization.permits() else {
                        completion(false); return
                    }
                    switch outcome {
                    case .passed: completion(true)
                    case .cancelled: completion(false)
                    case .failed, .timedOut:
                        self.runFailure = true
                        self.launchCheckFailure = outcome == .timedOut
                            ? "订阅校验超时；未启动后续节点，定时已停止。"
                            : "订阅或配置已变化；未启动后续节点，定时已停止。"
                        authorization.revoke()
                        self.disarm(); self.cancelLaunchChecks(); completion(false)
                    }
                })
            },
            onResult: { [weak self] job, result in self?.receive(job, result: result, token: token) },
            onFinish: { [weak self] summary in self?.finished(summary, token: token) })
        self.runner = runner; status.stringValue = "测速中：全局最多 \(batch.parallel) 个节点，可停止。"
        updateControls(); runner.start()
    }
    private func receive(_ job: MultiBatchJob, result: MultiBatchResult, token: TestSchedule.RunToken) {
        let identity = MultiIdentity(profile: job.profileIdentity, node: job.nodeName)
        guard !closed, activeToken == token, runJobIdentity[job.index] == identity, runIdentities.contains(identity),
              let index = rows.firstIndex(where: { $0.identity == identity && $0.state == "等待" }) else { return }
        let measurement = multiDecode(result, job: job)
        // One unreachable or unsupported node does not invalidate independent
        // complete measurements from other nodes. Systemic launch/configuration
        // failures still stop the queue and disarm unattended runs.
        switch result {
        case .failure(.launchFailed), .failure(.invalidJob):
            runFailure = true; disarm("测速组件无法启动，定时已停止。"); cancelLaunchChecks(); runner?.cancel()
        default: break
        }
        if measurement == nil, case .success = result { runFailure = true; disarm("测速结果无效，定时已停止。") }
        rows[index].measurement = measurement
        if case .cancelled = result { rows[index].state = "已取消" }
        else if measurement?.score == nil { rows[index].state = "无效" }
        else if rows[index].eligibleScore == nil { rows[index].state = "信息项（不参与排名）" }
        else { rows[index].state = "有效" }
        let highlighted = highlightedIdentity()
        recentlyScoredIdentities.insert(identity)
        reloadVisibleRows(preserving: highlighted)
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(300)) { [weak self] in
            guard let self = self, !self.closed else { return }
            self.recentlyScoredIdentities.remove(identity)
            self.resultTable.reloadData()
        }
        copyButton.isEnabled = true
        let done = rows.filter { runIdentities.contains($0.identity) && $0.state != "等待" }.count
        status.stringValue = "测速中：已处理 \(done) / \(runIdentities.count)；完成后按综合评分排序。"
        updateDockAndProgressState()
        updateChampionCard()
    }
    private func finished(_ summary: MultiBatchSummary, token: TestSchedule.RunToken) {
        guard !closed, activeToken == token else { return }
        runner = nil; cancelLaunchChecks()
        if summary.cancelled || cancelled {
            funnelStage = 0
            for i in rows.indices where rows[i].state == "等待" { rows[i].state = "已取消" }
            let highlighted = highlightedIdentity(); rows = multiRank(rows); reloadVisibleRows(preserving: highlighted)
            disarm(); abort(token, launchCheckFailure ?? "测速已停止；未发起后续切换。")
            return
        }
        for i in rows.indices where rows[i].state == "等待" { rows[i].state = "无效" }
        let highlighted = highlightedIdentity(); rows = multiRank(rows); reloadVisibleRows(preserving: highlighted)
        if funnelStage == 1, !runFailure, let batch = currentBatch, summary.attempted == batch.jobCount,
           let size20Index = sizeValues.firstIndex(of: 20 * 1_048_576) {
            let topCount = Int(topNBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 5
            let promoted = multiFunnelPromotedIdentities(rows: rows, runIdentities: runIdentities, topCount: max(1, min(10, topCount)))
            if !promoted.isEmpty {
                funnelStage = 2
                nodeSelection = promoted
                cascadeHighlightedIdentities = promoted
                parallelBox.stringValue = "2"; modePopup.selectItem(at: 1); sizePopup.selectItem(at: size20Index)
                reloadVisibleRows(preserving: highlighted); updateControls()
                status.stringValue = "初筛完成，前 \(promoted.count) 名已自动晋级 20MiB 精确加压决选…"
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(650)) { [weak self] in
                    self?.cascadeHighlightedIdentities.removeAll()
                    self?.resultTable.reloadData()
                }
                prepare { [weak self] stage2Batch in
                    guard let self = self, !self.closed, self.activeToken == token else { return }
                    guard self.allowed(token, scheduled: false), let stage2Batch = stage2Batch, self.describe(stage2Batch) != nil else {
                        self.funnelStage = 0
                        self.disarm(); self.abort(token, "二阶段晋级校验未通过；未启动加压复测。"); return
                    }
                    self.verifyAndLaunch(stage2Batch, token: token, scheduled: false, switching: self.runSwitchRequested)
                }
                return
            }
        }
        funnelStage = 0
        guard !runFailure, let batch = currentBatch, summary.attempted == batch.jobCount,
              let winner = rows.first(where: { runIdentities.contains($0.identity) && $0.eligibleScore != nil }),
              let measurement = winner.measurement,
              let profile = batch.profiles.first(where: { $0.option.id == winner.identity.profile }) else {
            disarm(); abort(token, "测速失败或没有合格结果；定时已停止，未切换。") ; return
        }
        status.stringValue = "测速完成，正在重新核对订阅状态…"
        let root = self.root
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let valid = (try? multiRevalidate(root: root, batch: batch)) != nil
            DispatchQueue.main.async { [weak self] in
                guard let self = self, !self.closed, self.activeToken == token else { return }
                guard valid, self.allowed(token, scheduled: self.runScheduled) else {
                    self.disarm(); self.abort(token, "订阅已变化或操作已停止；结果仅供参考，未发起切换。"); return
                }
                guard self.runSwitchRequested else {
                    self.abort(token, "测速完成；自动切换关闭，未切换。")
                    return
                }
                let pending = MultiPendingWinner(profile: profile, node: winner.nodeName, measurement: measurement)
                if batch.registry.first(where: { $0.isCurrent })?.id != profile.option.id {
                    self.disarm()
                    if self.switchOutcomeUnknown {
                        self.abort(token, "最高分属于其他订阅，但此前 PUT 结果未知；本次应用中已禁用切换，请直接在 Verge 核对。")
                    } else {
                        self.pendingWinner = pending
                        self.abort(token, "最高分属于 ‘\(profile.option.name)’（UID：\(profile.option.uid)）；正在自动热切换订阅并继续应用，不改选次优。")
#if !MULTI_UI_TEST
                        self.continuePendingSwitchIfNeeded()
#endif
                    }
                } else if let capture = self.runCapture {
                    self.applyCurrentWinner(pending, batch: batch, capture: capture, token: token)
                } else {
                    self.abort(token, "测速完成，最高分属于当前订阅；本次自动切换关闭，未切换。")
                }
            }
        }
    }
    private func coordinator(_ winner: MultiPendingWinner, identity: MultiControllerIdentity) -> AssistedSwitchCoordinator {
        AssistedSwitchCoordinator(root: root, settings: ControllerSettings(socket: identity.path, secret: ""),
                                  winner: winner.measurement, nodeName: winner.node,
                                  profileUID: winner.profile.option.uid, profilePath: winner.profile.option.path,
                                  measuredYAML: winner.profile.data)
    }

    // The coordinator repeats profile/socket/group/member/now checks before
    // any PUT. In addition, compare its fresh preparation to the PRE-TEST now,
    // never merely to the new preparation itself.
    private func applyCurrentWinner(_ winner: MultiPendingWinner, batch: MultiPreparedBatch,
                                    capture: MultiSwitchCapture, token: TestSchedule.RunToken) {
        guard allowed(token, scheduled: runScheduled), !switchOutcomeUnknown else {
            disarm(); abort(token, "操作已停止，未发起切换。"); return
        }
        status.stringValue = "正在重新确认测速前的策略组状态…"
        let root = self.root
        DispatchQueue.global(qos: .userInitiated).async {
            let valid = (try? multiRevalidate(root: root, batch: batch)) != nil && multiLocalController(root: root) == capture.controller
            DispatchQueue.main.async {
                guard self.activeToken == token else { return }
                guard valid, self.allowed(token, scheduled: self.runScheduled) else {
                    self.disarm(); self.abort(token, "订阅或控制器已变化，未发起切换。"); return
                }
                let coordinator = self.coordinator(winner, identity: capture.controller)
                self.switchCoordinator = coordinator
                coordinator.prepare { preparation in
                    guard self.activeToken == token else { return }
                    guard self.allowed(token, scheduled: self.runScheduled), case .ready(let catalogue) = preparation else {
                        self.disarm(); self.abort(token, "当前订阅或控制器无法确认，未发起切换。"); return
                    }
                    let matches = catalogue.groups.filter { multiBytesEqual($0.name, capture.groupName) }
                    guard matches.count == 1,
                          multiCaptureMatches(capture, controller: capture.controller, profile: winner.profile,
                                              group: matches[0], winner: winner.node) else {
                        self.disarm(); self.abort(token, "测速期间策略组、当前节点或成员已变化；未发起切换。"); return
                    }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let valid = (try? multiRevalidate(root: root, batch: batch)) != nil && multiLocalController(root: root) == capture.controller
                        DispatchQueue.main.async {
                            guard self.activeToken == token else { return }
                            guard valid, self.allowed(token, scheduled: self.runScheduled) else {
                                self.disarm(); self.abort(token, "操作已撤销或环境已变化；未发起切换。"); return
                            }
                            self.pendingWinner = nil; self.applying = true
                            if !multiBytesEqual(capture.now, winner.node),
                               winner.profile.names.contains(where: { multiBytesEqual($0, capture.now) }) {
                                self.lastUndoSwitchChoice = MultiPickerChoice(profile: winner.profile, node: capture.now)
                                self.lastUndoTargetNodeName = winner.node
                            }
                            let authorization = MultiSwitchAuthorization(); self.switchAuthorization = authorization
                            self.updateControls()
                            coordinator.apply(group: capture.groupName, authorization: {
                                authorization.permits() && multiLocalController(root: root) == capture.controller &&
                                    multiProfileSnapshotMatches(root: root, profile: winner.profile) && authorization.permits()
                            }) { self.finishSwitch($0, token: token) }
                        }
                    }
                }
            }
        }
    }

    @objc private func resumeWinner(_ sender: Any?) {
        if let choice = pendingManual { beginManualSwitch(choice); return }
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown, let winner = pendingWinner else { return }
#if !MULTI_UI_TEST
        if registry.first(where: { $0.isCurrent })?.id != winner.profile.option.id {
            runtimeSwitchProfile(winner.profile.option)
            return
        }
#endif
        guard let capture = selectedGroupCapture() else {
            status.stringValue = "请先在主界面选择当前订阅的策略组；尚未应用最高分。"
            updateControls()
            return
        }
        guard capture.profile.option.id == winner.profile.option.id,
              capture.profile.version == winner.profile.version, capture.profile.data == winner.profile.data,
              multiBytesEqual(capture.profile.option.path, winner.profile.option.path) else {
            status.stringValue = "目标订阅已变化；请刷新订阅和策略组后重试，尚未应用最高分。"
            return
        }
        guard let token = schedule.beginManualRun() else { return }
        disarm(); cancelled = false; runScheduled = false; pendingWinner = nil
        activeToken = token; busy = true; updateControls()
        status.stringValue = "正在重新检查目标订阅、快照和唯一控制器…"
        let root = self.root
        // Do not call multiRevalidate here: the user was explicitly instructed
        // to change `isCurrent` in Verge since the measurement was taken.
        DispatchQueue.global(qos: .userInitiated).async {
            let identity = multiProfileSnapshotMatches(root: root, profile: winner.profile) ?
                multiWaitForLocalController(root: root) : nil
            DispatchQueue.main.async {
                guard self.activeToken == token else { return }
                guard self.allowed(token, scheduled: false), let identity = identity else {
                    self.abort(token, "已停止或没有唯一可信的本地控制器；未发起切换。"); return
                }
                let coordinator = self.coordinator(winner, identity: identity)
                self.switchCoordinator = coordinator
                coordinator.prepare { preparation in
                    guard self.activeToken == token else { return }
                    guard self.allowed(token, scheduled: false) else { self.abort(token, "已停止；未发起切换。"); return }
                    guard case .ready(let catalogue) = preparation else {
                        let message: String
                        if case .manualProfileRequired = preparation {
                            self.pendingWinner = nil
                            message = "目标订阅未保持为当前订阅；未发起切换，请刷新后重试。"
                        } else { message = "目标 UID、路径、快照或控制器无法验证；未发起切换。" }
                        self.abort(token, message); return
                    }
                    let matches = catalogue.groups.filter { multiBytesEqual($0.name, capture.groupName) }
                    guard self.allowed(token, scheduled: false), matches.count == 1,
                          multiCaptureMatches(capture, controller: identity, profile: winner.profile,
                                              group: matches[0], winner: winner.node) else {
                        self.abort(token, "所示策略组、当前节点或成员已变化；请刷新后重试，未发起切换。"); return
                    }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let same = multiLocalController(root: root) == identity
                        DispatchQueue.main.async {
                            guard self.activeToken == token else { return }
                            guard same, self.allowed(token, scheduled: false) else {
                                self.abort(token, "控制器已变化或操作已停止；未发起切换。"); return
                            }
                            self.pendingWinner = nil; self.applying = true
                            if !multiBytesEqual(capture.now, winner.node),
                               winner.profile.names.contains(where: { multiBytesEqual($0, capture.now) }) {
                                self.lastUndoSwitchChoice = MultiPickerChoice(profile: winner.profile, node: capture.now)
                                self.lastUndoTargetNodeName = winner.node
                            }
                            let authorization = MultiSwitchAuthorization(); self.switchAuthorization = authorization
                            self.updateControls()
                            coordinator.apply(group: capture.groupName, authorization: {
                                authorization.permits() && multiLocalController(root: root) == identity &&
                                    multiProfileSnapshotMatches(root: root, profile: winner.profile) && authorization.permits()
                            }) { self.finishSwitch($0, token: token) }
                        }
                    }
                }
            }
        }
    }

    // Manual selection is a separate intent: it may be unmeasured or failed and
    // never invents a VerifiedMeasurement or weakens automatic winner checks.
    private func beginManualSwitch(_ choice: MultiPickerChoice) {
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown else { return }
#if !MULTI_UI_TEST
        if registry.first(where: { $0.isCurrent })?.id != choice.profile.option.id {
            manualSwitchDisarmedSchedule = manualSwitchDisarmedSchedule || schedule.isArmed
            disarm(); pendingWinner = nil; pendingManual = choice
            runtimeSwitchProfile(choice.profile.option)
            return
        }
#endif
        guard let capture = selectedGroupCapture(), capture.profile.option.id == choice.profile.option.id,
              capture.profile.version == choice.profile.version, capture.profile.data == choice.profile.data,
              multiBytesEqual(capture.profile.option.path, choice.profile.option.path) else {
            manualSwitchDisarmedSchedule = manualSwitchDisarmedSchedule || schedule.isArmed
            disarm(); pendingWinner = nil; pendingManual = choice
            status.stringValue = "请先在主界面选择当前订阅的策略组；未发起节点切换。" + (manualSwitchDisarmedSchedule ? " 原定时已停止。" : "")
            updateControls(); return
        }
        guard let token = schedule.beginManualRun() else { return }
        manualSwitchDisarmedSchedule = manualSwitchDisarmedSchedule || schedule.isArmed
        disarm(); cancelled = false; runScheduled = false; activeToken = token
        pendingWinner = nil; pendingManual = nil; busy = true; updateControls()
        status.stringValue = "正在确认手动选择的订阅、节点和唯一控制器…"
        let root = self.root
        DispatchQueue.global(qos: .userInitiated).async {
            let identity = multiProfileSnapshotMatches(root: root, profile: choice.profile) ?
                multiWaitForLocalController(root: root) : nil
            DispatchQueue.main.async {
                guard self.activeToken == token else { return }
                guard self.allowed(token, scheduled: false), let identity = identity else {
                    self.abort(token, "已停止或没有唯一可信的本地控制器；未发起切换。"); return
                }
                let coordinator = AssistedSwitchCoordinator(root: root,
                    settings: ControllerSettings(socket: identity.path, secret: ""), manualNodeName: choice.node,
                    profileUID: choice.profile.option.uid, profilePath: choice.profile.option.path,
                    measuredYAML: choice.profile.data)
                self.switchCoordinator = coordinator
                coordinator.prepare { preparation in
                    guard self.activeToken == token else { return }
                    guard self.allowed(token, scheduled: false) else { self.abort(token, "已停止；未发起切换。"); return }
                    guard case .ready(let catalogue) = preparation else {
                        if case .manualProfileRequired = preparation {
                            self.pendingManual = nil
                            self.abort(token, "目标订阅未保持为当前订阅；未发起节点切换，请刷新后重试。")
                        } else {
                            self.pendingManual = nil
                            self.abort(token, "目标订阅、节点快照或控制器无法验证；未发起切换。请重新选择节点。")
                        }
                        return
                    }
                    let matches = catalogue.groups.filter { multiBytesEqual($0.name, capture.groupName) }
                    guard self.allowed(token, scheduled: false), matches.count == 1,
                          multiCaptureMatches(capture, controller: identity, profile: choice.profile,
                                              group: matches[0], winner: choice.node) else {
                        self.abort(token, "所示策略组、当前节点或成员已变化；请刷新后重试，未发起切换。"); return
                    }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let same = multiLocalController(root: root) == identity
                        DispatchQueue.main.async {
                            guard self.activeToken == token else { return }
                            guard same, self.allowed(token, scheduled: false) else {
                                self.abort(token, "控制器已变化或操作已停止；未发起切换。"); return
                            }
                            if !multiBytesEqual(capture.now, choice.node),
                               choice.profile.names.contains(where: { multiBytesEqual($0, capture.now) }) {
                                self.lastUndoSwitchChoice = MultiPickerChoice(profile: choice.profile, node: capture.now)
                                self.lastUndoTargetNodeName = choice.node
                            }
                            self.pendingManual = nil; self.applying = true
                            let authorization = MultiSwitchAuthorization(); self.switchAuthorization = authorization
                            self.updateControls()
                            coordinator.apply(group: capture.groupName, authorization: {
                                authorization.permits() && multiLocalController(root: root) == identity &&
                                    multiProfileSnapshotMatches(root: root, profile: choice.profile) && authorization.permits()
                            }) { self.finishSwitch($0, token: token) }
                        }
                    }
                }
            }
        }
    }

#if !MULTI_UI_TEST
    private func continuePendingSwitchIfNeeded() {
        guard !busy, !groupRefreshInFlight, !closed, !switchOutcomeUnknown,
              pendingWinner != nil || pendingManual != nil else { return }
        // The current-profile and selected-group checks remain inside
        // resumeWinner. This helper only replaces the removed continuation
        // button with an automatic guarded continuation.
        resumeWinner(nil)
    }

    private func runtimeSwitchProfile(_ option: ProfileOption) {
        busy = true; updateControls()
        status.stringValue = "正在通过 Clash Verge 运行时补丁切换 ‘\(option.name)’…"
        runtimeInjector.switchProfile(uid: option.uid) { [weak self] result in
            DispatchQueue.main.async {
                guard let self, !self.closed else { return }
                self.busy = false
                switch result {
                case .success(let switched):
                    guard switched.current.utf8.elementsEqual(option.uid.utf8) else {
                        self.status.stringValue = "Clash Verge 未确认目标订阅；请手动核对。"
                        self.updateControls()
                        return
                    }
                    self.status.stringValue = "已热切换到 ‘\(switched.name)’；正在刷新助手界面…"
                    self.refresh(preservingPending: true)
                case .failure:
                    self.pendingWinner = nil
                    self.pendingManual = nil
                    self.status.stringValue = "运行时切换失败；未应用 ‘\(option.name)’，请检查 Clash Verge 运行时桥接。"
                    self.updateControls()
                }
            }
        }
    }
#endif

    private func finishSwitch(_ outcome: AssistedSwitchResult, token: TestSchedule.RunToken) {
        guard activeToken == token else { return }
        let message: String
        switch outcome {
        case .confirmedNoOp:
            dockConfirmedUntil = Date().addingTimeInterval(2.6)
            message = "策略组本已选择目标节点；已确认，无需 PUT。"
        case .switched:
            dockConfirmedUntil = Date().addingTimeInterval(2.6)
            message = "已切换并经控制器复核；旧连接可能保留，订阅恢复可能覆盖。"
        case .manualProfileRequired:
            lastUndoSwitchChoice = nil; lastUndoTargetNodeName = nil
            disarm(); message = "当前订阅已变化，未发起切换；定时已停止。"
        case .notAttempted:
            lastUndoSwitchChoice = nil; lastUndoTargetNodeName = nil
            disarm(); message = "环境或策略组校验失败；未发起 PUT，定时已停止。"
        case .outcomeUnknown:
            lastUndoSwitchChoice = nil; lastUndoTargetNodeName = nil
            disarm(); switchOutcomeUnknown = true; pendingWinner = nil; pendingManual = nil; autoSwitch.state = .off
            message = "切换结果未知：PUT 可能已发生。定时已停止，本次运行不再尝试切换；请在 Verge 核对，切勿重试。"
            showWarning("切换结果未知", message)
        }
        abort(token, message)
        switch outcome {
        case .confirmedNoOp, .switched:
            // Keep the displayed group's `now` current for the next one-click
            // operation without revoking an already armed recurring test.
            refreshGroups(true)
        default: break
        }
    }

    private func abort(_ token: TestSchedule.RunToken, _ message: String) {
        guard activeToken == token else { return }
        cancelLaunchChecks()
        _ = schedule.finishRun(token, now: TestSchedule.monotonicNow()); activeToken = nil; currentBatch = nil
        runCapture = nil; switchCoordinator = nil; switchAuthorization?.revoke(); switchAuthorization = nil; applying = false
        let disarmedNotice = manualSwitchDisarmedSchedule && !message.contains("定时已停止") ? " 原定时已停止。" : ""
        if pendingManual == nil { manualSwitchDisarmedSchedule = false }
        busy = false; status.stringValue = message + disarmedNotice; updateControls()
    }
    @objc private func stop(_ sender: Any?) {
        guard busy else { return }
        // A request past its final guard may still be sent; revocation is not atomic with PUT.
        switchAuthorization?.revoke(); cancelled = true; pendingWinner = nil; pendingManual = nil
        disarm(); cancelLaunchChecks(); runner?.cancel(); updateControls()
        // The runner may finish synchronously during disarm; keep its final
        // status instead of falsely saying an already-idle operation is stopping.
        if busy {
            status.stringValue = applying ? "正在撤销授权；已通过最终检查的切换仍可能发送或已发出，请等待结果并核实。" : "正在停止并撤销尚未通过检查的授权；定时已停止。"
        }
    }
    private func cells(_ row: MultiRow) -> [String] {
        guard let m = row.measurement else { return ["\(row.index + 1)", row.profileName, row.nodeName, "—", "—", "—", "—", "—", row.state] }
        let failures = m.headAttempts > 0 ? 100.0 * Double(m.headAttempts - m.headSuccesses) / Double(m.headAttempts) : 100
        let speed = String(format: "%.2f MiB/s", m.downloadBytesPerSecond / 1_048_576)
        let latency = String(format: "%.1f ms", m.latencyMilliseconds)
        let jitter = String(format: "%.1f ms", m.jitterMilliseconds)
        let score = m.score.map { String(format: "%.2f", $0) } ?? "无效"
        return ["\(row.index + 1)", row.profileName, row.nodeName, m.nodeType, speed, latency,
                String(format: "%.1f%%", failures), jitter, row.eligibleScore == nil && m.score != nil ? "\(score)（信息项）" : score]
    }
    @objc private func copyResults(_ sender: Any?) {
        guard !rows.isEmpty else { return }
        let header = "原序号\t订阅\t节点\t类型\t已验证速度\tHEAD延迟\tHEAD失败率\tHEAD抖动\t综合分"
        let content = ([header] + rows.map { cells($0).map(multiTSVSafeCell).joined(separator: "\t") }).joined(separator: "\n")
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(content, forType: .string)
        status.stringValue = "已复制结果；其中包含订阅与节点名称，请注意隐私。"
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let table = notification.object as? NSTableView, table === resultTable else { return }
        if visibleRows.indices.contains(table.selectedRow) {
            let selected = rows[visibleRows[table.selectedRow]]
            status.stringValue = "选中：\(selected.profileName) / \(selected.nodeName)；点击下方切换按钮即执行。"
        }
        updateControls()
    }
    private func centeredCell(_ field: NSTextField, align: NSTextAlignment = .left) -> NSView {
        field.alignment = align
        field.lineBreakMode = .byTruncatingTail
        field.translatesAutoresizingMaskIntoConstraints = false
        let cell = NSTableCellView()
        cell.textField = field
        cell.addSubview(field)
        let lead: CGFloat = align == .left ? 6 : 4
        let trail: CGFloat = align == .right ? -8 : -4
        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: lead),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: trail),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
        ])
        return cell
    }
    private func badgeCell(_ text: String, textColor: NSColor, fillColor: NSColor, fillAlpha: CGFloat = 1.0,
                           font: NSFont, align: NSTextAlignment = .center) -> NSView {
        let lbl = NSTextField(labelWithString: text)
        lbl.font = font
        lbl.textColor = textColor
        lbl.lineBreakMode = .byTruncatingTail
        let pill = pillBadge(lbl, fill: fillColor, fillAlpha: fillAlpha, cornerRadius: 6, hPad: 7, vPad: 2)
        pill.translatesAutoresizingMaskIntoConstraints = false
        let cell = NSTableCellView()
        cell.addSubview(pill)
        var constraints = [pill.centerYAnchor.constraint(equalTo: cell.centerYAnchor)]
        if align == .right {
            constraints.append(pill.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6))
            constraints.append(pill.leadingAnchor.constraint(greaterThanOrEqualTo: cell.leadingAnchor, constant: 4))
        } else if align == .center {
            constraints.append(pill.centerXAnchor.constraint(equalTo: cell.centerXAnchor))
        } else {
            constraints.append(pill.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6))
        }
        NSLayoutConstraint.activate(constraints)
        return cell
    }
    func numberOfRows(in tableView: NSTableView) -> Int { tableView == profileTable ? registry.count : visibleRows.count }
    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        guard tableView == resultTable else { return nil }
        let rv = MultiResultRowView()
        if visibleRows.indices.contains(row), rows.indices.contains(visibleRows[row]) {
            let item = rows[visibleRows[row]]
            rv.isCascadeHighlighted = cascadeHighlightedIdentities.contains(item.identity)
            rv.isRecentlyScored = recentlyScoredIdentities.contains(item.identity)
            rv.isActiveTesting = busy && runIdentities.contains(item.identity) && item.state == "等待"
        }
        return rv
    }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let id = tableColumn?.identifier.rawValue else { return nil }
        if tableView == profileTable {
            guard registry.indices.contains(row) else { return nil }
            if id == "select" {
                let check = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleProfile(_:)))
                check.imagePosition = .imageOnly; check.alignment = .center
                check.tag = row; check.state = selected.contains(registry[row].id) ? .on : .off; check.isEnabled = !busy
                return check
            }
            let option = registry[row]
            let cell = NSTableCellView()
            let icon = symbolIcon(option.isCurrent ? "dot.radiowaves.left.and.right" : "tray.full",
                                  color: option.isCurrent ? MultiPalette.accent : MultiPalette.muted, size: 11.5)
            let nameField = NSTextField(labelWithString: option.name)
            nameField.font = .systemFont(ofSize: 12.5, weight: option.isCurrent ? .semibold : .medium)
            nameField.textColor = MultiPalette.text
            nameField.lineBreakMode = .byTruncatingTail
            nameField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            var items: [NSView] = [icon, nameField, spacer()]
            if option.isCurrent {
                let activeLbl = NSTextField(labelWithString: "当前")
                activeLbl.font = .systemFont(ofSize: 10, weight: .semibold)
                activeLbl.textColor = MultiPalette.accent
                let activePill = pillBadge(activeLbl, fill: MultiPalette.accent, fillAlpha: 0.15, cornerRadius: 5, hPad: 6, vPad: 1.5)
                items.append(activePill)
            }
            let r = self.row(items, spacing: 6)
            r.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(r)
            NSLayoutConstraint.activate([
                r.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                r.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
                r.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
            ])
            return cell
        }
        guard visibleRows.indices.contains(row), rows.indices.contains(visibleRows[row]) else { return nil }
        let item = rows[visibleRows[row]]
        if id == "check" {
            let check = MultiNodeCheckbox(checkboxWithTitle: "", target: self, action: #selector(toggleNode(_:)))
            check.imagePosition = .imageOnly; check.alignment = .center
            check.nodeIdentity = item.identity; check.state = nodeSelection?.contains(item.identity) == true ? .on : .off
            check.isEnabled = !busy; return check
        }
        let ids = ["rank", "profile", "node", "type", "speed", "latency", "failure", "jitter", "score"]
        guard let index = ids.firstIndex(of: id) else { return nil }
        let text = cells(item)[index]
        if id == "type" && text != "—" {
            return badgeCell(text.uppercased(), textColor: MultiPalette.muted, fillColor: MultiPalette.elevated,
                             font: .monospacedSystemFont(ofSize: 10.5, weight: .semibold), align: .center)
        }
        if id == "speed" {
            let isRowTesting = busy && runIdentities.contains(item.identity) && item.state == "等待"
            if let m = item.measurement {
                let cell = MultiSpeedBarCellView()
                let mib = m.downloadBytesPerSecond / 1_048_576
                let maxSpeedMiB = max(20.0, (rows.compactMap { $0.measurement?.downloadBytesPerSecond }.max() ?? 20_971_520) / 1_048_576)
                cell.configure(text: text, fraction: CGFloat(mib / maxSpeedMiB), isTesting: false)
                return cell
            } else if isRowTesting {
                let cell = MultiSpeedBarCellView()
                cell.configure(text: "测速中…", fraction: 0, isTesting: true)
                return cell
            }
        }
        if id == "latency", let m = item.measurement {
            let field = label(text, size: 11.5, color: MultiPalette.text, monospacedDigits: true)
            field.toolTip = String(format: "HEAD 延迟 %.1f ms", m.latencyMilliseconds)
            return centeredCell(field, align: .right)
        }
        if id == "score" {
            let hasScore = item.eligibleScore != nil
            if hasScore {
                let isTop = item.eligibleScore == rows.compactMap(\.eligibleScore).max()
                let tint: NSColor = isTop ? MultiPalette.accent : MultiPalette.text
                let display = isTop ? "★ \(text)" : text
                return badgeCell(display, textColor: tint, fillColor: .clear,
                                 font: .monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold), align: .right)
            } else if busy && runIdentities.contains(item.identity) && item.state == "等待" {
                return badgeCell("测速中…", textColor: MultiPalette.accent, fillColor: .clear,
                                 font: .systemFont(ofSize: 11, weight: .semibold), align: .right)
            } else if text.contains("失败") || text.contains("未完成") || text.contains("无效") {
                return badgeCell(text, textColor: MultiPalette.danger, fillColor: .clear,
                                 font: .systemFont(ofSize: 11, weight: .semibold), align: .right)
            }
        }
        let field = NSTextField(labelWithString: text)
        let align: NSTextAlignment = ["rank", "type"].contains(id) ? .center : ["speed", "latency", "failure", "jitter", "score"].contains(id) ? .right : .left
        if id == "node" {
            field.font = .systemFont(ofSize: 12.5, weight: .medium)
            field.textColor = MultiPalette.text
        } else if id == "profile" {
            field.font = .systemFont(ofSize: 12, weight: .regular)
            field.textColor = MultiPalette.muted
        } else if id == "speed" {
            field.font = .monospacedDigitSystemFont(ofSize: 12, weight: text == "—" ? .regular : .semibold)
            field.textColor = text == "—" ? MultiPalette.faint : MultiPalette.text
        } else {
            field.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
            field.textColor = text == "—" ? MultiPalette.faint : MultiPalette.muted
        }
        return centeredCell(field, align: align)
    }
}

#if !MULTI_UI_TEST
@main
struct MultiMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = MultiAppDelegate()
        app.delegate = delegate; app.setActivationPolicy(.regular); app.run()
        withExtendedLifetime(delegate) {}
    }
}
#endif
