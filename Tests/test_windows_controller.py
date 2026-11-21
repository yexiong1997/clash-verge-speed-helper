"""Synthetic Named Pipe HTTP tests; no installed Clash instance is contacted."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    import pywintypes
    import win32con
    import win32file
    import win32pipe
    from Windows.controller import (AssistedSwitch, ControllerError, PipeController, ProbeError,
                                    Settings, SwitchLatch, _IO_UNRESOLVED, read_settings)
    from Windows.desktop import Desktop
    from Windows.measurement import VerifiedMeasurement
    from Windows.profile_registry import load_registry, snapshot_profile


@unittest.skipUnless(os.name == "nt", "Windows Named Pipes")
class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-pipe-synthetic-")
        self.root = Path(self.tmp.name)
        (self.root / "profiles").mkdir()
        (self.root / "profiles.yaml").write_text(
            "current: uid1\nitems:\n- {uid: uid1, name: Test, type: local, file: test.yaml}\n", encoding="utf-8")
        (self.root / "profiles" / "test.yaml").write_text(
            "proxies:\n- {name: A}\n- {name: B}\n", encoding="utf-8")
        self.pipe = "\\\\.\\pipe\\clash-helper-test-" + uuid.uuid4().hex
        (self.root / "clash-verge.yaml").write_text(
            "external-controller-pipe: '" + self.pipe + "'\nsecret: synthetic-secret\n", encoding="utf-8")
        self.now = "A"
        self.puts = 0
        self.bad_put_status = False
        self.custom_proxies = None
        self.delay_handler = None
        self.stall = False
        self.release_stall = threading.Event()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.server, daemon=True)
        self.thread.start()
        self.assertTrue(self.ready.wait(5))

    def tearDown(self):
        self.stop.set()
        self.release_stall.set()
        try:
            handle = win32file.CreateFile(self.pipe, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                          0, None, win32con.OPEN_EXISTING, 0, None)
            handle.Close()
        except pywintypes.error:
            pass
        self.thread.join(2)
        self.tmp.cleanup()

    def server(self):
        def create_pipe():
            return win32pipe.CreateNamedPipe(self.pipe, win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 1000, None)
        pipe = create_pipe()
        self.ready.set()
        while not self.stop.is_set():
            next_pipe = None
            try:
                try:
                    win32pipe.ConnectNamedPipe(pipe, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:
                        raise
                if self.stop.is_set():
                    continue
                next_pipe = create_pipe()
                data = b""
                while b"\r\n\r\n" not in data:
                    data += win32file.ReadFile(pipe, 65536)[1]
                header, body = data.split(b"\r\n\r\n", 1)
                lines = header.split(b"\r\n")
                method, target, _ = lines[0].split(b" ")
                length = next((int(line.split(b":", 1)[1]) for line in lines if line.lower().startswith(b"content-length:")), 0)
                while len(body) < length:
                    body += win32file.ReadFile(pipe, 65536)[1]
                if self.stall:
                    self.release_stall.wait(4)
                    continue
                if b"Authorization: Bearer synthetic-secret" not in lines:
                    status, response = 401, b""
                elif method == b"PUT":
                    self.puts += 1
                    self.now = json.loads(body)["name"]
                    status, response = (500 if self.bad_put_status else 204), b""
                else:
                    group = {"name": "Test", "type": "Selector", "now": self.now, "all": ["A", "B"]}
                    if b"/delay?" in target and self.delay_handler is not None:
                        status, response = self.delay_handler(target)
                    elif target == b"/proxies":
                        obj = {"proxies": self.custom_proxies if self.custom_proxies is not None else {"Test": group}}
                        status, response = 200, json.dumps(obj).encode()
                    else:
                        obj = group
                        status, response = 200, json.dumps(obj).encode()
                win32file.WriteFile(pipe, f"HTTP/1.1 {status} OK\r\nContent-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode() + response)
                win32file.FlushFileBuffers(pipe)
            except pywintypes.error:
                pass
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(pipe)
                except pywintypes.error:
                    pass
                pipe.Close()
                pipe = next_pipe

    def prepared(self):
        registry = load_registry(self.root)
        snapshot = snapshot_profile(registry, registry.profiles[0])
        coordinator = AssistedSwitch(registry, snapshot, "B", latch=SwitchLatch())
        return coordinator

    def test_pipe_get_put_and_verify(self):
        coordinator = self.prepared()
        self.assertEqual([group.name for group in coordinator.prepare()], ["Test"])
        self.assertEqual(coordinator.apply("Test", lambda: True), "switched")
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.now, "B")

    def test_expected_current_change_blocks_write(self):
        coordinator = self.prepared()
        coordinator.prepare()
        self.now = "B"
        self.assertEqual(coordinator.apply("Test", lambda: True), "not_attempted")
        self.assertEqual(self.puts, 0)

    def test_uncertain_result_locks(self):
        coordinator = self.prepared()
        coordinator.prepare()
        self.bad_put_status = True
        self.assertEqual(coordinator.apply("Test", lambda: True), "outcome_unknown")
        with self.assertRaises(ControllerError):
            coordinator.prepare()
        replacement = AssistedSwitch(coordinator.registry, coordinator.snapshot, "B", latch=coordinator.latch)
        with self.assertRaises(ControllerError):
            replacement.prepare()
        self.assertEqual(coordinator.apply("Test", lambda: True), "previous_outcome_unknown")
        self.assertEqual(self.puts, 1)

    def test_changed_snapshot_blocks_write(self):
        coordinator = self.prepared()
        coordinator.prepare()
        (self.root / "profiles" / "test.yaml").write_text("proxies: []\n", encoding="utf-8")
        self.assertEqual(coordinator.apply("Test", lambda: True), "not_attempted")
        self.assertEqual(self.puts, 0)

    def test_pending_pipe_read_times_out_and_cancels(self):
        self.stall = True
        controller = PipeController(read_settings(load_registry(self.root)), timeout=.2)
        start = time.monotonic()
        try:
            with self.assertRaises(ControllerError):
                controller.groups(["B"])
            self.assertLess(time.monotonic() - start, 3)
            self.assertFalse(_IO_UNRESOLVED.is_set())
        finally:
            self.release_stall.set()

    def test_cross_subscription_continuation_needs_fresh_consent(self):
        registry = self.root / "profiles.yaml"
        entries = ("items:\n- {uid: uid1, name: Test, type: local, file: test.yaml}\n"
                   "- {uid: uid2, name: Other, type: local, file: other.yaml}\n")
        (self.root / "profiles" / "other.yaml").write_text("proxies:\n- {name: other}\n", encoding="utf-8")
        registry.write_text("current: uid2\n" + entries, encoding="utf-8")
        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            measurement = VerifiedMeasurement("B", "Synthetic", 6, 6, 10, 0, 100, 100, True, 200)
            app.pending = ("uid1", "B", measurement, app.registry, dict(app.snapshots))
            self.assertFalse(app.get_state()["pending"]["ready"])
            registry.write_text("current: uid1\n" + entries, encoding="utf-8")
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.get_state()["pending"]["ready"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertTrue(app.continue_pending({"group": "Test"})["ok"])
            self.assertEqual(self.now, "B")
            self.assertEqual(self.puts, 1)
            self.assertIsNone(app.get_state()["pending"])
        finally:
            app.close()

    def test_undo_switch_requires_fresh_state_and_restores_previous_direct_member(self):
        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertTrue(app.switch_manual({"uid": "uid1", "name": "B", "group": "Test"})["ok"])
            self.assertEqual((self.now, self.puts), ("B", 1))
            self.assertEqual(app.get_state()["undo"], {"group": "Test", "name": "A"})
            self.assertTrue(app.undo_switch()["ok"])
            self.assertEqual((self.now, self.puts), ("A", 2))
            self.assertIsNone(app.get_state()["undo"])
        finally:
            app.close()

    def test_undo_does_not_overwrite_an_independent_change(self):
        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertTrue(app.switch_manual({"uid": "uid1", "name": "B", "group": "Test"})["ok"])
            self.now = "other"
            self.assertFalse(app.undo_switch()["ok"])
            self.assertEqual((self.now, self.puts), ("other", 1))
        finally:
            app.close()

    def test_retest_revalidates_group_after_selection_and_launches_with_auto_enabled(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        app = Desktop(worker, root=self.root)
        entered = threading.Event()
        release = threading.Event()
        runners = []

        class FixtureRunner:
            def __init__(self, _exe, jobs, parallel, **_kwargs):
                self.jobs, self.parallel = tuple(jobs), parallel
                self.cancelled = threading.Event()
                runners.append(self)

            def run(self, _callback):
                entered.set()
                release.wait(3)

            def cancel(self):
                self.cancelled.set()
                release.set()

        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            previous = app.capture
            with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
                result = app.retest_node({"uid": "uid1", "name": "B",
                                          "auto_switch": True, "group": "Test"})
                self.assertTrue(result["ok"], result)
                self.assertTrue(entered.wait(3))
                self.assertTrue(app.running)
                self.assertIsNotNone(app.capture)
                self.assertEqual(app.capture[:4], previous[:4])
                self.assertEqual([(job.uid, job.name, job.download_bytes, job.streams)
                                  for job in runners[0].jobs], [("uid1", "B", 20 * 1048576, 4)])
                self.assertEqual(runners[0].parallel, 1)
                self.assertEqual(self.puts, 0)
                app.stop()
                deadline = time.monotonic() + 3
                while app.running and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(app.running)
        finally:
            release.set()
            app.close()

    def test_retest_refuses_changed_group_without_starting_or_writing(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        app = Desktop(worker, root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.now = "B"
            result = app.retest_node({"uid": "uid1", "name": "B",
                                      "auto_switch": True, "group": "Test"})
            self.assertFalse(result["ok"])
            self.assertFalse(app.running)
            self.assertEqual(self.puts, 0)
        finally:
            app.close()

    def test_retest_rechecks_selected_group_even_when_auto_is_off(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        app = Desktop(worker, root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.now = "B"
            result = app.retest_node({"uid": "uid1", "name": "B",
                                      "auto_switch": False, "group": "Test"})
            self.assertFalse(result["ok"])
            self.assertFalse(app.running)
            self.assertEqual(self.puts, 0)
        finally:
            app.close()

    def test_retest_disarms_schedule_even_when_selection_is_already_single(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        app = Desktop(worker, root=self.root)
        release = threading.Event()
        entered = threading.Event()

        class FixtureRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, _callback):
                entered.set()
                release.wait(3)

            def cancel(self):
                self.cancelled.set()
                release.set()

        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.set_selection({"profiles": ["uid1"],
                                               "nodes": [{"uid": "uid1", "name": "B"}]})["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            options = {"parallel": 1, "download_bytes": 20 * 1048576, "mode": "accurate",
                       "auto_switch": True, "group": "Test"}
            self.assertTrue(app.set_schedule({"minutes": 15, "options": options})["ok"])
            with patch("Windows.desktop.MultiBatchRunner", FixtureRunner):
                result = app.retest_node({"uid": "uid1", "name": "B",
                                          "auto_switch": True, "group": "Test"})
                self.assertTrue(result["ok"], result)
                self.assertTrue(entered.wait(3))
                self.assertIsNone(app.get_state()["schedule_minutes"])
                self.assertEqual(self.puts, 0)
                app.stop()
                deadline = time.monotonic() + 3
                while app.running and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(app.running)
        finally:
            release.set()
            app.close()

    def test_outcome_unknown_locks_writes_and_refresh_groups_while_allowing_pure_speedtest(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        app = Desktop(worker, root=self.root)
        runs = []

        class InstantRunner:
            def __init__(self, _exe, jobs, parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.parallel = parallel
                self.cancelled = threading.Event()
                runs.append((parallel, [j.name for j in self.jobs]))

            def run(self, callback):
                for job in self.jobs:
                    m = VerifiedMeasurement(job.name, "Synthetic", 6, 6, 10, 0,
                                            job.download_bytes, job.download_bytes, True, 2_000_000)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        def wait_idle():
            deadline = time.monotonic() + 3
            while app.get_state()["running"] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(app.get_state()["running"])

        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.bad_put_status = True
            switch_res = app.switch_manual({"uid": "uid1", "name": "B", "group": "Test"})
            self.assertFalse(switch_res["ok"])
            self.assertTrue(app.get_state()["outcome_unknown"])
            self.assertEqual(self.puts, 1)

            self.bad_put_status = False
            self.assertFalse(app.refresh_groups()["ok"])
            self.assertFalse(app.switch_manual({"uid": "uid1", "name": "B", "group": "Test"})["ok"])
            self.assertFalse(app.undo_switch()["ok"])
            self.assertFalse(app.continue_pending({"group": "Test"})["ok"])
            self.assertFalse(app.set_schedule({
                "minutes": 15,
                "options": {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": True, "group": "Test"},
            })["ok"])
            self.assertFalse(app.start({
                "parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": True, "group": "Test",
            })["ok"])
            self.assertFalse(app.start_funnel({
                "parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": True, "group": "Test", "top_count": 1,
            })["ok"])
            self.assertFalse(app.retest_node({"uid": "uid1", "name": "B", "auto_switch": False, "group": "Test"})["ok"])
            self.assertEqual(self.puts, 1)

            with patch("Windows.desktop.MultiBatchRunner", InstantRunner):
                self.assertTrue(app.start({
                    "parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": "",
                })["ok"])
                wait_idle()
                self.assertTrue(app.start_funnel({
                    "parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": "", "top_count": 1,
                })["ok"])
                wait_idle()
                self.assertTrue(app.retest_node({
                    "uid": "uid1", "name": "B", "auto_switch": False, "group": "",
                })["ok"])
                wait_idle()
            self.assertTrue(app.set_schedule({
                "minutes": 15,
                "options": {"parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": ""},
            })["ok"])
            self.assertEqual(self.puts, 1)
            self.assertGreaterEqual(len(runs), 4)
        finally:
            app.close()

    def test_pending_only_gates_continue_pending_and_clears_on_new_launch(self):
        worker = self.root / "fixture.exe"
        worker.touch()
        registry = self.root / "profiles.yaml"
        entries = ("items:\n- {uid: uid1, name: Test, type: local, file: test.yaml}\n"
                   "- {uid: uid2, name: Other, type: local, file: other.yaml}\n")
        (self.root / "profiles" / "other.yaml").write_text("proxies:\n- {name: other}\n", encoding="utf-8")
        registry.write_text("current: uid1\n" + entries, encoding="utf-8")
        app = Desktop(worker, root=self.root)

        class InstantRunner:
            def __init__(self, _exe, jobs, _parallel, **_kwargs):
                self.jobs = tuple(jobs)
                self.cancelled = threading.Event()

            def run(self, callback):
                for job in self.jobs:
                    m = VerifiedMeasurement(job.name, "Synthetic", 6, 6, 10, 0,
                                            job.download_bytes, job.download_bytes, True, 1_000_000)
                    callback(job, "ok", m)

            def cancel(self):
                self.cancelled.set()

        try:
            self.assertTrue(app.refresh()["ok"])
            measurement = VerifiedMeasurement("other", "Synthetic", 6, 6, 10, 0, 100, 100, True, 200)
            app.pending = ("uid2", "other", measurement, app.registry, dict(app.snapshots))
            self.assertFalse(app.get_state()["pending"]["ready"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertFalse(app.continue_pending({"group": "Test"})["ok"])
            self.assertEqual(self.puts, 0)

            self.assertTrue(app.switch_manual({"uid": "uid1", "name": "B", "group": "Test"})["ok"])
            self.assertEqual((self.now, self.puts), ("B", 1))
            self.assertIsNotNone(app.get_state()["pending"])

            with patch("Windows.desktop.MultiBatchRunner", InstantRunner):
                self.assertTrue(app.start({
                    "parallel": 2, "download_bytes": 262144, "mode": "fast", "auto_switch": False, "group": "",
                })["ok"])
                deadline = time.monotonic() + 3
                while app.get_state()["running"] and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertIsNone(app.get_state()["pending"])
        finally:
            app.close()

    def test_refresh_groups_suggests_unique_non_global_full_group(self):
        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.custom_proxies = {
                "GLOBAL": {"name": "GLOBAL", "type": "Selector", "now": "A", "all": ["A", "B"]},
                "Partial": {"name": "Partial", "type": "Selector", "now": "A", "all": ["A"]},
                "Main": {"name": "Main", "type": "Selector", "now": "A", "all": ["A", "B"]},
            }
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertEqual(app.get_state()["suggested_group"], "Main")

            self.custom_proxies["Main2"] = {"name": "Main2", "type": "Selector", "now": "A", "all": ["A", "B"]}
            self.assertTrue(app.refresh_groups()["ok"])
            self.assertIsNone(app.get_state()["suggested_group"])
        finally:
            app.close()

    def test_refresh_groups_preserves_tun_enabled_on_offline_pipe_and_resets_on_profile_error(self):
        verge_cfg = self.root / "clash-verge.yaml"
        offline_pipe = r"\\.\pipe\clash-helper-offline-" + uuid.uuid4().hex
        verge_cfg.write_text(
            f"external-controller-pipe: '{offline_pipe}'\nsecret: synthetic-secret\ntun:\n  enable: true\n",
            encoding="utf-8",
        )
        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertIs(app.get_state()["tun_enabled"], True)
            self.assertFalse(app.refresh_groups()["ok"])
            self.assertIs(app.get_state()["tun_enabled"], True)

            verge_cfg.unlink()
            verge_cfg.mkdir()
            self.assertFalse(app.refresh_groups()["ok"])
            self.assertIsNone(app.get_state()["tun_enabled"])
        finally:
            app.close()

    def test_probe_node_delay_and_desktop_health_probe_penalty_decay(self):
        registry = load_registry(self.root)
        settings = read_settings(registry)
        ctrl = PipeController(settings)
        self.delay_handler = lambda _target: (200, json.dumps({"delay": 142}).encode())
        self.assertEqual(ctrl.probe_node_delay("A", timeout_ms=2500), 142.0)

        self.delay_handler = lambda _target: (504, b'{"message":"timeout"}')
        with self.assertRaises(ProbeError):
            ctrl.probe_node_delay("A", timeout_ms=2500)

        self.delay_handler = lambda _target: (200, b'{"delay": -1}')
        with self.assertRaises(ProbeError):
            ctrl.probe_node_delay("A", timeout_ms=2500)

        with self.assertRaises(ControllerError):
            ctrl.probe_node_delay("A", timeout_ms=2500, test_url="ftp://invalid")

        app = Desktop(self.root / "not-used.exe", root=self.root)
        try:
            self.assertTrue(app.refresh()["ok"])
            self.assertTrue(app.refresh_groups()["ok"])
            if app._health_probe_thread is not None:
                app._health_probe_thread.join(timeout=2.0)
            app.telemetry_store.clear_all()
            self.delay_handler = lambda _target: (504, b"")
            app._run_health_probe()
            st1 = app.get_state()
            self.assertEqual(st1["health_probe"]["level"], "warn")
            app._run_health_probe()
            st2 = app.get_state()
            self.assertEqual(st2["health_probe"]["level"], "danger")
            self.assertIn("活跃异常", st2["health_probe"]["text"])
            self.delay_handler = lambda _target: (200, json.dumps({"delay": 88}).encode())
            app._run_health_probe()
            st3 = app.get_state()
            self.assertEqual(st3["health_probe"]["level"], "ok")
            self.assertIn("88ms", st3["health_probe"]["text"])
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
