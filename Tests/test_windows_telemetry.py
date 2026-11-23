"""Unit tests for Windows SQLite telemetry history, patrol planner, and health decay."""

from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    from Windows.telemetry_history import (
        TelemetryHealthEvent,
        TelemetryHistoryStore,
        TelemetrySampleRecord,
        telemetry_compute_health_state,
        telemetry_compute_summaries,
        telemetry_is_peak_hour,
        telemetry_patrol_bytes_used_today,
        telemetry_payload_weight,
        telemetry_select_patrol_identities,
        telemetry_weighted_quantile,
    )


@unittest.skipUnless(os.name == "nt", "Windows telemetry history implementation")
class TelemetryHistoryTests(unittest.TestCase):
    def test_peak_hour_and_payload_weights(self):
        t_before = datetime(2026, 11, 15, 19, 59, 0).timestamp()
        t_start = datetime(2026, 11, 15, 20, 0, 0).timestamp()
        t_mid = datetime(2026, 11, 15, 22, 15, 0).timestamp()
        t_end = datetime(2026, 11, 15, 23, 30, 0).timestamp()
        self.assertFalse(telemetry_is_peak_hour(t_before))
        self.assertTrue(telemetry_is_peak_hour(t_start))
        self.assertTrue(telemetry_is_peak_hour(t_mid))
        self.assertFalse(telemetry_is_peak_hour(t_end))

        self.assertEqual(telemetry_payload_weight(20 * 1_048_576), 1.0)
        self.assertEqual(telemetry_payload_weight(3 * 1_048_576), 0.75)
        self.assertEqual(telemetry_payload_weight(1 * 1_048_576), 0.45)
        self.assertEqual(telemetry_payload_weight(256 * 1024), 0.30)

    def test_weighted_quantile_interpolation(self):
        self.assertAlmostEqual(telemetry_weighted_quantile([(10, 1), (30, 1)], 0.5), 20.0)
        self.assertAlmostEqual(telemetry_weighted_quantile([(10, 1), (30, 3)], 0.5), 25.0, places=4)
        self.assertAlmostEqual(telemetry_weighted_quantile([(10, 1), (20, 2), (40, 1)], 0.25), 13.3333333, places=4)
        self.assertEqual(telemetry_weighted_quantile([], 0.5), 0.0)

    def test_health_penalty_accumulation_decay_and_reset(self):
        now = 1_700_000_000.0
        events_one = [TelemetryHealthEvent(now - 60, "u1", "A", "G", ok=False)]
        s1 = telemetry_compute_health_state(events_one, [], now)
        self.assertEqual(s1.penalty, 0.0)
        self.assertGreater(s1.streak, 0.9)

        events_two = [
            TelemetryHealthEvent(now - 120, "u1", "A", "G", ok=False),
            TelemetryHealthEvent(now - 60, "u1", "A", "G", ok=False),
        ]
        s2 = telemetry_compute_health_state(events_two, [], now)
        self.assertGreater(s2.penalty, 5.0)
        self.assertLessEqual(s2.penalty, 25.0)

        # A speedtest success resets consecutive failure streak completely.
        s_reset = telemetry_compute_health_state(events_two, [now - 10], now)
        self.assertEqual(s_reset.penalty, 0.0)
        self.assertEqual(s_reset.streak, 0.0)

        # Health OK reduces streak substantially.
        events_recover = events_two + [TelemetryHealthEvent(now - 30, "u1", "A", "G", ok=True, delay_ms=90.0)]
        s_rec = telemetry_compute_health_state(events_recover, [], now)
        self.assertEqual(s_rec.penalty, 0.0)

    def test_summaries_qualification_windows_and_bayesian_shrinkage(self):
        now = datetime(2026, 11, 15, 21, 0, 0).timestamp()
        samples = []
        # Node A: 3 peak-hour successes -> qualified
        for i in range(3):
            samples.append(TelemetrySampleRecord(
                timestamp=now - (i + 1) * 600,
                profile_uid="u1", profile_name="Sub1", node_name="NodeA", node_type="vless",
                source="manual", requested_bytes=3 * 1_048_576, streams=2,
                download_complete=True, download_bytes_per_second=10_000_000,
                latency_ms=40, jitter_ms=3, head_attempts=6, head_successes=6,
                composite_score=88.0, is_peak_hour=True,
            ))
        # Node B: only 2 successes -> unqualified even if higher raw score
        for i in range(2):
            samples.append(TelemetrySampleRecord(
                timestamp=now - (i + 1) * 300,
                profile_uid="u1", profile_name="Sub1", node_name="NodeB", node_type="ss",
                source="manual", requested_bytes=20 * 1_048_576, streams=4,
                download_complete=True, download_bytes_per_second=20_000_000,
                latency_ms=20, jitter_ms=1, head_attempts=6, head_successes=6,
                composite_score=96.0, is_peak_hour=False,
            ))
        res_7d = telemetry_compute_summaries(samples, [], window="7d", now=now)
        self.assertEqual([s.node_name for s in res_7d], ["NodeA", "NodeB"])
        self.assertTrue(res_7d[0].is_qualified)
        self.assertFalse(res_7d[1].is_qualified)
        # Peak window excludes NodeB (is_peak_hour=False)
        res_peak = telemetry_compute_summaries(samples, [], window="peak", now=now)
        self.assertEqual([s.node_name for s in res_peak], ["NodeA"])

    def test_patrol_quota_and_defender_explorer_selection(self):
        now = datetime(2026, 11, 15, 14, 0, 0).timestamp()
        candidates = [("u1", "N1"), ("u1", "N2"), ("u1", "N3"), ("u1", "N4")]
        samples = [
            # N1 has 3 samples (latest 2h ago > 90m defender cooldown) -> qualified defender
            TelemetrySampleRecord(
                timestamp=now - 10800, profile_uid="u1", profile_name="Sub1", node_name="N1",
                node_type="vless", source="manual", requested_bytes=3 * 1_048_576, streams=2,
                download_complete=True, download_bytes_per_second=8_000_000,
                latency_ms=50, jitter_ms=2, head_attempts=6, head_successes=6,
                composite_score=85.0,
            ),
            TelemetrySampleRecord(
                timestamp=now - 9000, profile_uid="u1", profile_name="Sub1", node_name="N1",
                node_type="vless", source="manual", requested_bytes=3 * 1_048_576, streams=2,
                download_complete=True, download_bytes_per_second=8_000_000,
                latency_ms=50, jitter_ms=2, head_attempts=6, head_successes=6,
                composite_score=85.0,
            ),
            TelemetrySampleRecord(
                timestamp=now - 7200, profile_uid="u1", profile_name="Sub1", node_name="N1",
                node_type="vless", source="patrol", requested_bytes=1_048_576, streams=2,
                download_complete=True, download_bytes_per_second=8_000_000,
                latency_ms=50, jitter_ms=2, head_attempts=6, head_successes=6,
                composite_score=85.0,
            ),
            TelemetrySampleRecord(
                timestamp=now - 1800, profile_uid="u1", profile_name="Sub1", node_name="N2",
                node_type="vless", source="manual", requested_bytes=3 * 1_048_576, streams=2,
                download_complete=True, download_bytes_per_second=6_000_000,
                latency_ms=60, jitter_ms=4, head_attempts=6, head_successes=6,
                composite_score=78.0,
            ),
        ]
        self.assertEqual(telemetry_patrol_bytes_used_today(samples, now=now), 1_048_576)
        plan = telemetry_select_patrol_identities(
            candidates, samples, [], daily_quota_bytes=4 * 1_048_576, max_per_tick=8, now=now,
        )
        # Remaining = 3 MiB -> 3 nodes: 1 qualified defender (N1) + 2 unsampled explorers (N3, N4)
        self.assertEqual(plan.defender_count, 1)
        self.assertEqual(plan.explorer_count, 2)
        self.assertEqual(plan.identities[0], ("u1", "N1"))
        self.assertEqual(set(plan.identities[1:]), {("u1", "N3"), ("u1", "N4")})

    def test_patrol_smart_optimizations_and_algorithm_fixes(self):
        overnight_ts = datetime(2026, 11, 15, 3, 0, 0).timestamp()
        day_ts = datetime(2026, 11, 15, 14, 0, 0).timestamp()
        peak_ts = datetime(2026, 11, 15, 20, 30, 0).timestamp()
        post_peak_ts = datetime(2026, 11, 15, 23, 45, 0).timestamp()

        def mk(ts, node, uid="u1", profile="Sub1", source="manual", req=3 * 1_048_576, ok=True, score=85.0):
            return TelemetrySampleRecord(
                timestamp=ts, profile_uid=uid, profile_name=profile, node_name=node,
                node_type="vless", source=source, requested_bytes=req, streams=2,
                download_complete=ok, download_bytes_per_second=(10_000_000.0 if ok else 0.0),
                latency_ms=(40.0 if ok else 0.0), jitter_ms=(2.0 if ok else 0.0),
                head_attempts=6, head_successes=(6 if ok else 0),
                composite_score=(score if ok else None),
            )

        # 1) Weighted availability qualification & saturated overnight health streak recovery
        wq_samples = [
            mk(day_ts - 22 * 86400, "Recovered", ok=False),
            mk(day_ts - 21 * 86400, "Recovered", ok=False),
            mk(day_ts - 20 * 86400, "Recovered", ok=False),
            mk(day_ts - 19 * 86400, "Recovered", ok=False),
            mk(day_ts - 18 * 86400, "Recovered", ok=False),
            mk(day_ts - 7200, "Recovered", score=86.0),
            mk(day_ts - 5400, "Recovered", score=87.0),
            mk(day_ts - 3600, "Recovered", score=88.0),
            mk(day_ts - 26 * 86400, "RecentlyDead", score=90.0),
            mk(day_ts - 25 * 86400, "RecentlyDead", score=90.0),
            mk(day_ts - 24 * 86400, "RecentlyDead", score=90.0),
            mk(day_ts - 23 * 86400, "RecentlyDead", score=90.0),
            mk(day_ts - 7200, "RecentlyDead", ok=False),
            mk(day_ts - 5400, "RecentlyDead", ok=False),
            mk(day_ts - 3600, "RecentlyDead", ok=False),
        ]
        wq = {s.node_name: s for s in telemetry_compute_summaries(wq_samples, [], window="30d", now=day_ts)}
        self.assertTrue(wq["Recovered"].is_qualified)
        self.assertFalse(wq["RecentlyDead"].is_qualified)

        ninety_fails = [
            TelemetryHealthEvent(timestamp=day_ts - (10800 - i * 120), profile_uid="u1", node_name="Recovered", group_name="PROXY", ok=False)
            for i in range(90)
        ]
        st_rec = telemetry_compute_health_state(
            ninety_fails + [TelemetryHealthEvent(timestamp=day_ts - 10, profile_uid="u1", node_name="Recovered", group_name="PROXY", ok=True, delay_ms=25.0)],
            [], day_ts,
        )
        self.assertEqual(st_rec.penalty, 0.0)

        # 2) Smart patrol: penalized flagship sorted by base_score, 90m defender rotation,
        #    strict cooldown & failure-backoff exclusion, multi-sub round-robin, cold-start graduation
        cands = [("u1", n) for n in ("FreshTop", "HourlyCooledTop", "ReadyTop", "PenalizedFlagship", "PenalizedMid", "Promising1", "DeadNode", "SteadyOld")]
        samples = [
            mk(day_ts - 7200, "FreshTop", score=96), mk(day_ts - 3600, "FreshTop", score=96), mk(day_ts - 120, "FreshTop", score=96),
            mk(day_ts - 14400, "HourlyCooledTop", score=94), mk(day_ts - 10800, "HourlyCooledTop", score=94), mk(day_ts - 3600, "HourlyCooledTop", score=94),
            mk(day_ts - 14400, "ReadyTop", score=89), mk(day_ts - 10800, "ReadyTop", score=89), mk(day_ts - 7200, "ReadyTop", score=89),
            mk(day_ts - 10800, "PenalizedFlagship", score=92), mk(day_ts - 7200, "PenalizedFlagship", score=92), mk(day_ts - 3600, "PenalizedFlagship", score=92),
            mk(day_ts - 10800, "PenalizedMid", score=74), mk(day_ts - 7200, "PenalizedMid", score=74), mk(day_ts - 3600, "PenalizedMid", score=74),
            mk(day_ts - 1800, "Promising1", score=84),
            mk(day_ts - 7200, "DeadNode", ok=False), mk(day_ts - 3600, "DeadNode", ok=False), mk(day_ts - 1800, "DeadNode", ok=False),
            mk(day_ts - 20000, "SteadyOld", score=62), mk(day_ts - 18000, "SteadyOld", score=62), mk(day_ts - 14000, "SteadyOld", score=62), mk(day_ts - 10800, "SteadyOld", score=62),
        ]
        pen_events = [
            TelemetryHealthEvent(timestamp=day_ts - (400 - i * 30), profile_uid="u1", node_name="PenalizedFlagship", group_name="PROXY", ok=False)
            for i in range(6)
        ] + [
            TelemetryHealthEvent(timestamp=day_ts - (300 - i * 30), profile_uid="u1", node_name="PenalizedMid", group_name="PROXY", ok=False)
            for i in range(2)
        ]
        plan = telemetry_select_patrol_identities(cands, samples, pen_events, daily_quota_bytes=50 * 1_048_576, max_per_tick=4, now=day_ts)
        names = [n for _, n in plan.identities]
        self.assertEqual(names[:2], ["PenalizedFlagship", "ReadyTop"])
        self.assertIn("Promising1", names)
        self.assertNotIn("FreshTop", names)
        self.assertNotIn("DeadNode", names)
        self.assertEqual((plan.skipped_backoff_count, plan.skipped_cooldown_count), (1, 1))

        wide = telemetry_select_patrol_identities(cands, samples, pen_events, daily_quota_bytes=50 * 1_048_576, max_per_tick=8, now=day_ts)
        wide_names = [n for _, n in wide.identities]
        self.assertEqual(len(wide_names), 6)
        self.assertNotIn("FreshTop", wide_names)
        self.assertNotIn("DeadNode", wide_names)

        # Multi-sub round-robin cold start + graduation assist
        multi_cands = [("a", "A1"), ("a", "A2"), ("a", "A3"), ("b", "B1"), ("b", "B2"), ("c", "C1")]
        rr = telemetry_select_patrol_identities(multi_cands, [], [], daily_quota_bytes=50 * 1_048_576, max_per_tick=4, now=day_ts)
        self.assertEqual([n for _, n in rr.identities], ["A1", "B1", "C1", "A2"])
        grad = telemetry_select_patrol_identities(
            multi_cands, [mk(day_ts - 3600, "A1", uid="a", score=86), mk(day_ts - 3600, "B1", uid="b", score=82)],
            [], daily_quota_bytes=50 * 1_048_576, max_per_tick=4, now=day_ts,
        )
        self.assertEqual(grad.defender_count, 0)
        self.assertEqual(set(n for _, n in grad.identities[:2]), {"A1", "B1"})
        self.assertEqual(set(n for _, n in grad.identities[2:]), {"C1", "A2"})

        # 3) Failed patrol probes charge 256 KiB each; peak reserve held at 14:00, unlocked at 20:30 & 23:45; overnight throttle at 03:00
        fail_patrols = [mk(day_ts - 600 - i * 10, "DeadNode", source="patrol", req=1_048_576, ok=False) for i in range(4)]
        self.assertEqual(telemetry_patrol_bytes_used_today(fail_patrols, now=day_ts), 1_048_576)
        spent = [mk(day_ts - 1000 - i * 10, "ReadyTop", source="patrol", req=1_048_576, score=85) for i in range(38)]
        day_def = telemetry_select_patrol_identities(multi_cands, spent, [], daily_quota_bytes=50 * 1_048_576, max_per_tick=8, now=day_ts, reserve_peak_ratio=0.25)
        self.assertTrue(not day_def.identities and day_def.deferred_for_peak_quota)
        peak_ok = telemetry_select_patrol_identities(multi_cands, spent, [], daily_quota_bytes=50 * 1_048_576, max_per_tick=8, now=peak_ts, reserve_peak_ratio=0.25)
        self.assertEqual(len(peak_ok.identities), 6)
        post_ok = telemetry_select_patrol_identities(multi_cands, spent, [], daily_quota_bytes=50 * 1_048_576, max_per_tick=8, now=post_peak_ts, reserve_peak_ratio=0.25)
        self.assertEqual(len(post_ok.identities), 6)
        night = telemetry_select_patrol_identities(multi_cands, [], [], daily_quota_bytes=50 * 1_048_576, max_per_tick=6, now=overnight_ts, reserve_peak_ratio=0.25, overnight_throttle=True)
        self.assertEqual((night.effective_max_per_tick, len(night.identities)), (3, 3))

    def test_sqlite_store_persistence_pruning_and_clear(self):
        with tempfile.TemporaryDirectory(prefix="clash-telemetry-") as tmp:
            now = [1_700_000_000.0]
            store = TelemetryHistoryStore(directory=Path(tmp), retention_days=30, now_provider=lambda: now[0])
            try:
                self.assertFalse(store.is_in_memory_fallback)
                self.assertTrue(Path(store.storage_path).is_file())
                # Old sample (>30d) should be pruned automatically
                store.record_sample(TelemetrySampleRecord(
                    timestamp=now[0] - 31 * 86400.0, profile_uid="u1", profile_name="S1",
                    node_name="Old", node_type="ss", source="manual", requested_bytes=1_048_576,
                    streams=2, download_complete=True, download_bytes_per_second=1_000_000,
                    latency_ms=30, jitter_ms=1, head_attempts=6, head_successes=6, composite_score=70.0,
                ))
                self.assertEqual(store.sample_count(), 0)
                store.record_sample(TelemetrySampleRecord(
                    timestamp=now[0] - 60.0, profile_uid="u1", profile_name="S1",
                    node_name="Fresh", node_type="vless", source="manual", requested_bytes=3 * 1_048_576,
                    streams=2, download_complete=True, download_bytes_per_second=5_000_000,
                    latency_ms=30, jitter_ms=1, head_attempts=6, head_successes=6, composite_score=82.0,
                ))
                store.record_health_event(TelemetryHealthEvent(
                    timestamp=now[0] - 30.0, profile_uid="u1", node_name="Fresh", group_name="Proxy", ok=True, delay_ms=35.0,
                ))
                self.assertEqual(store.sample_count(), 1)
                self.assertEqual(store.health_event_count(), 1)
                store.close()

                reopened = TelemetryHistoryStore(directory=Path(tmp), retention_days=30, now_provider=lambda: now[0])
                try:
                    self.assertEqual(reopened.sample_count(), 1)
                    reopened.clear_all()
                    self.assertEqual(reopened.sample_count(), 0)
                    self.assertEqual(reopened.health_event_count(), 0)
                finally:
                    reopened.close()
            finally:
                store.close()

        # Invalid network UNC path falls back cleanly to :memory:
        fallback_store = TelemetryHistoryStore(directory=Path(r"\\nonexistent-host\share\dir"))
        try:
            self.assertTrue(fallback_store.is_in_memory_fallback)
            self.assertIsNone(fallback_store.storage_path)
        finally:
            fallback_store.close()

        # Regular file passed as directory also falls back cleanly to :memory:
        with tempfile.TemporaryDirectory(prefix="clash-telemetry-file-") as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            file_fallback = TelemetryHistoryStore(directory=blocker)
            try:
                self.assertTrue(file_fallback.is_in_memory_fallback)
            finally:
                file_fallback.close()


if __name__ == "__main__":
    unittest.main()
