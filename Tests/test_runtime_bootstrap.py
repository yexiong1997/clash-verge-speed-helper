#!/usr/bin/env python3
"""Isolated bootstrap lifecycle tests; every desktop/config entry is replaced."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def replace_method(source, signature, body):
    start = source.index(signature) + len(signature)
    end = source.index('\n    }', start)
    return source[:start] + '\n' + body + source[end:]


FIXTURES = r'''
private var fixtureMode = ""
private var fixtureService: RuntimeBridge?
private var fixtureInjector: ClashRuntimeInjector?
private let fixtureLock = NSLock()
private var fixtureEvents: [String] = []
private var fixtureAttributes: [UInt: [String: Any]] = [:]
private func recordEvent(_ event: String) {
    fixtureLock.lock(); fixtureEvents.append(event); fixtureLock.unlock()
}
private func attachFixture(_ bridge: RuntimeBridge) throws {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:\(bridge.port)/hello")!)
    request.httpMethod = "POST"
    request.setValue("Bearer \(bridge.token)", forHTTPHeaderField: "Authorization")
    request.httpBody = try JSONSerialization.data(withJSONObject: ["client": "fixture", "origin": "tauri://localhost"])
    let done = DispatchSemaphore(value: 0)
    var status = 0
    URLSession.shared.dataTask(with: request) { _, response, _ in
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        done.signal()
    }.resume()
    guard done.wait(timeout: .now() + 3) == .success, status == 200 else {
        throw ClashRuntimeError.notAttached
    }
}
extension ClashRuntimeInjector {
    static func exercise(_ mode: String) throws -> [String] {
        if mode == "proxies-navigation" {
            let app = AXUIElementCreateApplication(123470)
            let window = AXUIElementCreateApplication(123471)
            let mainPage = AXUIElementCreateApplication(123472)
            let homeBtn = AXUIElementCreateApplication(123473)
            let proxiesBtn = AXUIElementCreateApplication(123474)
            let settingsBtn = AXUIElementCreateApplication(123475)
            let proxyCardClose = AXUIElementCreateApplication(123476)
            let inspector = AXUIElementCreateApplication(123477)
            let dock = AXUIElementCreateApplication(123478)
            let close = AXUIElementCreateApplication(123479)
            let consoleGroup = AXUIElementCreateApplication(123480)
            let filterField = AXUIElementCreateApplication(123481)
            let promptArea = AXUIElementCreateApplication(123482)
            fixtureAttributes = [
                CFHash(app): [kAXWindowsAttribute: [window]],
                CFHash(window): [kAXRoleAttribute: kAXWindowRole, kAXChildrenAttribute: [mainPage, inspector]],
                CFHash(mainPage): [
                    kAXRoleAttribute: "AXWebArea",
                    kAXTitleAttribute: "",
                    kAXDescriptionAttribute: "Clash Verge",
                    kAXURLAttribute: URL(string: "tauri://localhost/settings")!,
                    kAXChildrenAttribute: [homeBtn, proxiesBtn, settingsBtn, proxyCardClose]
                ],
                CFHash(homeBtn): [kAXRoleAttribute: kAXButtonRole, kAXTitleAttribute: "首 页"],
                CFHash(proxiesBtn): [kAXRoleAttribute: kAXButtonRole, kAXTitleAttribute: "代 理"],
                CFHash(settingsBtn): [kAXRoleAttribute: kAXButtonRole, kAXTitleAttribute: "设 置"],
                CFHash(proxyCardClose): [kAXRoleAttribute: kAXTextAreaRole, kAXTitleAttribute: "Console prompt"],
                CFHash(inspector): [kAXRoleAttribute: "AXWebArea", kAXTitleAttribute: "Web Inspector", kAXChildrenAttribute: [close, dock, consoleGroup]],
                CFHash(close): [kAXRoleAttribute: kAXButtonRole, kAXDescriptionAttribute: "关闭"],
                CFHash(dock): [kAXRoleAttribute: kAXButtonRole, kAXDescriptionAttribute: "停靠到窗口底部"],
                CFHash(consoleGroup): [kAXRoleAttribute: kAXGroupRole, kAXTitleAttribute: "Console", kAXChildrenAttribute: [filterField, promptArea]],
                CFHash(filterField): [kAXRoleAttribute: kAXTextFieldRole, kAXSubroleAttribute: kAXSearchFieldSubrole, kAXDescriptionAttribute: "Filter Console Logs"],
                CFHash(promptArea): [kAXRoleAttribute: kAXTextAreaRole, kAXDescriptionAttribute: "Console prompt"]
            ]
            let injector = ClashRuntimeInjector()
            let foundProxies = injector.findProxiesNavigationButton(app)
            let foundPrompt = injector.findConsolePrompt(app)
            let foundInspector = injector.findInspector(app)
            return [
                "fromSettings:\(shouldNavigateToProxies(didInject: true, currentPath: "/settings"))",
                "fromProfile:\(shouldNavigateToProxies(didInject: true, currentPath: "/profile"))",
                "alreadyProxies:\(shouldNavigateToProxies(didInject: true, currentPath: "/proxies"))",
                "failedInject:\(shouldNavigateToProxies(didInject: false, currentPath: "/settings"))",
                "proxiesBtn:\(foundProxies.map { CFEqual($0, proxiesBtn) } ?? false)",
                "promptSkipsMainPageAndFilter:\(foundPrompt.map { CFEqual($0.prompt, promptArea) } ?? false)",
                "inspectorSkipsMainPage:\(foundInspector.map { CFEqual($0.area, inspector) } ?? false)"
            ]
        }
        if mode == "inspector-tree" {
            let app = AXUIElementCreateApplication(123460)
            let window = AXUIElementCreateApplication(123461)
            let unrelatedPage = AXUIElementCreateApplication(123462)
            let unrelatedClose = AXUIElementCreateApplication(123463)
            let inspector = AXUIElementCreateApplication(123464)
            let close = AXUIElementCreateApplication(123465)
            let dock = AXUIElementCreateApplication(123466)
            fixtureAttributes = [
                CFHash(app): [kAXWindowsAttribute: [window]],
                CFHash(window): [kAXRoleAttribute: kAXWindowRole, kAXChildrenAttribute: [unrelatedPage, inspector]],
                CFHash(unrelatedPage): [kAXRoleAttribute: "AXWebArea", kAXChildrenAttribute: [unrelatedClose]],
                CFHash(unrelatedClose): [kAXRoleAttribute: kAXButtonRole, kAXTitleAttribute: "Close"],
                CFHash(inspector): [kAXRoleAttribute: "AXWebArea", kAXChildrenAttribute: [close, dock]],
                CFHash(close): [kAXRoleAttribute: kAXButtonRole, kAXHelpAttribute: "Close"],
                CFHash(dock): [kAXRoleAttribute: kAXButtonRole, kAXHelpAttribute: "Dock to left of window"]
            ]
            let injector = ClashRuntimeInjector()
            let found = injector.findInspector(app)
            return ["inspector:\(found.map { CFEqual($0.area, inspector) } ?? false)",
                    "close:\(found.map { CFEqual($0.closeButton, close) } ?? false)"]
        }
        if mode == "ownership" {
            let owned = AXUIElementCreateApplication(123456)
            let other = AXUIElementCreateApplication(123457)
            return [
                "missing:\(shouldCloseInspector(openedByBootstrap: true, existedBefore: false, owned: nil, current: owned))",
                "existing:\(shouldCloseInspector(openedByBootstrap: true, existedBefore: true, owned: owned, current: owned))",
                "same:\(shouldCloseInspector(openedByBootstrap: true, existedBefore: false, owned: owned, current: owned))",
                "replaced:\(shouldCloseInspector(openedByBootstrap: true, existedBefore: false, owned: owned, current: other))",
                "closed:\(shouldCloseInspector(openedByBootstrap: true, existedBefore: false, owned: owned, current: nil))"
            ]
        }
        fixtureMode = mode
        fixtureService = mode == "remote" || mode == "warm" ? try RuntimeBridge(sessionID: "fixture-session") : nil
        defer { fixtureService?.stop(); fixtureService = nil }
        if mode == "warm" { try attachFixture(fixtureService!) }
        let injector = ClashRuntimeInjector()
        fixtureInjector = injector
        defer { injector.stop(); fixtureInjector = nil }
        let done = DispatchSemaphore(value: 0)
        injector.start { result in
            switch result {
            case .success: recordEvent("callback:success")
            case .failure(let error):
                if case ClashRuntimeError.cancelled = error { recordEvent("callback:cancelled") }
                else { recordEvent("callback:error") }
            }
            done.signal()
        }
        guard done.wait(timeout: .now() + 10) == .success else { throw ClashRuntimeError.switchTimeout }
        injector.work.sync {}
        fixtureLock.lock(); defer { fixtureLock.unlock() }
        return fixtureEvents
    }
}
'''

HARNESS = r'''
import Foundation
@main struct BootstrapHarness {
    static func main() throws {
        let events = try ClashRuntimeInjector.exercise(CommandLine.arguments[1])
        let data = try JSONSerialization.data(withJSONObject: events)
        print(String(decoding: data, as: UTF8.self))
    }
}
'''


class RuntimeBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='clash-bootstrap-test-', dir='/private/tmp')
        root = Path(cls.tmp.name)
        source = (PROJECT / 'Sources/ClashRuntimeInjector.swift').read_text()
        replacements = {
            'private func verifyOfficialBundle() throws {': '        recordEvent("verify")',
            'private func ensureOfficialAppRunning() throws {': '''
        recordEvent("launch")
        if fixtureMode == "launchfailure" { throw ClashRuntimeError.officialWindowUnavailable }''',
            'private func runningOfficialApp() -> NSRunningApplication? {': '        return nil',
            'private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {': '''
        return fixtureAttributes[CFHash(element)]?[name] as AnyObject?''',
            'private func readSingletonRecord() throws -> SingletonRecord {': '        return SingletonRecord(port: 12345, token: "fixture")',
            'private func readAttachedExternalBridge() -> ExternalRuntimeBridge? {': '''
        guard fixtureMode == "warm", let service = fixtureService else { return nil }
        return ExternalRuntimeBridge(port: service.port, token: service.token, sessionID: "fixture-session")''',
            'private func readAvailableBroker() -> ExternalRuntimeBridge? {': '''
        guard let service = fixtureService else { return nil }
        return ExternalRuntimeBridge(port: service.port, token: service.token, sessionID: "fixture-session")''',
            'private func showOfficialWindow(_ record: SingletonRecord) throws {': '        recordEvent("window")',
            'private func captureBootstrapUI(generation: UInt64) -> BootstrapUIContext {': '''
        recordEvent("capture")
        return BootstrapUIContext(generation: generation, previousFrontApp: nil)''',
            'private func restoreBootstrapUI(_ ui: BootstrapUIContext) {': '''
        precondition(executingStartGeneration == ui.generation)
        precondition(ui.touchedUI)
        recordEvent("cleanup")''',
            'private func injectLoader(bridge: RuntimeBridge, ui: BootstrapUIContext) throws {': '''
        fixtureService = bridge
        try injectLoader(loader: "fixture", ui: ui)''',
            'private func injectLoader(loader: String, ui: BootstrapUIContext) throws {': '''
        recordEvent("inject")
        if fixtureMode == "failure" { throw ClashRuntimeError.consoleInputFailed }
        if fixtureMode == "cancel" { fixtureInjector!.stop(); return }
        try attachFixture(fixtureService!)''',
            'private static func isScreenLocked() -> Bool {': '        return false',
            'private func promptAccessibility(openSettings: Bool, alwaysOpenSettings: Bool = false) {': '        fatalError("unexpected permission UI")',
        }
        for signature, body in replacements.items():
            source = replace_method(source, signature, body)
        source = source.replace('AXIsProcessTrusted()', 'true')
        signature = 'private func captureOwnedInspector(_ ui: BootstrapUIContext) {'
        assert source.count(signature) == 1
        source = source.replace(signature, signature + '\n        recordEvent("ownership")')
        copied = root / 'ClashRuntimeInjector.swift'
        copied.write_text(source + FIXTURES)
        harness = root / 'Harness.swift'
        harness.write_text(HARNESS)
        cls.binary = root / 'BootstrapHarness'
        result = subprocess.run([
            '/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library',
            '-target', f'{os.uname().machine}-apple-macos12.0',
            '-module-cache-path', str(root / 'cache'), str(copied), str(harness),
            '-o', str(cls.binary), '-framework', 'AppKit', '-framework', 'ApplicationServices',
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), mode], capture_output=True, text=True,
                                env={**os.environ, 'CLASH_RUNTIME_BROKER_DISABLED': '1'}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_owned_cleanup_precedes_success_callback(self):
        self.assertEqual(self.run_case('owned'),
                         ['verify', 'capture', 'launch', 'window', 'inject', 'ownership', 'cleanup', 'callback:success'])

    def test_broker_cleanup_precedes_success_callback(self):
        self.assertEqual(self.run_case('remote'),
                         ['verify', 'capture', 'launch', 'window', 'inject', 'ownership', 'cleanup', 'callback:success'])

    def test_injection_failure_cleans_up_once_and_preserves_failure(self):
        self.assertEqual(self.run_case('failure'),
                         ['verify', 'capture', 'launch', 'window', 'inject', 'cleanup', 'callback:error'])

    def test_cancelled_generation_still_cleans_up(self):
        self.assertEqual(self.run_case('cancel'),
                         ['verify', 'capture', 'launch', 'window', 'inject', 'callback:cancelled', 'cleanup'])

    def test_attached_broker_never_captures_or_changes_desktop(self):
        self.assertEqual(self.run_case('warm'), ['verify', 'callback:success'])

    def test_launch_failure_also_cleans_up(self):
        self.assertEqual(self.run_case('launchfailure'),
                         ['verify', 'capture', 'launch', 'cleanup', 'callback:error'])

    def test_only_identified_owned_inspector_can_close(self):
        self.assertEqual(self.run_case('ownership'),
                         ['missing:false', 'existing:false', 'same:true', 'replaced:false', 'closed:false'])

    def test_inspector_buttons_with_only_help_labels_are_identified(self):
        self.assertEqual(self.run_case('inspector-tree'), ['inspector:true', 'close:true'])

    def test_post_injection_returns_to_proxies_and_skips_main_dom_during_inspector_scan(self):
        self.assertEqual(
            self.run_case('proxies-navigation'),
            [
                'fromSettings:true',
                'fromProfile:true',
                'alreadyProxies:false',
                'failedInject:false',
                'proxiesBtn:true',
                'promptSkipsMainPageAndFilter:true',
                'inspectorSkipsMainPage:true',
            ],
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
