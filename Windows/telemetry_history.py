"""Local SQLite telemetry archive, long-term scoring, patrol planner, and health decay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

if os.name == "nt":
    import win32con
    import win32file

WINDOW_SPECS = {
    "24h": {"title": "24小时", "lookback": 24.0 * 3600.0, "half_life": 12.0 * 3600.0},
    "7d": {"title": "7天", "lookback": 7.0 * 86400.0, "half_life": 3.5 * 86400.0},
    "30d": {"title": "30天", "lookback": 30.0 * 86400.0, "half_life": 7.0 * 86400.0},
    "peak": {"title": "晚高峰", "lookback": 30.0 * 86400.0, "half_life": 7.0 * 86400.0},
}

SAMPLE_SOURCES = frozenset((
    "manual",
    "load_20m",
    "funnel_s1",
    "funnel_s2",
    "scheduled",
    "patrol",
    "retest",
))


@dataclass(frozen=True)
class TelemetrySampleRecord:
    timestamp: float
    profile_uid: str
    profile_name: str
    node_name: str
    node_type: str
    source: str
    requested_bytes: int
    streams: int
    download_complete: bool
    download_bytes_per_second: float
    latency_ms: float
    jitter_ms: float
    head_attempts: int
    head_successes: int
    composite_score: float | None
    is_peak_hour: bool | None = None
    id: int = 0

    def __post_init__(self):
        if self.is_peak_hour is None:
            object.__setattr__(self, "is_peak_hour", telemetry_is_peak_hour(self.timestamp))

    @property
    def identity(self):
        return (self.profile_uid, self.node_name)

    @property
    def is_success(self):
        return (
            bool(self.download_complete)
            and math.isfinite(self.download_bytes_per_second)
            and self.download_bytes_per_second > 0
            and self.composite_score is not None
            and math.isfinite(self.composite_score)
        )


@dataclass(frozen=True)
class TelemetryHealthEvent:
    timestamp: float
    profile_uid: str
    node_name: str
    group_name: str
    ok: bool
    delay_ms: float | None = None
    failure_kind: str | None = None
    id: int = 0

    @property
    def identity(self):
        return (self.profile_uid, self.node_name)


@dataclass(frozen=True)
class TelemetryHealthState:
    penalty: float
    streak: float
    last_check_at: float | None
    last_ok: bool | None


@dataclass(frozen=True)
class TelemetryNodeSummary:
    profile_uid: str
    profile_name: str
    node_name: str
    node_type: str
    long_term_score: float
    base_score: float
    health_penalty: float
    active_failure_streak: float
    weighted_median_score: float
    weighted_p20_score: float
    weighted_median_speed_bps: float
    weighted_median_latency_ms: float
    weighted_median_jitter_ms: float
    availability_rate: float
    weighted_availability_rate: float
    total_count: int
    success_count: int
    failure_count: int
    is_qualified: bool
    last_tested_at: float
    last_health_check_at: float | None
    last_health_ok: bool | None
    last_latency_ms: float | None
    latest_speed_bps: float | None
    latest_score: float | None

    @property
    def identity(self):
        return (self.profile_uid, self.node_name)


@dataclass(frozen=True)
class TelemetryPatrolSelection:
    identities: tuple
    defender_count: int
    explorer_count: int
    remaining_quota_bytes: int
    deferred_for_peak_quota: bool = False
    peak_reserved_bytes: int = 0
    skipped_backoff_count: int = 0
    skipped_cooldown_count: int = 0
    effective_max_per_tick: int = 0


def telemetry_is_peak_hour(timestamp):
    if not math.isfinite(timestamp):
        return False
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return False
    minutes = dt.hour * 60 + dt.minute
    return 20 * 60 <= minutes < (23 * 60 + 30)


def telemetry_is_before_peak_hour(timestamp):
    if not math.isfinite(timestamp):
        return False
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return False
    return (dt.hour * 60 + dt.minute) < (20 * 60)


def telemetry_is_overnight_hour(timestamp):
    if not math.isfinite(timestamp):
        return False
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return False
    minutes = dt.hour * 60 + dt.minute
    return (1 * 60) <= minutes < (7 * 60)


def telemetry_effective_patrol_batch_cap(max_per_tick, now=None, overnight_throttle=False):
    if max_per_tick <= 0:
        return 0
    ts = time.time() if now is None else float(now)
    if overnight_throttle and telemetry_is_overnight_hour(ts):
        return min(max_per_tick, max(2, max_per_tick // 2))
    return max_per_tick


def telemetry_payload_weight(requested_bytes):
    if requested_bytes >= 20 * 1_048_576:
        return 1.0
    if requested_bytes >= 3 * 1_048_576:
        return 0.75
    if requested_bytes >= 1 * 1_048_576:
        return 0.45
    return 0.30


def telemetry_weighted_quantile(pairs, quantile):
    q = min(1.0, max(0.0, float(quantile)))
    filtered = [
        (float(val), float(w))
        for val, w in pairs
        if math.isfinite(val) and math.isfinite(w) and w > 0
    ]
    if not filtered:
        return 0.0
    filtered.sort(key=lambda item: item[0])
    if len(filtered) == 1:
        return filtered[0][0]
    total_weight = sum(w for _, w in filtered)
    if total_weight <= 0:
        return filtered[0][0]

    midpoints = []
    cumulative = 0.0
    for _, w in filtered:
        midpoints.append((cumulative + w * 0.5) / total_weight)
        cumulative += w

    if q <= midpoints[0]:
        return filtered[0][0]
    if q >= midpoints[-1]:
        return filtered[-1][0]
    for i in range(1, len(filtered)):
        left_m = midpoints[i - 1]
        right_m = midpoints[i]
        if q <= right_m:
            span = right_m - left_m
            if span <= 1e-12:
                return filtered[i][0]
            frac = (q - left_m) / span
            return filtered[i - 1][0] + frac * (filtered[i][0] - filtered[i - 1][0])
    return filtered[-1][0]


def telemetry_compute_health_state(health_events, success_sample_timestamps, now):
    half_life = 24.0 * 3600.0
    decay_lambda = math.log(2.0) / half_life
    cutoff = now - 30.0 * 86400.0

    # kind: 0 = health_failure, 1 = health_ok, 2 = speed_success
    timeline = []
    last_check_at = None
    last_ok = None

    for event in health_events:
        ts = event.timestamp
        if math.isfinite(ts) and cutoff <= ts <= now + 60.0:
            timeline.append((ts, 1 if event.ok else 0))
            if last_check_at is None or ts >= last_check_at:
                last_check_at = ts
                last_ok = bool(event.ok)
    for ts in success_sample_timestamps:
        if math.isfinite(ts) and cutoff <= ts <= now + 60.0:
            timeline.append((ts, 2))

    timeline.sort(key=lambda item: (item[0], item[1]))
    streak = 0.0
    prev_ts = None
    for ts, kind in timeline:
        if prev_ts is not None and ts > prev_ts:
            streak *= math.exp(-decay_lambda * (ts - prev_ts))
        if kind == 0:
            # Saturate failure streak so prolonged overnight outages do not accumulate
            # unbounded debt that blocks verified recovery (max -25 penalty is reached at 5.0).
            streak = min(7.0, streak + 1.0)
        elif kind == 1:
            streak = max(0.0, streak * 0.35 - 0.8)
        else:
            streak = 0.0
        prev_ts = ts

    if prev_ts is not None and now > prev_ts:
        streak *= math.exp(-decay_lambda * (now - prev_ts))

    if streak >= 1.8:
        penalty = min(25.0, 5.0 + (streak - 1.8) * 6.25)
    else:
        penalty = 0.0
    return TelemetryHealthState(penalty=penalty, streak=streak, last_check_at=last_check_at, last_ok=last_ok)


def telemetry_compute_summaries(samples, health_events, window="7d", now=None):
    spec = WINDOW_SPECS.get(window)
    if spec is None:
        raise ValueError(f"invalid telemetry window: {window}")
    now = time.time() if now is None else float(now)
    cutoff = now - spec["lookback"]
    decay_lambda = math.log(2.0) / spec["half_life"]

    grouped_samples = {}
    for sample in samples:
        if not math.isfinite(sample.timestamp) or sample.timestamp < cutoff or sample.timestamp > now + 60.0:
            continue
        if window == "peak" and not sample.is_peak_hour:
            continue
        grouped_samples.setdefault(sample.identity, []).append(sample)
    if not grouped_samples:
        return []

    all_success_timestamps = {}
    for sample in samples:
        if sample.is_success:
            all_success_timestamps.setdefault(sample.identity, []).append(sample.timestamp)

    grouped_health = {}
    for event in health_events:
        grouped_health.setdefault(event.identity, []).append(event)

    intermediates = []
    for identity, node_samples in grouped_samples.items():
        sorted_by_time = sorted(node_samples, key=lambda s: s.timestamp)
        latest = sorted_by_time[-1]
        latest_success = next((s for s in reversed(sorted_by_time) if s.is_success), None)
        if latest_success is not None:
            display_type = latest_success.node_type
        else:
            display_type = "" if latest.node_type == "—" else latest.node_type

        score_pairs = []
        speed_pairs = []
        latency_pairs = []
        jitter_pairs = []
        total_weight = 0.0
        success_weight = 0.0
        success_count = 0

        for s in sorted_by_time:
            age = max(0.0, now - s.timestamp)
            time_weight = math.exp(-decay_lambda * age)
            payload_weight = telemetry_payload_weight(s.requested_bytes)
            peak_mult = 1.25 if (window != "peak" and s.is_peak_hour) else 1.0
            w = time_weight * payload_weight * peak_mult
            total_weight += w
            if s.is_success and s.composite_score is not None:
                success_count += 1
                success_weight += w
                score_pairs.append((s.composite_score, w))
                speed_pairs.append((s.download_bytes_per_second, w))
                latency_pairs.append((s.latency_ms, w))
                jitter_pairs.append((s.jitter_ms, w))

        total_count = len(sorted_by_time)
        failure_count = total_count - success_count
        availability_rate = success_count / total_count
        weighted_availability = min(1.0, max(0.0, success_weight / total_weight)) if total_weight > 0 else 0.0

        w_med_score = telemetry_weighted_quantile(score_pairs, 0.50) if success_count > 0 else 0.0
        w_p20_score = telemetry_weighted_quantile(score_pairs, 0.20) if success_count > 0 else 0.0
        core_quality = (0.60 * w_med_score + 0.40 * w_p20_score) if success_count > 0 else 0.0
        w_med_speed = telemetry_weighted_quantile(speed_pairs, 0.50) if success_count > 0 else 0.0
        w_med_latency = telemetry_weighted_quantile(latency_pairs, 0.50) if success_count > 0 else 0.0
        w_med_jitter = telemetry_weighted_quantile(jitter_pairs, 0.50) if success_count > 0 else 0.0

        intermediates.append({
            "identity": identity,
            "profile_uid": latest.profile_uid,
            "profile_name": latest.profile_name,
            "node_name": latest.node_name,
            "node_type": display_type,
            "success_count": success_count,
            "total_count": total_count,
            "failure_count": failure_count,
            "total_weight": total_weight,
            "success_weight": success_weight,
            "core_quality": core_quality,
            "weighted_median_score": w_med_score,
            "weighted_p20_score": w_p20_score,
            "weighted_median_speed": w_med_speed,
            "weighted_median_latency": w_med_latency,
            "weighted_median_jitter": w_med_jitter,
            "weighted_availability": weighted_availability,
            "availability_rate": availability_rate,
            "last_tested_at": latest.timestamp,
            "last_latency_ms": latest_success.latency_ms if latest_success else None,
            "latest_speed": latest_success.download_bytes_per_second if latest_success else None,
            "latest_score": latest_success.composite_score if latest_success else None,
        })

    valid_cores = sorted(item["core_quality"] for item in intermediates if item["success_count"] > 0)
    if not valid_cores:
        global_median_core = 50.0
    elif len(valid_cores) % 2 == 1:
        global_median_core = valid_cores[len(valid_cores) // 2]
    else:
        mid = len(valid_cores) // 2
        global_median_core = 0.5 * (valid_cores[mid - 1] + valid_cores[mid])
    prior = min(70.0, global_median_core)
    prior_weight = 1.2

    summaries = []
    for item in intermediates:
        if item["success_count"] > 0:
            shrunk = (item["success_weight"] * item["core_quality"] + prior_weight * prior) / (item["success_weight"] + prior_weight)
            base_score = max(0.0, min(100.0, shrunk * (item["weighted_availability"] ** 1.35)))
        else:
            base_score = 0.0

        health = telemetry_compute_health_state(
            grouped_health.get(item["identity"], ()),
            all_success_timestamps.get(item["identity"], ()),
            now,
        )
        final_score = max(0.0, min(100.0, base_score - health.penalty)) if item["success_count"] > 0 else 0.0
        qualified = (item["success_count"] >= 3 and item["weighted_availability"] >= 0.5)

        summaries.append(TelemetryNodeSummary(
            profile_uid=item["profile_uid"],
            profile_name=item["profile_name"],
            node_name=item["node_name"],
            node_type=item["node_type"],
            long_term_score=final_score,
            base_score=base_score,
            health_penalty=health.penalty if item["success_count"] > 0 else 0.0,
            active_failure_streak=health.streak,
            weighted_median_score=item["weighted_median_score"],
            weighted_p20_score=item["weighted_p20_score"],
            weighted_median_speed_bps=item["weighted_median_speed"],
            weighted_median_latency_ms=item["weighted_median_latency"],
            weighted_median_jitter_ms=item["weighted_median_jitter"],
            availability_rate=item["availability_rate"],
            weighted_availability_rate=item["weighted_availability"],
            total_count=item["total_count"],
            success_count=item["success_count"],
            failure_count=item["failure_count"],
            is_qualified=qualified,
            last_tested_at=item["last_tested_at"],
            last_health_check_at=health.last_check_at,
            last_health_ok=health.last_ok,
            last_latency_ms=item["last_latency_ms"],
            latest_speed_bps=item["latest_speed"],
            latest_score=item["latest_score"],
        ))

    summaries.sort(
        key=lambda s: (
            0 if s.is_qualified else 1,
            -round(s.long_term_score, 9),
            -s.success_count,
            -round(s.weighted_median_speed_bps, 6),
            -s.last_tested_at,
            s.profile_uid.encode("utf-8"),
            s.node_name.encode("utf-8"),
        )
    )
    return summaries


def telemetry_patrol_bytes_used_today(samples, now=None):
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        return 0
    dt = datetime.fromtimestamp(now)
    start_of_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = start_of_day + timedelta(days=1)
    start_ts = start_of_day.timestamp()
    end_ts = next_day.timestamp()
    total = 0
    for s in samples:
        if s.source == "patrol" and start_ts <= s.timestamp < end_ts:
            req = max(0, int(s.requested_bytes))
            if s.is_success:
                total += req
            else:
                # Failed or timed-out probes never completed the full download;
                # charge a bounded anti-spin cost (<= 256 KiB) instead of full 1 MiB.
                total += min(req, 262_144)
    return total


def telemetry_select_patrol_identities(
    candidates,
    samples,
    health_events,
    daily_quota_bytes,
    per_node_bytes=1_048_576,
    max_per_tick=8,
    now=None,
    reserve_peak_ratio=0.0,
    defender_cooldown_seconds=5400.0,
    overnight_throttle=False,
):
    now = time.time() if now is None else float(now)
    used_today = telemetry_patrol_bytes_used_today(samples, now=now)
    remaining = max(0, int(daily_quota_bytes) - used_today)
    clamped_reserve = min(0.5, max(0.0, float(reserve_peak_ratio)))
    is_peak_now = telemetry_is_peak_hour(now)
    is_before_peak = telemetry_is_before_peak_hour(now)
    peak_reserved_bytes = (
        int(max(0, int(daily_quota_bytes)) * clamped_reserve)
        if (is_before_peak and clamped_reserve > 0)
        else 0
    )
    spendable_now = max(0, remaining - peak_reserved_bytes)
    effective_batch_cap = telemetry_effective_patrol_batch_cap(
        max_per_tick, now=now, overnight_throttle=overnight_throttle
    )

    if per_node_bytes <= 0 or effective_batch_cap <= 0:
        return TelemetryPatrolSelection(
            (), 0, 0, remaining,
            deferred_for_peak_quota=False,
            peak_reserved_bytes=peak_reserved_bytes,
            effective_max_per_tick=effective_batch_cap,
        )

    unique_candidates = []
    seen = set()
    profile_order_map = {}
    profile_count_map = {}
    round_robin_rank = {}
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        unique_candidates.append(item)
        p_uid = item[0]
        if p_uid not in profile_order_map:
            profile_order_map[p_uid] = len(profile_order_map)
        within = profile_count_map.get(p_uid, 0)
        profile_count_map[p_uid] = within + 1
        round_robin_rank[item] = (within, profile_order_map[p_uid])

    affordable = spendable_now // per_node_bytes
    target_count = min(effective_batch_cap, affordable, len(unique_candidates))
    if target_count <= 0:
        deferred = (remaining >= per_node_bytes and spendable_now < per_node_bytes and peak_reserved_bytes > 0)
        return TelemetryPatrolSelection(
            (), 0, 0, remaining,
            deferred_for_peak_quota=deferred,
            peak_reserved_bytes=peak_reserved_bytes,
            effective_max_per_tick=effective_batch_cap,
        )

    dt_now = datetime.fromtimestamp(now)
    today_peak_start_ts = dt_now.replace(hour=20, minute=0, second=0, microsecond=0).timestamp()

    seven_day_cutoff = now - 7.0 * 86400.0
    samples_by_id = {}
    last_sample_by_id = {}
    has_recent_peak_sample = set()
    for s in samples:
        if not math.isfinite(s.timestamp) or s.timestamp > now + 60.0:
            continue
        ident = s.identity
        if ident not in seen:
            continue
        samples_by_id.setdefault(ident, []).append(s)
        prev = last_sample_by_id.get(ident)
        if prev is None or s.timestamp > prev:
            last_sample_by_id[ident] = s.timestamp
        if s.is_peak_hour and s.timestamp >= seven_day_cutoff:
            has_recent_peak_sample.add(ident)

    consecutive_failures = {}
    for ident, slist in samples_by_id.items():
        ordered = sorted(slist, key=lambda x: x.timestamp, reverse=True)
        streak = 0
        for item in ordered:
            if item.is_success:
                break
            streak += 1
        if streak > 0:
            consecutive_failures[ident] = streak

    def is_in_failure_backoff(ident):
        streak = consecutive_failures.get(ident, 0)
        last_ts = last_sample_by_id.get(ident)
        if streak < 2 or last_ts is None:
            return False
        backoff_hours = min(24.0, (2.0 ** (streak - 2)) * 2.0)
        return (now - last_ts) < (backoff_hours * 3600.0)

    summaries_7d = [
        s for s in telemetry_compute_summaries(samples, health_events, window="7d", now=now)
        if s.identity in seen and s.success_count > 0
    ]
    summary_7d_by_id = {s.identity: s for s in summaries_7d}

    defender_cap = 0 if target_count <= 1 else min(4, target_count // 2)
    defenders = []
    picked = set()

    effective_cooldown = max(0.0, float(defender_cooldown_seconds))
    recheck_cooldown = min(effective_cooldown, 300.0)
    explorer_cooldown = min(effective_cooldown, 900.0)
    graduation_cooldown = min(effective_cooldown, 1200.0)
    defender_slot_cooldown = min(effective_cooldown, 2700.0) if is_peak_now else effective_cooldown

    # Priority 1: up to 1 fast re-verification slot for the highest base-score node with active
    # passive health penalty (sorted by base_score desc so heavy penalties don't hide flagship nodes).
    if defender_cap > 0:
        penalized = [
            s for s in summaries_7d
            if s.health_penalty > 0 and s.base_score >= 50.0 and not is_in_failure_backoff(s.identity)
            and (now - last_sample_by_id.get(s.identity, s.last_tested_at)) >= recheck_cooldown
        ]
        penalized.sort(
            key=lambda s: (
                -round(s.base_score, 6),
                -round(s.health_penalty, 6),
                last_sample_by_id.get(s.identity, s.last_tested_at),
            )
        )
        if penalized:
            top_pen = penalized[0].identity
            picked.add(top_pen)
            defenders.append(top_pen)

    # Priority 2: qualified 7d leaders outside cooldown (evening peak bypasses daytime cooldown on first check).
    for summary in summaries_7d:
        if len(defenders) >= defender_cap:
            break
        if not summary.is_qualified:
            continue
        ident = summary.identity
        if ident in picked or is_in_failure_backoff(ident):
            continue
        last_ts = last_sample_by_id.get(ident, summary.last_tested_at)
        needs_first_peak = is_peak_now and last_ts < today_peak_start_ts
        required_gap = recheck_cooldown if needs_first_peak else defender_slot_cooldown
        if required_gap > 0 and (now - last_ts) < required_gap:
            continue
        picked.add(ident)
        defenders.append(ident)

    # Explorer selection with strict backoff & cooldown enforcement + multi-sub round-robin + graduation assist
    skipped_backoff = 0
    skipped_cooldown = 0
    tier0 = []
    tier1 = []
    tier2 = []

    for offset, ident in enumerate(unique_candidates):
        if ident in picked:
            continue
        within_p, p_order = round_robin_rank.get(ident, (offset, 0))
        last_ts = last_sample_by_id.get(ident)
        if last_ts is None:
            tier0.append((within_p, p_order, offset, ident))
            continue
        if is_in_failure_backoff(ident):
            skipped_backoff += 1
            continue
        needs_first_peak = is_peak_now and last_ts < today_peak_start_ts
        s7 = summary_7d_by_id.get(ident)
        if (
            s7 is not None
            and not s7.is_qualified
            and 1 <= s7.success_count < 3
            and s7.weighted_availability_rate >= 0.5
        ):
            req_gap = recheck_cooldown if needs_first_peak else graduation_cooldown
            if req_gap > 0 and (now - last_ts) < req_gap:
                skipped_cooldown += 1
                continue
            needs_peak = is_peak_now and (ident not in has_recent_peak_sample)
            tier1.append((0 if needs_peak else 1, -round(s7.base_score, 6), last_ts, offset, s7.base_score, ident))
            continue
        req_gap = recheck_cooldown if needs_first_peak else explorer_cooldown
        if req_gap > 0 and (now - last_ts) < req_gap:
            skipped_cooldown += 1
            continue
        needs_peak = is_peak_now and (ident not in has_recent_peak_sample)
        tier2.append((0 if needs_peak else 1, last_ts, offset, ident))

    tier0.sort(key=lambda x: (x[0], x[1], x[2]))
    tier1.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    tier2.sort(key=lambda x: (x[0], x[1], x[2]))

    explorers = []
    remaining_slots = max(0, target_count - len(defenders))

    unfilled_defenders = max(0, defender_cap - len(defenders))
    if unfilled_defenders > 0 and remaining_slots > 0 and tier0:
        grad_budget = min(unfilled_defenders, max(1, remaining_slots // 2))
        grad_used = 0
        for item in tier1:
            if grad_used >= grad_budget or len(explorers) >= remaining_slots:
                break
            if item[4] >= 55.0 and item[5] not in picked:
                picked.add(item[5])
                explorers.append(item[5])
                grad_used += 1

    for pool, idx_pos in ((tier0, 3), (tier1, 5), (tier2, 3)):
        if len(explorers) >= remaining_slots:
            break
        for item in pool:
            if len(explorers) >= remaining_slots:
                break
            ident = item[idx_pos]
            if ident not in picked:
                picked.add(ident)
                explorers.append(ident)

    return TelemetryPatrolSelection(
        identities=tuple(defenders + explorers),
        defender_count=len(defenders),
        explorer_count=len(explorers),
        remaining_quota_bytes=remaining,
        deferred_for_peak_quota=False,
        peak_reserved_bytes=peak_reserved_bytes,
        skipped_backoff_count=skipped_backoff,
        skipped_cooldown_count=skipped_cooldown,
        effective_max_per_tick=effective_batch_cap,
    )


def _is_reparse(path):
    if os.name != "nt":
        return path.is_symlink()
    attrs = win32file.GetFileAttributesW(str(path))
    if attrs == -1:
        return False
    return bool(attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_prepare_sqlite_path(directory):
    try:
        if not directory:
            return None
        target = Path(os.path.abspath(str(directory)))
        drive = os.path.splitdrive(str(target))[0]
        if os.name == "nt" and (not drive or drive.startswith("\\")):
            return None
        # Check all existing ancestors for reparse points before creating anything.
        probe = target
        to_create = []
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                return None
            to_create.append(probe)
            probe = parent
        curr = probe
        while True:
            if _is_reparse(curr):
                return None
            if curr.parent == curr:
                break
            curr = curr.parent
        if not probe.is_dir():
            return None
        for part in reversed(to_create):
            part.mkdir(exist_ok=True)
            if _is_reparse(part) or not part.is_dir():
                return None
        db_path = target / "telemetry-v1.sqlite"
        for name in ("telemetry-v1.sqlite", "telemetry-v1.sqlite-wal", "telemetry-v1.sqlite-shm", "telemetry-v1.sqlite-journal"):
            candidate = target / name
            if candidate.exists() and (_is_reparse(candidate) or candidate.is_dir()):
                return None
        return str(db_path)
    except Exception:
        return None


def _safe_text(value, max_bytes=4096):
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8", "strict")) <= max_bytes
    except UnicodeError:
        return False


class TelemetryHistoryStore:
    """Thread-safe local SQLite archive with automatic 30-day pruning."""

    def __init__(self, directory=None, retention_days=30, now_provider=None):
        self.retention_days = max(1, int(retention_days))
        self.now_provider = now_provider or time.time
        self._lock = threading.RLock()
        if directory is None:
            directory = self._default_directory()
        db_path = _safe_prepare_sqlite_path(directory)
        conn = None
        if db_path is not None:
            try:
                conn = sqlite3.connect(db_path, timeout=2.0, check_same_thread=False)
                self.storage_path = db_path
                self.is_in_memory_fallback = False
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
        if conn is None:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.storage_path = None
            self.is_in_memory_fallback = True
        self._conn = conn
        with self._lock:
            self._configure_and_prune()

    @staticmethod
    def _default_directory():
        override = os.environ.get("CLASH_SPEED_HELPER_TELEMETRY_DIR", "").strip()
        if override:
            return Path(override)
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "ClashSpeedHelper"
        return None

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _configure_and_prune(self):
        if self._conn is None:
            return
        self._conn.execute("PRAGMA busy_timeout=2000;")
        self._conn.execute("PRAGMA journal_mode=DELETE;")
        self._conn.execute("PRAGMA secure_delete=ON;")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                profile_uid TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                node_name TEXT NOT NULL,
                node_type TEXT NOT NULL,
                source TEXT NOT NULL,
                requested_bytes INTEGER NOT NULL,
                streams INTEGER NOT NULL,
                download_complete INTEGER NOT NULL,
                bps REAL NOT NULL,
                latency_ms REAL NOT NULL,
                jitter_ms REAL NOT NULL,
                head_attempts INTEGER NOT NULL,
                head_successes INTEGER NOT NULL,
                composite_score REAL,
                is_peak_hour INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON telemetry_samples(ts);
            CREATE INDEX IF NOT EXISTS idx_samples_identity_ts ON telemetry_samples(profile_uid, node_name, ts);
            CREATE TABLE IF NOT EXISTS health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                profile_uid TEXT NOT NULL,
                node_name TEXT NOT NULL,
                group_name TEXT NOT NULL,
                ok INTEGER NOT NULL,
                delay_ms REAL,
                failure_kind TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_health_ts ON health_events(ts);
            CREATE INDEX IF NOT EXISTS idx_health_identity_ts ON health_events(profile_uid, node_name, ts);
        """)
        self._prune_locked(self.now_provider() - self.retention_days * 86400.0)
        self._conn.commit()

    def _prune_locked(self, cutoff):
        if self._conn is None or not math.isfinite(cutoff):
            return
        self._conn.execute("DELETE FROM telemetry_samples WHERE ts < ?;", (cutoff,))
        self._conn.execute("DELETE FROM health_events WHERE ts < ?;", (cutoff,))

    def record_sample(self, sample):
        with self._lock:
            if (self._conn is None or not math.isfinite(sample.timestamp) or
                    not sample.profile_uid or not sample.node_name or
                    sample.source not in SAMPLE_SOURCES or
                    not _safe_text(sample.profile_uid) or not _safe_text(sample.profile_name) or
                    not _safe_text(sample.node_name) or not _safe_text(sample.node_type)):
                return
            score = float(sample.composite_score) if sample.composite_score is not None and math.isfinite(sample.composite_score) else None
            bps = max(0.0, float(sample.download_bytes_per_second)) if math.isfinite(sample.download_bytes_per_second) else 0.0
            lat = max(0.0, float(sample.latency_ms)) if math.isfinite(sample.latency_ms) else 0.0
            jit = max(0.0, float(sample.jitter_ms)) if math.isfinite(sample.jitter_ms) else 0.0
            self._conn.execute(
                """INSERT INTO telemetry_samples (
                    ts, profile_uid, profile_name, node_name, node_type, source,
                    requested_bytes, streams, download_complete, bps, latency_ms,
                    jitter_ms, head_attempts, head_successes, composite_score, is_peak_hour
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                (
                    float(sample.timestamp), sample.profile_uid, sample.profile_name,
                    sample.node_name, sample.node_type, sample.source,
                    max(0, int(sample.requested_bytes)), max(0, int(sample.streams)),
                    1 if sample.download_complete else 0, bps, lat, jit,
                    max(0, int(sample.head_attempts)), max(0, int(sample.head_successes)),
                    score, 1 if sample.is_peak_hour else 0,
                ),
            )
            self._prune_locked(self.now_provider() - self.retention_days * 86400.0)
            self._conn.commit()

    def record_health_event(self, event):
        with self._lock:
            if (self._conn is None or not math.isfinite(event.timestamp) or
                    not event.profile_uid or not event.node_name or
                    not _safe_text(event.profile_uid) or not _safe_text(event.node_name) or
                    not _safe_text(event.group_name)):
                return
            delay = float(event.delay_ms) if event.delay_ms is not None and math.isfinite(event.delay_ms) else None
            kind = event.failure_kind if event.failure_kind and _safe_text(event.failure_kind, 128) else None
            self._conn.execute(
                """INSERT INTO health_events (
                    ts, profile_uid, node_name, group_name, ok, delay_ms, failure_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?);""",
                (
                    float(event.timestamp), event.profile_uid, event.node_name,
                    event.group_name, 1 if event.ok else 0, delay, kind,
                ),
            )
            self._prune_locked(self.now_provider() - self.retention_days * 86400.0)
            self._conn.commit()

    def _load_samples_locked(self, cutoff):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            """SELECT id, ts, profile_uid, profile_name, node_name, node_type, source,
                      requested_bytes, streams, download_complete, bps, latency_ms,
                      jitter_ms, head_attempts, head_successes, composite_score, is_peak_hour
               FROM telemetry_samples WHERE ts >= ? ORDER BY ts ASC, id ASC;""",
            (cutoff,),
        ).fetchall()
        out = []
        for row in rows:
            if row[6] not in SAMPLE_SOURCES:
                continue
            out.append(TelemetrySampleRecord(
                id=int(row[0]), timestamp=float(row[1]), profile_uid=row[2],
                profile_name=row[3], node_name=row[4], node_type=row[5],
                source=row[6], requested_bytes=int(row[7]), streams=int(row[8]),
                download_complete=bool(row[9]), download_bytes_per_second=float(row[10]),
                latency_ms=float(row[11]), jitter_ms=float(row[12]),
                head_attempts=int(row[13]), head_successes=int(row[14]),
                composite_score=None if row[15] is None else float(row[15]),
                is_peak_hour=bool(row[16]),
            ))
        return out

    def _load_health_events_locked(self, cutoff):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            """SELECT id, ts, profile_uid, node_name, group_name, ok, delay_ms, failure_kind
               FROM health_events WHERE ts >= ? ORDER BY ts ASC, id ASC;""",
            (cutoff,),
        ).fetchall()
        return [
            TelemetryHealthEvent(
                id=int(row[0]), timestamp=float(row[1]), profile_uid=row[2],
                node_name=row[3], group_name=row[4], ok=bool(row[5]),
                delay_ms=None if row[6] is None else float(row[6]), failure_kind=row[7],
            )
            for row in rows
        ]

    def compute_summaries(self, window="7d", now=None):
        with self._lock:
            effective_now = self.now_provider() if now is None else float(now)
            cutoff = effective_now - self.retention_days * 86400.0
            return telemetry_compute_summaries(
                self._load_samples_locked(cutoff),
                self._load_health_events_locked(cutoff),
                window=window,
                now=effective_now,
            )

    def select_patrol_plan(
        self,
        candidates,
        daily_quota_bytes,
        per_node_bytes=1_048_576,
        max_per_tick=8,
        now=None,
        reserve_peak_ratio=0.0,
        defender_cooldown_seconds=5400.0,
        overnight_throttle=False,
    ):
        with self._lock:
            effective_now = self.now_provider() if now is None else float(now)
            cutoff = effective_now - self.retention_days * 86400.0
            return telemetry_select_patrol_identities(
                candidates,
                self._load_samples_locked(cutoff),
                self._load_health_events_locked(cutoff),
                daily_quota_bytes=daily_quota_bytes,
                per_node_bytes=per_node_bytes,
                max_per_tick=max_per_tick,
                now=effective_now,
                reserve_peak_ratio=reserve_peak_ratio,
                defender_cooldown_seconds=defender_cooldown_seconds,
                overnight_throttle=overnight_throttle,
            )

    def patrol_bytes_used_today(self, now=None):
        with self._lock:
            effective_now = self.now_provider() if now is None else float(now)
            cutoff = effective_now - self.retention_days * 86400.0
            return telemetry_patrol_bytes_used_today(self._load_samples_locked(cutoff), now=effective_now)

    def health_state(self, profile_uid, node_name, now=None):
        with self._lock:
            effective_now = self.now_provider() if now is None else float(now)
            cutoff = effective_now - self.retention_days * 86400.0
            identity = (profile_uid, node_name)
            success_ts = [s.timestamp for s in self._load_samples_locked(cutoff) if s.identity == identity and s.is_success]
            events = [e for e in self._load_health_events_locked(cutoff) if e.identity == identity]
            return telemetry_compute_health_state(events, success_ts, effective_now)

    def sample_count(self):
        with self._lock:
            if self._conn is None:
                return 0
            return int(self._conn.execute("SELECT COUNT(*) FROM telemetry_samples;").fetchone()[0])

    def health_event_count(self):
        with self._lock:
            if self._conn is None:
                return 0
            return int(self._conn.execute("SELECT COUNT(*) FROM health_events;").fetchone()[0])

    def prune(self, older_than):
        with self._lock:
            self._prune_locked(float(older_than))
            if self._conn is not None:
                self._conn.commit()

    def clear_all(self):
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM telemetry_samples;")
            self._conn.execute("DELETE FROM health_events;")
            self._conn.commit()
            self._conn.execute("VACUUM;")
