#!/usr/bin/env python3
"""Purely local tests for MultiBatchRunner; no real worker, profile or network.

Run: python3 -B Tests/test_multi_batch.py
The compiler cache, synthetic Swift driver, fake worker, fixtures and event log
are all under one temporary directory. The fake worker never uses networking.
JSON snapshots are also valid YAML 1.2, and contain only synthetic data.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parent.parent
SOURCES = [PROJECT / "Sources" / name for name in
           ("MultiBatchRunner.swift", "VerifiedMeasurement.swift", "CompositeScore.swift")]

# The fake has no network code and reads its only configuration from /dev/stdin.
# It records process lifecycle events, but never snapshots or secrets, to a
# temporary file. Its protocol emulates the verified v1 JSON worker.
FAKE_WORKER = r'''
import json
import os
import re
import signal
import sys
import time
import unicodedata

log_path = os.environ["MULTIBATCH_EVENTS"]
def event(kind, detail=None):
    line = (json.dumps({"event": kind, "pid": os.getpid(), "detail": detail}) + "\n").encode()
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

def terminated(signum, frame):
    event("term")
    time.sleep(0.12)  # Make completion-before-termination observable.
    sys.exit(128 + signum)

def ignore_term(signum, frame):
    event("term-ignored")

signal.signal(signal.SIGTERM, terminated)
event("start")
try:
    argv = sys.argv[1:]
    if len(argv) != 16:
        sys.stderr.write("SYNTHETIC_SECRET_STDERR wrong argc\n")
        sys.exit(20)
    target = argv[-2]
    if target == "fixture://no-read":
        os.close(0)
        sys.stderr.write("SYNTHETIC_SECRET_STDERR no read\n")
        sys.exit(23)
    if target == "fixture://sleep-no-read":
        time.sleep(30)
        sys.exit(24)
    if target == "fixture://trap-no-read":
        signal.signal(signal.SIGTERM, ignore_term)
        event("trap-ready")
        time.sleep(30)
        sys.exit(24)
    if target == "fixture://flood":
        os.write(1, b"X" * (2 * 1024 * 1024))
    with open("/dev/stdin", "rb") as stream:
        raw = stream.read()
    config = json.loads(raw.decode("utf-8"))
    node = config["node"]
    regex = "\\A" + "".join(
        f"\\x{{{ord(ch):x}}}" if ord(ch) > 0x7f else ("\\" if ch in "\\.+*?()|[]{}^$" else "") + ch
        for ch in node) + "\\z"
    # Interpret the emitted RE2 scalar escapes with Python's equivalent Unicode
    # escapes, then check that canonically equivalent names remain distinct.
    python_pattern = re.sub(r"\\x\{([0-9a-f]+)\}",
                            lambda m: r"\U" + f"{int(m.group(1), 16):08x}", regex)
    python_pattern = python_pattern[:-2] + r"\Z"
    matcher = re.compile(python_pattern)
    alternatives = [unicodedata.normalize(form, node) for form in ("NFC", "NFD")]
    if not matcher.fullmatch(node) or any(alt != node and matcher.fullmatch(alt) for alt in alternatives):
        event("bad-regex")
        sys.exit(22)
    expected = ["-c", "/dev/stdin", "-f", regex,
                "-download-size", str(config["bytes"]), "-concurrent", str(config["streams"]),
                "-timeout", config["download_timeout"], "-max-latency", config["latency_timeout"],
                "-rename=false", "-server-url", config["target"], "-json-v1"]
    if argv != expected or any(config["secret"] in arg for arg in argv):
        sanitized = ["<redacted>" if config["secret"] in arg else arg for arg in argv]
        event("bad-argv", {"actual": sanitized, "expected": expected})
        sys.stderr.write("SYNTHETIC_SECRET_STDERR invalid argv\n")
        sys.exit(21)
    time.sleep(config.get("delay", 0))
    behavior = config.get("behavior", "normal")
    if behavior == "bad_utf8":
        os.write(1, b"\xff\xfe\n")
        sys.exit(0)
    expected_bytes = (config["bytes"] // config["streams"]) * config["streams"]
    record = {
        "version": 1,
        "node_name": config.get("output_name", node),
        "node_type": config["fixture_id"],
        "head_attempts": 6,
        "head_successes": 6,
        "latency_ms": 10,
        "jitter_ms": 2,
        "expected_bytes": expected_bytes,
        "received_bytes": expected_bytes,
        "download_complete": True,
        "download_bytes_per_second": 3 * 1024 * 1024,
    }
    if behavior == "extra_field":
        record["unexpected"] = "extra"
    elif behavior == "missing_field":
        del record["jitter_ms"]
    elif behavior == "wrong_size":
        record["expected_bytes"] += 1
        record["received_bytes"] += 1
    elif behavior == "wrong_received":
        record["received_bytes"] -= 1
    elif behavior == "wrong_type":
        record["head_successes"] = True
    elif behavior == "wrong_version":
        record["version"] = 2
    elif behavior == "incomplete":
        record["download_complete"] = False
        record["received_bytes"] -= 1
        record["download_bytes_per_second"] = 0
    elif behavior == "incomplete_with_speed":
        record["download_complete"] = False
    elif behavior == "few_heads":
        record["head_successes"] = 2
    elif behavior == "zero_latency":
        record["latency_ms"] = 0
    output = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if behavior == "malformed_json":
        output = "{not JSON}"
    elif behavior == "array":
        output = "[" + output + "]"
    elif behavior == "duplicate_field":
        output = output[:-1] + ',"version":1}'
    elif behavior == "multiple_inline":
        output += output
    elif behavior == "multiple_lines":
        output += "\n" + output
    elif behavior == "ansi":
        output += "\x1b[31m"
    if behavior != "no_newline":
        output += "\r\n" if behavior == "crlf" else "\n"
    if behavior == "extra_line":
        output += "synthetic unexpected line\n"
    elif behavior == "double_newline":
        output += "\n"
    os.write(1, output.encode("utf-8"))
    if behavior == "valid_then_trap":
        signal.signal(signal.SIGTERM, ignore_term)
        event("trap-ready")
        time.sleep(30)
    sys.stderr.write("SYNTHETIC_SECRET_STDERR must not leak\n")
    if behavior == "nonzero":
        sys.exit(42)
finally:
    event("end")
'''

SWIFT_DRIVER = r'''
import Foundation
import Dispatch
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

// A background cancellation thread only accesses this sendable wrapper.
final class RunnerBox: @unchecked Sendable { var runner: MultiBatchRunner? }
final class CompletionProbe: @unchecked Sendable {
    private let lock = NSLock()
    private var calls = [Int]()
    func mark(_ index: Int) { lock.lock(); calls.append(index); lock.unlock() }
    func snapshot() -> [Int] { lock.lock(); defer { lock.unlock() }; return calls }
}

let raw = FileHandle.standardInput.readDataToEndOfFile()
let input = try JSONSerialization.jsonObject(with: raw) as! [String: Any]
let jobInputs = input["jobs"] as! [[String: Any]]
let jobs = jobInputs.map { item -> MultiBatchJob in
    let profile = Data(base64Encoded: item["profile"] as! String)!
    let snapshot = Data(base64Encoded: item["snapshot"] as! String)!
    // Do not let a JSON/NSString bridge canonicalize a synthetic node name.
    let name = String(data: Data(base64Encoded: item["name_utf8"] as! String)!, encoding: .utf8)!
    let options = MultiBatchOptions(downloadBytes: item["bytes"] as! Int,
                                    streams: item["streams"] as! Int,
                                    latencyTimeout: item["latency_timeout"] as! String,
                                    downloadTimeout: item["download_timeout"] as! String,
                                    serverURL: item["server_url"] as? String)
    if item["external_mutable"] as? Bool == true {
        let a = UnsafeMutableRawPointer.allocate(byteCount: max(profile.count, 1), alignment: 1)
        let b = UnsafeMutableRawPointer.allocate(byteCount: max(snapshot.count, 1), alignment: 1)
        profile.withUnsafeBytes { if $0.count > 0 { a.copyMemory(from: $0.baseAddress!, byteCount: $0.count) } }
        snapshot.withUnsafeBytes { if $0.count > 0 { b.copyMemory(from: $0.baseAddress!, byteCount: $0.count) } }
        let profileView = Data(bytesNoCopy: a, count: profile.count, deallocator: .none)
        let snapshotView = Data(bytesNoCopy: b, count: snapshot.count, deallocator: .none)
        let job = MultiBatchJob(profileIdentity: profileView, nodeName: name,
                                yamlSnapshot: snapshotView, index: item["index"] as! Int, options: options)
        a.initializeMemory(as: UInt8.self, repeating: 88, count: profile.count)
        b.initializeMemory(as: UInt8.self, repeating: 88, count: snapshot.count)
        precondition(job.profileIdentity == profile && job.yamlSnapshot == snapshot, "snapshot aliased external storage")
        a.deallocate(); b.deallocate()
        return job
    }
    return MultiBatchJob(profileIdentity: profile, nodeName: name,
                         yamlSnapshot: snapshot, index: item["index"] as! Int, options: options)
}

// Do not let Python's inherited SIGPIPE disposition hide a faulty pipe writer.
_ = signal(SIGPIPE, SIG_DFL)
let box = RunnerBox()
let probe = CompletionProbe()
let asyncMode = input["async_prelaunch"] as? Bool == true
var inResult = false
var inPreLaunch = false
var finished = false
var events = [[String: Any]]()
var preLaunchCalls = [Int]()
var heldCompletions: [(Int, Bool, @Sendable (Bool) -> Void)] = []
var outstanding = 0
var peakOutstanding = 0
let syncHook: MultiBatchRunner.PreLaunch = { job in
    precondition(Thread.isMainThread && !inResult && !inPreLaunch && !finished, "pre-launch order/thread")
    preLaunchCalls.append(job.index)
    return job.index != input["reject_at_index"] as? Int
}
let asyncHook: MultiBatchRunner.AsyncPreLaunch = { job, completion in
    precondition(Thread.isMainThread && !inResult && !inPreLaunch && !finished, "async hook order/thread")
    inPreLaunch = true
    defer { inPreLaunch = false }
    preLaunchCalls.append(job.index)
    outstanding += 1
    peakOutstanding = max(peakOutstanding, outstanding)
    if job.index == input["cancel_in_async_hook"] as? Int { box.runner?.cancel() }
    let delays = input["async_delays"] as? [String: Int] ?? [:]
    let delay = delays[String(job.index)] ?? 0
    let accepted = job.index != input["reject_at_index"] as? Int
    let index = job.index
    let duplicate = input["async_duplicate"] as? Bool == true
    let background = input["async_background"] as? Bool == true
    let onMain = input["async_complete_on_main"] as? Bool == true
    let fanout = input["async_concurrent_duplicates"] as? Int ?? 0
    let compete = input["async_concurrent_opposites"] as? Bool == true
    if let count = input["async_reverse_initial"] as? Int {
        heldCompletions.append((index, accepted, completion))
        if heldCompletions.count == count {
            let captured = Array(heldCompletions.reversed())
            heldCompletions.removeAll()
            DispatchQueue.global().async {
                for (index, accepted, completion) in captured {
                    completion(accepted); probe.mark(index)
                    if duplicate { completion(!accepted); probe.mark(index) }
                }
            }
        }
    } else if input["async_hold_index"] as? Int == index {
        heldCompletions.append((index, accepted, completion))
    } else if input["async_wait_worker_start"] as? Int == index {
        // This synthetic synchronization observes only the fake worker's temp
        // log, so the test does not rely on how quickly a process starts.
        let eventPath = ProcessInfo.processInfo.environment["MULTIBATCH_EVENTS"]!
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(8)
            while Date() < deadline {
                if let contents = try? String(contentsOfFile: eventPath, encoding: .utf8),
                   contents.contains("\"event\": \"start\"") { break }
                Thread.sleep(forTimeInterval: 0.01)
            }
            if delay > 0 { Thread.sleep(forTimeInterval: Double(delay) / 1000) }
            completion(accepted); probe.mark(index)
            if duplicate { completion(!accepted); probe.mark(index) }
        }
    } else if fanout > 0 {
        for n in 0..<fanout {
            let decision = compete && n % 2 == 1 ? !accepted : accepted
            DispatchQueue.global().async { completion(decision); probe.mark(index) }
        }
    } else if delay >= 0 {
        if delay == 0 && !background && !onMain {
            completion(accepted); probe.mark(index)
            if duplicate { completion(!accepted); probe.mark(index) }
        } else if onMain {
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(delay)) {
                completion(accepted); probe.mark(index)
                if duplicate { completion(!accepted); probe.mark(index) }
            }
        } else {
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(delay)) {
                completion(accepted); probe.mark(index)
                if duplicate { completion(!accepted); probe.mark(index) }
            }
        }
    }
    if job.index == input["nested_async_hook_index"] as? Int {
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
    }
}
let onResult: MultiBatchRunner.ResultCallback = { job, result in
    precondition(Thread.isMainThread && !inResult && !inPreLaunch && !finished, "result callback order/thread")
    if asyncMode { precondition(outstanding > 0); outstanding -= 1 }
    inResult = true
    var item: [String: Any] = ["event": "result", "index": job.index,
                               "profile": job.profileIdentity.base64EncodedString(),
                               "name_utf8": Data(job.nodeName.utf8).base64EncodedString()]
    switch result {
    case .success(let fields):
        item["status"] = "success"
        item["fields"] = fields
        if fields.count == 1,
           let measurement = VerifiedMeasurement.decode(Data(fields[0].utf8), expectedName: job.nodeName,
                                                        requestedBytes: job.options.downloadBytes,
                                                        streams: job.options.streams) {
            item["decoded"] = true
            if let score = measurement.score { item["score"] = score }
            else { item["score"] = NSNull() }
        } else {
            item["decoded"] = false
        }
    case .failure(let error): item["status"] = String(describing: error)
    case .cancelled: item["status"] = "cancelled"
    }
    events.append(item)
    if input["async_release_held_on_result"] as? Bool == true {
        let captured = heldCompletions
        heldCompletions.removeAll()
        for (index, accepted, completion) in captured { completion(accepted); probe.mark(index) }
    }
    if input["nested_loop"] as? Bool == true && events.count == 1 {
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
    }
    if input["cancel_on_result"] as? Bool == true { box.runner?.cancel() }
    inResult = false
}
let onFinish: MultiBatchRunner.FinishCallback = { summary in
    precondition(Thread.isMainThread && !inResult && !inPreLaunch && !finished, "finish before callback returned")
    finished = true
    events.append(["event": "finish", "cancelled": summary.cancelled,
                   "attempted": summary.attempted, "launched": summary.launched,
                   "peak": summary.peakWorkers, "peak_outstanding": peakOutstanding,
                   "pre_launch_calls": preLaunchCalls])
}
let executable = URL(fileURLWithPath: input["executable"] as! String)
let parallel = input["parallel"] as! Int
let watchdog = input["watchdog"] as? Double ?? 180
let grace = input["termination_grace"] as? Double ?? 2
let runner: MultiBatchRunner
if asyncMode {
    runner = MultiBatchRunner(executable: executable, jobs: jobs, parallel: parallel,
                              watchdog: watchdog, terminationGrace: grace,
                              asyncPreLaunch: asyncHook, onResult: onResult, onFinish: onFinish)
} else {
    runner = MultiBatchRunner(executable: executable, jobs: jobs, parallel: parallel,
                              watchdog: watchdog, terminationGrace: grace,
                              preLaunch: syncHook, onResult: onResult, onFinish: onFinish)
}
box.runner = runner
if input["cancel_on_main_turn"] as? Bool == true {
    DispatchQueue.main.async { box.runner?.cancel() }
}
if input["cancel_before_start"] as? Bool == true { runner.cancel(); runner.cancel() }
if input["start_in_background"] as? Bool == true {
    DispatchQueue.global().async { box.runner?.start(); box.runner?.start() }
} else {
    runner.start(); runner.start()
}
if let millis = input["cancel_after_ms"] as? Int {
    DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(millis)) { box.runner?.cancel() }
}
let deadline = Date().addingTimeInterval(25)
var readinessCancelled = false
let readyEvent = input["cancel_when_event"] as? String ?? "start"
while !finished && Date() < deadline {
    if !readinessCancelled, let required = input["cancel_when_started"] as? Int,
       let path = ProcessInfo.processInfo.environment["MULTIBATCH_EVENTS"],
       let contents = try? String(contentsOfFile: path, encoding: .utf8),
       contents.components(separatedBy: "\"event\": \"\(readyEvent)\"").count - 1 >= required {
        readinessCancelled = true
        runner.cancel()
    }
    RunLoop.main.run(until: Date().addingTimeInterval(0.01))
}
precondition(finished, "synthetic driver timed out")
runner.cancel(); runner.start()  // calls after finish cannot redeliver anything
RunLoop.main.run(until: Date().addingTimeInterval(input["after_finish_wait"] as? Double ?? 0.15))
events[events.count - 1]["probe_calls_after_wait"] = probe.snapshot()
let data = try JSONSerialization.data(withJSONObject: events, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


def b64(data):
    return base64.b64encode(data).decode("ascii")


def job(index, profile=b"profile-A", name="node", *, behavior="normal", delay=0,
        output_name=None, server_url="fixture://target", size=65536, streams=2,
        external_mutable=False, padding=""):
    target = server_url if server_url is not None else f"https://speed.cloudflare.com/__down?bytes={size // streams}"
    fixture_id = f"fixture-{index}"
    config = {"node": name, "bytes": size, "streams": streams, "target": target,
              "latency_timeout": "7s", "download_timeout": "17s", "delay": delay,
              "behavior": behavior, "fixture_id": fixture_id,
              "secret": f"SYNTHETIC_PRIVATE_PROFILE_{index}_8a9b", "padding": padding}
    if output_name is not None:
        config["output_name"] = output_name
    return {"index": index, "profile": b64(profile), "name": name, "name_utf8": b64(name.encode()),
            "snapshot": b64(json.dumps(config, ensure_ascii=False).encode()),
            "bytes": size, "streams": streams, "latency_timeout": "7s",
            "download_timeout": "17s", "server_url": server_url,
            "external_mutable": external_mutable}


class MultiBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("swiftc")
        if not compiler:
            raise RuntimeError("swiftc is required")
        cls.temp = tempfile.TemporaryDirectory(prefix="multi-batch-local-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.driver = cls.root / "main.swift"
        cls.driver.write_text(SWIFT_DRIVER, encoding="utf-8")
        cls.worker = cls.root / "fake-worker"
        cls.worker.write_text(f"#!{sys.executable}\n" + FAKE_WORKER, encoding="utf-8")
        cls.worker.chmod(0o700)
        cls.executable = cls.root / "multi-batch-driver"
        cls.log = cls.root / "events.jsonl"
        cls.env = os.environ.copy()
        cls.env.update({"HOME": str(cls.root), "TMPDIR": str(cls.root),
                        "CLANG_MODULE_CACHE_PATH": str(cls.root / "module-cache"),
                        "MULTIBATCH_EVENTS": str(cls.log), "PYTHONDONTWRITEBYTECODE": "1"})
        command = [compiler, "-swift-version", "6", *(str(source) for source in SOURCES),
                   str(cls.driver), "-module-cache-path", str(cls.root / "module-cache"),
                   "-o", str(cls.executable)]
        compiled = subprocess.run(command, env=cls.env, text=True, capture_output=True, timeout=120)
        if compiled.returncode:
            raise RuntimeError("swiftc failed:\n" + compiled.stdout + compiled.stderr)

    def run_batch(self, jobs, parallel=3, **options):
        # Each test gets a fresh synthetic lifecycle log.
        self.log.write_text("", encoding="utf-8")
        request = {"jobs": jobs, "parallel": parallel, "executable": str(self.worker), **options}
        process = subprocess.run([str(self.executable)], input=json.dumps(request, ensure_ascii=False),
                                 env=self.env, text=True, capture_output=True, timeout=35)
        self.assertEqual(process.returncode, 0, f"driver failed: {process.stdout}\n{process.stderr}")
        self.assertNotIn("SYNTHETIC_SECRET_STDERR", process.stdout + process.stderr)
        self.assertNotIn("SYNTHETIC_PRIVATE_PROFILE", process.stdout + process.stderr)
        events = json.loads(process.stdout)
        self.assertEqual(sum(e["event"] == "finish" for e in events), 1)
        self.assertEqual(events[-1]["event"], "finish")
        results = [e for e in events if e["event"] == "result"]
        if not options.get("allow_duplicate_indices", False):
            self.assertEqual(len({e["index"] for e in results}), len(results))
        self.assertEqual(events[-1]["attempted"], len(results))
        for result in results:
            if result["status"] == "success":
                self.assertEqual(len(result["fields"]), 1, result)
                self.assertIsInstance(result["fields"][0], str)
                self.assertTrue(result.get("decoded"), result)
        log = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        # Search the temporary test artifacts for per-job secret canaries. The
        # compiler cache predates all job inputs and can be very large.
        secrets = {json.loads(base64.b64decode(item["snapshot"]))["secret"].encode() for item in jobs}
        for directory, subdirs, files in os.walk(self.root):
            subdirs[:] = [name for name in subdirs if name != "module-cache"]
            for filename in files:
                artifact = Path(directory) / filename
                content = artifact.read_bytes()
                for secret in secrets:
                    self.assertNotIn(secret, content, f"snapshot canary written to {artifact.name}")
        return results, events[-1], log

    def test_profiles_snapshots_regex_unicode_and_cli(self):
        special = "a.*+?()|[]{}^$\\\u0301节点"
        jobs = [job(0, b"profile-\xc3\xa9", special, external_mutable=True),
                job(1, b"profile-e\xcc\x81", special),
                job(2, b"\x00opaque\xff", "cafe\u0301", server_url=None, size=12000, streams=3),
                job(3, b"profile-4", "café", server_url="fixture://other", size=13, streams=2)]
        results, summary, log = self.run_batch(jobs, parallel=4, start_in_background=True)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(summary["launched"], 4)
        self.assertEqual(len(log), 8, (results, log))
        for result in results:
            expected = jobs[result["index"]]
            self.assertEqual(result["status"], "success", (result, log))
            self.assertEqual(result["profile"], expected["profile"])
            self.assertEqual(result["name_utf8"], b64(expected["name"].encode()))
            record = json.loads(result["fields"][0])
            self.assertEqual(set(record), {"version", "node_name", "node_type", "head_attempts",
                                           "head_successes", "latency_ms", "jitter_ms", "expected_bytes",
                                           "received_bytes", "download_complete", "download_bytes_per_second"})
            self.assertEqual(record["version"], 1)
            self.assertEqual(record["node_name"].encode(), expected["name"].encode())
            self.assertEqual(record["node_type"], f"fixture-{result['index']}")
            expected_bytes = (expected["bytes"] // expected["streams"]) * expected["streams"]
            self.assertEqual((record["expected_bytes"], record["received_bytes"]),
                             (expected_bytes, expected_bytes))
            self.assertEqual((record["head_attempts"], record["head_successes"]), (6, 6))
            self.assertTrue(record["download_complete"])
            self.assertGreater(result["score"], 0)
        self.assertNotEqual(results[0]["profile"], results[1]["profile"])

    def test_global_peak_clamped_to_ten_across_profiles(self):
        jobs = [job(i, b"profile-A" if i % 2 else b"profile-B", f"node-{i}", delay=1.5)
                for i in range(18)]
        results, summary, log = self.run_batch(jobs, parallel=1000)
        self.assertEqual(len(results), 18)
        self.assertTrue(all(result["status"] == "success" for result in results))
        self.assertEqual(summary["peak"], 10)
        active = peak = 0
        for event in log:
            if event["event"] == "start": active += 1; peak = max(peak, active)
            if event["event"] == "end": active -= 1
            self.assertGreaterEqual(active, 0)
            self.assertLessEqual(active, 10)
        self.assertEqual(active, 0)
        self.assertEqual(peak, 10)

    def test_parallel_lower_bound_and_empty_batch(self):
        results, summary, _ = self.run_batch([job(i, name=f"n-{i}") for i in range(3)], parallel=-17)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(item["status"] == "success" for item in results))
        self.assertEqual(summary["peak"], 1)
        results, summary, log = self.run_batch([], parallel=0)
        self.assertEqual((results, summary["attempted"], summary["launched"], log), ([], 0, 0, []))

    def test_cancel_stops_launches_terminates_and_waits(self):
        jobs = [job(i, name=f"long-{i}", delay=20) for i in range(12)]
        results, summary, log = self.run_batch(jobs, parallel=3, cancel_when_started=3)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["launched"], 3)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["status"] == "cancelled" for r in results))
        self.assertEqual(sum(e["event"] == "start" for e in log), 3)
        self.assertEqual(sum(e["event"] == "term" for e in log), 3)
        self.assertEqual(sum(e["event"] == "end" for e in log), 3)
        results, summary, log = self.run_batch(jobs, cancel_before_start=True)
        self.assertEqual(results, [])
        self.assertTrue(summary["cancelled"])
        self.assertEqual((summary["launched"], log), (0, []))

    def test_cancel_from_result_is_reentrant_and_finishes_last(self):
        jobs = [job(0, delay=0.1)] + [job(i, name=f"later-{i}", delay=8) for i in range(1, 8)]
        results, summary, _ = self.run_batch(jobs, parallel=3, cancel_on_result=True)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["launched"], 3)
        self.assertEqual(len(results), 3)
        self.assertEqual(sum(r["status"] == "success" for r in results), 1)
        self.assertEqual(sum(r["status"] == "cancelled" for r in results), 2)

    def test_nested_main_loop_does_not_reenter_callbacks(self):
        jobs = [job(i, name=f"reentrant-{i}", delay=0.1) for i in range(5)]
        results, summary, _ = self.run_batch(jobs, parallel=5, nested_loop=True)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["status"] == "success" for r in results))
        self.assertFalse(summary["cancelled"])

    def test_worker_failures_and_untrusted_output_are_rejected(self):
        cases = [("nonzero", None, "workerFailed"), ("malformed_json", None, "invalidOutput"),
                 ("bad_utf8", None, "invalidOutput"), ("array", None, "invalidOutput"),
                 ("multiple_inline", None, "invalidOutput"), ("multiple_lines", None, "invalidOutput"),
                 ("extra_line", None, "invalidOutput"), ("double_newline", None, "invalidOutput"),
                 ("extra_field", None, "invalidOutput"), ("missing_field", None, "invalidOutput"),
                 ("duplicate_field", None, "invalidOutput"), ("wrong_size", None, "invalidOutput"),
                 ("wrong_received", None, "invalidOutput"), ("wrong_type", None, "invalidOutput"),
                 ("wrong_version", None, "invalidOutput"), ("incomplete_with_speed", None, "invalidOutput"),
                 ("ansi", None, "invalidOutput"), ("normal", "café", "invalidOutput"),
                 ("crlf", None, "success"), ("no_newline", None, "success")]
        jobs = [job(i, name="cafe\u0301", behavior=behavior, output_name=out)
                for i, (behavior, out, _) in enumerate(cases)]
        results, summary, _ = self.run_batch(jobs, parallel=8)
        self.assertFalse(summary["cancelled"])
        actual = {r["index"]: r["status"] for r in results}
        self.assertEqual(actual, {i: case[2] for i, case in enumerate(cases)})

    def test_valid_unscorable_measurements_are_successful(self):
        behaviors = ["incomplete", "few_heads", "zero_latency"]
        jobs = [job(i, name=f"unscorable-{i}", behavior=behavior, size=13, streams=2)
                for i, behavior in enumerate(behaviors)]
        results, summary, _ = self.run_batch(jobs, parallel=3)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(summary["launched"], len(jobs))
        for result in results:
            self.assertEqual(result["status"], "success", result)
            self.assertIsNone(result["score"], result)
            record = json.loads(result["fields"][0])
            self.assertEqual(record["expected_bytes"], 12)
            if behaviors[result["index"]] == "incomplete":
                self.assertFalse(record["download_complete"])
                self.assertEqual(record["received_bytes"], 11)
                self.assertEqual(record["download_bytes_per_second"], 0)

    def test_launch_invalid_options_and_pipe_failure(self):
        jobs = [job(0), job(1)]
        results, summary, _ = self.run_batch(jobs, executable=str(self.root / "missing-worker"))
        self.assertEqual([r["status"] for r in results], ["launchFailed", "launchFailed"])
        self.assertEqual((summary["attempted"], summary["launched"]), (2, 0))
        bad = job(2, name="bad\x00name")
        bad2 = job(3); bad2["streams"] = 0
        results, summary, log = self.run_batch([bad, bad2])
        self.assertTrue(all(r["status"] == "invalidJob" for r in results))
        self.assertEqual((summary["launched"], log), (0, []))
        # Larger than a pipe buffer, with SIGPIPE explicitly restored to default.
        results, summary, _ = self.run_batch([job(4, server_url="fixture://no-read", padding="x" * 2_000_000)])
        self.assertEqual(results[0]["status"], "inputFailed")
        self.assertEqual(summary["launched"], 1)

    def test_watchdog_kills_term_trap_and_unblocks_input(self):
        stuck = job(0, server_url="fixture://trap-no-read", padding="x" * 1_500_000)
        before = time.monotonic()
        results, summary, log = self.run_batch(
            [stuck, job(1, name="after-timeout")], parallel=1,
            watchdog=1.2, termination_grace=0.2)
        self.assertLess(time.monotonic() - before, 6)
        self.assertFalse(summary["cancelled"])
        self.assertEqual([r["status"] for r in results], ["workerFailed", "success"])
        self.assertEqual(summary["launched"], 2)
        self.assertEqual(sum(e["event"] == "trap-ready" for e in log), 1)
        self.assertEqual(sum(e["event"] == "term-ignored" for e in log), 1)
        self.assertEqual(sum(e["event"] == "end" for e in log), 1)

    def test_cancel_kills_term_trap_and_unblocks_input(self):
        stuck = job(0, server_url="fixture://trap-no-read", padding="x" * 1_500_000)
        before = time.monotonic()
        results, summary, log = self.run_batch(
            [stuck, job(1)], parallel=1, watchdog=8, termination_grace=0.2,
            cancel_when_started=1, cancel_when_event="trap-ready")
        self.assertLess(time.monotonic() - before, 5)
        self.assertEqual([r["status"] for r in results], ["cancelled"])
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["launched"], 1)
        self.assertEqual(sum(e["event"] == "term-ignored" for e in log), 1)
        self.assertFalse(any(e["event"] == "end" for e in log))

    def test_complete_json_is_not_accepted_after_watchdog(self):
        before = time.monotonic()
        results, summary, log = self.run_batch(
            [job(0, behavior="valid_then_trap")], watchdog=1.2, termination_grace=0.2)
        self.assertLess(time.monotonic() - before, 5)
        self.assertFalse(summary["cancelled"])
        self.assertEqual([r["status"] for r in results], ["workerFailed"])
        self.assertEqual(sum(e["event"] == "trap-ready" for e in log), 1)
        self.assertEqual(sum(e["event"] == "term-ignored" for e in log), 1)

    def test_prelaunch_guard_cancels_before_stale_job(self):
        jobs = [job(i, name=f"snapshot-{i}") for i in range(8)]
        results, summary, log = self.run_batch(jobs, parallel=1, reject_at_index=2)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [0, 1, 2])
        self.assertEqual((summary["attempted"], summary["launched"]), (2, 2))
        self.assertEqual([r["index"] for r in results], [0, 1])
        self.assertTrue(all(r["status"] == "success" for r in results))
        self.assertEqual(sum(e["event"] == "start" for e in log), 2)
        results, summary, _ = self.run_batch(jobs, parallel=4, reject_at_index=2)
        self.assertEqual(summary["pre_launch_calls"], [0, 1, 2])
        self.assertEqual(summary["attempted"], 2)
        self.assertTrue(summary["cancelled"])
        self.assertTrue(all(r["status"] == "cancelled" for r in results))

    def test_many_synchronous_failures_yield_to_main_thread_stop(self):
        base = job(0, name="invalid\x00node")
        jobs = [{**base, "index": i} for i in range(2000)]
        results, summary, log = self.run_batch(jobs, parallel=10, cancel_on_main_turn=True)
        self.assertTrue(summary["cancelled"])
        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 16)
        self.assertEqual(len(results), summary["attempted"])
        self.assertTrue(all(r["status"] == "invalidJob" for r in results))
        self.assertEqual((summary["launched"], log), (0, []))

    def test_nested_main_loop_and_stop_do_not_reenter_sync_failure(self):
        base = job(0, name="invalid\x00node")
        jobs = [{**base, "index": i} for i in range(2000)]
        results, summary, log = self.run_batch(
            jobs, parallel=10, cancel_on_main_turn=True, nested_loop=True)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "invalidJob")
        self.assertEqual((summary["launched"], log), (0, []))

    def test_many_missing_executables_do_not_block_stop(self):
        base = job(0)
        jobs = [{**base, "index": i} for i in range(2000)]
        results, summary, log = self.run_batch(
            jobs, parallel=10, executable=str(self.root / "missing-worker"), cancel_on_main_turn=True)
        self.assertTrue(summary["cancelled"])
        self.assertLessEqual(len(results), 10)
        self.assertEqual((summary["launched"], log), (0, []))
        self.assertTrue(all(r["status"] == "cancelled" for r in results))

    def test_full_duplex_and_blocked_writer_on_cancel(self):
        results, summary, _ = self.run_batch([job(0, server_url="fixture://flood", padding="x" * 1_500_000)])
        self.assertEqual(results[0]["status"], "invalidOutput")
        self.assertFalse(summary["cancelled"])
        blocked = job(1, server_url="fixture://sleep-no-read", padding="x" * 1_500_000)
        results, summary, log = self.run_batch([blocked, job(2)], parallel=1, cancel_when_started=1)
        self.assertEqual([r["status"] for r in results], ["cancelled"])
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["launched"], 1)
        self.assertEqual(sum(e["event"] == "term" for e in log), 1)

    def test_async_immediate_duplicates_preserve_job_identity(self):
        indices = [71, -9, 302, 14, 600, 3, 48, 90]
        jobs = [job(i, profile=f"profile-{i}".encode(), name=f"node-{i}") for i in indices]
        results, summary, log = self.run_batch(
            jobs, parallel=4, async_prelaunch=True, async_duplicate=True)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], indices)
        self.assertEqual((summary["attempted"], summary["launched"]), (len(jobs), len(jobs)))
        self.assertEqual(summary["peak_outstanding"], 4)
        self.assertEqual(len(summary["probe_calls_after_wait"]), 2 * len(jobs))
        by_index = {item["index"]: item for item in jobs}
        for result in results:
            expected = by_index[result["index"]]
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["profile"], expected["profile"])
            self.assertEqual(result["name_utf8"], expected["name_utf8"])
            self.assertEqual(json.loads(result["fields"][0])["node_type"], f"fixture-{result['index']}")
        self.assertEqual(sum(e["event"] == "start" for e in log), len(jobs))

    def test_async_out_of_order_background_and_main_completions(self):
        jobs = [job(i, profile=f"p-{i}".encode(), name=f"node-{i}") for i in range(3)]
        results, summary, _ = self.run_batch(
            jobs, parallel=3, async_prelaunch=True, async_duplicate=True,
            async_reverse_initial=3)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [0, 1, 2])
        self.assertEqual(summary["probe_calls_after_wait"], [2, 2, 1, 1, 0, 0])
        self.assertEqual({r["index"] for r in results}, {0, 1, 2})
        for result in results:
            expected = jobs[result["index"]]
            self.assertEqual((result["status"], result["profile"], result["name_utf8"]),
                             ("success", expected["profile"], expected["name_utf8"]))
            self.assertEqual(json.loads(result["fields"][0])["node_type"],
                             f"fixture-{result['index']}")
        # Completion delivered on main is also deferred and cannot reenter the hook.
        results, summary, _ = self.run_batch(
            jobs, parallel=3, async_prelaunch=True, async_complete_on_main=True,
            async_duplicate=True, nested_async_hook_index=0)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(len(results), 3)
        self.assertEqual(summary["attempted"], 3)

    def test_async_global_reservations_clamp_and_no_early_finish(self):
        jobs = [job(i, name=f"budget-{i}", delay=0.15) for i in range(14)]
        for parallel, expected in ((-7, 1), (1000, 10)):
            with self.subTest(parallel=parallel):
                results, summary, _ = self.run_batch(
                    jobs, parallel=parallel, async_prelaunch=True, async_background=True)
                self.assertFalse(summary["cancelled"])
                self.assertEqual(len(results), len(jobs))
                self.assertEqual(summary["peak_outstanding"], expected)
                self.assertLessEqual(summary["peak"], expected)
        # The first worker can finish while the second check is still pending;
        # finish must wait for that check and its worker.
        jobs = [job(0, name="quick"), job(1, name="pending")]
        results, summary, _ = self.run_batch(
            jobs, parallel=2, async_prelaunch=True, async_hold_index=1,
            async_release_held_on_result=True)
        self.assertEqual(summary["probe_calls_after_wait"], [0, 1])
        self.assertEqual([r["index"] for r in results], [0, 1])
        self.assertEqual((summary["attempted"], summary["launched"]), (2, 2))
        self.assertFalse(summary["cancelled"])

    def test_async_concurrent_duplicate_completion_storm(self):
        jobs = [job(i, name=f"invalid\x00{i}") for i in range(20)]
        results, summary, log = self.run_batch(
            jobs, parallel=10, async_prelaunch=True, async_concurrent_duplicates=8,
            after_finish_wait=0.6)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r["status"] == "invalidJob" for r in results))
        self.assertEqual(len(summary["probe_calls_after_wait"]), 160)
        self.assertLessEqual(summary["peak_outstanding"], 10)
        self.assertEqual(log, [])
        # Either decision may win a real race; the other seven must be ignored.
        results, summary, log = self.run_batch(
            [job(5, name="invalid\x00node")], parallel=1, async_prelaunch=True,
            async_concurrent_duplicates=8, async_concurrent_opposites=True,
            after_finish_wait=0.6)
        self.assertEqual(len(summary["probe_calls_after_wait"]), 8)
        self.assertEqual(log, [])
        if summary["cancelled"]:
            self.assertEqual((summary["attempted"], results), (0, []))
        else:
            self.assertEqual((summary["attempted"], [r["status"] for r in results]),
                             (1, ["invalidJob"]))

    def test_async_cancel_pending_without_wait_and_ignore_late_callbacks(self):
        jobs = [job(i, name=f"pending-{i}") for i in range(4)]
        before = time.monotonic()
        results, summary, log = self.run_batch(
            jobs, parallel=2, async_prelaunch=True, async_duplicate=True,
            async_delays={"0": -1, "1": 300}, cancel_after_ms=30, after_finish_wait=0.6)
        self.assertLess(time.monotonic() - before, 4)
        self.assertEqual(summary["pre_launch_calls"], [0, 1])
        self.assertEqual(summary["probe_calls_after_wait"], [1, 1])
        self.assertEqual((summary["attempted"], summary["launched"], results, log), (0, 0, [], []))
        self.assertTrue(summary["cancelled"])

    def test_async_cancel_active_and_preparing_cleans_child(self):
        jobs = [job(0, name="active", delay=20), job(1, name="pending"), job(2, name="queued")]
        results, summary, log = self.run_batch(
            jobs, parallel=2, async_prelaunch=True, async_duplicate=True,
            async_wait_worker_start=1, async_delays={"1": 600},
            cancel_when_started=1, after_finish_wait=1.2)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [0, 1])
        self.assertEqual(summary["probe_calls_after_wait"], [0, 0, 1, 1])
        self.assertEqual((summary["attempted"], summary["launched"]), (1, 1))
        self.assertEqual([(r["index"], r["status"]) for r in results], [(0, "cancelled")])
        self.assertEqual(sum(e["event"] == "start" for e in log), 1)
        self.assertEqual(sum(e["event"] == "term" for e in log), 1)
        self.assertEqual(sum(e["event"] == "end" for e in log), 1)

    def test_async_reject_cancels_active_and_prevents_queued_launch(self):
        jobs = [job(0, name="active", delay=20), job(1, name="reject"), job(2, name="queued")]
        results, summary, log = self.run_batch(
            jobs, parallel=2, async_prelaunch=True, async_duplicate=True,
            async_wait_worker_start=1, reject_at_index=1)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [0, 1])
        self.assertEqual((summary["attempted"], summary["launched"]), (1, 1))
        self.assertEqual([(r["index"], r["status"]) for r in results], [(0, "cancelled")])
        self.assertEqual(sum(e["event"] == "start" for e in log), 1)
        self.assertEqual(sum(e["event"] == "term" for e in log), 1)

    def test_async_reentry_cancel_before_start_and_empty(self):
        jobs = [job(i, name=f"reentry-{i}") for i in range(3)]
        results, summary, log = self.run_batch(
            jobs, parallel=2, async_prelaunch=True, cancel_in_async_hook=0,
            async_duplicate=True, nested_async_hook_index=0)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [0])
        self.assertEqual((summary["attempted"], results, log), (0, [], []))
        results, summary, log = self.run_batch(jobs, async_prelaunch=True, cancel_before_start=True)
        self.assertTrue(summary["cancelled"])
        self.assertEqual((summary["pre_launch_calls"], results, log), ([], [], []))
        results, summary, log = self.run_batch([], async_prelaunch=True, parallel=0)
        self.assertFalse(summary["cancelled"])
        self.assertEqual((summary["pre_launch_calls"], results, log), ([], [], []))

    def test_async_invalid_jobs_duplicate_indices_and_nested_result(self):
        # Reservations use independent internal IDs even if a caller supplies
        # duplicate indices. Distinct names/profiles expose any job mix-up.
        jobs = [job(17, b"profile-a", "first"), job(17, b"profile-b", "second"),
                job(44, b"profile-c", "invalid\x00node"), job(91, b"profile-d", "last")]
        results, summary, log = self.run_batch(
            jobs, parallel=4, async_prelaunch=True, async_duplicate=True,
            nested_loop=True, allow_duplicate_indices=True)
        self.assertFalse(summary["cancelled"])
        self.assertEqual(summary["pre_launch_calls"], [17, 17, 44, 91])
        self.assertEqual((summary["attempted"], summary["launched"]), (4, 3))
        self.assertEqual(sum(e["event"] == "start" for e in log), 3)
        actual = {r["name_utf8"]: (r["profile"], r["status"]) for r in results}
        self.assertEqual(actual, {j["name_utf8"]: (j["profile"],
                             "invalidJob" if j["index"] == 44 else "success") for j in jobs})

    def test_async_accepted_worker_retains_watchdog(self):
        stuck = job(0, server_url="fixture://trap-no-read", padding="x" * 1_500_000)
        results, summary, log = self.run_batch(
            [stuck, job(1, name="after-timeout")], parallel=1, async_prelaunch=True,
            watchdog=1.2, termination_grace=0.2)
        self.assertFalse(summary["cancelled"])
        self.assertEqual([r["status"] for r in results], ["workerFailed", "success"])
        self.assertEqual(summary["launched"], 2)
        self.assertEqual(sum(e["event"] == "term-ignored" for e in log), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
