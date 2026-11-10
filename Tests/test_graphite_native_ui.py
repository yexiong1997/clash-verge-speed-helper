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
        let batch = MultiPreparedBatch(registry: [option], registryData: Data(), registryVersion: MultiFileVersion(stat()), profiles: [profile], options: MultiBatchOptions(downloadBytes: 1_048_576, streams: 2, latencyTimeout: "1s", downloadTimeout: "3s"), parallel: 10, jobCount: names.count, requestedBytes: names.count * 1_048_576, previewOnly: true)
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
            [("export", copyButton), ("funnel", funnelButton), ("load", loadTestButton), ("stop", stopButton), ("start", startButton)],
            [("parallel", parallelBox), ("size", sizePopup), ("mode", modePopup), ("interval", intervalPopup), ("schedule", scheduleButton)],
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
        check("reduced motion preserves progress and disables pulse", progressView.fraction == 0.7 && feedbackTimer == nil && champCardView.crownPulseIntensity == 0)
        busy = false; updateControls()
        let checkView = resultTable.view(atColumn: 0, row: 0, makeIfNecessary: true) as? NSButton
        check("node checkbox re-enables after testing", checkView?.isEnabled == true)
        currentBatch = nil; rows = multiPreviewRows(batch); reloadVisibleRows(); updateControls()
        check("fresh preview does not inherit old progress", progressView.isHidden && progressView.fraction == 0)
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
        sources = ['MultiMain.swift', 'ProfileRegistry.swift', 'MultiBatchRunner.swift', 'VerifiedMeasurement.swift', 'CompositeScore.swift', 'TestSchedule.swift', 'SwitchController.swift', 'AssistedSwitch.swift']
        for name in sources:
            shutil.copyfile(PROJECT / 'Sources' / name, work / name)
        main = work / 'MultiMain.swift'
        text = main.read_text()
        anchor = 'static var reducesMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }'
        assert text.count(anchor) == 1
        main.write_text(text.replace(anchor, 'static var reducesMotion: Bool { FixtureMotion.reduced }') + DRIVER)
        env = os.environ.copy()
        cache = os.environ.get('CLASH_GRAPHITE_REVIEW_CACHE', str(work / 'cache'))
        env.update(CLANG_MODULE_CACHE_PATH=cache, SWIFT_MODULECACHE_PATH=cache)
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
