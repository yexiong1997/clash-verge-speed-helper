#!/usr/bin/env python3
"""Unit tests for TelemetryHistoryStore, scoring decay, patrol rotation, and health penalty."""
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

SWIFT_DRIVER = r'''
import Foundation

func check(_ ok: Bool, _ msg: String) {
    if !ok {
        fputs("FAIL: \(msg)\n", stderr)
        exit(1)
    }
}

func makeSample(
    ts: TimeInterval,
    uid: String = "uid-a",
    profileName: String = "SubA",
    node: String,
    nodeType: String = "VLESS",
    source: TelemetrySampleSource = .manual,
    bytes: Int = 3 * 1_048_576,
    complete: Bool = true,
    bps: Double = 10_485_760,
    latency: Double = 45,
    jitter: Double = 3,
    score: Double? = 82.0,
    isPeak: Bool? = false
) -> TelemetrySampleRecord {
    TelemetrySampleRecord(
        timestamp: ts,
        profileUID: uid,
        profileName: profileName,
        nodeName: node,
        nodeType: nodeType,
        source: source,
        requestedBytes: bytes,
        streams: bytes >= 20 * 1_048_576 ? 4 : 2,
        downloadComplete: complete,
        downloadBytesPerSecond: bps,
        latencyMilliseconds: latency,
        jitterMilliseconds: jitter,
        headAttempts: 6,
        headSuccesses: complete ? 6 : 0,
        compositeScore: score,
        isPeakHour: isPeak
    )
}

@main struct TelemetryTestDriver {
    static func main() throws {
        let input = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
        let action = input["action"] as! String
        let now: TimeInterval = 1_760_000_000

        switch action {
        case "scoring_qualification_and_shrinkage":
            let samples = [
                makeSample(ts: now - 300, node: "Fluke", bytes: 1_048_576, bps: 25_000_000, score: 98.0),
                makeSample(ts: now - 7200, node: "Steady", bytes: 3 * 1_048_576, bps: 15_000_000, score: 88.0),
                makeSample(ts: now - 3600, node: "Steady", bytes: 3 * 1_048_576, bps: 16_000_000, score: 88.0),
                makeSample(ts: now - 600, node: "Steady", bytes: 3 * 1_048_576, bps: 17_000_000, score: 88.0),
                makeSample(ts: now - 5000, node: "Unreliable", bytes: 3 * 1_048_576, bps: 18_000_000, score: 90.0),
                makeSample(ts: now - 4000, node: "Unreliable", bytes: 3 * 1_048_576, complete: false, bps: 0, latency: 0, jitter: 0, score: nil),
                makeSample(ts: now - 3000, node: "Unreliable", bytes: 3 * 1_048_576, complete: false, bps: 0, latency: 0, jitter: 0, score: nil),
            ]
            let summaries = telemetryComputeSummaries(samples: samples, healthEvents: [], window: .days7, now: now)
            check(summaries.count == 3, "expected 3 summaries")
            check(summaries[0].nodeName == "Steady", "qualified 3-sample Steady must rank #1 over 1-sample Fluke, got \(summaries[0].nodeName)")
            check(summaries[0].isQualified == true, "Steady must be qualified")
            let fluke = summaries.first(where: { $0.nodeName == "Fluke" })!
            check(!fluke.isQualified && fluke.baseScore < summaries[0].baseScore, "single 1M fluke should be shrunk below 3x 3M steady")
            let unrel = summaries.first(where: { $0.nodeName == "Unreliable" })!
            check(!unrel.isQualified && unrel.baseScore < 35.0, "unreliable 33% availability must be heavily penalized, got \(unrel.baseScore)")

        case "windows_peak_and_decay":
            var utcCal = Calendar(identifier: .gregorian)
            utcCal.timeZone = TimeZone(secondsFromGMT: 0)!
            let peakComps = DateComponents(year: 2026, month: 11, day: 10, hour: 20, minute: 30)
            let offComps = DateComponents(year: 2026, month: 11, day: 10, hour: 10, minute: 0)
            let peakTs = utcCal.date(from: peakComps)!.timeIntervalSince1970
            let offTs = utcCal.date(from: offComps)!.timeIntervalSince1970
            check(telemetryIsPeakHour(timestamp: peakTs, calendar: utcCal) == true, "20:30 must be peak hour")
            check(telemetryIsPeakHour(timestamp: offTs, calendar: utcCal) == false, "10:00 must not be peak hour")

            let samples = [
                makeSample(ts: now - 10 * 86400, node: "OldOnly", score: 90.0, isPeak: false),
                makeSample(ts: now - 4 * 86400, node: "WeekPeak", score: 85.0, isPeak: true),
                makeSample(ts: now - 3600, node: "FreshOff", score: 80.0, isPeak: false),
            ]
            let s24 = telemetryComputeSummaries(samples: samples, healthEvents: [], window: .hours24, now: now)
            check(s24.map(\.nodeName) == ["FreshOff"], "24h window must include only FreshOff")
            let s7 = telemetryComputeSummaries(samples: samples, healthEvents: [], window: .days7, now: now)
            check(Set(s7.map(\.nodeName)) == ["WeekPeak", "FreshOff"], "7d window must exclude 10d sample")
            let s30 = telemetryComputeSummaries(samples: samples, healthEvents: [], window: .days30, now: now)
            check(s30.count == 3, "30d window includes all 3")
            let sPeak = telemetryComputeSummaries(samples: samples, healthEvents: [], window: .peakHours, now: now)
            check(sPeak.map(\.nodeName) == ["WeekPeak"], "peakHours window includes only peak-hour samples")

            let mixed = [
                makeSample(ts: now - 6 * 86400, node: "Decayed", bytes: 1_048_576, bps: 2_000_000, score: 40.0),
                makeSample(ts: now - 600, node: "Decayed", bytes: 20 * 1_048_576, bps: 20_000_000, score: 92.0),
                makeSample(ts: now - 300, node: "Decayed", bytes: 20 * 1_048_576, bps: 20_000_000, score: 92.0),
            ]
            let decayedSummary = telemetryComputeSummaries(samples: mixed, healthEvents: [], window: .days7, now: now).first!
            check(decayedSummary.weightedMedianScore > 88.0, "recent 20M samples must dominate decayed old 1M sample, got \(decayedSummary.weightedMedianScore)")

        case "health_penalty_and_recovery":
            let baseSamples = [
                makeSample(ts: now - 7200, node: "A", score: 88.0),
                makeSample(ts: now - 5400, node: "A", score: 88.0),
                makeSample(ts: now - 3600, node: "A", score: 88.0),
                makeSample(ts: now - 7200, node: "B", score: 84.0),
                makeSample(ts: now - 5400, node: "B", score: 84.0),
                makeSample(ts: now - 3600, node: "B", score: 84.0),
            ]
            let oneFail = [TelemetryHealthEvent(timestamp: now - 120, profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed")]
            let afterOne = telemetryComputeSummaries(samples: baseSamples, healthEvents: oneFail, window: .days7, now: now)
            check(afterOne.first(where: { $0.nodeName == "A" })!.healthPenalty == 0.0, "single probe glitch must not penalize")

            let threeFails = [
                TelemetryHealthEvent(timestamp: now - 180, profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"),
                TelemetryHealthEvent(timestamp: now - 120, profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"),
                TelemetryHealthEvent(timestamp: now - 60, profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"),
            ]
            let penalized = telemetryComputeSummaries(samples: baseSamples, healthEvents: threeFails, window: .days7, now: now)
            let nodeA = penalized.first(where: { $0.nodeName == "A" })!
            check(nodeA.healthPenalty >= 10.0 && nodeA.healthPenalty <= 25.0, "3 consecutive failures should penalize 10..25 pts, got \(nodeA.healthPenalty)")
            check(penalized[0].nodeName == "B", "healthy B (84) must rank above penalized A, got \(penalized[0].nodeName)")

            let recoveredByProbe = threeFails + [
                TelemetryHealthEvent(timestamp: now - 15, profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: true, delayMilliseconds: 32, failureKind: nil)
            ]
            let afterProbe = telemetryComputeSummaries(samples: baseSamples, healthEvents: recoveredByProbe, window: .days7, now: now)
            check(afterProbe.first(where: { $0.nodeName == "A" })!.healthPenalty == 0.0, "healthy probe must clear penalty")

            let recoveredSamples = baseSamples + [makeSample(ts: now - 10, node: "A", score: 88.0)]
            let afterSpeed = telemetryComputeSummaries(samples: recoveredSamples, healthEvents: threeFails, window: .days7, now: now)
            check(afterSpeed.first(where: { $0.nodeName == "A" })!.healthPenalty == 0.0, "newer successful speed test must clear health penalty")

            let twentyFails = (0..<20).map { i in
                TelemetryHealthEvent(timestamp: now - Double(600 - i * 10), profileUID: "uid-a", nodeName: "A", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed")
            }
            let capped = telemetryComputeSummaries(samples: baseSamples, healthEvents: twentyFails, window: .days7, now: now)
            check(capped.first(where: { $0.nodeName == "A" })!.healthPenalty == 25.0, "penalty must be capped at 25.0")

        case "patrol_rotation_and_quota":
            var utcCal = Calendar(identifier: .gregorian)
            utcCal.timeZone = TimeZone(secondsFromGMT: 0)!
            let candidates = (1...12).map { TelemetryIdentity(profileUID: "uid-a", nodeName: String(format: "N%02d", $0)) }
            var samples: [TelemetrySampleRecord] = []
            for i in 1...5 {
                let name = String(format: "N%02d", i)
                for j in 0..<3 {
                    samples.append(makeSample(ts: now - Double(7200 + j * 1000 + i * 60), node: name, score: Double(95 - i * 3)))
                }
            }
            for i in 6...8 {
                let name = String(format: "N%02d", i)
                samples.append(makeSample(ts: now - 86400, node: name, source: .patrol, bytes: 1_048_576, score: 65))
            }
            let plan = telemetrySelectPatrolIdentities(candidates: candidates, samples: samples, healthEvents: [], dailyQuotaBytes: 50 * 1_048_576, now: now, calendar: utcCal)
            check(plan.identities.count == 8, "patrol tick must pick up to 8 nodes, got \(plan.identities.count)")
            check(plan.defenderCount == 4 && plan.explorerCount == 4, "expected 4 defenders + 4 explorers, got \(plan.defenderCount)+\(plan.explorerCount)")
            let pickedNames = plan.identities.map { String(decoding: $0.node, as: UTF8.self) }
            check(Array(pickedNames.prefix(4)) == ["N01", "N02", "N03", "N04"], "top 4 7d leaders must defend, got \(pickedNames)")
            check(Set(pickedNames.suffix(4)) == ["N09", "N10", "N11", "N12"], "unsampled nodes must be explored first, got \(pickedNames)")

            var quotaSamples = samples
            for i in 0..<47 {
                quotaSamples.append(makeSample(ts: now - Double(100 + i), node: "N06", source: .patrol, bytes: 1_048_576, score: 60))
            }
            let tightPlan = telemetrySelectPatrolIdentities(candidates: candidates, samples: quotaSamples, healthEvents: [], dailyQuotaBytes: 50 * 1_048_576, now: now, calendar: utcCal)
            check(tightPlan.identities.count == 3 && tightPlan.remainingQuotaBytes == 3 * 1_048_576, "remaining quota must cap batch to 3 nodes, got \(tightPlan.identities.count)")

            var fullSamples = quotaSamples
            for i in 0..<3 {
                fullSamples.append(makeSample(ts: now - Double(10 + i), node: "N07", source: .patrol, bytes: 1_048_576, score: 60))
            }
            let emptyPlan = telemetrySelectPatrolIdentities(candidates: candidates, samples: fullSamples, healthEvents: [], dailyQuotaBytes: 50 * 1_048_576, now: now, calendar: utcCal)
            check(emptyPlan.identities.isEmpty && emptyPlan.remainingQuotaBytes == 0, "exhausted quota must return 0 patrol nodes")

        case "sqlite_persistence_and_pruning":
            let dirPath = input["dir"] as! String
            let dirURL = URL(fileURLWithPath: dirPath, isDirectory: true)
            let store = TelemetryHistoryStore(directoryURL: dirURL, retentionDays: 30, nowProvider: { now })
            check(!store.isInMemoryFallback, "expected disk DB, got fallback")
            check(store.storagePath == dirPath + "/telemetry-v1.sqlite", "unexpected storagePath")
            store.recordSample(makeSample(ts: now - 100, uid: "\u{00E9}", profileName: "NFC", node: "\u{00E9}", score: 90.0))
            store.recordSample(makeSample(ts: now - 90, uid: "e\u{0301}", profileName: "NFD", node: "e\u{0301}", score: 75.0))
            store.recordSample(makeSample(ts: now - 31 * 86400, uid: "old", profileName: "Old", node: "Stale", score: 99.0))
            check(store.sampleCount() == 2, "31-day-old sample must be pruned automatically, count=\(store.sampleCount())")

            let reopened = TelemetryHistoryStore(directoryURL: dirURL, retentionDays: 30, nowProvider: { now })
            let sums = reopened.computeSummaries(window: .days7, now: now)
            check(sums.count == 2, "byte-distinct NFC vs NFD identities must stay separate, got \(sums.count)")
            reopened.clearAll()
            check(reopened.sampleCount() == 0 && reopened.healthEventCount() == 0, "clearAll must empty all tables")

        case "patrol_smart_optimizations":
            var utcCal = Calendar(identifier: .gregorian)
            utcCal.timeZone = TimeZone(secondsFromGMT: 0)!
            let overnightTs = utcCal.date(from: DateComponents(year: 2026, month: 11, day: 10, hour: 3, minute: 0))!.timeIntervalSince1970
            let dayTs = utcCal.date(from: DateComponents(year: 2026, month: 11, day: 10, hour: 14, minute: 0))!.timeIntervalSince1970
            let peakTs = utcCal.date(from: DateComponents(year: 2026, month: 11, day: 10, hour: 20, minute: 30))!.timeIntervalSince1970
            let postPeakTs = utcCal.date(from: DateComponents(year: 2026, month: 11, day: 10, hour: 23, minute: 45))!.timeIntervalSince1970

            // 1) Defender 90-min rotation + penalized flagship sorted by baseScore (not post-penalty score)
            //    + strict exclusion of cooled & backed-off dead nodes (even with spare maxPerTick slots!)
            let cands = ["FreshTop", "HourlyCooledTop", "ReadyTop", "PenalizedFlagship", "PenalizedMid", "Promising1", "DeadNode", "SteadyOld"].map {
                TelemetryIdentity(profileUID: "uid-a", nodeName: $0)
            }
            let smartSamples: [TelemetrySampleRecord] = [
                // FreshTop: tested 2 min ago -> strictly inside 15m/90m cooldown
                makeSample(ts: dayTs - 7200, node: "FreshTop", score: 96),
                makeSample(ts: dayTs - 3600, node: "FreshTop", score: 96),
                makeSample(ts: dayTs - 120, node: "FreshTop", score: 96),
                // HourlyCooledTop: tested 60 min ago -> inside 90m defender cooldown (yields defender slot to ReadyTop),
                // and >15m so eligible for Tier 2 only if spare slots remain
                makeSample(ts: dayTs - 14400, node: "HourlyCooledTop", score: 94),
                makeSample(ts: dayTs - 10800, node: "HourlyCooledTop", score: 94),
                makeSample(ts: dayTs - 3600, node: "HourlyCooledTop", score: 94),
                // ReadyTop: tested 2h ago -> outside 90m cooldown, takes defender slot!
                makeSample(ts: dayTs - 14400, node: "ReadyTop", score: 89),
                makeSample(ts: dayTs - 10800, node: "ReadyTop", score: 89),
                makeSample(ts: dayTs - 7200, node: "ReadyTop", score: 89),
                // PenalizedFlagship: baseScore 92, heavy -25 penalty -> longTermScore 67, MUST beat PenalizedMid (baseScore 74, -5 penalty -> 69)!
                makeSample(ts: dayTs - 10800, node: "PenalizedFlagship", score: 92),
                makeSample(ts: dayTs - 7200, node: "PenalizedFlagship", score: 92),
                makeSample(ts: dayTs - 3600, node: "PenalizedFlagship", score: 92),
                // PenalizedMid: baseScore 74, minor penalty
                makeSample(ts: dayTs - 10800, node: "PenalizedMid", score: 74),
                makeSample(ts: dayTs - 7200, node: "PenalizedMid", score: 74),
                makeSample(ts: dayTs - 3600, node: "PenalizedMid", score: 74),
                // Promising1: 1 high-score success 30m ago -> Tier 1 promising explorer
                makeSample(ts: dayTs - 1800, node: "Promising1", score: 84),
                // DeadNode: 3 consecutive failures, latest 30m ago -> strictly excluded in 4h backoff!
                makeSample(ts: dayTs - 7200, node: "DeadNode", complete: false, bps: 0, latency: 0, jitter: 0, score: nil),
                makeSample(ts: dayTs - 3600, node: "DeadNode", complete: false, bps: 0, latency: 0, jitter: 0, score: nil),
                makeSample(ts: dayTs - 1800, node: "DeadNode", complete: false, bps: 0, latency: 0, jitter: 0, score: nil),
                // SteadyOld: tested 3h ago -> Tier 2 healthy rotation
                makeSample(ts: dayTs - 20000, node: "SteadyOld", score: 62),
                makeSample(ts: dayTs - 18000, node: "SteadyOld", score: 62),
                makeSample(ts: dayTs - 14000, node: "SteadyOld", score: 62),
                makeSample(ts: dayTs - 10800, node: "SteadyOld", score: 62),
            ]
            var penEvents: [TelemetryHealthEvent] = []
            // 6 failures on PenalizedFlagship (-25 pts) vs 2 failures on PenalizedMid (-5 pts)
            for i in 0..<6 {
                penEvents.append(TelemetryHealthEvent(timestamp: dayTs - Double(400 - i * 30), profileUID: "uid-a", nodeName: "PenalizedFlagship", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"))
            }
            for i in 0..<2 {
                penEvents.append(TelemetryHealthEvent(timestamp: dayTs - Double(300 - i * 30), profileUID: "uid-a", nodeName: "PenalizedMid", groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"))
            }
            let smartPlan = telemetrySelectPatrolIdentities(
                candidates: cands, samples: smartSamples, healthEvents: penEvents,
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 4, now: dayTs, calendar: utcCal
            )
            let smartNames = smartPlan.identities.map { String(decoding: $0.node, as: UTF8.self) }
            check(smartNames.count == 4, "expected 4 picks, got \(smartNames)")
            check(smartPlan.defenderCount == 2 && Array(smartNames.prefix(2)) == ["PenalizedFlagship", "ReadyTop"],
                  "penalized flagship (baseScore 92) + uncooled ReadyTop must defend, got \(smartNames)")
            check(!smartNames.contains("FreshTop"), "FreshTop inside 15m cooldown must be skipped, got \(smartNames)")
            check(!smartNames.contains("DeadNode"), "DeadNode inside 4h backoff must be strictly excluded, got \(smartNames)")
            check(smartNames.contains("Promising1"), "Promising1 (Tier 1) must be explored before Tier 2, got \(smartNames)")
            check(smartPlan.skippedBackoffCount == 1 && smartPlan.skippedCooldownCount == 1,
                  "expected 1 backoff skip + 1 cooldown skip, got backoff=\(smartPlan.skippedBackoffCount) cooldown=\(smartPlan.skippedCooldownCount)")

            // Even with maxPerTick=8 (more slots than candidates!), FreshTop & DeadNode MUST STILL be strictly excluded!
            let widePlan = telemetrySelectPatrolIdentities(
                candidates: cands, samples: smartSamples, healthEvents: penEvents,
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 8, now: dayTs, calendar: utcCal
            )
            let wideNames = widePlan.identities.map { String(decoding: $0.node, as: UTF8.self) }
            check(wideNames.count == 6 && !wideNames.contains("DeadNode") && !wideNames.contains("FreshTop"),
                  "wide batch must never leak cooled FreshTop or backed-off DeadNode, got \(wideNames)")

            // Once DeadNode's 4h backoff expires (5h later), DeadNode automatically re-enters recovery probing
            let afterBackoff = telemetrySelectPatrolIdentities(
                candidates: [TelemetryIdentity(profileUID: "uid-a", nodeName: "DeadNode")],
                samples: smartSamples, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 4, now: dayTs + 5 * 3600, calendar: utcCal
            )
            check(afterBackoff.identities.count == 1 && afterBackoff.skippedBackoffCount == 0,
                  "DeadNode must re-enter recovery probe once 4h backoff expires, got \(afterBackoff)")

            // 2) First peak-hour check bypasses daytime 90-min defender cooldown at 20:30 for leader tested at 19:50
            let peakLeaderCand = [TelemetryIdentity(profileUID: "uid-a", nodeName: "PeakHero")]
            let peakLeaderSamples = [
                makeSample(ts: peakTs - 10800, node: "PeakHero", score: 90),
                makeSample(ts: peakTs - 7200, node: "PeakHero", score: 90),
                makeSample(ts: peakTs - 2400, node: "PeakHero", score: 90), // 19:50 (40m ago, < 90m daytime cooldown, before 20:00!)
            ]
            let peakLeaderPlan = telemetrySelectPatrolIdentities(
                candidates: peakLeaderCand, samples: peakLeaderSamples, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 4, now: peakTs, calendar: utcCal
            )
            check(peakLeaderPlan.identities.count == 1,
                  "leader last tested before 20:00 must immediately qualify for peak-hour check, got \(peakLeaderPlan)")

            // 3) Multi-subscription round-robin cold start + cold-start graduation assist
            let multiSubCands = [
                TelemetryIdentity(profileUID: "sub-a", nodeName: "A1"),
                TelemetryIdentity(profileUID: "sub-a", nodeName: "A2"),
                TelemetryIdentity(profileUID: "sub-a", nodeName: "A3"),
                TelemetryIdentity(profileUID: "sub-b", nodeName: "B1"),
                TelemetryIdentity(profileUID: "sub-b", nodeName: "B2"),
                TelemetryIdentity(profileUID: "sub-c", nodeName: "C1"),
            ]
            let rrPlan = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: [], healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 4, now: dayTs, calendar: utcCal
            )
            let rrNames = rrPlan.identities.map { String(decoding: $0.node, as: UTF8.self) }
            check(rrNames == ["A1", "B1", "C1", "A2"],
                  "multi-subscription cold start must round-robin across profiles, got \(rrNames)")

            // Cold-start graduation assist: A1 & B1 each got 1 high-score sample 1h ago, 0 qualified yet;
            // next tick (maxPerTick=4) graduates A1 & B1 while STILL discovering 2 new unsampled nodes (C1, A2)!
            let gradSamples = [
                makeSample(ts: dayTs - 3600, uid: "sub-a", profileName: "A", node: "A1", score: 86),
                makeSample(ts: dayTs - 3600, uid: "sub-b", profileName: "B", node: "B1", score: 82),
            ]
            let gradPlan = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: gradSamples, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 4, now: dayTs, calendar: utcCal
            )
            let gradNames = gradPlan.identities.map { String(decoding: $0.node, as: UTF8.self) }
            check(gradPlan.defenderCount == 0 && gradNames.count == 4 &&
                  Set(gradNames.prefix(2)) == ["A1", "B1"] && Set(gradNames.suffix(2)) == ["C1", "A2"],
                  "cold-start must graduate promising Tier 1 nodes while still discovering Tier 0, got \(gradNames)")

            // 4) Fair failure quota accounting: 4 failed 1MiB patrol probes charge 4 * 256KiB = 1MiB
            let failPatrolSamples = (0..<4).map { i in
                makeSample(ts: dayTs - Double(600 + i * 10), node: "DeadNode", source: .patrol, bytes: 1_048_576, complete: false, bps: 0, latency: 0, jitter: 0, score: nil)
            }
            let usedByFailures = telemetryPatrolBytesUsedToday(samples: failPatrolSamples, now: dayTs, calendar: utcCal)
            check(usedByFailures == 1_048_576, "4 failed 1M patrol probes must charge 1 MiB anti-spin budget, got \(usedByFailures)")

            // 5) Peak 25% reserve: held at 14:00, unlocked at 20:30, AND stays unlocked at 23:45 (post-peak regression!)
            var daytimeSpent: [TelemetrySampleRecord] = []
            for i in 0..<38 {
                daytimeSpent.append(makeSample(ts: dayTs - Double(1000 + i * 10), node: "ReadyTop", source: .patrol, bytes: 1_048_576, score: 85))
            }
            let dayDeferred = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: daytimeSpent, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 8, now: dayTs, calendar: utcCal, reservePeakRatio: 0.25
            )
            check(dayDeferred.identities.isEmpty && dayDeferred.deferredForPeakQuota && dayDeferred.peakReservedBytes > 0,
                  "daytime (14:00) must hold last 25% quota for evening peak, got \(dayDeferred)")
            let peakUnlocked = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: daytimeSpent, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 8, now: peakTs, calendar: utcCal, reservePeakRatio: 0.25
            )
            check(peakUnlocked.identities.count == 6 && !peakUnlocked.deferredForPeakQuota && peakUnlocked.peakReservedBytes == 0,
                  "evening peak (20:30) must unlock reserved quota, got \(peakUnlocked)")
            let postPeakUnlocked = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: daytimeSpent, healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 8, now: postPeakTs, calendar: utcCal, reservePeakRatio: 0.25
            )
            check(postPeakUnlocked.identities.count == 6 && !postPeakUnlocked.deferredForPeakQuota && postPeakUnlocked.peakReservedBytes == 0,
                  "post-peak (23:45) must NOT re-lock peak reserve, got \(postPeakUnlocked)")

            // 6) Overnight throttle (01:00-07:00) halves batch cap to avoid burning daytime budget while asleep
            let nightPlan = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: [], healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 6, now: overnightTs, calendar: utcCal,
                reservePeakRatio: 0.25, overnightThrottle: true
            )
            check(nightPlan.effectiveMaxPerTick == 3 && nightPlan.identities.count == 3,
                  "overnight 03:00 with maxPerTick=6 must throttle to 3, got \(nightPlan)")
            let dayFullPlan = telemetrySelectPatrolIdentities(
                candidates: multiSubCands, samples: [], healthEvents: [],
                dailyQuotaBytes: 50 * 1_048_576, maxPerTick: 6, now: dayTs, calendar: utcCal,
                reservePeakRatio: 0.25, overnightThrottle: true
            )
            check(dayFullPlan.effectiveMaxPerTick == 6 && dayFullPlan.identities.count == 6,
                  "daytime 14:00 with maxPerTick=6 must use full 6, got \(dayFullPlan)")

        case "symlink_rejection_falls_back_to_memory":
            let dirPath = input["dir"] as! String
            let store = TelemetryHistoryStore(directoryURL: URL(fileURLWithPath: dirPath, isDirectory: true), nowProvider: { now })
            check(store.isInMemoryFallback && store.storagePath == nil, "symlinked directory must safely fall back to :memory:")
            store.recordSample(makeSample(ts: now - 10, node: "MemNode", score: 85))
            check(store.sampleCount() == 1, "in-memory fallback must remain functional")

        case "home_env_override":
            let store = TelemetryHistoryStore(nowProvider: { now })
            let expectedSuffix = "/Library/Application Support/local.clashverge.multispeedhelper/telemetry-v1.sqlite"
            check(!store.isInMemoryFallback, "HOME override should open disk store cleanly")
            check(store.storagePath?.hasSuffix(expectedSuffix) == true, "unexpected HOME-derived path: \(String(describing: store.storagePath))")
            store.recordSample(makeSample(ts: now - 5, node: "HomeNode", score: 81))
            check(store.sampleCount() == 1, "expected 1 sample")

        default:
            check(false, "unknown action \(action)")
        }
        print(String(data: try JSONSerialization.data(withJSONObject: ["ok": true]), encoding: .utf8)!)
    }
}
'''


class TelemetryHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='telemetry-unit-', dir='/private/tmp')
        cls.work = Path(cls.temp.name)
        cls.driver = cls.work / 'Driver.swift'
        cls.driver.write_text(SWIFT_DRIVER, encoding='utf-8')
        cls.binary = cls.work / 'telemetry-test'
        cls.env = os.environ.copy()
        cls.env.update(
            CLANG_MODULE_CACHE_PATH=str(cls.work / 'clang-cache'),
            SWIFT_MODULECACHE_PATH=str(cls.work / 'swift-cache'),
        )
        subprocess.run(
            ['/usr/bin/swiftc', '-swift-version', '5', '-target',
             f'{os.uname().machine}-apple-macos12.0', '-parse-as-library',
             str(PROJECT / 'Sources' / 'TelemetryHistory.swift'),
             str(cls.driver), '-o', str(cls.binary)],
            check=True, env=cls.env, timeout=120
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_action(self, payload, env_override=None):
        env = self.env.copy()
        if env_override:
            env.update(env_override)
        res = subprocess.run(
            [str(self.binary)],
            input=json.dumps(payload).encode('utf-8'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(res.stdout), {'ok': True})

    def test_scoring_qualification_and_shrinkage(self):
        self.run_action({'action': 'scoring_qualification_and_shrinkage'})

    def test_windows_peak_and_decay(self):
        self.run_action({'action': 'windows_peak_and_decay'})

    def test_health_penalty_and_recovery(self):
        self.run_action({'action': 'health_penalty_and_recovery'})

    def test_patrol_rotation_and_quota(self):
        self.run_action({'action': 'patrol_rotation_and_quota'})

    def test_sqlite_persistence_permissions_privacy_and_pruning(self):
        db_dir = self.work / 'disk-db'
        self.run_action({'action': 'sqlite_persistence_and_pruning', 'dir': str(db_dir)})
        db_file = db_dir / 'telemetry-v1.sqlite'
        self.assertTrue(db_file.exists())
        self.assertEqual(stat.S_IMODE(db_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(db_file.stat().st_mode), 0o600)
        con = sqlite3.connect(db_file)
        sample_cols = {row[1] for row in con.execute('PRAGMA table_info(telemetry_samples)').fetchall()}
        health_cols = {row[1] for row in con.execute('PRAGMA table_info(health_events)').fetchall()}
        con.close()
        forbidden = {'server', 'host', 'ip', 'port', 'uuid', 'password', 'url', 'yaml', 'secret'}
        self.assertTrue(sample_cols.isdisjoint(forbidden), sample_cols)
        self.assertTrue(health_cols.isdisjoint(forbidden), health_cols)

    def test_patrol_smart_optimizations(self):
        self.run_action({'action': 'patrol_smart_optimizations'})

    def test_symlink_rejection_falls_back_to_memory(self):
        real_dir = self.work / 'real-target'
        real_dir.mkdir(mode=0o700)
        link_dir = self.work / 'symlink-dir'
        link_dir.symlink_to(real_dir, target_is_directory=True)
        self.run_action({'action': 'symlink_rejection_falls_back_to_memory', 'dir': str(link_dir)})
        self.assertFalse((real_dir / 'telemetry-v1.sqlite').exists())

    def test_home_env_override(self):
        home_dir = self.work / 'fake-home'
        home_dir.mkdir(mode=0o700)
        self.run_action({'action': 'home_env_override'}, env_override={'HOME': str(home_dir)})
        expected = home_dir / 'Library' / 'Application Support' / 'local.clashverge.multispeedhelper' / 'telemetry-v1.sqlite'
        self.assertTrue(expected.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
