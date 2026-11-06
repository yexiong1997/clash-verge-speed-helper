#!/usr/bin/env python3
"""Private synthetic macOS multi-subscription integration test.

Run: python3 -B Tests/test_multi_e2e.py. The app and all descendants are
sandboxed after mandatory confinement canaries; all test output is sanitized.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

PROJECT = Path(__file__).resolve().parent.parent
SOURCES = ('MultiMain.swift', 'ProfileRegistry.swift', 'MultiBatchRunner.swift',
           'VerifiedMeasurement.swift', 'CompositeScore.swift', 'TestSchedule.swift',
           'SwitchController.swift', 'AssistedSwitch.swift')
A_NODES = tuple('A%02d' % i for i in range(1, 13))
B_NODES = ('B01', 'B02')
SELECT_A = ('A01', 'Shared', 'é', 'e\u0301')
SELECT_B = ('B01', 'Shared', 'é', 'e\u0301')
# Distinct byte identities despite Swift String's canonical-equivalence rules.
UID_A, UID_B = 'é', 'e\u0301'
SELECTION_CASES = ('select-preview', 'select-exact', 'select-stale', 'select-invalid', 'manual-unicode')
ALL_NODES = A_NODES + B_NODES
GROUP, OTHER_GROUP = 'SyntheticSelect', 'SyntheticOther'
TARGET = 'http://127.0.0.1:1/synthetic-disabled'
SCENARIOS = ('startup', 'same', 'other', 'stop', 'scheduled-stale', 'during-stale',
             'group-stale', 'now-stale', 'preput-stale', 'allfailed', 'uncertain',
             'delayed', 'close-busy', 'schedule-overlap', 'manual-no-auto', 'partial-failure',
             'scheduled-other-no-auto', 'only-other', *SELECTION_CASES, 'zero-selection',
             'load-run', 'manual-same', 'manual-unchecked', 'manual-other', 'manual-revoked',
             'manual-unknown', 'manual-group-stale', 'manual-now-stale',
             'result-ranked', 'multiple-groups', 'mixed-groups', 'global-only', 'old-scores',
             'quick-stop-valid', 'quick-stop-stale', 'quick-load-stop', 'scheduled-decision-disarm',
             'scheduled-quick-stop', 'scheduled-quick-close')

QUICK_STOP_CASES = ('quick-stop-valid', 'quick-stop-stale', 'quick-load-stop',
                    'scheduled-quick-stop', 'scheduled-quick-close')
NO_WORKER_REGRESSIONS = QUICK_STOP_CASES + ('scheduled-decision-disarm',)


class HarnessError(Exception):
    """Only fixed non-secret messages may be passed to this exception."""


def require(ok, label):
    if not ok:
        raise HarnessError(label)


def replace_once(text, old, new, label):
    require(text.count(old) == 1, 'source instrumentation anchor changed: ' + label)
    return text.replace(old, new, 1)


def literal(value):
    value = str(value)
    require(value.isascii() and all(c not in value for c in '"\\\n'), 'unsupported fixture path')
    return '"' + value + '"'


# Appended to the temporary source so private AppDelegate methods remain testable.
SWIFT_DRIVER = r'''
#if MULTI_UI_TEST
enum Fixture {
    static var warnings = 0, peak = 0, stopCount = 0
    static var clockOffset: TimeInterval = 0, stopBeforeApply = false
    static var disarmQueuedDecision = false, decisionCount = 0, runnerLaunches = 0
    static var decisionWasArmedAndEnabled = false, decisionDisarmed = false
    static let root = __ROOT__, events = __EVENTS__, putMarker = __PUTMARKER__, controllerEvents = __CONTROL__
    static func now() -> Date { TestSchedule.monotonicNow().addingTimeInterval(clockOffset) }

    // Both gates are inside the real quick-check body in the disposable copy.
    // The first lets a stale fixture be created after preflight; the second
    // delays either return or throw, simulating an IO result racing with Stop.
    // A bounded fail-safe makes accidental main-thread blocking fail closed.
    private static let quickCondition = NSCondition()
    private static var quickEnabled = false, quickReadReleased = false, quickReturnReleased = false
    private static var quickExpired = false, quickOnMain = false
    private static var quickEntered = 0, quickAtReturn = 0, quickReturned = 0, quickResult: Bool?
    static func enableQuickGate() {
        quickCondition.lock(); quickEnabled = true; quickCondition.unlock()
    }
    static func withQuickGate(_ read: () -> Bool) -> Bool {
        quickCondition.lock(); let enabled = quickEnabled; quickCondition.unlock()
        guard enabled else { return read() }
        Thread.current.threadDictionary["synthetic-quick-gate"] = true
        defer { Thread.current.threadDictionary.removeObject(forKey: "synthetic-quick-gate") }
        let result = read()
        quickCondition.lock(); quickReturned += 1; quickResult = result
        quickCondition.broadcast(); quickCondition.unlock()
        return result
    }
    static func beforeQuickRead() {
        guard Thread.current.threadDictionary["synthetic-quick-gate"] as? Bool == true else { return }
        quickCondition.lock()
        quickEntered += 1; quickOnMain = quickOnMain || Thread.isMainThread
        quickCondition.broadcast()
        let deadline = Date().addingTimeInterval(10)
        while !quickReadReleased {
            if !quickCondition.wait(until: deadline) { quickExpired = true; break }
        }
        quickCondition.unlock()
    }
    static func atQuickReturn() {
        guard Thread.current.threadDictionary["synthetic-quick-gate"] as? Bool == true else { return }
        quickCondition.lock(); quickAtReturn += 1; quickCondition.broadcast()
        let deadline = Date().addingTimeInterval(10)
        while !quickReturnReleased {
            if !quickCondition.wait(until: deadline) { quickExpired = true; break }
        }
        quickCondition.unlock()
    }
    static func releaseQuickRead() {
        quickCondition.lock(); quickReadReleased = true; quickCondition.broadcast(); quickCondition.unlock()
    }
    static func releaseQuickReturn() {
        quickCondition.lock(); quickReturnReleased = true; quickCondition.broadcast(); quickCondition.unlock()
    }
    static func quickState() -> (entered: Int, atReturn: Int, returned: Int, readReleased: Bool,
                                 returnReleased: Bool, expired: Bool, onMain: Bool, result: Bool?) {
        quickCondition.lock(); defer { quickCondition.unlock() }
        return (quickEntered, quickAtReturn, quickReturned, quickReadReleased, quickReturnReleased,
                quickExpired, quickOnMain, quickResult)
    }
}
private extension MultiAppDelegate {
    func syntheticScenario(_ scenario: String) -> Int32 {
        func wait(_ seconds: TimeInterval, _ condition: () -> Bool) -> Bool {
            let until = Date().addingTimeInterval(seconds)
            while !condition() && Date() < until {
                _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            return condition()
        }
        func pause(_ seconds: TimeInterval) { _ = wait(seconds, { false }) }
        func fail(_ code: String) -> Int32 { print("E2E \(scenario) FAIL \(code)"); return 1 }
        func idle() -> Bool { !self.busy && self.runner == nil && self.activeToken == nil }
        func noPut() -> Bool { !FileManager.default.fileExists(atPath: Fixture.putMarker) }
        func started() -> Bool {
            (try? String(contentsOfFile: Fixture.events, encoding: .utf8).contains("S ")) == true
        }
        func click(_ button: NSButton, _ action: Selector) -> Bool {
            guard button.target as AnyObject? === self, button.action == action, button.isEnabled else { return false }
            button.performClick(nil); return true
        }
        func finish() -> Int32 {
            let expectedWarnings = ["uncertain", "manual-unknown"].contains(scenario) ? 1 : 0
            guard Fixture.warnings == expectedWarnings else { return fail("unexpected-warning") }
            closeApp(); print("E2E \(scenario) PASS"); return 0
        }
        func settleDisarmed() -> Bool {
            func bytes(_ path: String) -> Data { (try? Data(contentsOf: URL(fileURLWithPath: path))) ?? Data() }
            let workers = bytes(Fixture.events), controller = bytes(Fixture.controllerEvents)
            Fixture.clockOffset += 5000; self.pollSchedule(); pause(1.2); self.pollSchedule(); pause(0.3)
            if self.schedule.isArmed { print("E2E \(scenario) FAIL settle-armed") }
            if self.runner != nil { print("E2E \(scenario) FAIL settle-runner") }
            if self.busy { print("E2E \(scenario) FAIL settle-busy") }
            if workers != bytes(Fixture.events) { print("E2E \(scenario) FAIL settle-worker-events") }
            if controller != bytes(Fixture.controllerEvents) { print("E2E \(scenario) FAIL settle-controller-events") }
            return !self.schedule.isArmed && self.runner == nil && !self.busy &&
                workers == bytes(Fixture.events) && controller == bytes(Fixture.controllerEvents)
        }
        func arm() -> Bool {
            click(self.scheduleButton, #selector(toggleSchedule(_:))) && wait(30, { !self.busy }) && self.schedule.isArmed
        }
        func registryToB() -> Bool {
            do {
                let changed = try Data(contentsOf: URL(fileURLWithPath: Fixture.root + "/registry-b.json"))
                try changed.write(to: URL(fileURLWithPath: Fixture.root + "/profiles.yaml")); return true
            } catch { return false }
        }
        func mark(_ text: String) -> Bool {
            do { try Data(text.utf8).write(to: URL(fileURLWithPath: Fixture.root + "/mock-state")); return true }
            catch { return false }
        }
        func stale() -> Bool {
            guard let h = FileHandle(forWritingAtPath: Fixture.root + "/profiles/a.yaml") else { return false }
            h.seekToEndOfFile(); h.write(Data("\n# synthetic stale\n".utf8)); h.closeFile(); return true
        }
        func highlight(_ id: MultiIdentity) -> Bool {
            guard let row = visibleRows.firstIndex(where: { rows[$0].identity == id }) else { return false }
            resultTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            return highlightedIdentity() == id
        }
        func setNode(_ id: MultiIdentity, _ checked: Bool) -> Bool {
            guard let row = visibleRows.firstIndex(where: { rows[$0].identity == id }),
                  let column = resultTable.tableColumns.first(where: { $0.identifier.rawValue == "check" }),
                  let box = tableView(resultTable, viewFor: column, row: row) as? MultiNodeCheckbox,
                  box.nodeIdentity == id, box.target as AnyObject? === self, box.action == #selector(toggleNode(_:)) else { return false }
            box.state = checked ? .on : .off
            guard NSApp.sendAction(box.action!, to: box.target, from: box) else { return false }
            return nodeSelection?.contains(id) == checked
        }
        func refreshed(_ uid: Data) -> Bool {
            guard click(refreshGroupsButton, #selector(refreshGroups(_:))) else { return false }
            return wait(30, { self.groupContext?.profile.option.id == uid })
        }
        applicationDidFinishLaunching(Notification(name: Notification.Name("fixture")))
        guard wait(25, { !self.busy }), registry.count == 2 else { return fail("startup-registry") }
        guard !schedule.isArmed, runner == nil, rows.isEmpty, parallelBox.stringValue == "10",
              modePopup.indexOfSelectedItem == 0, sizeValues[sizePopup.indexOfSelectedItem] == 3 * 1_048_576,
              chosenOptions().map({ $0.0.streams == 2 && $0.1 == 10 }) == true, autoSwitch.state == .off,
              startButton.action == #selector(startManual(_:)), loadTestButton.action == #selector(startLoadTest(_:)),
              funnelButton.action == #selector(startFunnelTest(_:)), undoSwitchButton.action == #selector(undoLastSwitch(_:)),
              champQuickSwitchButton.action == #selector(switchChampionRoute(_:)), champRetestButton.action == #selector(retestChampionRoute(_:)),
              switchResultButton.action == #selector(switchSelectedResult(_:)), scheduleButton.action == #selector(toggleSchedule(_:)),
              refreshGroupsButton.action == #selector(refreshGroups(_:)), groupPopup.action == #selector(groupChanged(_:))
        else { return fail("startup-controls") }
        Fixture.clockOffset += 5000; pollSchedule(); pause(0.3)
        guard !started(), noPut(), rows.isEmpty,
              !FileManager.default.fileExists(atPath: Fixture.controllerEvents) else { return fail("startup-implicit-work") }
        if scenario == "startup" { return finish() }
        let selectionCase = ["select-preview", "select-exact", "select-stale", "select-invalid", "manual-unicode"].contains(scenario)
        let uidA = Data((selectionCase ? "\u{00E9}" : "a").utf8), uidB = Data((selectionCase ? "e\u{0301}" : "b").utf8)
        guard uidA != uidB, let aIndex = registry.firstIndex(where: { $0.id == uidA }),
              let bIndex = registry.firstIndex(where: { $0.id == uidB }),
              let profileColumn = profileTable.tableColumns.first(where: { $0.identifier.rawValue == "select" })
        else { return fail("profile-identity") }
        for index in [aIndex, bIndex] {
            if scenario == "only-other" && index == aIndex { continue }
            guard let check = tableView(profileTable, viewFor: profileColumn, row: index) as? NSButton,
                  check.target as AnyObject? === self, check.action == #selector(toggleProfile(_:)) else { return fail("profile-control") }
            check.state = .on
            guard NSApp.sendAction(check.action!, to: check.target, from: check),
                  wait(30, { idle() && self.selectionPreview?.profiles.count == self.selected.count }),
                  self.selected.contains(registry[index].id) else { return fail("profile-selection") }
        }
        guard rows.count == (selectionCase ? 8 : scenario == "only-other" ? 2 : 14),
              nodeSelection?.count == rows.count, selectionPreview?.previewOnly == true,
              selectionPreview.map({ multiJobs($0).isEmpty }) == true, !started(), noPut()
        else { return fail("preview-default") }
        // Group refresh is asynchronous even when the current profile is not
        // in the selected preview; controls must become genuinely ready.
        guard wait(30, { !self.groupRefreshInFlight }) else { return fail("group-refresh-timeout") }
        if scenario != "only-other" {
            guard wait(30, { self.groupContext?.profile.option.id == uidA }) else { return fail("group-preview") }
            if !["multiple-groups", "mixed-groups", "global-only"].contains(scenario) {
                guard selectedGroupCapture()?.profile.option.id == uidA,
                      selectedGroupCapture().map({ multiBytesEqual($0.groupName, "SyntheticSelect") && multiBytesEqual($0.now, "A02") }) == true
                else { return fail("group-default") }
            }
        }
        if scenario == "select-preview" {
            topNBox.stringValue = "3"
            guard click(selectTopNButton, #selector(selectTopNNodes(_:))),
                  nodeSelection?.count == 3,
                  visibleRows.count >= 3,
                  nodeSelection == Set(visibleRows.prefix(3).map { rows[$0].identity }),
                  click(filterValidButton, #selector(statusFilterChanged(_:))),
                  visibleRows.isEmpty,
                  click(filterAllButton, #selector(statusFilterChanged(_:))),
                  visibleRows.count == rows.count
            else { return fail("select-top-n") }
            return finish()
        }
        if ["multiple-groups", "mixed-groups", "global-only"].contains(scenario) {
            guard groupPopup.indexOfSelectedItem == 0, selectedGroupCapture() == nil else { return fail("group-not-explicit") }
            if scenario == "mixed-groups" {
                guard let context = groupContext,
                      Set(context.groups.map { Data($0.name.utf8) }) == Set([Data("GLOBAL".utf8), Data("SyntheticSelect".utf8)])
                else { return fail("mixed-group-candidates") }
            }
            guard highlight(MultiIdentity(profile: uidA, node: "A03")),
                  click(switchResultButton, #selector(switchSelectedResult(_:))), wait(10, { idle() }), noPut()
            else { return fail("group-implicit-switch") }
            if scenario == "global-only" { return finish() }
            guard let context = groupContext, let i = context.groups.firstIndex(where: { multiBytesEqual($0.name, "SyntheticSelect") }) else { return fail("group-choice-missing") }
            guard groupPopup.target as AnyObject? === self, groupPopup.action == #selector(groupChanged(_:)) else { return fail("group-control") }
            groupPopup.selectItem(at: i + 1)
            guard NSApp.sendAction(groupPopup.action!, to: groupPopup.target, from: groupPopup) else { return fail("group-action") }
            guard click(switchResultButton, #selector(switchSelectedResult(_:))), wait(30, { idle() }), !noPut()
            else { return fail("group-explicit-switch") }
            return finish()
        }
        if scenario == "manual-unicode" {
            // Canonically equivalent names and UIDs remain byte-distinct.
            let decomposed = MultiIdentity(profile: uidA, node: "e\u{0301}")
            guard highlight(decomposed), click(switchResultButton, #selector(switchSelectedResult(_:))),
                  wait(30, { idle() }), !started(), !noPut(), status.stringValue.contains("已切换")
            else { return fail("manual-unicode-switch") }
            return finish()
        }
        if selectionCase {
            guard click(clearShownButton, #selector(clearShownNodes(_:))), nodeSelection?.isEmpty == true else { return fail("clear-selection") }
            let shared = MultiIdentity(profile: uidA, node: "Shared")
            let composed = MultiIdentity(profile: uidA, node: "\u{00E9}")
            let decomposed = MultiIdentity(profile: uidB, node: "e\u{0301}")
            guard highlight(MultiIdentity(profile: uidB, node: "Shared")), setNode(shared, true),
                  setNode(composed, true), setNode(decomposed, true), nodeSelection?.count == 3,
                  highlightedIdentity() == MultiIdentity(profile: uidB, node: "Shared"),
                  nodeSelection?.contains(MultiIdentity(profile: uidB, node: "Shared")) == false
            else { return fail("selection-highlight-identity") }
            if scenario == "select-stale" { guard stale() else { return fail("fixture-open") } }
            if scenario == "select-invalid" { nodeSelection?.insert(MultiIdentity(profile: uidA, node: "not-in-snapshot")) }
            if scenario != "select-exact" {
                autoSwitch.state = .off; startManual(nil)
                guard wait(30, { idle() }), !started(), noPut(), settleDisarmed() else { return fail("stale-selection-accepted") }
                return finish()
            }
        }
        if scenario == "zero-selection" {
            guard click(clearShownButton, #selector(clearShownNodes(_:))), nodeSelection?.isEmpty == true else { return fail("zero-clear") }
            guard !startButton.isEnabled, !scheduleButton.isEnabled else { return fail("zero-controls-enabled") }
            autoSwitch.state = .off; startManual(nil)
            guard wait(30, { idle() }), !started(), noPut(), settleDisarmed() else { return fail("zero-launched") }
            return finish()
        }
        if scenario == "manual-group-stale" || scenario == "manual-now-stale" {
            let changed = scenario == "manual-group-stale" ? "group-stale" : "now-stale"
            guard mark(changed), highlight(MultiIdentity(profile: uidA, node: "A03")),
                  click(switchResultButton, #selector(switchSelectedResult(_:))), wait(30, { idle() }),
                  !started(), noPut(), settleDisarmed() else { return fail("manual-stale-capture") }
            return finish()
        }
        if ["manual-same", "manual-unchecked", "manual-other", "manual-revoked", "manual-unknown"].contains(scenario) {
            let id = MultiIdentity(profile: scenario == "manual-other" ? uidB : uidA, node: scenario == "manual-other" ? "B01" : "A03")
            if scenario == "manual-unchecked" { guard setNode(id, false) else { return fail("manual-uncheck") } }
            guard highlight(id) else { return fail("manual-highlight") }
            if scenario == "manual-revoked" { Fixture.stopBeforeApply = true }
            guard click(switchResultButton, #selector(switchSelectedResult(_:))), wait(30, { idle() }), !started() else { return fail("manual-initial") }
            if scenario == "manual-other" {
                guard pendingManual != nil, noPut(), click(resumeButton, #selector(resumeWinner(_:))), wait(10, { idle() }), noPut(),
                      registryToB(), click(resumeButton, #selector(resumeWinner(_:))), wait(10, { idle() }), noPut(),
                      refreshed(uidB), pendingManual != nil, click(resumeButton, #selector(resumeWinner(_:))), wait(30, { idle() }),
                      pendingManual == nil, !noPut(), status.stringValue.contains("已切换") else { return fail("manual-other-continuation") }
            } else if scenario == "manual-revoked" {
                guard Fixture.stopCount == 1, noPut(), settleDisarmed() else { return fail("manual-revocation") }
            } else if scenario == "manual-unknown" {
                guard switchOutcomeUnknown, !noPut(), !schedule.isArmed else { return fail("manual-unknown") }
                switchSelectedResult(nil)
                guard wait(5, { idle() }), settleDisarmed() else { return fail("manual-unknown-retry") }
            } else { guard !noPut(), status.stringValue.contains("已切换") else { return fail("manual-switch") } }
            return finish()
        }
        autoSwitch.state = ["schedule-overlap", "manual-no-auto", "scheduled-other-no-auto", "select-exact", "load-run", "result-ranked", "old-scores"].contains(scenario) ? .off : .on
        let quickStop = ["quick-stop-valid", "quick-stop-stale", "quick-load-stop", "scheduled-quick-stop", "scheduled-quick-close"].contains(scenario)
        if ["stop", "allfailed", "close-busy", "schedule-overlap", "scheduled-stale", "partial-failure", "scheduled-other-no-auto", "load-run", "scheduled-decision-disarm"].contains(scenario) || quickStop {
            guard arm(), !started() else { return fail("schedule-arm") }
        }
        if quickStop { Fixture.enableQuickGate() }
        if scenario == "scheduled-decision-disarm" { Fixture.disarmQueuedDecision = true }
        if scenario == "scheduled-stale" { guard stale() else { return fail("fixture-open") } }
        if ["group-stale", "now-stale", "preput-stale"].contains(scenario) { guard mark(scenario) else { return fail("fixture-mark") } }
        if ["schedule-overlap", "allfailed", "scheduled-stale", "partial-failure", "scheduled-other-no-auto", "scheduled-decision-disarm", "scheduled-quick-stop", "scheduled-quick-close"].contains(scenario) {
            Fixture.clockOffset += 901; pollSchedule()
        } else if scenario == "load-run" || scenario == "quick-load-stop" {
            guard click(loadTestButton, #selector(startLoadTest(_:))), parallelBox.stringValue == "2", modePopup.indexOfSelectedItem == 1,
                  sizeValues[sizePopup.indexOfSelectedItem] == 20 * 1_048_576, !schedule.isArmed else { return fail("load-preset") }
        } else { guard click(startButton, #selector(startManual(_:))) else { return fail("start-control") } }
        if ["scheduled-stale", "group-stale", "now-stale"].contains(scenario) {
            guard wait(30, { idle() }), !started(), noPut(), settleDisarmed() else { return fail("prelaunch-stop") }
            return finish()
        }
        if quickStop {
            guard wait(8, { Fixture.quickState().entered == 1 }), runner != nil, busy,
                  !Fixture.quickState().onMain, !Fixture.quickState().expired,
                  !Fixture.quickState().readReleased, !started(), noPut(), Fixture.runnerLaunches == 0
            else { return fail("quick-read-not-blocked") }
            if scenario == "quick-stop-stale" { guard stale() else { return fail("fixture-open") } }
            Fixture.releaseQuickRead()
            guard wait(8, { Fixture.quickState().atReturn == 1 }),
                  Fixture.quickState().returned == 0, !Fixture.quickState().returnReleased,
                  !Fixture.quickState().expired, runner != nil, busy, !started(), noPut()
            else { return fail("quick-result-not-blocked") }
            var heartbeat = false
            DispatchQueue.main.async { heartbeat = true }
            guard wait(1, { heartbeat }) else { return fail("quick-main-unresponsive") }
            let stopTime = Date()
            if scenario == "scheduled-quick-close" {
                guard !windowShouldClose(window) else { return fail("quick-close-allowed-while-busy") }
            } else {
                guard click(stopButton, #selector(stop(_:))) else { return fail("quick-stop-control") }
            }
            guard wait(2, { idle() }), Date().timeIntervalSince(stopTime) < 2.5, !schedule.isArmed,
                  Fixture.quickState().atReturn == 1, Fixture.quickState().returned == 0,
                  !Fixture.quickState().returnReleased, !Fixture.quickState().expired,
                  !started(), noPut(), Fixture.runnerLaunches == 0
            else { return fail("quick-stop-unresponsive") }
            if scenario == "scheduled-quick-stop" {
                guard status.stringValue.contains("测速已停止"), !status.stringValue.contains("正在停止")
                else { return fail("scheduled-stop-final-status") }
            } else if scenario == "scheduled-quick-close" {
                guard status.stringValue.contains("可再次关闭窗口"), !status.stringValue.contains("正在停止")
                else { return fail("scheduled-close-final-status") }
            }
            let stoppedStatus = status.stringValue, stoppedRows = rows.map(\.state)
            Fixture.releaseQuickReturn()
            guard wait(5, { Fixture.quickState().returned == 1 }),
                  Fixture.quickState().result == (scenario != "quick-stop-stale"),
                  !Fixture.quickState().expired else { return fail("quick-late-result") }
            pause(0.3)
            guard idle(), !schedule.isArmed, pendingWinner == nil, pendingManual == nil,
                  status.stringValue == stoppedStatus, rows.map(\.state) == stoppedRows,
                  rows.allSatisfy({ $0.measurement == nil }), Fixture.quickState().entered == 1,
                  Fixture.runnerLaunches == 0, !started(), noPut(), settleDisarmed()
            else { return fail("quick-late-launch-or-mutation") }
            return finish()
        }
        if scenario == "scheduled-decision-disarm" {
            guard wait(30, { Fixture.decisionCount > 0 }), Fixture.decisionCount == 1,
                  Fixture.decisionWasArmedAndEnabled, Fixture.decisionDisarmed,
                  wait(5, { idle() }), !schedule.isArmed, Fixture.runnerLaunches == 0,
                  !started(), noPut(), settleDisarmed()
            else { return fail("scheduled-queued-decision-launched") }
            return finish()
        }
        guard wait(35, { self.runner != nil && started() }) else { return fail("worker-start") }
        guard currentBatch?.parallel == (scenario == "load-run" ? 2 : 10) else { return fail("batch-parallel") }
        if scenario == "during-stale" { guard stale() else { return fail("fixture-open") } }
        if scenario == "stop" || scenario == "close-busy" {
            if scenario == "stop" { stop(nil) } else if windowShouldClose(window) { return fail("close-allowed-while-busy") }
            guard wait(30, { idle() }), noPut(), settleDisarmed() else { return fail("stop-or-close") }
            return finish()
        }
        if scenario == "schedule-overlap" {
            let token = activeToken; Fixture.clockOffset += 901; pollSchedule()
            guard activeToken == token, runner != nil, schedule.isArmed else { return fail("schedule-overlap") }
        }
        if scenario == "delayed" { guard wait(40, { !noPut() }), applying else { return fail("delayed-put-not-observed") }; stop(nil) }
        guard wait(30, { idle() }), Fixture.peak >= 2 && Fixture.peak <= 10 else { return fail("finish-or-peak") }
        switch scenario {
        case "select-exact":
            let expected = Set([MultiIdentity(profile: uidA, node: "Shared"), MultiIdentity(profile: uidA, node: "\u{00E9}"), MultiIdentity(profile: uidB, node: "e\u{0301}")])
            guard rows.count == 8, Set(rows.filter({ $0.measurement?.score != nil }).map(\.identity)) == expected,
                  rows.filter({ !expected.contains($0.identity) }).allSatisfy({ $0.measurement == nil }), noPut() else { return fail("selected-rows") }
        case "load-run":
            guard rows.count == 14, rows.allSatisfy({ $0.measurement?.score != nil }), Fixture.peak <= 2, noPut(), !schedule.isArmed else { return fail("load-execution") }
        case "result-ranked":
            guard rows.count == 14, rows[0].nodeName == "A12", rows[0].index == 11, noPut(), highlight(rows[0].identity),
                  click(switchResultButton, #selector(switchSelectedResult(_:))), wait(30, { idle() }), !noPut() else { return fail("ranked-result-switch") }
        case "same":
            guard rows.count == 14, rows.allSatisfy({ $0.measurement?.score != nil }), rows.first?.nodeName == "A01", !noPut(), status.stringValue.contains("已切换") else { return fail("same-winner") }
        case "partial-failure":
            guard rows.count == 14, rows.filter({ $0.eligibleScore == nil }).count == 2, rows.first?.nodeName == "A01", !noPut(), schedule.isArmed else { return fail("partial-failure-policy") }
        case "manual-no-auto": guard rows.count == 14, rows.first?.nodeName == "A01", noPut() else { return fail("manual-no-auto") }
        case "scheduled-other-no-auto": guard rows.count == 14, rows.first?.nodeName == "B01", pendingWinner == nil, noPut(), schedule.isArmed else { return fail("scheduled-other-no-auto-policy") }
        case "other", "only-other":
            guard rows.first?.nodeName == "B01", pendingWinner != nil, noPut(), !schedule.isArmed,
                  click(resumeButton, #selector(resumeWinner(_:))), wait(10, { idle() }), noPut(), registryToB(),
                  click(resumeButton, #selector(resumeWinner(_:))), wait(10, { idle() }), noPut(),
                  refreshed(uidB), pendingWinner != nil, click(resumeButton, #selector(resumeWinner(_:))), wait(30, { idle() }),
                  pendingWinner == nil, !noPut(), status.stringValue.contains("已切换") else { return fail("other-continuation") }
        case "during-stale", "preput-stale": guard noPut(), settleDisarmed() else { return fail("stale-not-rejected") }
        case "allfailed": guard rows.count == 14, rows.allSatisfy({ $0.eligibleScore == nil }), noPut(), settleDisarmed() else { return fail("allfailed-not-disarmed") }
        case "uncertain":
            guard switchOutcomeUnknown, !schedule.isArmed, autoSwitch.state == .off else { return fail("uncertain-state") }
            autoSwitch.state = .on; startManual(nil)
            guard wait(25, { idle() }), switchOutcomeUnknown, settleDisarmed() else { return fail("uncertain-retry") }
        case "delayed":
            // The acknowledged in-flight PUT may trigger one read-only group
            // refresh. Synchronize on its captured now before probing for retries.
            guard wait(30, { self.selectedGroupCapture()?.now == "A01" }),
                  !schedule.isArmed, settleDisarmed() else { return fail("delayed-retry") }
        case "schedule-overlap":
            guard rows.count == 14, schedule.isArmed, runner == nil else { return fail("schedule-completion") }
            pollSchedule(); pause(0.4); guard runner == nil, !busy else { return fail("scheduled-catchup") }
        case "old-scores":
            guard rows.count == 14, rows.allSatisfy({ $0.measurement?.score != nil }),
                  click(clearShownButton, #selector(clearShownNodes(_:))),
                  setNode(MultiIdentity(profile: uidA, node: "A12"), true),
                  setNode(MultiIdentity(profile: uidB, node: "B02"), true),
                  click(startButton, #selector(startManual(_:))), wait(30, { self.runner != nil && started() }),
                  rows.allSatisfy({ $0.measurement == nil }), wait(30, { idle() }) else { return fail("old-scores-start") }
            let expected = Set([MultiIdentity(profile: uidA, node: "A12"), MultiIdentity(profile: uidB, node: "B02")])
            guard Set(rows.filter({ $0.measurement?.score != nil }).map(\.identity)) == expected,
                  rows.filter({ !expected.contains($0.identity) }).allSatisfy({ $0.measurement == nil }), noPut() else { return fail("old-scores-retained") }
        default: return fail("unknown-case")
        }
        return finish()
    }
}
@main private struct SyntheticMain {
    static func main() {
        let app = NSApplication.shared; app.setActivationPolicy(.prohibited)
        guard CommandLine.arguments.count == 2 else { print("E2E invalid arguments"); exit(1) }
        let delegate = MultiAppDelegate(); exit(delegate.syntheticScenario(CommandLine.arguments[1]))
    }
}
#endif
'''

def instrument(source, fixture_root, event_file, put_marker, socket_path):
    source = replace_once(source, 'private let root = ProfileRegistry.defaultRoot',
                          'private let root = URL(fileURLWithPath: ' + literal(fixture_root) + ')', 'profile root')
    source = replace_once(source,
        'var paths = ["/var/run/clash-verge-service/users/\\(geteuid())/verge-mihomo.sock"]\n'
        '        if let configured = try multiConfiguredSocket(root: root) { paths.append(configured) }',
        'var paths: [String] = []\n'
        '        if let configured = try multiConfiguredSocket(root: root), configured == ' + literal(socket_path) + ' { paths.append(configured) }',
        'remove service socket and require fixture socket')
    require('/var/run/clash-verge-service' not in source, 'service socket reference remains')
    source = replace_once(source,
        'latencyTimeout: quick ? "2s" : "5s", downloadTimeout: quick ? "12s" : "30s"), parallel)',
        'latencyTimeout: quick ? "2s" : "5s", downloadTimeout: quick ? "12s" : "30s", serverURL: ' + literal(TARGET) + '), parallel)',
        'offline synthetic target')
    source = replace_once(source, '        window.makeKeyAndOrderFront(nil)\n        NSApp.activate(ignoringOtherApps: true)',
                          '        // Fixture: construct but do not present the window.', 'window presentation')
    source = replace_once(source,
        'let alert = NSAlert(); alert.alertStyle = .warning; alert.messageText = title; alert.informativeText = message; alert.runModal()',
        'Fixture.warnings += 1', 'warning dialog')
    # The warning above is intercepted solely to prevent a failing case from
    # blocking; every other modal is forbidden, not silently authorized.
    require(re.search(r'\.\s*(?:runModal|runModalSession|beginSheet|beginSheetModal)\s*\(', source) is None and
            re.search(r'\bNSAlert\s*\(', source) is None, 'unexpected additional alert or modal remains')
    for anchor in (
        'startButton.action = #selector(startManual(_:))',
        'loadTestButton.action = #selector(startLoadTest(_:))',
        'switchResultButton.action = #selector(switchSelectedResult(_:))',
        'scheduleButton.action = #selector(toggleSchedule(_:))',
        'refreshGroupsButton.action = #selector(refreshGroups(_:))',
        'groupPopup.action = #selector(groupChanged(_:))',
        'action: #selector(toggleProfile(_:))', 'action: #selector(toggleNode(_:))',
        'clearShownButton.action = #selector(clearShownNodes(_:))',
        'resumeButton.action = #selector(resumeWinner(_:))',
        'let runner = MultiBatchRunner(executable: executable, jobs: jobs, parallel: batch.parallel,',
    ):
        require(source.count(anchor) == 1, 'UI action anchor changed: ' + anchor)
    source = replace_once(source,
        'self.pendingManual = nil; self.applying = true\n                            let authorization = MultiSwitchAuthorization(); self.switchAuthorization = authorization',
        'self.pendingManual = nil; self.applying = true\n                            let authorization = MultiSwitchAuthorization(); self.switchAuthorization = authorization\n                            if Fixture.stopBeforeApply { Fixture.stopCount += 1; self.stop(nil) }',
        'manual pre-apply stop')
    source = replace_once(source,
        'private func finished(_ summary: MultiBatchSummary, token: TestSchedule.RunToken) {',
        'private func finished(_ summary: MultiBatchSummary, token: TestSchedule.RunToken) {\n        Fixture.peak = summary.peakWorkers',
        'runner peak observation')
    # Delay the real check itself, not a fake replacement. Thread-local marking
    # limits the two gates to the runner's per-job call, leaving all preflights
    # and other scenarios unchanged. Defer covers both successful and thrown IO.
    quick_definition = 'func multiQuickRevalidate(root: URL, batch: MultiPreparedBatch, mayContinue: () -> Bool = { true }) throws {'
    source = replace_once(source, quick_definition,
                          quick_definition + '\n    Fixture.beforeQuickRead(); defer { Fixture.atQuickReturn() }',
                          'quick-check body')
    quick_call = ('let valid = (try? multiQuickRevalidate(root: root, batch: batch,\n'
                  '                                                           mayContinue: { authorization.permits() })) != nil')
    source = replace_once(source, quick_call,
                          'let valid = Fixture.withQuickGate { (try? multiQuickRevalidate(root: root, batch: batch,\n'
                          '                                                           mayContinue: { authorization.permits() })) != nil }',
                          'per-job quick-check call')
    # completion(true) queues the runner decision on main. Click the genuine
    # schedule control in the *same* main-thread turn before it can consume it.
    source = replace_once(source, 'case .passed: completion(true)', '''case .passed:
                        completion(true)
                        if Fixture.disarmQueuedDecision {
                            Fixture.decisionCount += 1
                            Fixture.decisionWasArmedAndEnabled = Thread.isMainThread && self.runScheduled && self.busy &&
                                self.runner != nil && self.schedule.isArmed && self.scheduleButton.isEnabled &&
                                self.scheduleButton.target as AnyObject? === self &&
                                self.scheduleButton.action == #selector(self.toggleSchedule(_:))
                            if Fixture.decisionWasArmedAndEnabled { self.scheduleButton.performClick(nil) }
                            Fixture.decisionDisarmed = !self.schedule.isArmed
                        }''', 'queued scheduled approval')
    require(source.count('TestSchedule.monotonicNow()') >= 3, 'clock instrumentation anchor changed')
    source = source.replace('TestSchedule.monotonicNow()', 'Fixture.now()')
    driver = (SWIFT_DRIVER.replace('__ROOT__', literal(fixture_root))
              .replace('__EVENTS__', literal(event_file)).replace('__PUTMARKER__', literal(put_marker))
              .replace('__CONTROL__', literal(event_file.parent / 'controller-events')))
    return source + '\n' + driver


# Worker validates every argument and exact hash of synthetic stdin, never uses
# sockets, and emits one line of the verified-worker JSON-v1 contract.
WORKER = r'''#!/usr/bin/python3
import fcntl, hashlib, json, os, re, signal, sys, time
log = os.environ['FIXTURE_EVENTS']
def event(letter):
    with open(log, 'a', encoding='ascii') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write('%s %d\n' % (letter, os.getpid())); f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
def stopped(*unused):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stopped)
args = sys.argv[1:]
load = os.environ['FIXTURE_SCENARIO'] == 'load-run'
size = 20971520 if load else 3145728
expected = ['-c', '/dev/stdin', '-f', None, '-download-size', str(size),
            '-concurrent', '4' if load else '2', '-timeout', '30s' if load else '12s', '-max-latency', '5s' if load else '2s',
            '-rename=false', '-server-url', 'http://127.0.0.1:1/synthetic-disabled', '-json-v1']
if len(args) != len(expected) or any(v is not None and v != args[i] for i, v in enumerate(expected)):
    event('X'); sys.exit(2)
regex = args[3]
if not (regex.startswith('\\A') and regex.endswith('\\z')):
    event('X'); sys.exit(2)
encoded = regex[2:-2]
if not re.fullmatch(r'(?:[A-Za-z0-9]|\\x\{[0-9a-f]+\})+', encoded):
    event('X'); sys.exit(2)
name = re.sub(r'\\x\{([0-9a-f]+)\}', lambda m: chr(int(m.group(1), 16)), encoded)
a = ['A%02d' % i for i in range(1, 13)]; b = ['B01', 'B02']
digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
selected = os.environ['FIXTURE_SCENARIO'] == 'select-exact'
allowed = ((name in a and digest == os.environ['FIXTURE_HASH_A']) or
           (name in b and digest == os.environ['FIXTURE_HASH_B']))
if selected:
    allowed = ((digest == os.environ['FIXTURE_HASH_A'] and name in ('Shared', 'é')) or
               (digest == os.environ['FIXTURE_HASH_B'] and name == 'e\u0301'))
if not allowed:
    event('X'); sys.exit(2)
if selected:
    with open(log, 'a', encoding='ascii') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write('J %s %s\n' % (digest, name.encode('utf-8').hex())); f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
started = False
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
try:
    event('S'); started = True
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    time.sleep(0.7)
    if os.environ['FIXTURE_SCENARIO'] == 'partial-failure' and name in ('A02', 'B02'):
        sys.exit(23)
    failed = os.environ['FIXTURE_SCENARIO'] == 'allfailed'
    best = ('B01' if os.environ['FIXTURE_SCENARIO'] in ('other', 'only-other', 'scheduled-other-no-auto', 'other-group-denied', 'other-consent-denied')
            else 'A12' if os.environ['FIXTURE_SCENARIO'] == 'result-ranked' else 'A01')
    record = {'version': 1, 'node_name': name, 'node_type': 'http', 'head_attempts': 6,
              'head_successes': 0 if failed else 6, 'latency_ms': 0 if failed else 20,
              'jitter_ms': 0 if failed else 2, 'expected_bytes': size,
              'received_bytes': 0 if failed else size, 'download_complete': not failed,
              'download_bytes_per_second': 0 if failed else (104857600 if name == best else 1048576)}
    print(json.dumps(record, separators=(',', ':')), flush=True)
finally:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if started: event('E')
'''

class MockCase:
    def __init__(self, name, fixture_root, marker):
        self.name, self.root, self.marker = name, fixture_root, marker
        self.controller_log = marker.parent / 'controller-events'
        self.lock = threading.Lock()
        self.current = 'A02'
        self.events, self.errors = [], []
        self.group_gets = 0

    def error(self, text):
        with self.lock:
            self.errors.append(text)


class Controller(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        self.case.error('mock handler exception')


class ControllerHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *unused):
        pass

    def reply(self, code, payload=None):
        data = b'' if payload is None else json.dumps(payload).encode('ascii')
        try:
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        case = self.server.case
        with case.lock:
            case.events.append(('GET', self.path))
            with case.controller_log.open('a', encoding='ascii') as f: f.write('GET\n')
            state_path = case.root / 'mock-state'
            state = state_path.read_text(encoding='utf-8') if state_path.exists() else ''
            if self.path == '/proxies/' + GROUP:
                case.group_gets += 1
                if state == 'preput-stale' and case.group_gets == 1:
                    with (case.root / 'profiles/a.yaml').open('a', encoding='ascii') as f:
                        f.write('\n# synthetic pre-put stale\n')
            current = 'A03' if state == 'now-stale' else case.current
            members = list(SELECT_A + SELECT_B if case.name in SELECTION_CASES else ALL_NODES)
            if state == 'group-stale':
                members.remove('A01')
        group = {'name': GROUP, 'type': 'Direct' if state == 'group-stale' else 'Selector', 'now': current, 'all': members}
        other = {'name': OTHER_GROUP, 'type': 'Selector', 'now': current, 'all': members}
        global_group = {'name': 'GLOBAL', 'type': 'Selector', 'now': current, 'all': members}
        if self.path == '/proxies':
            groups = ({'GLOBAL': global_group} if case.name == 'global-only' else
                      {'GLOBAL': global_group, GROUP: group} if case.name == 'mixed-groups' else
                      {GROUP: group, OTHER_GROUP: other} if case.name == 'multiple-groups' else {GROUP: group})
            self.reply(200, {'proxies': groups})
        elif self.path == '/proxies/' + GROUP:
            self.reply(200, group)
        elif self.path == '/proxies/' + OTHER_GROUP and case.name == 'multiple-groups':
            self.reply(200, other)
        elif self.path == '/proxies/GLOBAL' and case.name == 'global-only':
            self.reply(200, global_group)
        else:
            case.error('unexpected mock GET'); self.reply(404)

    def do_PUT(self):
        case = self.server.case
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size > 256:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            case.error('malformed mock PUT'); self.reply(400); return
        winner = ('e\u0301' if case.name == 'manual-unicode'
                  else 'B01' if case.name in ('other', 'only-other', 'manual-other')
                  else 'A03' if case.name in ('manual-same', 'manual-unchecked', 'manual-revoked', 'manual-unknown', 'multiple-groups', 'mixed-groups')
                  else 'A12' if case.name == 'result-ranked' else 'A01')
        with case.lock:
            case.events.append(('PUT', self.path, body))
            with case.controller_log.open('a', encoding='ascii') as f: f.write('PUT\n')
            if self.path != '/proxies/' + GROUP or body != {'name': winner}:
                case.errors.append('unexpected mock PUT')
                self.reply(400); return
            case.current = winner
            case.marker.write_text('synthetic PUT\n', encoding='ascii')
        if case.name in ('uncertain', 'manual-unknown'):
            self.close_connection = True
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.connection.close()
        elif case.name == 'delayed':
            # Bounded delay allows cancellation while a mock request is in flight.
            threading.Event().wait(1.5); self.reply(204)
        else:
            self.reply(204)


def sandbox_profile(root, allowed_socket, denied_fixture, real_homes):
    q = literal
    lines = ['(version 1)', '(allow default)', '(deny network-outbound)',
             '(deny file-write*)', '(allow file-write* (subpath ' + q(root) + '))',
             '(allow file-write* (literal "/dev/null"))',
             '(deny file-read* (subpath "/Users"))',
             '(deny file-read* (subpath ' + q(denied_fixture) + '))']
    for home in real_homes:
        require(home != Path('/') and not root.is_relative_to(home), 'unsafe real HOME overlap')
        lines.append('(deny file-read* (subpath ' + q(home) + '))')
    lines.append('(allow network-outbound (remote unix-socket (path ' + q(allowed_socket) + ')))')
    return '\n'.join(lines)


def sandbox_run(policy, command, root, env, timeout):
    # A fresh process group lets us reap the app's sandboxed descendants if a
    # timeout occurs. Normal completion still waits for their captured pipes.
    args = ['/usr/bin/sandbox-exec', '-p', policy, *map(str, command)]
    process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(args, process.returncode, out, err)


def verify_sandbox(root, policy, allowed_socket, denied_fixture, real_homes, env):
    # Both denied listeners are private live endpoints: refusal must be EPERM,
    # not ENOENT/ECONNREFUSED. Never try reading actual profile contents.
    denied_socket = root / 'denied.sock'
    with socket.socket(socket.AF_INET) as tcp, socket.socket(socket.AF_UNIX) as unix:
        tcp.bind(('127.0.0.1', 0)); tcp.listen()
        unix.bind(str(denied_socket)); unix.listen()
        code = r'''import errno, os, socket, sys
port, good, bad, allowed, denied, *homes = sys.argv[1:]
for kind, address, expect in [(socket.AF_INET, ('127.0.0.1', int(port)), False),
                              (socket.AF_UNIX, good, True), (socket.AF_UNIX, bad, False)]:
    s = socket.socket(kind); s.settimeout(3)
    try:
        s.connect(address); ok = True
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES): raise
        ok = False
    finally: s.close()
    if ok != expect: raise SystemExit(1)
with open(allowed, 'rb') as f:
    if f.read() != b'synthetic allowed': raise SystemExit(2)
for target in [denied] + homes:
    try:
        fd = os.open(target, os.O_RDONLY); os.close(fd)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES): raise
    else: raise SystemExit(3)
'''
        result = sandbox_run(policy, ['/usr/bin/python3', '-c', code, str(tcp.getsockname()[1]),
                            allowed_socket, denied_socket, root / 'allowed-canary', denied_fixture / 'profile.yaml',
                            *real_homes], root, env, 15)
        require(result.returncode == 0, 'sandbox network/profile canary failed; no scenarios run')
    denied_socket.unlink()


def fixture_yaml(nodes):
    return ('proxies:\n' + ''.join('  - {name: %s, type: http, server: 127.0.0.1, port: 1}\n' % json.dumps(n, ensure_ascii=True)
                                   for n in nodes)).encode('ascii')


def registry(current, unicode_uids=False):
    # JSON string scalars preserve canonical-equivalent but byte-distinct UIDs.
    a, b = (UID_A, UID_B) if unicode_uids else ('a', 'b')
    chosen = a if current == 'a' else b
    return ('current: %s\nitems:\n' % json.dumps(chosen, ensure_ascii=True) +
            '  - {uid: %s, type: local, name: Synthetic-A, file: a.yaml}\n' % json.dumps(a, ensure_ascii=True) +
            '  - {uid: %s, type: remote, name: Synthetic-B, file: b.yaml}\n' % json.dumps(b, ensure_ascii=True))


def check_events(case, worker_events, scenario):
    require(not case.errors, 'mock controller rejected request')
    puts = [i for i, event in enumerate(case.events) if event[0] == 'PUT']
    put_cases = ('same', 'other', 'only-other', 'uncertain', 'delayed', 'partial-failure',
                 'manual-same', 'manual-unchecked', 'manual-other', 'manual-unknown',
                 'result-ranked', 'multiple-groups', 'mixed-groups', 'manual-unicode')
    require(len(puts) == (1 if scenario in put_cases else 0), 'unexpected mock PUT count in ' + scenario)
    confirmed = ('same', 'other', 'only-other', 'partial-failure', 'manual-same',
                 'manual-unchecked', 'manual-other', 'result-ranked', 'multiple-groups', 'mixed-groups', 'manual-unicode')
    if scenario in confirmed:
        i = puts[0]
        require(any(e[:2] == ('GET', '/proxies/' + GROUP) for e in case.events[:i]) and
                any(e[:2] == ('GET', '/proxies/' + GROUP) for e in case.events[i+1:]),
                'mock PUT lacked preceding verification or readback in ' + scenario)
    if scenario == 'preput-stale':
        require(case.group_gets >= 1, 'mock did not reach pre-PUT guard')
    if scenario in ('group-stale', 'now-stale', 'manual-group-stale', 'manual-now-stale'):
        require(sum(e[:2] == ('GET', '/proxies') for e in case.events) >= 2,
                'mock did not reach displayed-group revalidation')
    active, peak, starts, jobs = set(), 0, 0, []
    for line in worker_events.splitlines():
        if line.startswith('J '):
            require(scenario == 'select-exact' and re.fullmatch(r'J [0-9a-f]{64} [0-9a-f]+', line), 'invalid synthetic job log')
            jobs.append(tuple(line.split()[1:]))
            continue
        match = re.fullmatch(r'([SEX]) ([0-9]+)', line)
        require(match is not None, 'invalid synthetic worker log')
        kind, pid = match.groups()
        require(kind != 'X', 'synthetic worker rejected inputs')
        if kind == 'S':
            require(pid not in active, 'duplicate synthetic worker start')
            active.add(pid); starts += 1; peak = max(peak, len(active))
        else:
            require(pid in active, 'unexpected synthetic worker exit')
            active.remove(pid)
    if scenario == 'select-exact':
        ha, hb = (hashlib.sha256(fixture_yaml(n)).hexdigest() for n in (SELECT_A, SELECT_B))
        wanted_jobs = [(ha, 'Shared'.encode().hex()), (ha, 'é'.encode().hex()), (hb, 'e\u0301'.encode().hex())]
        require(sorted(jobs) == sorted(wanted_jobs), 'byte-exact selected job identities differ')
    require(peak <= (2 if scenario == 'load-run' else 10), 'worker concurrency exceeded limit')
    require(not active, 'synthetic worker did not exit in ' + scenario)
    no_workers = ('startup', 'scheduled-stale', 'group-stale', 'now-stale', 'select-preview',
                  'select-stale', 'select-invalid', 'zero-selection', 'manual-same', 'manual-unchecked',
                  'manual-other', 'manual-revoked', 'manual-unknown', 'manual-group-stale', 'manual-now-stale',
                  'multiple-groups', 'mixed-groups', 'global-only', 'manual-unicode', *NO_WORKER_REGRESSIONS)
    if scenario in no_workers:
        require(starts == 0, 'unexpected worker before valid preflight')
    elif scenario in ('stop', 'close-busy', 'during-stale'):
        require(1 <= starts <= 14, 'cancelled worker launch mismatch in ' + scenario)
    else:
        expected_starts = 2 if scenario == 'only-other' else 3 if scenario == 'select-exact' else 16 if scenario == 'old-scores' else 14
        require(starts == expected_starts and peak >= 2, 'worker launch or peak mismatch in ' + scenario)



def main():
    require(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
            'macOS sandbox-exec required; refusing unsandboxed execution')
    with tempfile.TemporaryDirectory(prefix='multi-e2e-', dir='/private/tmp') as folder:
        root = Path(folder).resolve()
        require(root.stat().st_mode & 0o077 == 0, 'temporary directory is not private')
        home, tmp, config = root / 'home', root / 'tmp', root / 'config'
        for path in (home, tmp, config, config / 'profiles', root / 'denied-profile'):
            path.mkdir(mode=0o700)
        (root / 'allowed-canary').write_bytes(b'synthetic allowed')
        (root / 'denied-profile/profile.yaml').write_bytes(b'synthetic denied')
        socket_path, marker, events = root / 'controller.sock', root / 'put-marker', root / 'worker-events'
        require(len(os.fsencode(socket_path)) < 100, 'fixture socket path is too long')
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home), 'CFFIXED_USER_HOME': str(home),
               'TMPDIR': str(tmp), 'CURL_HOME': str(home), 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
               'NO_PROXY': '*', 'no_proxy': '*', 'FIXTURE_EVENTS': str(events),
               'FIXTURE_HASH_A': hashlib.sha256(fixture_yaml(A_NODES)).hexdigest(),
               'FIXTURE_HASH_B': hashlib.sha256(fixture_yaml(B_NODES)).hexdigest()}
        homes = {Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}
        if os.environ.get('HOME'):
            homes.add(Path(os.environ['HOME']).resolve())
        homes = sorted(homes)
        policy = sandbox_profile(root, socket_path, root / 'denied-profile', homes)
        server = Controller(str(socket_path), ControllerHandler)
        server.case = MockCase('canary', config, marker)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            verify_sandbox(root, policy, socket_path, root / 'denied-profile', homes, env)
            print('Sandbox canaries PASS: fixture-only Unix, blocked TCP/Unix and profile reads', flush=True)
            builddir = root / 'build'; builddir.mkdir(mode=0o700)
            for name in SOURCES:
                data = (PROJECT / 'Sources' / name).read_text(encoding='utf-8')
                if name == 'MultiMain.swift':
                    data = instrument(data, config, events, marker, socket_path)
                elif name == 'MultiBatchRunner.swift':
                    # Observe launch decisions even when a cancelled child is
                    # killed before its synthetic worker can emit an S event.
                    anchor = 'private func launchNext(_ job: MultiBatchJob, reserved: Bool = false) -> Bool {'
                    data = replace_once(data, anchor, anchor + '\n        Fixture.runnerLaunches += 1',
                                        'runner launch observation')
                (builddir / name).write_text(data, encoding='utf-8')
            app = root / 'Synthetic.app/Contents'; macos = app / 'MacOS'
            macos.mkdir(parents=True, mode=0o700)
            binary, worker = macos / 'Synthetic', macos / 'clash-speedtest'
            worker.write_text(WORKER, encoding='utf-8'); worker.chmod(0o700)
            with (app / 'Info.plist').open('wb') as f:
                plistlib.dump({'CFBundleExecutable': 'Synthetic', 'CFBundleIdentifier': 'local.synthetic.multi-e2e',
                               'CFBundlePackageType': 'APPL', 'LSUIElement': True}, f)
            command = ['/usr/bin/swiftc', '-D', 'MULTI_UI_TEST', '-module-cache-path', root / 'module-cache',
                       *[builddir / name for name in SOURCES], '-o', binary]
            build = sandbox_run(policy, command, root, env, 180)
            if build.returncode:
                # Never print compiler output: it can contain local paths or source text.
                count = sum(': error: ' in line for line in build.stderr.splitlines())
                print('Swift compile error count: %d' % count, flush=True)
                raise HarnessError('disposable Swift build failed')
            print('Disposable sandboxed build PASS', flush=True)
            failures = []
            for scenario in (tuple(sys.argv[1:]) if len(sys.argv) > 1 else SCENARIOS):
                require(scenario in SCENARIOS, 'unknown scenario')
                nodes_a, nodes_b = (SELECT_A, SELECT_B) if scenario in SELECTION_CASES else (A_NODES, B_NODES)
                data_a, data_b = fixture_yaml(nodes_a), fixture_yaml(nodes_b)
                (config / 'profiles/a.yaml').write_bytes(data_a)
                (config / 'profiles/b.yaml').write_bytes(data_b)
                env['FIXTURE_HASH_A'] = hashlib.sha256(data_a).hexdigest()
                env['FIXTURE_HASH_B'] = hashlib.sha256(data_b).hexdigest()
                (config / 'profiles.yaml').write_text(registry('a', scenario in SELECTION_CASES), encoding='ascii')
                (config / 'registry-b.json').write_text(registry('b', scenario in SELECTION_CASES), encoding='ascii')
                (config / 'clash-verge.yaml').write_text('external-controller-unix: ' + json.dumps(str(socket_path)) + '\n', encoding='ascii')
                for path in (marker, events, root / 'controller-events', config / 'mock-state'):
                    if path.exists(): path.unlink()
                case = MockCase(scenario, config, marker); server.case = case
                env['FIXTURE_SCENARIO'] = scenario
                try:
                    result = sandbox_run(policy, [binary, scenario], root, env, 100)
                    expected = 'E2E %s PASS' % scenario
                    if result.returncode != 0 or expected not in result.stdout.splitlines():
                        codes = [line for line in result.stdout.splitlines()
                                 if re.fullmatch(r'E2E [a-z-]+ FAIL [a-z-]+', line)]
                        diagnostics = [line for line in result.stdout.splitlines()
                                       if re.fullmatch(r'E2E diagnostic rows=[0-9]+ busy=(true|false) applying=(true|false) runner=(true|false) peak=[0-9]+', line)]
                        if diagnostics: print(diagnostics[0], flush=True)
                        if events.exists():
                            lines = events.read_text(encoding='ascii').splitlines()
                            print('E2E events starts=%d ends=%d rejected=%d mock=%d' %
                                  (sum(s.startswith('S ') for s in lines), sum(s.startswith('E ') for s in lines),
                                   sum(s.startswith('X ') for s in lines), len(case.events)), flush=True)
                        raise HarnessError(codes[0] if codes else 'scenario process failed: ' + scenario)
                    worker_events = events.read_text(encoding='ascii') if events.exists() else ''
                    check_events(case, worker_events, scenario)
                    print(expected, flush=True)
                except (HarnessError, subprocess.TimeoutExpired) as exc:
                    message = str(exc) if isinstance(exc, HarnessError) else 'scenario timeout: ' + scenario
                    failures.append(message); print('FAIL ' + message, flush=True)
                    # Do not run another case after an abnormal or timed-out app:
                    # no residual child may contaminate a later fixture.
                    break
            require(not failures, 'one or more synthetic scenarios failed')
            print('Synthetic integration suite PASS' if len(sys.argv) == 1 else 'Selected synthetic scenarios PASS', flush=True)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__':
    try:
        main()
    except HarnessError as exc:
        print('Synthetic integration FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Unexpected exception text/tracebacks can contain local paths; do not emit them.
        print('Synthetic integration FAIL: fixture, tool or execution error', file=sys.stderr)
        sys.exit(1)

