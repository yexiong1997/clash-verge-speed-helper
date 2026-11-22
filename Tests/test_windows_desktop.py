"""Exercise the desktop bridge with temporary synthetic files, not the user's data."""

import json
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
    from Windows.settings import AppSettingsStore
    from Windows.tray import (CMD_EXIT, CMD_SHOW_HIDE, CMD_STOP_PATROL, CMD_STOP_RUN,
                              CMD_STOP_SCHEDULE, CMD_TOGGLE_MINIMIZE,
                              SingleInstanceGuard, build_tray_menu_model)


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

    def test_refresh_allows_rule_providers_and_skips_incompatible_profile_only(self):
        (self.root / "profiles.yaml").write_text(
            "current: u1\nitems:\n"
            "- {uid: u1, name: ok_rules, type: remote, file: one.yaml}\n"
            "- {uid: u2, name: bad_provider, type: remote, file: two.yaml}\n"
            "- {uid: u3, name: empty_nodes, type: local, file: three.yaml}\n",
            encoding="utf-8",
        )
        (self.root / "profiles" / "one.yaml").write_text(
            "rule-providers:\n  geosite: {type: http}\n"
            "proxies:\n- {name: alpha, type: socks5, server: 127.0.0.1, port: 1}\n",
            encoding="utf-8",
        )
        (self.root / "profiles" / "two.yaml").write_text(
            "proxy-providers: {pool: {type: http}}\nproxies: []\n",
            encoding="utf-8",
        )
        (self.root / "profiles" / "three.yaml").write_text("proxies: []\n", encoding="utf-8")
        res = self.app.refresh()
        self.assertTrue(res["ok"], res)
        st = self.app.get_state()
        self.assertEqual([(p["uid"], p["compatible"], p["selected"]) for p in st["profiles"]],
                         [("u1", True, True), ("u2", False, False), ("u3", False, False)])
        self.assertEqual([r["name"] for r in st["rows"]], ["alpha"])
        self.assertIn("已跳过 2 个不兼容订阅", st["status"])
        self.assertFalse(self.app.set_selection({"profiles": ["u2"], "nodes": []})["ok"])

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
            "undo_switch", "continue_pending", "set_schedule", "set_patrol",
            "get_history", "clear_history", "export_tsv", "copy_node_name",
            "copy_node_summary", "probe_active_node", "save_preferences",
            "hide_to_tray", "set_minimize_to_tray", "configure_runtime_bridge",
        })

    def test_resolve_worker_and_headless_smoke_do_not_require_webview(self):
        base = self.root / "bundle"
        (base / "Windows" / "ui").mkdir(parents=True)
        (base / "Windows" / "ui" / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (base / "Windows" / "ui" / "app.ico").write_bytes(b"\x00\x00\x01\x00")
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

    def test_funnel_preserves_stage1_score_and_records_telemetry_history(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable

        class FixtureRunner:
            def __init__(self, _exe, jobs, parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback, on_progress=None):
                for job in self.jobs:
                    speed = 3_000_000 if job.name == "beta" else 1_200_000
                    measurement = VerifiedMeasurement(job.name, "vless", 6, 6, 15, 1,
                                                      job.download_bytes, job.download_bytes, True, speed)
                    callback(job, "ok", measurement)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
            self.assertTrue(self.app.start_funnel({"parallel": 4, "download_bytes": 262144,
                                                   "mode": "fast", "auto_switch": False,
                                                   "group": "", "top_count": 1})["ok"])
            deadline = time.monotonic() + 5
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

        state = self.app.get_state()
        alpha_row = next(r for r in state["rows"] if r["name"] == "alpha")
        self.assertIsNotNone(alpha_row["stage1_score"])
        self.assertTrue(alpha_row["state"].startswith("初筛 "))

        hist = self.app.get_history({"window": "7d"})
        self.assertTrue(hist["ok"])
        by_name = {s["name"]: s for s in hist["summaries"]}
        self.assertEqual(set(by_name), {"alpha", "beta"})
        self.assertEqual(by_name["beta"]["total_count"], 2)  # S1 + S2
        self.assertEqual(by_name["alpha"]["total_count"], 1)  # S1 only
        self.assertTrue(self.app.clear_history()["ok"])
        self.assertEqual(self.app.get_history({"window": "7d"})["summaries"], [])

    def test_patrol_is_mutually_exclusive_with_schedule_and_keeps_checkboxes(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable

        opts = {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertTrue(self.app.set_schedule({"minutes": 15, "options": opts})["ok"])
        self.assertEqual(self.app.get_state()["schedule_minutes"], 15)

        # Enabling patrol disarms regular schedule
        self.assertTrue(self.app.set_patrol({"enabled": True, "quota_bytes": 150 * 1_048_576})["ok"])
        st = self.app.get_state()
        self.assertIsNone(st["schedule_minutes"])
        self.assertTrue(st["patrol"]["armed"])
        self.assertEqual(st["patrol"]["minutes"], 30)

        # Only alpha selected by user; patrol still tests eligible nodes without changing row.selected
        self.assertTrue(self.app.set_selection({"profiles": ["u1"], "nodes": [{"uid": "u1", "name": "alpha"}]})["ok"])
        self.assertTrue(self.app.set_patrol({"enabled": True, "quota_bytes": 80 * 1_048_576})["ok"])
        with self.app.lock:
            now = time.monotonic()
            self.app.patrol_schedule.last = now - 10
            self.app.patrol_schedule.deadline = now - 1

        class InstantRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback, on_progress=None):
                for job in self.jobs:
                    m = VerifiedMeasurement(job.name, "ss", 6, 6, 20, 1, job.download_bytes, job.download_bytes, True, 2_000_000)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", InstantRunner):
            self.app._poll_patrol()
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

        st2 = self.app.get_state()
        self.assertEqual([(r["name"], r["selected"]) for r in st2["rows"]], [("alpha", True), ("beta", False)])
        self.assertEqual(st2["patrol"]["used_bytes_today"], 2 * 1_048_576)

    def test_retest_node_accepts_unselected_compatible_profile(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable
        self.assertTrue(self.app.set_selection({"profiles": [], "nodes": []})["ok"])

        class InstantRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback, on_progress=None):
                for job in self.jobs:
                    m = VerifiedMeasurement(job.name, "ss", 6, 6, 20, 1, job.download_bytes, job.download_bytes, True, 2_000_000)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", InstantRunner):
            self.assertTrue(self.app.retest_node({"uid": "u1", "name": "beta", "auto_switch": False, "group": ""})["ok"])
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)
        st = self.app.get_state()
        self.assertEqual([(r["name"], r["selected"], r["score"] is not None) for r in st["rows"]],
                         [("alpha", False, False), ("beta", False, True)])
        self.assertTrue(any(p["uid"] == "u1" and p["selected"] for p in st["profiles"]))

    def test_lossless_retest_preserves_other_scores_selection_and_scopes_auto_switch(self):
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable

        from Windows.batch import ProgressUpdate
        from Windows.controller import ProxyGroup, Settings

        class InitialRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback, on_progress=None):
                for job in self.jobs:
                    bps = 20 * 1_048_576 if job.name == "alpha" else 4 * 1_048_576
                    if on_progress:
                        on_progress(job, ProgressUpdate("download", 524_288, job.download_bytes, bps * 0.8))
                        on_progress(job, ProgressUpdate("download", job.download_bytes, job.download_bytes, bps))
                    m = VerifiedMeasurement(job.name, "ss", 6, 6, 20, 1, job.download_bytes, job.download_bytes, True, bps)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", InitialRunner):
            self.assertTrue(self.app.start({"parallel": 2, "download_bytes": 1_048_576, "mode": "fast", "auto_switch": False, "group": ""})["ok"])
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

        st1 = self.app.get_state()
        alpha1 = next(r for r in st1["rows"] if r["name"] == "alpha")
        beta1 = next(r for r in st1["rows"] if r["name"] == "beta")
        self.assertGreater(alpha1["score"], beta1["score"])
        self.assertGreaterEqual(len(alpha1["samples"]), 2)

        # Retest only beta at 20MiB with auto_switch=True; alpha must keep its higher score and selection,
        # and auto_switch must strictly target beta (never untouched alpha).
        auto_targets = []
        fake_group = ProxyGroup("Proxy", "alpha", ("alpha", "beta"))
        fake_settings = Settings("pipe", r"\\.\pipe\synthetic", "", ("v", 1, 1))
        with self.app.lock:
            self.app.capture = (self.app.registry, self.app.snapshots["u1"], fake_settings, "id-1", [fake_group])
            self.app.groups = [{"name": "Proxy", "now": "alpha"}]

        class RetestRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback, on_progress=None):
                for job in self.jobs:
                    if on_progress:
                        on_progress(job, ProgressUpdate("download", 10 * 1_048_576, job.download_bytes, 6 * 1_048_576))
                        on_progress(job, ProgressUpdate("download", 20 * 1_048_576, job.download_bytes, 8 * 1_048_576))
                    m = VerifiedMeasurement(job.name, "ss", 6, 6, 30, 2, job.download_bytes, job.download_bytes, True, 8 * 1_048_576)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        with patch("Windows.desktop.MultiBatchRunner", RetestRunner), \
             patch.object(self.app, "refresh_groups", return_value={"ok": True}), \
             patch.object(self.app, "_automatic", side_effect=lambda w, g, c, gen: auto_targets.append((w.name, g))):
            self.assertTrue(self.app.retest_node({"uid": "u1", "name": "beta", "auto_switch": True, "group": "Proxy"})["ok"])
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

        st2 = self.app.get_state()
        alpha2 = next(r for r in st2["rows"] if r["name"] == "alpha")
        beta2 = next(r for r in st2["rows"] if r["name"] == "beta")
        self.assertTrue(alpha2["selected"] and beta2["selected"])
        self.assertEqual(alpha2["score"], alpha1["score"])
        self.assertEqual(alpha2["samples"], alpha1["samples"])
        self.assertGreater(beta2["score"], beta1["score"])
        self.assertEqual(beta2["samples"], [6.0, 8.0])
        self.assertEqual(auto_targets, [("beta", "Proxy")])

    def test_safe_preferences_persistence_isolation_and_forbids_auto_switch(self):
        settings_file = self.root / ".telemetry" / "settings.json"
        res = self.app.save_preferences({
            "parallel": 4,
            "download_bytes": 5 * 1_048_576,
            "mode": "accurate",
            "interval": 30,
            "patrol_quota": 150 * 1_048_576,
            "top_n": 15,
        })
        self.assertTrue(res["ok"])
        self.assertTrue(settings_file.is_file())
        # Disallowed or invalid keys must be refused
        self.assertFalse(self.app.save_preferences({"auto_switch": True})["ok"])
        self.assertFalse(self.app.save_preferences({"schedule_armed": True})["ok"])
        self.assertFalse(self.app.save_preferences({"patrol_armed": True})["ok"])
        self.assertFalse(self.app.save_preferences({"parallel": True})["ok"])
        self.assertFalse(self.app.save_preferences({"parallel": 99})["ok"])
        self.assertFalse(self.app.save_preferences({"top_n": 0})["ok"])

        # Even if someone tampers auto_switch into settings.json, reopen ignores it and never arms schedule/patrol
        raw = json.loads(settings_file.read_text(encoding="utf-8"))
        raw["auto_switch"] = True
        raw["preferences"]["auto_switch"] = True
        settings_file.write_text(json.dumps(raw), encoding="utf-8")

        reopened = Desktop(self.root / "missing.exe", root=self.root)
        try:
            st = reopened.get_state()
            self.assertEqual(st["preferences"], {
                "parallel": 4,
                "download_bytes": 5 * 1_048_576,
                "mode": "accurate",
                "interval": 30,
                "patrol_quota": 150 * 1_048_576,
                "top_n": 15,
            })
            self.assertNotIn("auto_switch", st["preferences"])
            self.assertIsNone(st["schedule_minutes"])
            self.assertFalse(st["patrol"]["armed"])
            self.assertEqual(st["patrol"]["quota_bytes"], 150 * 1_048_576)
        finally:
            reopened.close()

    def test_manual_probe_active_node_and_tray_progress_best_summary(self):
        self.assertTrue(self.app.refresh()["ok"])
        # Without capture, manual probe fails cleanly
        self.assertFalse(self.app.probe_active_node({"group": "Proxy"})["ok"])

        from Windows.controller import ProxyGroup, Settings
        fake_group = ProxyGroup("Proxy", "alpha", ("alpha", "beta"))
        fake_settings = Settings("pipe", r"\\.\pipe\synthetic", "", ("v", 1, 1))
        with self.app.lock:
            self.app.capture = (self.app.registry, self.app.snapshots["u1"], fake_settings, "id-1", [fake_group])
            self.app.groups = [{"name": "Proxy", "now": "alpha"}]
            self.app.rows[0].measurement = VerifiedMeasurement("alpha", "ss", 6, 6, 35, 2, 1_048_576, 1_048_576, True, 12 * 1_048_576)

        started = threading.Event()
        release = threading.Event()

        class FakeCtrl:
            def __init__(self, _s): pass
            def group(self, _name): return fake_group
            def probe_node_delay(self, _node, timeout_ms=3500):
                started.set()
                release.wait(2)
                return 42.0

        with patch("Windows.desktop.PipeController", FakeCtrl):
            res = self.app.probe_active_node({"group": "Proxy"})
            self.assertTrue(res["ok"])
            self.assertTrue(started.wait(2))
            st_probing = self.app.get_state()
            self.assertEqual(st_probing["health_probe"]["level"], "probing")
            self.assertIn("探测中", st_probing["health_probe"]["text"])
            release.set()
            deadline = time.monotonic() + 2
            while self.app.get_state()["health_probe"]["level"] == "probing" and time.monotonic() < deadline:
                time.sleep(.01)
        st_done = self.app.get_state()
        self.assertEqual(st_done["health_probe"]["level"], "ok")
        self.assertIn("42ms", st_done["health_probe"]["text"])

        # Tray snapshot carries best route summary and running progress pct
        with self.app.lock:
            self.app.running = True
            self.app.run_targets = {("u1", "alpha"), ("u1", "beta")}
            self.app.run_total = 2
            self.app.run_done = 1
            self.app.live_progress[("u1", "beta")] = {"phase": "download", "received_bytes": 524_288, "expected_bytes": 1_048_576, "bps": 4_000_000, "samples": [4.0]}
        snap = self.app.get_tray_snapshot()
        self.assertIn("测速中 (1/2 · 75%)", snap["primary_status"])
        self.assertIn("最优：alpha · 12.00 MiB/s", snap["best_summary"])
        model = build_tray_menu_model(snap)
        labels = [it["label"] for it in model["items"] if not it["separator"]]
        self.assertIn(snap["best_summary"], labels)
        with self.app.lock:
            self.app.running = False

    def test_copy_node_summary_validates_catalogue_and_formats_live_and_history(self):
        self.assertTrue(self.app.refresh()["ok"])
        with self.app.lock:
            self.app.rows[0].node_type = "vless"
            self.app.rows[0].measurement = VerifiedMeasurement("alpha", "vless", 6, 6, 38, 3, 3_145_728, 3_145_728, True, 15 * 1_048_576)
        copied = []
        with patch("Windows.desktop.win32clipboard.OpenClipboard"), \
             patch("Windows.desktop.win32clipboard.EmptyClipboard"), \
             patch("Windows.desktop.win32clipboard.SetClipboardText", side_effect=lambda txt, _fmt: copied.append(txt)), \
             patch("Windows.desktop.win32clipboard.CloseClipboard"):
            res = self.app.copy_node_summary({"uid": "u1", "name": "alpha", "view": "live"})
            self.assertTrue(res["ok"])
            self.assertIn("[demo] alpha (VLESS)", res["text"])
            self.assertIn("15.00 MiB/s", res["text"])
            self.assertEqual(copied[-1], res["text"])
            self.assertFalse(self.app.copy_node_summary({"uid": "u1", "name": "ghost", "view": "live"})["ok"])

    def test_tray_settings_persistence_and_isolation(self):
        settings_file = self.root / ".telemetry" / "settings.json"
        self.assertIs(self.app.get_state()["minimize_to_tray"], True)
        self.assertTrue(self.app.set_minimize_to_tray({"enabled": False})["ok"])
        self.assertTrue(settings_file.is_file())
        self.assertIs(self.app.get_state()["minimize_to_tray"], False)

        reopened = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertIs(reopened.get_state()["minimize_to_tray"], False)
            self.assertFalse(reopened.set_minimize_to_tray({"enabled": "yes"})["ok"])
            # Corrupt JSON safely falls back to True
            settings_file.write_text("{corrupt", encoding="utf-8")
            self.assertIs(AppSettingsStore(directory=self.root / ".telemetry").load_minimize_to_tray(), True)
        finally:
            reopened.close()

    def test_window_closing_minimizes_to_tray_keeps_background_and_quits_when_disabled(self):
        events = []
        self.app._window_hide_cb = lambda: events.append("hide")
        self.app._window_show_cb = lambda: events.append("show")
        self.app._window_destroy_cb = lambda: events.append("destroy")

        # With minimize_to_tray=True, closing hides to tray and keeps schedule/background alive
        self.assertTrue(self.app.refresh()["ok"])
        opts = {"parallel": 1, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""}
        self.assertTrue(self.app.set_schedule({"minutes": 15, "options": opts})["ok"])
        self.assertFalse(self.app.handle_window_closing())
        self.assertEqual(events, ["hide"])
        self.assertFalse(self.app.closed.is_set())
        self.assertFalse(self.app.get_state()["window_visible"])
        self.assertEqual(self.app.get_state()["schedule_minutes"], 15)

        snap_hidden = self.app.get_tray_snapshot()
        self.assertEqual(snap_hidden["primary_status"], "定时已启用")
        menu_hidden = build_tray_menu_model(snap_hidden)
        by_id = {it["id"]: it for it in menu_hidden["items"] if it["id"] is not None}
        self.assertEqual(by_id[CMD_SHOW_HIDE]["label"], "显示主窗口")
        self.assertTrue(by_id[CMD_TOGGLE_MINIMIZE]["checked"])
        self.assertFalse(by_id[CMD_STOP_RUN]["enabled"])
        self.assertTrue(by_id[CMD_STOP_SCHEDULE]["enabled"])
        self.assertFalse(by_id[CMD_STOP_PATROL]["enabled"])

        # Disabling minimize_to_tray lets idle close proceed cleanly
        self.assertTrue(self.app.set_minimize_to_tray({"enabled": False})["ok"])
        self.assertTrue(self.app.handle_window_closing())
        self.assertTrue(self.app.closed.is_set())

    def test_busy_exit_and_close_wake_window_stop_runner_and_preserve_outcome_unknown(self):
        events = []
        self.app._window_hide_cb = lambda: events.append("hide")
        self.app._window_show_cb = lambda: events.append("show")
        self.app._window_destroy_cb = lambda: events.append("destroy")
        self.assertTrue(self.app.refresh()["ok"])
        executable = self.root / "fixture.exe"
        executable.touch()
        self.app.executable = executable

        entered = threading.Event()
        release = threading.Event()

        class BlockingRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, _callback, on_progress=None):
                entered.set()
                release.wait(3)

            def cancel(self):
                self.cancelled.set()
                release.set()

        with patch("Windows.desktop.MultiBatchRunner", BlockingRunner):
            self.assertTrue(self.app.start({"parallel": 1, "download_bytes": 262144,
                                            "mode": "fast", "auto_switch": False, "group": ""})["ok"])
            self.assertTrue(entered.wait(3))
            self.app.hide_to_tray()
            res = self.app.request_exit()
            self.assertFalse(res["ok"])
            self.assertTrue(res["busy"])
            self.assertIn("show", events)
            self.assertNotIn("destroy", events)
            self.assertFalse(self.app.closed.is_set())
            deadline = time.monotonic() + 3
            while self.app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)

        # Switching in flight also blocks request_exit and handle_window_closing
        self.assertTrue(self.app.set_minimize_to_tray({"enabled": False})["ok"])
        with self.app.lock:
            self.app._switching_count = 1
            self.app.latch.outcome_unknown = True
            self.app.status = "切换结果未知，请手动核验；切换已锁定"
        self.assertFalse(self.app.handle_window_closing())
        self.assertFalse(self.app.closed.is_set())
        self.assertEqual(self.app.get_state()["status"], "切换结果未知，请手动核验；切换已锁定")
        with self.app.lock:
            self.app._switching_count = 0

        # Once idle, request_exit quits cleanly and invokes destroy callback
        res_idle = self.app.request_exit()
        self.assertTrue(res_idle["ok"])
        self.assertTrue(self.app.closed.is_set())
        self.assertIn("destroy", events)

    def test_single_instance_guard_wakes_primary_and_isolates_distinct_roots(self):
        root_a = self.root / "profile-a"
        root_b = self.root / "profile-b"
        g1 = SingleInstanceGuard(profile_root=root_a)
        g2 = SingleInstanceGuard(profile_root=root_a)
        g_other = SingleInstanceGuard(profile_root=root_b)
        woken = threading.Event()
        try:
            self.assertTrue(g1.acquire())
            g1.start_listener(woken.set)
            self.assertFalse(g2.acquire())
            self.assertTrue(woken.wait(2.0))
            self.assertTrue(g_other.acquire())
        finally:
            g_other.release()
            g1.release()

    def test_tray_menu_model_status_summary_and_tooltip_limit(self):
        snap = {
            "window_visible": False,
            "minimize_to_tray": True,
            "running": False,
            "schedule_minutes": None,
            "patrol_armed": True,
            "primary_status": "智能巡检中",
            "detail_summary": "● 活跃正常 34ms · 今日巡检 12/80 MiB",
            "tooltip": "X" * 160,
        }
        model = build_tray_menu_model(snap)
        self.assertLessEqual(len(model["tooltip"]), 120)
        self.assertEqual(model["items"][0]["label"], "状态：智能巡检中")
        self.assertFalse(model["items"][0]["enabled"])
        self.assertEqual(model["items"][1]["label"], "● 活跃正常 34ms · 今日巡检 12/80 MiB")
        by_id = {it["id"]: it for it in model["items"] if it["id"] is not None}
        self.assertEqual(by_id[CMD_SHOW_HIDE]["label"], "显示主窗口")
        self.assertFalse(by_id[CMD_STOP_RUN]["enabled"])
        self.assertFalse(by_id[CMD_STOP_SCHEDULE]["enabled"])
        self.assertTrue(by_id[CMD_STOP_PATROL]["enabled"])
        self.assertTrue(by_id[CMD_EXIT]["enabled"])

    def test_initial_refresh_selects_only_current_profile_and_history_export_tsv(self):
        (self.root / "profiles" / "two.yaml").write_text(
            "proxies:\n- {name: gamma, type: vless}\n", encoding="utf-8"
        )
        (self.root / "profiles.yaml").write_text(
            "current: u2\nitems:\n"
            "- {uid: u1, name: first, type: local, file: one.yaml}\n"
            "- {uid: u2, name: second, type: remote, file: two.yaml}\n",
            encoding="utf-8",
        )
        self.assertTrue(self.app.refresh()["ok"])
        st = self.app.get_state()
        self.assertEqual([(p["uid"], p["selected"]) for p in st["profiles"]], [("u1", False), ("u2", True)])

        all_nodes = [{"uid": r["uid"], "name": r["name"]} for r in st["rows"]]
        self.assertTrue(self.app.set_selection({"profiles": ["u1", "u2"], "nodes": all_nodes})["ok"])
        self.assertTrue(self.app.refresh()["ok"])
        st2 = self.app.get_state()
        self.assertEqual([(p["uid"], p["selected"]) for p in st2["profiles"]], [("u1", True), ("u2", True)])

        from Windows.telemetry_history import TelemetrySampleRecord
        now = time.time()
        for i in range(3):
            self.app.telemetry_store.record_sample(TelemetrySampleRecord(
                timestamp=now - i * 10, profile_uid="u2", profile_name="=second",
                node_name="gamma", node_type="vless", source="manual",
                requested_bytes=3 * 1_048_576, streams=2, download_complete=True,
                download_bytes_per_second=12 * 1_048_576, latency_ms=35.0, jitter_ms=2.0,
                head_attempts=6, head_successes=6, composite_score=91.5,
            ))
        text, filename, is_hist = self.app._build_tsv_export({"view": "history", "window": "7d"})
        self.assertTrue(is_hist)
        self.assertEqual(filename, "clash-history-results.tsv")
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("排名\t状态\t订阅\t节点\t类型"))
        self.assertEqual(len(lines), 2)
        self.assertIn("'=second", lines[1])
        self.assertIn("gamma", lines[1])
        self.assertIn("已认证", lines[1])

if __name__ == "__main__":
    unittest.main()
