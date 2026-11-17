"""Desktop application state and explicit, validated webview bridge."""

from dataclasses import dataclass
from pathlib import Path
import csv
import io
import threading
import time
import win32clipboard

from .batch import Job, MultiBatchRunner
from .controller import (AssistedSwitch, ControllerError, PipeController, SwitchLatch,
                         read_settings, read_tun_enabled)
from .profile_registry import ProfileError, default_root, load_registry, revalidate, snapshot_profile
from .schedule import TestSchedule

_METADATA = ("剩余流量", "到期", "流量重置", "重置时间", "过期时间", "订阅信息",
             "traffic remaining", "remaining traffic", "subscription expires", "expiration date")


@dataclass
class Row:
    uid: str
    profile: str
    name: str
    selected: bool = True
    state: str = "待测速"
    measurement: object = None


def _registry_signature(registry):
    return tuple((p.uid, p.name, p.filename, p.identity) for p in registry.profiles)


def _cell(value):
    value = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


class Desktop:
    def __init__(self, executable, root=None):
        self.executable = Path(executable)
        self.root = Path(root) if root else default_root()
        self.lock = threading.RLock()
        self.registry = None
        self.snapshots = {}
        self.rows = []
        self.selected_profiles = set()
        self.groups = []
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
        self.schedule = TestSchedule()
        self.scheduled_options = None
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

    def get_state(self):
        with self.lock:
            profiles = [] if not self.registry else [
                {"uid": p.uid, "name": p.name, "current": p.current,
                 "selected": p.uid in self.selected_profiles} for p in self.registry.profiles]
            rows = []
            for row in self.rows:
                m = row.measurement
                rows.append({"uid": row.uid, "profile": row.profile, "name": row.name,
                             "selected": row.selected, "state": row.state,
                             "eligible": not any(term in row.name.lower() for term in _METADATA),
                             "score": m.score if m else None,
                             "speed": m.download_bytes_per_second if m else None,
                             "latency": m.latency_ms if m else None,
                             "jitter": m.jitter_ms if m else None,
                             "successes": m.head_successes if m else None})
            pending = None if not self.pending else {"uid": self.pending[0], "name": self.pending[1],
                       "ready": bool(self.registry and any(p.current and p.uid == self.pending[0] for p in self.registry.profiles))}
            undo = None if not self.undo else {"group": self.undo[2], "name": self.undo[1]}
            schedule_minutes, schedule_remaining, schedule_active = self.schedule.display()
            return {"profiles": profiles, "rows": rows, "groups": list(self.groups), "pending": pending,
                    "undo": undo,
                    "funnel_stage": self.funnel_stage,
                    "tun_enabled": self.tun_enabled,
                    "running": self.running or self.refreshing, "schedule_minutes": schedule_minutes,
                    "schedule_remaining": schedule_remaining, "schedule_active": schedule_active,
                    "status": self.status, "outcome_unknown": self.latch.outcome_unknown}

    def refresh(self):
        with self.lock:
            if self.running or self.refreshing:
                return {"ok": False, "error": "测速进行中"}
            self._invalidate(clear_pending=False)
            generation = self.generation
            self.refreshing = True
            pending = self.pending
            self.capture = None
            self.groups = []
        try:
            registry = load_registry(self.root)
            if len(registry.profiles) > 32:
                raise ProfileError("too many profiles")
            snapshots = {}
            rows = []
            total = 0
            for profile in registry.profiles:
                snapshot = snapshot_profile(registry, profile)
                snapshots[profile.uid] = snapshot
                total += len(snapshot.data)
                rows.extend(Row(profile.uid, profile.name, name) for name in snapshot.names)
                if total > 8 * 1024 * 1024 or len(rows) > 2000:
                    raise ProfileError("too many nodes")
            revalidate(registry, snapshots.values())
            tun_enabled = read_tun_enabled(registry)
            with self.lock:
                if generation != self.generation or self.closed.is_set():
                    raise ProfileError("cancelled")
                self.registry, self.snapshots, self.rows = registry, snapshots, rows
                self.selected_profiles = {p.uid for p in registry.profiles}
                self.tun_enabled = tun_enabled
                self.status = f"已加载 {len(registry.profiles)} 个订阅、{len(rows)} 个节点"
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
                if not profiles <= {p.uid for p in self.registry.profiles}:
                    raise ValueError()
                available = {(row.uid, row.name) for row in self.rows}
                if not nodes <= available or any(uid not in profiles for uid, _ in nodes):
                    raise ValueError()
                old_nodes = {(r.uid, r.name) for r in self.rows if r.selected and r.uid in self.selected_profiles}
                if profiles != self.selected_profiles or nodes != old_nodes:
                    self._invalidate()
                    self.capture, self.groups = None, []
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
                current = next(p for p in registry.profiles if p.current)
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
                self.status = "策略组已刷新，请选择目标组"
            return {"ok": True}
        except ProfileError:
            with self.lock:
                if self.registry is registry and generation == self.generation:
                    self.tun_enabled = None
                self.capture, self.groups = None, []
                self.status = "无法读取当前订阅的策略组；请确认本地控制器可用"
            return {"ok": False, "error": self.status}
        except Exception:
            with self.lock:
                if tun_updated and self.registry is registry and generation == self.generation:
                    self.tun_enabled = tun_enabled
                self.capture, self.groups = None, []
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

    def _start_validated_launch(self, options, token, scheduled, chosen, snapshots, capture):
        generation = self.generation
        streams = 2 if options["mode"] == "fast" else 4
        latency, download = ("2s", "12s") if streams == 2 else ("5s", "30s")
        jobs = [Job(row.uid, row.name, snapshots[row.uid].data, options["download_bytes"],
                    streams, latency, download) for row in chosen]
        runner = MultiBatchRunner(self.executable, jobs, options["parallel"],
                   prelaunch=lambda _job: self._prelaunch(generation, snapshots.values()))
        for row in self.rows:
            row.measurement = None
            row.state = "等待" if row in chosen else "未勾选"
        self.running = True
        self.funnel_stage = 1 if options.get("funnel_top") else None
        self.pending = None
        self.status = "测速中"
        self.runner = runner
        threading.Thread(target=self._run, args=(runner, options, capture, snapshots,
                                                 generation, token, scheduled), daemon=True).start()

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
        try:
            uid, node = value["uid"], value["name"]
            auto, group = value["auto_switch"], value["group"]
            if not isinstance(uid, str) or not isinstance(node, str) or type(auto) is not bool or not isinstance(group, str):
                raise ValueError("复测参数无效")
            options = {"parallel": 1, "download_bytes": 20 * 1_048_576, "mode": "accurate",
                       "auto_switch": auto, "group": group}
            with self.lock:
                if self.running or self.refreshing or self.schedule.active is not None or self.closed.is_set() or not self.registry:
                    raise ValueError("当前无法开始复测")
                if uid not in self.selected_profiles or not any(r.uid == uid and r.name == node for r in self.rows):
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
                selected_profiles = list(self.selected_profiles)
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
                return self.start(options)
        except (KeyError, TypeError, ValueError) as exc:
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
                self._invalidate()
                self._start_validated_launch(options, token, False, chosen, snapshots, capture)
            return {"ok": True}
        except (ValueError, KeyError, TypeError) as exc:
            if token is not None:
                with self.lock:
                    self.schedule.finish(token)
            message = str(exc) if str(exc) and isinstance(exc, ValueError) else "测速参数无效"
            return {"ok": False, "error": message}

    def _run(self, runner, options, capture, snapshots, generation, token, scheduled):
        try:
            revalidate(self.registry, snapshots.values())
            fatal = []
            expected_results = {(job.uid, job.name) for job in runner.jobs}
            received_results = set()
            def result(job, state, measurement):
                identity = (job.uid, job.name)
                if options.get("funnel_top") and (identity not in expected_results or identity in received_results):
                    fatal.append("unexpected_result")
                    runner.cancel()
                    return
                received_results.add(identity)
                with self.lock:
                    row = next((r for r in self.rows if r.uid == job.uid and r.name == job.name), None)
                    if row:
                        row.measurement = measurement
                        row.state = {"ok": "完成" if measurement and measurement.download_complete else "下载未完成", "cancelled": "已停止", "timeout": "超时",
                                     "profile_changed": "配置已变更"}.get(state, "失败")
                if options.get("funnel_top") and state in ("worker_failed", "invalid_job", "invalid_output", "profile_changed"):
                    fatal.append(state)
                    runner.cancel()
            runner.run(result)
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
                                  and not any(term in r.name.lower() for term in _METADATA)]
                    candidates.sort(key=lambda r: r.measurement.score, reverse=True)
                    promoted = candidates[:options["funnel_top"]]
                    if not promoted:
                        self.status = "初筛没有合格节点，未开始加压复测"
                        return
                    selected = {(r.uid, r.name) for r in promoted}
                    jobs = [Job(r.uid, r.name, snapshots[r.uid].data, 20 * 1_048_576,
                                4, "5s", "30s") for r in promoted]
                    for row in self.rows:
                        row.selected = (row.uid, row.name) in selected
                        row.measurement = None
                        row.state = "等待" if row.selected else "未晋级"
                    self.status = f"初筛完成，{len(promoted)} 个节点进行 20 MiB 加压复测"
                    self.funnel_stage = 2
                    runner = MultiBatchRunner(self.executable, jobs, 2,
                             prelaunch=lambda _job: self._prelaunch(generation, snapshots.values()))
                    self.runner = runner
                    expected_results = {(job.uid, job.name) for job in runner.jobs}
                    received_results.clear()
                runner.run(result)
                if received_results != expected_results and not runner.cancelled.is_set():
                    fatal.append("missing_result")
                if runner.cancelled.is_set() or fatal:
                    if fatal:
                        with self.lock:
                            self.status = "加压复测失败或配置已变更，未自动切换"
                    return
                revalidate(self.registry, snapshots.values())
            with self.lock:
                candidates = [r for r in self.rows if r.measurement and r.measurement.score is not None
                              and not any(term in r.name.lower() for term in _METADATA)]
                winner = max(candidates, key=lambda r: r.measurement.score) if candidates else None
                allowed = generation == self.generation and not runner.cancelled.is_set() and not self.closed.is_set()
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
                self.funnel_stage = None
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
                if self.refreshing or self.running or self.schedule.active is not None or self.closed.is_set():
                    raise ValueError()
                if not any(r.selected and r.uid in self.selected_profiles for r in self.rows):
                    raise ValueError()
                if options["auto_switch"] and (not self.capture or self.latch.outcome_unknown):
                    raise ValueError()
                self.schedule.arm(minutes)
                self.scheduled_options = options
                self.status = "定时已启用，仅在应用运行时生效"
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "无法启用定时，请检查节点与策略组"}

    def _timer(self):
        while not self.closed.wait(1):
            with self.lock:
                state, token = self.schedule.poll(
                    external_busy=self.running or self.refreshing or self.closed.is_set())
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

    def switch_manual(self, value):
        try:
            uid, node, group = value["uid"], value["name"], value["group"]
            with self.lock:
                if self.running or self.refreshing or not self.capture or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
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

    def undo_switch(self):
        try:
            with self.lock:
                if self.running or self.refreshing or not self.undo or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
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

    def continue_pending(self, value):
        try:
            group = value["group"]
            with self.lock:
                if self.running or self.refreshing or not self.capture or not self.pending or self.latch.outcome_unknown:
                    raise ControllerError("unavailable")
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
