"""Exercise the desktop bridge with temporary synthetic files, not the user's data."""

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    from Windows.desktop import Desktop
    from Windows.measurement import VerifiedMeasurement
    from Windows.run import PublicApi


@unittest.skipUnless(os.name == "nt", "Windows desktop implementation")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-desktop-synthetic-")
        self.root = Path(self.tmp.name)
        (self.root / "profiles").mkdir()
        (self.root / "profiles.yaml").write_text(
            "current: u1\nitems:\n- {uid: u1, name: demo, type: local, file: one.yaml}\n", encoding="utf-8")
        (self.root / "profiles" / "one.yaml").write_text(
            "proxies:\n- {name: alpha, type: socks5, server: 127.0.0.1, port: 1, password: private-fixture}\n"
            "- {name: beta, type: socks5, server: 127.0.0.1, port: 1}\n", encoding="utf-8")
        self.app = Desktop(self.root / "not-used.exe", root=self.root)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_refresh_selection_and_no_config_leak(self):
        self.assertTrue(self.app.refresh()["ok"])
        data = self.app.get_state()
        self.assertEqual([r["name"] for r in data["rows"]], ["alpha", "beta"])
        self.assertNotIn("private-fixture", str(data))
        self.assertNotIn("127.0.0.1", str(data))
        selection = {"profiles": ["u1"], "nodes": [{"uid": "u1", "name": "alpha"}]}
        self.assertTrue(self.app.set_selection(selection)["ok"])
        self.assertEqual([r["selected"] for r in self.app.get_state()["rows"]], [True, False])
        self.assertFalse(self.app.set_selection({"profiles": ["u1"], "nodes": [{"uid": "u1", "name": "missing"}]})["ok"])

    def test_schedule_requires_explicit_rearm_after_selection_change(self):
        self.app.refresh()
        options = {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertTrue(self.app.set_schedule({"minutes": 15, "options": options})["ok"])
        self.assertEqual(self.app.get_state()["schedule_minutes"], 15)
        self.assertTrue(self.app.set_selection({"profiles": ["u1"], "nodes": [{"uid": "u1", "name": "beta"}]})["ok"])
        self.assertIsNone(self.app.get_state()["schedule_minutes"])
        self.assertFalse(self.app.start(options)["ok"])

    def test_bridge_only_exposes_narrow_methods(self):
        bridge = PublicApi(self.app)
        self.assertFalse(hasattr(bridge, "window"))
        self.assertFalse(hasattr(bridge, "registry"))
        self.assertTrue(callable(bridge.get_state))

    def test_copy_node_name_accepts_only_a_known_catalogue_identity(self):
        self.assertTrue(self.app.refresh()["ok"])
        with patch("Windows.desktop.win32clipboard") as clipboard:
            self.assertTrue(self.app.copy_node_name({"uid": "u1", "name": "alpha"})["ok"])
            clipboard.SetClipboardText.assert_called_once_with("alpha", clipboard.CF_UNICODETEXT)
            self.assertFalse(self.app.copy_node_name({"uid": "u1", "name": "missing"})["ok"])
            self.assertEqual(clipboard.OpenClipboard.call_count, 1)

    def test_funnel_promotes_highest_eligible_result_to_accurate_second_stage(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        phases = []

        class FixtureRunner:
            def __init__(self, _exe, jobs, parallel, **kwargs):
                self.jobs = tuple(jobs)
                self.parallel = parallel
                self.prelaunch = kwargs["prelaunch"]
                self.cancelled = threading.Event()
                phases.append(self)

            def run(self, callback):
                for job in self.jobs:
                    if not self.prelaunch(job):
                        self.cancelled.set()
                        callback(job, "profile_changed", None)
                        return
                    speed = 2_000_000 if job.name == "beta" else 1_000_000
                    measurement = VerifiedMeasurement(job.name, "Synthetic", 6, 6, 10, 0,
                                                      job.download_bytes, job.download_bytes, True, speed)
                    callback(job, "ok", measurement)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
            result = self.app.start_funnel({"parallel": 4, "download_bytes": 262144,
                                           "mode": "fast", "auto_switch": False,
                                           "group": "", "top_count": 1})
            self.assertTrue(result["ok"], result)
            deadline = time.monotonic() + 5
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.app.get_state()["running"])
        self.assertEqual(len(phases), 2)
        self.assertEqual((phases[0].parallel, phases[0].jobs[0].download_bytes, phases[0].jobs[0].streams),
                         (10, 1_048_576, 2))
        self.assertEqual([(j.name, j.download_bytes, j.streams) for j in phases[1].jobs],
                         [("beta", 20 * 1_048_576, 4)])
        self.assertEqual(phases[1].parallel, 2)
        state = self.app.get_state()
        self.assertEqual([(r["name"], r["selected"], r["score"] is not None) for r in state["rows"]],
                         [("alpha", False, False), ("beta", True, True)])

    def test_funnel_stop_before_promotion_does_not_start_second_stage(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        entered = threading.Event()
        release = threading.Event()
        phases = []

        class FixtureRunner:
            def __init__(self, _exe, jobs, parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()
                phases.append(self)

            def run(self, _callback):
                entered.set()
                release.wait(3)

            def cancel(self):
                self.cancelled.set()
                release.set()

        with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
            self.assertTrue(self.app.start_funnel({"parallel": 1, "download_bytes": 262144,
                                                  "mode": "fast", "auto_switch": False,
                                                  "group": "", "top_count": 1})["ok"])
            self.assertTrue(entered.wait(3))
            self.app.stop()
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.app.get_state()["running"])
        self.assertEqual(len(phases), 1)

    def test_funnel_rejects_invalid_top_count(self):
        for value in (0, 11, True, "1", None):
            with self.subTest(value=value):
                result = self.app.start_funnel({"parallel": 1, "download_bytes": 262144,
                                                "mode": "fast", "auto_switch": False,
                                                "group": "", "top_count": value})
                self.assertFalse(result["ok"])

    def test_funnel_system_failure_or_missing_result_never_launches_pressure_stage(self):
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        for failure in ("worker_failed", "invalid_output", "missing"):
            with self.subTest(failure=failure):
                self.assertTrue(self.app.refresh()["ok"])
                phases = []

                class FixtureRunner:
                    def __init__(self, _exe, jobs, parallel, **_kwargs):
                        self.jobs = tuple(jobs)
                        self.cancelled = threading.Event()
                        phases.append(self)

                    def run(self, callback):
                        first = self.jobs[0]
                        measurement = VerifiedMeasurement(first.name, "Synthetic", 6, 6, 10, 0,
                                                          first.download_bytes, first.download_bytes,
                                                          True, 1_000_000)
                        callback(first, "ok", measurement)
                        if failure != "missing":
                            callback(self.jobs[1], failure, None)

                    def cancel(self):
                        self.cancelled.set()

                with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
                    self.assertTrue(self.app.start_funnel({"parallel": 1, "download_bytes": 262144,
                                                          "mode": "fast", "auto_switch": False,
                                                          "group": "", "top_count": 1})["ok"])
                    deadline = time.monotonic() + 3
                    while self.app.get_state()["running"] and time.monotonic() < deadline:
                        time.sleep(.01)
                self.assertFalse(self.app.get_state()["running"])
                self.assertEqual(len(phases), 1)


if __name__ == "__main__":
    unittest.main()
