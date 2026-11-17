"""Windows job-object runner tests with a synthetic executable, no network."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if os.name == "nt":
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import Windows.batch as batch
    from Windows.batch import Job, MultiBatchRunner


SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
class Fixture {
  static int Main(string[] args) {
    if (args.Length > 0 && args[0] == "--child") { Thread.Sleep(30000); return 0; }
    string input = Console.In.ReadToEnd();
    if (input.StartsWith("child:")) {
      string file = input.Substring(6);
      var p = Process.Start(new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "--child") {
        UseShellExecute = false, CreateNoWindow = true
      });
      File.WriteAllText(file, p.Id.ToString());
      Thread.Sleep(30000);
      return 0;
    }
    if (input == "wait") { Thread.Sleep(30000); return 0; }
    if (input.StartsWith("slow:")) {
      File.WriteAllText(input.Substring(5), "started");
      Thread.Sleep(6500);
    }
    if (input.StartsWith("fast:")) {
      string[] markers = input.Substring(5).Split('|');
      while (!File.Exists(markers[0])) Thread.Sleep(10);
      Console.WriteLine("{\"version\":1,\"node_name\":\"A\",\"node_type\":\"Synthetic\",\"head_attempts\":6,\"head_successes\":5,\"latency_ms\":12,\"jitter_ms\":1,\"expected_bytes\":100,\"received_bytes\":100,\"download_complete\":true,\"download_bytes_per_second\":200}");
      Console.Out.Flush();
      File.WriteAllText(markers[1], "output complete");
      return 0;
    }
    Console.WriteLine("{\"version\":1,\"node_name\":\"A\",\"node_type\":\"Synthetic\",\"head_attempts\":6,\"head_successes\":5,\"latency_ms\":12,\"jitter_ms\":1,\"expected_bytes\":100,\"received_bytes\":100,\"download_complete\":true,\"download_bytes_per_second\":200}");
    return 0;
  }
}
'''


@unittest.skipUnless(os.name == "nt", "Windows process job objects")
class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not csc.exists():
            raise unittest.SkipTest("Framework C# compiler unavailable for synthetic worker")
        cls.tmp = tempfile.TemporaryDirectory(prefix="clash-batch-synthetic-")
        cls.root = Path(cls.tmp.name)
        source = cls.root / "fixture.cs"
        source.write_text(SOURCE, encoding="utf-8")
        cls.exe = cls.root / "fixture.exe"
        result = subprocess.run([str(csc), "/nologo", f"/out:{cls.exe}", str(source)], capture_output=True)
        if result.returncode:
            cls.tmp.cleanup()
            raise RuntimeError("could not build synthetic worker")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def job(self, data):
        return Job("profile", "A", data, 100, 2, "1s", "1s", "http://127.0.0.1:1")

    def test_success_and_output_validation(self):
        results = []
        runner = MultiBatchRunner(self.exe, [self.job(b"synthetic") for _ in range(3)], 2, watchdog=5)
        runner.run(lambda job, status, value: results.append((status, value)))
        self.assertEqual([status for status, _ in results], ["ok"] * 3)
        self.assertTrue(all(value.node_name == "A" and value.score is not None for _, value in results))

    def test_timeout(self):
        results = []
        runner = MultiBatchRunner(self.exe, [self.job(b"wait")], 1, watchdog=.2)
        runner.run(lambda job, status, value: results.append(status))
        self.assertEqual(results, ["timeout"])

    def test_fast_result_and_unrelated_handle_eof_do_not_wait_for_slow_worker(self):
        slow_started = self.root / "slow-started.txt"
        fast_written = self.root / "fast-written.txt"
        for marker in (slow_started, fast_written):
            if marker.exists():
                marker.unlink()
        fast = Job("fast", "A", ("fast:" + str(slow_started) + "|" + str(fast_written)).encode(),
                   100, 2, "1s", "1s")
        slow = Job("slow", "A", ("slow:" + str(slow_started)).encode(), 100, 2, "1s", "1s")
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.bInheritHandle = True
        sentinel_r, sentinel_w = win32pipe.CreatePipe(security, 0)
        win32api.SetHandleInformation(sentinel_r, win32con.HANDLE_FLAG_INHERIT, 0)
        eof = threading.Event()
        results = {}
        fast_result = threading.Event()
        slow_result = threading.Event()
        launch_barrier = threading.Barrier(2)
        def synchronize_launch(_job):
            launch_barrier.wait(5)
            return True
        runner = MultiBatchRunner(self.exe, [fast, slow], 2, watchdog=12,
                                  prelaunch=synchronize_launch)

        def collect(job, status, value):
            results[job.uid] = (status, value, time.monotonic())
            (fast_result if job.uid == "fast" else slow_result).set()

        def read_sentinel():
            try:
                win32file.ReadFile(sentinel_r, 1)
            except pywintypes.error as exc:
                if exc.winerror != 109:  # ERROR_BROKEN_PIPE signals EOF.
                    return
            eof.set()

        worker = threading.Thread(target=runner.run, args=(collect,))
        reader = threading.Thread(target=read_sentinel, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not slow_started.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(slow_started.exists(), "slow worker did not start")
            sentinel_w.Close()
            reader.start()
            self.assertTrue(eof.wait(1.5), "slow worker inherited an unrelated pipe writer")
            deadline = time.monotonic() + 3
            while not fast_written.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(fast_written.exists(), "fast worker did not produce output")
            self.assertTrue(fast_result.wait(1.5), "fast result waited for unrelated pipe EOF")
            self.assertFalse(slow_result.is_set(), "fast result waited for slow worker")
            status, measurement, _ = results["fast"]
            self.assertEqual(status, "ok")
            self.assertEqual(measurement.node_name, "A")
            self.assertIsNotNone(measurement.score)
            self.assertTrue(slow_result.wait(9), "slow worker did not finish")
            self.assertEqual(results["slow"][0], "ok")
            self.assertLess(results["fast"][2], results["slow"][2])
        finally:
            try:
                sentinel_w.Close()
            except Exception:
                pass
            if worker.is_alive():
                runner.cancel()
            worker.join(5)
            if reader.ident is not None:
                reader.join(3)
            sentinel_r.Close()

    def test_failed_pipe_or_process_creation_closes_handles_and_unlocks_spawn(self):
        original_pipe = batch._pipe
        for failure in ("pipe", "process"):
            handles = []
            def create_pipe():
                if failure == "pipe" and handles:
                    raise OSError("synthetic pipe failure")
                pair = original_pipe()
                handles.extend(pair)
                return pair
            results = []
            with self.subTest(failure=failure), patch.object(batch, "_pipe", side_effect=create_pipe):
                with patch.object(batch._winapi, "CreateProcess", side_effect=OSError("synthetic spawn failure")):
                    MultiBatchRunner(self.exe, [self.job(b"synthetic")], 1).run(
                        lambda job, status, value: results.append(status))
                self.assertEqual(results, ["worker_failed"])
                self.assertEqual(len(handles), 2 if failure == "pipe" else 6)
                self.assertTrue(all(int(handle) == 0 for handle in handles))
            recovered = []
            MultiBatchRunner(self.exe, [self.job(b"synthetic")], 1).run(
                lambda job, status, value: recovered.append(status))
            self.assertEqual(recovered, ["ok"])

    def test_assignment_failure_terminates_suspended_process(self):
        observed = []
        results = []
        def fail_assignment(_job, process):
            observed.append(win32api.DuplicateHandle(win32api.GetCurrentProcess(), process,
                            win32api.GetCurrentProcess(), win32con.SYNCHRONIZE, False, 0))
            raise OSError("synthetic job assignment failure")
        with patch.object(win32job, "AssignProcessToJobObject", side_effect=fail_assignment):
            MultiBatchRunner(self.exe, [self.job(b"synthetic")], 1).run(
                lambda job, status, value: results.append(status))
        self.assertEqual(results, ["worker_failed"])
        self.assertEqual(len(observed), 1)
        try:
            self.assertEqual(win32event.WaitForSingleObject(observed[0], 3000), win32event.WAIT_OBJECT_0)
        finally:
            observed[0].Close()

    def test_cancel_terminates_descendant(self):
        marker = self.root / "child-pid.txt"
        results = []
        runner = MultiBatchRunner(self.exe, [self.job(("child:" + str(marker)).encode())], 1, watchdog=10)
        thread = threading.Thread(target=runner.run, args=(lambda job, status, value: results.append(status),))
        thread.start()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists(), "synthetic child did not launch")
        pid = int(marker.read_text())
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        try:
            runner.cancel()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(win32event.WaitForSingleObject(handle, 3000), win32event.WAIT_OBJECT_0)
            self.assertEqual(results, ["cancelled"])
        finally:
            handle.Close()


if __name__ == "__main__":
    unittest.main()
