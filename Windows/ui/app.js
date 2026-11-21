"use strict";
const $ = id => document.getElementById(id);
let state = {profiles: [], rows: [], groups: [], running: false, status: "准备就绪"};
let active = null, busy = false, sortColumn = "score", sortAscending = false, groupToRestore = null, lastSelectedGroup = "";
let viewMode = "live", historyWindow = "7d", historySelectedOnly = true, historySortColumn = "rank", historySortAscending = false;
let historySummaries = [], historyActiveKey = null;
let refreshSeq = 0, actionEpoch = 0, refreshInFlight = false, historySeq = 0;
let pendingConfirm = null;
let lastProfilesSig = null, lastGroupsSig = null;
let lastBackendPatrolQuota = null;
let wasRunning = false, lastScoreRevision = -1, lastChampSig = "", champDisplayScore = null, champDisplaySpeed = null, champAnimRaf = null, champPulseTimer = null;
let clearHistoryResetTimer = null, clearHistoryArmedUntil = 0;
let lastIdlePollAt = 0;
const rowCache = new Map();
const key = row => JSON.stringify([row.uid, row.name]);
const api = () => window.pywebview.api;
const reducesMotion = () => Boolean(typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

function notify(message, isError = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", isError);
  const dot = $("dock-dot");
  if (dot) {
    dot.classList.toggle("error", Boolean(isError));
    dot.classList.toggle("running", Boolean(state.running) && !isError);
  }
}

function visible() {
  return RowModel.visibleRows(state.rows, state.profiles, {
    query: $("search").value,
    status: $("status-filter").value,
    sort: sortColumn,
    ascending: sortAscending,
    freezeOrder: Boolean(state.running)
  });
}

function visibleHistory() {
  return RowModel.historyRows(historySummaries, state.profiles, {
    query: $("history-search") ? $("history-search").value : "",
    selectedOnly: historySelectedOnly,
    sort: historySortColumn,
    ascending: historySortAscending
  });
}

function winner() {
  const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid));
  return state.rows.filter(r => profiles.has(r.uid) && r.eligible && r.score != null).sort((a, b) => b.score - a.score)[0];
}

function activeHeroRow() {
  if (viewMode === "history") {
    const items = visibleHistory();
    return items.find(r => key(r) === historyActiveKey) || items[0] || historySummaries[0] || null;
  }
  const best = winner();
  if (best) return best;
  const vis = visible();
  const sel = vis.find(r => key(r) === active);
  if (sel) return sel;
  if (state.running) {
    const liveRows = state.rows.filter(r => r.live);
    if (liveRows.length) return liveRows.sort((a, b) => (b.live.bps || 0) - (a.live.bps || 0))[0];
  }
  return vis[0] || null;
}

function selectedNodes() {
  const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid));
  return state.rows.filter(r => r.selected && profiles.has(r.uid));
}
function selectionSignature() { return JSON.stringify(selectedNodes().map(key)); }
function groupsSignature() { return JSON.stringify((state.groups || []).map(g => [g.name, g.now])); }
function formatBytes(bytes) {
  const mib = Number(bytes || 0) / 1048576;
  return mib >= 1 ? `${mib.toFixed(1)} MiB` : `${Math.max(0, Math.round(Number(bytes || 0) / 1024))} KiB`;
}

function options() {
  const locked = Boolean(state.outcome_unknown);
  return {
    parallel: Number($("parallel").value),
    download_bytes: Number($("size").value),
    mode: $("mode").value,
    auto_switch: locked ? false : $("auto").checked,
    group: locked ? "" : $("group").value
  };
}
function optionsSignature(opts = options()) { return JSON.stringify([opts.parallel, opts.download_bytes, opts.mode, opts.auto_switch, opts.group]); }

function closeConfirm(restoreFocus = false, notice = "") {
  const current = pendingConfirm;
  pendingConfirm = null;
  const dialog = $("confirm-dialog");
  if (dialog) dialog.hidden = true;
  if (notice) notify(notice, true);
  if (restoreFocus && current?.trigger && current.trigger.isConnected !== false && !current.trigger.disabled && typeof current.trigger.focus === "function") {
    current.trigger.focus();
  }
}

function validatePendingConfirm() {
  if (!pendingConfirm || pendingConfirm.submitting) return true;
  if (state.running || busy || !pendingConfirm.validate()) {
    closeConfirm(false, "页面状态或参数已变化，原确认已作废");
    return false;
  }
  return true;
}

function openConfirm({title, message, trigger, validate, onAccept}) {
  if (pendingConfirm) closeConfirm(false);
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  pendingConfirm = {
    trigger: trigger || document.activeElement || null,
    validate,
    onAccept,
    submitting: false
  };
  if (dialog) dialog.hidden = false;
  const cancelBtn = $("confirm-cancel");
  if (cancelBtn && typeof cancelBtn.focus === "function") cancelBtn.focus();
}

async function call(name, ...args) {
  actionEpoch += 1;
  try {
    const result = await api()[name](...args);
    if (!result.ok) notify(result.error || "操作失败", true);
    return result.ok;
  } catch (_) {
    notify("操作失败，请检查本地状态", true);
    return false;
  }
}

async function runBusy(fn) {
  if (busy || state.running) return false;
  busy = true;
  render();
  try {
    return await fn();
  } finally {
    busy = false;
    await refreshState();
    render();
  }
}

async function updateSelection(profiles, selected) {
  if (busy || state.running) return false;
  busy = true;
  render();
  let ok = false;
  try {
    ok = await call("set_selection", {
      profiles,
      nodes: state.rows.filter(r => selected.has(key(r))).map(r => ({uid: r.uid, name: r.name}))
    });
  } finally {
    busy = false;
    await refreshState();
    validatePendingConfirm();
  }
  if (ok && !state.groups.length && hasSelectedCurrentProfile() && !state.outcome_unknown) {
    await autoSyncGroups(state.status, true);
  }
  return ok;
}

function hasSelectedCurrentProfile() {
  return state.profiles.some(p => p.current && p.selected && p.compatible !== false);
}

async function autoSyncGroups(loadedStatus = "", quietOnFail = true) {
  if (busy || state.running || state.outcome_unknown || !hasSelectedCurrentProfile()) return false;
  if (typeof api().refresh_groups !== "function") return false;
  busy = true;
  render();
  let res = null;
  try {
    res = await api().refresh_groups();
    await refreshState();
  } catch (_) {
    res = {ok: false};
  } finally {
    busy = false;
    render();
    if (!res?.ok && quietOnFail && loadedStatus) {
      notify(`${loadedStatus}（未连接 Clash Verge 控制器，仅可纯测速）`);
    }
  }
  return Boolean(res?.ok);
}

function profileSelection(uid, selected) {
  const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid));
  selected ? profiles.add(uid) : profiles.delete(uid);
  const nodes = new Set(state.rows.filter(r => r.selected && profiles.has(r.uid)).map(key));
  if (selected) state.rows.filter(r => r.uid === uid).forEach(r => nodes.add(key(r)));
  return updateSelection([...profiles], nodes);
}

function renderProfiles() {
  const host = $("profiles");
  const sig = JSON.stringify([state.running, state.profiles.map(p => [p.uid, p.name, p.selected, p.current, p.compatible !== false])]);
  if (sig !== lastProfilesSig) {
    lastProfilesSig = sig;
    host.replaceChildren();
    for (const p of state.profiles) {
      const compatible = p.compatible !== false;
      const button = document.createElement("button");
      button.className = `profile-chip${p.selected ? " selected" : ""}${p.current ? " current" : ""}${compatible ? "" : " incompatible"}`;
      button.textContent = compatible ? p.name : `${p.name}（不支持）`;
      button.setAttribute("aria-pressed", String(p.selected));
      button.title = compatible ? p.uid : `${p.uid}（包含不支持的 proxy-providers 或无内联节点）`;
      button.disabled = state.running || !compatible;
      button.onclick = () => profileSelection(p.uid, !p.selected);
      host.append(button);
    }
    if (!state.profiles.length) {
      const empty = document.createElement("span");
      empty.className = "empty";
      empty.textContent = "点击刷新读取本机订阅";
      host.append(empty);
    }
  }
  $("source-summary").textContent = `${state.profiles.filter(p => p.selected).length} / ${state.profiles.length} 个订阅已选择`;
}

function createRowEntry(row) {
  const tr = document.createElement("tr");
  const entry = {tr, row, box: null, cells: [], speedWrap: null, speedText: null, speedBar: null, values: null};
  tr.onclick = () => { active = key(entry.row); renderRows(); };
  tr.ondblclick = () => { active = key(entry.row); renderRows(); confirmManual(tr); };
  const box = document.createElement("input");
  box.type = "checkbox";
  box.onclick = e => e.stopPropagation();
  box.ondblclick = e => e.stopPropagation();
  box.onchange = () => {
    const nodes = new Set(state.rows.filter(r => r.selected).map(key));
    box.checked ? nodes.add(key(entry.row)) : nodes.delete(key(entry.row));
    updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), nodes);
  };
  entry.box = box;
  const first = document.createElement("td");
  first.append(box);
  tr.append(first);
  for (let i = 0; i < 8; i++) {
    const td = document.createElement("td");
    entry.cells.push(td);
    tr.append(td);
  }
  return entry;
}

function updateRowEntry(entry, row, maxSpeed) {
  entry.row = row;
  const rowKey = key(row);
  entry.tr.classList.toggle("active", active === rowKey);
  entry.tr.classList.toggle("is-testing-active", Boolean(row.live));
  entry.tr.classList.toggle("is-recently-scored", Boolean(row.recently_scored));
  entry.tr.classList.toggle("is-cascade", Boolean(row.cascade_highlighted));
  entry.box.checked = row.selected;
  entry.box.disabled = state.running;
  entry.box.setAttribute("aria-label", `选择 ${row.name}`);

  const live = row.live;
  let stateText = row.state;
  let scoreText = row.score == null ? "—" : row.score.toFixed(1);
  let speedText = row.speed == null ? "—" : `${(row.speed / 1048576).toFixed(2)} MiB/s`;
  if (live) {
    if (live.phase === "head") {
      stateText = "探测延迟";
      scoreText = "探测中";
      speedText = "探测延迟…";
    } else {
      const pct = live.expected_bytes > 0 ? Math.min(99, Math.max(0, Math.round(live.received_bytes / live.expected_bytes * 100))) : 0;
      stateText = "下载中";
      scoreText = pct > 0 ? `下载 ${pct}%` : "下载中…";
      speedText = live.bps > 0 ? `${(live.bps / 1048576).toFixed(2)} MiB/s` : "连接下载…";
    }
  } else if (row.score == null && row.stage1_score != null && row.state && row.state.startsWith("初筛")) {
    scoreText = row.state;
  }

  const values = [
    row.name,
    row.profile,
    stateText,
    scoreText,
    speedText,
    row.latency == null ? "—" : `${row.latency.toFixed(0)} ms`,
    row.jitter == null ? "—" : `${row.jitter.toFixed(0)} ms`,
    row.successes == null ? "—" : `${row.successes}/6`
  ];
  for (let i = 0; i < values.length; i++) {
    const td = entry.cells[i];
    const v = values[i];
    if (i === 4) {
      const hasBar = Boolean(live) || row.speed != null;
      if (hasBar) {
        if (!entry.speedWrap) {
          td.textContent = "";
          const wrap = document.createElement("div");
          wrap.className = "speed-cell-wrap";
          const lbl = document.createElement("span");
          lbl.className = "speed-cell-text";
          const track = document.createElement("div");
          track.className = "speed-track";
          const bar = document.createElement("div");
          bar.className = "speed-bar";
          track.append(bar);
          wrap.append(lbl, track);
          td.append(wrap);
          entry.speedWrap = wrap;
          entry.speedText = lbl;
          entry.speedBar = bar;
        }
        entry.speedText.textContent = v;
        td.title = v;
        let pct = 0;
        let indet = false;
        if (live) {
          if (live.phase === "head" || live.expected_bytes <= 0 || live.received_bytes <= 0) indet = true;
          else pct = Math.min(100, Math.max(4, (live.received_bytes / live.expected_bytes) * 100));
        } else if (row.speed != null) {
          pct = Math.min(100, Math.max(2, (row.speed / Math.max(1048576, maxSpeed)) * 100));
        }
        entry.speedBar.classList.toggle("testing", Boolean(live));
        entry.speedBar.classList.toggle("indeterminate", indet);
        entry.speedBar.style.width = indet ? "" : `${pct.toFixed(1)}%`;
      } else {
        if (entry.speedWrap) entry.speedWrap = entry.speedText = entry.speedBar = null;
        if (!entry.values || entry.values[i] !== v) {
          td.textContent = v;
          td.title = v;
        }
      }
      continue;
    }
    if (!entry.values || entry.values[i] !== v) {
      td.textContent = v;
      td.title = v;
    }
    if (i === 3) td.classList.toggle("score-top", Boolean(row.eligible && row.score != null));
    if (i === 5) td.classList.toggle("latency-cell", row.latency != null);
  }
  entry.values = values;
}

function makeBadgeCell(text, cls) {
  const td = document.createElement("td");
  const span = document.createElement("span");
  span.className = `badge ${cls}`;
  span.textContent = text;
  td.append(span);
  td.title = text;
  return td;
}

function renderHistoryRows() {
  const host = $("rows");
  const items = visibleHistory();
  $("count").textContent = `${items.length} 候选`;
  $("empty").textContent = "暂无长效历史数据；运行测速或开启巡检后自动积累";
  $("empty").hidden = items.length > 0;
  rowCache.clear();
  host.replaceChildren();
  const maxBps = Math.max(20 * 1048576, ...items.map(s => s.weighted_median_speed_bps || 0));
  for (let idx = 0; idx < items.length; idx++) {
    const s = items[idx];
    const tr = document.createElement("tr");
    const rk = key(s);
    tr.classList.toggle("active", historyActiveKey === rk);
    tr.onclick = () => { historyActiveKey = rk; renderHistoryRows(); updateChampion(); };
    tr.ondblclick = () => { historyActiveKey = rk; renderHistoryRows(); confirmManual(tr); };

    const tdRank = document.createElement("td");
    tdRank.textContent = String(idx + 1);
    tr.append(tdRank);

    if (s.health_penalty > 0) tr.append(makeBadgeCell("扣分", "badge-penalty"));
    else if (s.is_qualified) tr.append(makeBadgeCell("主力", "badge-main"));
    else tr.append(makeBadgeCell("待验", "badge-wait"));

    const tdName = document.createElement("td");
    tdName.textContent = s.name;
    tdName.title = s.health_penalty > 0 ? `活跃健康异常：扣分 -${s.health_penalty.toFixed(1)}` : s.name;
    tr.append(tdName);

    const tdProf = document.createElement("td");
    tdProf.textContent = s.profile;
    tdProf.title = s.uid;
    tr.append(tdProf);

    tr.append(makeBadgeCell((s.node_type || "—").toUpperCase(), "proto-badge"));

    const tdSpeed = document.createElement("td");
    if (s.success_count > 0) {
      const mibText = `${(s.weighted_median_speed_bps / 1048576).toFixed(2)} MiB/s`;
      const wrap = document.createElement("div");
      wrap.className = "speed-cell-wrap";
      const lbl = document.createElement("span");
      lbl.className = "speed-cell-text";
      lbl.textContent = mibText;
      const track = document.createElement("div");
      track.className = "speed-track";
      const bar = document.createElement("div");
      bar.className = "speed-bar";
      bar.style.width = `${Math.min(100, Math.max(2, (s.weighted_median_speed_bps / maxBps) * 100)).toFixed(1)}%`;
      track.append(bar);
      wrap.append(lbl, track);
      tdSpeed.append(wrap);
      tdSpeed.title = mibText;
    } else {
      tdSpeed.textContent = "—";
    }
    tr.append(tdSpeed);

    const tdLat = document.createElement("td");
    tdLat.classList.toggle("latency-cell", s.success_count > 0);
    tdLat.textContent = s.success_count > 0 ? `${s.weighted_median_latency_ms.toFixed(0)} ms` : "—";
    tr.append(tdLat);

    const tdAvail = document.createElement("td");
    const pct = Math.round(s.availability_rate * 100);
    tdAvail.textContent = `${pct}%`;
    tdAvail.classList.toggle("avail-low", s.availability_rate < 0.5);
    tdAvail.classList.toggle("avail-mid", s.availability_rate >= 0.5 && s.availability_rate < 0.85);
    tr.append(tdAvail);

    const tdCount = document.createElement("td");
    tdCount.textContent = `${s.success_count}/${s.total_count}`;
    tr.append(tdCount);

    const tdScore = document.createElement("td");
    const isTop = idx === 0 && historySortColumn === "rank" && s.long_term_score > 0;
    const baseTxt = s.long_term_score.toFixed(1);
    if (s.health_penalty > 0) {
      tdScore.textContent = `${baseTxt} -${Math.round(s.health_penalty)}`;
      tdScore.classList.toggle("score-penalty", true);
    } else if (isTop) {
      tdScore.textContent = `★ ${baseTxt}`;
      tdScore.classList.toggle("score-top", true);
    } else {
      tdScore.textContent = baseTxt;
    }
    tdScore.title = `长效分 ${baseTxt}（基础 ${s.base_score.toFixed(1)} · 中位 ${s.weighted_median_score.toFixed(1)} · P20 ${s.weighted_p20_score.toFixed(1)} · 扣分 -${s.health_penalty.toFixed(1)}）`;
    tr.append(tdScore);

    host.append(tr);
  }
  updateChampion();
}

function updateSparkline(points) {
  const svg = $("best-sparkline");
  const path = $("best-sparkline-path");
  if (!svg || !path) return;
  if (!points || points.length < 2) {
    svg.hidden = true;
    if (typeof svg.setAttribute === "function") svg.setAttribute("hidden", "");
    path.setAttribute("d", "");
    return;
  }
  const peak = Math.max(1, ...points);
  const d = points.map((v, i) => {
    const x = (i * 120) / (points.length - 1);
    const norm = Math.min(0.96, Math.max(0.1, v / peak));
    const y = 24 - norm * 20;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
  path.setAttribute("d", d);
  svg.hidden = false;
  if (typeof svg.removeAttribute === "function") svg.removeAttribute("hidden");
}

function setAnimatedChampionNumbers(targetScore, targetSpeed, scoreLabel, speedLabel) {
  if (champAnimRaf && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(champAnimRaf);
    champAnimRaf = null;
  }
  if (targetScore == null || targetSpeed == null || reducesMotion() || typeof requestAnimationFrame !== "function") {
    champDisplayScore = targetScore;
    champDisplaySpeed = targetSpeed;
    $("best-score").textContent = scoreLabel;
    $("best-speed").textContent = speedLabel;
    return;
  }
  const startScore = champDisplayScore == null ? targetScore : champDisplayScore;
  const startSpeed = champDisplaySpeed == null ? targetSpeed : champDisplaySpeed;
  if (Math.abs(startScore - targetScore) < 0.05 && Math.abs(startSpeed - targetSpeed) < 0.005) {
    $("best-score").textContent = scoreLabel;
    $("best-speed").textContent = speedLabel;
    return;
  }
  let step = 0;
  const steps = 10;
  const tick = () => {
    step += 1;
    const t = step / steps;
    const eased = 1 - Math.pow(1 - t, 3);
    const s = startScore + (targetScore - startScore) * eased;
    const v = startSpeed + (targetSpeed - startSpeed) * eased;
    $("best-score").textContent = s.toFixed(1);
    $("best-speed").textContent = `${v.toFixed(2)} MiB/s`;
    if (step < steps) {
      champAnimRaf = requestAnimationFrame(tick);
    } else {
      champDisplayScore = targetScore;
      champDisplaySpeed = targetSpeed;
      $("best-score").textContent = scoreLabel;
      $("best-speed").textContent = speedLabel;
      champAnimRaf = null;
    }
  };
  champAnimRaf = requestAnimationFrame(tick);
}

function triggerChampionPulse() {
  const card = $("champion-card");
  if (!card || reducesMotion()) return;
  card.classList.toggle("changed", false);
  void card.offsetWidth;
  card.classList.toggle("changed", true);
  if (champPulseTimer) clearTimeout(champPulseTimer);
  champPulseTimer = setTimeout(() => {
    card.classList.toggle("changed", false);
    champPulseTimer = null;
  }, 300);
}

function updateChampion() {
  const eyebrow = $("best-eyebrow");
  const meta = $("best-meta");
  const ready = $("best-ready");
  if (viewMode === "history") {
    const winTitle = {"24h": "24小时", "7d": "7天", "30d": "30天", "peak": "晚高峰"}[historyWindow] || "7天";
    if (eyebrow) eyebrow.textContent = `长效优选 · ${winTitle}`;
    const hero = activeHeroRow();
    updateSparkline(null);
    if (!hero) {
      $("best-name").textContent = "暂无长效历史数据";
      $("best-profile").textContent = "运行测速或智能巡检后自动积累";
      if (meta) meta.textContent = "典型延迟 —  ·  可用率 —  ·  样本 —";
      if (ready) ready.textContent = "● 归档等待中";
      setAnimatedChampionNumbers(null, null, "—", "—");
      return;
    }
    $("best-name").textContent = hero.name;
    const availPct = Math.round(hero.availability_rate * 100);
    $("best-profile").textContent = `订阅：${hero.profile}  ·  样本 ${hero.success_count}/${hero.total_count}  ·  可用 ${availPct}%`;
    const latText = hero.success_count > 0 ? `${hero.weighted_median_latency_ms.toFixed(0)} ms` : "—";
    if (meta) meta.textContent = `典型延迟 ${latText}  ·  基础 ${hero.base_score.toFixed(1)}  ·  健康扣分 -${hero.health_penalty.toFixed(1)}`;
    if (ready) {
      ready.textContent = hero.health_penalty > 0
        ? `● 活跃异常 -${Math.round(hero.health_penalty)}`
        : (hero.is_qualified ? "● 长期主力候选" : `● 样本累积 (${hero.success_count}/3)`);
    }
    const speedMib = hero.success_count > 0 ? hero.weighted_median_speed_bps / 1048576 : null;
    setAnimatedChampionNumbers(hero.long_term_score, speedMib, hero.long_term_score.toFixed(1), speedMib != null ? `${speedMib.toFixed(2)} MiB/s` : "—");
    return;
  }

  if (eyebrow) eyebrow.textContent = "最佳路由 · OPTIMAL";
  const best = winner();
  const hero = best || activeHeroRow();
  if (!hero) {
    $("best-name").textContent = "等待测速";
    $("best-profile").textContent = "选择订阅源与节点后开始";
    if (meta) meta.textContent = "延迟 —  ·  抖动 —  ·  丢包 —";
    if (ready) ready.textContent = state.running ? "● 测速运行中" : "● 待命就绪";
    updateSparkline(null);
    lastChampSig = "";
    setAnimatedChampionNumbers(null, null, "—", "—");
    return;
  }
  $("best-name").textContent = hero.name;
  if (hero.score != null && hero.speed != null) {
    const speedMib = hero.speed / 1048576;
    const loss = hero.successes != null ? ((6 - hero.successes) * 100 / 6).toFixed(0) : "—";
    $("best-profile").textContent = `${hero.profile}${hero.node_type ? ` · ${hero.node_type.toUpperCase()}` : ""}`;
    if (meta) meta.textContent = `延迟 ${hero.latency == null ? "—" : `${hero.latency.toFixed(0)} ms`}  ·  抖动 ${hero.jitter == null ? "—" : `${hero.jitter.toFixed(0)} ms`}  ·  丢包 ${loss}%`;
    if (ready) ready.textContent = state.pending?.ready ? "● 可继续跨订阅切换" : (state.running ? "● 测速运行中" : "● 最佳路由就绪");
    const norm = Math.min(0.96, Math.max(0.15, speedMib / 25));
    updateSparkline([norm * 0.68, norm * 0.82, norm * 0.76, norm * 0.91, norm, norm * 0.95]);
    const sig = `${hero.uid}|${hero.name}|${hero.score.toFixed(2)}|${speedMib.toFixed(2)}`;
    if (sig !== lastChampSig) {
      if (lastChampSig) triggerChampionPulse();
      lastChampSig = sig;
    }
    setAnimatedChampionNumbers(hero.score, speedMib, hero.score.toFixed(1), `${speedMib.toFixed(2)} MiB/s`);
  } else if (hero.live) {
    const live = hero.live;
    if (live.phase === "head") {
      $("best-profile").textContent = `${hero.profile} · 正在探测延迟…`;
      setAnimatedChampionNumbers(null, null, "探测", "…");
      updateSparkline(null);
    } else {
      const pct = live.expected_bytes > 0 ? Math.min(99, Math.max(0, Math.round(live.received_bytes / live.expected_bytes * 100))) : 0;
      const mib = live.bps / 1048576;
      $("best-profile").textContent = `${hero.profile} · 实时下载 ${formatBytes(live.received_bytes)} / ${formatBytes(live.expected_bytes)}`;
      setAnimatedChampionNumbers(null, null, pct > 0 ? `${pct}%` : "测速", live.bps > 0 ? `${mib.toFixed(2)} MiB/s` : "…");
      updateSparkline(live.samples || []);
    }
    if (meta) meta.textContent = "延迟 测算中  ·  抖动 测算中  ·  丢包 测算中";
    if (ready) ready.textContent = "● 实时吞吐探测";
  } else {
    $("best-profile").textContent = `${hero.profile} · 状态：${hero.state}`;
    if (meta) meta.textContent = "延迟 —  ·  抖动 —  ·  丢包 —";
    if (ready) ready.textContent = state.running ? "● 测速运行中" : "● 待命就绪";
    updateSparkline(null);
    setAnimatedChampionNumbers(null, null, "待测", "—");
  }
}

function updateOverallProgress() {
  const bar = $("overall-progress");
  const fill = $("overall-progress-fill");
  const pulse = $("overall-progress-pulse");
  if (!bar || !fill) return;
  if (!state.running) {
    bar.classList.toggle("active", false);
    fill.style.transform = "scaleX(0)";
    if (pulse) pulse.hidden = true;
    return;
  }
  const activeRows = state.rows.filter(r => r.selected || r.live || r.state === "等待");
  const total = activeRows.length;
  let progressSum = 0;
  for (const r of activeRows) {
    if (r.live && r.live.expected_bytes > 0) {
      progressSum += Math.min(0.95, r.live.received_bytes / r.live.expected_bytes);
    } else if (r.state !== "等待" && r.state !== "未勾选" && r.state !== "待测速") {
      progressSum += 1;
    }
  }
  const frac = total > 0 ? Math.min(1, Math.max(0.04, progressSum / total)) : 0.08;
  bar.classList.toggle("active", true);
  fill.style.transform = `scaleX(${frac.toFixed(4)})`;
  if (pulse) pulse.hidden = false;
}

function renderRows() {
  if (viewMode === "history") {
    renderHistoryRows();
    return;
  }
  const host = $("rows");
  const rows = visible();
  $("count").textContent = `${rows.length} 个可见节点 · ${state.rows.filter(r => r.selected).length} 已勾选`;
  $("empty").textContent = "当前筛选没有节点；可调整搜索或刷新订阅";
  $("empty").hidden = rows.length > 0;
  const maxSpeed = Math.max(25 * 1048576, ...rows.map(r => r.speed || 0));
  if (typeof host.insertBefore === "function") {
    const alive = new Set();
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      const rowKey = key(row);
      alive.add(rowKey);
      let entry = rowCache.get(rowKey);
      if (!entry) {
        entry = createRowEntry(row);
        rowCache.set(rowKey, entry);
      }
      updateRowEntry(entry, row, maxSpeed);
      const currentAt = host.children[i];
      if (currentAt !== entry.tr) host.insertBefore(entry.tr, currentAt || null);
    }
    while (host.children.length > rows.length) {
      const last = host.children[host.children.length - 1];
      if (typeof last.remove === "function") last.remove();
      else break;
    }
    for (const cachedKey of rowCache.keys()) {
      if (!alive.has(cachedKey)) rowCache.delete(cachedKey);
    }
  } else {
    host.replaceChildren();
    for (const row of rows) {
      const entry = createRowEntry(row);
      updateRowEntry(entry, row, maxSpeed);
      host.append(entry.tr);
    }
  }
  updateChampion();
  $("traffic").textContent = `预计流量 ${(state.rows.filter(r => r.selected).length * Number($("size").value) / 1048576).toFixed(1)} MiB`;
}

function renderGroups() {
  const select = $("group");
  const sig = `${groupsSignature()}|${state.suggested_group || ""}|${Boolean(state.outcome_unknown)}`;
  if (sig === lastGroupsSig && !groupToRestore) return;
  lastGroupsSig = sig;
  const old = groupToRestore ?? (select.value || lastSelectedGroup);
  select.replaceChildren(new Option("请选择策略组", ""));
  for (const g of state.groups) select.add(new Option(`${g.name}（当前 ${g.now}）`, g.name));
  if (old && state.groups.some(g => g.name === old)) {
    select.value = old;
  } else if (!state.outcome_unknown && state.suggested_group && state.groups.some(g => g.name === state.suggested_group)) {
    select.value = state.suggested_group;
    lastSelectedGroup = state.suggested_group;
  }
}

function renderPatrolAndHealth() {
  const patrol = state.patrol || {};
  const quotaSelect = $("patrol-quota");
  const toggleBtn = $("patrol-toggle");
  const patrolNote = $("patrol-note");
  const quotaBytes = patrol.quota_bytes || 80 * 1048576;
  const quotaMib = Math.round(quotaBytes / 1048576);
  const usedMib = patrol.used_bytes_today ? Math.ceil(patrol.used_bytes_today / 1048576) : 0;
  if (quotaSelect && quotaBytes !== lastBackendPatrolQuota) {
    lastBackendPatrolQuota = quotaBytes;
    quotaSelect.value = String(quotaBytes);
  } else if (quotaSelect && patrol.armed && quotaSelect.value !== String(quotaBytes)) {
    quotaSelect.value = String(quotaBytes);
  }
  if (toggleBtn) {
    toggleBtn.textContent = patrol.armed ? "停止巡检" : "开启巡检";
    toggleBtn.classList.toggle("armed", Boolean(patrol.armed));
    toggleBtn.disabled = state.running || busy;
  }
  if (quotaSelect) quotaSelect.disabled = state.running || busy || Boolean(patrol.armed);
  if (patrolNote) {
    if (patrol.armed && patrol.remaining != null) {
      const m = Math.floor(patrol.remaining / 60), s = String(patrol.remaining % 60).padStart(2, "0");
      patrolNote.textContent = `巡检 ${String(m).padStart(2, "0")}:${s} · 今日 ${usedMib}/${quotaMib} MiB`;
    } else if (patrol.armed) {
      patrolNote.textContent = `巡检已启用 · 今日 ${usedMib}/${quotaMib} MiB`;
    } else {
      patrolNote.textContent = patrol.summary || `今日 ${usedMib}/${quotaMib} MiB`;
    }
    patrolNote.classList.toggle("armed", Boolean(patrol.armed));
  }
  const health = state.health_probe || {};
  const pill = $("health-pill");
  if (pill) {
    pill.textContent = health.text || "● 活跃探测 待命";
    pill.classList.toggle("ok", health.level === "ok");
    pill.classList.toggle("warn", health.level === "warn");
    pill.classList.toggle("danger", health.level === "danger");
  }
}

function render() {
  if (state.funnel_stage === 1) {
    $("parallel").value = "10"; $("size").value = "1048576"; $("mode").value = "fast";
  } else if (state.funnel_stage === 2) {
    $("parallel").value = "2"; $("size").value = "20971520"; $("mode").value = "accurate";
  }
  renderProfiles();
  renderRows();
  renderGroups();
  renderPatrolAndHealth();
  updateOverallProgress();
  const blocked = state.running || busy;
  const unknown = Boolean(state.outcome_unknown);
  if (unknown) { $("auto").checked = false; lastSelectedGroup = ""; }
  $("start").disabled = $("dock-start").disabled = blocked;
  $("stop").disabled = $("dock-stop").disabled = !state.running;
  for (const id of ["refresh", "all-profiles", "clear-profiles", "top", "all-nodes", "clear-nodes", "parallel", "size", "mode", "stress", "funnel"]) {
    $(id).disabled = blocked;
  }
  for (const id of ["refresh-groups", "group", "manual", "auto"]) {
    $(id).disabled = blocked || unknown;
  }
  const hero = activeHeroRow();
  const heroReady = viewMode === "history" ? Boolean(hero && hero.available !== false) : Boolean(winner());
  $("retest-best").disabled = blocked || !heroReady;
  $("switch-best").disabled = blocked || !heroReady || unknown;
  $("undo").disabled = blocked || !state.undo || unknown;
  $("continue").disabled = blocked || !state.pending?.ready || unknown;
  if ($("clear-history")) $("clear-history").disabled = blocked;
  const tunNotice = $("tun-notice");
  if (tunNotice) tunNotice.hidden = state.tun_enabled !== true;
  if ($("minimize-to-tray") && state.minimize_to_tray !== undefined) {
    $("minimize-to-tray").checked = Boolean(state.minimize_to_tray);
  }
  const seconds = state.schedule_remaining;
  const remaining = seconds == null ? "" : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  $("schedule-note").textContent = state.schedule_minutes
    ? `定时：每 ${state.schedule_minutes} 分钟 · ${state.schedule_active ? "本轮结束后重新计时" : `距下次 ${remaining}`}（退出应用后停止）`
    : "支持托盘后台运行，退出应用后停止";
  notify(state.status || "准备就绪", unknown);
  validatePendingConfirm();
}

async function fetchHistory() {
  if (typeof api().get_history !== "function") return;
  const seq = ++historySeq;
  try {
    const res = await api().get_history({window: historyWindow});
    if (seq !== historySeq || !res?.ok) return;
    historySummaries = res.summaries || [];
    if (viewMode === "history") renderHistoryRows();
  } catch (_) {}
}

function setViewMode(mode) {
  viewMode = mode === "history" ? "history" : "live";
  const isHist = viewMode === "history";
  if ($("view-live")) {
    $("view-live").classList.toggle("active", !isHist);
    $("view-live").setAttribute("aria-selected", String(!isHist));
  }
  if ($("view-history")) {
    $("view-history").classList.toggle("active", isHist);
    $("view-history").setAttribute("aria-selected", String(isHist));
  }
  if ($("live-tools")) $("live-tools").hidden = isHist;
  if ($("history-tools")) $("history-tools").hidden = !isHist;
  if ($("live-thead")) $("live-thead").hidden = isHist;
  if ($("history-thead")) $("history-thead").hidden = !isHist;
  rowCache.clear();
  if (isHist) {
    fetchHistory();
    renderHistoryRows();
  } else {
    renderRows();
  }
  render();
}

async function refreshState(background = false) {
  if (background && refreshInFlight) return;
  const seq = ++refreshSeq;
  const epoch = actionEpoch;
  refreshInFlight = true;
  try {
    const nextState = await api().get_state();
    if (seq !== refreshSeq || (background && epoch !== actionEpoch)) return;
    const prevRunning = wasRunning;
    state = nextState;
    wasRunning = Boolean(state.running);
    if (prevRunning && !wasRunning) {
      sortColumn = "score";
      sortAscending = false;
      for (const other of document.querySelectorAll("button[data-sort]")) other.parentElement.removeAttribute("aria-sort");
      if (typeof document.querySelector === "function") {
        const scoreBtn = document.querySelector('button[data-sort="score"]');
        if (scoreBtn?.parentElement) scoreBtn.parentElement.setAttribute("aria-sort", "descending");
      }
      if (viewMode === "history") fetchHistory();
    } else if (state.score_revision != null && state.score_revision !== lastScoreRevision) {
      lastScoreRevision = state.score_revision;
      if (viewMode === "history") fetchHistory();
    }
    render();
  } catch (_) {
    if (seq === refreshSeq && (!background || epoch === actionEpoch)) notify("无法连接应用核心", true);
  } finally {
    if (seq === refreshSeq) refreshInFlight = false;
  }
}

$("refresh").onclick = async () => {
  const ok = await runBusy(async () => await call("refresh"));
  if (ok) await autoSyncGroups(state.status, true);
};
$("all-profiles").onclick = () => updateSelection(state.profiles.map(p => p.uid), new Set(state.rows.map(key)));
$("clear-profiles").onclick = () => updateSelection([], new Set());
$("all-nodes").onclick = () => {
  const s = new Set(state.rows.filter(r => r.selected).map(key));
  visible().forEach(r => s.add(key(r)));
  updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), s);
};
$("clear-nodes").onclick = () => {
  const s = new Set(state.rows.filter(r => r.selected).map(key));
  visible().forEach(r => s.delete(key(r)));
  updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), s);
};
$("top").onclick = () => {
  const n = Number($("topn").value);
  if (!Number.isInteger(n) || n < 1 || n > 2000) { notify("前 N 项请输入 1 至 2000 的整数", true); return; }
  updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), new Set(visible().slice(0, n).map(key)));
};
$("search").oninput = renderRows;
$("status-filter").onchange = renderRows;
$("topn").oninput = () => validatePendingConfirm();
for (const button of document.querySelectorAll("button[data-sort]")) {
  button.onclick = () => {
    const column = button.dataset.sort;
    sortAscending = sortColumn === column ? !sortAscending : ["name", "profile", "state", "latency", "jitter"].includes(column);
    sortColumn = column;
    for (const other of document.querySelectorAll("button[data-sort]")) other.parentElement.removeAttribute("aria-sort");
    button.parentElement.setAttribute("aria-sort", sortAscending ? "ascending" : "descending");
    renderRows();
  };
}
for (const button of document.querySelectorAll("button[data-hsort]")) {
  button.onclick = () => {
    const column = button.dataset.hsort;
    historySortAscending = historySortColumn === column ? !historySortAscending : ["rank", "name", "profile", "node_type", "weighted_median_latency_ms"].includes(column);
    historySortColumn = column;
    for (const other of document.querySelectorAll("button[data-hsort]")) other.parentElement.removeAttribute("aria-sort");
    button.parentElement.setAttribute("aria-sort", historySortAscending ? "ascending" : "descending");
    renderHistoryRows();
  };
}
if ($("view-live")) $("view-live").onclick = () => setViewMode("live");
if ($("view-history")) $("view-history").onclick = () => setViewMode("history");
for (const btn of document.querySelectorAll("button[data-hwin]")) {
  btn.onclick = () => {
    historyWindow = btn.dataset.hwin;
    for (const b of document.querySelectorAll("button[data-hwin]")) b.classList.toggle("active", b === btn);
    fetchHistory();
  };
}
if ($("history-selected-only")) {
  $("history-selected-only").onclick = () => {
    historySelectedOnly = !historySelectedOnly;
    $("history-selected-only").classList.toggle("selected", historySelectedOnly);
    $("history-selected-only").setAttribute("aria-pressed", String(historySelectedOnly));
    renderHistoryRows();
  };
}
if ($("history-search")) $("history-search").oninput = renderHistoryRows;
if ($("clear-history")) {
  $("clear-history").onclick = async () => {
    if (busy || state.running || typeof api().clear_history !== "function") return;
    const now = Date.now();
    if (now < clearHistoryArmedUntil) {
      clearHistoryArmedUntil = 0;
      if (clearHistoryResetTimer) { clearTimeout(clearHistoryResetTimer); clearHistoryResetTimer = null; }
      $("clear-history").textContent = "清空记录";
      const ok = await runBusy(async () => await call("clear_history"));
      if (ok) {
        historySummaries = [];
        renderHistoryRows();
      }
      return;
    }
    clearHistoryArmedUntil = now + 4000;
    $("clear-history").textContent = "确认清空?";
    notify("再次点击“确认清空?”将永久删除本地历史记录与健康扣分（4 秒后自动取消）");
    if (clearHistoryResetTimer) clearTimeout(clearHistoryResetTimer);
    clearHistoryResetTimer = setTimeout(() => {
      clearHistoryArmedUntil = 0;
      clearHistoryResetTimer = null;
      if ($("clear-history")) $("clear-history").textContent = "清空记录";
    }, 4100);
  };
}
if ($("patrol-toggle")) {
  $("patrol-toggle").onclick = () => {
    const armed = Boolean(state.patrol?.armed);
    const quota = Number($("patrol-quota")?.value || 80 * 1048576);
    return runBusy(async () => {
      await call("set_patrol", {enabled: !armed, quota_bytes: quota});
    });
  };
}
if ($("patrol-quota")) {
  $("patrol-quota").onchange = () => {
    if (state.patrol?.armed) {
      runBusy(async () => {
        await call("set_patrol", {enabled: false, quota_bytes: Number($("patrol-quota").value)});
      });
    }
  };
}

async function parametersChanged() {
  validatePendingConfirm();
  if (state.schedule_minutes) await call("set_schedule", {minutes: 0});
  renderRows();
  await refreshState();
}
$("group").onchange = () => { lastSelectedGroup = $("group").value; return parametersChanged(); };
for (const id of ["parallel", "size", "mode", "auto", "interval"]) $(id).onchange = parametersChanged;

$("stress").onclick = () => {
  if (busy || state.running) return;
  const count = selectedNodes().length;
  if (!count) { notify("请先选择要测速的节点", true); return; }
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const group = locked ? "" : $("group").value;
  const estimate = (count * 20).toFixed(1);
  const snap = {
    optionsSig: optionsSignature(),
    selectionSig: selectionSignature(),
    outcomeUnknown: locked
  };
  openConfirm({
    title: "确认加压验证 · 20M",
    message: `对已勾选的 ${count} 个节点执行加压验证（并发 2 / 每节点 20 MiB / 精确模式），预计产生约 ${estimate} MiB 实际流量。自动切换${auto ? "已开启" : "未开启"}${auto && group ? `，目标策略组“${group}”` : ""}。`,
    trigger: $("stress"),
    validate: () => Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
      optionsSignature() === snap.optionsSig &&
      selectionSignature() === snap.selectionSig,
    onAccept: async () => {
      await runBusy(async () => {
        $("parallel").value = "2";
        $("size").value = "20971520";
        $("mode").value = "accurate";
        if (state.schedule_minutes) await call("set_schedule", {minutes: 0});
        await call("start", options());
      });
    }
  });
};

$("funnel").onclick = () => {
  if (busy || state.running) return;
  const count = selectedNodes().length;
  if (!count) { notify("请先选择要测速的节点", true); return; }
  const top = 10, estimate = count + Math.min(count, top) * 20;
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const group = locked ? "" : $("group").value;
  const snap = {
    top,
    optionsSig: optionsSignature(),
    selectionSig: selectionSignature(),
    outcomeUnknown: locked
  };
  openConfirm({
    title: "确认智能两段优选",
    message: `先以每节点 1 MiB 初筛，再对综合分前 ${top} 名以 20 MiB 复测。预计最多约 ${estimate} MiB（不是流量上限）。自动切换${auto ? "已开启" : "未开启"}${auto && group ? `，目标策略组“${group}”` : ""}。`,
    trigger: $("funnel"),
    validate: () => Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
      optionsSignature() === snap.optionsSig &&
      selectionSignature() === snap.selectionSig,
    onAccept: async () => {
      await runBusy(async () => {
        await call("start_funnel", {...options(), top_count: top});
      });
    }
  });
};

function retestRow(row, trigger = $("retest-best")) {
  if (!row || busy || state.running) return;
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const group = locked ? "" : $("group").value;
  const snap = {
    rowKey: key(row),
    auto,
    group,
    outcomeUnknown: locked,
    groupsSig: groupsSignature()
  };
  openConfirm({
    title: "确认单节点复测",
    message: `以 20 MiB 复测“${row.name}”，会产生实际流量。自动切换${auto ? "已开启" : "未开启"}${group ? `，目标策略组“${group}”` : ""}。`,
    trigger,
    validate: () => {
      const currentLocked = Boolean(state.outcome_unknown);
      const currentAuto = currentLocked ? false : $("auto").checked;
      const currentGroup = currentLocked ? "" : $("group").value;
      const exists = viewMode === "history"
        ? historySummaries.some(r => key(r) === snap.rowKey && r.available !== false)
        : state.rows.some(r => key(r) === snap.rowKey);
      return exists &&
        currentLocked === snap.outcomeUnknown &&
        currentAuto === snap.auto &&
        currentGroup === snap.group &&
        groupsSignature() === snap.groupsSig;
    },
    onAccept: async () => {
      busy = true;
      groupToRestore = group || null;
      actionEpoch += 1;
      render();
      let result;
      try {
        result = await api().retest_node({uid: row.uid, name: row.name, auto_switch: auto, group});
      } catch (_) {
        result = {ok: false, error: "复测请求失败"};
      }
      if (result?.ok) {
        $("parallel").value = "1";
        $("size").value = "20971520";
        $("mode").value = "accurate";
        if (viewMode === "history") setViewMode("live");
      }
      await refreshState();
      busy = false;
      groupToRestore = null;
      render();
      if (!result?.ok) notify(result?.error || "复测未启动", true);
    }
  });
}

$("retest-best").onclick = () => retestRow(viewMode === "history" ? activeHeroRow() : winner(), $("retest-best"));

function findLiveOrHistoryRow(rowKey) {
  return viewMode === "history"
    ? historySummaries.find(r => key(r) === rowKey)
    : state.rows.find(r => key(r) === rowKey);
}

function confirmManual(trigger = $("manual")) {
  if (busy || state.running || state.outcome_unknown) return;
  const targetKey = viewMode === "history" ? historyActiveKey : active;
  const row = findLiveOrHistoryRow(targetKey);
  const group = $("group").value;
  if (!row || !group) { notify("请先选中节点和策略组", true); return; }
  const snap = {
    rowKey: key(row),
    group,
    groupsSig: groupsSignature(),
    mode: viewMode
  };
  openConfirm({
    title: "确认切换策略组",
    message: `将策略组“${group}”切换到“${row.name}”？`,
    trigger,
    validate: () => {
      const curKey = snap.mode === "history" ? historyActiveKey : active;
      const exists = snap.mode === "history"
        ? historySummaries.some(r => key(r) === snap.rowKey)
        : state.rows.some(r => key(r) === snap.rowKey);
      return !state.outcome_unknown &&
        curKey === snap.rowKey &&
        $("group").value === snap.group &&
        exists &&
        groupsSignature() === snap.groupsSig;
    },
    onAccept: async () => {
      await runBusy(async () => {
        await call("switch_manual", {uid: row.uid, name: row.name, group});
      });
    }
  });
}

$("switch-best").onclick = () => {
  const best = viewMode === "history" ? activeHeroRow() : winner();
  if (!best) return;
  if (viewMode === "history") { historyActiveKey = key(best); renderHistoryRows(); }
  else { active = key(best); renderRows(); }
  confirmManual($("switch-best"));
};
$("start").onclick = () => runBusy(async () => {
  if (viewMode === "history") setViewMode("live");
  await call("start", options());
});
$("stop").onclick = async () => { await call("stop"); await refreshState(); };
$("dock-start").onclick = () => $("start").click();
$("dock-stop").onclick = () => $("stop").click();
$("refresh-groups").onclick = () => runBusy(async () => { await call("refresh_groups"); });
$("manual").onclick = () => confirmManual($("manual"));
$("undo").onclick = () => {
  if (busy || state.running || state.outcome_unknown || !state.undo) return;
  const snapUndo = JSON.stringify(state.undo);
  const {group, name} = state.undo;
  openConfirm({
    title: "确认切回原节点",
    message: `将策略组“${group}”切回“${name}”？`,
    trigger: $("undo"),
    validate: () => !state.outcome_unknown && Boolean(state.undo) && JSON.stringify(state.undo) === snapUndo,
    onAccept: async () => {
      await runBusy(async () => { await call("undo_switch"); });
    }
  });
};

let menuTarget = null, menuTrigger = null;
function closeRowMenu(restoreFocus = false) {
  $("row-menu").hidden = true;
  menuTarget = null;
  if (restoreFocus && menuTrigger && menuTrigger.isConnected !== false && typeof menuTrigger.focus === "function") menuTrigger.focus();
  menuTrigger = null;
}
function openRowMenu(row, x, y, trigger = null) {
  if (!row || busy) return;
  if (viewMode === "history") historyActiveKey = key(row);
  else active = key(row);
  menuTarget = key(row);
  menuTrigger = trigger;
  const menu = $("row-menu");
  menu.hidden = false;
  $("menu-switch").disabled = state.running || Boolean(state.outcome_unknown) || !$("group").value || !state.profiles.some(p => p.uid === row.uid && p.current);
  $("menu-retest").disabled = state.running || (viewMode === "history" && row.available === false);
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - bounds.height - 8))}px`;
  $("menu-copy").focus();
}
function menuRow() { return findLiveOrHistoryRow(menuTarget); }
function eventRow(target) {
  const row = target.closest("tr"), index = Array.from($("rows").children).indexOf(row);
  if (index < 0) return null;
  return viewMode === "history" ? visibleHistory()[index] : visible()[index];
}
$("rows").addEventListener("contextmenu", event => { const row = eventRow(event.target); if (!row) return; event.preventDefault(); openRowMenu(row, event.clientX, event.clientY, event.target); });
$("rows").addEventListener("keydown", event => { if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return; const row = eventRow(event.target); if (!row) return; event.preventDefault(); const bounds = event.target.getBoundingClientRect(); openRowMenu(row, bounds.left, bounds.bottom, event.target); });
$("menu-switch").onclick = () => { const row = menuRow(); const trigger = menuTrigger; closeRowMenu(); if (!row || busy || state.running || state.outcome_unknown) return; if (viewMode === "history") { historyActiveKey = key(row); renderHistoryRows(); } else { active = key(row); renderRows(); } confirmManual(trigger || $("manual")); };
$("menu-retest").onclick = () => { const row = menuRow(); const trigger = menuTrigger; closeRowMenu(); retestRow(row, trigger || $("retest-best")); };
$("menu-copy").onclick = async () => { const row = menuRow(); closeRowMenu(); if (row && await call("copy_node_name", {uid: row.uid, name: row.name})) notify("已复制节点名称"); };

$("confirm-cancel").onclick = () => closeConfirm(true);
$("confirm-accept").onclick = async () => {
  if (!pendingConfirm || pendingConfirm.submitting) return;
  if (!validatePendingConfirm()) return;
  const current = pendingConfirm;
  current.submitting = true;
  pendingConfirm = null;
  const dialog = $("confirm-dialog");
  if (dialog) dialog.hidden = true;
  await current.onAccept();
};
if ($("confirm-dialog") && typeof $("confirm-dialog").addEventListener === "function") {
  $("confirm-dialog").addEventListener("pointerdown", event => {
    if (event.target === $("confirm-dialog")) closeConfirm(true);
  });
}

document.addEventListener("pointerdown", event => { if (!$("row-menu").hidden && !$("row-menu").contains(event.target)) closeRowMenu(); });
document.addEventListener("keydown", event => {
  const dialog = $("confirm-dialog");
  if (dialog && !dialog.hidden) {
    if (event.key === "Escape") { event.preventDefault(); closeConfirm(true); return; }
    if (event.key === "Tab") {
      const cancelBtn = $("confirm-cancel"), acceptBtn = $("confirm-accept");
      if (document.activeElement === cancelBtn && event.shiftKey) { event.preventDefault(); acceptBtn.focus(); }
      else if (document.activeElement === acceptBtn && !event.shiftKey) { event.preventDefault(); cancelBtn.focus(); }
    }
    return;
  }
  const menu = $("row-menu");
  if (menu.hidden) return;
  if (event.key === "Escape") { event.preventDefault(); closeRowMenu(true); return; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const items = [...menu.querySelectorAll("button:not(:disabled)")];
    if (!items.length) return;
    const index = items.indexOf(document.activeElement), step = event.key === "ArrowDown" ? 1 : -1;
    items[(index + step + items.length) % items.length].focus();
  }
});
window.addEventListener("blur", () => closeRowMenu());

$("continue").onclick = () => {
  if (busy || state.running || state.outcome_unknown) return;
  const group = $("group").value;
  if (!state.pending?.ready || !group) { notify("请先手动切换订阅并刷新策略组", true); return; }
  const snapPending = JSON.stringify(state.pending);
  const snapGroups = groupsSignature();
  openConfirm({
    title: "确认继续应用最优",
    message: `将策略组“${group}”切换到上轮最优节点“${state.pending.name}”？`,
    trigger: $("continue"),
    validate: () => !state.outcome_unknown &&
      Boolean(state.pending?.ready) &&
      JSON.stringify(state.pending) === snapPending &&
      $("group").value === group &&
      groupsSignature() === snapGroups,
    onAccept: async () => {
      await runBusy(async () => { await call("continue_pending", {group}); });
    }
  });
};

$("arm").onclick = async () => {
  if (busy) return;
  const minutes = Number($("interval").value);
  if (!minutes) {
    busy = true;
    render();
    try {
      await call("set_schedule", {minutes: 0, options: options()});
    } finally {
      busy = false;
      await refreshState();
      render();
    }
    return;
  }
  if (state.running) return;
  const count = selectedNodes().length;
  const traffic = (count * Number($("size").value) / 1048576).toFixed(1);
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const snap = {
    minutes,
    optionsSig: optionsSignature(),
    selectionSig: selectionSignature(),
    outcomeUnknown: locked
  };
  openConfirm({
    title: "确认启用定时测速",
    message: `每 ${minutes} 分钟测速，预计每轮 ${traffic} MiB。自动切换${auto ? "已开启" : "未开启"}；应用需保持运行。`,
    trigger: $("arm"),
    validate: () => Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
      Number($("interval").value) === snap.minutes &&
      optionsSignature() === snap.optionsSig &&
      selectionSignature() === snap.selectionSig,
    onAccept: async () => {
      await runBusy(async () => {
        await call("set_schedule", {minutes, options: options()});
      });
    }
  });
};

$("export").onclick = () => call("export_tsv");
if ($("hide-tray")) {
  $("hide-tray").onclick = () => {
    if (typeof api().hide_to_tray === "function") call("hide_to_tray");
  };
}
if ($("minimize-to-tray")) {
  $("minimize-to-tray").onchange = async () => {
    if (typeof api().set_minimize_to_tray !== "function") return;
    await call("set_minimize_to_tray", {enabled: $("minimize-to-tray").checked});
    await refreshState(true);
  };
}
window.addEventListener("pywebviewready", async () => {
  await refreshState();
  if (!state.running && !state.profiles.length) {
    const ok = await runBusy(async () => await call("refresh"));
    if (ok) await autoSyncGroups(state.status, true);
  }
  setInterval(() => {
    const now = Date.now();
    if (!state.running && now - lastIdlePollAt < 1150) return;
    lastIdlePollAt = now;
    refreshState(true);
  }, 350);
});
