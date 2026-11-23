"use strict";
const $ = id => document.getElementById(id);
let state = {profiles: [], rows: [], groups: [], running: false, status: "准备就绪"};
let active = null, busy = false, sortColumn = "score", sortAscending = false, groupToRestore = null, lastSelectedGroup = "";
let viewMode = "live", historyWindow = "7d", historySelectedOnly = true, historySortColumn = "rank", historySortAscending = false;
let historySummaries = [], historyActiveKey = null;
const historySelectedKeys = new Set();
let refreshSeq = 0, actionEpoch = 0, refreshInFlight = false, historySeq = 0;
let pendingConfirm = null;
let lastProfilesSig = null, lastGroupsSig = null;
let lastBackendPatrolQuota = null;
let prefsHydrated = false;
let wasRunning = false, lastScoreRevision = -1, lastChampSig = "", champDisplayScore = null, champDisplaySpeed = null, champAnimRaf = null, champPulseTimer = null;
let clearHistoryResetTimer = null, clearHistoryArmedUntil = 0;
let exportCopiedTimer = null, dockConfirmTimer = null, dockConfirmedUntil = 0, lastStatusSeen = "";
let lastEmptySig = "", locatedRowKey = null, locatedRowTimer = null;
let lastIdlePollAt = 0;
const rowCache = new Map();
const localCascadeUntil = new Map();
const key = row => JSON.stringify([row.uid, row.name]);
const api = () => window.pywebview.api;
const reducesMotion = () => Boolean(typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

function activeUsingIdentity() {
  const curProfile = (state.profiles || []).find(p => p.current);
  const groupName = $("group") ? $("group").value : "";
  if (!curProfile || !groupName) return null;
  const grp = (state.groups || []).find(g => g.name === groupName);
  if (!grp || !grp.now) return null;
  return {uid: curProfile.uid, name: grp.now};
}

function isActiveUsingRow(row) {
  const act = activeUsingIdentity();
  return Boolean(act && row && row.uid === act.uid && row.name === act.name);
}

function persistPreferences(patch) {
  if (!patch || typeof api().save_preferences !== "function") return;
  Promise.resolve(api().save_preferences(patch)).catch(() => {});
}

function setSelectIfValid(el, rawVal) {
  if (!el || rawVal == null) return;
  const next = String(rawVal);
  const opts = el.options;
  if (opts && typeof opts.length === "number" && opts.length > 0) {
    let found = false;
    for (let i = 0; i < opts.length; i++) {
      if (String(opts[i].value) === next) { found = true; break; }
    }
    if (!found) return;
  }
  el.value = next;
}

function hydratePreferences(prefs) {
  if (prefsHydrated || !prefs || state.running || state.funnel_stage != null) return;
  prefsHydrated = true;
  setSelectIfValid($("parallel"), prefs.parallel);
  setSelectIfValid($("size"), prefs.download_bytes);
  setSelectIfValid($("mode"), prefs.mode);
  setSelectIfValid($("interval"), prefs.interval);
  if ($("patrol-quota") && prefs.patrol_quota != null) {
    setSelectIfValid($("patrol-quota"), prefs.patrol_quota);
    lastBackendPatrolQuota = Number(prefs.patrol_quota);
  }
  if ($("topn") && prefs.top_n != null) $("topn").value = String(prefs.top_n);
}

function flashDockConfirmed() {
  dockConfirmedUntil = Date.now() + 2600;
  updateDockVisual(false);
  if (dockConfirmTimer) clearTimeout(dockConfirmTimer);
  dockConfirmTimer = setTimeout(() => {
    dockConfirmTimer = null;
    dockConfirmedUntil = 0;
    updateDockVisual(Boolean($("status")?.classList?.contains?.("error")));
  }, 2650);
}

function updateDockVisual(isError = false) {
  const dock = $("dock");
  if (!dock) return;
  const running = Boolean(state.running);
  const confirmed = !running && !isError && !state.outcome_unknown && Date.now() < dockConfirmedUntil;
  const pendingReady = !running && !isError && !confirmed && Boolean(state.pending?.ready);
  dock.classList.toggle("dock-running", running);
  dock.classList.toggle("dock-confirmed", confirmed);
  dock.classList.toggle("dock-pending", pendingReady);
  dock.classList.toggle("dock-error", Boolean(isError || state.outcome_unknown));
}

function notify(message, isError = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", isError);
  const dot = $("dock-dot");
  if (dot) {
    dot.classList.toggle("error", Boolean(isError));
    dot.classList.toggle("running", Boolean(state.running) && !isError);
  }
  updateDockVisual(isError);
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

function checkedVisibleHistory() {
  return visibleHistory().filter(s => historySelectedKeys.has(key(s)));
}

function selectedHistoryTargets(preferredRow = null) {
  const visChecked = checkedVisibleHistory();
  if (visChecked.length) return visChecked;
  const allChecked = historySummaries.filter(s => historySelectedKeys.has(key(s)));
  if (allChecked.length) return allChecked;
  if (preferredRow) return [preferredRow];
  const actRow = visibleHistory().find(s => key(s) === historyActiveKey);
  return actRow ? [actRow] : [];
}

function isResolvableLiveTarget(row) {
  if (!row || row.available === false) return false;
  const compatProf = state.profiles.some(p => p.uid === row.uid && p.compatible !== false);
  return compatProf && state.rows.some(r => r.uid === row.uid && r.name === row.name);
}

function resolvableHistoryTargets(targets) {
  return (targets || []).filter(isResolvableLiveTarget);
}

function historySelectionSignature(targets) {
  return JSON.stringify((targets || []).map(key));
}

function pruneHistorySelection() {
  const valid = new Set(historySummaries.map(key));
  for (const k of Array.from(historySelectedKeys)) {
    if (!valid.has(k)) historySelectedKeys.delete(k);
  }
}

function updateHistorySelectionUi(announce = false) {
  const items = visibleHistory();
  const checkedCount = items.filter(s => historySelectedKeys.has(key(s))).length;
  if (viewMode === "history") {
    $("count").textContent = checkedCount > 0 ? (items.length + " 候选 · 已选 " + checkedCount) : (items.length + " 候选");
  }
  const selAllBtn = $("history-select-all");
  const clrSelBtn = $("history-clear-sel");
  const blocked = state.running || busy;
  if (selAllBtn) selAllBtn.disabled = blocked || items.length === 0;
  if (clrSelBtn) clrSelBtn.disabled = blocked || historySelectedKeys.size === 0;
  if (announce) {
    notify(checkedCount > 0
      ? ("长效优选榜已勾选 " + checkedCount + " / " + items.length + " 个节点；可直接点击上方开始测速、加压复测或右键批量操作")
      : "已取消勾选长效候选节点");
  }
}

async function syncHistoryTargetsToLiveRaw(targets) {
  const resolvable = resolvableHistoryTargets(targets);
  if (!resolvable.length) {
    notify("已勾选的历史节点不在当前已加载的订阅列表中", true);
    return false;
  }
  const profs = [...new Set([
    ...state.profiles.filter(p => p.selected && p.compatible !== false).map(p => p.uid),
    ...resolvable.map(r => r.uid)
  ])];
  const ok = await call("set_selection", {
    profiles: profs,
    nodes: resolvable.map(r => ({uid: r.uid, name: r.name}))
  });
  if (!ok) return false;
  await refreshState();
  return true;
}
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
  tr.tabIndex = 0;
  const entry = {
    tr, row, box: null, cells: [],
    nameWrap: null, nameText: null, rankBadge: null, currentBadge: null,
    speedWrap: null, speedText: null, speedBar: null, values: null
  };
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

function updateRowEntry(entry, row, maxSpeed, isRankOne = false, isCurrent = false) {
  entry.row = row;
  const rowKey = key(row);
  const nowMs = Date.now();
  const localCasc = (localCascadeUntil.get(rowKey) || 0) > nowMs;
  if (!localCasc && localCascadeUntil.has(rowKey)) localCascadeUntil.delete(rowKey);
  entry.tr.classList.toggle("active", active === rowKey);
  entry.tr.classList.toggle("is-current-node", isCurrent);
  entry.tr.classList.toggle("is-rank-1", isRankOne);
  entry.tr.classList.toggle("is-testing-active", Boolean(row.live));
  entry.tr.classList.toggle("is-recently-scored", Boolean(row.recently_scored));
  entry.tr.classList.toggle("is-cascade", Boolean(row.cascade_highlighted) || localCasc);
  entry.tr.classList.toggle("is-located", locatedRowKey === rowKey);
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
  } else if (row.state === "等待") {
    stateText = "等待";
    scoreText = "队列中";
    speedText = "排队等待";
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
    if (i === 0) {
      if (!entry.nameWrap) {
        td.textContent = "";
        const wrap = document.createElement("div");
        wrap.className = "node-name-cell";
        const rankB = document.createElement("span");
        rankB.className = "badge badge-rank1";
        rankB.textContent = "#1";
        rankB.hidden = true;
        const nameSpan = document.createElement("span");
        nameSpan.className = "node-name-text";
        const curB = document.createElement("span");
        curB.className = "badge badge-current";
        curB.textContent = "当前";
        curB.hidden = true;
        wrap.append(rankB, nameSpan, curB);
        td.append(wrap);
        entry.nameWrap = wrap;
        entry.rankBadge = rankB;
        entry.nameText = nameSpan;
        entry.currentBadge = curB;
      }
      if (!entry.values || entry.values[0] !== v) {
        entry.nameText.textContent = v;
      }
      td.title = isCurrent ? `${v}（当前使用中）` : v;
      entry.rankBadge.hidden = !isRankOne;
      entry.currentBadge.hidden = !isCurrent;
      continue;
    }
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
        entry.speedBar.classList.toggle("rank-1", Boolean(isRankOne && !live));
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

function renderEmptyState(mode, itemsLength) {
  const emptyBox = $("empty");
  if (!emptyBox) return;
  emptyBox.hidden = itemsLength > 0;
  if (itemsLength > 0) {
    lastEmptySig = "";
    return;
  }
  let icon = "◎", title = "暂无可展示节点", desc = "当前筛选没有节点；可调整搜索或刷新订阅";
  const actions = [];
  if (mode === "history") {
    const query = ($("history-search")?.value || "").trim();
    if (historySummaries.length === 0) {
      icon = "◷";
      title = "暂无长效优选数据";
      desc = "执行测速或开启智能巡检后，系统将自动按 30 天窗口沉淀稳健排行";
      actions.push({label: "返回实时测速", primary: true, action: () => setViewMode("live")});
    } else {
      icon = "⌕";
      title = "当前筛选没有匹配的历史节点";
      desc = "可清空搜索条件、展示全部订阅的历史数据，或返回实时测速";
      if (query) {
        actions.push({label: "重置筛选条件", primary: true, action: () => {
          if ($("history-search")) $("history-search").value = "";
          renderHistoryRows();
        }});
      }
      if (historySelectedOnly) {
        actions.push({label: "显示全部订阅", primary: !query, action: () => {
          historySelectedOnly = false;
          if ($("history-selected-only")) {
            $("history-selected-only").classList.toggle("selected", false);
            $("history-selected-only").setAttribute("aria-pressed", "false");
          }
          renderHistoryRows();
        }});
      }
      actions.push({label: "返回实时测速", primary: false, action: () => setViewMode("live")});
    }
  } else {
    const profiles = state.profiles || [];
    const selectedProfiles = profiles.filter(p => p.selected);
    const query = ($("search")?.value || "").trim();
    const statusFilter = $("status-filter")?.value || "all";
    if (!profiles.length) {
      icon = "↻";
      title = "尚未加载本机订阅";
      desc = "点击下方按钮读取 Clash Verge 订阅配置并加载可测节点";
      actions.push({label: "刷新订阅列表", primary: true, action: () => $("refresh")?.click()});
    } else if (!selectedProfiles.length) {
      icon = "☰";
      title = "未勾选任何订阅源";
      desc = "请先勾选至少一个订阅源，再选择节点开始测速";
      const curCompat = profiles.find(p => p.current && p.compatible !== false);
      if (curCompat) {
        actions.push({label: "勾选当前订阅", primary: true, action: () => profileSelection(curCompat.uid, true)});
      }
      actions.push({label: "全选全部订阅", primary: !curCompat, action: () => $("all-profiles")?.click()});
    } else if (query || statusFilter !== "all") {
      icon = "⌕";
      title = "当前筛选条件下没有节点";
      desc = "可清空搜索词并切回「全部」状态查看已选订阅下的全部节点";
      actions.push({label: "重置筛选条件", primary: true, action: () => {
        if ($("search")) $("search").value = "";
        setStatusFilter("all");
      }});
    } else {
      icon = "◎";
      title = "已选订阅暂无可测节点";
      desc = "该订阅可能无内联代理节点，可刷新订阅或勾选其他订阅源";
      actions.push({label: "刷新订阅列表", primary: true, action: () => $("refresh")?.click()});
    }
  }
  const sig = `${mode}|${icon}|${title}|${desc}|${actions.map(a => a.label).join(",")}|${Boolean(state.running || busy)}`;
  if (sig === lastEmptySig) return;
  lastEmptySig = sig;
  if ($("empty-icon")) $("empty-icon").textContent = icon;
  if ($("empty-title")) $("empty-title").textContent = title;
  if ($("empty-desc")) $("empty-desc").textContent = desc;
  const actHost = $("empty-actions");
  if (actHost) {
    actHost.replaceChildren();
    for (const item of actions) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = item.label;
      if (item.primary) btn.className = "primary";
      btn.disabled = Boolean(state.running || busy);
      btn.onclick = item.action;
      actHost.append(btn);
    }
  }
}

function renderHistoryRows() {
  const host = $("rows");
  const items = visibleHistory();
  updateHistorySelectionUi(false);
  renderEmptyState("history", items.length);
  rowCache.clear();
  host.replaceChildren();
  const maxBps = Math.max(20 * 1048576, ...items.map(s => s.weighted_median_speed_bps || 0));
  for (let idx = 0; idx < items.length; idx++) {
    const s = items[idx];
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    const rk = key(s);
    const isCurrent = isActiveUsingRow(s);
    const isTopRank = idx === 0 && historySortColumn === "rank" && s.long_term_score > 0;
    const isChecked = historySelectedKeys.has(rk);
    tr.classList.toggle("active", historyActiveKey === rk);
    tr.classList.toggle("hist-checked", isChecked);
    tr.classList.toggle("is-current-node", isCurrent);
    tr.classList.toggle("is-rank-1", isTopRank);
    tr.onclick = () => { historyActiveKey = rk; renderHistoryRows(); updateChampion(); };
    tr.ondblclick = () => { historyActiveKey = rk; renderHistoryRows(); confirmManual(tr); };

    const tdRank = document.createElement("td");
    const rankWrap = document.createElement("div");
    rankWrap.className = "hist-rank-cell";
    const rankSpan = document.createElement("span");
    rankSpan.className = "hist-rank-text";
    rankSpan.textContent = isTopRank ? "#1" : String(idx + 1);
    const hbox = document.createElement("input");
    hbox.type = "checkbox";
    hbox.className = "hist-check";
    hbox.checked = isChecked;
    hbox.disabled = Boolean(state.running || busy);
    hbox.setAttribute("aria-label", "勾选历史节点 " + s.name);
    hbox.onclick = e => e.stopPropagation();
    hbox.ondblclick = e => e.stopPropagation();
    hbox.onchange = () => {
      if (hbox.checked) historySelectedKeys.add(rk);
      else historySelectedKeys.delete(rk);
      tr.classList.toggle("hist-checked", hbox.checked);
      updateHistorySelectionUi(true);
    };
    rankWrap.append(rankSpan, hbox);
    tdRank.append(rankWrap);
    tr.append(tdRank);

    if (s.health_penalty > 0) tr.append(makeBadgeCell("扣分", "badge-penalty"));
    else if (s.is_qualified) tr.append(makeBadgeCell("主力", "badge-main"));
    else tr.append(makeBadgeCell("待验", "badge-wait"));

    const tdName = document.createElement("td");
    const nameWrap = document.createElement("div");
    nameWrap.className = "node-name-cell";
    const nameSpan = document.createElement("span");
    nameSpan.className = "node-name-text";
    nameSpan.textContent = s.name;
    nameWrap.append(nameSpan);
    if (isCurrent) {
      const curB = document.createElement("span");
      curB.className = "badge badge-current";
      curB.textContent = "当前";
      nameWrap.append(curB);
    }
    tdName.append(nameWrap);
    tdName.title = s.health_penalty > 0
      ? `活跃健康异常：扣分 -${s.health_penalty.toFixed(1)}`
      : (isCurrent ? `${s.name}（当前使用中）` : s.name);
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
      bar.className = isTopRank ? "speed-bar rank-1" : "speed-bar";
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
    const isTop = isTopRank;
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

function updateSparkline(points, isReal = true) {
  const svg = $("best-sparkline");
  const path = $("best-sparkline-path");
  const area = $("best-sparkline-area");
  const dot = $("best-sparkline-dot");
  const titleEl = $("best-sparkline-title");
  if (!svg || !path) return;
  if (!points || points.length < 2) {
    svg.hidden = true;
    if (typeof svg.setAttribute === "function") svg.setAttribute("hidden", "");
    path.setAttribute("d", "");
    if (area) area.setAttribute("d", "");
    if (dot) {
      dot.hidden = true;
      if (typeof dot.setAttribute === "function") dot.setAttribute("hidden", "");
    }
    svg.classList.toggle("indicative", false);
    return;
  }
  const peak = Math.max(0.1, ...points);
  const coords = points.map((v, i) => {
    const x = (i * 116) / (points.length - 1);
    const norm = Math.min(0.96, Math.max(0.08, v / peak));
    const y = 25 - norm * 20;
    return [x, y];
  });
  let d = `M${coords[0][0].toFixed(1)} ${coords[0][1].toFixed(1)}`;
  for (let i = 1; i < coords.length; i++) {
    const [x0, y0] = coords[i - 1];
    const [x1, y1] = coords[i];
    const mx = ((x0 + x1) / 2).toFixed(1);
    const my = ((y0 + y1) / 2).toFixed(1);
    d += ` Q${x0.toFixed(1)} ${y0.toFixed(1)} ${mx} ${my}`;
  }
  const last = coords[coords.length - 1];
  d += ` L${last[0].toFixed(1)} ${last[1].toFixed(1)}`;
  path.setAttribute("d", d);
  if (area) {
    area.setAttribute("d", `${d} L${last[0].toFixed(1)} 28.0 L${coords[0][0].toFixed(1)} 28.0 Z`);
  }
  if (dot) {
    dot.setAttribute("cx", last[0].toFixed(1));
    dot.setAttribute("cy", last[1].toFixed(1));
    dot.hidden = false;
    if (typeof dot.removeAttribute === "function") dot.removeAttribute("hidden");
  }
  const tip = isReal
    ? `真实下载吞吐采样（${points.length} 点 · 峰值 ${Math.max(...points).toFixed(2)} MiB/s）`
    : "估算吞吐示意曲线（无实时采样）";
  if (titleEl) titleEl.textContent = tip;
  if (typeof svg.setAttribute === "function") svg.setAttribute("title", tip);
  svg.classList.toggle("indicative", !isReal);
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
  }, 420);
}

function setTelemetryItem(idx, label, value, tone = "muted") {
  const lEl = $(`best-t${idx}-label`);
  const vEl = $(`best-t${idx}-val`);
  if (lEl) lEl.textContent = label;
  if (vEl) {
    vEl.textContent = value;
    vEl.classList.toggle("tele-good", tone === "good");
    vEl.classList.toggle("tele-warn", tone === "warn");
    vEl.classList.toggle("tele-bad", tone === "bad");
    vEl.classList.toggle("tele-muted", tone === "muted");
  }
}

function setReadyPill(text, tone = "idle") {
  const ready = $("best-ready");
  if (!ready) return;
  ready.textContent = text;
  ready.classList.toggle("tone-ok", tone === "ok");
  ready.classList.toggle("tone-active", tone === "active");
  ready.classList.toggle("tone-warn", tone === "warn");
  ready.classList.toggle("tone-danger", tone === "danger");
  ready.classList.toggle("tone-idle", tone === "idle");
}

function latencyTone(ms) {
  if (ms == null) return "muted";
  if (ms <= 80) return "good";
  if (ms <= 180) return "warn";
  return "bad";
}

function jitterTone(ms) {
  if (ms == null) return "muted";
  if (ms <= 15) return "good";
  if (ms <= 45) return "warn";
  return "bad";
}

function lossTone(pct) {
  if (pct == null) return "muted";
  if (pct === 0) return "good";
  if (pct <= 17) return "warn";
  return "bad";
}

function updateChampion() {
  const eyebrow = $("best-eyebrow");
  if (viewMode === "history") {
    const winTitle = {"24h": "24小时", "7d": "7天", "30d": "30天", "peak": "晚高峰"}[historyWindow] || "7天";
    if (eyebrow) eyebrow.textContent = `长效优选 · ${winTitle}`;
    const hero = activeHeroRow();
    updateSparkline(null);
    if (!hero) {
      $("best-name").textContent = "暂无长效历史数据";
      $("best-profile").textContent = "运行测速或智能巡检后自动积累";
      setTelemetryItem(1, "典型延迟", "—", "muted");
      setTelemetryItem(2, "可用率", "—", "muted");
      setTelemetryItem(3, "样本", "—", "muted");
      setReadyPill("● 归档等待中", "idle");
      setAnimatedChampionNumbers(null, null, "—", "—");
      return;
    }
    const usingNow = isActiveUsingRow(hero);
    $("best-name").textContent = hero.name;
    const availPct = Math.round(hero.availability_rate * 100);
    const protoPart = hero.node_type ? ` · ${hero.node_type.toUpperCase()}` : "";
    $("best-profile").textContent = `${hero.profile}${protoPart}${usingNow ? " · 使用中" : ""}`;
    const hasLat = hero.success_count > 0;
    setTelemetryItem(1, "典型延迟", hasLat ? `${hero.weighted_median_latency_ms.toFixed(0)} ms` : "—", hasLat ? latencyTone(hero.weighted_median_latency_ms) : "muted");
    const availTone = hero.availability_rate >= 0.85 ? "good" : (hero.availability_rate >= 0.5 ? "warn" : "bad");
    setTelemetryItem(2, "可用率", `${availPct}%`, availTone);
    if (hero.health_penalty > 0) {
      setTelemetryItem(3, "健康扣分", `-${hero.health_penalty.toFixed(1)}`, "bad");
      setReadyPill(`● 活跃异常 -${Math.round(hero.health_penalty)}`, "danger");
    } else {
      setTelemetryItem(3, "样本", `${hero.success_count}/${hero.total_count}`, hero.is_qualified ? "good" : "warn");
      if (usingNow) setReadyPill("● 当前使用中", "ok");
      else if (hero.is_qualified) setReadyPill("● 长期主力候选", "ok");
      else setReadyPill(`● 样本累积 (${hero.success_count}/3)`, "warn");
    }
    const speedMib = hero.success_count > 0 ? hero.weighted_median_speed_bps / 1048576 : null;
    setAnimatedChampionNumbers(hero.long_term_score, speedMib, hero.long_term_score.toFixed(1), speedMib != null ? `${speedMib.toFixed(2)} MiB/s` : "—");
    return;
  }

  if (eyebrow) eyebrow.textContent = "最优路由";
  const best = winner();
  const hero = best || activeHeroRow();
  if (!hero) {
    $("best-name").textContent = "等待测速";
    $("best-profile").textContent = "选择订阅源与节点后开始";
    setTelemetryItem(1, "延迟", "—", "muted");
    setTelemetryItem(2, "抖动", "—", "muted");
    setTelemetryItem(3, "丢包", "—", "muted");
    setReadyPill(state.running ? "● 测速运行中" : "● 待命就绪", state.running ? "active" : "idle");
    updateSparkline(null);
    lastChampSig = "";
    setAnimatedChampionNumbers(null, null, "—", "—");
    return;
  }
  const usingNow = isActiveUsingRow(hero);
  $("best-name").textContent = hero.name;
  if (hero.score != null && hero.speed != null) {
    const speedMib = hero.speed / 1048576;
    const lossPct = hero.successes != null ? Math.round((6 - hero.successes) * 100 / 6) : null;
    $("best-profile").textContent = `${hero.profile}${hero.node_type ? ` · ${hero.node_type.toUpperCase()}` : ""}${usingNow ? " · 使用中" : ""}`;
    setTelemetryItem(1, "延迟", hero.latency == null ? "—" : `${hero.latency.toFixed(0)} ms`, latencyTone(hero.latency));
    setTelemetryItem(2, "抖动", hero.jitter == null ? "—" : `${hero.jitter.toFixed(0)} ms`, jitterTone(hero.jitter));
    setTelemetryItem(3, "丢包", lossPct == null ? "—" : `${lossPct}%`, lossTone(lossPct));
    if (state.outcome_unknown) setReadyPill("● 切换已锁定", "danger");
    else if (state.pending && !state.pending.ready) setReadyPill("● 待切换订阅激活", "warn");
    else if (state.pending?.ready) setReadyPill("● 可继续跨订阅切换", "warn");
    else if (state.running) setReadyPill("● 测速运行中", "active");
    else if (usingNow) setReadyPill("● 当前使用中", "ok");
    else setReadyPill("● 最优路由就绪", "ok");
    if (Array.isArray(hero.samples) && hero.samples.length >= 2) {
      updateSparkline(hero.samples, true);
    } else {
      const norm = Math.min(0.96, Math.max(0.15, speedMib / 25));
      updateSparkline([norm * 0.68, norm * 0.82, norm * 0.76, norm * 0.91, norm, norm * 0.95], false);
    }
    const sig = `${hero.uid}|${hero.name}|${hero.score.toFixed(2)}|${speedMib.toFixed(2)}`;
    if (sig !== lastChampSig) {
      if (lastChampSig) triggerChampionPulse();
      lastChampSig = sig;
    }
    setAnimatedChampionNumbers(hero.score, speedMib, hero.score.toFixed(1), `${speedMib.toFixed(2)} MiB/s`);
  } else if (hero.live) {
    const live = hero.live;
    if (live.phase === "head") {
      $("best-profile").textContent = `${hero.profile}${usingNow ? " · 使用中" : ""} · 正在探测延迟…`;
      setAnimatedChampionNumbers(null, null, "探测", "…");
      updateSparkline(null);
    } else {
      const pct = live.expected_bytes > 0 ? Math.min(99, Math.max(0, Math.round(live.received_bytes / live.expected_bytes * 100))) : 0;
      const mib = live.bps / 1048576;
      $("best-profile").textContent = `${hero.profile}${usingNow ? " · 使用中" : ""} · 实时下载 ${formatBytes(live.received_bytes)} / ${formatBytes(live.expected_bytes)}`;
      setAnimatedChampionNumbers(null, null, pct > 0 ? `${pct}%` : "测速", live.bps > 0 ? `${mib.toFixed(2)} MiB/s` : "…");
      updateSparkline(live.samples || [], true);
    }
    setTelemetryItem(1, "延迟", "测算中", "muted");
    setTelemetryItem(2, "抖动", "测算中", "muted");
    setTelemetryItem(3, "丢包", "测算中", "muted");
    setReadyPill("● 实时吞吐探测", "active");
  } else {
    $("best-profile").textContent = `${hero.profile}${usingNow ? " · 使用中" : ""} · 状态：${hero.state}`;
    setTelemetryItem(1, "延迟", "—", "muted");
    setTelemetryItem(2, "抖动", "—", "muted");
    setTelemetryItem(3, "丢包", "—", "muted");
    if (state.running) setReadyPill("● 测速运行中", "active");
    else if (usingNow) setReadyPill("● 当前使用中", "ok");
    else setReadyPill("● 待命就绪", "idle");
    updateSparkline(null);
    setAnimatedChampionNumbers(null, null, "待测", "—");
  }
}

function updateOverallProgress() {
  const bar = $("overall-progress");
  const fill = $("overall-progress-fill");
  const pulse = $("overall-progress-pulse");
  const pill = $("dock-progress-pill");
  if (!bar || !fill) return;
  if (!state.running) {
    bar.classList.toggle("active", false);
    fill.style.transform = "scaleX(0)";
    if (pulse) pulse.hidden = true;
    if (pill) pill.hidden = true;
    return;
  }
  const rp = state.run_progress;
  let done = 0, total = 0, frac = 0.08, pct = 0;
  if (rp && rp.total > 0) {
    done = rp.done;
    total = rp.total;
    pct = rp.pct;
    frac = Math.min(1, Math.max(0.04, pct / 100));
  } else {
    const activeRows = state.rows.filter(r => r.selected || r.live || r.state === "等待");
    total = activeRows.length;
    let progressSum = 0;
    for (const r of activeRows) {
      if (r.live && r.live.expected_bytes > 0) {
        progressSum += Math.min(0.95, r.live.received_bytes / r.live.expected_bytes);
      } else if (r.state !== "等待" && r.state !== "未勾选" && r.state !== "待测速") {
        progressSum += 1;
        done += 1;
      }
    }
    frac = total > 0 ? Math.min(1, Math.max(0.04, progressSum / total)) : 0.08;
    pct = total > 0 ? Math.min(99, Math.round(frac * 100)) : 0;
  }
  bar.classList.toggle("active", true);
  fill.style.transform = `scaleX(${frac.toFixed(4)})`;
  if (pulse) pulse.hidden = false;
  if (pill) {
    if (total > 0) {
      pill.textContent = `${done}/${total} · ${pct}%`;
      pill.hidden = false;
    } else {
      pill.hidden = true;
    }
  }
}

function setStatusFilter(val) {
  const sel = $("status-filter");
  if (sel) sel.value = val;
  updateFilterPills();
  renderRows();
}

function updateFilterPills() {
  const current = $("status-filter")?.value || "all";
  const counts = typeof RowModel.bucketCounts === "function"
    ? RowModel.bucketCounts(state.rows, state.profiles, $("search")?.value || "")
    : {all: 0, valid: 0, testing: 0, failed: 0};
  if ($("fc-all")) $("fc-all").textContent = String(counts.all);
  if ($("fc-valid")) $("fc-valid").textContent = String(counts.valid);
  if ($("fc-testing")) $("fc-testing").textContent = String(counts.testing);
  if ($("fc-failed")) $("fc-failed").textContent = String(counts.failed);
  for (const btn of document.querySelectorAll("button[data-status]")) {
    const isAct = btn.dataset.status === current;
    btn.classList.toggle("active", isAct);
    btn.setAttribute("aria-pressed", String(isAct));
  }
}

function renderRows() {
  if (viewMode === "history") {
    renderHistoryRows();
    return;
  }
  const host = $("rows");
  const rows = visible();
  updateFilterPills();
  const selectedProfCount = (state.profiles || []).filter(p => p.selected).length;
  const checkedInSelected = selectedNodes().length;
  $("count").textContent = selectedProfCount === 0
    ? "0 / 0"
    : `${rows.length} 节点 · ${checkedInSelected} 已选`;
  renderEmptyState("live", rows.length);
  const maxSpeed = Math.max(25 * 1048576, ...rows.map(r => r.speed || 0));
  const actIdent = activeUsingIdentity();
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
      const isRankOne = i === 0 && sortColumn === "score" && !sortAscending && Boolean(row.eligible && row.score != null);
      const isCurrent = Boolean(actIdent && row.uid === actIdent.uid && row.name === actIdent.name);
      updateRowEntry(entry, row, maxSpeed, isRankOne, isCurrent);
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
      const isCurrent = Boolean(actIdent && row.uid === actIdent.uid && row.name === actIdent.name);
      updateRowEntry(entry, row, maxSpeed, false, isCurrent);
      host.append(entry.tr);
    }
  }
  updateChampion();
  $("traffic").textContent = selectedProfCount === 0 || checkedInSelected === 0
    ? "预计流量 —"
    : `预计流量 ${(checkedInSelected * Number($("size").value) / 1048576).toFixed(1)} MiB`;
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

function buildPatrolMeta() {
  const patrol = state.patrol || {};
  const quotaBytes = Number(patrol.quota_bytes || $("patrol-quota")?.value || 80 * 1048576);
  const quotaMib = Math.max(1, Math.round(quotaBytes / 1048576));
  const usedMib = Math.ceil((patrol.used_bytes || 0) / 1048576);
  const intervalMin = Number(patrol.interval_minutes || (quotaBytes >= 150 * 1048576 ? 30 : 60));
  const defaultBatch = quotaBytes >= 300 * 1048576 ? 8 : (quotaBytes >= 150 * 1048576 ? 6 : (quotaBytes >= 80 * 1048576 ? 5 : 4));
  const batchCap = Number(patrol.max_per_tick || defaultBatch);
  const nightCap = Math.max(2, Math.floor(batchCap / 2));
  const peakReserveMib = Math.max(1, Math.floor(quotaMib / 4));
  const stateLine = $("patrol-note")?.textContent || (patrol.armed ? "巡检已启用" : "巡检未启用");
  return {quotaBytes, quotaMib, usedMib, intervalMin, batchCap, nightCap, peakReserveMib, stateLine};
}

function buildPatrolToolTip() {
  const m = buildPatrolMeta();
  return [
    "智能巡检（后台低干扰积累长效评分）",
    "• 按配额自动配速：每 " + m.intervalMin + " 分钟以 2 并发抽测最多 " + m.batchCap + " 节点（深夜 01:00–07:00 减半至 " + m.nightCap + " 节点，每节点 1 MiB / 2 流），结果归档至「长效优选榜」。",
    "• 智能轮换挑点：健康扣分旗舰 5 分钟快复核 + 合格守擂 90 分钟轮换（晚高峰 45 分钟且首轮直测）+ 多订阅轮询冷启动与潜力节点快速毕业。",
    "• 死节点退避与晚高峰保护：连续失败节点严格退避 2~24 小时不重复探测（失败仅计 ≤256 KiB）；20:00 前预留 25%（约 " + m.peakReserveMib + " MiB）供晚高峰及夜间采样。",
    "• 只测速不改动节点勾选，也不会自动切换代理节点；支持托盘后台运行。",
    "当前状态：" + m.stateLine + "（今日 " + m.usedMib + "/" + m.quotaMib + " MiB）"
  ].join("\n");
}

function renderPatrolPopoverContent() {
  const m = buildPatrolMeta();
  if ($("patrol-pop-sub")) {
    $("patrol-pop-sub").textContent = "每 " + m.intervalMin + " 分钟最多 " + m.batchCap + " 节点 · 今日 " + m.usedMib + "/" + m.quotaMib + " MiB（晚高峰预留 " + m.peakReserveMib + " MiB）";
  }
  const list = $("patrol-pop-list");
  if (list) {
    const bullets = [
      "全天均衡配速：按日配额自动控速（深夜 01:00–07:00 减半至 " + m.nightCap + " 节点），20:00 前预留 25% 给晚高峰采样，避免通宵或午前耗尽预算。",
      "守擂轮换与冷启动：健康扣分旗舰 5 分钟快复核；合格主力 90 分钟轮换（晚高峰 45 分钟）；多订阅交错探索并优先助推 1–2 样本潜力节点达标。",
      "死节点严格退避：连续失败节点自动跳过 2~24 小时指数退避窗口，失败探针仅计低额防空转成本（≤256 KiB）。",
      "低干扰且安全：仅 2 并发 1 MiB 轻量采样，绝不改勾选、不会自动切换节点，支持托盘后台运行。"
    ];
    list.replaceChildren();
    for (const line of bullets) {
      const li = document.createElement("li");
      li.textContent = line;
      list.append(li);
    }
  }
}

function closePatrolPopover(restoreFocus = false) {
  const pop = $("patrol-popover");
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  if ($("patrol-info")) {
    $("patrol-info").setAttribute("aria-expanded", "false");
    if (restoreFocus && typeof $("patrol-info").focus === "function") $("patrol-info").focus();
  }
}

function openPatrolPopover() {
  const pop = $("patrol-popover");
  const btn = $("patrol-info");
  if (!pop || !btn) return;
  closeRowMenu();
  renderPatrolPopoverContent();
  pop.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  const rect = btn.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  const popW = popRect.width || 360;
  const popH = popRect.height || 230;
  const vw = typeof window !== "undefined" ? (window.innerWidth || 1240) : 1240;
  const vh = typeof window !== "undefined" ? (window.innerHeight || 800) : 800;
  const left = Math.max(12, Math.min(rect.left - popW + 24, vw - popW - 12));
  const top = Math.max(12, Math.min(rect.bottom + 8, vh - popH - 12));
  pop.style.left = left + "px";
  pop.style.top = top + "px";
}

function togglePatrolPopover() {
  const pop = $("patrol-popover");
  if (!pop) return;
  if (pop.hidden) openPatrolPopover();
  else closePatrolPopover(true);
}

function renderPatrolAndHealth() {
  const patrol = state.patrol || {};
  const quotaSelect = $("patrol-quota");
  const toggleBtn = $("patrol-toggle");
  const patrolNote = $("patrol-note");
  const quotaBytes = patrol.quota_bytes || state.preferences?.patrol_quota || 80 * 1048576;
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
  const patrolHelp = buildPatrolToolTip();
  if (toggleBtn) toggleBtn.title = patrolHelp;
  if (quotaSelect) quotaSelect.title = patrolHelp;
  if ($("patrol-info")) $("patrol-info").title = patrolHelp;
  if (patrolNote) patrolNote.title = patrolHelp;
  if ($("patrol-popover") && !$("patrol-popover").hidden) {
    renderPatrolPopoverContent();
  }
  const health = state.health_probe || {};
  const pill = $("health-pill");
  if (pill) {
    pill.textContent = health.text || "● 活跃探测 待命";
    pill.classList.toggle("ok", health.level === "ok");
    pill.classList.toggle("warn", health.level === "warn");
    pill.classList.toggle("danger", health.level === "danger");
    pill.classList.toggle("probing", health.level === "probing");
    pill.disabled = Boolean(state.running || busy || !$("group")?.value);
    pill.title = pill.disabled
      ? "空闲且已选策略组时，点击可立即探测当前活跃节点延迟"
      : "点击立即探测当前活跃节点延迟";
  }
  const bp = $("bridge-pill");
  if (bp) {
    const rb = state.runtime_bridge || {};
    const ready = Boolean(rb.ready);
    const needsRestart = rb.state === "needs_restart";
    bp.textContent = ready
      ? (rb.attached ? "● 热切已连接" : "● 热切就绪")
      : (needsRestart ? "● 热切待重启" : "○ 跨订阅热切");
    bp.classList.toggle("ready", ready);
    bp.classList.toggle("warn", needsRestart);
    bp.disabled = Boolean(state.running || busy);
    bp.title = ready
      ? "跨订阅无感热切已就绪（零弹窗本机受权轻桥）；点击重新检测"
      : (needsRestart
        ? "已为 clash-verge.exe 写入官方 WebView2 策略，请重启一次 Clash Verge 生效；点击重新检测"
        : "点击启用跨订阅无感热切（仅为 clash-verge.exe 写入官方 WebView2 本机策略，不修改原版程序）");
  }
}

function render() {
  hydratePreferences(state.preferences);
  if (state.funnel_stage === 1) {
    $("parallel").value = "10"; $("size").value = "1048576"; $("mode").value = "fast";
  } else if (state.funnel_stage === 2) {
    $("parallel").value = "2"; $("size").value = "20971520"; $("mode").value = "accurate";
  }
  renderProfiles();
  renderGroups();
  renderRows();
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
  if (viewMode === "history") updateHistorySelectionUi(false);
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
  if (state.status && state.status !== lastStatusSeen) {
    lastStatusSeen = state.status;
    if (!state.running && !unknown && (state.status.includes("已切换并复核") || state.status.includes("已切回原节点并复核") || state.status.includes("已自动热切订阅"))) {
      flashDockConfirmed();
    }
  }
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
    pruneHistorySelection();
    if (viewMode === "history") renderHistoryRows();
  } catch (_) {}
}

function setViewMode(mode) {
  const nextMode = mode === "history" ? "history" : "live";
  const changed = nextMode !== viewMode;
  viewMode = nextMode;
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
  lastEmptySig = "";
  const scroller = $("table-scroll");
  if (changed && scroller && !reducesMotion()) {
    scroller.classList.toggle("view-entering", false);
    void scroller.offsetWidth;
    scroller.classList.toggle("view-entering", true);
  }
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
  persistPreferences({top_n: n});
  const chosenKeys = visible().slice(0, n).map(key);
  const exp = Date.now() + 520;
  for (const rk of chosenKeys) localCascadeUntil.set(rk, exp);
  updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), new Set(chosenKeys));
};
$("search").oninput = renderRows;
$("status-filter").onchange = () => { updateFilterPills(); renderRows(); };
for (const btn of document.querySelectorAll("button[data-status]")) {
  btn.onclick = () => setStatusFilter(btn.dataset.status);
}
$("topn").oninput = () => validatePendingConfirm();
$("topn").onchange = () => {
  const n = Number($("topn").value);
  if (Number.isInteger(n) && n >= 1 && n <= 2000) persistPreferences({top_n: n});
};
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
if ($("history-select-all")) {
  $("history-select-all").onclick = () => {
    if (busy || state.running) return;
    const items = visibleHistory();
    if (!items.length) return;
    for (const item of items) historySelectedKeys.add(key(item));
    renderHistoryRows();
    updateHistorySelectionUi(true);
  };
}
if ($("history-clear-sel")) {
  $("history-clear-sel").onclick = () => {
    if (busy || state.running || !historySelectedKeys.size) return;
    historySelectedKeys.clear();
    renderHistoryRows();
    notify("已清空长效优选榜勾选");
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
        historySelectedKeys.clear();
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
    const quotaVal = Number($("patrol-quota").value);
    if (Number.isInteger(quotaVal) && quotaVal > 0) persistPreferences({patrol_quota: quotaVal});
    if (state.patrol?.armed) {
      runBusy(async () => {
        await call("set_patrol", {enabled: false, quota_bytes: quotaVal});
      });
    } else {
      renderPatrolAndHealth();
    }
  };
}
if ($("patrol-info")) $("patrol-info").onclick = () => togglePatrolPopover();
if ($("patrol-pop-close")) $("patrol-pop-close").onclick = () => closePatrolPopover(true);

if ($("health-pill")) {
  $("health-pill").onclick = async () => {
    if (busy || state.running || !$("group")?.value) return;
    if (typeof api().probe_active_node !== "function") return;
    const pill = $("health-pill");
    pill.textContent = "● 探测中…";
    pill.classList.toggle("ok", false);
    pill.classList.toggle("warn", false);
    pill.classList.toggle("danger", false);
    pill.classList.toggle("probing", true);
    const ok = await call("probe_active_node", {group: $("group").value});
    if (ok) {
      setTimeout(() => refreshState(true), 250);
    } else {
      await refreshState(true);
    }
  };
}

async function parametersChanged() {
  validatePendingConfirm();
  if (state.schedule_minutes) await call("set_schedule", {minutes: 0});
  renderRows();
  await refreshState();
}
$("group").onchange = () => {
  lastSelectedGroup = $("group").value;
  renderRows();
  renderPatrolAndHealth();
  return parametersChanged();
};
$("parallel").onchange = () => { persistPreferences({parallel: Number($("parallel").value)}); return parametersChanged(); };
$("size").onchange = () => { persistPreferences({download_bytes: Number($("size").value)}); return parametersChanged(); };
$("mode").onchange = () => { persistPreferences({mode: $("mode").value}); return parametersChanged(); };
$("interval").onchange = () => { persistPreferences({interval: Number($("interval").value)}); return parametersChanged(); };
$("auto").onchange = parametersChanged;

function confirmBatchHistoryStress(targets, trigger) {
  if (busy || state.running) return;
  const resolvable = resolvableHistoryTargets(targets);
  if (!resolvable.length) { notify("已勾选的历史节点不在当前已加载的订阅列表中", true); return; }
  const count = resolvable.length;
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const group = locked ? "" : $("group").value;
  const estimate = (count * 20).toFixed(1);
  const snap = {
    optionsSig: optionsSignature(),
    histSig: historySelectionSignature(resolvable),
    outcomeUnknown: locked
  };
  openConfirm({
    title: "确认加压验证 · 20M",
    message: "对已勾选的 " + count + " 个长效优选节点执行加压验证（并发 2 / 每节点 20 MiB / 精确模式），预计产生约 " + estimate + " MiB 实际流量。自动切换" + (auto ? "已开启" : "未开启") + (auto && group ? ("，目标策略组“" + group + "”") : "") + "。",
    trigger,
    validate: () => Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
      optionsSignature() === snap.optionsSig &&
      historySelectionSignature(resolvableHistoryTargets(selectedHistoryTargets())) === snap.histSig,
    onAccept: async () => {
      await runBusy(async () => {
        const synced = await syncHistoryTargetsToLiveRaw(resolvable);
        if (!synced) return;
        setViewMode("live");
        $("parallel").value = "2";
        $("size").value = "20971520";
        $("mode").value = "accurate";
        if (state.schedule_minutes) await call("set_schedule", {minutes: 0});
        await call("start", options());
      });
    }
  });
}

$("stress").onclick = () => {
  if (busy || state.running) return;
  if (viewMode === "history") {
    const histChecked = checkedVisibleHistory();
    if (histChecked.length) {
      confirmBatchHistoryStress(histChecked, $("stress"));
      return;
    }
  }
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
    message: "对已勾选的 " + count + " 个节点执行加压验证（并发 2 / 每节点 20 MiB / 精确模式），预计产生约 " + estimate + " MiB 实际流量。自动切换" + (auto ? "已开启" : "未开启") + (auto && group ? ("，目标策略组“" + group + "”") : "") + "。",
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
  const histChecked = viewMode === "history" ? checkedVisibleHistory() : [];
  const histResolvable = histChecked.length ? resolvableHistoryTargets(histChecked) : [];
  if (histChecked.length && !histResolvable.length) {
    notify("已勾选的历史节点不在当前已加载的订阅列表中", true);
    return;
  }
  const usingHist = histResolvable.length > 0;
  const count = usingHist ? histResolvable.length : selectedNodes().length;
  if (!count) { notify("请先选择要测速的节点", true); return; }
  const top = 10, estimate = count + Math.min(count, top) * 20;
  const locked = Boolean(state.outcome_unknown);
  const auto = locked ? false : $("auto").checked;
  const group = locked ? "" : $("group").value;
  const snap = {
    top,
    usingHist,
    optionsSig: optionsSignature(),
    selectionSig: usingHist ? historySelectionSignature(histResolvable) : selectionSignature(),
    outcomeUnknown: locked
  };
  openConfirm({
    title: "确认智能两段优选",
    message: "先以每节点 1 MiB 初筛，再对综合分前 " + top + " 名以 20 MiB 复测。预计最多约 " + estimate + " MiB（不是流量上限）。自动切换" + (auto ? "已开启" : "未开启") + (auto && group ? ("，目标策略组“" + group + "”") : "") + "。",
    trigger: $("funnel"),
    validate: () => Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
      optionsSignature() === snap.optionsSig &&
      (snap.usingHist
        ? historySelectionSignature(resolvableHistoryTargets(checkedVisibleHistory())) === snap.selectionSig
        : selectionSignature() === snap.selectionSig),
    onAccept: async () => {
      await runBusy(async () => {
        if (snap.usingHist) {
          const synced = await syncHistoryTargetsToLiveRaw(histResolvable);
          if (!synced) return;
          setViewMode("live");
        }
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
  const isCurrentProf = Boolean(row && state.profiles.some(p => p.uid === row.uid && p.current));
  const bridgeReady = Boolean(state.runtime_bridge?.ready);
  const crossHotSwitch = Boolean(row && !isCurrentProf && bridgeReady);
  if (!row) { notify("请先选中节点和策略组", true); return; }
  if (!isCurrentProf && !bridgeReady) {
    notify("该节点不在当前订阅；请先启用跨订阅热切或在 Clash Verge 手动切换订阅", true);
    return;
  }
  if (isCurrentProf && !group) { notify("请先选中节点和策略组", true); return; }
  const snap = {
    rowKey: key(row),
    group,
    groupsSig: groupsSignature(),
    mode: viewMode,
    crossHotSwitch,
    bridgeReady
  };
  const confirmTitle = crossHotSwitch ? "确认跨订阅无感热切" : "确认切换策略组";
  const confirmMessage = crossHotSwitch
    ? `将自动热切 Clash Verge 订阅至“${row.profile || row.uid}”，并${group ? `将策略组“${group}”` : "自动匹配策略组"}切换到“${row.name}”？`
    : `将策略组“${group}”切换到“${row.name}”？`;
  openConfirm({
    title: confirmTitle,
    message: confirmMessage,
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
        groupsSignature() === snap.groupsSig &&
        Boolean(state.runtime_bridge?.ready) === snap.bridgeReady;
    },
    onAccept: async () => {
      const ok = await runBusy(async () => {
        return await call("switch_manual", {uid: row.uid, name: row.name, group});
      });
      if (ok) flashDockConfirmed();
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
  if (viewMode === "history") {
    const histChecked = checkedVisibleHistory();
    if (histChecked.length) {
      const synced = await syncHistoryTargetsToLiveRaw(histChecked);
      if (!synced) return;
    }
    setViewMode("live");
  }
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
      const ok = await runBusy(async () => await call("undo_switch"));
      if (ok) flashDockConfirmed();
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
function formatSummaryFallback(row) {
  if (viewMode === "history") {
    const proto = (row.node_type || "—").toUpperCase();
    const speed = row.success_count > 0 ? `${(row.weighted_median_speed_bps / 1048576).toFixed(2)} MiB/s` : "—";
    const lat = row.success_count > 0 ? `${row.weighted_median_latency_ms.toFixed(0)} ms` : "—";
    const avail = `${Math.round((row.availability_rate || 0) * 100)}%`;
    return `[${row.profile}] ${row.name} (${proto}) · 稳健吞吐 ${speed} · 典型延迟 ${lat} · 可用率 ${avail} · 样本 ${row.success_count}/${row.total_count} · 长效分 ${(row.long_term_score || 0).toFixed(1)}`;
  }
  const proto = (row.node_type || "—").toUpperCase();
  const speed = row.speed != null ? `${(row.speed / 1048576).toFixed(2)} MiB/s` : "—";
  const lat = row.latency != null ? `${row.latency.toFixed(0)} ms` : "—";
  const jitter = row.jitter != null ? `${row.jitter.toFixed(0)} ms` : "—";
  const loss = row.successes != null ? `${Math.round((6 - row.successes) * 100 / 6)}%` : "—";
  const score = row.score != null ? row.score.toFixed(1) : (row.state || "—");
  return `[${row.profile}] ${row.name} (${proto}) · 吞吐 ${speed} · 延迟 ${lat} · 抖动 ${jitter} · 丢包 ${loss} · 评分 ${score}`;
}
function locateInLiveQueue(row) {
  if (!row) return;
  const rk = key(row);
  if (!state.rows.some(r => key(r) === rk)) {
    notify("该历史节点不在当前已加载的订阅列表中", true);
    return;
  }
  if (viewMode !== "live") setViewMode("live");
  if ($("search")) $("search").value = "";
  if ($("status-filter")) $("status-filter").value = "all";
  updateFilterPills();
  active = rk;
  locatedRowKey = rk;
  if (locatedRowTimer) clearTimeout(locatedRowTimer);
  locatedRowTimer = setTimeout(() => {
    locatedRowKey = null;
    locatedRowTimer = null;
    if (viewMode === "live") renderRows();
  }, 950);
  const needsProfile = !state.profiles.some(p => p.uid === row.uid && p.selected);
  const scrollTarget = () => {
    renderRows();
    const entry = rowCache.get(rk);
    if (entry?.tr && typeof entry.tr.scrollIntoView === "function") {
      entry.tr.scrollIntoView({block: "center", behavior: reducesMotion() ? "auto" : "smooth"});
    }
    if (entry?.tr && typeof entry.tr.focus === "function") entry.tr.focus();
  };
  if (needsProfile && !state.running && !busy) {
    profileSelection(row.uid, true).then(scrollTarget);
  } else {
    scrollTarget();
  }
}
function openRowMenu(row, x, y, trigger = null) {
  if (!row || busy) return;
  closePatrolPopover();
  const isHist = viewMode === "history";
  if (isHist) historyActiveKey = key(row);
  else active = key(row);
  menuTarget = key(row);
  menuTrigger = trigger;
  const menu = $("row-menu");
  menu.hidden = false;
  const targets = isHist ? selectedHistoryTargets(row) : [row];
  const batchCount = isHist ? targets.length : 1;
  const resolvable = isHist ? resolvableHistoryTargets(targets) : (isResolvableLiveTarget(row) ? [row] : []);
  const isCurrentProf = state.profiles.some(p => p.uid === row.uid && p.current);
  const isCompatProf = state.profiles.some(p => p.uid === row.uid && p.compatible !== false);
  const inLiveCatalog = state.rows.some(r => r.uid === row.uid && r.name === row.name);
  const bridgeReady = Boolean(state.runtime_bridge?.ready);
  const canSwitchRow = isCurrentProf
    ? Boolean($("group").value)
    : Boolean(bridgeReady && isCompatProf);
  $("menu-switch").disabled = state.running || Boolean(state.outcome_unknown) || !canSwitchRow || (isHist && row.available === false);
  $("menu-retest").textContent = batchCount > 1 ? ("批量 20MiB 加压复测已选 (" + batchCount + " 个)") : "单独 20MiB 加压复测此节点";
  $("menu-retest").disabled = isHist
    ? (state.running || resolvable.length === 0)
    : (state.running || !isCompatProf);
  if ($("menu-locate")) {
    $("menu-locate").hidden = !isHist;
    $("menu-locate").textContent = batchCount > 1 ? ("将已选 " + batchCount + " 个同步到实时队列") : "在实时队列中查看";
    $("menu-locate").disabled = batchCount > 1 ? (state.running || resolvable.length === 0) : !inLiveCatalog;
  }
  if ($("menu-live-sep")) $("menu-live-sep").hidden = false;
  if ($("menu-only-node")) {
    $("menu-only-node").hidden = false;
    $("menu-only-node").disabled = state.running;
  }
  if ($("menu-filter-profile")) {
    $("menu-filter-profile").hidden = isHist;
    $("menu-filter-profile").textContent = row.profile ? ("仅筛选「" + row.profile + "」") : "仅筛选「同订阅」";
    $("menu-filter-profile").disabled = !row.profile;
  }
  $("menu-copy").textContent = batchCount > 1 ? ("复制已选节点名称 (" + batchCount + " 个)") : "复制节点名称";
  if ($("menu-copy-summary")) {
    $("menu-copy-summary").textContent = batchCount > 1 ? ("复制已选测速摘要 (" + batchCount + " 个)") : "复制测速摘要";
    $("menu-copy-summary").disabled = false;
  }
  const bounds = menu.getBoundingClientRect();
  menu.style.left = Math.max(0, Math.min(x, window.innerWidth - bounds.width - 8)) + "px";
  menu.style.top = Math.max(0, Math.min(y, window.innerHeight - bounds.height - 8)) + "px";
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
$("menu-retest").onclick = () => {
  const row = menuRow();
  const trigger = menuTrigger;
  closeRowMenu();
  if (!row) return;
  if (viewMode === "history") {
    const targets = selectedHistoryTargets(row);
    if (targets.length > 1) {
      confirmBatchHistoryStress(targets, trigger || $("stress"));
      return;
    }
  }
  retestRow(row, trigger || $("retest-best"));
};
if ($("menu-locate")) {
  $("menu-locate").onclick = async () => {
    const row = menuRow();
    closeRowMenu();
    if (!row) return;
    const targets = viewMode === "history" ? selectedHistoryTargets(row) : [row];
    if (viewMode === "history" && targets.length > 1) {
      const resolvable = resolvableHistoryTargets(targets);
      if (!resolvable.length) {
        notify("已勾选的历史节点不在当前已加载的订阅列表中", true);
        return;
      }
      const ok = await runBusy(async () => await syncHistoryTargetsToLiveRaw(resolvable));
      if (!ok) return;
      if ($("search")) $("search").value = "";
      if ($("status-filter")) $("status-filter").value = "all";
      updateFilterPills();
      setViewMode("live");
      active = key(resolvable[0]);
      renderRows();
      notify("已将 " + resolvable.length + " 个长效节点同步勾选到实时队列");
      return;
    }
    locateInLiveQueue(row);
  };
}
if ($("menu-only-node")) {
  $("menu-only-node").onclick = () => {
    const row = menuRow();
    closeRowMenu();
    if (!row || busy || state.running) return;
    if (viewMode === "history") {
      historySelectedKeys.clear();
      historySelectedKeys.add(key(row));
      historyActiveKey = key(row);
      renderHistoryRows();
      updateHistorySelectionUi(true);
      return;
    }
    const profs = [...new Set([...state.profiles.filter(p => p.selected).map(p => p.uid), row.uid])];
    updateSelection(profs, new Set([key(row)]));
  };
}
if ($("menu-filter-profile")) {
  $("menu-filter-profile").onclick = () => {
    const row = menuRow();
    closeRowMenu();
    if (!row || !row.profile) return;
    if ($("search")) $("search").value = row.profile;
    renderRows();
  };
}
$("menu-copy").onclick = async () => {
  const row = menuRow();
  closeRowMenu();
  if (!row) return;
  const targets = viewMode === "history" ? selectedHistoryTargets(row) : [row];
  if (targets.length > 1) {
    if (await call("copy_node_name", {nodes: targets.map(t => ({uid: t.uid, name: t.name}))})) {
      notify("已复制 " + targets.length + " 个节点名称");
    }
    return;
  }
  if (await call("copy_node_name", {uid: row.uid, name: row.name})) notify("已复制节点名称");
};
if ($("menu-copy-summary")) {
  $("menu-copy-summary").onclick = async () => {
    const row = menuRow();
    closeRowMenu();
    if (!row) return;
    const targets = viewMode === "history" ? selectedHistoryTargets(row) : [row];
    if (typeof api().copy_node_summary === "function") {
      const payload = targets.length > 1
        ? {nodes: targets.map(t => ({uid: t.uid, name: t.name})), view: viewMode, window: historyWindow}
        : {uid: row.uid, name: row.name, view: viewMode, window: historyWindow};
      if (await call("copy_node_summary", payload)) {
        notify(targets.length > 1 ? ("已复制 " + targets.length + " 个节点测速摘要") : "已复制测速摘要");
      }
    } else if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(targets.map(formatSummaryFallback).join("\n"));
      notify(targets.length > 1 ? ("已复制 " + targets.length + " 个节点测速摘要") : "已复制测速摘要");
    }
  };
}

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

document.addEventListener("pointerdown", event => {
  if (!$("row-menu").hidden && !$("row-menu").contains(event.target)) closeRowMenu();
  const pop = $("patrol-popover");
  if (pop && !pop.hidden && !pop.contains(event.target) && event.target !== $("patrol-info") && !$("patrol-info")?.contains(event.target)) {
    closePatrolPopover();
  }
});
function isEditableTarget(el) {
  if (!el) return false;
  const tag = (el.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(el.isContentEditable);
}
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
  const pop = $("patrol-popover");
  if (pop && !pop.hidden && event.key === "Escape") {
    event.preventDefault();
    closePatrolPopover(true);
    return;
  }
  const menu = $("row-menu");
  if (!menu.hidden) {
    if (event.key === "Escape") { event.preventDefault(); closeRowMenu(true); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const items = [...menu.querySelectorAll("button:not(:disabled)")].filter(b => !b.hidden);
      if (!items.length) return;
      const index = items.indexOf(document.activeElement), step = event.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length].focus();
    }
    return;
  }

  const mod = Boolean(event.ctrlKey || event.metaKey);
  const k = (event.key || "").toLowerCase();
  if (mod && !event.altKey) {
    if (k === "f" && !event.shiftKey) {
      event.preventDefault();
      const box = viewMode === "history" ? $("history-search") : $("search");
      if (box && typeof box.focus === "function") {
        box.focus();
        if (typeof box.select === "function") box.select();
      }
      return;
    }
    if (k === "r" && !event.shiftKey) {
      event.preventDefault();
      if (!busy && !state.running) $("refresh")?.click();
      return;
    }
    if (k === "1" && !event.shiftKey) {
      event.preventDefault();
      setViewMode("live");
      return;
    }
    if (k === "2" && !event.shiftKey) {
      event.preventDefault();
      setViewMode("history");
      return;
    }
    if (event.key === "." && !event.shiftKey) {
      event.preventDefault();
      if (state.running) $("stop")?.click();
      return;
    }
    if (k === "c" && event.shiftKey) {
      event.preventDefault();
      $("export")?.click();
      return;
    }
    if (k === "c" && !event.shiftKey && !isEditableTarget(event.target)) {
      const selText = typeof window !== "undefined" && typeof window.getSelection === "function"
        ? String(window.getSelection() || "") : "";
      if (!selText) {
        if (viewMode === "history") {
          const targets = selectedHistoryTargets();
          if (targets.length > 1) {
            event.preventDefault();
            call("copy_node_name", {nodes: targets.map(t => ({uid: t.uid, name: t.name}))}).then(ok => {
              if (ok) notify("已复制 " + targets.length + " 个节点名称");
            });
            return;
          }
          const single = targets[0] || findLiveOrHistoryRow(historyActiveKey);
          if (single) {
            event.preventDefault();
            call("copy_node_name", {uid: single.uid, name: single.name}).then(ok => {
              if (ok) notify("已复制节点名称");
            });
          }
        } else {
          const row = findLiveOrHistoryRow(active);
          if (row) {
            event.preventDefault();
            call("copy_node_name", {uid: row.uid, name: row.name}).then(ok => {
              if (ok) notify("已复制节点名称");
            });
          }
        }
      }
      return;
    }
  }
  if (event.key === " " && !mod && !event.altKey && !busy && !state.running && !isEditableTarget(event.target)) {
    const tag = (event.target?.tagName || "").toUpperCase();
    if (tag !== "BUTTON") {
      if (viewMode === "live" && active) {
        const row = state.rows.find(r => key(r) === active);
        if (row) {
          event.preventDefault();
          const nodes = new Set(state.rows.filter(r => r.selected).map(key));
          row.selected ? nodes.delete(active) : nodes.add(active);
          updateSelection(state.profiles.filter(p => p.selected).map(p => p.uid), nodes);
        }
      } else if (viewMode === "history" && historyActiveKey) {
        const row = visibleHistory().find(r => key(r) === historyActiveKey);
        if (row) {
          event.preventDefault();
          if (historySelectedKeys.has(historyActiveKey)) historySelectedKeys.delete(historyActiveKey);
          else historySelectedKeys.add(historyActiveKey);
          renderHistoryRows();
          updateHistorySelectionUi(true);
        }
      }
    }
  }
});
window.addEventListener("blur", () => { closeRowMenu(); closePatrolPopover(); });

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
      const ok = await runBusy(async () => await call("continue_pending", {group}));
      if (ok) flashDockConfirmed();
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

$("export").onclick = async () => {
  actionEpoch += 1;
  try {
    const isHist = viewMode === "history";
    const checkedHist = isHist ? checkedVisibleHistory() : [];
    const exportReq = isHist ? {
      view: "history",
      window: historyWindow,
      selected_only: historySelectedOnly,
      query: $("history-search") ? $("history-search").value : ""
    } : null;
    if (isHist && checkedHist.length > 0) {
      exportReq.identities = checkedHist.map(r => ({uid: r.uid, name: r.name}));
    }
    const res = isHist ? await api().export_tsv(exportReq) : await api().export_tsv();
    if (!res?.ok) {
      notify(res?.error || "导出失败", true);
      return;
    }
    if (res.cancelled) return;
    const btn = $("export");
    if (btn) {
      btn.textContent = "✓ 已复制";
      btn.classList.toggle("btn-copied", true);
      if (exportCopiedTimer) clearTimeout(exportCopiedTimer);
      exportCopiedTimer = setTimeout(() => {
        exportCopiedTimer = null;
        if ($("export")) {
          $("export").textContent = "导出 TSV";
          $("export").classList.toggle("btn-copied", false);
        }
      }, 1500);
    }
    notify(isHist ? (checkedHist.length > 0 ? ("已导出已选 " + checkedHist.length + " 个长效节点 TSV 并复制到剪贴板") : "已导出长效优选 TSV 并复制到剪贴板") : "已导出 TSV 并复制到剪贴板");
  } catch (_) {
    notify("导出失败，请检查本地状态", true);
  }
};
if ($("bridge-pill")) {
  $("bridge-pill").onclick = () => {
    if (busy || state.running || typeof api().configure_runtime_bridge !== "function") return;
    const rb = state.runtime_bridge || {};
    if (rb.state === "not_configured" || !rb.policy_configured && !rb.ready) {
      openConfirm({
        title: "启用跨订阅无感热切",
        message: "将通过 Windows 官方 Edge WebView2 策略仅为 clash-verge.exe 写入本机随机调试端口（--remote-debugging-port=0），不修改 Clash Verge 原版文件、不影响其他应用。需要一次管理员授权，完成后重启一次 Clash Verge 即永久生效。",
        trigger: $("bridge-pill"),
        validate: () => !state.running && !busy,
        onAccept: async () => {
          await runBusy(async () => await call("configure_runtime_bridge", {action: "enable"}));
        }
      });
    } else {
      runBusy(async () => await call("configure_runtime_bridge", {action: "probe"}));
    }
  };
}
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
