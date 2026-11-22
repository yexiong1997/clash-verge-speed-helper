"""Offline Windows packaged-bundle smoke and install-script boundary tests.

Never touches real %APPDATA% Clash Verge directories, Named Pipes, or network.
Packaged executable checks run only when CLASH_SPEED_HELPER_WINDOWS_BUNDLE
explicitly points to a staged ClashSpeedHelper bundle directory; otherwise
they raise SkipTest rather than silently passing on stale or missing builds.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

if os.name == "nt":
    import win32con
    import win32file


def _has_reparse_point(path: Path) -> bool:
    attrs = win32file.GetFileAttributesW(str(path))
    if attrs == -1:
        raise FileNotFoundError(str(path))
    return bool(attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT)


def _isolated_env(sandbox: Path, **extra) -> dict:
    appdata = sandbox / "Roaming"
    localappdata = sandbox / "Local"
    tempdir = sandbox / "Temp"
    home = sandbox / "Home"
    for folder in (appdata, localappdata, tempdir, home):
        folder.mkdir(parents=True, exist_ok=True)
    env = {
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
        "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
        "PATH": os.environ.get("PATH", ""),
        "OS": os.environ.get("OS", "Windows_NT"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(localappdata),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "TEMP": str(tempdir),
        "TMP": str(tempdir),
        "CLASH_SPEED_HELPER_ROOT": str(sandbox / "unused-profile-root"),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    env.update(extra)
    return env


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell install guard test")
class InstallScriptGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-win-install-guard-")
        self.sandbox = Path(self.tmp.name)
        self.script = ROOT / "Scripts" / "install-multi.ps1"

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke(self, *args, **env_overrides):
        env = _isolated_env(self.sandbox, **env_overrides)
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), *args],
            cwd=self.sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_help_flag_exits_cleanly_without_side_effects(self):
        res = self._invoke("-Help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("install-multi.ps1", res.stdout)
        self.assertIn(r"D:\Program Files\ClashSpeedHelper", res.stdout)
        self.assertIn(r"build\verified-worker\clash-speedtest.exe", res.stdout)
        self.assertFalse((self.sandbox / "Roaming" / "io.github.clash-verge-rev.clash-verge-rev").exists())

    def test_defaults_target_d_drive_and_reject_explicit_empty_paths(self):
        install_text = (ROOT / "Scripts" / "install-multi.ps1").read_text(encoding="utf-8")
        build_text = (ROOT / "Scripts" / "build-multi.ps1").read_text(encoding="utf-8")
        worker_text = (ROOT / "Scripts" / "build-verified-worker.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$InstallRoot = 'D:\\Program Files'", install_text)
        self.assertIn(r"build\verified-worker\clash-speedtest.exe", install_text)
        self.assertIn(r"build\verified-worker\clash-speedtest.exe", build_text)
        self.assertIn(r"build\verified-worker", worker_text)

        env = _isolated_env(self.sandbox)
        for script_name, flag in (
            ("install-multi.ps1", "-WorkerPath"),
            ("build-multi.ps1", "-WorkerPath"),
            ("build-verified-worker.ps1", "-OutputDirectory"),
        ):
            res = subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(ROOT / "Scripts" / script_name), flag, ""],
                cwd=self.sandbox,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(res.returncode, 0, f"{script_name} accepted empty {flag}")

    def test_refuses_clash_verge_config_root_drive_root_and_conflicting_flags(self):
        verge_dir = self.sandbox / "Roaming" / "io.github.clash-verge-rev.clash-verge-rev"
        verge_dir.mkdir(parents=True)
        marker = verge_dir / "profiles.yaml"
        marker.write_text("current: keep\n", encoding="utf-8")

        res_verge = self._invoke("-InstallRoot", str(verge_dir))
        self.assertNotEqual(res_verge.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "current: keep\n")

        drive_root = Path(self.sandbox.anchor)
        res_drive = self._invoke("-InstallRoot", str(drive_root))
        self.assertNotEqual(res_drive.returncode, 0)

        src_dir = self.sandbox / "src"
        src_dir.mkdir()
        res_conflict = self._invoke(
            "-SourceDir", str(src_dir),
            "-WorkerPath", str(self.sandbox / "worker.exe"),
            "-InstallRoot", str(self.sandbox / "Programs"),
        )
        self.assertNotEqual(res_conflict.returncode, 0)


@unittest.skipUnless(os.name == "nt", "Windows packaged-app smoke test")
class WindowsPackagedBundleSmokeTests(unittest.TestCase):
    REQUIRED_FILES = (
        "ClashSpeedHelper.exe",
        "clash-speedtest.exe",
        "worker-source-notices/verified-worker.patch",
        "worker-source-notices/UPSTREAM-GPL-3.0.txt",
        "worker-source-notices/NOTICE.md",
        "worker-source-notices/CONTRACT.md",
        "worker-source-notices/build-verified-worker.ps1",
    )

    def setUp(self):
        raw = os.environ.get("CLASH_SPEED_HELPER_WINDOWS_BUNDLE", "").strip()
        if not raw:
            raise unittest.SkipTest(
                "set CLASH_SPEED_HELPER_WINDOWS_BUNDLE to a freshly built ClashSpeedHelper directory"
            )
        candidate = Path(raw)
        if candidate.is_file() and candidate.name.lower() == "clashspeedhelper.exe":
            candidate = candidate.parent
        self.bundle = candidate.resolve()
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-win-pkg-smoke-")
        self.sandbox = Path(self.tmp.name)

    def tearDown(self):
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_bundle_layout_notices_and_offline_smoke_boundaries(self):
        self.assertTrue(self.bundle.is_dir(), f"missing bundle directory: {self.bundle}")
        self.assertFalse(_has_reparse_point(self.bundle))
        for rel in self.REQUIRED_FILES:
            item = self.bundle / rel
            self.assertTrue(item.is_file(), f"missing required bundle file: {rel}")
            self.assertFalse(_has_reparse_point(item), f"unexpected reparse point: {rel}")

        exe = self.bundle / "ClashSpeedHelper.exe"
        env = _isolated_env(self.sandbox)
        verge_roaming = Path(env["APPDATA"]) / "io.github.clash-verge-rev.clash-verge-rev"

        ok_smoke = subprocess.run([str(exe), "--smoke-test"], cwd=self.sandbox, env=env,
                                  capture_output=True, timeout=20)
        self.assertEqual(ok_smoke.returncode, 0)
        self.assertFalse(verge_roaming.exists())

        missing_worker_env = _isolated_env(
            self.sandbox,
            CLASH_SPEED_HELPER_WORKER=str(self.sandbox / "nonexistent-worker.exe"),
        )
        bad_smoke = subprocess.run([str(exe), "--smoke-test"], cwd=self.sandbox, env=missing_worker_env,
                                   capture_output=True, timeout=20)
        self.assertEqual(bad_smoke.returncode, 1)
        self.assertFalse(verge_roaming.exists())

        missing_root_arg = subprocess.run([str(exe), "--profile-root"], cwd=self.sandbox, env=env,
                                          capture_output=True, timeout=20)
        self.assertEqual(missing_root_arg.returncode, 2)
        self.assertFalse(verge_roaming.exists())


if __name__ == "__main__":
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        os.environ["CLASH_SPEED_HELPER_WINDOWS_BUNDLE"] = sys.argv.pop(1)
    unittest.main(verbosity=2)
