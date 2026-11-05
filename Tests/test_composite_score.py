#!/usr/bin/env python3
"""Compile and test the pure Swift scorer without accessing the live app.

All Swift driver, executable, and compiler-cache files live in a temporary
folder. The executable only reads synthetic numerical test cases from stdin.
"""

import itertools
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT / "Sources" / "CompositeScore.swift"
MIB = 1_048_576.0

# This is deliberately named main.swift when written: it is the executable's
# top-level entry point, compiled alongside the real, app-independent source.
SWIFT_DRIVER = r'''
import Foundation

while let line = readLine() {
    let parts = line.split(separator: " ")
    guard parts.count == 4,
          let speed = Double(parts[0]),
          let latency = Double(parts[1]),
          let jitter = Double(parts[2]),
          let failure = Double(parts[3]) else {
        fatalError("invalid synthetic test input")
    }
    if let score = CompositeScore.score(downloadBytesPerSecond: speed,
                                        httpLatencyMilliseconds: latency,
                                        httpJitterMilliseconds: jitter,
                                        failurePercent: failure) {
        print(score)
    } else {
        print("nil")
    }
}
'''


class CompositeScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise RuntimeError("swiftc is required to run the composite score tests")
        cls.temporary = tempfile.TemporaryDirectory(prefix="composite-score-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        driver = root / "main.swift"
        driver.write_text(SWIFT_DRIVER, encoding="utf-8")
        cls.executable = root / "composite-score-test"
        cls.environment = os.environ.copy()
        cls.environment.update({
            "HOME": str(root),
            "TMPDIR": str(root),
            "CLANG_MODULE_CACHE_PATH": str(root / "module-cache"),
        })
        command = [compiler, str(SOURCE), str(driver), "-module-cache-path",
                   str(root / "module-cache"), "-o", str(cls.executable)]
        compiled = subprocess.run(command, env=cls.environment, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if compiled.returncode:
            raise RuntimeError("swiftc failed:\n" + compiled.stdout + compiled.stderr)

    def scores(self, rows):
        rows = list(rows)
        data = "".join(" ".join(repr(float(value)) for value in row) + "\n"
                       for row in rows)
        result = subprocess.run([str(self.executable)], input=data,
                                env=self.environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), len(rows), result.stdout)
        return [None if line == "nil" else float(line) for line in lines]

    def score(self, speed=5 * MIB, latency=100, jitter=10, failure=0):
        return self.scores([(speed, latency, jitter, failure)])[0]

    def test_known_weighted_score(self):
        # 40 download + 24 latency + 15 success + 6 jitter.
        self.assertAlmostEqual(self.score(25 * MIB, 100, 40, 25), 85.0, places=12)
        # Check the uncapped logarithmic rate and fractional HEAD failure rate.
        expected = (40 * math.log1p(5) / math.log1p(25)
                    + 30 * (1 - 120 / 500)
                    + 20 * (1 - (100 / 6) / 100)
                    + 10 * (1 - 25 / 100))
        self.assertAlmostEqual(self.score(5 * MIB, 120, 25, 100 / 6),
                               expected, places=12)

    def test_download_is_monotone_and_caps_at_25_mib_per_second(self):
        values = self.scores((speed * MIB, 100, 10, 0)
                             for speed in (0.01, 1, 5, 25, 100, 1_000))
        self.assertTrue(all(a < b for a, b in zip(values[:3], values[1:4])))
        self.assertEqual(values[3], values[4])
        self.assertEqual(values[4], values[5])

    def test_latency_decreases_and_caps_at_500_milliseconds(self):
        values = self.scores((25 * MIB, latency, 0, 0)
                             for latency in (1, 100, 250, 499, 500, 1_000))
        self.assertTrue(all(a > b for a, b in zip(values[:4], values[1:5])))
        self.assertEqual(values[4], values[5])
        self.assertAlmostEqual(values[4], 70.0)

    def test_jitter_decreases_and_caps_at_100_milliseconds(self):
        values = self.scores((25 * MIB, 100, jitter, 0)
                             for jitter in (0, 10, 50, 99, 100, 1_000))
        self.assertTrue(all(a > b for a, b in zip(values[:4], values[1:5])))
        self.assertEqual(values[4], values[5])
        self.assertAlmostEqual(values[4], 84.0)

    def test_six_head_failure_percentages_and_inclusive_boundary(self):
        values = self.scores((25 * MIB, 100, 0, 100.0 * failed / 6)
                             for failed in range(7))
        self.assertTrue(all(a > b for a, b in zip(values[:3], values[1:4])))
        self.assertAlmostEqual(values[3], 84.0)  # three failures = 50%
        self.assertEqual(values[4:], [None, None, None])
        below = math.nextafter(50.0, -math.inf)
        above = math.nextafter(50.0, math.inf)
        results = self.scores((25 * MIB, 100, 0, percentage)
                              for percentage in (0.0, 0.1, 37.3, below, 50.0, above))
        self.assertTrue(all(result is not None for result in results[:-1]))
        self.assertIsNone(results[-1])

    def test_rejects_zero_or_negative_speed_and_latency(self):
        cases = [(speed, 100, 0, 0) for speed in (0.0, -0.0, -1.0)]
        cases += [(MIB, latency, 0, 0) for latency in (0.0, -0.0, -1.0)]
        self.assertEqual(self.scores(cases), [None] * len(cases))
        # Even the smallest positive finite Double is a valid measurement.
        tiny = math.nextafter(0.0, 1.0)
        self.assertIsNotNone(self.score(speed=tiny, latency=tiny))

    def test_zero_jitter_and_failure_are_valid_but_negatives_are_not(self):
        self.assertIsNotNone(self.score(jitter=0, failure=0))
        self.assertIsNotNone(self.score(jitter=-0.0, failure=-0.0))
        cases = [(MIB, 100, -1, 0), (MIB, 100, -math.ulp(0.0), 0),
                 (MIB, 100, 0, -1), (MIB, 100, 0, -math.ulp(0.0))]
        self.assertEqual(self.scores(cases), [None] * len(cases))

    def test_all_nonfinite_values_are_rejected_for_every_input(self):
        cases = []
        for index, invalid in itertools.product(range(4),
                                                (math.nan, math.inf, -math.inf)):
            case = [MIB, 100.0, 10.0, 0.0]
            case[index] = invalid
            cases.append(case)
        self.assertEqual(self.scores(cases), [None] * len(cases))

    def test_extreme_finite_valid_values_stay_within_bounds(self):
        tiny = math.nextafter(0.0, 1.0)
        greatest = sys.float_info.max
        cases = itertools.product((tiny, MIB, 25 * MIB, greatest),
                                  (tiny, 1, 500, greatest),
                                  (0, 1, 100, greatest), (0, 1, 50))
        for score in self.scores(cases):
            self.assertIsNotNone(score)
            self.assertTrue(math.isfinite(score) and 0 <= score <= 100, score)
        self.assertAlmostEqual(self.score(greatest, greatest, greatest, 50), 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
