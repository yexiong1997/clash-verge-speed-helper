#!/usr/bin/env python3
"""Headless synthetic-only tests for MultiMain; no app, worker, network or real config.

Run on macOS: python3 -B Tests/test_multi_app_ui.py
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parent.parent
DRIVER = r'''
import AppKit
import Foundation
import Darwin

private final class FixtureEvents: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []
    func add(_ value: String) { lock.lock(); values.append(value); lock.unlock() }
    func snapshot() -> [String] { lock.lock(); defer { lock.unlock() }; return values }
}

private func fixtureLuminance(_ color: NSColor) -> Double {
    let c = color.usingColorSpace(.sRGB)!
    func linear(_ v: CGFloat) -> Double {
        let v = Double(v)
        return v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
    }
    return 0.2126 * linear(c.redComponent) + 0.7152 * linear(c.greenComponent) + 0.0722 * linear(c.blueComponent)
}

// In-memory catalogue only: no real profiles, controller, worker or network.
// Both profile IDs and node names contain canonically equivalent UTF-8 pairs.
private func fixtureBatch() -> MultiPreparedBatch {
    let first = ProfileOption(uid: "é", name: "First", path: "/fixture/first", isCurrent: true)
    let second = ProfileOption(uid: "é", name: "Second", path: "/fixture/second", isCurrent: false)
    let version = MultiFileVersion(stat())
    let profiles = [
        MultiProfileSnapshot(option: first, data: Data("first snapshot".utf8), version: version,
                             names: ["same", "é", "é", "Alpha"]),
        MultiProfileSnapshot(option: second, data: Data("second snapshot".utf8), version: version,
                             names: ["same", "é", "é", "Bravo"])
    ]
    return MultiPreparedBatch(registry: [first, second], registryData: Data("fixture".utf8),
        registryVersion: version, profiles: profiles,
        options: MultiBatchOptions(downloadBytes: 262144, streams: 2, latencyTimeout: "1s", downloadTimeout: "5s"),
        parallel: 10, jobCount: 8, requestedBytes: 8 * 262144, previewOnly: true)
}

private func fixtureMeasurement(_ name: String) -> VerifiedMeasurement {
    VerifiedMeasurement(nodeName: name, nodeType: "fixture", headAttempts: 6, headSuccesses: 6,
        latencyMilliseconds: 50, jitterMilliseconds: 2, expectedBytes: 262144,
        receivedBytes: 262144, downloadComplete: true, downloadBytesPerSecond: 1048576)
}

private func fixtureRows(_ rows: [MultiRow]) -> [[String: Any]] {
    rows.map { row in
        ["profile": row.identity.profile.map { String(format: "%02x", $0) }.joined(),
         "node": row.identity.node.map { String(format: "%02x", $0) }.joined(),
         "name": row.nodeName, "profileName": row.profileName, "index": row.index,
         "state": row.state, "hasMeasurement": row.measurement != nil,
         "hasEligibleScore": row.eligibleScore != nil]
    }
}

@main struct MultiFixtureDriver {
    static func main() throws {
        let input = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
        var out: [String: Any] = [:]
        switch input["action"] as! String {
        case "palette":
            let foregrounds: [(String, NSColor)] = [
                ("text", MultiPalette.text), ("muted", MultiPalette.muted),
                ("faint", MultiPalette.faint), ("accent", MultiPalette.accent),
                ("success", MultiPalette.success), ("danger", MultiPalette.danger)
            ]
            let backgrounds: [(String, NSColor)] = [
                ("background", MultiPalette.background), ("bar", MultiPalette.bar),
                ("panel", MultiPalette.panel), ("elevated", MultiPalette.elevated),
                ("selection", MultiPalette.selection), ("feedback", MultiPalette.successSurface)
            ]
            for (name, appearanceName) in [("dark", NSAppearance.Name.darkAqua), ("light", .aqua)] {
                var checks: [[String: Any]] = []
                NSAppearance(named: appearanceName)!.performAsCurrentDrawingAppearance {
                    func check(_ label: String, _ foreground: NSColor, _ background: NSColor) {
                        let a = fixtureLuminance(foreground), b = fixtureLuminance(background)
                        checks.append(["pair": label, "ratio": (max(a, b) + 0.05) / (min(a, b) + 0.05)])
                    }
                    for (fgName, fg) in foregrounds {
                        for (bgName, bg) in backgrounds { check("\(fgName)/\(bgName)", fg, bg) }
                    }
                    check("primary button", MultiPalette.accentInk, MultiPalette.accent)
                }
                out[name] = checks
            }
        case "model":
            let batch = fixtureBatch()
            let preview = multiPreviewRows(batch)
            let order = input["order"] as? [Int] ?? Array(preview.indices)
            var oldRows = order.map { preview[$0] }
            for i in oldRows.indices {
                oldRows[i].measurement = fixtureMeasurement(oldRows[i].nodeName)
                oldRows[i].state = "旧成绩"
            }
            let chosen = input["selected"] as? [Int] ?? [1, 4, 6]
            func jobs(_ indices: [Int]) -> [MultiBatchJob] {
                indices.enumerated().map { offset, index in
                    let row = preview[index]
                    return MultiBatchJob(profileIdentity: row.identity.profile, nodeName: row.nodeName,
                                         yamlSnapshot: Data("synthetic".utf8), index: offset, options: batch.options)
                }
            }
            var selectedJobs = jobs(chosen)
            if let unknown = input["unknown"] as? [String] {
                selectedJobs.append(MultiBatchJob(profileIdentity: Data(unknown[0].utf8), nodeName: unknown[1],
                    yamlSnapshot: Data("synthetic".utf8), index: selectedJobs.count, options: batch.options))
            }
            let selected = Set(selectedJobs.map { MultiIdentity(profile: $0.profileIdentity, node: $0.nodeName) })
            let oldBefore = fixtureRows(oldRows)
            let selectedBefore = oldRows.filter { selected.contains($0.identity) }.map(\.index)
            let searches: [[String: Any]] = (input["queries"] as? [String] ?? []).map { query in
                let visible = multiVisibleRows(oldRows, query: query)
                return ["query": query, "positions": visible, "indices": visible.map { oldRows[$0].index },
                        "selected": oldRows.filter { selected.contains($0.identity) }.map(\.index)]
            }
            let reset = multiRunRows(oldRows, jobs: selectedJobs)
            out = ["preview": fixtureRows(preview), "previewJobs": multiJobs(batch).count,
                   "oldBefore": oldBefore, "oldAfter": fixtureRows(oldRows),
                   "selectedBefore": selectedBefore,
                   "selectedAfter": oldRows.filter { selected.contains($0.identity) }.map(\.index),
                   "searches": searches, "accepted": reset != nil]
            if let reset = reset {
                out["run"] = fixtureRows(reset)
                out["rankedIndices"] = multiRank(reset).map(\.index)
                if let freshIndex = input["fresh"] as? Int,
                   let rowIndex = reset.firstIndex(where: { $0.identity == preview[freshIndex].identity }) {
                    var fresh = reset
                    fresh[rowIndex].measurement = fixtureMeasurement(fresh[rowIndex].nodeName)
                    fresh[rowIndex].state = "有效"
                    out["fresh"] = fixtureRows(fresh)
                    out["freshRanked"] = multiRank(fresh).map(\.index)
                    if let nextIndices = input["nextSelected"] as? [Int],
                       let next = multiRunRows(fresh, jobs: jobs(nextIndices)) {
                        out["nextRun"] = fixtureRows(next)
                        out["nextRanked"] = multiRank(next).map(\.index)
                    }
                }
            }
        case "topN":
            let batch = fixtureBatch()
            let preview = multiPreviewRows(batch)
            let order = input["order"] as? [Int] ?? Array(preview.indices)
            var rows = order.map { preview[$0] }
            for i in rows.indices { rows[i].measurement = fixtureMeasurement(rows[i].nodeName) }
            let query = input["query"] as? String ?? ""
            let visible = input["visible"] as? [Int] ?? multiVisibleRows(rows, query: query)
            let count = input["count"] as! Int
            if let picked = multiSelectTopN(rows: rows, visibleRows: visible, count: count) {
                let chosenIndices = visible.compactMap { rows.indices.contains($0) && picked.contains(rows[$0].identity) ? rows[$0].index : nil }
                out = ["ok": true, "count": picked.count, "indices": chosenIndices]
            } else {
                out = ["ok": false]
            }
        case "filterSortFunnel":
            let batch = fixtureBatch()
            var rows = multiPreviewRows(batch)
            // Populate measurements with varying speeds/latencies/states
            for i in rows.indices {
                if i % 3 == 0 {
                    let speed = Double(max(1, 10 - i)) * 1_048_576
                    let latency = Double(30 + i * 10)
                    let m = VerifiedMeasurement(
                        nodeName: rows[i].nodeName,
                        nodeType: "fixture",
                        headAttempts: 6,
                        headSuccesses: 6,
                        latencyMilliseconds: latency,
                        jitterMilliseconds: 2.0,
                        expectedBytes: 262_144,
                        receivedBytes: 262_144,
                        downloadComplete: true,
                        downloadBytesPerSecond: speed
                    )
                    rows[i].measurement = m
                    rows[i].state = "有效"
                } else if i % 3 == 1 {
                    rows[i].state = "等待"
                } else {
                    rows[i].state = "无效"
                }
            }
            let activeSet: Set<MultiIdentity> = Set(rows.enumerated().compactMap { $0.offset % 3 == 1 ? $0.element.identity : nil })
            let validIndices = multiVisibleRows(rows, query: "", statusFilter: .validOnly, activeIdentities: activeSet).map { rows[$0].index }
            let testingIndices = multiVisibleRows(rows, query: "", statusFilter: .testingOnly, activeIdentities: activeSet).map { rows[$0].index }
            let failedIndices = multiVisibleRows(rows, query: "", statusFilter: .failedOnly, activeIdentities: activeSet).map { rows[$0].index }
            let sortedLatencyAsc = multiVisibleRows(rows, query: "", statusFilter: .validOnly, activeIdentities: activeSet,
                                                    sortColumn: "latency", sortAscending: true).map { rows[$0].index }
            let sortedLatencyDesc = multiVisibleRows(rows, query: "", statusFilter: .validOnly, activeIdentities: activeSet,
                                                     sortColumn: "latency", sortAscending: false).map { rows[$0].index }
            let ranked = multiRank(rows)
            let promoted = multiFunnelPromotedIdentities(rows: ranked, runIdentities: Set(rows.map(\.identity)), topCount: 2)
            let promotedIndices = ranked.filter { promoted.contains($0.identity) }.map(\.index)
            out = [
                "valid": validIndices,
                "testing": testingIndices,
                "failed": failedIndices,
                "latencyAsc": sortedLatencyAsc,
                "latencyDesc": sortedLatencyDesc,
                "promoted": promotedIndices
            ]
        case "inspect":
            let data = (input["yaml"] as! String).data(using: .utf8)!
            if let names = try? multiInspectNodes(data) { out = ["ok": true, "names": names] }
            else { out = ["ok": false] }
        case "prepare":
            let root = URL(fileURLWithPath: input["root"] as! String, isDirectory: true)
            var stage = "registry"
            do {
                let registry = try ProfileRegistry.options(root: root)
                stage = "prepare"
                let options = MultiBatchOptions(downloadBytes: 262144, streams: 2, latencyTimeout: "1s", downloadTimeout: "5s")
                let selectedNodes = (input["selectedNodes"] as? [[String: String]]).map { entries in
                    Set(entries.map { MultiIdentity(profile: Data($0["uid"]!.utf8), node: $0["node"]!) })
                }
                let preview = input["preview"] as? Bool ?? false
                let batch = try multiPrepare(root: root, registry: registry, selected: Set(registry.map(\.id)), options: options,
                                             parallel: 10, nodeSelection: selectedNodes, previewOnly: preview)
                let jobs = multiJobs(batch)
                out = ["ok": true, "count": jobs.count, "jobCount": batch.jobCount, "preview": batch.previewOnly,
                       "profileCounts": batch.profiles.map { $0.names.count }, "bytes": batch.requestedBytes,
                       "parallel": batch.parallel,
                       "ids": jobs.map { $0.profileIdentity.map { String(format: "%02x", $0) }.joined() },
                       "names": jobs.map(\.nodeName), "indices": jobs.map(\.index)]
                if preview { out["previewRows"] = fixtureRows(multiPreviewRows(batch)) }
                out["quickBefore"] = (try? multiQuickRevalidate(root: root, batch: batch)) != nil
                if let limit = input["permitChecks"] as? Int {
                    var checks = 0
                    let permitted = (try? multiQuickRevalidate(root: root, batch: batch, mayContinue: {
                        checks += 1; return checks <= limit
                    })) != nil
                    out["quickPermitted"] = permitted
                    out["quickChecks"] = checks
                }
                if input["stale"] as? Bool == true {
                    try Data("proxies: [{name: changed}]".utf8).write(to: URL(fileURLWithPath: batch.profiles[0].option.path))
                    out["revalidates"] = (try? multiRevalidate(root: root, batch: batch)) != nil
                    out["quickAfter"] = (try? multiQuickRevalidate(root: root, batch: batch)) != nil
                }
                if let mutation = input["registryMutation"] as? String {
                    let path = root.appendingPathComponent("profiles.yaml")
                    let original = try Data(contentsOf: path)
                    if mutation == "change" {
                        try (original + Data("\n# fixture change\n".utf8)).write(to: path)
                    } else if mutation == "replace" {
                        try original.write(to: path, options: .atomic)
                    } else if mutation == "symlink" {
                        let backup = root.appendingPathComponent("backup.yaml")
                        try FileManager.default.moveItem(at: path, to: backup)
                        try FileManager.default.createSymbolicLink(at: path, withDestinationURL: backup)
                    }
                    out["quickAfter"] = (try? multiQuickRevalidate(root: root, batch: batch)) != nil
                    out["fullAfter"] = (try? multiRevalidate(root: root, batch: batch)) != nil
                }
            } catch { out = ["ok": false, "error": String(describing: error), "stage": stage] }
        case "snapshot":
            let root = URL(fileURLWithPath: input["root"] as! String, isDirectory: true)
            let registry = try ProfileRegistry.options(root: root)
            let options = MultiBatchOptions(downloadBytes: 262144, streams: 2, latencyTimeout: "1s", downloadTimeout: "5s")
            let batch = try multiPrepare(root: root, registry: registry, selected: Set(registry.map(\.id)), options: options, parallel: 1)
            let snapshot = batch.profiles[0]
            let profileURL = URL(fileURLWithPath: snapshot.option.path)
            let mutation = input["mutation"] as! String
            out["before"] = multiProfileSnapshotMatches(root: root, profile: snapshot)
            if mutation == "replace" {
                try snapshot.data.write(to: profileURL, options: .atomic)
            } else if mutation == "content" {
                try Data("proxies: [{name: changed}]".utf8).write(to: profileURL)
            } else if mutation == "registry" {
                let path = root.appendingPathComponent("profiles.yaml")
                var document = try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as! [String: Any]
                document["current"] = "two"
                try JSONSerialization.data(withJSONObject: document).write(to: path)
            }
            out["after"] = multiProfileSnapshotMatches(root: root, profile: snapshot)
        case "decode":
            let options = MultiBatchOptions(downloadBytes: 262144, streams: 2, latencyTimeout: "1s", downloadTimeout: "5s")
            let job = MultiBatchJob(profileIdentity: Data("uid".utf8), nodeName: "节点", yamlSnapshot: Data("fixture".utf8), index: 0, options: options)
            let strings = input["lines"] as! [String]
            let result = multiDecode(.success(strings), job: job)
            out = ["decoded": result != nil, "eligible": result?.score != nil]
            if let result = result {
                let rows = [MultiRow(identity: MultiIdentity(profile: Data("a".utf8), node: "节点"), profileName: "A", nodeName: "节点", index: 2, measurement: result, state: "有效"),
                            MultiRow(identity: MultiIdentity(profile: Data("b".utf8), node: "节点"), profileName: "B", nodeName: "节点", index: 1, measurement: result, state: "有效"),
                            MultiRow(identity: MultiIdentity(profile: Data("c".utf8), node: "到期"), profileName: "C", nodeName: "到期", index: 0, measurement: result, state: "有效")]
                out["ranking"] = multiRank(rows).map(\.index)
                if let names = input["candidateNames"] as? [String] {
                    let candidates = names.enumerated().map { index, name in
                        MultiRow(identity: MultiIdentity(profile: Data("uid".utf8), node: name), profileName: "fixture",
                                 nodeName: name, index: index, measurement: result, state: "fixture")
                    }
                    out["candidates"] = candidates.map { $0.eligibleScore != nil }
                    out["candidateRanking"] = multiRank(candidates).map(\.index)
                }
            }
        case "tsv":
            out["cells"] = (input["cells"] as! [String]).map(multiTSVSafeCell)
        case "refreshChoice":
            // Synthetic values only: distinguish canonical-equivalent UTF-8,
            // controller identity and a same-bytes profile inode replacement.
            let controller = MultiControllerIdentity(path: "/fixture/é.sock", device: 1, inode: 2)
            let differentSocket = MultiControllerIdentity(path: "/fixture/é.sock", device: 1, inode: 2)
            let option = ProfileOption(uid: "é", name: "fixture", path: "/fixture/profile", isCurrent: true)
            let version = MultiFileVersion(stat())
            let profile = MultiProfileSnapshot(option: option, data: Data("profile".utf8), version: version, names: ["node"])
            let group1 = ProxyGroup(name: "é", now: "old", all: ["node"])
            let group1Rebased = ProxyGroup(name: "é", now: "new", all: ["node"])
            let group2 = ProxyGroup(name: "é", now: "other", all: ["node"])
            let global = ProxyGroup(name: "GLOBAL", now: "old", all: ["node"])
            let recurring = MultiSwitchCapture(controller: controller, profile: profile, groupName: group1.name, now: group1.now)
            let previous = MultiSwitchCapture(controller: controller, profile: profile, groupName: group2.name, now: group2.now)
            let otherUID = ProfileOption(uid: "é", name: "fixture", path: option.path, isCurrent: true)
            let otherPath = ProfileOption(uid: option.uid, name: "fixture", path: "/fixture/other", isCurrent: true)
            var otherStat = stat(); otherStat.st_ino = 99
            let alteredUID = MultiProfileSnapshot(option: otherUID, data: profile.data, version: version, names: profile.names)
            let alteredPath = MultiProfileSnapshot(option: otherPath, data: profile.data, version: version, names: profile.names)
            let alteredData = MultiProfileSnapshot(option: option, data: Data("changed".utf8), version: version, names: profile.names)
            let alteredVersion = MultiProfileSnapshot(option: option, data: profile.data, version: MultiFileVersion(otherStat), names: profile.names)
            func choice(_ previous: MultiSwitchCapture?, _ recurring: MultiSwitchCapture?,
                        _ socket: MultiControllerIdentity, _ profile: MultiProfileSnapshot,
                        _ groups: [ProxyGroup]) -> String {
                switch multiGroupRefreshChoice(previous: previous, recurring: recurring,
                                               controller: socket, profile: profile, candidates: groups) {
                case .select(let index): return "select:\(index)"
                case .noSelection: return "none"
                case .invalidateRecurringAuthorization: return "invalidate"
                }
            }
            let both = [group2, group1Rebased]
            out = ["recurringRebasedNow": choice(nil, recurring, controller, profile, both),
                   "recurringOverridesPrevious": choice(previous, recurring, controller, profile, both),
                   "recurringMissingNoFallback": choice(previous, recurring, controller, profile, [group2]),
                   "recurringDuplicateName": choice(nil, recurring, controller, profile, [group1, group1Rebased]),
                   "recurringChangedSocketBytes": choice(nil, recurring, differentSocket, profile, both),
                   "recurringChangedUIDBytes": choice(nil, recurring, controller, alteredUID, both),
                   "recurringChangedPath": choice(nil, recurring, controller, alteredPath, both),
                   "recurringChangedContents": choice(nil, recurring, controller, alteredData, both),
                   "recurringChangedFileVersion": choice(nil, recurring, controller, alteredVersion, both),
                   "ordinaryPreservesPrevious": choice(previous, nil, controller, profile, both),
                   "ordinarySoleNonGlobal": choice(nil, nil, controller, profile, [group2]),
                   "ordinaryNoGlobalDefault": choice(nil, nil, controller, profile, [global]),
                   "ordinaryMixedNoGuess": choice(nil, nil, controller, profile, [global, group2]),
                   "ordinaryMultipleNoGuess": choice(nil, nil, controller, profile, both),
                   "ordinaryEmpty": choice(nil, nil, controller, profile, [])]
        case "groups":
            let raw = input["groups"] as! [[String: Any]]
            let groups = raw.map { ProxyGroup(name: $0["name"] as! String, now: $0["now"] as! String, all: $0["all"] as! [String]) }
            let names = input["names"] as! [String]
            out = ["candidates": multiGroupCandidates(groups, testedNames: names).map(\.name),
                   "suggestion": multiSuggestedGroup(groups, testedNames: names) as Any? ?? NSNull()]
        case "capture":
            let option = ProfileOption(uid: "é", name: "fixture", path: "/fixture/profile", isCurrent: true)
            let changed = ProfileOption(uid: input["uid"] as? String ?? "é", name: "fixture", path: input["path"] as? String ?? "/fixture/profile", isCurrent: true)
            let version = MultiFileVersion(stat())
            let before = MultiProfileSnapshot(option: option, data: Data("snapshot".utf8), version: version, names: ["node"])
            let after = MultiProfileSnapshot(option: changed, data: Data((input["snapshot"] as? String ?? "snapshot").utf8), version: version, names: ["node"])
            let identity = MultiControllerIdentity(path: "/fixture/socket", device: 1, inode: 2)
            let other = MultiControllerIdentity(path: input["socket"] as? String ?? "/fixture/socket", device: 1, inode: input["inode"] as? ino_t ?? 2)
            let capture = MultiSwitchCapture(controller: identity, profile: before, groupName: "é", now: "old")
            let group = ProxyGroup(name: input["group"] as? String ?? "é", now: input["now"] as? String ?? "old", all: input["members"] as? [String] ?? ["node"])
            out = ["matches": multiCaptureMatches(capture, controller: other, profile: after, group: group, winner: "node")]
        case "socket":
            let identity = multiUniqueController(paths: input["paths"] as! [String])
            out = ["path": identity?.path as Any? ?? NSNull()]
        case "permission":
            out = ["allowed": multiRunPermitted(closed: input["closed"] as? Bool ?? false,
                cancelled: input["cancelled"] as? Bool ?? false, tokenMatches: input["token"] as? Bool ?? true,
                scheduled: input["scheduled"] as? Bool ?? false, armed: input["armed"] as? Bool ?? false)]
        case "authorization":
            let authorization = MultiSwitchAuthorization()
            let before = authorization.permits()
            authorization.revoke(); authorization.revoke()
            out = ["before": before, "after": authorization.permits()]
        case "validationQueue":
            let queue = MultiSerialValidationQueue(), events = FixtureEvents()
            let release = DispatchSemaphore(value: 0)
            func spin(_ condition: () -> Bool) -> Bool {
                let limit = Date(timeIntervalSinceNow: 3)
                while !condition() && Date() < limit {
                    _ = RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
                }
                return condition()
            }
            func record(_ name: String) -> @Sendable (MultiValidationOutcome) -> Void {
                { outcome in events.add(name + ":" + String(describing: outcome)) }
            }
            queue.submit(timeout: 0.25, work: {
                events.add("a:start"); release.wait(); events.add("a:end"); return true
            }, completion: record("a"))
            queue.submit(timeout: 0.25, work: {
                events.add("b:start"); return true
            }, completion: record("b"))
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.03) { events.add("main:tick") }
            let started = spin { events.snapshot().contains("a:start") }
            let ticked = spin { events.snapshot().contains("main:tick") }
            let startedAt = Date()
            if input["variant"] as? String == "cancel" { queue.cancelAll() }
            let cancelMs = Date().timeIntervalSince(startedAt) * 1000
            let expected = input["variant"] as? String == "cancel" ? "cancelled" : "timedOut"
            let initialResolved = spin { events.snapshot().contains("a:" + expected) && events.snapshot().contains("b:" + expected) }
            // While the physical read is still blocked, another run's request
            // must time out without starting a second worker or accumulating IO.
            queue.submit(timeout: 0.15, work: {
                events.add("c:start"); return true
            }, completion: record("c"))
            let nextResolved = spin { events.snapshot().contains("c:timedOut") }
            let beforeRelease = events.snapshot()
            release.signal()
            let readReturned = spin { events.snapshot().contains("a:end") }
            queue.submit(timeout: 1, work: {
                events.add("d:start"); return true
            }, completion: record("d"))
            queue.submit(timeout: 1, work: {
                events.add("e:start"); return false
            }, completion: record("e"))
            let finalResolved = spin { events.snapshot().contains("d:passed") && events.snapshot().contains("e:failed") }
            _ = RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
            out = ["started": started, "ticked": ticked, "initialResolved": initialResolved,
                   "nextResolved": nextResolved, "readReturned": readReturned, "finalResolved": finalResolved,
                   "cancelMs": cancelMs, "beforeRelease": beforeRelease, "events": events.snapshot()]
        case "validationPaced":
            let queue = MultiSerialValidationQueue(), events = FixtureEvents(), start = Date()
            for index in 0..<10 {
                queue.submit(timeout: 0.45, work: {
                    events.add("start:\(index)")
                    Thread.sleep(forTimeInterval: 0.08)
                    return true
                }, completion: { outcome in events.add("end:\(index):" + String(describing: outcome)) })
            }
            let limit = Date(timeIntervalSinceNow: 5)
            while events.snapshot().filter({ $0.hasPrefix("end:") }).count < 10 && Date() < limit {
                _ = RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
            }
            out = ["events": events.snapshot(), "elapsed": Date().timeIntervalSince(start)]
        case "validationDeadline":
            let queue = MultiSerialValidationQueue(), events = FixtureEvents()
            let physicalDone = DispatchSemaphore(value: 0)
            queue.submit(timeout: 0.15, work: {
                events.add("physical:done"); physicalDone.signal(); return true
            }, completion: { outcome in events.add("end:" + String(describing: outcome)) })
            let doneWhileMainBlocked = physicalDone.wait(timeout: .now() + 1) == .success
            // The success is queued before the timer, but the main loop cannot
            // deliver it until after the deadline. It must still fail closed.
            Thread.sleep(forTimeInterval: 0.25)
            let limit = Date(timeIntervalSinceNow: 2)
            while !events.snapshot().contains(where: { $0.hasPrefix("end:") }) && Date() < limit {
                _ = RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
            }
            out = ["physicalDone": doneWhileMainBlocked, "events": events.snapshot()]
        default: out = ["ok": false]
        }
        FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]))
    }
}
'''


class MultiAppHeadlessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="multi-ui-fixtures-", dir="/private/tmp")
        cls.work = Path(cls.temp.name)
        (cls.work / "home").mkdir(mode=0o700)
        cls.env = os.environ.copy()
        cls.env.update(HOME=str(cls.work / "home"), TMPDIR=str(cls.work),
                       CLANG_MODULE_CACHE_PATH=str(cls.work / "clang-cache"),
                       SWIFT_MODULECACHE_PATH=str(cls.work / "swift-cache"))
        driver = cls.work / "Driver.swift"
        driver.write_text(DRIVER, encoding="utf-8")
        cls.binary = cls.work / "driver"
        sources = ["MultiMain.swift", "ProfileRegistry.swift", "MultiBatchRunner.swift",
                   "VerifiedMeasurement.swift", "CompositeScore.swift", "TestSchedule.swift",
                   "SwitchController.swift", "AssistedSwitch.swift"]
        # Keep compiler inputs immutable even while collaborators edit the checkout.
        for name in sources:
            shutil.copyfile(PROJECT / "Sources" / name, cls.work / name)
        # Report the actual immutable compiler input even if the checkout changes concurrently.
        revision = hashlib.sha256((cls.work / "MultiMain.swift").read_bytes()).hexdigest()
        print(f"Synthetic compiler input MultiMain.swift SHA-256: {revision}", flush=True)
        # Exercise the advertised deployment target, not just this host's newer SDK.
        subprocess.run(["/usr/bin/swiftc", "-swift-version", "5", "-target",
                        f"{os.uname().machine}-apple-macos12.0", "-D", "MULTI_UI_TEST",
                        "-parse-as-library", "-framework", "AppKit", *map(str, (cls.work / x for x in sources)),
                        str(driver), "-o", str(cls.binary)], check=True, env=cls.env,
                       stdout=subprocess.DEVNULL, timeout=180)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_driver(self, payload):
        result = subprocess.run([str(self.binary)], input=json.dumps(payload, ensure_ascii=False).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=25)
        self.assertEqual(result.returncode, 0, "fixture driver failed")
        self.assertEqual(result.stderr, b"", "fixture driver leaked diagnostics")
        return json.loads(result.stdout)

    def test_graphite_text_is_readable_in_both_appearances_and_row_states(self):
        out = self.run_driver({"action": "palette"})
        for appearance, checks in out.items():
            self.assertEqual(len(checks), 37)
            for check in checks:
                with self.subTest(appearance=appearance, pair=check["pair"]):
                    self.assertGreaterEqual(check["ratio"], 4.5)

    def test_preview_catalogue_preserves_byte_exact_profile_and_node_identities(self):
        out = self.run_driver({"action": "model"})
        rows = out["preview"]
        self.assertEqual(out["previewJobs"], 0, "preview snapshots must never create launchable jobs")
        self.assertEqual(len(rows), 8)
        names = ["same", "é", "é", "Alpha", "same", "é", "é", "Bravo"]
        profiles = ["é"] * 4 + ["é"] * 4
        self.assertEqual([(r["profile"], r["node"]) for r in rows],
                         [(p.encode().hex(), n.encode().hex()) for p, n in zip(profiles, names)])
        self.assertEqual(len({(r["profile"], r["node"]) for r in rows}), 8)
        self.assertEqual([r["index"] for r in rows], list(range(8)))
        self.assertEqual([r["profileName"] for r in rows],
                         ["First [é]"] * 4 + ["Second [é]"] * 4)
        self.assertTrue(all(r["state"] == "待测速" and not r["hasMeasurement"] and
                            not r["hasEligibleScore"] for r in rows))

    def test_subset_run_resets_all_old_scores_and_retains_catalogue_indices(self):
        # Previous ranking need not match the catalogue order; job indices are
        # newly compacted and must not replace the original row indices.
        order = [7, 1, 4, 3, 0, 6, 5, 2]
        chosen = [1, 4, 6]
        out = self.run_driver({"action": "model", "order": order, "selected": chosen,
                               "fresh": 4, "nextSelected": [2]})
        self.assertTrue(out["accepted"])
        self.assertEqual(out["oldBefore"], out["oldAfter"], "the input catalogue must remain unchanged")
        self.assertTrue(all(r["hasMeasurement"] and r["hasEligibleScore"] for r in out["oldBefore"]))
        self.assertEqual([r["index"] for r in out["run"]], order)
        for old, row in zip(out["oldBefore"], out["run"]):
            for key in ("profile", "node", "name", "profileName", "index"):
                self.assertEqual(row[key], old[key])
            self.assertEqual(row["state"], "等待" if row["index"] in chosen else "未勾选")
            self.assertFalse(row["hasMeasurement"])
            self.assertFalse(row["hasEligibleScore"])
        self.assertEqual(out["rankedIndices"], list(range(8)))

        # A synthetic current result is eligible only for the chosen row; when
        # the next subset starts even that result becomes stale and is cleared.
        self.assertEqual([r["index"] for r in out["fresh"] if r["hasEligibleScore"]], [4])
        self.assertEqual(out["freshRanked"][0], 4)
        self.assertEqual([r["index"] for r in out["nextRun"]], order)
        self.assertTrue(all(not r["hasMeasurement"] and not r["hasEligibleScore"] for r in out["nextRun"]))
        self.assertEqual([r["index"] for r in out["nextRun"] if r["state"] == "等待"], [2])
        self.assertEqual(out["nextRanked"], list(range(8)))

    def test_run_rejects_empty_duplicate_and_missing_identities(self):
        cases = [
            {"selected": []},
            {"selected": [1, 1]},  # duplicate job identity
            {"order": list(range(8)) + [0], "selected": [1]},  # duplicate catalogue identity
            {"order": list(range(1, 8)), "selected": [0]},  # job missing from catalogue
            {"selected": [1], "unknown": ["é", "missing"]},
            {"selected": [1], "unknown": ["missing", "same"]},
            {"order": [], "selected": []},
        ]
        for case in cases:
            with self.subTest(case=case):
                out = self.run_driver({"action": "model", **case})
                self.assertFalse(out["accepted"], out)
                self.assertEqual(out["oldBefore"], out["oldAfter"])
                self.assertNotIn("run", out)

    def test_search_filters_positions_without_changing_checked_identities(self):
        order = [7, 1, 4, 3, 0, 6, 5, 2]
        queries = ["", " alpha ", "SECOND", "same", "not-present", " \n\t "]
        out = self.run_driver({"action": "model", "order": order, "selected": [1, 4, 6],
                               "queries": queries})
        expected = [order, [3], [7, 4, 6, 5], [4, 0], [], order]
        self.assertEqual(out["selectedBefore"], [1, 4, 6])
        self.assertEqual(out["selectedAfter"], [1, 4, 6])
        self.assertEqual([search["indices"] for search in out["searches"]], expected)
        for search, indices in zip(out["searches"], expected):
            self.assertEqual(search["selected"], [1, 4, 6])
            self.assertEqual(search["positions"], [order.index(index) for index in indices])
        self.assertEqual(out["oldBefore"], out["oldAfter"])
        self.assertEqual([r["index"] for r in out["run"] if r["state"] == "等待"], [1, 4, 6])

    def test_switch_authorization_revocation_is_one_way(self):
        self.assertEqual(self.run_driver({"action": "authorization"}), {"before": True, "after": False})

    def test_async_validation_timeout_cancel_and_late_read_are_bounded(self):
        for variant in ("cancel", "timeout"):
            with self.subTest(variant=variant):
                out = self.run_driver({"action": "validationQueue", "variant": variant})
                for key in ("started", "ticked", "initialResolved", "nextResolved", "readReturned", "finalResolved"):
                    self.assertTrue(out[key], (key, out))
                if variant == "cancel":
                    self.assertLess(out["cancelMs"], 250, "cancel must not wait for a blocked disk read")
                before, events = out["beforeRelease"], out["events"]
                self.assertIn("main:tick", before, "the main loop must run while IO is blocked")
                self.assertNotIn("a:end", before)
                self.assertNotIn("b:start", events)
                self.assertNotIn("c:start", events)
                self.assertEqual(events.count("a:cancelled" if variant == "cancel" else "a:timedOut"), 1)
                self.assertEqual(events.count("b:cancelled" if variant == "cancel" else "b:timedOut"), 1)
                self.assertEqual(events.count("c:timedOut"), 1)
                self.assertNotIn("a:passed", events, "late physical success must not complete twice")
                self.assertEqual(events.count("d:passed"), 1)
                self.assertEqual(events.count("e:failed"), 1)
                self.assertLess(events.index("a:end"), events.index("d:start"))
                self.assertLess(events.index("d:start"), events.index("e:start"))

    def test_serial_validation_starts_each_deadline_when_work_begins(self):
        out = self.run_driver({"action": "validationPaced"})
        self.assertGreater(out["elapsed"], 0.45, "total queue time must exceed one timeout")
        self.assertEqual([e for e in out["events"] if e.startswith("start:")],
                         [f"start:{i}" for i in range(10)])
        self.assertEqual([e for e in out["events"] if e.startswith("end:")],
                         [f"end:{i}:passed" for i in range(10)])

    def test_late_main_delivery_cannot_accept_an_overdue_success(self):
        out = self.run_driver({"action": "validationDeadline"})
        self.assertTrue(out["physicalDone"], out)
        self.assertEqual(out["events"], ["physical:done", "end:timedOut"])

    def test_suggestion_requires_unique_full_byte_coverage_and_never_global(self):
        def group(name, members, now="old"):
            return {"name": name, "now": now, "all": members}
        names = ["é", "é"]
        full = group("full", names)
        global_group = group("GLOBAL", names)
        partial = group("partial", ["é"])
        out = self.run_driver({"action": "groups", "groups": [global_group, partial, full], "names": names})
        self.assertEqual(out["suggestion"], "full")
        self.assertEqual(out["candidates"], ["GLOBAL", "partial", "full"])
        out = self.run_driver({"action": "groups", "groups": [global_group, partial], "names": names})
        self.assertIsNone(out["suggestion"])
        out = self.run_driver({"action": "groups", "groups": [full, group("other", names)], "names": names})
        self.assertIsNone(out["suggestion"])
        out = self.run_driver({"action": "groups", "groups": [full, full], "names": names})
        self.assertEqual(out["candidates"], [])
        self.assertIsNone(out["suggestion"])
        out = self.run_driver({"action": "groups", "groups": [group("other", ["é"])], "names": ["é"]})
        self.assertEqual(out["candidates"], [])
        out = self.run_driver({"action": "groups", "groups": [group("empty-now", names, "")], "names": names})
        self.assertEqual(out["candidates"], [])
        for now in ["line\nbreak", "x" * 8193]:
            out = self.run_driver({"action": "groups", "groups": [group("unsafe", names, now)], "names": names})
            self.assertEqual(out["candidates"], [])

    def test_refresh_keeps_recurring_target_or_fails_closed_without_fallback(self):
        out = self.run_driver({"action": "refreshChoice"})
        self.assertEqual(out, {
            "recurringRebasedNow": "select:1", "recurringOverridesPrevious": "select:1",
            "recurringMissingNoFallback": "invalidate", "recurringDuplicateName": "invalidate",
            "recurringChangedSocketBytes": "invalidate", "recurringChangedUIDBytes": "invalidate",
            "recurringChangedPath": "invalidate", "recurringChangedContents": "invalidate",
            "recurringChangedFileVersion": "invalidate", "ordinaryPreservesPrevious": "select:0",
            "ordinarySoleNonGlobal": "select:0", "ordinaryNoGlobalDefault": "none",
            "ordinaryMixedNoGuess": "none", "ordinaryMultipleNoGuess": "none", "ordinaryEmpty": "none",
        })

    def test_capture_requires_exact_socket_profile_group_now_and_membership(self):
        self.assertTrue(self.run_driver({"action": "capture"})["matches"])
        for change in [{"uid": "é"}, {"path": "/fixture/other"}, {"snapshot": "changed"},
                       {"socket": "/fixture/other"}, {"inode": 3}, {"group": "é"},
                       {"now": "new"}, {"members": ["other"]}]:
            with self.subTest(change=change):
                self.assertFalse(self.run_driver({"action": "capture", **change})["matches"])

    def test_revocation_and_run_token_gate(self):
        allowed = lambda **kw: self.run_driver({"action": "permission", **kw})["allowed"]
        self.assertTrue(allowed())
        self.assertTrue(allowed(scheduled=True, armed=True))
        for args in [{"closed": True}, {"cancelled": True}, {"token": False},
                     {"scheduled": True, "armed": False}]:
            with self.subTest(args=args):
                self.assertFalse(allowed(**args))

    def test_unique_same_user_unix_socket_identity(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            first, second = str(Path(temp) / "a.sock"), str(Path(temp) / "b.sock")
            s1, s2 = socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX)
            try:
                s1.bind(first)
                self.assertEqual(self.run_driver({"action": "socket", "paths": [first, first]})["path"], first)
                link = str(Path(temp) / "link.sock")
                os.symlink(first, link)
                self.assertIsNone(self.run_driver({"action": "socket", "paths": [link]})["path"])
                s2.bind(second)
                self.assertIsNone(self.run_driver({"action": "socket", "paths": [first, second]})["path"])
                self.assertIsNone(self.run_driver({"action": "socket", "paths": ["relative.sock"]})["path"])
            finally:
                s1.close()
                s2.close()

    def test_inspect_nodes_and_reject_unsafe_yaml(self):
        valid = "proxies:\n  - name: 'é'\n  - name: 'é'\n"
        self.assertEqual(self.run_driver({"action": "inspect", "yaml": valid})["names"], ["é", "é"])
        invalid = ["", "[]", "proxies: []", "proxies: [{name: a}, {name: a}]",
                   "proxies: [{name: a}]\nproxy-providers: {}", "proxies: [{name: a}]\nrule-providers: {}",
                   "proxies: [{name: a}, {name: &x b}, {name: *x}]", "proxies: [{name: a, name: b}]",
                   'proxies: [{name: "a\\nb"}]', "proxies: [{name: a}]\nproxies: [{name: b}]",
                   "proxies: [{name: a}]\n1: first\n01: second",
                   "proxies: [{name: a}]\n!!binary cHJveGllcw==: []", "x: " + "a" * (1024*1024)]
        for yaml in invalid:
            with self.subTest(case=yaml[:60]):
                self.assertFalse(self.run_driver({"action": "inspect", "yaml": yaml})["ok"])

    def test_prepare_combines_byte_distinct_profiles_and_detects_staleness(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            root = Path(temp) / "fixture"
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "a.yaml").write_text("proxies: [{name: same}]", encoding="utf-8")
            (profiles / "b.yaml").write_text("proxies: [{name: same}]", encoding="utf-8")
            items = [{"uid": "é", "name": "same", "file": "a.yaml", "type": "local"},
                     {"uid": "é", "name": "same", "file": "b.yaml", "type": "local"}]
            (root / "profiles.yaml").write_text(json.dumps({"items": items, "current": "é"}), encoding="utf-8")
            out = self.run_driver({"action": "prepare", "root": str(root), "stale": True})
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["ids"], ["é".encode().hex(), "é".encode().hex()])
            self.assertEqual((out["count"], out["bytes"], out["parallel"]), (2, 524288, 10))
            self.assertEqual(out["indices"], [0, 1])
            self.assertTrue(out["quickBefore"])
            self.assertFalse(out["quickAfter"])
            self.assertFalse(out["revalidates"])

    def test_quick_validation_checks_revocation_during_snapshot_reads(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            root = Path(temp) / "fixture"
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "a.yaml").write_text("proxies: [{name: one}]", encoding="utf-8")
            (profiles / "b.yaml").write_text("proxies: [{name: two}]", encoding="utf-8")
            items = [{"uid": "one", "name": "first", "file": "a.yaml", "type": "local"},
                     {"uid": "two", "name": "second", "file": "b.yaml", "type": "local"}]
            (root / "profiles.yaml").write_text(json.dumps({"items": items, "current": "one"}), encoding="utf-8")
            for allowed in (0, 1, 5, 10, 15, 25):
                with self.subTest(allowed=allowed):
                    out = self.run_driver({"action": "prepare", "root": str(root), "permitChecks": allowed})
                    self.assertTrue(out["ok"] and out["quickBefore"], out)
                    self.assertFalse(out["quickPermitted"], out)
                    self.assertEqual(out["quickChecks"], allowed + 1, out)
            out = self.run_driver({"action": "prepare", "root": str(root), "permitChecks": 10000})
            self.assertTrue(out["quickPermitted"], out)

    def test_selected_nodes_use_exact_profile_and_node_bytes_and_preview_cannot_launch(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            root = Path(temp) / "fixture"
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "a.yaml").write_text("proxies: [{name: 'é'}, {name: 'é'}]", encoding="utf-8")
            (profiles / "b.yaml").write_text("proxies: [{name: 'é'}, {name: 'é'}]", encoding="utf-8")
            items = [{"uid": "é", "name": "first", "file": "a.yaml", "type": "local"},
                     {"uid": "é", "name": "second", "file": "b.yaml", "type": "local"}]
            (root / "profiles.yaml").write_text(json.dumps({"items": items, "current": "é"}), encoding="utf-8")
            selected = [{"uid": "é", "node": "é"}, {"uid": "é", "node": "é"}]
            out = self.run_driver({"action": "prepare", "root": str(root), "selectedNodes": selected})
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["names"], ["é", "é"])
            self.assertEqual(out["ids"], ["é".encode().hex(), "é".encode().hex()])
            self.assertEqual((out["jobCount"], out["count"], out["profileCounts"]), (2, 2, [1, 1]))
            preview = self.run_driver({"action": "prepare", "root": str(root), "preview": True})
            self.assertTrue(preview["ok"], preview)
            self.assertEqual((preview["jobCount"], preview["count"], preview["preview"]), (4, 0, True))
            expected = [(profile, node) for profile in ("é", "é") for node in ("é", "é")]
            self.assertEqual([(r["profile"], r["node"]) for r in preview["previewRows"]],
                             [(p.encode().hex(), n.encode().hex()) for p, n in expected])
            self.assertEqual([r["index"] for r in preview["previewRows"]], [0, 1, 2, 3])
            self.assertTrue(all(not r["hasMeasurement"] for r in preview["previewRows"]))
            for bad in ([], [{"uid": "é", "node": "missing"}], [{"uid": "missing", "node": "é"}],
                        selected + [{"uid": "é", "node": "missing"}]):
                with self.subTest(bad=bad):
                    self.assertFalse(self.run_driver({"action": "prepare", "root": str(root), "selectedNodes": bad})["ok"])

    def test_selection_budgets_only_launched_jobs_but_preview_cannot_launch(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            root = Path(temp) / "fixture"
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            nodes = ", ".join("{name: n%03d}" % n for n in range(75))
            (profiles / "a.yaml").write_text("filler: " + "x" * 950000 + "\nproxies: [" + nodes + "]\n", encoding="utf-8")
            items = [{"uid": "one", "name": "fixture", "file": "a.yaml", "type": "local"}]
            (root / "profiles.yaml").write_text(json.dumps({"items": items, "current": "one"}), encoding="utf-8")
            preview = self.run_driver({"action": "prepare", "root": str(root), "preview": True})
            self.assertTrue(preview["ok"], preview)
            self.assertEqual((preview["jobCount"], preview["count"]), (75, 0))
            one = self.run_driver({"action": "prepare", "root": str(root),
                                   "selectedNodes": [{"uid": "one", "node": "n000"}]})
            self.assertTrue(one["ok"], one)
            self.assertEqual((one["jobCount"], one["count"]), (1, 1))
            all_nodes = self.run_driver({"action": "prepare", "root": str(root)})
            self.assertFalse(all_nodes["ok"])

    def test_registry_change_replacement_and_symlink_stop_queued_launch(self):
        for mutation in ("change", "replace", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.work) as temp:
                root = Path(temp) / "fixture"
                profiles = root / "profiles"
                profiles.mkdir(parents=True)
                (profiles / "one.yaml").write_text("proxies: [{name: a}]", encoding="utf-8")
                (root / "profiles.yaml").write_text(json.dumps({"items": [
                    {"uid": "one", "name": "fixture", "file": "one.yaml", "type": "local"}],
                    "current": "one"}), encoding="utf-8")
                out = self.run_driver({"action": "prepare", "root": str(root), "registryMutation": mutation})
                self.assertTrue(out["ok"], out)
                self.assertTrue(out["quickBefore"])
                self.assertFalse(out["quickAfter"])
                self.assertFalse(out["fullAfter"])

    def test_captured_profile_version_rejects_identical_replacement_but_not_current_profile_change(self):
        for mutation in ("unchanged", "replace", "content", "registry"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.work) as temp:
                root = Path(temp) / "fixture"
                profiles = root / "profiles"
                profiles.mkdir(parents=True)
                (profiles / "one.yaml").write_text("proxies: [{name: a}]", encoding="utf-8")
                (profiles / "two.yaml").write_text("proxies: [{name: b}]", encoding="utf-8")
                items = [{"uid": "one", "name": "first", "file": "one.yaml", "type": "local"},
                         {"uid": "two", "name": "second", "file": "two.yaml", "type": "local"}]
                (root / "profiles.yaml").write_text(json.dumps({"items": items, "current": "one"}), encoding="utf-8")
                out = self.run_driver({"action": "snapshot", "root": str(root), "mutation": mutation})
                self.assertTrue(out["before"], out)
                self.assertEqual(out["after"], mutation in ("unchanged", "registry"), out)

    def test_tsv_cells_neutralize_formulas_and_delimiters(self):
        cells = ["normal", "=1+2", "  +cmd", "\t-5", "\n@r", "safe\tcell\nrow", "'safe", "123", "", "  ="]
        out = self.run_driver({"action": "tsv", "cells": cells})
        self.assertEqual(out["cells"], ["normal", "'=1+2", "'  +cmd", "' -5", "' @r", "safe cell row", "'safe", "123", "", "'  ="])

    def test_prepare_rejects_symlinked_ancestor_and_memory_budget(self):
        with tempfile.TemporaryDirectory(dir=self.work) as temp:
            base = Path(temp)
            root = base / "real" / "fixture"
            profiles = root / "profiles"
            profiles.mkdir(parents=True)
            profile = profiles / "one.yaml"
            profile.write_text("proxies: [{name: a}]", encoding="utf-8")
            (root / "profiles.yaml").write_text(json.dumps({"items": [
                {"uid": "one", "name": "fixture", "file": "one.yaml", "type": "local"}]}), encoding="utf-8")
            link = base / "linked-parent"
            link.symlink_to(base / "real", target_is_directory=True)
            self.assertFalse(self.run_driver({"action": "prepare", "root": str(link / "fixture")})["ok"])
            profile.write_text("padding: " + "x" * (1024 * 1024), encoding="utf-8")
            self.assertFalse(self.run_driver({"action": "prepare", "root": str(root)})["ok"])
            names = "\n".join("  - name: n%d" % i for i in range(80))
            profile.write_text("padding: " + "x" * 900000 + "\nproxies:\n" + names + "\n", encoding="utf-8")
            self.assertFalse(self.run_driver({"action": "prepare", "root": str(root)})["ok"])

    def test_strict_decode_and_stable_eligibility_ranking(self):
        fixture = {"version": 1, "node_name": "节点", "node_type": "ss", "head_attempts": 6,
                   "head_successes": 6, "latency_ms": 50.0, "jitter_ms": 2.0,
                   "expected_bytes": 262144, "received_bytes": 262144, "download_complete": True,
                   "download_bytes_per_second": 1048576.0}
        line = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
        out = self.run_driver({"action": "decode", "lines": [line]})
        self.assertTrue(out["decoded"] and out["eligible"])
        self.assertEqual(out["ranking"], [1, 2, 0])
        names = ["流量重置", "节点", "TRAFFIC REMAINING", "香港 01"]
        candidates = self.run_driver({"action": "decode", "lines": [line], "candidateNames": names})
        self.assertEqual(candidates["candidates"], [False, True, False, True])
        self.assertEqual(candidates["candidateRanking"], [1, 3, 0, 2])
        for terminator in ("\n", "\r\n"):
            self.assertTrue(self.run_driver({"action": "decode", "lines": [line + terminator]})["decoded"])
        for lines in [[], [line, line], [line + "\n{}"], [line.replace('"version":1', '"version":1,"version":1')],
                      [line.replace("节点", "other")], ["a\tb\tc"]]:
            with self.subTest(lines=lines):
                self.assertFalse(self.run_driver({"action": "decode", "lines": lines})["decoded"])
        fixture.update(download_complete=False, received_bytes=10, download_bytes_per_second=0)
        out = self.run_driver({"action": "decode", "lines": [json.dumps(fixture, ensure_ascii=False)]})
        self.assertTrue(out["decoded"])
        self.assertFalse(out["eligible"])

    def test_select_top_n_picks_first_visible_rows_and_validates_bounds(self):
        order = [7, 2, 4, 0, 6, 1, 5, 3]
        out3 = self.run_driver({"action": "topN", "order": order, "count": 3})
        self.assertEqual(out3, {"ok": True, "count": 3, "indices": [7, 2, 4]})
        out_filtered = self.run_driver({"action": "topN", "order": order, "query": "Second", "count": 2})
        self.assertEqual(out_filtered, {"ok": True, "count": 2, "indices": [7, 4]})
        out_capped = self.run_driver({"action": "topN", "order": order, "count": 50})
        self.assertEqual(out_capped, {"ok": True, "count": 8, "indices": order})
        for invalid in (0, -1, 2001):
            self.assertFalse(self.run_driver({"action": "topN", "order": order, "count": invalid})["ok"])
        self.assertFalse(self.run_driver({"action": "topN", "order": order, "query": "no-such-node", "count": 3})["ok"])
        self.assertFalse(self.run_driver({"action": "topN", "order": order, "visible": [0, 99], "count": 2})["ok"])

    def test_status_filter_column_sorting_and_funnel_promotion(self):
        out = self.run_driver({"action": "filterSortFunnel"})
        self.assertEqual(out["valid"], [0, 3, 6])
        self.assertEqual(out["testing"], [1, 4, 7])
        self.assertEqual(out["failed"], [2, 5])
        self.assertEqual(out["latencyAsc"], [0, 3, 6])
        self.assertEqual(out["latencyDesc"], [6, 3, 0])
        self.assertEqual(out["promoted"], [0, 3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
