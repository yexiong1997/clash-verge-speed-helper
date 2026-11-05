#!/usr/bin/env python3
"""Focused synthetic offline-builder tests; never use real proxy nodes.

    python3 -B Tests/test_worker_offline_build.py
    VERIFIED_WORKER_REAL_CACHE=/absolute/path/to/cache python3 -B Tests/test_worker_offline_build.py

The opt-in real-cache test performs a fresh source build in a private temporary
project on macOS, under a deny-network / deny-external-writes sandbox. It probes
both TCP and Unix sockets and a forbidden write before invoking the builder.
The test never executes the produced worker.
"""
import base64
import errno
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
BUILDER = PROJECT / "Scripts" / "build-verified-worker.sh"
PATCH = PROJECT / "Worker" / "verified-worker.patch"
DARWIN = sys.platform == "darwin"
TEMP_PARENT = "/private/tmp" if DARWIN else "/tmp"
SANDBOX = Path("/usr/bin/sandbox-exec")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_tar(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def go_h1(files):
    # Go's dirhash.Hash1: hash each file, then hash the sorted manifest.
    manifest = "".join("%s  %s\n" % (hashlib.sha256(data).hexdigest(), name)
                       for name, data in sorted(files.items())).encode()
    return "h1:" + base64.b64encode(hashlib.sha256(manifest).digest()).decode()


def host_go_filename():
    system, machine = platform.system(), platform.machine()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(machine)
    if system not in ("Darwin", "Linux") or arch is None:
        raise unittest.SkipTest("unsupported host")
    return "go1.25.0.%s-%s.tar.gz" % (system.lower(), arch)


FAKE_GO = b'''#!/bin/sh
set -eu
[ "$GOPROXY" = off ] && [ "$GOSUMDB" = off ] && [ "$GOTOOLCHAIN" = local ]
[ "$GOENV" = off ] && [ "$GOWORK" = off ] && [ "$GOFLAGS" = -mod=readonly ]
[ "${HTTP_PROXY-unset}" = unset ] && [ "${HTTPS_PROXY-unset}" = unset ]
[ "${ALL_PROXY-unset}" = unset ] && [ "${http_proxy-unset}" = unset ]
[ "${https_proxy-unset}" = unset ] && [ "${all_proxy-unset}" = unset ]
case "$GOMODCACHE:$GOCACHE:$GOPATH" in
  "${HOME%/home}"/modcache:"${HOME%/home}"/gocache:"${HOME%/home}"/gopath) ;;
  *) exit 25 ;;
esac
[ -f "$GOMODCACHE/cache/download/example.com/mod/@v/v1.0.0.zip" ]
[ ! -e "$GOMODCACHE/cache/download/example.com/mod/@v/v1.0.0.ziphash" ]
[ ! -e "$GOMODCACHE/example.com/mod@v1.0.0" ]
[ ! -e "$GOCACHE/old-executable" ]
printf '%s\n' "$*" >> "$GOPATH/calls"
if [ "$1" = telemetry ] && [ "$2" = off ]; then
  :
elif [ "$1" = mod ] && [ "$2" = download ]; then
  if grep -q MUTATE_SUM "$GOMODCACHE/cache/download/example.com/mod/@v/v1.0.0.mod"; then
    printf 'unexpected checksum\n' >> go.sum
  fi
elif [ "$1" = mod ] && [ "$2" = verify ]; then
  :
elif [ "$1" = build ]; then
  grep -q '^patched$' note.txt
  while [ "$#" -gt 0 ]; do
    if [ "$1" = -o ]; then shift; output=$1; break; fi
    shift
  done
  printf '#!/bin/sh\nexit 0\n' > "$output"
  chmod 700 "$output"
else
  exit 26
fi
'''


class OfflineBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="worker-offline-test-", dir=TEMP_PARENT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "project"
        self.cache = self.root / "cache"
        (self.project / "Scripts").mkdir(parents=True)
        (self.project / "Worker").mkdir()
        (self.cache / "modcache" / "cache" / "download" / "example.com" / "mod" / "@v").mkdir(parents=True)
        self.moddir = self.cache / "modcache" / "cache" / "download" / "example.com" / "mod" / "@v"
        (self.moddir / "v1.0.0.zip").write_bytes(b"synthetic zip")
        (self.moddir / "v1.0.0.mod").write_bytes(b"synthetic module")
        (self.moddir / "v1.0.0.info").write_bytes(b"synthetic info")
        (self.moddir / "v1.0.0.ziphash").write_bytes(b"untrusted hash")
        (self.cache / "modcache" / "example.com" / "mod@v1.0.0").mkdir(parents=True)
        (self.cache / "modcache" / "example.com" / "mod@v1.0.0" / "old.go").write_text("untrusted")
        (self.cache / "gocache").mkdir()
        (self.cache / "gocache" / "old-executable").write_bytes(b"do not reuse")
        (self.cache / "clash-speedtest").write_bytes(b"do not reuse")
        source = self.cache / "upstream.tar.gz"
        go = self.cache / host_go_filename()
        write_tar(source, [
            ("upstream/go.mod", b"module example.com/root\n\ngo 1.25.0\n", 0o644),
            ("upstream/go.sum", b"example.com/mod v1.0.0 h1:synthetic\n", 0o644),
            ("upstream/note.txt", b"original\n", 0o644),
        ])
        write_tar(go, [("go/bin/go", FAKE_GO, 0o755)])
        text = BUILDER.read_text()
        text, n = re.subn(r"(?m)^readonly SOURCE_SHA256='[0-9a-f]{64}'$",
                          "readonly SOURCE_SHA256='%s'" % sha(source), text)
        self.assertEqual(n, 1)
        text, n = re.subn(r"GO_SHA256=[0-9a-f]{64}", "GO_SHA256=" + sha(go), text)
        self.assertEqual(n, 4)
        self.builder = self.project / "Scripts" / "build-verified-worker.sh"
        self.builder.write_text(text)
        self.builder.chmod(0o700)
        (self.project / "Worker" / "verified-worker.patch").write_text(
            "--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-original\n+patched\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.curl_marker = self.root / "curl-called"
        fake_curl = self.bin / "curl"
        fake_curl.write_text("#!/bin/sh\nprintf called > %s\nexit 91\n" % self.curl_marker)
        fake_curl.chmod(0o700)

    def run_builder(self, cache=None, *, offline=True, extra=None):
        env = os.environ.copy()
        env.update({"PATH": str(self.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                    "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1"})
        if offline:
            env["VERIFIED_WORKER_OFFLINE_CACHE"] = str(self.cache if cache is None else cache)
        else:
            env.pop("VERIFIED_WORKER_OFFLINE_CACHE", None)
        if extra:
            env.update(extra)
        proc = subprocess.run(["/bin/bash", str(self.builder)], cwd=self.project, env=env,
                              text=True, capture_output=True, timeout=60)
        self.assertFalse(self.curl_marker.exists(), "offline path tried to invoke curl")
        return proc

    def assert_rejected(self, *, cache=None, contains=None):
        p = self.run_builder(cache)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        if contains:
            self.assertIn(contains, p.stderr)
        self.assertFalse((self.project / "build" / "verified-worker" / "clash-speedtest").exists())

    def test_fresh_build_uses_only_isolated_download_cache(self):
        before = {str(p.relative_to(self.cache)): sha(p) for p in self.cache.rglob("*") if p.is_file()}
        p = self.run_builder(extra={"VERIFIED_WORKER_GOPROXY": "invalid-but-ignored-offline"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        runs = list((self.project / "build").glob(".verified-worker.*"))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run / "gopath" / "calls").read_text().splitlines(), [
            "telemetry off", "mod download", "mod verify",
            "build -p 4 -trimpath -buildvcs=false -o %s ./cmd/verified-worker" % (run / "clash-speedtest"),
            "mod verify"])
        self.assertEqual((run / "source" / "note.txt").read_text(), "patched\n")
        self.assertTrue((self.project / "build" / "verified-worker" / "clash-speedtest").is_file())
        after = {str(p.relative_to(self.cache)): sha(p) for p in self.cache.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "source cache must never be modified")
        self.assertIn("not fetching release metadata", p.stderr)

    def test_absolute_existing_directory_required(self):
        for path in ("", "relative/cache", str(self.root / "missing"), "/"):
            with self.subTest(path=path):
                self.assert_rejected(cache=path)

    def test_cache_or_parent_symlink_refused(self):
        link = self.root / "cache-link"
        link.symlink_to(self.cache, target_is_directory=True)
        self.assert_rejected(cache=link)

    def test_missing_and_tampered_archives_refused(self):
        for name in ("upstream.tar.gz", host_go_filename()):
            with self.subTest(name=name):
                path = self.cache / name
                old = path.read_bytes()
                path.unlink()
                self.assert_rejected()
                path.write_bytes(old + b"tampered")
                self.assert_rejected(contains="SHA-256")
                path.write_bytes(old)

    def test_symlink_archive_refused(self):
        source = self.cache / "upstream.tar.gz"
        source.rename(self.root / "real-archive")
        source.symlink_to(self.root / "real-archive")
        self.assert_rejected()

    def test_module_symlink_and_fifo_refused_even_when_not_selected(self):
        for kind in ("link", "fifo"):
            with self.subTest(kind=kind):
                path = self.cache / "modcache" / "danger"
                if kind == "link":
                    path.symlink_to(self.root, target_is_directory=True)
                else:
                    os.mkfifo(path)
                self.assert_rejected()
                path.unlink()

    def test_hardlinked_module_file_refused(self):
        os.link(self.moddir / "v1.0.0.zip", self.root / "other-link")
        self.assert_rejected(contains="hard-linked")

    def test_other_writable_cache_and_module_refused(self):
        for path in (self.cache, self.cache / "modcache", self.moddir / "v1.0.0.zip"):
            with self.subTest(path=path):
                old = path.stat().st_mode & 0o777
                path.chmod(old | 0o020)
                self.assert_rejected(contains="writable")
                path.chmod(old)

    def test_incomplete_module_cache_refused(self):
        (self.cache / "modcache" / "cache" / "download").rename(self.root / "not-download")
        self.assert_rejected()

    def test_go_sum_change_stops_before_compilation(self):
        (self.moddir / "v1.0.0.mod").write_text("MUTATE_SUM")
        self.assert_rejected()
        run = next((self.project / "build").glob(".verified-worker.*"))
        self.assertEqual((run / "gopath" / "calls").read_text().splitlines(), ["telemetry off", "mod download"])

    def test_existing_output_is_not_overwritten(self):
        output = self.project / "build" / "verified-worker" / "clash-speedtest"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"original binary")
        output.chmod(0o700)
        p = self.run_builder()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(output.read_bytes(), b"original binary")
        self.assertIn("existing worker differs", p.stderr)

    def test_symlink_output_directory_refused(self):
        (self.project / "build").mkdir()
        (self.project / "build" / "verified-worker").symlink_to(self.root, target_is_directory=True)
        self.assert_rejected()

    def test_writable_project_directory_refused(self):
        self.project.chmod(0o777)
        self.assert_rejected(contains="writable")
        self.project.chmod(0o700)

    def test_foreign_owned_cache_refused(self):
        if os.geteuid() == 0:
            self.skipTest("requires a non-root test user")
        self.assert_rejected(cache=TEMP_PARENT, contains="ownership")

    @unittest.skipUnless(DARWIN, "macOS ACL syntax required")
    def test_cache_acl_refused(self):
        f = self.moddir / "v1.0.0.zip"
        result = subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(f)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest("cannot create ACL in fixture: " + result.stderr)
        self.assert_rejected(contains="ACL")

    def test_patch_cannot_write_outside_source(self):
        canary = self.root / "forbidden-write"
        canary.write_text("unchanged\n")
        (self.project / "Worker" / "verified-worker.patch").write_text(
            "--- a/../../forbidden-write\n+++ b/../../forbidden-write\n"
            "@@ -1 +1 @@\n-unchanged\n+changed\n")
        self.assert_rejected(contains="unsafe source patch")
        self.assertEqual(canary.read_text(), "unchanged\n")

    def test_online_mode_still_attempts_metadata_fetch(self):
        env = os.environ.copy()
        env.pop("VERIFIED_WORKER_OFFLINE_CACHE", None)
        env.pop("VERIFIED_WORKER_GOPROXY", None)
        env["PATH"] = str(self.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        p = subprocess.run(["/bin/bash", str(self.builder)], cwd=self.project,
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(self.curl_marker.exists(), "online metadata fetch was skipped")
        self.assertIn("public artifact download failed", p.stderr)

    def test_online_proxy_validation_remains_enabled(self):
        p = self.run_builder(offline=False, extra={"VERIFIED_WORKER_GOPROXY": "invalid"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("VERIFIED_WORKER_GOPROXY must be", p.stderr)


def quote_profile(path):
    text = str(path)
    if any(c in text for c in ('"', "\\", "\n", "\r")):
        raise ValueError("unquotable sandbox path")
    return '"' + text + '"'


class RealOfflineBuildTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("VERIFIED_WORKER_REAL_CACHE"), "set VERIFIED_WORKER_REAL_CACHE for real sandboxed build")
    def test_real_build_under_network_and_write_denial(self):
        if not DARWIN or not SANDBOX.is_file():
            self.skipTest("a verified deny-network macOS sandbox is required")
        cache = Path(os.environ["VERIFIED_WORKER_REAL_CACHE"])
        if not cache.is_absolute():
            self.fail("VERIFIED_WORKER_REAL_CACHE must be absolute")
        with tempfile.TemporaryDirectory(prefix="worker-offline-real-", dir=TEMP_PARENT) as tmp:
            root = Path(tmp).resolve()
            project = root / "project"
            (project / "Scripts").mkdir(parents=True)
            (project / "Worker").mkdir()
            shutil.copy2(BUILDER, project / "Scripts" / BUILDER.name)
            shutil.copy2(PATCH, project / "Worker" / PATCH.name)
            # Children can read the SDK/toolchain and the source cache but may
            # write only into the private synthetic project and /dev/null.
            profile = "\n".join([
                "(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)",
                '(allow file-write* (subpath %s) (literal "/dev/null"))' % quote_profile(project),
            ])
            python = Path(sys.executable).resolve()
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(tcp.close)
            self.addCleanup(unix.close)
            tcp.bind(("127.0.0.1", 0))
            tcp.listen(1)
            unix_path = root / "denied.sock"
            unix.bind(str(unix_path))
            unix.listen(1)
            canary = r'''import errno, socket, sys
for family, target in ((socket.AF_INET, ('127.0.0.1', int(sys.argv[1]))),
                       (socket.AF_UNIX, sys.argv[2])):
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        try: s.connect(target)
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.EACCES): raise
        else: raise RuntimeError('network not denied')
try:
    with open(sys.argv[3], 'w') as f: f.write('forbidden')
except OSError as e:
    if e.errno not in (errno.EPERM, errno.EACCES): raise
else: raise RuntimeError('external write not denied')
'''
            env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(project),
                   "TMPDIR": str(project), "VERIFIED_WORKER_OFFLINE_CACHE": str(cache),
                   "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}
            prefix = [str(SANDBOX), "-p", profile]
            probe = subprocess.run(prefix + [str(python), "-B", "-c", canary,
                                    str(tcp.getsockname()[1]), str(unix_path), str(root / "forbidden")],
                                   cwd=project, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(probe.returncode, 0, "sandbox canary failed: " + probe.stderr)
            self.assertFalse((root / "forbidden").exists())
            result = subprocess.run(prefix + ["/bin/bash", str(project / "Scripts" / BUILDER.name)],
                                    cwd=project, env=env, text=True, capture_output=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            worker = project / "build" / "verified-worker" / "clash-speedtest"
            runs = list((project / "build").glob(".verified-worker.*"))
            self.assertEqual(len(runs), 1)
            telemetry_dir = runs[0] / "home/Library/Application Support/go/telemetry"
            self.assertTrue((telemetry_dir / "mode").read_text().startswith("off"))
            self.assertFalse((telemetry_dir / "local").exists())
            self.assertEqual(sha(worker), sha(runs[0] / "clash-speedtest"))
            print("REAL_OFFLINE_WORKER_SHA256=" + sha(worker), flush=True)
            print("REAL_OFFLINE_SOURCE_SHA256=" + sha(runs[0] / "upstream.tar.gz"), flush=True)
            print("REAL_OFFLINE_GO_SHA256=" + sha(runs[0] / host_go_filename()), flush=True)
            print("REAL_OFFLINE_SANDBOX=TCP_DENIED,UNIX_DENIED,EXTERNAL_WRITE_DENIED", flush=True)

    @unittest.skipUnless(os.environ.get("VERIFIED_WORKER_REAL_CACHE"), "set VERIFIED_WORKER_REAL_CACHE for real Go test")
    def test_real_go_rejects_forged_module_and_ziphash(self):
        if not DARWIN or not SANDBOX.is_file():
            self.skipTest("a deny-network macOS sandbox is required")
        source_cache = Path(os.environ["VERIFIED_WORKER_REAL_CACHE"])
        if not source_cache.is_absolute():
            self.fail("VERIFIED_WORKER_REAL_CACHE must be absolute")
        with tempfile.TemporaryDirectory(prefix="worker-offline-tamper-", dir=TEMP_PARENT) as tmp:
            root = Path(tmp).resolve()
            project, cache = root / "project", root / "cache"
            (project / "Scripts").mkdir(parents=True)
            (project / "Worker").mkdir()
            download = cache / "modcache/cache/download/example.com/dependency/@v"
            download.mkdir(parents=True)
            shutil.copy2(source_cache / host_go_filename(), cache / host_go_filename())
            module_go_mod = b"module example.com/dependency\n\ngo 1.24.0\n"
            prefix = "example.com/dependency@v1.0.0/"
            good = {prefix + "go.mod": module_go_mod,
                    prefix + "dependency.go": b"package dependency\nconst Value = 1\n"}
            bad = dict(good)
            bad[prefix + "dependency.go"] = b"package dependency\nconst Value = 99\n"
            with zipfile.ZipFile(download / "v1.0.0.zip", "w") as z:
                for name, data in bad.items():
                    z.writestr(name, data)
            (download / "v1.0.0.ziphash").write_text(go_h1(bad) + "\n")
            (download / "v1.0.0.mod").write_bytes(module_go_mod)
            (download / "v1.0.0.info").write_text('{"Version":"v1.0.0","Time":"2025-01-01T00:00:00Z"}\n')
            root_mod = b"module example.com/root\n\ngo 1.24.0\n\nrequire example.com/dependency v1.0.0\n"
            root_sum = ("example.com/dependency v1.0.0 %s\n"
                        "example.com/dependency v1.0.0/go.mod %s\n" %
                        (go_h1(good), go_h1({"go.mod": module_go_mod}))).encode()
            archive = cache / "upstream.tar.gz"
            write_tar(archive, [
                ("upstream/go.mod", root_mod, 0o644), ("upstream/go.sum", root_sum, 0o644),
                ("upstream/note.txt", b"original\n", 0o644),
                ("upstream/cmd/verified-worker/main.go",
                 b'package main\nimport "example.com/dependency"\nfunc main() { _ = dependency.Value }\n', 0o644),
            ])
            text, n = re.subn(r"(?m)^readonly SOURCE_SHA256='[0-9a-f]{64}'$",
                              "readonly SOURCE_SHA256='%s'" % sha(archive), BUILDER.read_text())
            self.assertEqual(n, 1)
            builder = project / "Scripts" / BUILDER.name
            builder.write_text(text)
            (project / "Worker" / PATCH.name).write_text(
                "--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-original\n+patched\n")
            profile = "\n".join([
                "(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)",
                '(allow file-write* (subpath %s) (literal "/dev/null"))' % quote_profile(project),
            ])
            env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(project),
                   "TMPDIR": str(project), "VERIFIED_WORKER_OFFLINE_CACHE": str(cache)}
            p = subprocess.run([str(SANDBOX), "-p", profile, "/bin/bash", str(builder)],
                               cwd=project, env=env, text=True, capture_output=True, timeout=180)
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("checksum mismatch", p.stderr)
            self.assertFalse((project / "build/verified-worker/clash-speedtest").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
