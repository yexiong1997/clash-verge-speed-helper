#!/usr/bin/env python3
import concurrent.futures
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import uuid


PROJECT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT / 'Scripts/install-runtime-broker.py'
SPEC = importlib.util.spec_from_file_location('runtime_install', INSTALLER)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class RuntimeBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = Path(tempfile.mkdtemp(prefix='clash-broker-tests-', dir='/private/tmp'))
        cls.binary = cls.build / 'ClashRuntimeBroker'
        result = subprocess.run([
            '/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library',
            '-target', f'{os.uname().machine}-apple-macos12.0', '-module-cache-path', str(cls.build / 'cache'),
            str(PROJECT / 'Sources/ClashRuntimeInjector.swift'), str(PROJECT / 'Sources/RuntimeBrokerMain.swift'),
            '-o', str(cls.binary),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr)
        copied_source = (PROJECT / 'Sources/ClashRuntimeInjector.swift').read_text()
        copied_source = copied_source.replace('/private/tmp/clash-verge-runtime-state.json', str(cls.build / 'absent-legacy-state'))
        for signature in (
            'private func verifyOfficialBundle() throws {',
            'private func ensureOfficialAppRunning() throws {',
            'private func readSingletonRecord() throws -> SingletonRecord {',
            'private func injectLoader(loader: String, ui: BootstrapUIContext) throws {',
            "private func captureBootstrapUI(generation: UInt64) -> BootstrapUIContext {",
            "private func restoreBootstrapUI(_ ui: BootstrapUIContext) {",
            'private func promptAccessibility(openSettings: Bool, alwaysOpenSettings: Bool = false) {',
        ):
            assert copied_source.count(signature) == 1
            copied_source = copied_source.replace(signature, signature + '\n fatalError("unexpected bootstrap in client fixture")')
        (cls.build / 'Injector.swift').write_text(copied_source)
        (cls.build / 'Client.swift').write_text(r'''
import Foundation
@main struct ClientFixture {
    static func main() {
        let finished = DispatchSemaphore(value: 0)
        var success = false
        let injector = ClashRuntimeInjector.shared
        injector.switchProfile(uid: "fixture-target") { result in
            if case .success(let value) = result, value.current == "fixture-target" { success = true }
            finished.signal()
        }
        guard finished.wait(timeout: .now() + 8) == .success else { exit(2) }
        injector.stop()
        print(success ? "SWITCH_OK" : "SWITCH_FAILED")
        exit(success ? 0 : 1)
    }
}
''')
        cls.client = cls.build / 'ClientFixture'
        result = subprocess.run([
            '/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library',
            '-module-cache-path', str(cls.build / 'cache'), str(cls.build / 'Injector.swift'),
            str(cls.build / 'Client.swift'), '-o', str(cls.client),
        ], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.build)

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='case-', dir=self.build))
        self.root = self.directory / 'runtime'
        self.root.mkdir(mode=0o700)
        self.environment = {**os.environ, 'CLASH_RUNTIME_BROKER_ROOT': str(self.root)}
        self.environment.pop('CLASH_RUNTIME_BROKER_DISABLED', None)
        self.process = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)

    def tearDown(self):
        subprocess.run([str(self.binary), '--shutdown'], env=self.environment, capture_output=True, timeout=6)
        if self.process:
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=4)
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()
        self.pool.shutdown(wait=True)
        shutil.rmtree(self.directory)

    def start(self):
        self.process = subprocess.Popen([str(self.binary)], env=self.environment,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while not (self.root / 'state.json').exists():
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.descriptor = json.loads((self.root / 'state.json').read_text())

    def request(self, method, route, body=None, authenticated=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.descriptor['port'], timeout=32)
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.descriptor['token']
        try:
            connection.request(method, route, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    def hello(self):
        self.assertEqual(self.request('POST', '/hello', {'client': 'fixture-page', 'origin': 'tauri://localhost'})[0], 200)

    def pickup(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, body = self.request('GET', '/poll?client=fixture-page')
            if status == 200:
                return body['command']
            self.assertEqual(status, 204)
            time.sleep(0.02)
        self.fail('command not picked up')

    def submit(self, identifier=None):
        identifier = identifier or str(uuid.uuid4()).upper()
        future = self.pool.submit(self.request, 'POST', '/command', {'op': 'switch', 'target': 'fixture-target', 'id': identifier})
        return identifier, future

    def setup_app(self):
        app = self.directory / '助手 Fixture.app'
        executable = app / 'Contents/MacOS/ClashRuntimeBroker'
        executable.parent.mkdir(parents=True)
        shutil.copy2(self.binary, executable)
        self.app = app
        self.config = self.directory / 'verge.yaml'

    def install(self, *arguments, success=True):
        result = subprocess.run([
            '/usr/bin/python3', '-I', str(INSTALLER), '--root', str(self.root),
            '--config', str(self.config), '--app', str(self.app), *arguments,
        ], capture_output=True, text=True, env=self.environment, timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_headless_single_instance_and_private_state(self):
        self.start()
        status, body = self.request('GET', '/status')
        self.assertEqual(status, 200)
        self.assertFalse(body['attached'])
        self.assertEqual(body['sessionID'], self.descriptor['sessionID'])
        self.assertEqual((self.root / 'state.json').stat().st_mode & 0o777, 0o600)
        duplicate = subprocess.run([str(self.binary)], env=self.environment, capture_output=True, timeout=3)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads((self.root / 'state.json').read_text()), self.descriptor)
        self.assertEqual(self.request('POST', '/shutdown', authenticated=False)[0], 401)
        self.assertIsNone(self.process.poll())

    def test_reconnected_client_roundtrip_stale_result_and_duplicate(self):
        self.start()
        self.hello()
        identifier, future = self.submit()
        command = self.pickup()
        self.assertEqual(command['id'], identifier)
        self.assertEqual(command['uid'], 'fixture-target')
        self.assertEqual(self.request('POST', '/result', {'id': identifier, 'client': 'wrong', 'ok': True, 'value': {}})[0], 409)
        self.assertFalse(future.done())
        value = {'previous': 'fixture-old', 'current': 'fixture-target', 'name': 'Fixture'}
        self.request('POST', '/result', {'id': identifier, 'client': 'fixture-page', 'ok': True, 'value': value})
        self.assertEqual(future.result(timeout=3), (200, value))
        self.assertEqual(self.request('GET', '/status')[1]['sessionID'], self.descriptor['sessionID'])
        status, result = self.request('POST', '/command', {'op': 'switch', 'target': 'fixture-target', 'id': identifier})
        self.assertEqual((status, result['errorCode']), (409, 'duplicate'))
        self.assertEqual(self.request('GET', '/poll?client=fixture-page')[0], 204)
        self.assertEqual(self.request('POST', '/command', {'op': 'eval', 'id': str(uuid.uuid4()), 'target': 'x'})[0], 400)

    def test_cancel_after_pickup_reports_unknown_without_replay(self):
        self.start()
        self.hello()
        identifier, future = self.submit()
        self.pickup()
        self.assertEqual(self.request('POST', '/cancel', {'id': identifier}), (200, {'cancelledBeforePickup': False}))
        self.assertEqual(future.result(timeout=3)[1]['errorCode'], 'outcome_unknown')
        self.assertEqual(self.request('GET', '/poll?client=fixture-page')[0], 204)

    def test_swift_client_exit_and_relaunch_reuses_broker(self):
        self.start()
        self.hello()
        for _ in range(2):
            process = subprocess.Popen([str(self.client)], env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                command = self.pickup()
                value = {'previous': 'fixture-old', 'current': 'fixture-target', 'name': 'Fixture'}
                self.request('POST', '/result', {'id': command['id'], 'client': 'fixture-page', 'ok': True, 'value': value})
                stdout, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout.strip(), b'SWITCH_OK')
                self.assertIsNone(self.process.poll())
                self.assertEqual(self.request('GET', '/status')[1]['sessionID'], self.descriptor['sessionID'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=4)

    def test_disconnected_page_does_not_execute_command(self):
        self.start()
        _, future = self.submit()
        self.assertEqual(future.result(timeout=3)[1]['errorCode'], 'not_attached')

    def test_accepted_shutdown_refuses_new_commands(self):
        self.start()
        self.hello()
        self.assertEqual(self.request('POST', '/shutdown')[0], 200)
        try:
            status, body = self.request('POST', '/command', {'op': 'switch', 'target': 'fixture-target', 'id': str(uuid.uuid4())})
            self.assertNotEqual(status, 200)
            self.assertEqual(body['errorCode'], 'busy')
        except (ConnectionRefusedError, ConnectionResetError, http.client.RemoteDisconnected):
            pass

    def test_early_cancel_prevents_later_submission(self):
        self.start()
        self.hello()
        identifier = str(uuid.uuid4()).upper()
        self.assertEqual(self.request('POST', '/cancel', {'id': identifier}), (200, {'cancelledBeforePickup': True}))
        _, future = self.submit(identifier)
        self.assertEqual(future.result(timeout=3)[1]['errorCode'], 'duplicate')
        self.assertEqual(self.request('GET', '/poll?client=fixture-page')[0], 204)

    def test_unrelated_shutdown_config_change_is_preserved(self):
        self.setup_app()
        before = b'keep: before\nstartup_script: ""\n'
        changed = b'keep: changed-during-quit\nstartup_script: ""\n'
        self.config.write_bytes(changed)
        desired = INSTALL.patch_field(before, '/fixture/start.sh')
        INSTALL.activate_config(self.config, before, desired, self.binary, self.root, False)
        self.assertIn(b'keep: changed-during-quit\n', self.config.read_bytes())
        self.assertEqual(INSTALL.read_field(self.config.read_bytes())['value'], '/fixture/start.sh')

    def test_install_repeat_wrapper_and_uninstall_preserve_original_script(self):
        self.setup_app()
        original = self.directory / '原脚本 有空格.sh'
        marker = self.directory / 'original-cwd.txt'
        original.write_text('pwd -P > original-cwd.txt\nexit 7\n')
        before = '# preserved\nkeep: original\nstartup_script: ' + json.dumps(str(original), ensure_ascii=False) + '\n'
        self.config.write_text(before)
        first = self.install()
        self.assertTrue(first['previousScriptPreserved'])
        self.assertFalse(first['attached'])
        second = self.install()
        self.assertEqual(first['pid'], second['pid'])
        receipt = json.loads((self.root / 'installation.json').read_text())
        self.assertEqual(receipt['previousStartupScript'], str(original))
        self.assertTrue(self.config.read_text().startswith('# preserved\nkeep: original\n'))
        started = time.monotonic()
        run = subprocess.run(['/bin/bash', str(self.root / 'start-clash-bridge.sh')],
                             env=self.environment, capture_output=True, timeout=3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(run.returncode, 7)
        self.assertEqual(marker.read_text().strip(), str(self.directory))
        self.assertTrue(self.install('--uninstall')['previousSettingRestored'])
        self.assertEqual(self.config.read_text(), before)
        self.assertFalse((self.root / 'ClashRuntimeBroker').exists())

    def test_missing_field_is_restored_and_user_change_is_kept(self):
        self.setup_app()
        before = 'keep: unchanged\n'
        self.config.write_text(before)
        self.install()
        self.install('--uninstall')
        self.assertEqual(self.config.read_text(), before)
        self.install()
        changed = 'keep: unchanged\nstartup_script: /user/new-choice.sh\n'
        self.config.write_text(changed)
        self.install(success=False)
        self.assertEqual(self.config.read_text(), changed)
        self.assertFalse(self.install('--uninstall')['previousSettingRestored'])
        self.assertEqual(self.config.read_text(), changed)

    def test_yaml_and_symlink_inputs_fail_before_configuration_change(self):
        self.setup_app()
        duplicate = 'keep: preserved\nstartup_script: first.sh\nstartup_script: second.sh\n'
        self.config.write_text(duplicate)
        self.install(success=False)
        self.assertEqual(self.config.read_text(), duplicate)
        self.assertFalse((self.root / 'installation.json').exists())
        self.config.unlink()
        target = self.directory / 'untouched.yaml'
        target.write_text('keep: preserved\n')
        self.config.symlink_to(target)
        self.install(success=False)
        self.assertEqual(target.read_text(), 'keep: preserved\n')

    def test_yaml_scalar_forms_and_unrelated_bytes(self):
        for field in ('startup_script: null\n', 'startup_script: ""\n', "startup_script: '/a path/example.sh'\n",
                      'startup_script: |-\n  /a path/example.sh\n'):
            with self.subTest(field=field):
                before = ('# title\n' + field + 'other: [1, 2, 3] # preserve\n').encode()
                after = INSTALL.patch_field(before, '/new location/start.sh')
                self.assertEqual(INSTALL.read_field(after)['value'], '/new location/start.sh')
                self.assertTrue(after.endswith(b'other: [1, 2, 3] # preserve\n'))

    def test_unreachable_locked_broker_preserves_installation(self):
        self.setup_app()
        self.config.write_text('keep: unchanged\n')
        self.install()
        state = (self.root / 'state.json').read_bytes()
        config = self.config.read_bytes()
        try:
            (self.root / 'state.json').write_text('{}')
            self.install('--uninstall', success=False)
            self.assertEqual(self.config.read_bytes(), config)
            self.assertTrue((self.root / 'ClashRuntimeBroker').is_file())
        finally:
            (self.root / 'state.json').write_bytes(state)

    def test_interrupted_upgrade_does_not_override_new_choice(self):
        self.setup_app()
        self.config.write_text('keep: unchanged\n')
        self.install()
        receipt_path = self.root / 'installation.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['phase'] = 'prepared'
        receipt_path.write_text(json.dumps(receipt))
        changed = b'keep: unchanged\nstartup_script: /new/user-choice.sh\n'
        self.config.write_bytes(changed)
        self.install(success=False)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_failed_quit_restores_originally_running_application(self):
        self.setup_app()
        before = b'keep: unchanged\n'
        self.config.write_bytes(before)
        actions = []

        def controlled_command(binary, arguments, root, check=True):
            actions.append(arguments[0])
            if arguments == ['--clash-running']:
                return subprocess.CompletedProcess([], 0, '{"running":true}', '')
            if arguments == ['--quit-clash']:
                self.assertTrue((self.root / 'clash-restart.json').exists())
                raise RuntimeError('synthetic interrupted quit')
            return subprocess.CompletedProcess([], 0, '', '')

        with mock.patch.object(INSTALL, 'DEFAULT_CONFIG', self.config), mock.patch.object(INSTALL, 'command', controlled_command):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                INSTALL.activate_config(self.config, before, INSTALL.patch_field(before, '/fixture/start.sh'), self.binary, self.root, True)
        self.assertEqual(actions, ['--clash-running', '--quit-clash', '--launch-clash'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.root / 'clash-restart.json').exists())

    def test_pending_restart_record_is_recovered(self):
        self.setup_app()
        recovery = {'schemaVersion': 1, 'config': str(self.config.resolve()), 'wasRunning': True}
        (self.root / 'clash-restart.json').write_text(json.dumps(recovery))
        with mock.patch.object(INSTALL, 'DEFAULT_CONFIG', self.config), mock.patch.object(INSTALL, 'command') as command:
            INSTALL.recover_application(self.config, self.binary, self.root)
            command.assert_called_once_with(self.binary, ['--launch-clash'], self.root)
        self.assertFalse((self.root / 'clash-restart.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
