"""Desktop application state and explicit, validated webview bridge."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import csv
import inspect
import io
import threading
import time
import win32clipboard

from .batch import Job, MultiBatchRunner
from .controller import (AssistedSwitch, ControllerError, PipeController, ProbeError, ProxyGroup,
                         SwitchLatch, read_settings, read_tun_enabled)
from .profile_registry import ProfileError, default_root, load_registry, revalidate, snapshot_profile
from .runtime_bridge import (
    WindowsRuntimeBridge,
    disable_webview2_cdp_policy,
    enable_webview2_cdp_policy,
)
from .runtime_broker import (
    RuntimeBridgeUnavailable,
    RuntimeBrokerError,
    RuntimeSwitchCancelled,
    RuntimeSwitchOutcomeUnknown,
)
from .schedule import TestSchedule
from .settings import AppSettingsStore, SettingsSecurityError
from .telemetry_history import (TelemetryHealthEvent, TelemetryHistoryStore,
                                TelemetrySampleRecord, WINDOW_SPECS)

_METADATA = (
    "剩余流量", "已用流量", "到期", "流量重置", "重置时间", "距离下次重置", "下次重置",
    "过期时间", "订阅信息", "订阅地址", "更新订阅", "最新网址", "最新域名", "官方网址", "官网",
    "防失联", "套餐余量",
    "traffic remaining", "remaining traffic", "used traffic",
    "subscription expires", "expiration date", "expire date", "next reset",
)
_PATROL_QUOTAS = frozenset((50 * 1_048_576, 80 * 1_048_576, 150 * 1_048_576, 300 * 1_048_576))
_PATROL_BATCH_BY_QUOTA = {
    50 * 1_048_576: 4,
    80 * 1_048_576: 5,
    150 * 1_048_576: 6,
    300 * 1_048_576: 8,
}
_PRIMARY_SELECTOR_KEYS = frozenset((
    "proxy", "proxies", "select", "selector", "nodeselect", "proxyselect", "main", "default", "manual",
    "节点选择", "手动切换", "手动选择", "节点切换", "选择节点", "代理选择", "手动节点",
    "代理", "主节点", "主代理", "默认", "默认节点", "默认代理", "国外流量", "国外代理", "节点", "全局选择",
))


@dataclass
class Row:
    uid: str
    profile: str
    name: str
    selected: bool = True
    state: str = "待测速"
    measurement: object = None
    stage1_score: object = None
    node_type: str = ""
    samples: list = field(default_factory=list)
    eligible: bool = field(init=False)

    def __post_init__(self):
        self.eligible = _eligible_name(self.name)


def _registry_signature(registry):
    return tuple((p.uid, p.name, p.filename, p.identity) for p in registry.profiles)


def _cell(value):
    value = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _eligible_name(name):
    lowered = name.lower()
    return not any(term in lowered for term in _METADATA)


class Desktop:
    def __init__(self, executable, root=None, telemetry_dir=None, settings_dir=None,
                 runtime_bridge=None):
        self.executable = Path(executable)
        self.root = Path(root) if root else default_root()
        self.lock = threading.RLock()
        if telemetry_dir is None and root is not None:
            telemetry_dir = Path(root) / ".telemetry"
        if settings_dir is None:
            settings_dir = telemetry_dir
        self.telemetry_store = TelemetryHistoryStore(directory=telemetry_dir)
        self.settings_store = AppSettingsStore(directory=settings_dir)
        self.minimize_to_tray = self.settings_store.load_minimize_to_tray(default=True)
        self.preferences = self.settings_store.load_preferences()
        self.window_visible = True
        self.quit_requested = False
        self._switching_count = 0
        self._window_show_cb = None
        self._window_hide_cb = None
        self._window_destroy_cb = None
        self._tray_notify_cb = None
        self.tray_icon = None
        self.registry = None
        self.snapshots = {}
        self.rows = []
        self.selected_profiles = set()
        self._has_applied_initial_selection = False
        self.groups = []
        self.suggested_group = None
        self.probe_group = None
        self.capture = None
        self.tun_enabled = None
        self.pending = None
        self.undo = None
        self.status = "请刷新订阅源"
        self.running = False
        self.funnel_stage = None
        self.refreshing = False
        self.runner = None
        self.generation = 0
        self.score_revision = 0
        self.live_progress = {}
        self.recently_scored = {}
        self.cascade_highlighted = {}
        self.run_targets = set()
        self.run_total = 0
        self.run_done = 0
        self.schedule = TestSchedule()
        self.scheduled_options = None
        self.patrol_schedule = TestSchedule()
        self.patrol_quota_bytes = self.preferences["patrol_quota"]
        self.patrol_snapshots = None
        self.patrol_last_summary = ""
        self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
        self.cached_patrol_quota_day = date.today()
        self.active_run_kind = "normal"
        self.health_probe_status = "● 活跃探测 待命"
        self.health_probe_level = "idle"
        self.health_probe_inflight = False
        self._health_probe_lock = threading.Lock()
        self._health_probe_thread = None
        self._last_health_probe_at = 0.0
        self.latch = SwitchLatch()
        if runtime_bridge is not None:
            self.runtime_bridge = runtime_bridge
        elif root is not None:
            self.runtime_bridge = WindowsRuntimeBridge(
                webview_dir=Path(root) / ".ebwebview",
                policy_checker=lambda: False,
            )
        else:
            self.runtime_bridge = WindowsRuntimeBridge()
        self._runtime_bridge_status = {
            "state": "not_configured",
            "ready": False,
            "attached": False,
            "cdp_ready": False,
            "policy_configured": False,
            "label": "跨订阅热切 未启用",
        }
        self.closed = threading.Event()
        self.window = None
        threading.Thread(target=self._timer, daemon=True).start()

    def _refresh_runtime_bridge_status(self, force_probe=False):
        try:
            info = self.runtime_bridge.status(force_probe=force_probe)
        except Exception:
            info = {
                "state": "not_configured",
                "ready": False,
                "attached": False,
                "cdp_ready": False,
                "policy_configured": False,
                "label": "跨订阅热切 未启用",
            }
        with self.lock:
            self._runtime_bridge_status = dict(info)
        return info

    def configure_runtime_bridge(self, value):
        try:
            if not isinstance(value, dict):
                raise ValueError("参数无效")
            action = value.get("action")
            if action not in ("enable", "disable", "probe"):
                raise ValueError("不支持的操作")
            with self.lock:
                if self.closed.is_set() or self.running or self.refreshing:
                    return {"ok": False, "error": "测速或刷新进行中，请稍后再试"}
            if action == "enable":
                res = enable_webview2_cdp_policy()
                info = self._refresh_runtime_bridge_status(force_probe=True)
                with self.lock:
                    self.status = res.get("message") or res.get("error") or self.status
                return {"ok": bool(res.get("ok")), "error": res.get("error", ""), "runtime_bridge": info}
            if action == "disable":
                res = disable_webview2_cdp_policy()
                info = self._refresh_runtime_bridge_status(force_probe=True)
                with self.lock:
                    self.status = res.get("message") or res.get("error") or self.status
                return {"ok": bool(res.get("ok")), "error": res.get("error", ""), "runtime_bridge": info}
            info = self._refresh_runtime_bridge_status(force_probe=True)
            if info.get("cdp_ready") and not info.get("attached"):
                try:
                    self.runtime_bridge.ensure_attached(timeout=3.0)
                    info = self._refresh_runtime_bridge_status(force_probe=True)
                except Exception:
                    info = self._refresh_runtime_bridge_status(force_probe=True)
            with self.lock:
                if info.get("ready"):
                    self.status = "跨订阅无感热切已就绪，切换非当前订阅节点时将自动热切"
                elif info.get("policy_configured"):
                    self.status = "跨订阅热切策略已配置，请重启一次 Clash Verge 生效"
                else:
                    self.status = "跨订阅热切尚未启用；可点此启用或继续使用手动切换订阅流程"
            return {"ok": True, "runtime_bridge": info}
        except Exception:
            return {"ok": False, "error": "跨订阅热切配置失败"}

    def _invalidate(self, clear_pending=True):
        self.generation += 1
        self.schedule.disarm()
        self.scheduled_options = None
        self.undo = None
        if clear_pending:
            self.pending = None

    def _disarm_patrol(self, reason=None):
        was_armed = self.patrol_schedule.minutes is not None
        self.patrol_schedule.disarm()
        self.patrol_snapshots = None
        if was_armed and reason:
            self.status = reason

    def _run_progress_locked(self):
        if not self.running:
            return None
        total = self.run_total or len(self.run_targets)
        if total <= 0:
            return {"done": 0, "total": 0, "pct": 0}
        done = min(total, self.run_done)
        partial = 0.0
        for ident, live in self.live_progress.items():
            if self.run_targets and ident not in self.run_targets:
                continue
            expected = live.get("expected_bytes") or 0
            received = live.get("received_bytes") or 0
            if expected > 0 and received > 0:
                partial += min(0.95, received / expected)
            else:
                partial += 0.08
        frac = min(1.0, max(0.0, (done + partial) / total))
        pct = min(99 if done < total else 100, int(round(frac * 100)))
        return {"done": done, "total": total, "pct": pct}

    def _best_summary_locked(self):
        candidates = [
            r for r in self.rows
            if r.uid in self.selected_profiles and _eligible_name(r.name)
            and r.measurement and r.measurement.score is not None
        ]
        if not candidates:
            return ""
        best = max(candidates, key=lambda r: r.measurement.score)
        m = best.measurement
        name = best.name if len(best.name) <= 28 else best.name[:27] + "…"
        speed_mib = m.download_bytes_per_second / 1_048_576
        return f"最优：{name} · {speed_mib:.2f} MiB/s ({m.score:.1f})"

    def get_state(self):
        with self.lock:
            now_mono = time.monotonic()
            self.recently_scored = {k: exp for k, exp in self.recently_scored.items() if exp > now_mono}
            self.cascade_highlighted = {k: exp for k, exp in self.cascade_highlighted.items() if exp > now_mono}
            profiles = [] if not self.registry else [
                {"uid": p.uid, "name": p.name, "current": p.current,
                 "compatible": p.uid in self.snapshots,
                 "node_count": len(self.snapshots[p.uid].names) if p.uid in self.snapshots else 0,
                 "selected": p.uid in self.selected_profiles} for p in self.registry.profiles]
            rows = []
            for row in self.rows:
                m = row.measurement
                ident = (row.uid, row.name)
                live = self.live_progress.get(ident) if self.running else None
                rows.append({"uid": row.uid, "profile": row.profile, "name": row.name,
                             "selected": row.selected, "state": row.state,
                             "eligible": row.eligible,
                             "node_type": row.node_type or (m.node_type if m else ""),
                             "score": m.score if m else None,
                             "stage1_score": row.stage1_score,
                             "speed": m.download_bytes_per_second if m else None,
                             "latency": m.latency_ms if m else None,
                             "jitter": m.jitter_ms if m else None,
                             "successes": m.head_successes if m else None,
                             "samples": list(row.samples) if row.samples else [],
                             "live": dict(live) if live else None,
                             "recently_scored": ident in self.recently_scored,
                             "cascade_highlighted": ident in self.cascade_highlighted})
            pending = None if not self.pending else {"uid": self.pending[0], "name": self.pending[1],
                       "ready": bool(self.registry and self.pending[0] in self.snapshots and any(p.current and p.uid == self.pending[0] for p in self.registry.profiles))}
            undo = None if not self.undo else {"group": self.undo[2], "name": self.undo[1]}
            schedule_minutes, schedule_remaining, schedule_active = self.schedule.display()
            patrol_minutes, patrol_remaining, patrol_active = self.patrol_schedule.display()
            return {"profiles": profiles, "rows": rows, "groups": list(self.groups), "pending": pending,
                    "suggested_group": self.suggested_group,
                    "undo": undo,
                    "funnel_stage": self.funnel_stage,
                    "score_revision": self.score_revision,
                    "tun_enabled": self.tun_enabled,
                    "running": self.running or self.refreshing, "schedule_minutes": schedule_minutes,
                    "schedule_remaining": schedule_remaining, "schedule_active": schedule_active,
                    "patrol": {
                        "armed": patrol_minutes is not None,
                        "minutes": patrol_minutes,
                        "interval_minutes": 30 if self.patrol_quota_bytes >= 150 * 1_048_576 else 60,
                        "max_per_tick": _PATROL_BATCH_BY_QUOTA.get(self.patrol_quota_bytes, 5),
                        "remaining": patrol_remaining,
                        "active": patrol_active,
                        "quota_bytes": self.patrol_quota_bytes,
                        "used_bytes_today": self.cached_patrol_bytes_today,
                        "summary": self.patrol_last_summary,
                    },
                    "health_probe": {
                        "text": self.health_probe_status,
                        "level": self.health_probe_level,
                    },
                    "minimize_to_tray": self.minimize_to_tray,
                    "window_visible": self.window_visible,
                    "preferences": dict(self.preferences),
                    "run_progress": self._run_progress_locked(),
                    "runtime_bridge": dict(self._runtime_bridge_status),
                    "status": self.status, "outcome_unknown": self.latch.outcome_unknown}

    def _notify_tray(self):
        cb = self._tray_notify_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def is_busy(self):
        with self.lock:
            return bool(self.running or self.refreshing or self._switching_count > 0)

    def set_minimize_to_tray(self, value):
        try:
            enabled = value["enabled"] if isinstance(value, dict) else value
            if type(enabled) is not bool:
                raise ValueError("invalid")
            self.settings_store.save_minimize_to_tray(enabled)
            with self.lock:
                self.minimize_to_tray = enabled
            self._notify_tray()
            return {"ok": True, "minimize_to_tray": enabled}
        except SettingsSecurityError:
            return {"ok": False, "error": "设置路径安全校验失败，无法保存托盘设置"}
        except Exception:
            return {"ok": False, "error": "托盘设置参数无效"}

    def save_preferences(self, value):
        try:
            saved = self.settings_store.save_preferences(value)
            with self.lock:
                self.preferences = saved
                if self.patrol_schedule.minutes is None:
                    self.patrol_quota_bytes = saved["patrol_quota"]
            self._notify_tray()
            return {"ok": True, "preferences": dict(saved)}
        except SettingsSecurityError:
            return {"ok": False, "error": "设置路径安全校验失败，无法保存参数偏好"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc) or "参数偏好无效"}
        except Exception:
            return {"ok": False, "error": "保存参数偏好失败"}

    def hide_to_tray(self):
        with self.lock:
            if self.closed.is_set():
                return {"ok": False, "error": "应用已关闭"}
            self.window_visible = False
            cb = self._window_hide_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        self._notify_tray()
        return {"ok": True}

    def show_from_tray(self):
        with self.lock:
            if self.closed.is_set():
                return {"ok": False, "error": "应用已关闭"}
            self.window_visible = True
            cb = self._window_show_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        self._notify_tray()
        return {"ok": True}

    def get_tray_snapshot(self):
        with self.lock:
            sched_minutes, _sched_rem, _sched_active = self.schedule.display()
            patrol_minutes, _patrol_rem, _patrol_active = self.patrol_schedule.display()
            patrol_armed = patrol_minutes is not None
            running = bool(self.running)
            refreshing = bool(self.refreshing)
            switching = self._switching_count > 0
            busy = running or refreshing or switching
            prog = self._run_progress_locked()
            if running and self.active_run_kind == "patrol":
                primary = (
                    f"智能巡检中 ({prog['done']}/{prog['total']} · {prog['pct']}%)"
                    if prog and prog["total"] > 0 else "智能巡检中"
                )
            elif running:
                primary = (
                    f"测速中 ({prog['done']}/{prog['total']} · {prog['pct']}%)"
                    if prog and prog["total"] > 0 else "测速中"
                )
            elif switching:
                primary = "切换处理中"
            elif refreshing:
                primary = "刷新订阅中"
            elif patrol_armed:
                primary = "智能巡检中"
            elif sched_minutes is not None:
                primary = "定时已启用"
            elif not self.window_visible:
                primary = "托盘后台运行中"
            else:
                primary = "待命就绪"

            used_mib = int((self.cached_patrol_bytes_today + 1_048_575) // 1_048_576) if self.cached_patrol_bytes_today else 0
            quota_mib = self.patrol_quota_bytes // 1_048_576
            parts = [self.health_probe_status]
            if patrol_armed or self.cached_patrol_bytes_today > 0:
                parts.append(f"今日巡检 {used_mib}/{quota_mib} MiB")
            detail = " · ".join(parts)
            best_summary = self._best_summary_locked()
            tooltip = f"Clash 测速工作台 · {primary}"
            if best_summary:
                tooltip += f" · {best_summary}"
            elif self.health_probe_level != "idle":
                tooltip += f"（{self.health_probe_status.replace('● ', '')}）"
            if len(tooltip) > 120:
                tooltip = tooltip[:119] + "…"
            return {
                "window_visible": self.window_visible,
                "minimize_to_tray": self.minimize_to_tray,
                "busy": busy,
                "running": running,
                "refreshing": refreshing,
                "switching": switching,
                "schedule_minutes": sched_minutes,
                "patrol_armed": patrol_armed,
                "outcome_unknown": self.latch.outcome_unknown,
                "primary_status": primary,
                "detail_summary": detail,
                "best_summary": best_summary,
                "run_progress": prog,
                "tooltip": tooltip,
            }

    def handle_window_closing(self):
        with self.lock:
            if self.closed.is_set():
                return True
            should_quit = self.quit_requested or not self.minimize_to_tray
            busy = self.running or self.refreshing or self._switching_count > 0
        if not should_quit:
            self.hide_to_tray()
            return False
        if busy:
            self.show_from_tray()
            with self.lock:
                self.quit_requested = False
                was_running = self.running
            if was_running:
                self.stop()
            else:
                with self.lock:
                    if not self.latch.outcome_unknown:
                        self.status = "操作处理中，请确认安全状态后再退出"
            self._notify_tray()
            return False
        self.close()
        return True

    def request_exit(self):
        with self.lock:
            if self.closed.is_set():
                return {"ok": True}
            busy = self.running or self.refreshing or self._switching_count > 0
            was_running = self.running
        if busy:
            self.show_from_tray()
            if was_running:
                self.stop()
            else:
                with self.lock:
                    if not self.latch.outcome_unknown:
                        self.status = "操作处理中，请确认安全状态后再退出"
            self._notify_tray()
            with self.lock:
                err = self.status
            return {"ok": False, "busy": True, "error": err}
        with self.lock:
            self.quit_requested = True
            destroy_cb = self._window_destroy_cb
        self.close()
        if destroy_cb is not None:
            try:
                destroy_cb()
            except Exception:
                pass
        return {"ok": True}

    def refresh(self):
        with self.lock:
            if self.running or self.refreshing:
                return {"ok": False, "error": "测速进行中"}
            self._invalidate(clear_pending=False)
            self._disarm_patrol()
            generation = self.generation
            self.refreshing = True
            pending = self.pending
            self.capture = None
            self.groups = []
            self.suggested_group = None
            self.probe_group = None
            self.live_progress.clear()
            self.recently_scored.clear()
            self.cascade_highlighted.clear()
        try:
            registry = load_registry(self.root)
            if len(registry.profiles) > 32:
                raise ProfileError("too many profiles")
            snapshots = {}
            rows = []
            total = 0
            skipped = 0
            for profile in registry.profiles:
                try:
                    snapshot = snapshot_profile(registry, profile)
                except ProfileError as exc:
                    if str(exc) in ("invalid profile", "invalid YAML"):
                        skipped += 1
                        continue
                    raise
                snapshots[profile.uid] = snapshot
                total += len(snapshot.data)
                for idx, name in enumerate(snapshot.names):
                    ntype = snapshot.node_types[idx] if idx < len(snapshot.node_types) else ""
                    rows.append(Row(profile.uid, profile.name, name, node_type=ntype))
                if total > 8 * 1024 * 1024 or len(rows) > 2000:
                    raise ProfileError("too many nodes")
            if registry.profiles and not snapshots:
                raise ProfileError("invalid profile")
            revalidate(registry, snapshots.values())
            tun_enabled = read_tun_enabled(registry)
            with self.lock:
                if generation != self.generation or self.closed.is_set():
                    raise ProfileError("cancelled")
                prev_selected = set(self.selected_profiles)
                self.registry, self.snapshots, self.rows = registry, snapshots, rows
                if not self._has_applied_initial_selection:
                    self._has_applied_initial_selection = True
                    current = next((p for p in registry.profiles if p.current and p.uid in snapshots), None)
                    if current is not None:
                        self.selected_profiles = {current.uid}
                    elif snapshots:
                        first_uid = next(p.uid for p in registry.profiles if p.uid in snapshots)
                        self.selected_profiles = {first_uid}
                    else:
                        self.selected_profiles = set()
                else:
                    kept = prev_selected & set(snapshots)
                    if kept:
                        self.selected_profiles = kept
                    elif prev_selected:
                        current = next((p for p in registry.profiles if p.current and p.uid in snapshots), None)
                        self.selected_profiles = {current.uid} if current is not None else set()
                    else:
                        self.selected_profiles = set()
                for row in rows:
                    row.selected = row.uid in self.selected_profiles
                self.tun_enabled = tun_enabled
                if not registry.profiles:
                    self.status = "没有可用的本地订阅"
                elif skipped:
                    self.status = f"已加载 {len(snapshots)} 个订阅、{len(rows)} 个节点（已跳过 {skipped} 个不兼容订阅）"
                else:
                    self.status = f"已加载 {len(snapshots)} 个订阅、{len(rows)} 个节点"
                if pending:
                    uid, node, measurement, old_registry, old_snapshots = pending
                    same = (_registry_signature(registry) == _registry_signature(old_registry) and
                            all(uid_ in snapshots and snapshots[uid_].data == old.data and
                                snapshots[uid_].version == old.version for uid_, old in old_snapshots.items()) and
                            uid in snapshots and node in snapshots[uid].names)
                    self.pending = pending if same else None
                    if same:
                        row = next(r for r in rows if r.uid == uid and r.name == node)
                        row.measurement, row.state = measurement, "上轮最优"
                        self.status = "已保留上轮最优节点；确认当前订阅后刷新策略组并继续"
            self._refresh_runtime_bridge_status(force_probe=True)
            return {"ok": True}
        except Exception:
            with self.lock:
                self.registry, self.snapshots, self.rows = None, {}, []
                self.selected_profiles = set()
                self.pending = None
                self.suggested_group = None
                self.tun_enabled = None
                self.status = "订阅读取失败：请检查配置格式与文件安全状态"
            return {"ok": False, "error": self.status}
        finally:
            with self.lock:
                self.refreshing = False

    def set_selection(self, value):
        try:
            profiles = set(value["profiles"])
            nodes = {(item["uid"], item["name"]) for item in value["nodes"]}
            with self.lock:
                if self.running or self.refreshing or not self.registry:
                    raise ValueError()
                if not profiles <= set(self.snapshots):
                    raise ValueError()
                available = {(row.uid, row.name) for row in self.rows}
                if not nodes <= available or any(uid not in profiles for uid, _ in nodes):
                    raise ValueError()
                old_nodes = {(r.uid, r.name) for r in self.rows if r.selected and r.uid in self.selected_profiles}
                profiles_changed = profiles != self.selected_profiles
                if profiles_changed or nodes != old_nodes:
                    self._invalidate()
                    self._disarm_patrol()
                    current_uid = next((p.uid for p in self.registry.profiles if p.current), None)
                    if profiles_changed and (not current_uid or current_uid not in profiles):
                        self.capture, self.groups = None, []
                        self.suggested_group = None
                        self.probe_group = None
                self.selected_profiles = profiles
                for row in self.rows:
                    row.selected = (row.uid, row.name) in nodes
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "节点选择无效或测速进行中"}

    @staticmethod
    def _options(value):
        parallel, size, mode = value["parallel"], value["download_bytes"], value["mode"]
        if type(parallel) is not int or not 1 <= parallel <= 10:
            raise ValueError()
        if type(size) is not int or not 256 * 1024 <= size <= 50 * 1024 * 1024:
            raise ValueError()
        if mode not in ("fast", "accurate"):
            raise ValueError()
        auto, group = value.get("auto_switch", False), value.get("group", "")
        if type(auto) is not bool or not isinstance(group, str):
            raise ValueError()
        return {"parallel": parallel, "download_bytes": size, "mode": mode,
                "auto_switch": auto, "group": group}

    def refresh_groups(self, preserve_schedule=False):
        registry = None
        generation = None
        tun_updated = False
        tun_enabled = None
        try:
            with self.lock:
                if self.running or self.refreshing or not self.registry or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
                if self.schedule.minutes is not None and not preserve_schedule:
                    self._invalidate()
                registry = self.registry
                current = next((p for p in registry.profiles if p.current), None)
                if not current or current.uid not in self.snapshots:
                    raise ControllerError("unavailable")
                snapshot = self.snapshots[current.uid]
                generation = self.generation
            try:
                revalidate(registry, [snapshot])
            except ProfileError:
                if preserve_schedule:
                    raise
                status_code, _ = self._sync_stale_registry_for_manual_action(need_groups=False)
                if status_code not in ("fresh", "synced"):
                    raise
                with self.lock:
                    if self.running or self.refreshing or not self.registry:
                        raise ControllerError("unavailable")
                    registry = self.registry
                    current = next((p for p in registry.profiles if p.current), None)
                    if not current or current.uid not in self.snapshots:
                        raise ControllerError("unavailable")
                    snapshot = self.snapshots[current.uid]
                    generation = self.generation
                revalidate(registry, [snapshot])
            tun_enabled = read_tun_enabled(registry)
            tun_updated = True
            settings = read_settings(registry)
            controller = PipeController(settings)
            groups = controller.groups(snapshot.names)
            revalidate(registry, [snapshot])
            if read_settings(registry) != settings:
                raise ControllerError("changed")
            tun_enabled = read_tun_enabled(registry)
            with self.lock:
                if generation != self.generation:
                    raise ControllerError("changed")
                self.tun_enabled = tun_enabled
                self.capture = (registry, snapshot, settings, controller.identity, tuple(groups))
                self.groups = [{"name": g.name, "now": g.now} for g in groups]
                suggested = self._compute_suggested_group(groups, snapshot.names)
                self.suggested_group = suggested
                if suggested:
                    self.probe_group = suggested
                else:
                    current_probe = next((g for g in groups if g.name == self.probe_group), None)
                    if current_probe is None or current_probe.now not in snapshot.names:
                        self.probe_group = self._default_probe_group(groups, snapshot.names)
                self.status = f"策略组已刷新，已智能选中“{suggested}”" if suggested else "策略组已刷新，请选择目标组"
            self._trigger_health_probe_async()
            return {"ok": True}
        except ProfileError:
            with self.lock:
                if self.registry is registry and generation == self.generation:
                    self.tun_enabled = None
                self.capture, self.groups, self.suggested_group = None, [], None
                self.status = "无法读取当前订阅的策略组；请确认本地控制器可用"
            return {"ok": False, "error": self.status}
        except Exception:
            with self.lock:
                if tun_updated and self.registry is registry and generation == self.generation:
                    self.tun_enabled = tun_enabled
                self.capture, self.groups, self.suggested_group = None, [], None
                self.status = "无法读取当前订阅的策略组；请确认本地控制器可用"
            return {"ok": False, "error": self.status}

    @staticmethod
    def _group_core_name(name):
        return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

    def _sync_group_now_locked(self, group_name, new_now):
        self.groups = [
            {"name": g["name"], "now": new_now if g["name"] == group_name else g["now"]}
            for g in self.groups
        ]
        if self.capture is not None:
            registry, snapshot, settings, identity, groups = self.capture
            updated = tuple(
                ProxyGroup(g.name, new_now if g.name == group_name else g.now, g.all)
                for g in groups
            )
            self.capture = (registry, snapshot, settings, identity, updated)

    @staticmethod
    def _default_probe_group(groups, snapshot_names):
        """Pick a read-only health-probe group, preferring one whose current target is a real node."""
        if not groups:
            return None
        non_global = [g for g in groups if g.name != "GLOBAL"]
        pool = non_global if non_global else list(groups)
        node_set = set(snapshot_names)
        direct_node = [g for g in pool if g.now in node_set]
        if direct_node:
            return max(direct_node, key=lambda g: len(set(g.all) & node_set)).name
        return pool[0].name

    def _reload_registry_and_snapshots(self):
        registry = load_registry(self.root)
        if len(registry.profiles) > 32:
            raise ProfileError("too many profiles")
        snapshots = {}
        rows = []
        total = 0
        skipped = 0
        for profile in registry.profiles:
            try:
                snapshot = snapshot_profile(registry, profile)
            except ProfileError as exc:
                if str(exc) in ("invalid profile", "invalid YAML"):
                    skipped += 1
                    continue
                raise
            snapshots[profile.uid] = snapshot
            total += len(snapshot.data)
            for idx, name in enumerate(snapshot.names):
                ntype = snapshot.node_types[idx] if idx < len(snapshot.node_types) else ""
                rows.append(Row(profile.uid, profile.name, name, node_type=ntype))
            if total > 8 * 1024 * 1024 or len(rows) > 2000:
                raise ProfileError("too many nodes")
        if registry.profiles and not snapshots:
            raise ProfileError("invalid profile")
        revalidate(registry, snapshots.values())
        tun_enabled = read_tun_enabled(registry)
        return registry, snapshots, rows, skipped, tun_enabled

    def _sync_stale_registry_for_manual_action(self, *, need_groups=False, requested_group=""):
        """Auto-sync if Clash Verge updated profiles.yaml while the helper stayed open."""
        with self.lock:
            if self.running or self.refreshing or self.closed.is_set() or not self.registry:
                return "skip", None
            registry = self.registry
            generation = self.generation
            selected_profiles = set(self.selected_profiles)
            check_uids = set(selected_profiles)
            old_current = next((p.uid for p in registry.profiles if p.current), None)
            if old_current:
                check_uids.add(old_current)
            check_snaps = [self.snapshots[uid] for uid in check_uids if uid in self.snapshots]
        if not check_snaps:
            return "skip", None
        try:
            revalidate(registry, check_snaps)
            return "fresh", None
        except ProfileError:
            pass

        new_registry, new_snapshots, new_rows, _skipped, new_tun = self._reload_registry_and_snapshots()
        new_current = next((p for p in new_registry.profiles if p.current and p.uid in new_snapshots), None)
        new_current_uid = new_current.uid if new_current else None
        fresh_capture = None
        fresh_groups = None
        if need_groups and new_current is not None and not self.latch.outcome_unknown:
            try:
                snap_cur = new_snapshots[new_current.uid]
                settings = read_settings(new_registry)
                controller = PipeController(settings)
                groups = controller.groups(snap_cur.names)
                revalidate(new_registry, [snap_cur])
                if read_settings(new_registry) == settings:
                    fresh_capture = (new_registry, snap_cur, settings, controller.identity, tuple(groups))
                    fresh_groups = tuple(groups)
            except Exception:
                fresh_capture = None
                fresh_groups = None

        with self.lock:
            if generation != self.generation or self.running or self.refreshing or self.closed.is_set():
                return "skip", None
            prev_selected_profiles = set(self.selected_profiles)
            prev_selected_nodes = {(r.uid, r.name) for r in self.rows if r.selected and r.uid in prev_selected_profiles}
            prev_uids_with_nodes = {uid for uid, _ in prev_selected_nodes}
            prev_row_meta = {
                (r.uid, r.name): (r.measurement, list(r.samples), r.state, r.stage1_score)
                for r in self.rows
                if r.uid in self.snapshots and r.uid in new_snapshots
                and new_snapshots[r.uid].data == self.snapshots[r.uid].data
            }
            self.registry = new_registry
            self.snapshots = new_snapshots
            self.rows = new_rows
            self.tun_enabled = new_tun
            kept_profiles = prev_selected_profiles & set(new_snapshots)
            if kept_profiles:
                self.selected_profiles = kept_profiles
            elif new_current_uid:
                self.selected_profiles = {new_current_uid}
            elif new_snapshots:
                self.selected_profiles = {next(iter(new_snapshots))}
            else:
                self.selected_profiles = set()
            for row in new_rows:
                if row.uid in self.selected_profiles:
                    row.selected = (row.uid, row.name) in prev_selected_nodes if row.uid in prev_uids_with_nodes else True
                else:
                    row.selected = False
                meta = prev_row_meta.get((row.uid, row.name))
                if meta is not None:
                    row.measurement, row.samples, row.state, row.stage1_score = meta
            if self.schedule.minutes is not None:
                self.schedule.disarm()
                self.scheduled_options = None
            if self.patrol_schedule.minutes is not None:
                self._disarm_patrol()
            self.undo = None
            self.score_revision += 1

            if fresh_capture is not None and fresh_groups is not None:
                self.capture = fresh_capture
                self.groups = [{"name": g.name, "now": g.now} for g in fresh_groups]
                suggested = self._compute_suggested_group(fresh_groups, new_snapshots[new_current.uid].names)
                self.suggested_group = suggested
                if suggested:
                    self.probe_group = suggested
                else:
                    self.probe_group = self._default_probe_group(fresh_groups, new_snapshots[new_current.uid].names)
            elif old_current != new_current_uid or need_groups:
                self.capture = None
                self.groups = []
                self.suggested_group = None
                self.probe_group = None

            if need_groups:
                if fresh_capture is None or fresh_groups is None:
                    self.status = "已自动同步 Clash Verge 最新配置，但无法读取当前策略组；请确认 Clash Verge 正在运行"
                    return "abort", self.status
                group_exists = bool(requested_group) and any(g.name == requested_group for g in fresh_groups)
                if old_current != new_current_uid and not group_exists:
                    hint = f"（建议“{self.suggested_group}”）" if self.suggested_group else ""
                    self.status = f"检测到 Clash Verge 已切换订阅，已自动同步最新配置与策略组{hint}，请确认策略组后再开始测速"
                    return "abort", self.status
                if not group_exists:
                    self.status = "已自动同步 Clash Verge 最新配置，原策略组已不可用，请重新选择策略组后再测速"
                    return "abort", self.status
                return "synced", None

            if not any(r.selected and r.uid in self.selected_profiles for r in self.rows):
                self.status = "已自动同步 Clash Verge 最新订阅，请重新勾选节点后再测速"
                return "abort", self.status
            return "synced", None

    @staticmethod
    def _compute_suggested_group(groups, snapshot_names):
        non_global = [g for g in groups if g.name != "GLOBAL"]
        if not non_global:
            return None
        if len(non_global) == 1:
            return non_global[0].name
        tested_set = set(snapshot_names)
        if not tested_set:
            return None
        overlap = {g.name: len(set(g.all) & tested_set) for g in non_global}
        full = [g for g in non_global if overlap[g.name] == len(tested_set)]
        if len(full) == 1:
            return full[0].name
        if full:
            name_pool = full
        else:
            high = [g for g in non_global if overlap[g.name] * 5 >= len(tested_set) * 4]
            name_pool = high if high else non_global
        primary = [g for g in name_pool if Desktop._group_core_name(g.name) in _PRIMARY_SELECTOR_KEYS]
        if len(primary) == 1:
            return primary[0].name
        if len(primary) > 1:
            p_members = {g.name: set(g.all) for g in primary}
            parents = [
                g for g in primary
                if any(o.name != g.name and o.name in p_members[g.name] for o in primary)
                and not any(o.name != g.name and g.name in p_members[o.name] for o in primary)
            ]
            if len(parents) == 1:
                return parents[0].name
        if len(tested_set) >= 2:
            dominant = [g for g in non_global if overlap[g.name] * 5 >= len(tested_set) * 4]
            if len(dominant) == 1:
                winner = dominant[0]
                if all(g.name == winner.name or overlap[g.name] * 2 < len(tested_set) for g in non_global):
                    return winner.name
        return None

    @staticmethod
    def _resolve_target_group(groups, preferred_group, target_node, snapshot_names):
        if preferred_group:
            direct = next((g for g in groups if g.name == preferred_group and target_node in g.all), None)
            if direct is not None:
                return direct.name
            pref_core = Desktop._group_core_name(preferred_group)
            if pref_core:
                core_matches = [
                    g.name for g in groups
                    if g.name != "GLOBAL" and target_node in g.all and Desktop._group_core_name(g.name) == pref_core
                ]
                if len(core_matches) == 1:
                    return core_matches[0]
        containing = [g for g in groups if g.name != "GLOBAL" and target_node in g.all]
        if not containing:
            return None
        return Desktop._compute_suggested_group(containing, snapshot_names)

    def _wait_controller_for_profile(self, target_uid, target_node, timeout=12.0, is_cancelled=None):
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if is_cancelled is not None and is_cancelled():
                raise RuntimeSwitchCancelled("cancelled")
            try:
                fresh_registry = load_registry(self.root)
                target_profile = next(
                    (p for p in fresh_registry.profiles if p.uid == target_uid and p.current),
                    None,
                )
                if target_profile is not None:
                    fresh_snapshot = snapshot_profile(fresh_registry, target_profile)
                    if target_node in fresh_snapshot.names:
                        fresh_settings = read_settings(fresh_registry)
                        ctrl = PipeController(fresh_settings)
                        fresh_groups = ctrl.groups(fresh_snapshot.names)
                        if any(target_node in g.all for g in fresh_groups):
                            revalidate(fresh_registry, [fresh_snapshot])
                            if read_settings(fresh_registry) == fresh_settings:
                                return fresh_registry, fresh_snapshot, fresh_settings, ctrl.identity, tuple(fresh_groups)
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeSwitchOutcomeUnknown("controller did not converge after profile switch")

    def _hot_switch_cross_profile(self, target_uid, target_node, preferred_group, measurement,
                                  generation, is_cancelled, allow_pending_fallback):
        with self.lock:
            if self.latch.outcome_unknown:
                return "outcome_unknown", "切换结果未知，请手动核验；切换已锁定"
            if not self.registry or target_uid not in self.snapshots:
                return "not_attempted", "目标订阅不可用，请刷新订阅"
            target_snapshot_old = self.snapshots[target_uid]
            if target_node not in target_snapshot_old.names:
                return "not_attempted", "目标节点不在该订阅中"
            target_profile_name = target_snapshot_old.profile.name

        bridge_info = self._refresh_runtime_bridge_status(force_probe=True)
        if not bridge_info.get("ready"):
            if allow_pending_fallback:
                with self.lock:
                    self.pending = (target_uid, target_node, measurement, self.registry, dict(self.snapshots))
                    self.status = "最优节点不在当前订阅，请先在 Clash Verge 手动切换订阅（或启用跨订阅热切）"
                return "pending_fallback", self.status
            return "bridge_unavailable", "目标节点不在当前订阅；请先启用跨订阅热切或在 Clash Verge 手动切换订阅"

        try:
            self.runtime_bridge.switch_profile(target_uid, timeout=25.0, is_cancelled=is_cancelled)
        except RuntimeBridgeUnavailable:
            self._refresh_runtime_bridge_status(force_probe=True)
            if allow_pending_fallback:
                with self.lock:
                    self.pending = (target_uid, target_node, measurement, self.registry, dict(self.snapshots))
                    self.status = "跨订阅热切桥未就绪，请先在 Clash Verge 手动切换订阅"
                return "pending_fallback", self.status
            return "bridge_unavailable", "跨订阅热切桥未就绪，请检查 Clash Verge 或手动切换订阅"
        except RuntimeSwitchCancelled:
            return "not_attempted", "已取消跨订阅切换"
        except RuntimeSwitchOutcomeUnknown:
            with self.lock:
                self.latch.outcome_unknown = True
                self._invalidate()
                self.status = "跨订阅切换结果未知，请到 Clash Verge 手动核验；切换已锁定"
            self._refresh_runtime_bridge_status(force_probe=True)
            return "outcome_unknown", self.status
        except RuntimeBrokerError:
            if allow_pending_fallback:
                with self.lock:
                    self.pending = (target_uid, target_node, measurement, self.registry, dict(self.snapshots))
                    self.status = "未能在 Clash Verge 中定位目标订阅，请手动切换订阅"
                return "pending_fallback", self.status
            return "not_attempted", "未在 Clash Verge 中找到目标订阅，请刷新后重试"
        except Exception:
            with self.lock:
                self.latch.outcome_unknown = True
                self._invalidate()
                self.status = "跨订阅切换结果未知，请到 Clash Verge 手动核验；切换已锁定"
            self._refresh_runtime_bridge_status(force_probe=True)
            return "outcome_unknown", self.status

        # Profile switch transaction confirmed by official Clash Verge UI/core.
        try:
            fresh_registry, fresh_snapshot, fresh_settings, fresh_identity, fresh_groups = (
                self._wait_controller_for_profile(target_uid, target_node, timeout=12.0, is_cancelled=is_cancelled)
            )
        except Exception:
            with self.lock:
                self.latch.outcome_unknown = True
                self._invalidate()
                self.status = "订阅已发起热切但控制器状态未能在时限内确认，请到 Clash Verge 手动核验；切换已锁定"
            self._refresh_runtime_bridge_status(force_probe=True)
            return "outcome_unknown", self.status

        suggested = self._compute_suggested_group(fresh_groups, fresh_snapshot.names)
        resolved_group = self._resolve_target_group(fresh_groups, preferred_group, target_node, fresh_snapshot.names)
        tun_enabled = None
        try:
            tun_enabled = read_tun_enabled(fresh_registry)
        except Exception:
            tun_enabled = None

        with self.lock:
            self.registry = fresh_registry
            self.snapshots[target_uid] = fresh_snapshot
            if tun_enabled is not None:
                self.tun_enabled = tun_enabled
            self.capture = (fresh_registry, fresh_snapshot, fresh_settings, fresh_identity, fresh_groups)
            self.groups = [{"name": g.name, "now": g.now} for g in fresh_groups]
            self.suggested_group = suggested
            if suggested:
                self.probe_group = suggested
            elif fresh_groups:
                self.probe_group = self._default_probe_group(fresh_groups, fresh_snapshot.names)
            self.undo = None

        if resolved_group is None:
            with self.lock:
                self.pending = (target_uid, target_node, measurement, fresh_registry, dict(self.snapshots))
                self.status = f"已自动热切订阅“{target_profile_name}”，请选择策略组后继续应用节点“{target_node}”"
            self._refresh_runtime_bridge_status(force_probe=False)
            return "group_select_required", self.status

        try:
            expected = next(g for g in fresh_groups if g.name == resolved_group)
            switch = AssistedSwitch(fresh_registry, fresh_snapshot, target_node, measurement, self.latch)
            groups_now = switch.prepare()
            found = next(g for g in groups_now if g.name == resolved_group)
            if switch.settings != fresh_settings or switch.controller.identity != fresh_identity or found.now != expected.now:
                raise ControllerError("changed")

            def authorized():
                with self.lock:
                    if generation != self.generation or self.closed.is_set():
                        return False
                    if is_cancelled is not None and is_cancelled():
                        return False
                    return True

            outcome = switch.apply(resolved_group, authorized)
            with self.lock:
                if outcome == "outcome_unknown":
                    self._invalidate()
                    self.status = "订阅已热切但节点切换结果未知，请到 Clash Verge 手动核验；切换已锁定"
                elif outcome in ("switched", "confirmed_noop"):
                    self.pending = None
                    if outcome == "switched":
                        self._sync_group_now_locked(resolved_group, target_node)
                        if expected.now in fresh_snapshot.names and expected.now in expected.all and expected.now != target_node:
                            self.undo = (target_uid, expected.now, resolved_group, target_node,
                                         fresh_registry, fresh_snapshot, fresh_settings, fresh_identity)
                        self.status = f"已自动热切订阅“{target_profile_name}”并切换到“{target_node}”"
                    else:
                        self.status = f"已自动热切订阅“{target_profile_name}”，策略组当前已是“{target_node}”"
                else:
                    self.pending = (target_uid, target_node, measurement, fresh_registry, dict(self.snapshots))
                    self.status = f"已自动热切订阅“{target_profile_name}”，但节点条件已变化，请确认后继续应用"
            self._refresh_runtime_bridge_status(force_probe=False)
            return outcome, self.status
        except Exception:
            with self.lock:
                if self.latch.outcome_unknown:
                    self._invalidate()
                    self.status = "订阅已热切但节点切换结果未知，请到 Clash Verge 手动核验；切换已锁定"
                    return "outcome_unknown", self.status
                self.pending = (target_uid, target_node, measurement, fresh_registry, dict(self.snapshots))
                self.status = f"已自动热切订阅“{target_profile_name}”，请确认策略组后继续应用节点"
            return "not_attempted", self.status

    def _validate_launch(self, options, scheduled):
        if scheduled and (self.schedule.minutes is None or self.scheduled_options is not options):
            raise ValueError("定时已停止")
        if self.running or self.refreshing or self.closed.is_set():
            raise ValueError("测速进行中" if options.get("funnel_top") else "已有测速任务进行中")
        chosen = [r for r in self.rows if r.selected and r.uid in self.selected_profiles]
        if not self.registry or not chosen or not self.executable.is_file():
            raise ValueError("请先刷新并选择节点，确认测速程序存在")
        snapshots = {r.uid: self.snapshots[r.uid] for r in chosen}
        if sum(len(snapshots[r.uid].data) for r in chosen) > 64 * 1024 * 1024:
            raise ValueError("所选节点的配置总量超过安全上限")
        capture = self.capture
        if options["auto_switch"]:
            if self.latch.outcome_unknown or not capture or not any(g.name == options["group"] for g in capture[4]):
                raise ValueError("请先刷新并选择可用策略组")
        return chosen, snapshots, capture

    def _start_validated_launch(self, options, token, scheduled, chosen, snapshots, capture,
                                run_kind="normal", source=None):
        generation = self.generation
        if run_kind == "patrol":
            streams = 2
            latency, download = "3s", "3s"
            parallel = min(2, max(1, len(chosen)))
        else:
            base_streams = 2 if options["mode"] == "fast" else 4
            max_chunk = 5 * 1_048_576
            # Cloudflare /__down rejects >5 MiB chunks (e.g. 50MiB/4=13107200B) with HTTP 403 on proxy IPs.
            min_streams = (options["download_bytes"] + max_chunk - 1) // max_chunk
            streams = max(base_streams, min_streams)
            latency, download = "3s", "3s"
            parallel = options["parallel"]
        if source is None:
            if run_kind == "patrol":
                source = "patrol"
            elif options.get("funnel_top"):
                source = "funnel_s1"
            elif scheduled:
                source = "scheduled"
            elif options["download_bytes"] >= 20 * 1_048_576:
                source = "load_20m"
            else:
                source = "manual"
        jobs = [Job(row.uid, row.name, snapshots[row.uid].data, options["download_bytes"],
                    streams, latency, download) for row in chosen]
        runner = MultiBatchRunner(self.executable, jobs, parallel,
                   prelaunch=lambda _job: self._prelaunch(generation, snapshots.values()))
        chosen_set = {(r.uid, r.name) for r in chosen}
        for row in self.rows:
            if run_kind in ("patrol", "retest"):
                if (row.uid, row.name) in chosen_set:
                    row.measurement = None
                    row.stage1_score = None
                    row.samples = []
                    row.state = "等待"
            else:
                row.measurement = None
                row.stage1_score = None
                row.samples = []
                row.state = "等待" if row in chosen else "未勾选"
        self.running = True
        self.active_run_kind = run_kind
        self.run_targets = set(chosen_set)
        self.run_total = len(chosen)
        self.run_done = 0
        self.live_progress.clear()
        self.recently_scored.clear()
        self.cascade_highlighted.clear()
        self.funnel_stage = 1 if options.get("funnel_top") else None
        self.pending = None
        if options.get("group"):
            self.probe_group = options["group"]
        if run_kind == "patrol":
            self.status = "智能巡检中…"
        elif run_kind == "retest":
            self.status = f"单节点复测中（{chosen[0].name} · 20 MiB）…"
        else:
            self.status = "测速中"
        self.runner = runner
        self._notify_tray()
        threading.Thread(target=self._run, args=(runner, options, capture, snapshots,
                                                 generation, token, scheduled, run_kind, source), daemon=True).start()

    def _launch(self, options, token, scheduled):
        with self.lock:
            chosen, snapshots, capture = self._validate_launch(options, scheduled)
            self._start_validated_launch(options, token, scheduled, chosen, snapshots, capture)

    def _prelaunch(self, generation, snapshots):
        with self.lock:
            if generation != self.generation or self.closed.is_set():
                return False
            registry = self.registry
        revalidate(registry, snapshots)
        return True

    def start(self, value):
        token = None
        try:
            options = self._options(value)
            if self.registry and self.executable.is_file() and not self.running:
                try:
                    sync_code, sync_msg = self._sync_stale_registry_for_manual_action(
                        need_groups=options["auto_switch"], requested_group=options["group"]
                    )
                except ProfileError:
                    sync_code, sync_msg = "abort", "Clash Verge 订阅配置已变更且无法读取，请点击“刷新”重新加载"
                if sync_code == "abort":
                    return {"ok": False, "error": sync_msg}
            with self.lock:
                chosen, snapshots, capture = self._validate_launch(options, False)
                token = self.schedule.begin_manual()
                if token is None:
                    raise ValueError("已有测速任务进行中")
                if self.patrol_schedule.active is not None:
                    self.schedule.finish(token)
                    token = None
                    raise ValueError("已有测速任务进行中")
                if self.schedule.minutes is not None and options != self.scheduled_options:
                    self._invalidate()
                self._start_validated_launch(options, token, False, chosen, snapshots, capture)
            return {"ok": True}
        except (ValueError, KeyError, TypeError) as exc:
            if token is not None:
                with self.lock:
                    self.schedule.finish(token)
            message = str(exc) if str(exc) and isinstance(exc, ValueError) else "测速参数无效"
            return {"ok": False, "error": message}

    def retest_node(self, value):
        """Losslessly retest a single node at 20 MiB without wiping other scores or selections."""
        token = None
        try:
            uid, node = value["uid"], value["name"]
            auto, group = value["auto_switch"], value["group"]
            if not isinstance(uid, str) or not isinstance(node, str) or type(auto) is not bool or not isinstance(group, str):
                raise ValueError("复测参数无效")
            if not auto and not group and self.registry and self.executable.is_file() and not self.running:
                try:
                    sync_code, sync_msg = self._sync_stale_registry_for_manual_action(need_groups=False)
                except ProfileError:
                    sync_code, sync_msg = "abort", "Clash Verge 订阅配置已变更且无法读取，请点击“刷新”重新加载"
                if sync_code == "abort":
                    return {"ok": False, "error": sync_msg}
            options = {"parallel": 1, "download_bytes": 20 * 1_048_576, "mode": "accurate",
                       "auto_switch": auto, "group": group}
            with self.lock:
                if (self.running or self.refreshing or self.schedule.active is not None or
                        self.patrol_schedule.active is not None or self.closed.is_set() or not self.registry):
                    raise ValueError("当前无法开始复测")
                if uid not in self.snapshots or not any(r.uid == uid and r.name == node for r in self.rows):
                    raise ValueError("节点或订阅已变化，请重新选择")
                previous = self.capture
                original_group = None
                if auto or group:
                    if not group or not previous or self.latch.outcome_unknown:
                        raise ValueError("请先刷新并选择可用策略组")
                    matches = [g for g in previous[4] if g.name == group]
                    if len(matches) != 1:
                        raise ValueError("原策略组不可用，请重新选择")
                    original_group = matches[0]
                if self.schedule.minutes is not None:
                    self._invalidate()
                generation = self.generation
            if original_group is not None:
                if not self.refresh_groups()["ok"]:
                    raise ValueError("策略组刷新失败，未开始复测")
            with self.lock:
                if generation != self.generation or self.running or self.refreshing or self.closed.is_set():
                    raise ValueError("复测条件已变化，请重新选择")
                if original_group is not None:
                    refreshed = self.capture
                    matches = [] if not refreshed else [g for g in refreshed[4] if g.name == group]
                    if not refreshed or refreshed[:4] != previous[:4] or matches != [original_group]:
                        self.capture, self.groups = None, []
                        raise ValueError("策略组或控制器已变化，请刷新并重新确认")
                if not self.executable.is_file():
                    raise ValueError("找不到测速核心 clash-speedtest.exe")
                target_row = next((r for r in self.rows if r.uid == uid and r.name == node), None)
                if target_row is None or uid not in self.snapshots:
                    raise ValueError("节点或订阅已变化，请重新选择")
                capture = self.capture
                if auto:
                    if not group or not capture or self.latch.outcome_unknown or not any(g.name == group for g in capture[4]):
                        raise ValueError("请先刷新并选择可用策略组")
                if uid not in self.selected_profiles:
                    self.selected_profiles.add(uid)
                token = self.schedule.begin_manual()
                if token is None:
                    raise ValueError("已有测速任务进行中")
                self._start_validated_launch(options, token, False, [target_row], {uid: self.snapshots[uid]}, capture,
                                             run_kind="retest", source="retest")
                return {"ok": True}
        except (KeyError, TypeError, ValueError) as exc:
            if token is not None:
                with self.lock:
                    self.schedule.finish(token)
            message = str(exc) if isinstance(exc, ValueError) and str(exc) else "复测参数无效"
            with self.lock:
                self.status = message
            return {"ok": False, "error": message}

    def start_funnel(self, value):
        token = None
        try:
            options = self._options(value)
            top_count = value["top_count"]
            if type(top_count) is not int or not 1 <= top_count <= 10:
                raise ValueError("晋级数须为 1 至 10")
            options.update(parallel=10, download_bytes=1_048_576, mode="fast",
                           funnel_top=top_count)
            if self.registry and self.executable.is_file() and not self.running:
                try:
                    sync_code, sync_msg = self._sync_stale_registry_for_manual_action(
                        need_groups=options["auto_switch"], requested_group=options["group"]
                    )
                except ProfileError:
                    sync_code, sync_msg = "abort", "Clash Verge 订阅配置已变更且无法读取，请点击“刷新”重新加载"
                if sync_code == "abort":
                    return {"ok": False, "error": sync_msg}
            with self.lock:
                chosen, snapshots, capture = self._validate_launch(options, False)
                token = self.schedule.begin_manual()
                if token is None:
                    raise ValueError("已有测速任务进行中")
                if self.patrol_schedule.active is not None:
                    self.schedule.finish(token)
                    token = None
                    raise ValueError("已有测速任务进行中")
                self._invalidate()
                self._start_validated_launch(options, token, False, chosen, snapshots, capture,
                                             run_kind="normal", source="funnel_s1")
            return {"ok": True}
        except (ValueError, KeyError, TypeError) as exc:
            if token is not None:
                with self.lock:
                    self.schedule.finish(token)
            message = str(exc) if str(exc) and isinstance(exc, ValueError) else "测速参数无效"
            return {"ok": False, "error": message}

    @staticmethod
    def _invoke_runner(runner, on_result, on_progress):
        try:
            params = inspect.signature(runner.run).parameters
        except (TypeError, ValueError):
            params = {}
        if "on_progress" in params:
            return runner.run(on_result, on_progress=on_progress)
        return runner.run(on_result)

    def _run(self, runner, options, capture, snapshots, generation, token, scheduled,
             run_kind="normal", source="manual"):
        try:
            revalidate(self.registry, snapshots.values())
            fatal = []
            expected_results = {(job.uid, job.name) for job in runner.jobs}
            received_results = set()
            current_source = [source]
            current_jobs = {(job.uid, job.name): job for job in runner.jobs}
            buffered_samples = []

            def flush_telemetry():
                if runner.cancelled.is_set() or self.closed.is_set() or fatal:
                    return
                for rec in buffered_samples:
                    try:
                        self.telemetry_store.record_sample(rec)
                    except Exception:
                        pass
                buffered_samples.clear()

            def on_progress(job, update):
                identity = (job.uid, job.name)
                with self.lock:
                    if generation != self.generation or self.closed.is_set():
                        return
                    row = next((r for r in self.rows if r.uid == job.uid and r.name == job.name), None)
                    if not row or row.state != "等待":
                        return
                    prev = self.live_progress.get(identity)
                    samples = list(prev["samples"]) if prev else []
                    if update.phase == "download" and update.bps > 0:
                        samples.append(round(update.bps / 1_048_576, 3))
                        if len(samples) > 24:
                            samples = samples[-24:]
                    self.live_progress[identity] = {
                        "phase": update.phase,
                        "received_bytes": update.received_bytes,
                        "expected_bytes": update.expected_bytes,
                        "bps": update.bps,
                        "samples": samples,
                    }

            def result(job, state, measurement):
                identity = (job.uid, job.name)
                if options.get("funnel_top") and (identity not in expected_results or identity in received_results):
                    fatal.append("unexpected_result")
                    runner.cancel()
                    return
                received_results.add(identity)
                effective_state = "cancelled" if (state == "cancelled" or runner.cancelled.is_set()) else state
                sample_to_record = None
                with self.lock:
                    prev_live = self.live_progress.pop(identity, None)
                    samples = list(prev_live.get("samples", [])) if prev_live else []
                    if measurement and measurement.download_bytes_per_second > 0:
                        final_mib = round(measurement.download_bytes_per_second / 1_048_576, 3)
                        if len(samples) == 1:
                            samples.append(final_mib)
                    if len(samples) > 24:
                        samples = samples[-24:]
                    self.run_done += 1
                    row = next((r for r in self.rows if r.uid == job.uid and r.name == job.name), None)
                    if row:
                        row.measurement = measurement
                        row.samples = samples
                        if measurement and measurement.node_type:
                            row.node_type = measurement.node_type
                        row.state = {"ok": "完成" if measurement and measurement.download_complete else "下载未完成", "cancelled": "已停止", "timeout": "超时",
                                     "profile_changed": "配置已变更"}.get(effective_state, "失败")
                        self.recently_scored[identity] = time.monotonic() + 1.4
                        self.score_revision += 1
                        if effective_state in ("ok", "timeout") and not runner.cancelled.is_set() and row.eligible:
                            sample_to_record = TelemetrySampleRecord(
                                timestamp=time.time(),
                                profile_uid=row.uid,
                                profile_name=row.profile,
                                node_name=row.name,
                                node_type=measurement.node_type if measurement else "—",
                                source=current_source[0],
                                requested_bytes=job.download_bytes,
                                streams=job.streams,
                                download_complete=measurement.download_complete if measurement else False,
                                download_bytes_per_second=measurement.download_bytes_per_second if measurement else 0.0,
                                latency_ms=measurement.latency_ms if measurement else 0.0,
                                jitter_ms=measurement.jitter_ms if measurement else 0.0,
                                head_attempts=measurement.head_attempts if measurement else 6,
                                head_successes=measurement.head_successes if measurement else 0,
                                composite_score=measurement.score if measurement else None,
                            )
                if sample_to_record is not None:
                    buffered_samples.append(sample_to_record)
                if options.get("funnel_top") and effective_state in ("worker_failed", "invalid_job", "invalid_output", "profile_changed"):
                    fatal.append(effective_state)
                    runner.cancel()
            self._invoke_runner(runner, result, on_progress)
            if options.get("funnel_top"):
                if received_results != expected_results and not runner.cancelled.is_set():
                    fatal.append("missing_result")
                if runner.cancelled.is_set() or fatal:
                    if fatal:
                        with self.lock:
                            self.status = "初筛失败或配置已变更，未开始加压复测"
                    return
                revalidate(self.registry, snapshots.values())
                with self.lock:
                    if generation != self.generation or self.closed.is_set() or runner.cancelled.is_set():
                        return
                    candidates = [r for r in self.rows if r.selected and r.uid in snapshots and r.measurement
                                  and r.measurement.score is not None
                                  and _eligible_name(r.name)]
                    candidates.sort(key=lambda r: r.measurement.score, reverse=True)
                    promoted = candidates[:options["funnel_top"]]
                    if not promoted:
                        self.status = "初筛没有合格节点，未开始加压复测"
                if not promoted:
                    flush_telemetry()
                    return
                with self.lock:
                    selected = {(r.uid, r.name) for r in promoted}
                    jobs = [Job(r.uid, r.name, snapshots[r.uid].data, 20 * 1_048_576,
                                4, "3s", "3s") for r in promoted]
                    cascade_exp = time.monotonic() + 1.0
                    for row in self.rows:
                        is_promoted = (row.uid, row.name) in selected
                        if is_promoted:
                            row.stage1_score = None
                            row.measurement = None
                            row.samples = []
                            row.state = "等待"
                            self.cascade_highlighted[(row.uid, row.name)] = cascade_exp
                        else:
                            prev_score = row.measurement.score if row.measurement and _eligible_name(row.name) else None
                            row.measurement = None
                            row.samples = []
                            if prev_score is not None:
                                row.stage1_score = prev_score
                                row.state = f"初筛 {prev_score:.1f}分"
                            else:
                                row.stage1_score = None
                                row.state = "未晋级"
                    self.live_progress.clear()
                    self.run_targets = set(selected)
                    self.run_total = len(promoted)
                    self.run_done = 0
                    self.score_revision += 1
                    self.status = f"初筛完成，{len(promoted)} 个节点进行 20 MiB 加压复测"
                    self.funnel_stage = 2
                    current_source[0] = "funnel_s2"
                    runner = MultiBatchRunner(self.executable, jobs, 2,
                             prelaunch=lambda _job: self._prelaunch(generation, snapshots.values()))
                    self.runner = runner
                    expected_results = {(job.uid, job.name) for job in runner.jobs}
                    received_results.clear()
                self._invoke_runner(runner, result, on_progress)
                if received_results != expected_results and not runner.cancelled.is_set():
                    fatal.append("missing_result")
                if runner.cancelled.is_set() or fatal:
                    if fatal:
                        with self.lock:
                            self.status = "加压复测失败或配置已变更，未自动切换"
                    return
                revalidate(self.registry, snapshots.values())
            flush_telemetry()
            if run_kind == "patrol":
                with self.lock:
                    self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                    used_mib = int((self.cached_patrol_bytes_today + 1_048_575) // 1_048_576) if self.cached_patrol_bytes_today else 0
                    quota_mib = self.patrol_quota_bytes // 1_048_576
                    if not runner.cancelled.is_set() and not self.closed.is_set():
                        count = len(current_jobs)
                        self.patrol_last_summary = f"本次 {count} 节点 · 今日 {used_mib}/{quota_mib} MiB"
                        self.status = f"智能巡检完成：已更新 {count} 个节点（今日 {used_mib}/{quota_mib} MiB，不改变节点勾选）"
                return
            with self.lock:
                if run_kind == "retest":
                    candidates = [r for r in self.rows if (r.uid, r.name) in expected_results
                                  and r.measurement and r.measurement.score is not None
                                  and _eligible_name(r.name)]
                else:
                    candidates = [r for r in self.rows if r.measurement and r.measurement.score is not None
                                  and _eligible_name(r.name)]
                winner = max(candidates, key=lambda r: r.measurement.score) if candidates else None
                allowed = generation == self.generation and not runner.cancelled.is_set() and not self.closed.is_set()
                self.score_revision += 1
            if allowed and options["auto_switch"] and winner:
                self._automatic(winner, options["group"], capture, generation)
            elif allowed:
                with self.lock:
                    if run_kind == "retest":
                        self.status = f"单节点复测完成（{winner.name}）" if winner else "单节点复测完成，未能取得有效评分"
                    else:
                        self.status = "测速完成" if winner else "测速完成，无可评分节点"
        except Exception as exc:
            with self.lock:
                aborted_state = "配置已变更" if isinstance(exc, ProfileError) else "已停止"
                for row in self.rows:
                    if row.state == "等待":
                        row.state = aborted_state
                self.score_revision += 1
                if isinstance(exc, ProfileError):
                    self.status = "测速中途检测到 Clash Verge 配置已变更，请重新点“刷新”后再测速"
                else:
                    self.status = "测速失败或配置已变更"
                self._invalidate()
        finally:
            with self.lock:
                self.running, self.runner = False, None
                self.live_progress.clear()
                self.run_targets.clear()
                self.run_total = 0
                self.run_done = 0
                self.funnel_stage = None
                self.active_run_kind = "normal"
                if run_kind == "patrol":
                    self.patrol_schedule.finish(token)
                    self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                else:
                    self.schedule.finish(token)
            self._notify_tray()

    def _automatic(self, winner, group_name, capture, generation):
        registry, snapshot, settings, identity, groups = capture
        if winner.uid != snapshot.profile.uid:
            def _is_cancelled():
                with self.lock:
                    return generation != self.generation or self.closed.is_set() or (
                        self.runner is not None and self.runner.cancelled.is_set()
                    )
            self._hot_switch_cross_profile(
                winner.uid,
                winner.name,
                group_name,
                winner.measurement,
                generation,
                is_cancelled=_is_cancelled,
                allow_pending_fallback=True,
            )
            return
        try:
            expected = next(g for g in groups if g.name == group_name)
            switch = AssistedSwitch(registry, snapshot, winner.name, winner.measurement, self.latch)
            groups_now = switch.prepare()
            found = next(g for g in groups_now if g.name == group_name)
            if (switch.settings != settings or switch.controller.identity != identity or found.now != expected.now):
                raise ControllerError("changed")
            def authorized():
                with self.lock:
                    return generation == self.generation and not self.closed.is_set() and not self.runner.cancelled.is_set()
            outcome = switch.apply(group_name, authorized)
            with self.lock:
                if (outcome == "switched" and expected.now in snapshot.names and
                        expected.now in expected.all and expected.now != winner.name):
                    self.undo = (snapshot.profile.uid, expected.now, group_name, winner.name,
                                 registry, snapshot, settings, identity)
                if outcome == "switched":
                    self._sync_group_now_locked(group_name, winner.name)
                self.status = {"switched": "已切换并复核", "confirmed_noop": "当前已是最优节点",
                               "outcome_unknown": "切换结果未知，请到 Clash Verge 手动核验；自动切换已锁定"}.get(outcome, "切换条件已变化，未自动切换")
                if outcome == "outcome_unknown":
                    self._invalidate()
        except Exception:
            with self.lock:
                self.status = "切换条件已变化，未自动切换"

    def stop(self):
        with self.lock:
            if self.runner:
                self.runner.cancel()
            self._invalidate()
            self.status = "正在停止；已发出的切换无法撤回"
        return {"ok": True}

    def set_schedule(self, value):
        try:
            minutes = value["minutes"]
            if type(minutes) is int and minutes == 0:
                with self.lock:
                    self.schedule.disarm()
                    self.scheduled_options = None
                return {"ok": True}
            options = self._options(value["options"])
            with self.lock:
                if (self.refreshing or self.running or self.schedule.active is not None or
                        self.patrol_schedule.active is not None or self.closed.is_set()):
                    raise ValueError()
                if not any(r.selected and r.uid in self.selected_profiles for r in self.rows):
                    raise ValueError()
                if options["auto_switch"] and (not self.capture or self.latch.outcome_unknown):
                    raise ValueError()
                if self.patrol_schedule.minutes is not None:
                    self._disarm_patrol()
                self.schedule.arm(minutes)
                self.scheduled_options = options
                if options.get("group"):
                    self.probe_group = options["group"]
                self.status = "定时已启用，支持托盘后台运行，退出应用后停止"
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "无法启用定时，请检查节点与策略组"}

    def set_patrol(self, value):
        try:
            enabled = value["enabled"]
            quota = value.get("quota_bytes", self.patrol_quota_bytes)
            if type(enabled) is not bool or type(quota) is not int or quota not in _PATROL_QUOTAS:
                raise ValueError("巡检参数无效")
            with self.lock:
                if self.running or self.refreshing or self.closed.is_set():
                    raise ValueError("测速或刷新进行中")
                if quota != self.patrol_quota_bytes:
                    self.patrol_quota_bytes = quota
                    if self.patrol_schedule.minutes is not None and not enabled:
                        self._disarm_patrol()
                if not enabled:
                    self._disarm_patrol("智能巡检已停止")
                    self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                    return {"ok": True}
                if not self.registry or not self.snapshots or not self.executable.is_file():
                    raise ValueError("请先刷新并加载有效订阅")
                candidates = [r for r in self.rows if r.uid in self.selected_profiles and _eligible_name(r.name)]
                if not candidates:
                    candidates = [r for r in self.rows if _eligible_name(r.name)]
                if not candidates:
                    raise ValueError("没有可巡检的节点")
                if self.schedule.minutes is not None:
                    self._invalidate(clear_pending=False)
                snapshots = {r.uid: self.snapshots[r.uid] for r in candidates if r.uid in self.snapshots}
                registry = self.registry
            revalidate(registry, snapshots.values())
            interval_min = 30 if quota >= 150 * 1_048_576 else 60
            with self.lock:
                if self.running or self.refreshing or self.closed.is_set() or self.registry is not registry:
                    raise ValueError("状态已变化，未开启巡检")
                self.patrol_schedule.arm(interval_min)
                self.patrol_snapshots = dict(snapshots)
                self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                self.cached_patrol_quota_day = date.today()
                quota_mib = quota // 1_048_576
                batch_cap = _PATROL_BATCH_BY_QUOTA.get(quota, 5)
                self.status = f"智能巡检已开启：每 {interval_min} 分钟最多抽测 {batch_cap} 节点（1MiB/2流，日配额 {quota_mib} MiB，晚高峰预留 25%，不改节点勾选、不自动切换）"
            return {"ok": True}
        except (KeyError, TypeError, ValueError, ProfileError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and str(exc) else "无法开启智能巡检"
            with self.lock:
                self.status = message
            return {"ok": False, "error": message}

    def get_history(self, value=None):
        try:
            window = "7d" if value is None else value.get("window", "7d")
            if window not in WINDOW_SPECS:
                raise ValueError("invalid window")
            summaries = self.telemetry_store.compute_summaries(window=window)
            with self.lock:
                snapshots = dict(self.snapshots)
                selected_profiles = set(self.selected_profiles)
                profile_names = {p.uid: p.name for p in self.registry.profiles} if self.registry else {}
            items = []
            for s in summaries:
                snap = snapshots.get(s.profile_uid)
                available = bool(snap and s.node_name in snap.names)
                items.append({
                    "uid": s.profile_uid,
                    "profile": profile_names.get(s.profile_uid, s.profile_name),
                    "name": s.node_name,
                    "node_type": s.node_type,
                    "long_term_score": s.long_term_score,
                    "base_score": s.base_score,
                    "health_penalty": s.health_penalty,
                    "active_failure_streak": s.active_failure_streak,
                    "weighted_median_score": s.weighted_median_score,
                    "weighted_p20_score": s.weighted_p20_score,
                    "weighted_median_speed_bps": s.weighted_median_speed_bps,
                    "weighted_median_latency_ms": s.weighted_median_latency_ms,
                    "weighted_median_jitter_ms": s.weighted_median_jitter_ms,
                    "availability_rate": s.availability_rate,
                    "weighted_availability_rate": s.weighted_availability_rate,
                    "total_count": s.total_count,
                    "success_count": s.success_count,
                    "failure_count": s.failure_count,
                    "is_qualified": s.is_qualified,
                    "last_tested_at": s.last_tested_at,
                    "last_health_check_at": s.last_health_check_at,
                    "last_health_ok": s.last_health_ok,
                    "last_latency_ms": s.last_latency_ms,
                    "latest_speed_bps": s.latest_speed_bps,
                    "latest_score": s.latest_score,
                    "available": available,
                    "selected_profile": s.profile_uid in selected_profiles,
                })
            return {"ok": True, "window": window, "summaries": items}
        except Exception:
            return {"ok": False, "error": "读取长效历史失败", "summaries": []}

    def clear_history(self):
        with self.lock:
            if self.running or self.refreshing or self.closed.is_set():
                return {"ok": False, "error": "测速或刷新进行中"}
        try:
            self.telemetry_store.clear_all()
            with self.lock:
                self.cached_patrol_bytes_today = 0
                self.patrol_last_summary = ""
                self.score_revision += 1
                self.status = "已清空全部长效历史记录与健康探测数据"
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "清空历史记录失败"}

    def _trigger_health_probe_async(self):
        with self.lock:
            if self.closed.is_set() or self.running or self.refreshing or self.health_probe_inflight or not self.capture:
                return
            self.health_probe_inflight = True
            self._last_health_probe_at = time.monotonic()
            thread = threading.Thread(target=self._run_health_probe, daemon=True)
            self._health_probe_thread = thread
        thread.start()

    def probe_active_node(self, value=None):
        try:
            requested_group = None
            if value is not None:
                if not isinstance(value, dict):
                    raise ValueError("探测参数无效")
                g = value.get("group")
                if g is not None:
                    if not isinstance(g, str):
                        raise ValueError("策略组参数无效")
                    requested_group = g.strip() or None
            with self.lock:
                if self.closed.is_set() or self.running or self.refreshing or self._switching_count > 0:
                    return {"ok": False, "error": "当前任务进行中，请稍后再探测"}
                if not self.capture:
                    return {"ok": False, "error": "请先刷新策略组后再探测"}
                groups = self.capture[4]
                if requested_group is not None:
                    if not any(g.name == requested_group for g in groups):
                        return {"ok": False, "error": "所选策略组不可用，请刷新策略组"}
                    self.probe_group = requested_group
                if self.health_probe_inflight:
                    return {"ok": True, "inflight": True}
                self.health_probe_status = "● 探测中…"
                self.health_probe_level = "probing"
                self.health_probe_inflight = True
                self._last_health_probe_at = time.monotonic()
                thread = threading.Thread(target=self._run_health_probe, daemon=True)
                self._health_probe_thread = thread
            self._notify_tray()
            thread.start()
            return {"ok": True}
        except ValueError as exc:
            return {"ok": False, "error": str(exc) or "探测参数无效"}
        except Exception:
            return {"ok": False, "error": "触发活跃探测失败"}

    def _run_health_probe(self):
        with self._health_probe_lock:
            self._run_health_probe_locked()

    def _run_health_probe_locked(self):
        try:
            with self.lock:
                if self.closed.is_set() or self.running or self.refreshing or not self.capture:
                    return
                registry, snapshot, settings, _identity, groups = self.capture
                group_name = self.probe_group or self.suggested_group
                if not group_name or not any(g.name == group_name for g in groups):
                    group_name = self._default_probe_group(groups, snapshot.names)
                if not group_name:
                    return
            controller = PipeController(settings)
            live_group = controller.group(group_name)
            active_node = live_group.now
            if active_node not in snapshot.names:
                with self.lock:
                    self.health_probe_status = f"● 非节点({active_node})"
                    self.health_probe_level = "idle"
                return
            now_ts = time.time()
            try:
                delay_ms = controller.probe_node_delay(active_node, timeout_ms=3500)
                self.telemetry_store.record_health_event(TelemetryHealthEvent(
                    timestamp=now_ts,
                    profile_uid=snapshot.profile.uid,
                    node_name=active_node,
                    group_name=live_group.name,
                    ok=True,
                    delay_ms=delay_ms,
                    failure_kind=None,
                ))
                with self.lock:
                    self.health_probe_status = f"● 活跃正常 {round(delay_ms)}ms"
                    self.health_probe_level = "ok"
                    self.score_revision += 1
            except ProbeError:
                self.telemetry_store.record_health_event(TelemetryHealthEvent(
                    timestamp=now_ts,
                    profile_uid=snapshot.profile.uid,
                    node_name=active_node,
                    group_name=live_group.name,
                    ok=False,
                    delay_ms=None,
                    failure_kind="probe_failed",
                ))
                hstate = self.telemetry_store.health_state(snapshot.profile.uid, active_node)
                with self.lock:
                    if hstate.penalty > 0:
                        self.health_probe_status = f"● 活跃异常 -{hstate.penalty:.0f}分"
                        self.health_probe_level = "danger"
                    else:
                        self.health_probe_status = "● 活跃探测超时(1次)"
                        self.health_probe_level = "warn"
                    self.score_revision += 1
        except ControllerError:
            with self.lock:
                self.health_probe_status = "● 控制器未连通"
                self.health_probe_level = "idle"
        except Exception:
            pass
        finally:
            with self.lock:
                self.health_probe_inflight = False
            self._notify_tray()

    def _poll_patrol(self):
        today = date.today()
        with self.lock:
            if today != self.cached_patrol_quota_day:
                self.cached_patrol_quota_day = today
                self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                if self.patrol_last_summary in ("今日配额已满", "晚高峰配额预留中"):
                    self.patrol_last_summary = ""
            p_state, p_token = self.patrol_schedule.poll(
                external_busy=self.running or self.refreshing or self.schedule.active is not None or self.closed.is_set()
            )
            snapshots = self.patrol_snapshots
            registry = self.registry
            quota = self.patrol_quota_bytes
        if p_state == "skipped_busy":
            with self.lock:
                self.patrol_last_summary = "已有测速运行，本次巡检跳过"
            return
        if p_state != "started":
            return
        try:
            if not snapshots or not registry or not self.executable.is_file():
                raise ValueError("巡检快照不可用")
            revalidate(registry, snapshots.values())
            with self.lock:
                candidates = [
                    (r.uid, r.name) for r in self.rows
                    if r.uid in snapshots and _eligible_name(r.name)
                ]
            batch_cap = _PATROL_BATCH_BY_QUOTA.get(quota, 5)
            plan = self.telemetry_store.select_patrol_plan(
                candidates,
                daily_quota_bytes=quota,
                per_node_bytes=1_048_576,
                max_per_tick=batch_cap,
                reserve_peak_ratio=0.25,
                overnight_throttle=True,
            )
            with self.lock:
                self.cached_patrol_bytes_today = max(0, quota - plan.remaining_quota_bytes)
                self.cached_patrol_quota_day = today
                if not plan.identities:
                    if plan.deferred_for_peak_quota:
                        reserve_mib = max(1, plan.peak_reserved_bytes // 1_048_576)
                        self.patrol_last_summary = "晚高峰配额预留中"
                        self.status = f"白天巡检预算已达上限，已为 20:00 晚高峰保留 {reserve_mib} MiB，到点自动恢复"
                    elif plan.remaining_quota_bytes < 1_048_576:
                        self.patrol_last_summary = "今日配额已满"
                        self.status = f"今日智能巡检配额已用完（{quota // 1_048_576} MiB/天），明日自动恢复"
                    elif plan.skipped_cooldown_count > 0 or plan.skipped_backoff_count > 0:
                        self.patrol_last_summary = "冷却/退避等待中"
                        self.status = (
                            f"本轮巡检跳过：{plan.skipped_cooldown_count} 个节点近期已测在冷却期、"
                            f"{plan.skipped_backoff_count} 个连续失败节点在退避期，稍后自动复查"
                        )
                    else:
                        self.patrol_last_summary = "暂无待巡检节点"
                        self.status = "当前没有待巡检的节点"
                    self.patrol_schedule.finish(p_token)
                    return
                id_set = set(plan.identities)
                chosen = [r for r in self.rows if (r.uid, r.name) in id_set and r.uid in snapshots]
                if not chosen:
                    self.patrol_schedule.finish(p_token)
                    return
                options = {"parallel": min(2, len(chosen)), "download_bytes": 1_048_576,
                           "mode": "fast", "auto_switch": False, "group": ""}
                self.status = f"智能巡检：守擂 {plan.defender_count} + 探索 {plan.explorer_count}（1MiB/2流）…"
                self._start_validated_launch(options, p_token, True, chosen, snapshots, None,
                                             run_kind="patrol", source="patrol")
        except Exception:
            with self.lock:
                self.patrol_schedule.finish(p_token)
                self._disarm_patrol("订阅快照已变化，智能巡检已停止")

    def _timer(self):
        while not self.closed.wait(1):
            with self.lock:
                state, token = self.schedule.poll(
                    external_busy=self.running or self.refreshing or self.patrol_schedule.active is not None or self.closed.is_set())
                options = self.scheduled_options
                previous = self.capture
            if state == "started":
                try:
                    if not options:
                        raise ValueError()
                    # Refresh the consent-bound controller/group snapshot before
                    # each recurring run; never change the user's chosen group.
                    if options["auto_switch"]:
                        if not previous or not self.refresh_groups(preserve_schedule=True)["ok"]:
                            raise ValueError()
                    with self.lock:
                        if options["auto_switch"]:
                            current = self.capture
                            if (not current or current[:4] != previous[:4] or
                                    not any(g.name == options["group"] for g in current[4])):
                                raise ValueError()
                        chosen, snapshots, capture = self._validate_launch(options, True)
                        self._start_validated_launch(options, token, True, chosen, snapshots, capture)
                except Exception:
                    with self.lock:
                        self.schedule.finish(token)
                        if self.scheduled_options is options:
                            self.schedule.disarm()
                            self.scheduled_options = None
                            self.status = "定时已停止：配置或授权已变化"
            self._poll_patrol()
            now_mono = time.monotonic()
            with self.lock:
                should_probe = (
                    not self.running and not self.refreshing and not self.health_probe_inflight
                    and self.capture is not None and (now_mono - self._last_health_probe_at) >= 60.0
                )
            if should_probe:
                self._trigger_health_probe_async()
            self._notify_tray()

    def switch_manual(self, value):
        switching_entered = False
        try:
            uid, node = value["uid"], value["name"]
            group = value.get("group", "")
            with self.lock:
                if self.running or self.refreshing or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
                self._switching_count += 1
                switching_entered = True
                if self.schedule.minutes is not None:
                    self._invalidate()
                generation = self.generation
                capture = self.capture
                current_uid = capture[1].profile.uid if capture else (
                    next((p.uid for p in self.registry.profiles if p.current), None) if self.registry else None
                )
            if current_uid != uid:
                outcome, message = self._hot_switch_cross_profile(
                    uid,
                    node,
                    group,
                    measurement=None,
                    generation=generation,
                    is_cancelled=lambda: self.closed.is_set(),
                    allow_pending_fallback=False,
                )
                with self.lock:
                    self.status = message
                return {"ok": outcome in ("switched", "confirmed_noop"), "error": message}
            if not capture:
                raise ControllerError("unavailable")
            registry, snapshot, settings, identity, groups = capture
            expected = next(g for g in groups if g.name == group)
            switch = AssistedSwitch(registry, snapshot, node, latch=self.latch)
            found = next(g for g in switch.prepare() if g.name == group)
            if switch.settings != settings or switch.controller.identity != identity or found.now != expected.now:
                raise ControllerError("changed")
            def authorized():
                with self.lock:
                    return generation == self.generation and not self.closed.is_set()
            outcome = switch.apply(group, authorized)
            with self.lock:
                if (outcome == "switched" and expected.now in snapshot.names and
                        expected.now in expected.all and expected.now != node):
                    self.undo = (uid, expected.now, group, node, registry, snapshot, settings, identity)
                if outcome == "switched":
                    self._sync_group_now_locked(group, node)
                if outcome == "outcome_unknown":
                    self._invalidate()
                self.status = {"switched": "已切换并复核", "confirmed_noop": "该节点已被选中",
                               "outcome_unknown": "切换结果未知，请手动核验；切换已锁定"}.get(outcome, "未切换：条件已变化")
            return {"ok": outcome in ("switched", "confirmed_noop"), "error": self.status}
        except Exception:
            return {"ok": False, "error": "未切换：请刷新策略组并核对当前订阅"}
        finally:
            if switching_entered:
                with self.lock:
                    self._switching_count = max(0, self._switching_count - 1)
                self._notify_tray()

    def undo_switch(self):
        switching_entered = False
        try:
            with self.lock:
                if self.running or self.refreshing or not self.undo or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
                self._switching_count += 1
                switching_entered = True
                entry = self.undo
                uid, node, group, expected_now, registry, snapshot, settings, identity = entry
                if self.registry != registry or snapshot.profile.uid != uid:
                    raise ControllerError("changed")
                if self.schedule.minutes is not None:
                    self.schedule.disarm()
                    self.scheduled_options = None
                generation = self.generation
            switch = AssistedSwitch(registry, snapshot, node, latch=self.latch)
            found = next(g for g in switch.prepare() if g.name == group)
            if (switch.settings != settings or switch.controller.identity != identity or
                    found.now != expected_now or node not in found.all):
                raise ControllerError("changed")
            def authorized():
                with self.lock:
                    return generation == self.generation and not self.closed.is_set() and self.undo is entry
            outcome = switch.apply(group, authorized)
            with self.lock:
                if outcome == "outcome_unknown":
                    self._invalidate()
                elif outcome in ("switched", "confirmed_noop"):
                    self.undo = None
                    if outcome == "switched":
                        self._sync_group_now_locked(group, node)
                self.status = {"switched": "已切回原节点并复核", "confirmed_noop": "当前已是原节点",
                               "outcome_unknown": "切换结果未知，请手动核验；切换已锁定"}.get(outcome, "未切回：条件已变化")
            return {"ok": outcome in ("switched", "confirmed_noop"), "error": self.status}
        except Exception:
            return {"ok": False, "error": "未切回：策略组或订阅已变化，请重新核对"}
        finally:
            if switching_entered:
                with self.lock:
                    self._switching_count = max(0, self._switching_count - 1)
                self._notify_tray()

    def continue_pending(self, value):
        switching_entered = False
        try:
            group = value["group"]
            with self.lock:
                if self.running or self.refreshing or not self.capture or not self.pending or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
                self._switching_count += 1
                switching_entered = True
                if self.schedule.minutes is not None:
                    self._invalidate(clear_pending=False)
                uid, node, measurement, _old_registry, _old_snapshots = self.pending
                registry, snapshot, settings, identity, groups = self.capture
                generation = self.generation
                if snapshot.profile.uid != uid:
                    raise ControllerError("profile")
            expected = next(g for g in groups if g.name == group)
            switch = AssistedSwitch(registry, snapshot, node, measurement, self.latch)
            found = next(g for g in switch.prepare() if g.name == group)
            if switch.settings != settings or switch.controller.identity != identity or found.now != expected.now:
                raise ControllerError("changed")
            def authorized():
                with self.lock:
                    return generation == self.generation and not self.closed.is_set()
            outcome = switch.apply(group, authorized)
            with self.lock:
                if (outcome == "switched" and expected.now in snapshot.names and
                        expected.now in expected.all and expected.now != node):
                    self.undo = (uid, expected.now, group, node, registry, snapshot, settings, identity)
                if outcome == "switched":
                    self._sync_group_now_locked(group, node)
                if outcome == "outcome_unknown":
                    self._invalidate()
                elif outcome in ("switched", "confirmed_noop"):
                    self.pending = None
                self.status = {"switched": "已切换并复核", "confirmed_noop": "该节点已被选中",
                               "outcome_unknown": "切换结果未知，请手动核验；切换已锁定"}.get(outcome, "未切换：条件已变化")
            return {"ok": outcome in ("switched", "confirmed_noop"), "error": self.status}
        except Exception:
            return {"ok": False, "error": "无法继续：请确认当前订阅并重新刷新策略组"}
        finally:
            if switching_entered:
                with self.lock:
                    self._switching_count = max(0, self._switching_count - 1)
                self._notify_tray()

    def _build_tsv_export(self, value=None):
        req = value if isinstance(value, dict) else {}
        view = req.get("view", "live")
        if view == "history":
            window = req.get("window", "7d")
            if window not in WINDOW_SPECS:
                window = "7d"
            selected_only = bool(req.get("selected_only", False))
            query = str(req.get("query") or "").strip().lower()
            raw_ids = req.get("identities")
            identity_filter = None
            if isinstance(raw_ids, list) and raw_ids:
                parsed_ids = set()
                for entry in raw_ids:
                    if isinstance(entry, dict) and isinstance(entry.get("uid"), str) and isinstance(entry.get("name"), str):
                        parsed_ids.add((entry["uid"], entry["name"]))
                if parsed_ids:
                    identity_filter = parsed_ids
            with self.lock:
                selected_profiles = set(self.selected_profiles)
                known_nodes = {(r.uid, r.name) for r in self.rows}
            summaries = self.telemetry_store.compute_summaries(window=window)
            filtered = []
            for item in summaries:
                if identity_filter is not None and (item.profile_uid, item.node_name) not in identity_filter:
                    continue
                if identity_filter is None and selected_only and selected_profiles and item.profile_uid not in selected_profiles:
                    continue
                if identity_filter is None and query and query not in item.node_name.lower() and query not in item.profile_name.lower():
                    continue
                filtered.append(item)
            output = io.StringIO(newline="")
            writer = csv.writer(output, delimiter="\t", lineterminator="\n")
            writer.writerow([
                "排名", "状态", "订阅", "节点", "类型",
                "稳健吞吐", "典型延迟", "可用率", "样本",
                "长效稳健分", "基础分", "健康扣分",
            ])
            for idx, s in enumerate(filtered, 1):
                available = (s.profile_uid, s.node_name) in known_nodes
                if not available:
                    status_label = "已移除"
                elif s.is_qualified:
                    status_label = "已认证"
                else:
                    status_label = f"样本不足({s.total_count}/3)"
                speed = f"{s.weighted_median_speed_bps / 1_048_576:.2f} MiB/s" if s.success_count > 0 else "—"
                latency = f"{s.weighted_median_latency_ms:.0f} ms" if s.success_count > 0 else "—"
                avail = f"{round(s.availability_rate * 100)}%"
                samples = f"{s.success_count}/{s.total_count}"
                writer.writerow([
                    idx,
                    _cell(status_label),
                    _cell(s.profile_name),
                    _cell(s.node_name),
                    _cell((s.node_type or "—").upper()),
                    _cell(speed),
                    _cell(latency),
                    _cell(avail),
                    _cell(samples),
                    f"{s.long_term_score:.1f}",
                    f"{s.base_score:.1f}",
                    f"{s.health_penalty:.1f}",
                ])
            return output.getvalue(), "clash-history-results.tsv", True

        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(["订阅", "节点", "状态", "综合分", "速度 B/s", "延迟 ms", "抖动 ms"])
        with self.lock:
            for row in self.rows:
                m = row.measurement
                writer.writerow([_cell(row.profile), _cell(row.name), _cell(row.state),
                                 m.score if m and m.score is not None else "", m.download_bytes_per_second if m else "",
                                 m.latency_ms if m else "", m.jitter_ms if m else ""])
        return output.getvalue(), "clash-speed-results.tsv", False

    def export_tsv(self, value=None):
        try:
            import webview
            text, filename, is_history = self._build_tsv_export(value)
            paths = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename)
            if not paths:
                return {"ok": True, "cancelled": True}
            Path(paths if isinstance(paths, str) else paths[0]).write_text(text, encoding="utf-8-sig")
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                pass
            with self.lock:
                self.status = "已导出长效优选 TSV 并复制到剪贴板" if is_history else "已导出 TSV 并复制到剪贴板"
            return {"ok": True, "cancelled": False}
        except Exception:
            return {"ok": False, "error": "导出失败"}

    def copy_node_name(self, value):
        try:
            if not isinstance(value, dict):
                raise ValueError("节点已变化")
            if isinstance(value.get("nodes"), list):
                pairs = []
                for entry in value["nodes"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("uid"), str) or not isinstance(entry.get("name"), str):
                        raise ValueError("节点已变化")
                    pairs.append((entry["uid"], entry["name"]))
                if not pairs:
                    raise ValueError("节点已变化")
            else:
                uid, name = value["uid"], value["name"]
                if not isinstance(uid, str) or not isinstance(name, str):
                    raise ValueError("节点已变化")
                pairs = [(uid, name)]
            with self.lock:
                if self.closed.is_set():
                    raise ValueError("应用已关闭")
                live_set = {(row.uid, row.name) for row in self.rows}
            hist_set = None
            valid_names = []
            for uid, name in pairs:
                if (uid, name) not in live_set:
                    if hist_set is None:
                        hist_set = {(it.profile_uid, it.node_name) for it in self.telemetry_store.compute_summaries(window="30d")}
                    if (uid, name) not in hist_set:
                        raise ValueError("节点已变化")
                valid_names.append(name)
            payload = "\n".join(valid_names)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(payload, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return {"ok": True, "count": len(valid_names)}
        except Exception:
            return {"ok": False, "error": "复制失败，请重新选择节点"}

    def copy_node_summary(self, value):
        try:
            if not isinstance(value, dict):
                raise ValueError("参数无效")
            view = value.get("view", "live")
            window = value.get("window", "7d")
            if view not in ("live", "history"):
                raise ValueError("参数无效")
            if isinstance(value.get("nodes"), list):
                pairs = []
                for entry in value["nodes"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("uid"), str) or not isinstance(entry.get("name"), str):
                        raise ValueError("参数无效")
                    pairs.append((entry["uid"], entry["name"]))
                if not pairs:
                    raise ValueError("参数无效")
            else:
                uid, name = value["uid"], value["name"]
                if not isinstance(uid, str) or not isinstance(name, str):
                    raise ValueError("参数无效")
                pairs = [(uid, name)]
            if view == "history":
                if window not in WINDOW_SPECS:
                    window = "7d"
                with self.lock:
                    if self.closed.is_set():
                        raise ValueError("应用已关闭")
                by_id = {(it.profile_uid, it.node_name): it for it in self.telemetry_store.compute_summaries(window=window)}
                lines = []
                for uid, name in pairs:
                    item = by_id.get((uid, name))
                    if item is None:
                        raise ValueError("历史节点不存在")
                    proto = (item.node_type or "—").upper()
                    speed = f"{item.weighted_median_speed_bps / 1_048_576:.2f} MiB/s" if item.success_count > 0 else "—"
                    lat = f"{item.weighted_median_latency_ms:.0f} ms" if item.success_count > 0 else "—"
                    avail = f"{round(item.availability_rate * 100)}%"
                    penalty = f" · 扣分 -{item.health_penalty:.1f}" if item.health_penalty > 0 else ""
                    lines.append(
                        f"[{item.profile_name}] {item.node_name} ({proto}) · "
                        f"稳健吞吐 {speed} · 典型延迟 {lat} · 可用率 {avail} · "
                        f"样本 {item.success_count}/{item.total_count} · 长效分 {item.long_term_score:.1f}{penalty}"
                    )
                summary = "\n".join(lines)
            else:
                with self.lock:
                    if self.closed.is_set():
                        raise ValueError("应用已关闭")
                    by_row = {(r.uid, r.name): r for r in self.rows}
                    lines = []
                    for uid, name in pairs:
                        row = by_row.get((uid, name))
                        if row is None:
                            raise ValueError("节点已变化")
                        m = row.measurement
                        proto = (row.node_type or (m.node_type if m else "") or "—").upper()
                        speed = f"{m.download_bytes_per_second / 1_048_576:.2f} MiB/s" if m else "—"
                        lat = f"{m.latency_ms:.0f} ms" if m else "—"
                        jitter = f"{m.jitter_ms:.0f} ms" if m else "—"
                        loss = f"{round((6 - m.head_successes) * 100 / 6)}%" if m else "—"
                        score = f"{m.score:.1f}" if m and m.score is not None else row.state
                        lines.append(
                            f"[{row.profile}] {row.name} ({proto}) · "
                            f"吞吐 {speed} · 延迟 {lat} · 抖动 {jitter} · 丢包 {loss} · 评分 {score}"
                        )
                    summary = "\n".join(lines)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(summary, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return {"ok": True, "text": summary, "count": len(lines)}
        except Exception:
            return {"ok": False, "error": "复制摘要失败，请重新选择节点"}

    def close(self):
        self.closed.set()
        self.stop()
        tray = self.tray_icon
        self.tray_icon = None
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        probe_thread = self._health_probe_thread
        if probe_thread is not None and probe_thread.is_alive() and probe_thread is not threading.current_thread():
            probe_thread.join(timeout=0.5)
        try:
            self.runtime_bridge.close()
        except Exception:
            pass
        self.telemetry_store.close()
