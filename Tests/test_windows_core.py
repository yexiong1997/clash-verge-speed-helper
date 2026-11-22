#!/usr/bin/env python3
"""Synthetic Windows-core tests. Never access the installed Clash instance."""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Windows.measurement import composite_score, decode_measurement
from Windows.schedule import TestSchedule

if os.name == "nt":
    from Windows.profile_registry import (ProfileError, inspect_nodes, load_registry,
                                          parse_yaml, revalidate, snapshot_profile)


def good():
    return {"version": 1, "node_name": "示例 node", "node_type": "Synthetic",
            "head_attempts": 6, "head_successes": 5, "latency_ms": 120,
            "jitter_ms": 25, "expected_bytes": 100, "received_bytes": 100,
            "download_complete": True, "download_bytes_per_second": 5 * 1_048_576}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class MeasurementTests(unittest.TestCase):
    def decode(self, data):
        return decode_measurement(data, "示例 node", 101, 2)

    def test_complete_score(self):
        result = self.decode(encode(good()) + b"\r\n")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.score, composite_score(5 * 1_048_576, 120, 25, 100 / 6))

    def test_reject_invalid_json_and_ambiguity(self):
        base = encode(good())
        for data in (base + b"\n{}", b"\xef\xbb\xbf" + base,
                     base[:-1] + b',"version":1}', base.replace(b'"version":1', b'"version":1.0'),
                     base.replace(b'"version":1', b'"version":true'),
                     base.replace(b'"latency_ms":120', b'"latency_ms":1e-9999'),
                     base[:-1] + b',"unknown":0}'):
            with self.subTest(data=data):
                self.assertIsNone(self.decode(data))

    def test_name_type_counts_and_completion(self):
        for changes in ({"node_name": "other"}, {"node_type": "bad\nname"},
                        {"head_attempts": 5}, {"head_successes": 7},
                        {"expected_bytes": 101}, {"received_bytes": 99},
                        {"download_complete": False}, {"download_bytes_per_second": -1}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.decode(encode(good() | changes)))
        partial = self.decode(encode(good() | {"download_complete": False,
                                              "received_bytes": 99,
                                              "download_bytes_per_second": 0}))
        self.assertIsNotNone(partial)
        self.assertIsNone(partial.score)

    def test_scoring_rejects_invalid(self):
        for args in ((0, 1, 0, 0), (1, 0, 0, 0), (1, 1, -1, 0),
                     (1, 1, 0, 51), (math.inf, 1, 0, 0)):
            self.assertIsNone(composite_score(*args))


class ScheduleTests(unittest.TestCase):
    def test_skip_busy_and_rebase_on_finish(self):
        schedule = TestSchedule()
        schedule.arm(15, now=10)
        token = schedule.begin_manual()
        self.assertEqual(schedule.poll(910)[0], "skipped_busy")
        self.assertFalse(schedule.finish(object(), 1000))
        self.assertTrue(schedule.finish(token, 1000))
        self.assertEqual(schedule.poll(1899)[0], "not_due")
        state, token = schedule.poll(1900)
        self.assertEqual(state, "started")
        schedule.disarm()
        self.assertTrue(schedule.finish(token, 2000))
        self.assertEqual(schedule.poll(3000)[0], "disarmed")

    def test_backwards_and_invalid(self):
        schedule = TestSchedule()
        schedule.arm(30, now=100)
        self.assertEqual(schedule.poll(99)[0], "clock_moved")
        with self.assertRaises(ValueError):
            schedule.arm(7, now=100)
        self.assertIsNone(schedule.minutes)

    def test_countdown_tracks_deadline_and_completion_rebase(self):
        schedule = TestSchedule()
        self.assertEqual(schedule.display(now=10), (None, None, False))
        schedule.arm(15, now=10)
        self.assertEqual(schedule.display(now=10), (15, 900, False))
        self.assertEqual(schedule.display(now=11.2), (15, 899, False))
        state, token = schedule.poll(now=910)
        self.assertEqual(state, "started")
        self.assertEqual(schedule.display(now=911), (15, 899, True))
        self.assertTrue(schedule.finish(token, now=940))
        self.assertEqual(schedule.display(now=941), (15, 899, False))
        schedule.disarm()
        self.assertEqual(schedule.display(now=941), (None, None, False))


@unittest.skipUnless(os.name == "nt", "Windows file handles")
class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-synthetic-")
        self.root = Path(self.tmp.name)
        (self.root / "profiles").mkdir()
        (self.root / "profiles.yaml").write_text(
            "current: uid1\nitems:\n- uid: uid1\n  name: Test\n  type: local\n  file: test.yaml\n", encoding="utf-8")
        (self.root / "profiles" / "test.yaml").write_text(
            "proxies:\n- {name: example, type: socks5, server: 127.0.0.1, port: 1234}\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_and_change(self):
        registry = load_registry(self.root)
        snapshot = snapshot_profile(registry, registry.profiles[0])
        self.assertEqual(snapshot.names, ("example",))
        revalidate(registry, [snapshot])
        (self.root / "profiles" / "test.yaml").write_text("proxies: []\n", encoding="utf-8")
        with self.assertRaises(ProfileError):
            revalidate(registry, [snapshot])

    def test_duplicate_yaml_and_alias_rejected(self):
        for raw in (b"x: 1\nx: 2\n", b"x: &a 1\ny: *a\n", b"x: !!str hi\n", b"1: a\n"):
            with self.subTest(raw=raw), self.assertRaises(ProfileError):
                parse_yaml(raw)

    def test_yaml12_ss2022_and_ipv6_flow_scalar_compat_preserves_strict_rejects(self):
        valid_yaml = (
            b"proxies:\n"
            b"- {name: ss2022-node, type: ss, server: 1.2.3.4, port: 443, "
            b"cipher: 2022-blake3-aes-128-gcm, password: aaaa/bbb=:cccc+ddd=}\n"
            b"- {name: ipv6-node, type: vmess, server: 2001:db8::1, port: 443}\n"
        )
        parsed = parse_yaml(valid_yaml)
        self.assertEqual(parsed["proxies"][0]["password"], "aaaa/bbb=:cccc+ddd=")
        self.assertEqual(parsed["proxies"][1]["server"], "2001:db8::1")
        self.assertEqual(inspect_nodes(valid_yaml), ("ss2022-node", "ipv6-node"))

        for bad in (
            b"proxies:\n- &a {name: ss2022, password: aaaa/bbb=:cccc+ddd=}\n- *a\n",
            b"proxies:\n- {name: !!str ss2022, password: aaaa/bbb=:cccc+ddd=}\n",
            b"proxies:\n- {name: ss2022, name: dup, password: aaaa/bbb=:cccc+ddd=}\n",
            b"proxies:\n- {name: ss2022, password: aaaa/bbb=:cccc+ddd=}\nproxies:\n- {name: dup}\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(ProfileError):
                parse_yaml(bad)

    def test_reject_provider_and_duplicate_names(self):
        self.assertEqual(
            inspect_nodes(b"rule-providers: {ads: {type: http}}\nproxies:\n- {name: A}\n"),
            ("A",),
        )
        for raw in (b"proxy-providers: {}\nproxies: []\n",
                    b"proxies:\n- {name: A}\n- {name: A}\n"):
            with self.subTest(raw=raw), self.assertRaises(ProfileError):
                inspect_nodes(raw)

    def test_bad_filename_and_hardlink_rejected(self):
        path = self.root / "profiles.yaml"
        path.write_text("items:\n- {uid: u, name: n, type: local, file: '../bad'}\n", encoding="utf-8")
        with self.assertRaises(ProfileError):
            load_registry(self.root)
        os.link(self.root / "profiles" / "test.yaml", self.root / "profiles" / "hardlink.yaml")
        path.write_text("items:\n- {uid: u1, name: n, type: local, file: test.yaml}\n"
                        "- {uid: u2, name: n, type: local, file: hardlink.yaml}\n", encoding="utf-8")
        with self.assertRaises(ProfileError):
            load_registry(self.root)

    def test_reparse_profile_rejected(self):
        target = self.root / "elsewhere.yaml"
        target.write_text("proxies: []\n", encoding="utf-8")
        link = self.root / "profiles" / "link.yaml"
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest("creating synthetic symlinks is not permitted")
        (self.root / "profiles.yaml").write_text(
            "items:\n- {uid: u, name: n, type: local, file: link.yaml}\n", encoding="utf-8")
        with self.assertRaises(ProfileError):
            load_registry(self.root)

    def test_junction_directory_rejected(self):
        other = self.root / "other-root"
        other.mkdir()
        (other / "profiles.yaml").write_text("items: []\n", encoding="utf-8")
        link = other / "profiles"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link),
                                 str(self.root / "profiles")], capture_output=True)
        if result.returncode != 0:
            self.skipTest("creating synthetic junctions is not permitted")
        try:
            with self.assertRaises(ProfileError):
                load_registry(other)
        finally:
            os.rmdir(link)


if __name__ == "__main__":
    unittest.main()
