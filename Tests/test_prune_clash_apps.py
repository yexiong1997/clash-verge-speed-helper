#!/usr/bin/env python3
"""Deletion and application-registration regression tests on disposable fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT / 'Scripts/prune-clash-apps.sh'
spec = importlib.util.spec_from_file_location('pruner', PROJECT / 'Scripts/prune-clash-apps.py')
pruner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pruner)


class PruneClashAppsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clash-prune-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / 'Applications'
        self.install.mkdir()

    def app(self, name, identifier='local.clashverge.multispeedhelper', root=None):
        app = (root or self.root) / name
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': identifier,
            'CFBundleExecutable': 'fixture',
            'CFBundlePackageType': 'APPL',
        }))
        return app

    def cli(self, *args):
        env = os.environ.copy()
        env['CLASH_LEGACY_CLEANUP_ROOTS'] = str(self.root)
        env['CLASH_MULTI_INSTALL_ROOT'] = str(self.install)
        return subprocess.run(['/bin/bash', str(SCRIPT), *args], env=env,
                              text=True, capture_output=True, timeout=20)

    def test_dry_run_never_removes_recognized_temp_or_suffix_bundles(self):
        apps = [self.app('Clash Multi.app'),
                self.app('Clash Verge Direct.app.fallback', pruner.DIRECT_ID)]
        result = self.cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        for app in apps:
            self.assertTrue(app.exists())
            self.assertIn(f'would remove: {app}', result.stdout)

    def test_removes_duplicates_and_preserves_installed_and_staged_apps(self):
        old = self.app('Clash Multi.app')
        direct = self.app('Clash Verge Direct.app', pruner.DIRECT_ID)
        current = self.app(pruner.APP_NAME, root=self.install)
        staged = self.app('staged.app')
        result = self.cli('--keep', str(staged))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertFalse(direct.exists())
        self.assertTrue(current.exists())
        self.assertTrue(staged.exists())
        repeated = self.cli('--keep', str(staged))
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn('0 bundle(s)', repeated.stdout)

    def test_names_alone_cannot_delete_an_unrelated_bundle(self):
        apps = [self.app('Clash Verge Direct.app', 'other.app'),
                self.app('Clash 节点测速助手.app', 'other.app'),
                self.app('Clash Verge Direct.appEvil.app', pruner.DIRECT_ID)]
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(app.exists() for app in apps))

    def test_symlinked_app_and_contents_never_delete_external_data(self):
        outside = self.root / 'outside'
        outside.mkdir()
        app = self.app('outside.app', root=outside)
        scan = self.root / 'scan'
        scan.mkdir()
        (scan / 'Clash Multi.app').symlink_to(app, target_is_directory=True)
        alias = scan / 'Clash Verge Direct.app'
        alias.mkdir()
        (alias / 'Contents').symlink_to(app / 'Contents', target_is_directory=True)
        with mock.patch.object(pruner, 'refresh_registration'):
            count = pruner.cleanup([scan], set(), False, custom=True)
        self.assertEqual(count, 0)
        self.assertTrue((app / 'Contents/Info.plist').exists())

    def test_symlinked_root_is_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.install, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'physical directory'):
            pruner.cleanup([alias], set(), False, custom=True)

    def test_official_signature_is_preserved_in_generated_locations(self):
        official = self.app('Clash Verge.app', pruner.STOCK_ID,
                            root=self.root / 'target/release/bundle/macos')
        renamed = self.app('Clash Verge.app.patched.1234', pruner.STOCK_ID)
        with mock.patch.object(pruner, 'is_adhoc', return_value=False), \
             mock.patch.object(pruner, 'refresh_registration'):
            self.assertEqual(pruner.cleanup([self.root], set(), False), 0)
        self.assertTrue(official.exists())
        self.assertTrue(renamed.exists())

    def test_only_explicit_adhoc_stock_id_builds_are_removed(self):
        cargo = self.app('Clash Verge.app', pruner.STOCK_ID,
                         root=self.root / 'target/release/bundle/macos')
        patched = self.app('Clash Verge Patched.app.final-test', pruner.STOCK_ID)
        arbitrary = self.app('Clash Verge.app', pruner.STOCK_ID)
        with mock.patch.object(pruner, 'is_adhoc', return_value=True), \
             mock.patch.object(pruner, 'refresh_registration'):
            self.assertEqual(pruner.cleanup([self.root], set(), False), 2)
        self.assertFalse(cargo.exists())
        self.assertFalse(patched.exists())
        self.assertTrue(arbitrary.exists())

    def test_running_candidate_prevents_all_deletions(self):
        first = self.app('A.app')
        running = self.app('Clash Multi.app')
        output = subprocess.CompletedProcess([], 0, stdout=str(running) + '/Contents/MacOS/fixture --arg\n')
        with mock.patch.object(pruner.subprocess, 'run', return_value=output), \
             mock.patch.object(pruner, 'refresh_registration') as refresh:
            with self.assertRaisesRegex(RuntimeError, 'running app'):
                pruner.cleanup([self.root], set(), False)
            refresh.assert_not_called()
        self.assertTrue(first.exists())
        self.assertTrue(running.exists())

    def incomplete_app(self):
        temp = tempfile.TemporaryDirectory(prefix='clash-multi.', dir='/private/tmp')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = root / pruner.APP_NAME
        (app / 'Contents/MacOS').mkdir(parents=True)
        work = app / '.build-multi-work'
        (work / 'Sources').mkdir(parents=True)
        (work / 'worker-project/Scripts').mkdir(parents=True)
        (work / 'Sources/MultiMain.swift').write_text('// disposable build marker\n')
        (work / 'worker-project/Scripts/build-verified-worker.sh').write_text('# disposable build marker\n')
        return root, app

    def test_dot_staging_prefix_and_incomplete_build_are_removed(self):
        root, app = self.incomplete_app()
        self.assertTrue(root.name.startswith(pruner.TMP_PREFIXES))
        self.assertIsNone(pruner.bundle_id(app))
        with mock.patch.object(pruner, 'refresh_registration'):
            self.assertEqual(pruner.cleanup([root], set(), False, custom=True), 1)
        self.assertFalse(app.exists())

    def test_missing_plist_without_exact_owned_build_markers_is_preserved(self):
        root, app = self.incomplete_app()
        (app / '.build-multi-work/Sources/MultiMain.swift').unlink()
        with mock.patch.object(pruner, 'refresh_registration'):
            self.assertEqual(pruner.cleanup([root], set(), False, custom=True), 0)
        self.assertTrue(app.exists())

    def test_running_compiler_prevents_incomplete_build_removal(self):
        root, app = self.incomplete_app()
        command = '/compiler/swiftc ' + str(app / '.build-multi-work/Sources/MultiMain.swift') + '\n'
        result = subprocess.CompletedProcess([], 0, stdout=command)
        with mock.patch.object(pruner.subprocess, 'run', return_value=result), \
             mock.patch.object(pruner, 'refresh_registration') as refresh:
            with self.assertRaisesRegex(RuntimeError, 'running app or build'):
                pruner.cleanup([root], set(), False, custom=True)
            refresh.assert_not_called()
        self.assertTrue(app.exists())

    def test_only_project_ghosts_are_unregistered_and_staged_app_is_not_registered(self):
        ghost = self.root / 'missing/Clash Multi.app'
        unrelated = self.root / 'missing/Unrelated.app'
        current = self.app(pruner.APP_NAME, root=self.install)
        staged = self.app('Staged.app')
        register = self.root / 'lsregister'
        register.touch()
        dump = (f'identifier: {next(iter(pruner.HELPER_IDS))}\npath: {ghost} (0x123)\n'
                + '-' * 40 + '\n'
                + f'identifier: other.app\npath: {unrelated} (0x124)\n')
        with mock.patch.object(pruner, 'LSREGISTER', register), \
             mock.patch.object(pruner.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=dump)
            pruner.refresh_registration([], [self.root], {current, staged}, False, False, {current})
        mutations = [call.args[0] for call in run.call_args_list if call.args[0][1] != '-dump']
        self.assertEqual(mutations, [[str(register), '-u', str(ghost)],
                                     [str(register), '-f', str(current)]])

    def test_custom_scope_and_dry_run_cannot_mutate_other_registrations(self):
        register = self.root / 'lsregister'
        register.touch()
        dump = ('identifier: local.clashverge.multispeedhelper\n'
                'path: /private/tmp/outside/Clash Multi.app (0x123)\n')
        with mock.patch.object(pruner, 'LSREGISTER', register), \
             mock.patch.object(pruner.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=dump)
            pruner.refresh_registration([], [self.root], set(), True, False)
            pruner.refresh_registration([self.root / 'gone.app'], [self.root], set(), False, True)
        self.assertTrue(all(call.args[0][1] == '-dump' for call in run.call_args_list))

    def installer_fixture(self):
        scripts = self.root / 'project/Scripts'
        scripts.mkdir(parents=True)
        for name in ('install-multi.sh', 'prune-clash-apps.sh', 'prune-clash-apps.py'):
            shutil.copy2(PROJECT / 'Scripts' / name, scripts / name)
        # Exercise the real installer with a tiny signed fixture builder. No
        # speed-test worker, network call or generated executable is run.
        builder = scripts / 'build-multi.sh'
        builder.write_text('''#!/bin/bash
set -euo pipefail
[[ "$1" == --output ]]
app_path="$2"
printf '%s\\n' "$app_path" > "$BUILD_STAGE_RECEIPT"
/bin/mkdir -p "$app_path/Contents/MacOS"
/bin/cp /usr/bin/true "$app_path/Contents/MacOS/fixture"
/usr/bin/python3 -I - "$app_path" <<'PY'
from pathlib import Path
import plistlib, sys
info = {'CFBundleIdentifier': 'local.clashverge.multispeedhelper',
        'CFBundleExecutable': 'fixture', 'CFBundlePackageType': 'APPL'}
(Path(sys.argv[1]) / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
PY
/usr/bin/codesign --force --sign - --timestamp=none "$app_path/Contents/MacOS/fixture"
/usr/bin/codesign --force --sign - --timestamp=none "$app_path"
''', encoding='utf-8')
        builder.chmod(0o755)
        receipt = self.root / 'stage-path.txt'
        registrations = self.root / 'registrations.jsonl'
        wrapper = self.root / 'lsregister-trace'
        wrapper.write_text('''#!/usr/bin/python3
import json, os, pathlib, plistlib, subprocess, sys
args = sys.argv[1:]
if args == ['-dump']:
    raise SystemExit(subprocess.run([REAL_LSREGISTER, *args]).returncode)
path = pathlib.Path(args[-1])
info = path / 'Contents/Info.plist'
generation = None
if info.is_file():
    generation = plistlib.loads(info.read_bytes()).get('FixtureGeneration')
with open(os.environ['REGISTRATION_RECEIPT'], 'a') as stream:
    stream.write(json.dumps({'args': args, 'exists': path.exists(),
                             'generation': generation}) + '\\n')
if args[0] == '-u' and str(path) == os.environ.get('FAIL_UNREGISTER_PATH'):
    raise SystemExit(19)
raise SystemExit(subprocess.run([REAL_LSREGISTER, *args]).returncode)
'''.replace('REAL_LSREGISTER', repr(str(pruner.LSREGISTER))), encoding='utf-8')
        wrapper.chmod(0o755)
        installer = scripts / 'install-multi.sh'
        installer.write_text(installer.read_text().replace(str(pruner.LSREGISTER), str(wrapper)))
        env = os.environ.copy()
        env.update(CLASH_MULTI_INSTALL_ROOT=str(self.install),
                   CLASH_LEGACY_CLEANUP_ROOTS=str(self.root),
                   BUILD_STAGE_RECEIPT=str(receipt),
                   REGISTRATION_RECEIPT=str(registrations))
        return installer, receipt, registrations, env

    def registered_install(self, generation=None):
        current = self.app(pruner.APP_NAME, root=self.install)
        info = current / 'Contents/Info.plist'
        data = plistlib.loads(info.read_bytes())
        if generation:
            data['FixtureGeneration'] = generation
        info.write_bytes(plistlib.dumps(data))
        executable = current / 'Contents/MacOS/fixture'
        shutil.copyfile('/usr/bin/true', executable)
        executable.chmod(0o755)
        for target in (executable, current):
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-',
                            '--timestamp=none', str(target)], check=True, capture_output=True)
        subprocess.run([str(pruner.LSREGISTER), '-f', str(current)],
                       check=True, capture_output=True, timeout=15)
        self.addCleanup(subprocess.run, [str(pruner.LSREGISTER), '-u', str(current)],
                        capture_output=True, timeout=15)
        return current

    def test_install_replaces_helper_prunes_duplicates_and_removes_staging(self):
        installer, receipt, registrations, env = self.installer_fixture()
        current = self.registered_install('old')
        backup = self.app(pruner.APP_NAME + '.backup-20261108', root=self.install)
        direct = self.app('Clash Verge Direct.app.fallback', pruner.DIRECT_ID)
        try:
            result = subprocess.run(['/bin/bash', str(installer)],
                                    env=env, text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((current / 'Contents/MacOS/fixture').is_file())
            self.assertFalse(backup.exists())
            self.assertFalse(direct.exists())
            self.assertFalse(Path(receipt.read_text().strip()).parent.exists())
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(current)],
                           capture_output=True, check=True, timeout=15)
            events = [json.loads(line) for line in registrations.read_text().splitlines()]
            canonical = [event for event in events if event['args'][-1] == str(current)]
            self.assertEqual([event['args'][0] for event in canonical], ['-u', '-f'])
            self.assertEqual(canonical[0]['generation'], 'old')
            self.assertTrue(canonical[0]['exists'])
            self.assertIsNone(canonical[1]['generation'])
            self.assertTrue(canonical[1]['exists'])
            self.assertEqual([event['args'][-1] for event in events if event['args'][0] == '-f'],
                             [str(current)])
        finally:
            subprocess.run([str(pruner.LSREGISTER), '-u', str(current)],
                           capture_output=True, timeout=15)

    def test_failed_unregister_preserves_old_install_and_cleans_staging(self):
        installer, receipt, registrations, env = self.installer_fixture()
        current = self.registered_install()
        original = (current / 'Contents/Info.plist').read_bytes()
        env['FAIL_UNREGISTER_PATH'] = str(current)
        result = subprocess.run(['/bin/bash', str(installer)], env=env,
                                text=True, capture_output=True, timeout=40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((current / 'Contents/Info.plist').read_bytes(), original)
        self.assertFalse(Path(receipt.read_text().strip()).parent.exists())
        events = [json.loads(line) for line in registrations.read_text().splitlines()]
        self.assertTrue(any(event['args'] == ['-u', str(current)] and event['exists']
                            for event in events))
        self.assertFalse(any(event['args'][0] == '-f' for event in events))

    def test_unregistered_incomplete_install_can_be_replaced(self):
        installer, receipt, registrations, env = self.installer_fixture()
        current = self.app(pruner.APP_NAME, root=self.install)
        self.addCleanup(subprocess.run, [str(pruner.LSREGISTER), '-u', str(current)],
                        capture_output=True, timeout=15)
        result = subprocess.run(['/bin/bash', str(installer)], env=env,
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((current / 'Contents/MacOS/fixture').is_file())
        self.assertFalse(Path(receipt.read_text().strip()).parent.exists())
        events = [json.loads(line) for line in registrations.read_text().splitlines()]
        canonical = [event['args'][0] for event in events if event['args'][-1] == str(current)]
        self.assertEqual(canonical, ['-f'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
