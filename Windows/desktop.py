"""Desktop application state and explicit, validated webview bridge."""

from dataclasses import dataclass
from pathlib import Path
import csv
import inspect
import io
import threading
import time
import win32clipboard

from .batch import Job, MultiBatchRunner
from .controller import (AssistedSwitch, ControllerError, PipeController, ProbeError,
                         SwitchLatch, read_settings, read_tun_enabled)
from .profile_registry import ProfileError, default_root, load_registry, revalidate, snapshot_profile
from .schedule import TestSchedule
from .settings import AppSettingsStore, SettingsSecurityError
from .telemetry_history import (TelemetryHealthEvent, TelemetryHistoryStore,
                                TelemetrySampleRecord, WINDOW_SPECS)

_METADATA = ("剩余流量", "到期", "流量重置", "重置时间", "过期时间", "订阅信息",
             "traffic remaining", "remaining traffic", "subscription expires", "expiration date")
_PATROL_QUOTAS = frozenset((50 * 1_048_576, 80 * 1_048_576, 150 * 1_048_576, 300 * 1_048_576))


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


def _registry_signature(registry):
    return tuple((p.uid, p.name, p.filename, p.identity) for p in registry.profiles)


def _cell(value):
    value = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _eligible_name(name):
    lowered = name.lower()
    return not any(term in lowered for term in _METADATA)


class Desktop:
    def __init__(self, executable, root=None, telemetry_dir=None, settings_dir=None):
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
        self.schedule = TestSchedule()
        self.scheduled_options = None
        self.patrol_schedule = TestSchedule()
        self.patrol_quota_bytes = 80 * 1_048_576
        self.patrol_snapshots = None
        self.patrol_last_summary = ""
        self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
        self.active_run_kind = "normal"
        self.health_probe_status = "● 活跃探测 待命"
        self.health_probe_level = "idle"
        self.health_probe_inflight = False
        self._health_probe_lock = threading.Lock()
        self._health_probe_thread = None
        self._last_health_probe_at = 0.0
        self.latch = SwitchLatch()
        self.closed = threading.Event()
        self.window = None
        threading.Thread(target=self._timer, daemon=True).start()

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

    def get_state(self):
        with self.lock:
            now_mono = time.monotonic()
            self.recently_scored = {k: exp for k, exp in self.recently_scored.items() if exp > now_mono}
            self.cascade_highlighted = {k: exp for k, exp in self.cascade_highlighted.items() if exp > now_mono}
            profiles = [] if not self.registry else [
                {"uid": p.uid, "name": p.name, "current": p.current,
                 "compatible": p.uid in self.snapshots,
                 "selected": p.uid in self.selected_profiles} for p in self.registry.profiles]
            rows = []
            for row in self.rows:
                m = row.measurement
                ident = (row.uid, row.name)
                live = self.live_progress.get(ident) if self.running else None
                rows.append({"uid": row.uid, "profile": row.profile, "name": row.name,
                             "selected": row.selected, "state": row.state,
                             "eligible": _eligible_name(row.name),
                             "node_type": row.node_type or (m.node_type if m else ""),
                             "score": m.score if m else None,
                             "stage1_score": row.stage1_score,
                             "speed": m.download_bytes_per_second if m else None,
                             "latency": m.latency_ms if m else None,
                             "jitter": m.jitter_ms if m else None,
                             "successes": m.head_successes if m else None,
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
            if running and self.active_run_kind == "patrol":
                primary = "智能巡检中"
            elif running:
                primary = "测速中"
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
            tooltip = f"Clash 测速工作台 · {primary}"
            if self.health_probe_level != "idle":
                tooltip += f"（{self.health_probe_status.replace('● ', '')}）"
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
                rows.extend(Row(profile.uid, profile.name, name) for name in snapshot.names)
                if total > 8 * 1024 * 1024 or len(rows) > 2000:
                    raise ProfileError("too many nodes")
            if registry.profiles and not snapshots:
                raise ProfileError("invalid profile")
            revalidate(registry, snapshots.values())
            tun_enabled = read_tun_enabled(registry)
            with self.lock:
                if generation != self.generation or self.closed.is_set():
                    raise ProfileError("cancelled")
                self.registry, self.snapshots, self.rows = registry, snapshots, rows
                self.selected_profiles = set(snapshots)
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
                if profiles != self.selected_profiles or nodes != old_nodes:
                    self._invalidate()
                    self._disarm_patrol()
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
                names = set(snapshot.names)
                full = [g.name for g in groups if g.name != "GLOBAL" and names <= set(g.all)]
                if len(full) == 1:
                    suggested = full[0]
                else:
                    non_global = [g.name for g in groups if g.name != "GLOBAL"]
                    suggested = non_global[0] if len(non_global) == 1 else None
                self.suggested_group = suggested
                if suggested:
                    self.probe_group = suggested
                elif self.probe_group not in {g.name for g in groups}:
                    non_global = [g.name for g in groups if g.name != "GLOBAL"]
                    self.probe_group = non_global[0] if non_global else (groups[0].name if groups else None)
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
            parallel = min(4, max(1, len(chosen)))
        else:
            streams = 2 if options["mode"] == "fast" else 4
            latency, download = ("2s", "12s") if streams == 2 else ("5s", "30s")
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
            if run_kind == "patrol":
                if (row.uid, row.name) in chosen_set:
                    row.measurement = None
                    row.stage1_score = None
                    row.state = "等待"
            else:
                row.measurement = None
                row.stage1_score = None
                row.state = "等待" if row in chosen else "未勾选"
        self.running = True
        self.active_run_kind = run_kind
        self.live_progress.clear()
        self.recently_scored.clear()
        self.cascade_highlighted.clear()
        self.funnel_stage = 1 if options.get("funnel_top") else None
        self.pending = None
        if options.get("group"):
            self.probe_group = options["group"]
        self.status = "智能巡检中…" if run_kind == "patrol" else "测速中"
        self.runner = runner
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
        """Narrow the selection, revalidate the original group, then start 20 MiB."""
        token = None
        try:
            uid, node = value["uid"], value["name"]
            auto, group = value["auto_switch"], value["group"]
            if not isinstance(uid, str) or not isinstance(node, str) or type(auto) is not bool or not isinstance(group, str):
                raise ValueError("复测参数无效")
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
                selected_profiles = list(self.selected_profiles | {uid})
                result = self.set_selection({"profiles": selected_profiles,
                                             "nodes": [{"uid": uid, "name": node}]})
                if not result["ok"]:
                    raise ValueError("节点选择已变化")
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
                chosen, snapshots, capture = self._validate_launch(options, False)
                token = self.schedule.begin_manual()
                if token is None:
                    raise ValueError("已有测速任务进行中")
                self._start_validated_launch(options, token, False, chosen, snapshots, capture,
                                             run_kind="normal", source="retest")
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
                sample_to_record = None
                with self.lock:
                    self.live_progress.pop(identity, None)
                    row = next((r for r in self.rows if r.uid == job.uid and r.name == job.name), None)
                    if row:
                        row.measurement = measurement
                        if measurement and measurement.node_type:
                            row.node_type = measurement.node_type
                        row.state = {"ok": "完成" if measurement and measurement.download_complete else "下载未完成", "cancelled": "已停止", "timeout": "超时",
                                     "profile_changed": "配置已变更"}.get(state, "失败")
                        self.recently_scored[identity] = time.monotonic() + 1.4
                        self.score_revision += 1
                        if state != "cancelled" and _eligible_name(row.name):
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
                    try:
                        self.telemetry_store.record_sample(sample_to_record)
                    except Exception:
                        pass
                if options.get("funnel_top") and state in ("worker_failed", "invalid_job", "invalid_output", "profile_changed"):
                    fatal.append(state)
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
                        return
                    selected = {(r.uid, r.name) for r in promoted}
                    jobs = [Job(r.uid, r.name, snapshots[r.uid].data, 20 * 1_048_576,
                                4, "5s", "30s") for r in promoted]
                    cascade_exp = time.monotonic() + 1.0
                    for row in self.rows:
                        row.selected = (row.uid, row.name) in selected
                        if row.selected:
                            row.stage1_score = None
                            row.measurement = None
                            row.state = "等待"
                            self.cascade_highlighted[(row.uid, row.name)] = cascade_exp
                        else:
                            prev_score = row.measurement.score if row.measurement and _eligible_name(row.name) else None
                            row.measurement = None
                            if prev_score is not None:
                                row.stage1_score = prev_score
                                row.state = f"初筛 {prev_score:.1f}分"
                            else:
                                row.stage1_score = None
                                row.state = "未晋级"
                    self.live_progress.clear()
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
                candidates = [r for r in self.rows if r.measurement and r.measurement.score is not None
                              and _eligible_name(r.name)]
                winner = max(candidates, key=lambda r: r.measurement.score) if candidates else None
                allowed = generation == self.generation and not runner.cancelled.is_set() and not self.closed.is_set()
                self.score_revision += 1
            if allowed and options["auto_switch"] and winner:
                self._automatic(winner, options["group"], capture, generation)
            elif allowed:
                with self.lock:
                    self.status = "测速完成" if winner else "测速完成，无可评分节点"
        except Exception:
            with self.lock:
                self.status = "测速失败或配置已变更"
                self._invalidate()
        finally:
            with self.lock:
                self.running, self.runner = False, None
                self.live_progress.clear()
                self.funnel_stage = None
                self.active_run_kind = "normal"
                if run_kind == "patrol":
                    self.patrol_schedule.finish(token)
                    self.cached_patrol_bytes_today = self.telemetry_store.patrol_bytes_used_today()
                else:
                    self.schedule.finish(token)

    def _automatic(self, winner, group_name, capture, generation):
        registry, snapshot, settings, identity, groups = capture
        if winner.uid != snapshot.profile.uid:
            with self.lock:
                self.pending = (winner.uid, winner.name, winner.measurement, self.registry,
                                dict(self.snapshots))
                self.status = "最优节点不在当前订阅，请先在 Clash Verge 手动切换订阅"
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
                self.status = {"switched": "已切换并复核", "confirmed_noop": "当前已是最优节点",
                               "outcome_unknown": "切换结果未知，请到 Clash Verge 手动核验；自动切换已锁定"}.get(outcome, "切换条件已变化，未自动切换")
                if outcome == "outcome_unknown":
                    self._invalidate()
        except Exception:
            with self.lock:
                self.status = "切换条件已变化，未自动切换"

    def stop(self):
        with self.lock:
            self._invalidate()
            if self.runner:
                self.runner.cancel()
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
                quota_mib = quota // 1_048_576
                self.status = f"智能巡检已开启：每 {interval_min} 分钟最多抽测 8 节点（1MiB/2流，日配额 {quota_mib} MiB，不改节点勾选、不自动切换）"
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
                    non_global = [g.name for g in groups if g.name != "GLOBAL"]
                    group_name = non_global[0] if non_global else (groups[0].name if groups else None)
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

    def _poll_patrol(self):
        with self.lock:
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
            plan = self.telemetry_store.select_patrol_plan(
                candidates, daily_quota_bytes=quota, per_node_bytes=1_048_576, max_per_tick=8
            )
            with self.lock:
                self.cached_patrol_bytes_today = max(0, quota - plan.remaining_quota_bytes)
                if not plan.identities:
                    self.patrol_last_summary = "今日配额已满"
                    self.status = f"今日智能巡检配额已用完（{quota // 1_048_576} MiB/天），明日自动恢复"
                    self.patrol_schedule.finish(p_token)
                    return
                id_set = set(plan.identities)
                chosen = [r for r in self.rows if (r.uid, r.name) in id_set and r.uid in snapshots]
                if not chosen:
                    self.patrol_schedule.finish(p_token)
                    return
                options = {"parallel": min(4, len(chosen)), "download_bytes": 1_048_576,
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
            uid, node, group = value["uid"], value["name"], value["group"]
            with self.lock:
                if self.running or self.refreshing or not self.capture or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
                self._switching_count += 1
                switching_entered = True
                if self.schedule.minutes is not None:
                    self._invalidate()
                registry, snapshot, settings, identity, groups = self.capture
                generation = self.generation
                if snapshot.profile.uid != uid:
                    raise ControllerError("profile")
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

    def export_tsv(self):
        try:
            import webview
            paths = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename="clash-speed-results.tsv")
            if not paths:
                return {"ok": True}
            output = io.StringIO(newline="")
            writer = csv.writer(output, delimiter="\t", lineterminator="\n")
            writer.writerow(["订阅", "节点", "状态", "综合分", "速度 B/s", "延迟 ms", "抖动 ms"])
            with self.lock:
                for row in self.rows:
                    m = row.measurement
                    writer.writerow([_cell(row.profile), _cell(row.name), _cell(row.state),
                                     m.score if m and m.score is not None else "", m.download_bytes_per_second if m else "",
                                     m.latency_ms if m else "", m.jitter_ms if m else ""])
            Path(paths if isinstance(paths, str) else paths[0]).write_text(output.getvalue(), encoding="utf-8-sig")
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "导出失败"}

    def copy_node_name(self, value):
        try:
            with self.lock:
                uid, name = value["uid"], value["name"]
                if self.closed.is_set() or not any(row.uid == uid and row.name == name for row in self.rows):
                    raise ValueError("节点已变化")
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(name, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "复制失败，请重新选择节点"}

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
        self.telemetry_store.close()
