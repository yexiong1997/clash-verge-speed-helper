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
    from Windows import run as windows_run
    from Windows.desktop import Desktop
    from Windows.measurement import VerifiedMeasurement
    from Windows.run import PublicApi, resolve_worker


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
        public_names = {name for name in dir(bridge) if not name.startswith("_")}
        self.assertEqual(public_names, {
            "get_state", "refresh", "set_selection", "start", "retest_node",
            "start_funnel", "stop", "refresh_groups", "switch_manual",
            "undo_switch", "continue_pending", "set_schedule", "export_tsv",
            "copy_node_name",
        })

    def test_resolve_worker_and_headless_smoke_do_not_require_webview(self):
        base = self.root / "bundle"
        (base / "Windows" / "ui").mkdir(parents=True)
        (base / "Windows" / "ui" / "index.html").write_text("<!doctype html>", encoding="utf-8")
        localapp = self.root / "localapp"
        fallback = localapp / "ClashSpeedHelper" / "verified-worker" / "clash-speedtest.exe"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"fallback")
        primary = base / "build" / "verified-worker" / "clash-speedtest.exe"
        override = self.root / "override-worker.exe"
        override.write_bytes(b"override")
        frozen_dir = self.root / "frozen"
        frozen_dir.mkdir()
        frozen_exe = frozen_dir / "ClashSpeedHelper.exe"

        with patch.dict(os.environ, {"LOCALAPPDATA": str(localapp), "CLASH_SPEED_HELPER_WORKER": ""}, clear=False):
            self.assertEqual(resolve_worker(base=base, frozen=False), fallback)
            primary.parent.mkdir(parents=True)
            primary.write_bytes(b"primary")
            self.assertEqual(resolve_worker(base=base, frozen=False), primary)
            with patch.object(sys, "executable", str(frozen_exe)):
                self.assertEqual(resolve_worker(base=base, frozen=True), frozen_dir / "clash-speedtest.exe")

        with patch.dict(os.environ, {"CLASH_SPEED_HELPER_WORKER": str(override)}, clear=False):
            self.assertEqual(resolve_worker(base=base, frozen=True), override)

        with patch.dict(sys.modules, {"webview": None}):
            with patch.dict(os.environ, {"CLASH_SPEED_HELPER_WORKER": str(override)}, clear=False), \
                 patch.object(windows_run, "Path", wraps=Path), \
                 patch.object(sys, "argv", ["run.py", "--smoke-test"]):
                self.assertEqual(windows_run.main(), 0)
            with patch.dict(os.environ, {"CLASH_SPEED_HELPER_WORKER": str(self.root / "missing.exe")}, clear=False), \
                 patch.object(sys, "argv", ["run.py", "--smoke-test"]):
                self.assertEqual(windows_run.main(), 1)
            with patch.object(sys, "argv", ["run.py", "--profile-root"]):
                self.assertEqual(windows_run.main(), 2)

    def test_tun_enabled_tristate_pure_memory_read_and_security_reset(self):
        self.assertIsNone(self.app.get_state()["tun_enabled"])
        self.assertTrue(self.app.refresh()["ok"])
        self.assertIsNone(self.app.get_state()["tun_enabled"])

        verge_cfg = self.root / "clash-verge.yaml"
        verge_cfg.write_text(
            "external-controller-pipe: '\\\\.\\pipe\\private-pipe-name'\n"
            "secret: super-secret-controller-token\n"
            "tun:\n  enable: true\n",
            encoding="utf-8",
        )
        self.assertTrue(self.app.refresh()["ok"])
        state = self.app.get_state()
        self.assertIs(state["tun_enabled"], True)
        self.assertNotIn("super-secret-controller-token", str(state))
        self.assertNotIn("private-pipe-name", str(state))

        with patch("Windows.desktop.read_tun_enabled", side_effect=AssertionError("disk read during get_state")):
            self.assertIs(self.app.get_state()["tun_enabled"], True)

        verge_cfg.write_text("tun:\n  enable: false\n", encoding="utf-8")
        self.assertTrue(self.app.refresh()["ok"])
        self.assertIs(self.app.get_state()["tun_enabled"], False)

        for non_bool_payload in (
            "tun:\n  enable: 'true'\n",
            "tun:\n  enable: 1\n",
            "tun: [1, 2]\n",
            "- item1\n- item2\n",
            "tun: &anchor\n  enable: true\nalias: *anchor\n",
            "::: invalid yaml :::\n",
        ):
            with self.subTest(payload=non_bool_payload):
                verge_cfg.write_text(non_bool_payload, encoding="utf-8")
                self.assertTrue(self.app.refresh()["ok"])
                self.assertIsNone(self.app.get_state()["tun_enabled"])

        verge_cfg.write_text("tun:\n  enable: true\n", encoding="utf-8")
        self.assertTrue(self.app.refresh()["ok"])
        self.assertIs(self.app.get_state()["tun_enabled"], True)

        verge_cfg.unlink()
        verge_cfg.mkdir()
        failed = self.app.refresh()
        self.assertFalse(failed["ok"])
        self.assertIsNone(self.app.get_state()["tun_enabled"])
        self.assertNotIn(str(verge_cfg), failed["error"])

    def test_rejected_starts_preserve_generation_schedule_undo_and_pending(self):
        self.assertTrue(self.app.refresh()["ok"])
        base_opts = {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertTrue(self.app.set_schedule({"minutes": 15, "options": base_opts})["ok"])
        measurement = VerifiedMeasurement("alpha", "Synthetic", 6, 6, 10, 0, 262144, 262144, True, 1_000_000)
        self.app.undo = ("u1", "alpha", "Proxy", "beta", self.app.registry, self.app.snapshots["u1"], None, None)
        self.app.pending = ("u1", "beta", measurement, self.app.registry, dict(self.app.snapshots))
        gen_before = self.app.generation

        other_opts = {"parallel": 4, "download_bytes": 524288, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertFalse(self.app.start(other_opts)["ok"])
        self.assertFalse(self.app.start({"parallel": 0, "download_bytes": 262144, "mode": "fast"})["ok"])
        self.assertFalse(self.app.start_funnel({**other_opts, "top_count": 2})["ok"])

        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        self.assertFalse(self.app.start({**other_opts, "auto_switch": True, "group": "Missing"})["ok"])
        self.assertFalse(self.app.start_funnel({**other_opts, "auto_switch": True, "group": "Missing", "top_count": 2})["ok"])

        self.assertEqual(self.app.generation, gen_before)
        self.assertEqual(self.app.get_state()["schedule_minutes"], 15)
        self.assertIsNotNone(self.app.get_state()["undo"])
        self.assertIsNotNone(self.app.get_state()["pending"])

        entered = threading.Event()
        release = threading.Event()

        class BlockingRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, _callback):
                entered.set()
                release.wait(3)

            def cancel(self):
                self.cancelled.set()
                release.set()

        with patch("Windows.desktop.MultiBatchRunner", BlockingRunner):
            self.assertTrue(self.app.start(base_opts)["ok"])
            self.assertTrue(entered.wait(3))
            running_gen = self.app.generation
            self.assertFalse(self.app.start(other_opts)["ok"])
            self.assertFalse(self.app.start_funnel({**other_opts, "top_count": 1})["ok"])
            self.assertFalse(self.app.retest_node({"uid": "u1", "name": "alpha", "auto_switch": False, "group": ""})["ok"])
            self.assertEqual(self.app.generation, running_gen)
            self.assertEqual(self.app.get_state()["schedule_minutes"], 15)
            self.assertIsNotNone(self.app.get_state()["undo"])
            self.assertFalse(self.app.runner.cancelled.is_set())
            release.set()
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

    def test_set_schedule_boolean_rejected_and_concurrent_disarm_is_atomic(self):
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        self.assertTrue(self.app.refresh()["ok"])
        opts = {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertTrue(self.app.set_schedule({"minutes": 15, "options": opts})["ok"])
        self.assertFalse(self.app.set_schedule({"minutes": False})["ok"])
        self.assertEqual(self.app.get_state()["schedule_minutes"], 15)
        self.assertTrue(self.app.set_schedule({"minutes": 0})["ok"])
        self.assertIsNone(self.app.get_state()["schedule_minutes"])
        self.assertIsNone(self.app.scheduled_options)

        class InstantRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback):
                for job in self.jobs:
                    m = VerifiedMeasurement(job.name, "Synthetic", 6, 6, 10, 0, job.download_bytes, job.download_bytes, True, 1_000_000)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", InstantRunner):
            for _ in range(20):
                self.assertTrue(self.app.set_schedule({"minutes": 15, "options": opts})["ok"])
                with self.app.lock:
                    now = time.monotonic()
                    self.app.schedule.last = now - 10
                    self.app.schedule.deadline = now - 1
                barrier = threading.Barrier(2)

                def poll_once():
                    barrier.wait()
                    with self.app.lock:
                        state, token = self.app.schedule.poll(
                            external_busy=self.app.running or self.app.refreshing or self.app.closed.is_set()
                        )
                        options = self.app.scheduled_options
                    if state == "started":
                        try:
                            if not options:
                                raise ValueError()
                            with self.app.lock:
                                chosen, snapshots, capture = self.app._validate_launch(options, True)
                                self.app._start_validated_launch(options, token, True, chosen, snapshots, capture)
                        except Exception:
                            with self.app.lock:
                                self.app.schedule.finish(token)
                                if self.app.scheduled_options is options:
                                    self.app.schedule.disarm()
                                    self.app.scheduled_options = None

                t = threading.Thread(target=poll_once)
                t.start()
                barrier.wait()
                self.assertTrue(self.app.set_schedule({"minutes": 0})["ok"])
                t.join(3)
                deadline = time.monotonic() + 3
                while self.app.get_state()["running"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIsNone(self.app.get_state()["schedule_minutes"])
                self.assertIsNone(self.app.scheduled_options)
                self.assertIsNone(self.app.schedule.active)

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
