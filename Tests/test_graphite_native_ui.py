#!/usr/bin/env python3
"""Render the real AppKit views with synthetic, in-memory data only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

PROJECT = Path(__file__).resolve().parent.parent
DRIVER = r'''
private enum FixtureMotion { static var reduced = false }
extension MultiAppDelegate {
    func verifyGraphite(_ output: URL) throws -> [[String: Any]] {
        var checks: [[String: Any]] = []
        func check(_ name: String, _ passed: Bool, _ detail: String = "") {
            checks.append(["check": name, "passed": passed, "detail": detail])
        }
        let option = ProfileOption(uid: "synthetic", name: "示例订阅", path: "/fixture/unused.yaml", isCurrent: true)
        let names = ["JP 日本东京 01 · 电信联通", "JP 日本东京 02 · 电信联通", "HK 香港 01 · 三网优化", "SG 新加坡 01 · 家宽优化", "US 美国洛杉矶 01 · 流媒体", "HK 香港 05 · 电信联通", "JP 日本大阪 02 · 联通优化", "SG 新加坡 03 · 高速线路", "DE 德国法兰克福 01", "US 美国圣何塞 03"]
        let profile = MultiProfileSnapshot(option: option, data: Data("synthetic".utf8), version: MultiFileVersion(stat()), names: names)
        let batch = MultiPreparedBatch(registry: [option], registryData: Data(), registryVersion: MultiFileVersion(stat()), profiles: [profile], options: MultiBatchOptions(downloadBytes: 1_048_576, streams: 2, latencyTimeout: "3s", downloadTimeout: "3s"), parallel: 10, jobCount: names.count, requestedBytes: names.count * 1_048_576, previewOnly: true)
        registry = [option]; selected = [option.id]; selectionPreview = batch
        rows = multiPreviewRows(batch); nodeSelection = Set(rows.map(\.identity))
        for i in 0..<5 {
            rows[i].measurement = VerifiedMeasurement(nodeName: names[i], nodeType: i % 2 == 0 ? "VLESS" : "Trojan", headAttempts: 6, headSuccesses: 6, latencyMilliseconds: Double(38 + i * 17), jitterMilliseconds: 4.2, expectedBytes: 1_048_576, receivedBytes: 1_048_576, downloadComplete: true, downloadBytesPerSecond: Double(20 - i * 2) * 1_048_576)
            rows[i].state = "有效"
        }
        rows[8].state = "无效"; rows[9].state = "无效"
        buildWindow(); window.appearance = NSAppearance(named: .darkAqua)
        reloadVisibleRows(); updateControls()
        status.stringValue = "合成数据验收 · 未连接订阅、未启动测速或切换"
        let rootView = window.contentView!
        func rect(_ view: NSView) -> NSRect { view.convert(view.bounds, to: rootView) }
        func save(_ name: String, _ size: NSSize, _ appearance: NSAppearance.Name) throws {
            window.appearance = NSAppearance(named: appearance)
            window.setContentSize(size); rootView.layoutSubtreeIfNeeded()
            (rootView as? MultiClearAmbientCanvas)?.invalidateAppearance()
            rootView.displayIfNeeded()
            let bitmap = rootView.bitmapImageRepForCachingDisplay(in: rootView.bounds)!
            rootView.cacheDisplay(in: rootView.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
        }
        let groups: [[(String, NSView)]] = [
            [("liveMode", liveModeButton), ("historyMode", historyModeButton), ("export", copyButton), ("funnel", funnelButton), ("load", loadTestButton), ("stop", stopButton), ("start", startButton)],
            [("parallel", parallelBox), ("size", sizePopup), ("mode", modePopup), ("interval", intervalPopup), ("schedule", scheduleButton)],
            [("patrolQuota", patrolQuotaPopup), ("patrol", patrolButton), ("health", healthStatusPill)],
            [("all", filterAllButton), ("valid", filterValidButton), ("testing", filterTestingButton), ("failed", filterFailedButton), ("search", nodeSearch), ("topN", topNBox), ("choose", selectTopNButton), ("checkAll", selectShownButton), ("clear", clearShownButton)]
        ]
        for width in [1240.0, 1080.0] {
            try save("native-dark-\(Int(width)).png", NSSize(width: width, height: width == 1080 ? 700 : 800), .darkAqua)
            check("window honors \(Int(width)) width", abs(rootView.bounds.width - width) < 1, "actual:\(rootView.bounds.size), fitting:\(rootView.fittingSize)")
            var errors: [String] = []
            for group in groups {
                for (name, view) in group where !view.isHidden {
                    let frame = rect(view)
                    if !NSContainsRect(rootView.bounds, frame) || frame.width < 20 || frame.height < 14 { errors.append("\(name):\(frame)") }
                }
                for i in 1..<group.count {
                    if rect(group[i-1].1).maxX > rect(group[i].1).minX + 1 { errors.append("overlap:\(group[i-1].0)/\(group[i].0)") }
                }
            }
            check("controls fit at \(Int(width))", errors.isEmpty, errors.joined(separator: ";"))
            check("ten original columns at \(Int(width))", resultTable.tableColumns.count == 10)
        }
        try save("native-light-1240.png", NSSize(width: 1240, height: 800), .aqua)
        // Exercise Long-Term Leaderboard view layout & rendering at 1240 and 1080
        let nowSec = Date().timeIntervalSince1970
        for (idx, name) in names.prefix(5).enumerated() {
            for sampleIdx in 0..<3 {
                telemetryStore.recordSample(TelemetrySampleRecord(
                    timestamp: nowSec - Double((sampleIdx + 1) * 3600 + idx * 120),
                    profileUID: option.uid,
                    profileName: option.name,
                    nodeName: name,
                    nodeType: idx % 2 == 0 ? "VLESS" : "Trojan",
                    source: .manual,
                    requestedBytes: 3 * 1_048_576,
                    streams: 4,
                    downloadComplete: true,
                    downloadBytesPerSecond: Double(22 - idx * 3) * 1_048_576,
                    latencyMilliseconds: Double(35 + idx * 14),
                    jitterMilliseconds: 3.1,
                    headAttempts: 6,
                    headSuccesses: 6,
                    compositeScore: Double(90 - idx * 6)
                ))
            }
        }
        telemetryStore.recordHealthEvent(TelemetryHealthEvent(timestamp: nowSec - 60, profileUID: option.uid, nodeName: names[0], groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"))
        telemetryStore.recordHealthEvent(TelemetryHealthEvent(timestamp: nowSec - 10, profileUID: option.uid, nodeName: names[0], groupName: "PROXY", ok: false, delayMilliseconds: nil, failureKind: "probe_failed"))
        historyModeButton.performClick(nil)
        let histDeadline = ProcessInfo.processInfo.systemUptime + 5
        while historySummaries.isEmpty && ProcessInfo.processInfo.systemUptime < histDeadline {
            _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        let historyGroups: [[(String, NSView)]] = [
            [("win24h", historyWin24hButton), ("win7d", historyWin7dButton), ("win30d", historyWin30dButton), ("winPeak", historyWinPeakButton), ("selOnly", historySelectedOnlyButton), ("hSearch", historySearch), ("clearHist", clearHistoryButton)]
        ]
        for width in [1240.0, 1080.0] {
            try save("native-history-dark-\(Int(width)).png", NSSize(width: width, height: width == 1080 ? 700 : 800), .darkAqua)
            var hErrors: [String] = []
            for group in historyGroups {
                for (name, view) in group where !view.isHidden {
                    let frame = rect(view)
                    if !NSContainsRect(rootView.bounds, frame) || frame.width < 20 || frame.height < 14 { hErrors.append("\(name):\(frame)") }
                }
                for i in 1..<group.count {
                    if rect(group[i-1].1).maxX > rect(group[i].1).minX + 1 { hErrors.append("overlap:\(group[i-1].0)/\(group[i].0)") }
                }
            }
            check("history toolbar fits at \(Int(width))", hErrors.isEmpty, hErrors.joined(separator: ";"))
            check("ten columns preserved in history at \(Int(width))", resultTable.tableColumns.count == 10)
        }
        check("history view populates summaries and applies health penalty", !historySummaries.isEmpty && historySummaries.contains(where: { $0.nodeName == names[0] && $0.healthPenalty > 0 }))
        liveModeButton.performClick(nil)
        window.appearance = NSAppearance(named: .darkAqua)
        nodeSearch.stringValue = "missing-fixture-node"; filterNodes(nil)
        check("empty search explains the empty list", visibleRows.isEmpty && !emptyStateLabel.isHidden && emptyStateLabel.stringValue == "当前筛选没有节点")
        nodeSearch.stringValue = ""; filterNodes(nil)
        check("clearing search restores the catalogue", visibleRows.count == names.count && emptyStateLabel.isHidden)
        let token = schedule.beginManualRun()!
        activeToken = token; busy = true; currentBatch = batch
        for i in rows.indices { rows[i].measurement = nil; rows[i].state = "等待" }
        runIdentities = Set(rows.map(\.identity))
        let identity = rows[4].identity
        runJobIdentity = [0: identity]
        sortColumnIdentifier = "score"; sortColumnAscending = false
        reloadVisibleRows(); updateControls(); rootView.layoutSubtreeIfNeeded()
        let before = visibleRows.map { rows[$0].identity }
        let job = MultiBatchJob(profileIdentity: option.id, nodeName: names[4], yamlSnapshot: Data("synthetic".utf8), index: 0, options: batch.options)
        receiveProgress(job, progress: MultiBatchProgress(phase: .head, receivedBytes: 0, expectedBytes: 1_048_576, bytesPerSecond: 0), token: token)
        check("head progress advances progress bar slightly", progressView.fraction > 0 && progressView.fraction < 0.05 && startButton.title.contains("测速"), "frac=\(progressView.fraction)")
        receiveProgress(job, progress: MultiBatchProgress(phase: .download, receivedBytes: 524_288, expectedBytes: 1_048_576, bytesPerSecond: 4_194_304), token: token)
        check("download progress shows intermediate throughput and byte progress", progressView.fraction > 0.04 && progressView.fraction < 0.1 && status.stringValue.contains("4.00 MiB/s"))
        let liveRowIndex = visibleRows.firstIndex(where: { rows[$0].identity == identity })!
        let liveSpeedCol = resultTable.column(withIdentifier: NSUserInterfaceItemIdentifier("speed"))
        let liveSpeedCell = resultTable.view(atColumn: liveSpeedCol, row: liveRowIndex, makeIfNecessary: true) as? MultiSpeedBarCellView
        check("live testing row cell reflects intermediate throughput and fraction", liveSpeedCell?.speedText == "4.00 MiB/s" && abs((liveSpeedCell?.fraction ?? 0) - 0.5) < 0.01)
        let phaseBeforeTick = testingMotionPhase
        tickTestingMotion()
        check("testing motion advances phase and keeps pulse active", testingMotionPhase > phaseBeforeTick && progressView.isMeasuring)
        let payload: [String: Any] = ["version": 1, "node_name": names[4], "node_type": "VLESS", "head_attempts": 6, "head_successes": 6, "latency_ms": 42, "jitter_ms": 4, "expected_bytes": 1_048_576, "received_bytes": 1_048_576, "download_complete": true, "download_bytes_per_second": 18_720_000]
        receive(job, result: .success([String(data: try JSONSerialization.data(withJSONObject: payload), encoding: .utf8)!]), token: token)
        check("receiving a score preserves screen order", before == visibleRows.map { rows[$0].identity })
        check("champion updates independently of table position", champNodeLabel.stringValue == names[4])
        check("numeric result is final without count-up", champSpeedLabel.stringValue == String(format: "%.2f", 18_720_000.0 / 1_048_576))
        check("progress reports a real completed node", abs(progressView.fraction - 0.1) < 0.001)
        check("progress has one finite transition", progressView.layer?.sublayers?.first?.sublayers?.first?.animation(forKey: "progress")?.duration == 0.45)
        try save("native-testing-1240.png", NSSize(width: 1240, height: 800), .darkAqua)
        let feedbackDeadline = ProcessInfo.processInfo.systemUptime + 1
        while feedbackTimer != nil && ProcessInfo.processInfo.systemUptime < feedbackDeadline {
            _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        check("champion feedback stops after its finite transition", feedbackTimer == nil && champCardView.crownPulseIntensity == 0)
        FixtureMotion.reduced = true
        progressView.setFraction(0.7); pulseChampionFeedback()
        check("reduced motion removes the progress animation", progressView.layer?.sublayers?.first?.sublayers?.first?.animationKeys()?.isEmpty ?? true)
        tickTestingMotion()
        check("reduced motion preserves progress and disables pulse", progressView.fraction == 0.7 && feedbackTimer == nil && champCardView.crownPulseIntensity == 0 && testingMotionTimer == nil && !progressView.isMeasuring)
        busy = false; updateControls()
        let checkView = resultTable.view(atColumn: 0, row: 0, makeIfNecessary: true) as? NSButton
        check("node checkbox re-enables after testing", checkView?.isEnabled == true)
        currentBatch = nil; rows = multiPreviewRows(batch); reloadVisibleRows(); updateControls()
        check("fresh preview does not inherit old progress", progressView.isHidden && progressView.fraction == 0)

        // State machine checks: switchingProfile / applying champion pill, busy table selection guard, and stop during switchingProfile.
        for i in 0..<5 {
            rows[i].measurement = VerifiedMeasurement(nodeName: names[i], nodeType: "VLESS", headAttempts: 6, headSuccesses: 6, latencyMilliseconds: Double(40 + i * 10), jitterMilliseconds: 3.0, expectedBytes: 1_048_576, receivedBytes: 1_048_576, downloadComplete: true, downloadBytesPerSecond: Double(10 + i * 3) * 1_048_576)
            rows[i].state = "有效"
        }
        reloadVisibleRows()
        check("heroOrHighlightedRow uses highest score even before rows sort", heroOrHighlightedRow()?.nodeName == names[4])
        busy = true; currentBatch = nil; switchingProfile = true; updateControls()
        check("champion badge distinguishes runtime profile switch", champReadyLabel.stringValue == "● 正在热切换订阅")
        status.stringValue = "正在通过 Clash Verge 运行时补丁切换 ‘示例订阅’…"
        resultTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification, object: resultTable))
        check("busy selection change does not overwrite active status", status.stringValue == "正在通过 Clash Verge 运行时补丁切换 ‘示例订阅’…")
        stop(nil)
        check("stop cancels switchingProfile and clears busy", !switchingProfile && !busy && status.stringValue == "已停止热切换订阅；未继续应用节点。")
        busy = true; applying = true; updateControls()
        check("champion badge distinguishes node apply", champReadyLabel.stringValue == "● 正在切换节点")
        applying = false; busy = false; updateControls()

        // refreshAfterProfileSwitch preserves measured rows and updates registry/selectionPreview current flag & registry snapshot.
        let switchRoot = root
        let profilesDir = switchRoot.appendingPathComponent("profiles", isDirectory: true)
        try FileManager.default.createDirectory(at: profilesDir, withIntermediateDirectories: true)
        let yamlA = "proxies:\n  - {name: A1, type: ss, server: 1.1.1.1, port: 443, cipher: aes-128-gcm, password: p}\n"
        let yamlB = "proxies:\n  - {name: B1, type: vless, server: 2.2.2.2, port: 443, uuid: 11111111-1111-1111-1111-111111111111}\n"
        try yamlA.write(to: profilesDir.appendingPathComponent("pA.yaml"), atomically: true, encoding: .utf8)
        try yamlB.write(to: profilesDir.appendingPathComponent("pB.yaml"), atomically: true, encoding: .utf8)
        let profilesYaml = switchRoot.appendingPathComponent("profiles.yaml")
        func writeProfilesYaml(current: String) throws {
            let text = """
            current: \(current)
            items:
              - uid: uidA
                type: remote
                name: SubA
                file: pA.yaml
              - uid: uidB
                type: remote
                name: SubB
                file: pB.yaml
            """
            try text.write(to: profilesYaml, atomically: true, encoding: .utf8)
        }
        try writeProfilesYaml(current: "uidA")
        let initialReg = try ProfileRegistry.options(root: switchRoot)
        let optA = initialReg.first(where: { $0.uid == "uidA" })!
        let optB = initialReg.first(where: { $0.uid == "uidB" })!
        let initialBatch = try multiPrepare(root: switchRoot, registry: initialReg, selected: [optA.id, optB.id],
                                            options: MultiBatchOptions(downloadBytes: 1_048_576, streams: 2, latencyTimeout: "3s", downloadTimeout: "3s"),
                                            parallel: 2, previewOnly: true)
        registry = initialReg; selected = [optA.id, optB.id]; selectionPreview = initialBatch
        rows = multiPreviewRows(initialBatch)
        nodeSelection = Set(rows.map(\.identity))
        rows[1].measurement = VerifiedMeasurement(nodeName: "B1", nodeType: "VLESS", headAttempts: 6, headSuccesses: 6, latencyMilliseconds: 25, jitterMilliseconds: 2, expectedBytes: 20_971_520, receivedBytes: 20_971_520, downloadComplete: true, downloadBytesPerSecond: 15_728_640)
        rows[1].state = "有效"
        let bSnap = initialBatch.profiles.first(where: { $0.option.uid == "uidB" })!
        pendingWinner = MultiPendingWinner(profile: bSnap, node: "B1", measurement: rows[1].measurement!)
        reloadVisibleRows(); updateControls()
        try writeProfilesYaml(current: "uidB")
        cancelled = false
        refreshAfterProfileSwitch()
        let switchDeadline = ProcessInfo.processInfo.systemUptime + 5
        while (busy || registry.first(where: { $0.isCurrent })?.uid != "uidB") && ProcessInfo.processInfo.systemUptime < switchDeadline {
            _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        let freshReg = try ProfileRegistry.options(root: switchRoot)
        let freshPrepare = try multiPrepare(root: switchRoot, registry: freshReg, selected: [optA.id, optB.id],
                                            options: initialBatch.options, parallel: 2, previewOnly: true)
        check("refreshAfterProfileSwitch updates current profile", registry.first(where: { $0.isCurrent })?.uid == "uidB")
        check("refreshAfterProfileSwitch preserves measured rows", rows.count == 2 && rows[1].measurement?.receivedBytes == 20_971_520 && pendingWinner?.node == "B1")
        check("refreshAfterProfileSwitch keeps selectionPreview revalidatable", selectionPreview.map { multiSameSnapshots($0, freshPrepare) } == true)
        let pillsBefore = subPillsStack.arrangedSubviews.map { ObjectIdentifier($0) }
        topNBox.stringValue = "1"
        selectTopNNodes(nil)
        let topNStatus = status.stringValue
        applicationDidBecomeActive(Notification(name: NSApplication.didBecomeActiveNotification))
        let immediateNotBlocked = !groupRefreshInFlight && startButton.isEnabled && funnelButton.isEnabled && champQuickSwitchButton.isEnabled
        _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.08))
        check("applicationDidBecomeActive does not block buttons", immediateNotBlocked)
        check("applicationDidBecomeActive preserves selectTopN status", status.stringValue == topNStatus && topNStatus.contains("已勾选当前列表前 1 个节点"))
        copyResults(nil)
        let exportStatus = status.stringValue
        applicationDidBecomeActive(Notification(name: NSApplication.didBecomeActiveNotification))
        _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.08))
        check("applicationDidBecomeActive preserves copyResults status", status.stringValue == exportStatus && exportStatus.contains("已复制结果"))
        let pillsAfter = subPillsStack.arrangedSubviews.map { ObjectIdentifier($0) }
        check("rebuildSubscriptionPills reuses unchanged pills", !pillsBefore.isEmpty && pillsBefore == pillsAfter)
        clearSortColumn()
        if let scoreCol = resultTable.tableColumns.first(where: { $0.identifier.rawValue == "score" }) {
            tableView(resultTable, didClick: scoreCol)
            let descDir = (scoreCol.headerCell as? MultiTableHeaderCell)?.sortDirection
            tableView(resultTable, didClick: scoreCol)
            let ascDir = (scoreCol.headerCell as? MultiTableHeaderCell)?.sortDirection
            clearSortColumn()
            let clearedDir = (scoreCol.headerCell as? MultiTableHeaderCell)?.sortDirection
            check("table header displays and clears sort direction indicator",
                  descDir == -1 && ascDir == 1 && clearedDir == 0 && sortColumnIdentifier == nil,
                  "desc=\(String(describing: descDir)) asc=\(String(describing: ascDir)) cleared=\(String(describing: clearedDir))")
        } else {
            check("table header displays and clears sort direction indicator", false, "missing score column")
        }
        window.close()
        return checks
    }
}
@main struct NativeGraphiteReview {
    static func main() throws {
        let app = NSApplication.shared; app.setActivationPolicy(.prohibited)
        let delegate = MultiAppDelegate()
        let output = URL(fileURLWithPath: CommandLine.arguments[1])
        let checks = try delegate.verifyGraphite(output)
        let receipt: [String: Any] = ["scope": "real AppKit views, synthetic data only", "checks": checks, "passed": checks.allSatisfy { $0["passed"] as? Bool == true }]
        let data = try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: output.appendingPathComponent("native-qa.json"))
        FileHandle.standardOutput.write(data)
        if !(receipt["passed"] as! Bool) { exit(1) }
    }
}
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Keep synthetic screenshots and QA JSON in this directory.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('macOS with an active WindowServer is required')
    OUT = Path(args.output).expanduser().resolve() if args.output else None
    with tempfile.TemporaryDirectory(prefix='clash-graphite-review-', dir='/private/tmp') as temporary:
        work = Path(temporary)
        if OUT is None:
            OUT = work / 'renders'
        OUT.mkdir(parents=True, exist_ok=True)
        sources = ['MultiMain.swift', 'ProfileRegistry.swift', 'MultiBatchRunner.swift', 'VerifiedMeasurement.swift', 'CompositeScore.swift', 'TestSchedule.swift', 'SwitchController.swift', 'AssistedSwitch.swift', 'TelemetryHistory.swift']
        for name in sources:
            shutil.copyfile(PROJECT / 'Sources' / name, work / name)
        main = work / 'MultiMain.swift'
        text = main.read_text()
        anchor = 'static var reducesMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }'
        assert text.count(anchor) == 1
        root_anchor = 'private let root = ProfileRegistry.defaultRoot'
        assert text.count(root_anchor) == 1
        patched = text.replace(anchor, 'static var reducesMotion: Bool { FixtureMotion.reduced }')
        patched = patched.replace(root_anchor, f'private let root = URL(fileURLWithPath: {json.dumps(str(work / "switch-fixture-root"))})')
        main.write_text(patched + DRIVER)
        home = work / 'home'
        home.mkdir(mode=0o700, exist_ok=True)
        env = os.environ.copy()
        cache = os.environ.get('CLASH_GRAPHITE_REVIEW_CACHE', str(work / 'cache'))
        env.update(HOME=str(home), CFFIXED_USER_HOME=str(home), CLANG_MODULE_CACHE_PATH=cache, SWIFT_MODULECACHE_PATH=cache)
        result = subprocess.run(['/usr/bin/swiftc', '-swift-version', '5', '-target', 'arm64-apple-macos12.0', '-D', 'MULTI_UI_TEST', '-parse-as-library', '-framework', 'AppKit', *[str(work / name) for name in sources], '-o', str(work / 'GraphiteReview')], env=env, capture_output=True, text=True, timeout=180)
        if result.returncode:
            print(result.stderr)
            return result.returncode
        result = subprocess.run([str(work / 'GraphiteReview'), str(OUT)], env=env, capture_output=True, text=True, timeout=45)
        (OUT / 'native-render.log').write_text(result.stderr)
        print(result.stdout)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
