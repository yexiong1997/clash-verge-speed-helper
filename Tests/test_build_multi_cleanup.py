#!/usr/bin/env python3
"""Exercise the builder's private scratch cleanup with disposable fixtures only."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = (PROJECT / 'Scripts' / 'build-multi.sh').read_text(encoding='utf-8')
START = SCRIPT.index('cleanup_swift_tmp() {')
END = SCRIPT.index('\ncleanup_on_exit() {', START)
CLEANUP_FUNCTION = SCRIPT[START:END]
ARCHIVE_START = SCRIPT.index('resolve_worker_source_archive() {')
ARCHIVE_END = SCRIPT.index('\n}\n', ARCHIVE_START) + 2
ARCHIVE_FUNCTION = SCRIPT[ARCHIVE_START:ARCHIVE_END]


def identity(path):
    info = os.stat(path, follow_symlinks=False)
    return f'{info.st_dev}:{info.st_ino}'


def invoke(path, expected):
    source = ('set -euo pipefail\nswift_tmp=$1\nswift_tmp_id=$2\n'
              + CLEANUP_FUNCTION + '\ncleanup_swift_tmp\n')
    return subprocess.run(['/bin/bash', '-c', source, 'cleanup-test', str(path), expected],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


class ScratchCleanupTests(unittest.TestCase):
    def test_removes_only_owned_tree_without_following_child_symlink(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-swift.', dir='/private/tmp') as scratch, \
             tempfile.TemporaryDirectory(prefix='clashmulti-outside.', dir='/private/tmp') as outside:
            root, external = Path(scratch), Path(outside)
            (root / 'nested').mkdir()
            (root / 'nested' / 'temporary').write_text('temporary', encoding='utf-8')
            marker = external / 'keep'
            marker.write_text('keep', encoding='utf-8')
            (root / 'link').symlink_to(external, target_is_directory=True)
            result = invoke(root, identity(root))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertFalse(root.exists())
            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_rejects_changed_inode_without_removing_tree(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-swift.', dir='/private/tmp') as scratch:
            root = Path(scratch)
            marker = root / 'keep'
            marker.write_text('keep', encoding='utf-8')
            result = invoke(root, '0:0')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_rejects_unexpected_directory_prefix(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-other.', dir='/private/tmp') as scratch:
            root = Path(scratch)
            marker = root / 'keep'
            marker.write_text('keep', encoding='utf-8')
            result = invoke(root, identity(root))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')


class SourceArchiveResolutionTests(unittest.TestCase):
    def resolve(self, root, offline=None):
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
        if offline is not None:
            env['VERIFIED_WORKER_OFFLINE_CACHE'] = offline
        shell = 'set -euo pipefail\n' + ARCHIVE_FUNCTION + '\nresolve_worker_source_archive "$1"\n'
        return subprocess.run(['/bin/bash', '-c', shell, 'archive-test', str(root)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10)

    def test_online_bootstrap_archive_path(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-archive.', dir='/private/tmp') as folder:
            root = Path(folder)
            result = self.resolve(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode().strip(),
                             str(root / 'verified-worker-bootstrap' / 'upstream.tar.gz'))

    def test_offline_uses_unique_private_run_even_if_cache_is_empty(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-archive.', dir='/private/tmp') as folder:
            root = Path(folder)
            run = root / '.verified-worker.1234'
            run.mkdir()
            result = self.resolve(root, offline='')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode().strip(), str(run / 'upstream.tar.gz'))

    def test_offline_rejects_missing_or_ambiguous_runs(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-archive.', dir='/private/tmp') as folder:
            root = Path(folder)
            self.assertNotEqual(self.resolve(root, offline='/cache').returncode, 0)
            (root / '.verified-worker.1234').mkdir()
            (root / '.verified-worker.5678').mkdir()
            self.assertNotEqual(self.resolve(root, offline='/cache').returncode, 0)

    def test_offline_rejects_symlinked_run(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-archive.', dir='/private/tmp') as folder:
            root = Path(folder)
            target = root / 'other'
            target.mkdir()
            (root / '.verified-worker.1234').symlink_to(target, target_is_directory=True)
            self.assertNotEqual(self.resolve(root, offline='/cache').returncode, 0)


class OutputReservationTests(unittest.TestCase):
    def run_builder(self, output):
        return subprocess.run(['/bin/bash', str(PROJECT / 'Scripts' / 'build-multi.sh'),
                               '--output', str(output)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15,
                              env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                                   'HOME': str(output.parent)})

    def test_existing_app_is_untouched(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-output.', dir='/private/tmp') as parent:
            output = Path(parent) / 'already.app'
            output.mkdir()
            marker = output / 'keep'
            marker.write_text('keep', encoding='utf-8')
            result = self.run_builder(output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'output already exists', result.stderr)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_dangling_destination_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='clashmulti-output.', dir='/private/tmp') as parent:
            output = Path(parent) / 'already.app'
            output.symlink_to('missing')
            result = self.run_builder(output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'output already exists', result.stderr)
            self.assertTrue(output.is_symlink())
            self.assertFalse((Path(parent) / 'missing').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
