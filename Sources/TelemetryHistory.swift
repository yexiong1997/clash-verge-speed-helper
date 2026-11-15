import Foundation
import Darwin
import SQLite3

private let sqliteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

public struct TelemetryIdentity: Hashable, Sendable {
    public let profile: Data
    public let node: Data

    public init(profile: Data, node: Data) {
        self.profile = profile
        self.node = node
    }

    public init(profile: Data, nodeName: String) {
        self.profile = profile
        self.node = Data(nodeName.utf8)
    }

    public init(profileUID: String, nodeName: String) {
        self.profile = Data(profileUID.utf8)
        self.node = Data(nodeName.utf8)
    }
}

public enum TelemetryWindow: Int, CaseIterable, Sendable {
    case hours24 = 0
    case days7 = 1
    case days30 = 2
    case peakHours = 3

    public var title: String {
        switch self {
        case .hours24: return "24小时"
        case .days7: return "7天"
        case .days30: return "30天"
        case .peakHours: return "晚高峰"
        }
    }

    public var lookbackSeconds: TimeInterval {
        switch self {
        case .hours24: return 24.0 * 3600.0
        case .days7: return 7.0 * 86400.0
        case .days30, .peakHours: return 30.0 * 86400.0
        }
    }

    public var halfLifeSeconds: TimeInterval {
        switch self {
        case .hours24: return 12.0 * 3600.0
        case .days7: return 3.5 * 86400.0
        case .days30, .peakHours: return 7.0 * 86400.0
        }
    }
}

public enum TelemetrySampleSource: String, CaseIterable, Sendable {
    case manual
    case load20M = "load_20m"
    case funnelStage1 = "funnel_s1"
    case funnelStage2 = "funnel_s2"
    case scheduled
    case patrol
    case retest
}

public struct TelemetrySampleRecord: Sendable {
    public let id: Int64
    public let timestamp: TimeInterval
    public let profileUID: String
    public let profileName: String
    public let nodeName: String
    public let nodeType: String
    public let source: TelemetrySampleSource
    public let requestedBytes: Int
    public let streams: Int
    public let downloadComplete: Bool
    public let downloadBytesPerSecond: Double
    public let latencyMilliseconds: Double
    public let jitterMilliseconds: Double
    public let headAttempts: Int
    public let headSuccesses: Int
    public let compositeScore: Double?
    public let isPeakHour: Bool

    public var identity: TelemetryIdentity {
        TelemetryIdentity(profileUID: profileUID, nodeName: nodeName)
    }

    public var isSuccess: Bool {
        guard downloadComplete,
              downloadBytesPerSecond.isFinite, downloadBytesPerSecond > 0,
              let score = compositeScore, score.isFinite else {
            return false
        }
        return true
    }

    public init(
        id: Int64 = 0,
        timestamp: TimeInterval,
        profileUID: String,
        profileName: String,
        nodeName: String,
        nodeType: String,
        source: TelemetrySampleSource,
        requestedBytes: Int,
        streams: Int,
        downloadComplete: Bool,
        downloadBytesPerSecond: Double,
        latencyMilliseconds: Double,
        jitterMilliseconds: Double,
        headAttempts: Int,
        headSuccesses: Int,
        compositeScore: Double?,
        isPeakHour: Bool? = nil
    ) {
        self.id = id
        self.timestamp = timestamp
        self.profileUID = profileUID
        self.profileName = profileName
        self.nodeName = nodeName
        self.nodeType = nodeType
        self.source = source
        self.requestedBytes = requestedBytes
        self.streams = streams
        self.downloadComplete = downloadComplete
        self.downloadBytesPerSecond = downloadBytesPerSecond
        self.latencyMilliseconds = latencyMilliseconds
        self.jitterMilliseconds = jitterMilliseconds
        self.headAttempts = headAttempts
        self.headSuccesses = headSuccesses
        self.compositeScore = compositeScore
        self.isPeakHour = isPeakHour ?? telemetryIsPeakHour(timestamp: timestamp)
    }
}

public struct TelemetryHealthEvent: Sendable {
    public let id: Int64
    public let timestamp: TimeInterval
    public let profileUID: String
    public let nodeName: String
    public let groupName: String
    public let ok: Bool
    public let delayMilliseconds: Double?
    public let failureKind: String?

    public var identity: TelemetryIdentity {
        TelemetryIdentity(profileUID: profileUID, nodeName: nodeName)
    }

    public init(
        id: Int64 = 0,
        timestamp: TimeInterval,
        profileUID: String,
        nodeName: String,
        groupName: String,
        ok: Bool,
        delayMilliseconds: Double?,
        failureKind: String?
    ) {
        self.id = id
        self.timestamp = timestamp
        self.profileUID = profileUID
        self.nodeName = nodeName
        self.groupName = groupName
        self.ok = ok
        self.delayMilliseconds = delayMilliseconds
        self.failureKind = failureKind
    }
}

public struct TelemetryHealthState: Sendable, Equatable {
    public let penalty: Double
    public let streak: Double
    public let lastCheckAt: TimeInterval?
    public let lastOK: Bool?
}

public struct TelemetryNodeSummary: Sendable {
    public let identity: TelemetryIdentity
    public let profileUID: String
    public let profileName: String
    public let nodeName: String
    public let nodeType: String
    public let longTermScore: Double
    public let baseScore: Double
    public let healthPenalty: Double
    public let activeFailureStreak: Double
    public let weightedMedianScore: Double
    public let weightedP20Score: Double
    public let weightedMedianSpeedBytesPerSecond: Double
    public let weightedMedianLatencyMs: Double
    public let weightedMedianJitterMs: Double
    public let availabilityRate: Double
    public let weightedAvailabilityRate: Double
    public let totalCount: Int
    public let successCount: Int
    public let failureCount: Int
    public let isQualified: Bool
    public let lastTestedAt: TimeInterval
    public let lastHealthCheckAt: TimeInterval?
    public let lastHealthOK: Bool?
    public let lastLatencyMs: Double?
    public let latestSpeedBytesPerSecond: Double?
    public let latestScore: Double?
}

public struct TelemetryPatrolSelection: Sendable, Equatable {
    public let identities: [TelemetryIdentity]
    public let defenderCount: Int
    public let explorerCount: Int
    public let remainingQuotaBytes: Int
}

public func telemetryIsPeakHour(timestamp: TimeInterval, calendar: Calendar = .current) -> Bool {
    guard timestamp.isFinite else { return false }
    let date = Date(timeIntervalSince1970: timestamp)
    let comps = calendar.dateComponents([.hour, .minute], from: date)
    guard let hour = comps.hour, let minute = comps.minute else { return false }
    let minutes = hour * 60 + minute
    return minutes >= (20 * 60) && minutes < (23 * 60 + 30)
}

public func telemetryPayloadWeight(requestedBytes: Int) -> Double {
    if requestedBytes >= 20 * 1_048_576 { return 1.0 }
    if requestedBytes >= 3 * 1_048_576 { return 0.75 }
    if requestedBytes >= 1 * 1_048_576 { return 0.45 }
    return 0.30
}

public func telemetryWeightedQuantile(_ pairs: [(value: Double, weight: Double)], quantile: Double) -> Double {
    let q = min(1.0, max(0.0, quantile))
    let sorted = pairs.filter { $0.value.isFinite && $0.weight.isFinite && $0.weight > 0 }
        .sorted { $0.value < $1.value }
    guard !sorted.isEmpty else { return 0 }
    if sorted.count == 1 { return sorted[0].value }
    let totalWeight = sorted.reduce(0.0) { $0 + $1.weight }
    guard totalWeight > 0 else { return sorted[0].value }

    var midpoints: [Double] = []
    midpoints.reserveCapacity(sorted.count)
    var cumulative = 0.0
    for item in sorted {
        midpoints.append((cumulative + item.weight * 0.5) / totalWeight)
        cumulative += item.weight
    }

    if q <= midpoints[0] { return sorted[0].value }
    if q >= midpoints[sorted.count - 1] { return sorted[sorted.count - 1].value }
    for i in 1..<sorted.count {
        let leftM = midpoints[i - 1]
        let rightM = midpoints[i]
        if q <= rightM {
            let span = rightM - leftM
            if span <= 1e-12 { return sorted[i].value }
            let frac = (q - leftM) / span
            return sorted[i - 1].value + frac * (sorted[i].value - sorted[i - 1].value)
        }
    }
    return sorted[sorted.count - 1].value
}

public func telemetryComputeHealthState(
    healthEvents: [TelemetryHealthEvent],
    successSampleTimestamps: [TimeInterval],
    now: TimeInterval
) -> TelemetryHealthState {
    let halfLife = 24.0 * 3600.0
    let decayLambda = log(2.0) / halfLife
    let cutoff = now - 30.0 * 86400.0

    enum EventKind: Int {
        case healthFailure = 0
        case healthOK = 1
        case speedSuccess = 2
    }
    struct TimelineItem {
        let timestamp: TimeInterval
        let kind: EventKind
    }

    var timeline: [TimelineItem] = []
    var lastCheckAt: TimeInterval?
    var lastOK: Bool?

    for event in healthEvents where event.timestamp.isFinite && event.timestamp >= cutoff && event.timestamp <= now + 60.0 {
        timeline.append(TimelineItem(timestamp: event.timestamp, kind: event.ok ? .healthOK : .healthFailure))
        if lastCheckAt == nil || event.timestamp >= lastCheckAt! {
            lastCheckAt = event.timestamp
            lastOK = event.ok
        }
    }
    for ts in successSampleTimestamps where ts.isFinite && ts >= cutoff && ts <= now + 60.0 {
        timeline.append(TimelineItem(timestamp: ts, kind: .speedSuccess))
    }

    timeline.sort { lhs, rhs in
        if lhs.timestamp != rhs.timestamp { return lhs.timestamp < rhs.timestamp }
        return lhs.kind.rawValue < rhs.kind.rawValue
    }

    var streak = 0.0
    var previousTs: TimeInterval?
    for item in timeline {
        if let prev = previousTs, item.timestamp > prev {
            streak *= exp(-decayLambda * (item.timestamp - prev))
        }
        switch item.kind {
        case .healthFailure:
            streak += 1.0
        case .healthOK:
            streak = max(0.0, streak * 0.35 - 0.8)
        case .speedSuccess:
            streak = 0.0
        }
        previousTs = item.timestamp
    }

    if let prev = previousTs, now > prev {
        streak *= exp(-decayLambda * (now - prev))
    }

    let penalty: Double
    if streak >= 1.8 {
        penalty = min(25.0, 5.0 + (streak - 1.8) * 6.25)
    } else {
        penalty = 0.0
    }
    return TelemetryHealthState(penalty: penalty, streak: streak, lastCheckAt: lastCheckAt, lastOK: lastOK)
}

public func telemetryComputeSummaries(
    samples: [TelemetrySampleRecord],
    healthEvents: [TelemetryHealthEvent],
    window: TelemetryWindow,
    now: TimeInterval = Date().timeIntervalSince1970
) -> [TelemetryNodeSummary] {
    let cutoff = now - window.lookbackSeconds
    let halfLife = window.halfLifeSeconds
    let decayLambda = log(2.0) / halfLife

    var groupedSamples: [TelemetryIdentity: [TelemetrySampleRecord]] = [:]
    for sample in samples {
        guard sample.timestamp.isFinite, sample.timestamp >= cutoff, sample.timestamp <= now + 60.0 else { continue }
        if window == .peakHours && !sample.isPeakHour { continue }
        groupedSamples[sample.identity, default: []].append(sample)
    }
    guard !groupedSamples.isEmpty else { return [] }

    var allSuccessTimestamps: [TelemetryIdentity: [TimeInterval]] = [:]
    for sample in samples where sample.isSuccess {
        allSuccessTimestamps[sample.identity, default: []].append(sample.timestamp)
    }

    var groupedHealth: [TelemetryIdentity: [TelemetryHealthEvent]] = [:]
    for event in healthEvents {
        groupedHealth[event.identity, default: []].append(event)
    }

    struct Intermediate {
        let identity: TelemetryIdentity
        let profileUID: String
        let profileName: String
        let nodeName: String
        let nodeType: String
        let successCount: Int
        let totalCount: Int
        let failureCount: Int
        let totalWeight: Double
        let successWeight: Double
        let coreQuality: Double
        let weightedMedianScore: Double
        let weightedP20Score: Double
        let weightedMedianSpeed: Double
        let weightedMedianLatency: Double
        let weightedMedianJitter: Double
        let weightedAvailability: Double
        let availabilityRate: Double
        let lastTestedAt: TimeInterval
        let lastLatencyMs: Double?
        let latestSpeed: Double?
        let latestScore: Double?
    }

    var intermediates: [Intermediate] = []
    intermediates.reserveCapacity(groupedSamples.count)

    for (identity, nodeSamples) in groupedSamples {
        let sortedByTime = nodeSamples.sorted { $0.timestamp < $1.timestamp }
        guard let latest = sortedByTime.last else { continue }
        let latestSuccess = sortedByTime.last(where: { $0.isSuccess })
        let displayType = latestSuccess?.nodeType ?? (latest.nodeType == "—" ? "" : latest.nodeType)

        var scorePairs: [(Double, Double)] = []
        var speedPairs: [(Double, Double)] = []
        var latencyPairs: [(Double, Double)] = []
        var jitterPairs: [(Double, Double)] = []
        var totalWeight = 0.0
        var successWeight = 0.0
        var successCount = 0

        for s in sortedByTime {
            let age = max(0.0, now - s.timestamp)
            let timeWeight = exp(-decayLambda * age)
            let payloadWeight = telemetryPayloadWeight(requestedBytes: s.requestedBytes)
            let peakMultiplier = (window != .peakHours && s.isPeakHour) ? 1.25 : 1.0
            let w = timeWeight * payloadWeight * peakMultiplier
            totalWeight += w
            if s.isSuccess, let score = s.compositeScore {
                successCount += 1
                successWeight += w
                scorePairs.append((score, w))
                speedPairs.append((s.downloadBytesPerSecond, w))
                latencyPairs.append((s.latencyMilliseconds, w))
                jitterPairs.append((s.jitterMilliseconds, w))
            }
        }

        let totalCount = sortedByTime.count
        let failureCount = totalCount - successCount
        let availabilityRate = Double(successCount) / Double(totalCount)
        let weightedAvailability = totalWeight > 0 ? min(1.0, max(0.0, successWeight / totalWeight)) : 0.0

        let wMedScore = successCount > 0 ? telemetryWeightedQuantile(scorePairs, quantile: 0.50) : 0.0
        let wP20Score = successCount > 0 ? telemetryWeightedQuantile(scorePairs, quantile: 0.20) : 0.0
        let coreQuality = successCount > 0 ? (0.60 * wMedScore + 0.40 * wP20Score) : 0.0
        let wMedSpeed = successCount > 0 ? telemetryWeightedQuantile(speedPairs, quantile: 0.50) : 0.0
        let wMedLatency = successCount > 0 ? telemetryWeightedQuantile(latencyPairs, quantile: 0.50) : 0.0
        let wMedJitter = successCount > 0 ? telemetryWeightedQuantile(jitterPairs, quantile: 0.50) : 0.0

        intermediates.append(Intermediate(
            identity: identity,
            profileUID: latest.profileUID,
            profileName: latest.profileName,
            nodeName: latest.nodeName,
            nodeType: displayType,
            successCount: successCount,
            totalCount: totalCount,
            failureCount: failureCount,
            totalWeight: totalWeight,
            successWeight: successWeight,
            coreQuality: coreQuality,
            weightedMedianScore: wMedScore,
            weightedP20Score: wP20Score,
            weightedMedianSpeed: wMedSpeed,
            weightedMedianLatency: wMedLatency,
            weightedMedianJitter: wMedJitter,
            weightedAvailability: weightedAvailability,
            availabilityRate: availabilityRate,
            lastTestedAt: latest.timestamp,
            lastLatencyMs: latestSuccess?.latencyMilliseconds,
            latestSpeed: latestSuccess?.downloadBytesPerSecond,
            latestScore: latestSuccess?.compositeScore
        ))
    }

    let validCoreQualities = intermediates.filter { $0.successCount > 0 }.map(\.coreQuality).sorted()
    let globalMedianCore: Double
    if validCoreQualities.isEmpty {
        globalMedianCore = 50.0
    } else if validCoreQualities.count % 2 == 1 {
        globalMedianCore = validCoreQualities[validCoreQualities.count / 2]
    } else {
        let mid = validCoreQualities.count / 2
        globalMedianCore = 0.5 * (validCoreQualities[mid - 1] + validCoreQualities[mid])
    }
    let prior = min(70.0, globalMedianCore)
    let priorWeight = 1.2

    var summaries: [TelemetryNodeSummary] = []
    summaries.reserveCapacity(intermediates.count)

    for item in intermediates {
        let baseScore: Double
        if item.successCount > 0 {
            let shrunkQuality = (item.successWeight * item.coreQuality + priorWeight * prior) / (item.successWeight + priorWeight)
            baseScore = max(0.0, min(100.0, shrunkQuality * pow(item.weightedAvailability, 1.35)))
        } else {
            baseScore = 0.0
        }

        let health = telemetryComputeHealthState(
            healthEvents: groupedHealth[item.identity] ?? [],
            successSampleTimestamps: allSuccessTimestamps[item.identity] ?? [],
            now: now
        )
        let finalScore = item.successCount > 0 ? max(0.0, min(100.0, baseScore - health.penalty)) : 0.0
        let qualified = (item.successCount >= 3 && item.availabilityRate >= 0.5)

        summaries.append(TelemetryNodeSummary(
            identity: item.identity,
            profileUID: item.profileUID,
            profileName: item.profileName,
            nodeName: item.nodeName,
            nodeType: item.nodeType,
            longTermScore: finalScore,
            baseScore: baseScore,
            healthPenalty: item.successCount > 0 ? health.penalty : 0.0,
            activeFailureStreak: health.streak,
            weightedMedianScore: item.weightedMedianScore,
            weightedP20Score: item.weightedP20Score,
            weightedMedianSpeedBytesPerSecond: item.weightedMedianSpeed,
            weightedMedianLatencyMs: item.weightedMedianLatency,
            weightedMedianJitterMs: item.weightedMedianJitter,
            availabilityRate: item.availabilityRate,
            weightedAvailabilityRate: item.weightedAvailability,
            totalCount: item.totalCount,
            successCount: item.successCount,
            failureCount: item.failureCount,
            isQualified: qualified,
            lastTestedAt: item.lastTestedAt,
            lastHealthCheckAt: health.lastCheckAt,
            lastHealthOK: health.lastOK,
            lastLatencyMs: item.lastLatencyMs,
            latestSpeedBytesPerSecond: item.latestSpeed,
            latestScore: item.latestScore
        ))
    }

    return summaries.sorted { lhs, rhs in
        if lhs.isQualified != rhs.isQualified { return lhs.isQualified }
        if abs(lhs.longTermScore - rhs.longTermScore) > 1e-9 { return lhs.longTermScore > rhs.longTermScore }
        if lhs.successCount != rhs.successCount { return lhs.successCount > rhs.successCount }
        if abs(lhs.weightedMedianSpeedBytesPerSecond - rhs.weightedMedianSpeedBytesPerSecond) > 1e-6 {
            return lhs.weightedMedianSpeedBytesPerSecond > rhs.weightedMedianSpeedBytesPerSecond
        }
        if lhs.lastTestedAt != rhs.lastTestedAt { return lhs.lastTestedAt > rhs.lastTestedAt }
        if lhs.identity.profile != rhs.identity.profile {
            return lhs.identity.profile.lexicographicallyPrecedes(rhs.identity.profile)
        }
        return lhs.identity.node.lexicographicallyPrecedes(rhs.identity.node)
    }
}

public func telemetryPatrolBytesUsedToday(
    samples: [TelemetrySampleRecord],
    now: TimeInterval = Date().timeIntervalSince1970,
    calendar: Calendar = .current
) -> Int {
    guard now.isFinite else { return 0 }
    let nowDate = Date(timeIntervalSince1970: now)
    let startOfDay = calendar.startOfDay(for: nowDate)
    let nextDay = calendar.date(byAdding: .day, value: 1, to: startOfDay) ?? startOfDay.addingTimeInterval(86400.0)
    let startTs = startOfDay.timeIntervalSince1970
    let endTs = nextDay.timeIntervalSince1970
    var total = 0
    for s in samples where s.source == .patrol && s.timestamp >= startTs && s.timestamp < endTs {
        total += max(0, s.requestedBytes)
    }
    return total
}

public func telemetrySelectPatrolIdentities(
    candidates: [TelemetryIdentity],
    samples: [TelemetrySampleRecord],
    healthEvents: [TelemetryHealthEvent],
    dailyQuotaBytes: Int,
    perNodeBytes: Int = 1_048_576,
    maxPerTick: Int = 8,
    now: TimeInterval = Date().timeIntervalSince1970,
    calendar: Calendar = .current
) -> TelemetryPatrolSelection {
    let usedToday = telemetryPatrolBytesUsedToday(samples: samples, now: now, calendar: calendar)
    let remaining = max(0, dailyQuotaBytes - usedToday)
    guard perNodeBytes > 0, maxPerTick > 0 else {
        return TelemetryPatrolSelection(identities: [], defenderCount: 0, explorerCount: 0, remainingQuotaBytes: remaining)
    }

    var uniqueCandidates: [TelemetryIdentity] = []
    var seen = Set<TelemetryIdentity>()
    for item in candidates where seen.insert(item).inserted {
        uniqueCandidates.append(item)
    }

    let affordable = remaining / perNodeBytes
    let targetCount = min(maxPerTick, affordable, uniqueCandidates.count)
    guard targetCount > 0 else {
        return TelemetryPatrolSelection(identities: [], defenderCount: 0, explorerCount: 0, remainingQuotaBytes: remaining)
    }

    let summaries7d = telemetryComputeSummaries(samples: samples, healthEvents: healthEvents, window: .days7, now: now)
        .filter { seen.contains($0.identity) && $0.successCount > 0 }

    let defenderCap = targetCount <= 1 ? 0 : min(4, targetCount / 2)
    var defenders: [TelemetryIdentity] = []
    var pickedSet = Set<TelemetryIdentity>()
    for summary in summaries7d {
        if defenders.count >= defenderCap { break }
        if pickedSet.insert(summary.identity).inserted {
            defenders.append(summary.identity)
        }
    }

    var lastSampleByIdentity: [TelemetryIdentity: TimeInterval] = [:]
    for s in samples where s.timestamp.isFinite && s.timestamp <= now + 60.0 {
        if let prev = lastSampleByIdentity[s.identity] {
            if s.timestamp > prev { lastSampleByIdentity[s.identity] = s.timestamp }
        } else {
            lastSampleByIdentity[s.identity] = s.timestamp
        }
    }

    let explorerCandidates = uniqueCandidates.enumerated().filter { !pickedSet.contains($0.element) }
        .sorted { lhs, rhs in
            let lt = lastSampleByIdentity[lhs.element]
            let rt = lastSampleByIdentity[rhs.element]
            switch (lt, rt) {
            case (nil, nil): return lhs.offset < rhs.offset
            case (nil, _?): return true
            case (_?, nil): return false
            case let (a?, b?):
                if a != b { return a < b }
                return lhs.offset < rhs.offset
            }
        }

    var explorers: [TelemetryIdentity] = []
    let remainingSlots = targetCount - defenders.count
    for item in explorerCandidates.prefix(max(0, remainingSlots)) {
        if pickedSet.insert(item.element).inserted {
            explorers.append(item.element)
        }
    }

    return TelemetryPatrolSelection(
        identities: defenders + explorers,
        defenderCount: defenders.count,
        explorerCount: explorers.count,
        remainingQuotaBytes: remaining
    )
}

private final class TelemetryMainCallback<Value>: @unchecked Sendable {
    private let callback: (Value) -> Void
    init(_ callback: @escaping (Value) -> Void) { self.callback = callback }
    func deliver(_ value: Value) { DispatchQueue.main.async { self.callback(value) } }
}

public final class TelemetryHistoryStore: @unchecked Sendable {
    public let storagePath: String?
    public let isInMemoryFallback: Bool

    private let queue = DispatchQueue(label: "local.clashverge.telemetry.store", qos: .utility)
    private let retentionDays: Int
    private let nowProvider: @Sendable () -> TimeInterval
    private var db: OpaquePointer?

    public init(
        directoryURL: URL? = nil,
        retentionDays: Int = 30,
        nowProvider: @escaping @Sendable () -> TimeInterval = { Date().timeIntervalSince1970 }
    ) {
        self.retentionDays = max(1, retentionDays)
        self.nowProvider = nowProvider

        let resolvedDir = directoryURL ?? Self.defaultDirectoryURL()
        var openedDB: OpaquePointer?
        var resolvedFilePath: String?

        if let dirURL = resolvedDir, let safeDBPath = Self.prepareSafeSQLiteFile(in: dirURL) {
            var candidate: OpaquePointer?
            let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX
            if sqlite3_open_v2(safeDBPath, &candidate, flags, nil) == SQLITE_OK, candidate != nil {
                openedDB = candidate
                resolvedFilePath = safeDBPath
            } else {
                if candidate != nil { sqlite3_close(candidate) }
            }
        }

        if openedDB == nil {
            var mem: OpaquePointer?
            let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
            _ = sqlite3_open_v2(":memory:", &mem, flags, nil)
            self.db = mem
            self.storagePath = nil
            self.isInMemoryFallback = true
        } else {
            self.db = openedDB
            self.storagePath = resolvedFilePath
            self.isInMemoryFallback = false
        }

        queue.sync {
            self.configureSchemaAndPrune()
        }
    }

    deinit {
        if let db {
            sqlite3_close_v2(db)
        }
    }

    private static func defaultDirectoryURL() -> URL? {
        let homePath: String
        if let envHome = ProcessInfo.processInfo.environment["HOME"],
           !envHome.isEmpty, envHome.hasPrefix("/"), !envHome.utf8.contains(0) {
            homePath = envHome
        } else {
            let fallback = FileManager.default.homeDirectoryForCurrentUser.path
            guard !fallback.isEmpty, fallback.hasPrefix("/"), !fallback.utf8.contains(0) else { return nil }
            homePath = fallback
        }
        return URL(fileURLWithPath: homePath, isDirectory: true)
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("local.clashverge.multispeedhelper", isDirectory: true)
    }

    private static func prepareSafeSQLiteFile(in dirURL: URL) -> String? {
        guard dirURL.isFileURL, !dirURL.path.isEmpty, dirURL.path.hasPrefix("/"), !dirURL.path.utf8.contains(0) else {
            return nil
        }

        let targetPath = dirURL.path
        let components = targetPath.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard !components.isEmpty else { return nil }

        // Ensure Library/Application Support/local.clashverge.multispeedhelper components
        // exist without traversing symlinks for any created or final directory.
        let parentComponents = Array(components.dropLast())
        var running = ""
        for (idx, comp) in parentComponents.enumerated() {
            running += "/" + comp
            var st = stat()
            if lstat(running, &st) == 0 {
                let mode = st.st_mode & mode_t(S_IFMT)
                if mode == mode_t(S_IFLNK) {
                    // On macOS /tmp -> /private/tmp and /var -> /private/var at depth 0 are system symlinks;
                    // reject symlinks anywhere deeper (e.g. inside HOME/Library).
                    if idx > 0 { return nil }
                    var resolved = stat()
                    if stat(running, &resolved) != 0 || (resolved.st_mode & mode_t(S_IFMT)) != mode_t(S_IFDIR) {
                        return nil
                    }
                } else if mode != mode_t(S_IFDIR) {
                    return nil
                }
            } else if errno == ENOENT {
                if mkdir(running, 0o700) != 0 && errno != EEXIST {
                    return nil
                }
                var created = stat()
                if lstat(running, &created) != 0 || (created.st_mode & mode_t(S_IFMT)) != mode_t(S_IFDIR) {
                    return nil
                }
            } else {
                return nil
            }
        }

        let finalPath = running + "/" + components[components.count - 1]
        var dirStat = stat()
        if lstat(finalPath, &dirStat) != 0 {
            guard errno == ENOENT else { return nil }
            guard mkdir(finalPath, 0o700) == 0 || errno == EEXIST else { return nil }
        }

        let dirFD = Darwin.open(finalPath, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dirFD >= 0 else { return nil }
        defer { Darwin.close(dirFD) }

        var openedDir = stat()
        guard fstat(dirFD, &openedDir) == 0,
              (openedDir.st_mode & mode_t(S_IFMT)) == mode_t(S_IFDIR),
              openedDir.st_uid == geteuid() else {
            return nil
        }
        if (openedDir.st_mode & 0o077) != 0 {
            guard fchmod(dirFD, 0o700) == 0 else { return nil }
        }

        let dbName = "telemetry-v1.sqlite"
        for sidecar in ["telemetry-v1.sqlite-wal", "telemetry-v1.sqlite-shm", "telemetry-v1.sqlite-journal"] {
            var st = stat()
            if fstatat(dirFD, sidecar, &st, AT_SYMLINK_NOFOLLOW) == 0 {
                guard (st.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG), st.st_uid == geteuid() else {
                    return nil
                }
            } else if errno != ENOENT {
                return nil
            }
        }

        var fileFD = openat(dirFD, dbName, O_RDWR | O_NOFOLLOW | O_CLOEXEC)
        if fileFD < 0 {
            guard errno == ENOENT else { return nil }
            fileFD = openat(dirFD, dbName, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fileFD >= 0 else { return nil }
        }
        defer { Darwin.close(fileFD) }

        var fileStat = stat()
        guard fstat(fileFD, &fileStat) == 0,
              (fileStat.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG),
              fileStat.st_uid == geteuid() else {
            return nil
        }
        if (fileStat.st_mode & 0o177) != 0 {
            _ = fchmod(fileFD, 0o600)
        }

        return finalPath + "/" + dbName
    }

    private func execSQL(_ sql: String) {
        guard let db else { return }
        _ = sqlite3_exec(db, sql, nil, nil, nil)
    }

    private func configureSchemaAndPrune() {
        execSQL("PRAGMA busy_timeout=2000;")
        execSQL("PRAGMA journal_mode=DELETE;")
        execSQL("PRAGMA secure_delete=ON;")
        execSQL("""
        CREATE TABLE IF NOT EXISTS telemetry_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            profile_uid TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            node_name TEXT NOT NULL,
            node_type TEXT NOT NULL,
            source TEXT NOT NULL,
            requested_bytes INTEGER NOT NULL,
            streams INTEGER NOT NULL,
            download_complete INTEGER NOT NULL,
            bps REAL NOT NULL,
            latency_ms REAL NOT NULL,
            jitter_ms REAL NOT NULL,
            head_attempts INTEGER NOT NULL,
            head_successes INTEGER NOT NULL,
            composite_score REAL,
            is_peak_hour INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_samples_ts ON telemetry_samples(ts);
        CREATE INDEX IF NOT EXISTS idx_samples_identity_ts ON telemetry_samples(profile_uid, node_name, ts);
        CREATE TABLE IF NOT EXISTS health_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            profile_uid TEXT NOT NULL,
            node_name TEXT NOT NULL,
            group_name TEXT NOT NULL,
            ok INTEGER NOT NULL,
            delay_ms REAL,
            failure_kind TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_health_ts ON health_events(ts);
        CREATE INDEX IF NOT EXISTS idx_health_identity_ts ON health_events(profile_uid, node_name, ts);
        """)
        let cutoff = nowProvider() - Double(retentionDays) * 86400.0
        pruneLocked(olderThan: cutoff)
    }

    private static func isSafeText(_ text: String, maxBytes: Int = 4096) -> Bool {
        !text.utf8.contains(0) && text.utf8.count <= maxBytes
    }

    private static func bindUTF8(_ stmt: OpaquePointer?, index: Int32, text: String) {
        let bytes = Array(text.utf8)
        if bytes.isEmpty {
            sqlite3_bind_text(stmt, index, "", 0, sqliteTransient)
        } else {
            bytes.withUnsafeBufferPointer { buf in
                buf.baseAddress?.withMemoryRebound(to: CChar.self, capacity: bytes.count) { ptr in
                    _ = sqlite3_bind_text(stmt, index, ptr, Int32(bytes.count), sqliteTransient)
                }
            }
        }
    }

    private static func columnUTF8(_ stmt: OpaquePointer?, index: Int32) -> String {
        guard let ptr = sqlite3_column_text(stmt, index) else { return "" }
        let len = Int(sqlite3_column_bytes(stmt, index))
        guard len > 0 else { return "" }
        let data = Data(bytes: ptr, count: len)
        return String(decoding: data, as: UTF8.self)
    }

    private func pruneLocked(olderThan cutoff: TimeInterval) {
        guard let db, cutoff.isFinite else { return }
        for table in ["telemetry_samples", "health_events"] {
            var stmt: OpaquePointer?
            if sqlite3_prepare_v2(db, "DELETE FROM \(table) WHERE ts < ?;", -1, &stmt, nil) == SQLITE_OK, let stmt {
                sqlite3_bind_double(stmt, 1, cutoff)
                _ = sqlite3_step(stmt)
                sqlite3_finalize(stmt)
            }
        }
    }

    private func insertSampleLocked(_ sample: TelemetrySampleRecord) {
        guard let db,
              sample.timestamp.isFinite,
              !sample.profileUID.isEmpty, !sample.nodeName.isEmpty,
              Self.isSafeText(sample.profileUID), Self.isSafeText(sample.profileName),
              Self.isSafeText(sample.nodeName), Self.isSafeText(sample.nodeType) else { return }

        let sql = """
        INSERT INTO telemetry_samples (
            ts, profile_uid, profile_name, node_name, node_type, source,
            requested_bytes, streams, download_complete, bps, latency_ms,
            jitter_ms, head_attempts, head_successes, composite_score, is_peak_hour
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return }
        defer { sqlite3_finalize(stmt) }

        sqlite3_bind_double(stmt, 1, sample.timestamp)
        Self.bindUTF8(stmt, index: 2, text: sample.profileUID)
        Self.bindUTF8(stmt, index: 3, text: sample.profileName)
        Self.bindUTF8(stmt, index: 4, text: sample.nodeName)
        Self.bindUTF8(stmt, index: 5, text: sample.nodeType)
        Self.bindUTF8(stmt, index: 6, text: sample.source.rawValue)
        sqlite3_bind_int64(stmt, 7, Int64(max(0, sample.requestedBytes)))
        sqlite3_bind_int64(stmt, 8, Int64(max(0, sample.streams)))
        sqlite3_bind_int(stmt, 9, sample.downloadComplete ? 1 : 0)
        sqlite3_bind_double(stmt, 10, sample.downloadBytesPerSecond.isFinite ? max(0, sample.downloadBytesPerSecond) : 0)
        sqlite3_bind_double(stmt, 11, sample.latencyMilliseconds.isFinite ? max(0, sample.latencyMilliseconds) : 0)
        sqlite3_bind_double(stmt, 12, sample.jitterMilliseconds.isFinite ? max(0, sample.jitterMilliseconds) : 0)
        sqlite3_bind_int64(stmt, 13, Int64(max(0, sample.headAttempts)))
        sqlite3_bind_int64(stmt, 14, Int64(max(0, sample.headSuccesses)))
        if let score = sample.compositeScore, score.isFinite {
            sqlite3_bind_double(stmt, 15, score)
        } else {
            sqlite3_bind_null(stmt, 15)
        }
        sqlite3_bind_int(stmt, 16, sample.isPeakHour ? 1 : 0)
        _ = sqlite3_step(stmt)

        let cutoff = nowProvider() - Double(retentionDays) * 86400.0
        pruneLocked(olderThan: cutoff)
    }

    private func insertHealthEventLocked(_ event: TelemetryHealthEvent) {
        guard let db,
              event.timestamp.isFinite,
              !event.profileUID.isEmpty, !event.nodeName.isEmpty,
              Self.isSafeText(event.profileUID), Self.isSafeText(event.nodeName),
              Self.isSafeText(event.groupName) else { return }

        let sql = """
        INSERT INTO health_events (
            ts, profile_uid, node_name, group_name, ok, delay_ms, failure_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return }
        defer { sqlite3_finalize(stmt) }

        sqlite3_bind_double(stmt, 1, event.timestamp)
        Self.bindUTF8(stmt, index: 2, text: event.profileUID)
        Self.bindUTF8(stmt, index: 3, text: event.nodeName)
        Self.bindUTF8(stmt, index: 4, text: event.groupName)
        sqlite3_bind_int(stmt, 5, event.ok ? 1 : 0)
        if let delay = event.delayMilliseconds, delay.isFinite {
            sqlite3_bind_double(stmt, 6, delay)
        } else {
            sqlite3_bind_null(stmt, 6)
        }
        if let kind = event.failureKind, Self.isSafeText(kind, maxBytes: 128) {
            Self.bindUTF8(stmt, index: 7, text: kind)
        } else {
            sqlite3_bind_null(stmt, 7)
        }
        _ = sqlite3_step(stmt)

        let cutoff = nowProvider() - Double(retentionDays) * 86400.0
        pruneLocked(olderThan: cutoff)
    }

    private func loadSamplesLocked(since cutoff: TimeInterval) -> [TelemetrySampleRecord] {
        guard let db else { return [] }
        let sql = """
        SELECT id, ts, profile_uid, profile_name, node_name, node_type, source,
               requested_bytes, streams, download_complete, bps, latency_ms,
               jitter_ms, head_attempts, head_successes, composite_score, is_peak_hour
        FROM telemetry_samples
        WHERE ts >= ?
        ORDER BY ts ASC, id ASC;
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return [] }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, cutoff)

        var out: [TelemetrySampleRecord] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let sourceStr = Self.columnUTF8(stmt, index: 6)
            guard let source = TelemetrySampleSource(rawValue: sourceStr) else { continue }
            let score: Double? = sqlite3_column_type(stmt, 15) == SQLITE_NULL ? nil : sqlite3_column_double(stmt, 15)
            out.append(TelemetrySampleRecord(
                id: sqlite3_column_int64(stmt, 0),
                timestamp: sqlite3_column_double(stmt, 1),
                profileUID: Self.columnUTF8(stmt, index: 2),
                profileName: Self.columnUTF8(stmt, index: 3),
                nodeName: Self.columnUTF8(stmt, index: 4),
                nodeType: Self.columnUTF8(stmt, index: 5),
                source: source,
                requestedBytes: Int(sqlite3_column_int64(stmt, 7)),
                streams: Int(sqlite3_column_int64(stmt, 8)),
                downloadComplete: sqlite3_column_int(stmt, 9) != 0,
                downloadBytesPerSecond: sqlite3_column_double(stmt, 10),
                latencyMilliseconds: sqlite3_column_double(stmt, 11),
                jitterMilliseconds: sqlite3_column_double(stmt, 12),
                headAttempts: Int(sqlite3_column_int64(stmt, 13)),
                headSuccesses: Int(sqlite3_column_int64(stmt, 14)),
                compositeScore: score,
                isPeakHour: sqlite3_column_int(stmt, 16) != 0
            ))
        }
        return out
    }

    private func loadHealthEventsLocked(since cutoff: TimeInterval) -> [TelemetryHealthEvent] {
        guard let db else { return [] }
        let sql = """
        SELECT id, ts, profile_uid, node_name, group_name, ok, delay_ms, failure_kind
        FROM health_events
        WHERE ts >= ?
        ORDER BY ts ASC, id ASC;
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return [] }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, cutoff)

        var out: [TelemetryHealthEvent] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let delay: Double? = sqlite3_column_type(stmt, 6) == SQLITE_NULL ? nil : sqlite3_column_double(stmt, 6)
            let kind: String? = sqlite3_column_type(stmt, 7) == SQLITE_NULL ? nil : Self.columnUTF8(stmt, index: 7)
            out.append(TelemetryHealthEvent(
                id: sqlite3_column_int64(stmt, 0),
                timestamp: sqlite3_column_double(stmt, 1),
                profileUID: Self.columnUTF8(stmt, index: 2),
                nodeName: Self.columnUTF8(stmt, index: 3),
                groupName: Self.columnUTF8(stmt, index: 4),
                ok: sqlite3_column_int(stmt, 5) != 0,
                delayMilliseconds: delay,
                failureKind: kind
            ))
        }
        return out
    }

    public func recordSample(_ sample: TelemetrySampleRecord) {
        queue.sync { self.insertSampleLocked(sample) }
    }

    public func recordSampleAsync(_ sample: TelemetrySampleRecord) {
        queue.async { self.insertSampleLocked(sample) }
    }

    public func recordHealthEvent(_ event: TelemetryHealthEvent) {
        queue.sync { self.insertHealthEventLocked(event) }
    }

    public func recordHealthEventAsync(_ event: TelemetryHealthEvent, completion: (() -> Void)? = nil) {
        let cb = completion.map { fn in TelemetryMainCallback<Void> { _ in fn() } }
        queue.async {
            self.insertHealthEventLocked(event)
            cb?.deliver(())
        }
    }

    public func computeSummaries(window: TelemetryWindow, now: TimeInterval? = nil) -> [TelemetryNodeSummary] {
        queue.sync {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
            let health = self.loadHealthEventsLocked(since: cutoff)
            return telemetryComputeSummaries(samples: samples, healthEvents: health, window: window, now: effectiveNow)
        }
    }

    public func computeSummariesAsync(
        window: TelemetryWindow,
        now: TimeInterval? = nil,
        completion: @escaping ([TelemetryNodeSummary]) -> Void
    ) {
        let cb = TelemetryMainCallback(completion)
        queue.async {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
            let health = self.loadHealthEventsLocked(since: cutoff)
            let summaries = telemetryComputeSummaries(samples: samples, healthEvents: health, window: window, now: effectiveNow)
            cb.deliver(summaries)
        }
    }

    public func selectPatrolPlan(
        candidates: [TelemetryIdentity],
        dailyQuotaBytes: Int,
        perNodeBytes: Int = 1_048_576,
        maxPerTick: Int = 8,
        now: TimeInterval? = nil,
        calendar: Calendar = .current
    ) -> TelemetryPatrolSelection {
        queue.sync {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
            let health = self.loadHealthEventsLocked(since: cutoff)
            return telemetrySelectPatrolIdentities(
                candidates: candidates,
                samples: samples,
                healthEvents: health,
                dailyQuotaBytes: dailyQuotaBytes,
                perNodeBytes: perNodeBytes,
                maxPerTick: maxPerTick,
                now: effectiveNow,
                calendar: calendar
            )
        }
    }

    public func selectPatrolPlanAsync(
        candidates: [TelemetryIdentity],
        dailyQuotaBytes: Int,
        perNodeBytes: Int = 1_048_576,
        maxPerTick: Int = 8,
        now: TimeInterval? = nil,
        calendar: Calendar = .current,
        completion: @escaping (TelemetryPatrolSelection) -> Void
    ) {
        let cb = TelemetryMainCallback(completion)
        queue.async {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
            let health = self.loadHealthEventsLocked(since: cutoff)
            let plan = telemetrySelectPatrolIdentities(
                candidates: candidates,
                samples: samples,
                healthEvents: health,
                dailyQuotaBytes: dailyQuotaBytes,
                perNodeBytes: perNodeBytes,
                maxPerTick: maxPerTick,
                now: effectiveNow,
                calendar: calendar
            )
            cb.deliver(plan)
        }
    }

    public func patrolBytesUsedToday(now: TimeInterval? = nil, calendar: Calendar = .current) -> Int {
        queue.sync {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
            return telemetryPatrolBytesUsedToday(samples: samples, now: effectiveNow, calendar: calendar)
        }
    }

    public func healthState(for identity: TelemetryIdentity, now: TimeInterval? = nil) -> TelemetryHealthState {
        queue.sync {
            let effectiveNow = now ?? self.nowProvider()
            let cutoff = effectiveNow - Double(self.retentionDays) * 86400.0
            let samples = self.loadSamplesLocked(since: cutoff)
                .filter { $0.identity == identity && $0.isSuccess }
                .map(\.timestamp)
            let events = self.loadHealthEventsLocked(since: cutoff)
                .filter { $0.identity == identity }
            return telemetryComputeHealthState(healthEvents: events, successSampleTimestamps: samples, now: effectiveNow)
        }
    }

    public func sampleCount() -> Int {
        queue.sync {
            guard let db else { return 0 }
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM telemetry_samples;", -1, &stmt, nil) == SQLITE_OK, let stmt else { return 0 }
            defer { sqlite3_finalize(stmt) }
            return sqlite3_step(stmt) == SQLITE_ROW ? Int(sqlite3_column_int64(stmt, 0)) : 0
        }
    }

    public func healthEventCount() -> Int {
        queue.sync {
            guard let db else { return 0 }
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM health_events;", -1, &stmt, nil) == SQLITE_OK, let stmt else { return 0 }
            defer { sqlite3_finalize(stmt) }
            return sqlite3_step(stmt) == SQLITE_ROW ? Int(sqlite3_column_int64(stmt, 0)) : 0
        }
    }

    public func prune(olderThan cutoff: TimeInterval) {
        queue.sync {
            self.pruneLocked(olderThan: cutoff)
        }
    }

    public func clearAll() {
        queue.sync {
            self.execSQL("DELETE FROM telemetry_samples; DELETE FROM health_events; VACUUM;")
        }
    }

    public func clearAllAsync(completion: @escaping () -> Void) {
        let cb = TelemetryMainCallback(completion)
        queue.async {
            self.execSQL("DELETE FROM telemetry_samples; DELETE FROM health_events; VACUUM;")
            cb.deliver(())
        }
    }
}
