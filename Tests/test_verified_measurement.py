#!/usr/bin/env python3
"""Compile and exercise the worker-v1 decoder with synthetic, offline inputs.

Swift artifacts, driver, and compiler caches are confined to a temporary
folder. No worker, profile, network service, controller, or app is accessed.
Run: python3 -B Tests/test_verified_measurement.py -v
"""

import base64
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
SOURCES = [PROJECT / "Sources" / "CompositeScore.swift",
           PROJECT / "Sources" / "VerifiedMeasurement.swift"]
MIB = 1_048_576
MAX_OUTPUT = 1_048_576
INT_MAX = (1 << 63) - 1
BASE = {
    "version": 1,
    "node_name": "node A",
    "node_type": "Synthetic",
    "head_attempts": 6,
    "head_successes": 5,
    "latency_ms": 120.0,
    "jitter_ms": 25.0,
    "expected_bytes": 100,
    "received_bytes": 100,
    "download_complete": True,
    "download_bytes_per_second": 5 * MIB,
}

# Each input line carries base64 bytes for the JSON and expected name followed
# by the decimal requested size and stream count. The decoder's results alone
# are sent back as JSON; arbitrary malformed JSON never reaches this framing.
SWIFT_DRIVER = r'''
import Foundation

while let line = readLine() {
    let fields = line.split(separator: "\t", omittingEmptySubsequences: false)
    guard fields.count == 4,
          let raw = Data(base64Encoded: String(fields[0])),
          let nameData = Data(base64Encoded: String(fields[1])),
          let name = String(validating: nameData, as: UTF8.self),
          let requested = Int(fields[2]), let streams = Int(fields[3]) else {
        fatalError("invalid synthetic test frame")
    }
    guard let result = VerifiedMeasurement.decode(raw, expectedName: name,
                                                   requestedBytes: requested,
                                                   streams: streams) else {
        print("null")
        continue
    }
    let object: [String: Any] = [
        "nodeName": result.nodeName,
        "nodeType": result.nodeType,
        "headAttempts": result.headAttempts,
        "headSuccesses": result.headSuccesses,
        "latencyMilliseconds": result.latencyMilliseconds,
        "jitterMilliseconds": result.jitterMilliseconds,
        "expectedBytes": result.expectedBytes,
        "receivedBytes": result.receivedBytes,
        "downloadComplete": result.downloadComplete,
        "downloadBytesPerSecond": result.downloadBytesPerSecond,
        "score": result.score.map { $0 as Any } ?? NSNull()
    ]
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def object_with(**changes):
    return dict(BASE, **changes)


def raw_with(**literal_changes):
    """Use exact JSON literals (not Python values) for selected fields."""
    fields = []
    for key, value in BASE.items():
        raw = literal_changes.get(key)
        if raw is None:
            raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        fields.append(json.dumps(key) + ":" + raw)
    return ("{" + ",".join(fields) + "}").encode("ascii")


def frame(raw, name="node A", requested=101, streams=2):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if isinstance(raw, dict):
        raw = encode(raw)
    return (raw, name, requested, streams)


def expected_score(speed, latency, jitter, successes):
    return (40 * min(1, math.log1p(speed / MIB) / math.log1p(25))
            + 30 * min(1, max(0, 1 - latency / 500))
            + 20 * (successes / 6)
            + 10 * min(1, max(0, 1 - jitter / 100)))


class VerifiedMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise RuntimeError("swiftc is required to run the decoder tests")
        cls.temp = tempfile.TemporaryDirectory(prefix="verified-measurement-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        driver = root / "main.swift"
        driver.write_text(SWIFT_DRIVER, encoding="utf-8")
        cls.executable = root / "verified-measurement-test"
        cls.environment = os.environ.copy()
        cls.environment.update({
            "HOME": str(root),
            "TMPDIR": str(root),
            "XDG_CACHE_HOME": str(root / "cache"),
            "CLANG_MODULE_CACHE_PATH": str(root / "module-cache"),
            "SWIFT_MODULECACHE_PATH": str(root / "module-cache"),
        })
        command = [compiler, *map(str, SOURCES), str(driver),
                   "-module-cache-path", str(root / "module-cache"), "-o", str(cls.executable)]
        completed = subprocess.run(command, env=cls.environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if completed.returncode:
            raise RuntimeError("swiftc failed:\n" + completed.stdout + completed.stderr)

    def decode_many(self, cases):
        cases = list(cases)
        if not cases:
            return []
        lines = []
        for raw, name, requested, streams in cases:
            lines.append("\t".join((base64.b64encode(raw).decode("ascii"),
                                    base64.b64encode(name.encode("utf-8")).decode("ascii"),
                                    str(requested), str(streams))))
        completed = subprocess.run([str(self.executable)], input="\n".join(lines) + "\n",
                                   env=self.environment, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = completed.stdout.splitlines()
        self.assertEqual(len(output), len(cases), completed.stdout[:1000])
        return [json.loads(line) for line in output]

    def decode(self, raw, name="node A", requested=101, streams=2):
        return self.decode_many([frame(raw, name, requested, streams)])[0]

    def assert_all_invalid(self, cases):
        cases = list(cases)
        results = self.decode_many(cases)
        for case, result in zip(cases, results):
            with self.subTest(case=repr(case)[:400]):
                self.assertIsNone(result)

    def test_valid_fields_and_exact_score(self):
        result = self.decode(object_with())
        self.assertEqual(result, {
            "nodeName": "node A", "nodeType": "Synthetic", "headAttempts": 6,
            "headSuccesses": 5, "latencyMilliseconds": 120.0,
            "jitterMilliseconds": 25.0, "expectedBytes": 100,
            "receivedBytes": 100, "downloadComplete": True,
            "downloadBytesPerSecond": 5 * MIB, "score": result["score"],
        })
        self.assertAlmostEqual(result["score"], expected_score(5 * MIB, 120, 25, 5), places=12)

    def test_score_thresholds_and_scorer_constraints(self):
        cases = [frame(object_with(head_successes=i)) for i in range(7)]
        cases += [frame(object_with(latency_ms=0)), frame(object_with(jitter_ms=0)),
                  frame(object_with(latency_ms=5000, jitter_ms=5000))]
        results = self.decode_many(cases)
        for i in range(7):
            self.assertEqual(results[i]["headSuccesses"], i)
            if i < 3:
                self.assertIsNone(results[i]["score"])
            else:
                self.assertAlmostEqual(results[i]["score"],
                                       expected_score(5 * MIB, 120, 25, i), places=12)
        self.assertIsNone(results[7]["score"])  # CompositeScore requires positive latency.
        self.assertAlmostEqual(results[8]["score"], expected_score(5 * MIB, 120, 0, 5), places=12)
        self.assertAlmostEqual(results[9]["score"], expected_score(5 * MIB, 5000, 5000, 5), places=12)

    def test_incomplete_retains_measurements_with_zero_speed(self):
        cases = [frame(object_with(download_complete=False, download_bytes_per_second=0,
                                   received_bytes=count, head_successes=successes))
                 for count in (0, 1, 99, 100, 101, 102) for successes in (0, 3, 6)]
        for case, result in zip(cases, self.decode_many(cases)):
            with self.subTest(raw=case[0]):
                self.assertIsNotNone(result)
                source = json.loads(case[0])
                self.assertEqual(result["receivedBytes"], source["received_bytes"])
                self.assertEqual(result["headSuccesses"], source["head_successes"])
                self.assertFalse(result["downloadComplete"])
                self.assertEqual(result["downloadBytesPerSecond"], 0)
                self.assertIsNone(result["score"])

    def test_download_completeness_invariants(self):
        invalid = [object_with(received_bytes=n) for n in (-1, 0, 99, 101, 102, 103)]
        invalid += [object_with(download_bytes_per_second=n) for n in (0, -1)]
        invalid += [object_with(download_complete=False, download_bytes_per_second=n)
                    for n in (-1, 0.0001, 1, 1e200)]
        invalid += [object_with(download_complete=False, download_bytes_per_second=0,
                                received_bytes=n) for n in (-1, 103, 999999)]
        self.assert_all_invalid(frame(item) for item in invalid)
        self.assertIsNotNone(self.decode(raw_with(download_complete="false",
                                                 download_bytes_per_second="-0.0")))

    def test_head_counts_and_version(self):
        cases = [frame(object_with(head_attempts=n)) for n in (-1, 0, 5, 7, 999)]
        cases += [frame(object_with(head_successes=n)) for n in (-1, 7, 100)]
        cases += [frame(object_with(version=n)) for n in (-1, 0, 2)]
        self.assert_all_invalid(cases)

    def test_integer_tokens_are_strict_and_int_bounded(self):
        cases = []
        for key in ("version", "head_attempts", "head_successes", "expected_bytes", "received_bytes"):
            literal = str(BASE[key])
            for bad in ("true", "false", "null", '"' + literal + '"',
                        literal + ".0", literal + "e0", "[]", "{}",
                        str(INT_MAX + 1), str(-INT_MAX - 2)):
                cases.append(frame(raw_with(**{key: bad})))
        self.assert_all_invalid(cases)
        # Values above Double's exact-integer limit are still checked exactly.
        size = (1 << 53) + 1
        payload = object_with(expected_bytes=size, received_bytes=size)
        self.assertEqual(self.decode(payload, requested=size, streams=1)["receivedBytes"], size)
        self.assertIsNone(self.decode(object_with(expected_bytes=size - 1, received_bytes=size - 1),
                                      requested=size, streams=1))

    def test_requested_size_stream_count_and_overflow(self):
        cases = [frame(object_with(), requested=r, streams=s)
                 for r, s in ((0, 1), (-1, 1), (101, 0), (101, -1), (2, 3),
                              (INT_MAX, 1), (INT_MAX, 2), (INT_MAX, 3))]
        cases += [frame(object_with(expected_bytes=101, received_bytes=101), requested=101, streams=2),
                  frame(object_with(expected_bytes=99, received_bytes=99), requested=101, streams=2)]
        self.assert_all_invalid(cases)
        payload = object_with(expected_bytes=INT_MAX - 1, received_bytes=INT_MAX,
                              download_complete=False, download_bytes_per_second=0)
        result = self.decode(payload, requested=INT_MAX - 1, streams=1)
        self.assertEqual(result["expectedBytes"], INT_MAX - 1)
        self.assertEqual(result["receivedBytes"], INT_MAX)
        self.assertIsNone(result["score"])
        self.assertIsNotNone(self.decode(object_with(expected_bytes=99, received_bytes=99),
                                         requested=101, streams=3))

    def test_numeric_fields_reject_type_confusion_and_nonfinite_values(self):
        cases = []
        for key in ("latency_ms", "jitter_ms", "download_bytes_per_second"):
            for bad in ("true", "false", "null", '"1"', "[]", "{}", "NaN", "Infinity",
                        "-Infinity", "1e309", "-1", "-0.1", "-1e-9999", "1e-9999"):
                cases.append(frame(raw_with(**{key: bad})))
        for bad in ("0", "1", '"true"', "null", "[]", "{}"):
            cases.append(frame(raw_with(download_complete=bad)))
        self.assert_all_invalid(cases)
        result = self.decode(raw_with(latency_ms="1e+2", jitter_ms="0.0",
                                      download_bytes_per_second="5.24288e6"))
        self.assertEqual(result["latencyMilliseconds"], 100)
        self.assertEqual(result["jitterMilliseconds"], 0)
        self.assertEqual(result["downloadBytesPerSecond"], 5 * MIB)
        self.assertIsNotNone(self.decode(raw_with(jitter_ms="5e-324")))
        self.assertIsNotNone(self.decode(raw_with(download_complete="false",
                                                 download_bytes_per_second="-0e99")))

    def test_json_number_grammar(self):
        bad_numbers = ("01", "00", "-01", "+1", "1.", ".1", "1e", "1e+", "1e-",
                       "1..0", "0x1", "1_0", "--1", "0e+", "1e1.2", "-", "00.1")
        self.assert_all_invalid(frame(raw_with(latency_ms=value)) for value in bad_numbers)

    def test_missing_unknown_and_duplicate_fields(self):
        cases = []
        for key in BASE:
            missing = dict(BASE)
            del missing[key]
            cases.append(frame(missing))
            literal = json.dumps(BASE[key], ensure_ascii=True)
            # Duplicate at end, including duplicates with exactly the same value.
            cases.append(frame(encode(BASE)[:-1] + b"," + encode(key) + b":" + literal.encode() + b"}"))
        cases += [frame(dict(BASE, extra=1)),
                  frame(encode(BASE)[:-1] + b',"node\\u005fname":"node A"}'),
                  frame(encode(BASE)[:-1] + b',"\\u0076ersion":1}'),
                  frame(encode(BASE).replace(b'"node_type"', b'"NODE_TYPE"'))]
        self.assert_all_invalid(cases)
        # Escaped field names are legal if they decode to the exact required key.
        self.assertIsNotNone(self.decode(encode(BASE).replace(b'"version"', b'"\\u0076ersion"')))
        self.assertIsNotNone(self.decode(encode(dict(reversed(list(BASE.items()))))))

    def test_exact_utf8_name_and_valid_escapes(self):
        names = ["café", "cafe\u0301", "节点 🚀", 'quote" slash\\ path/', "😀",
                 "\ufeffleading BOM", "\u0000embedded control"]
        cases = []
        for name in names:
            value = object_with(node_name=name)
            cases.append(frame(encode(value), name=name))
            cases.append(frame(json.dumps(value, ensure_ascii=True, separators=(",", ":")), name=name))
        for case, result in zip(cases, self.decode_many(cases)):
            self.assertIsNotNone(result, repr(case))
            self.assertEqual(result["nodeName"].encode("utf-8"), case[1].encode("utf-8"))
        self.assert_all_invalid([frame(object_with(node_name="café"), name="cafe\u0301"),
                                 frame(object_with(node_name="cafe\u0301"), name="café"),
                                 frame(object_with(node_name="\ufeffnode A")),
                                 frame(object_with(node_name="node A"), name="\ufeffnode A"),
                                 frame(object_with(node_name="NODE A")),
                                 frame(raw_with(node_name="true"))])
        for raw in (b'"\\ud83d\\ude00"', b'"\\uD83D\\uDE00"'):
            self.assertIsNotNone(self.decode(raw_with(node_name=raw.decode("ascii")), name="😀"))

    def test_node_type_must_be_nonempty_and_free_of_controls(self):
        unsafe = ["", "x\n", "x\r", "x\t", "x\u2028", "x\u2029"]
        unsafe += ["x" + chr(value) for value in list(range(32)) + list(range(127, 160))]
        unsafe += ["x\u200b", "x\u202e", "\ufeffSynthetic"]
        self.assert_all_invalid([*(frame(object_with(node_type=value)) for value in unsafe),
                                 frame(raw_with(node_type='"\\uFEFFSynthetic"'))])
        for value in ("Synthetic", "节点 😀", "with space", "emoji 🌍"):
            self.assertEqual(self.decode(object_with(node_type=value))["nodeType"], value)

    def test_invalid_utf8_and_json_string_escapes(self):
        bad_strings = [b'"\\uD800"', b'"\\uDC00"', b'"\\uD800x"', b'"\\uD800\\u0041"',
                       b'"\\uD800\\uD800"', b'"\\u12"', b'"\\u12xz"', b'"\\q"',
                       b'"\\x20"', b'"abc\\"', b'"line\nfeed"', b'"\x00"',
                       b'"\xff"', b'"\xc0\x80"', b'"\xed\xa0\x80"', b'"\xf4\x90\x80\x80"']
        original = b'"node_type":"Synthetic"'
        baseline = encode(BASE)
        self.assert_all_invalid(frame(baseline.replace(original, b'"node_type":' + bad))
                                for bad in bad_strings)

    def test_whole_single_line_document_and_trailing_data(self):
        baseline = encode(BASE)
        for valid in (baseline, baseline + b"\n", baseline + b"\r\n",
                      b" \t" + baseline + b" \t\n"):
            self.assertIsNotNone(self.decode(valid))
        invalid = [b"", b"{}", b"[]", b"null", b"\xef\xbb\xbf" + baseline,
                   baseline + b"{}", baseline + b" garbage", baseline + b"\x00",
                   baseline + b"\n\n", baseline + b"\n \n", baseline + b"\r",
                   b"\n" + baseline, baseline.replace(b",", b",\n", 1),
                   baseline[:-1], baseline[:-1] + b",}",
                   baseline.replace(b'"version":1', b'"version" 1'),
                   baseline.replace(b'"version":1,', b'"version":1,,')]
        self.assert_all_invalid(frame(raw) for raw in invalid)

    def test_deterministic_invalid_byte_insertion(self):
        rng = random.Random(4087)
        baseline = encode(BASE)
        # Tab may be whitespace, and a final LF is an allowed line ending.
        invalid_bytes = [value for value in range(32) if value not in (9, 10)] + [0x80, 0xc0, 0xff]
        cases = []
        for _ in range(400):
            position = rng.randrange(len(baseline) + 1)
            byte = bytes([rng.choice(invalid_bytes)])
            cases.append(frame(baseline[:position] + byte + baseline[position:]))
        self.assert_all_invalid(cases)

    def test_output_size_limit_including_line_ending(self):
        baseline = encode(BASE)
        exact = baseline + b" " * (MAX_OUTPUT - len(baseline))
        self.assertEqual(len(exact), MAX_OUTPUT)
        self.assertIsNotNone(self.decode(exact))
        self.assertIsNotNone(self.decode(exact[:-1] + b"\n"))
        self.assertIsNone(self.decode(exact + b" "))
        self.assertIsNone(self.decode(exact + b"\n"))

    def test_deterministic_random_valid_measurements(self):
        rng = random.Random(712308)
        cases = []
        source = []
        for _ in range(300):
            requested = rng.randint(1, 100_000)
            streams = rng.randint(1, min(requested, 40))
            expected = (requested // streams) * streams
            complete = bool(rng.getrandbits(1))
            successes = rng.randrange(7)
            speed = rng.uniform(1, 100 * MIB) if complete else 0
            latency = rng.uniform(0.1, 1000)
            jitter = rng.uniform(0, 200)
            received = expected if complete else rng.randint(0, expected + streams)
            payload = object_with(expected_bytes=expected, received_bytes=received,
                                  download_complete=complete, download_bytes_per_second=speed,
                                  head_successes=successes, latency_ms=latency, jitter_ms=jitter)
            cases.append(frame(payload, requested=requested, streams=streams))
            source.append(payload)
        for payload, result in zip(source, self.decode_many(cases)):
            self.assertIsNotNone(result)
            self.assertEqual(result["expectedBytes"], payload["expected_bytes"])
            self.assertEqual(result["receivedBytes"], payload["received_bytes"])
            if payload["download_complete"] and payload["head_successes"] >= 3:
                self.assertAlmostEqual(result["score"], expected_score(
                    payload["download_bytes_per_second"], payload["latency_ms"],
                    payload["jitter_ms"], payload["head_successes"]), places=11)
            else:
                self.assertIsNone(result["score"])


if __name__ == "__main__":
    unittest.main()
