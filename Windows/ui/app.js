"use strict";
const $ = id => document.getElementById(id);
let state = {profiles: [], rows: [], groups: [], running: false, status: "准备就绪"};
let active = null, busy = false, sortColumn = "score", sortAscending = false, groupToRestore = null;
let refreshSeq = 0, actionEpoch = 0, refreshInFlight = false;
let pendingConfirm = null;
let lastProfilesSig = null, lastGroupsSig = null;
const rowCache = new Map();
const key = row => JSON.stringify([row.uid, row.name]);
const api = () => window.pywebview.api;
function notify(message, isError = false) { $("status").textContent = message; $("status").classList.toggle("error", isError); }
function visible() { return RowModel.visibleRows(state.rows, state.profiles, {query: $("search").value, status: $("status-filter").value, sort: sortColumn, ascending: sortAscending}); }
function winner() { const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid)); return state.rows.filter(r => profiles.has(r.uid) && r.eligible && r.score != null).sort((a, b) => b.score - a.score)[0]; }
function selectedNodes() { const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid)); return state.rows.filter(r => r.selected && profiles.has(r.uid)); }
function selectionSignature() { return JSON.stringify(selectedNodes().map(key)); }
function groupsSignature() { return JSON.stringify((state.groups || []).map(g => [g.name, g.now])); }
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
  return ok;
}
function profileSelection(uid, selected) { const profiles = new Set(state.profiles.filter(p => p.selected).map(p => p.uid)); selected ? profiles.add(uid) : profiles.delete(uid); const nodes = new Set(state.rows.filter(r => r.selected && profiles.has(r.uid)).map(key)); if (selected) state.rows.filter(r => r.uid === uid).forEach(r => nodes.add(key(r))); return updateSelection([...profiles], nodes); }

function renderProfiles() {
  const host = $("profiles");
  const sig = JSON.stringify([state.running, state.profiles.map(p => [p.uid, p.name, p.selected, p.current])]);
  if (sig !== lastProfilesSig) {
    lastProfilesSig = sig;
    host.replaceChildren();
    for (const p of state.profiles) {
      const button = document.createElement("button");
      button.className = `profile-chip${p.selected ? " selected" : ""}${p.current ? " current" : ""}`;
      button.textContent = p.name;
      button.setAttribute("aria-pressed", String(p.selected));
      button.title = p.uid;
      button.disabled = state.running;
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
  const entry = {tr, row, box: null, cells: [], speedBar: null, values: null};
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

function updateRowEntry(entry, row) {
  entry.row = row;
  const rowKey = key(row);
  entry.tr.classList.toggle("active", active === rowKey);
  entry.box.checked = row.selected;
  entry.box.disabled = state.running;
  entry.box.setAttribute("aria-label", `选择 ${row.name}`);
  const values = [
    row.name,
    row.profile,
    row.state,
    row.score == null ? "—" : row.score.toFixed(1),
    row.speed == null ? "—" : `${(row.speed / 1048576).toFixed(2)} MiB/s`,
    row.latency == null ? "—" : `${row.latency.toFixed(0)} ms`,
    row.jitter == null ? "—" : `${row.jitter.toFixed(0)} ms`,
    row.successes == null ? "—" : `${row.successes}/6`
  ];
  for (let i = 0; i < values.length; i++) {
    const td = entry.cells[i];
    const v = values[i];
    if (!entry.values || entry.values[i] !== v) {
      td.textContent = v;
      td.title = v;
      if (i === 4) entry.speedBar = null;
    }
    if (i === 4) {
      if (row.speed != null) {
        if (!entry.speedBar) {
          const bar = document.createElement("progress");
          bar.max = 25 * 1048576;
          entry.speedBar = bar;
          td.append(bar);
        }
        entry.speedBar.value = Math.min(row.speed, entry.speedBar.max);
        entry.speedBar.setAttribute("aria-label", `下载速度 ${v}`);
      } else if (entry.speedBar) {
        if (typeof entry.speedBar.remove === "function") entry.speedBar.remove();
        entry.speedBar = null;
      }
    }
    if (i === 5) td.classList.toggle("latency-cell", row.latency != null);
  }
  entry.values = values;
}

function renderRows() {
  const host = $("rows");
  const rows = visible();
  $("count").textContent = `${rows.length} 个可见节点 · ${state.rows.filter(r => r.selected).length} 已勾选`;
  $("empty").hidden = rows.length > 0;
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
      updateRowEntry(entry, row);
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
      updateRowEntry(entry, row);
      host.append(entry.tr);
    }
  }
  const best = winner();
  $("best-name").textContent = best?.name || "等待测速";
  $("best-profile").textContent = best?.profile || "选择订阅源与节点后开始";
  $("best-score").textContent = best ? best.score.toFixed(1) : "—";
  $("best-speed").textContent = best ? `${(best.speed / 1048576).toFixed(2)} MiB/s` : "—";
  $("traffic").textContent = `预计流量 ${(state.rows.filter(r => r.selected).length * Number($("size").value) / 1048576).toFixed(1)} MiB`;
}

function renderGroups() {
  const select = $("group");
  const sig = groupsSignature();
  if (sig === lastGroupsSig && !groupToRestore) return;
  lastGroupsSig = sig;
  const old = groupToRestore ?? select.value;
  select.replaceChildren(new Option("请选择策略组", ""));
  for (const g of state.groups) select.add(new Option(`${g.name}（当前 ${g.now}）`, g.name));
  if (state.groups.some(g => g.name === old)) select.value = old;
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
  const blocked = state.running || busy;
  const unknown = Boolean(state.outcome_unknown);
  if (unknown) $("auto").checked = false;
  $("start").disabled = $("dock-start").disabled = blocked;
  $("stop").disabled = $("dock-stop").disabled = !state.running;
  for (const id of ["refresh", "all-profiles", "clear-profiles", "top", "all-nodes", "clear-nodes", "parallel", "size", "mode", "stress", "funnel"]) {
    $(id).disabled = blocked;
  }
  for (const id of ["refresh-groups", "group", "manual", "auto"]) {
    $(id).disabled = blocked || unknown;
  }
  $("retest-best").disabled = blocked || !winner();
  $("switch-best").disabled = blocked || !winner() || unknown;
  $("undo").disabled = blocked || !state.undo || unknown;
  $("continue").disabled = blocked || !state.pending?.ready || unknown;
  const tunNotice = $("tun-notice");
  if (tunNotice) tunNotice.hidden = state.tun_enabled !== true;
  const seconds = state.schedule_remaining;
  const remaining = seconds == null ? "" : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  $("schedule-note").textContent = state.schedule_minutes
    ? `定时：每 ${state.schedule_minutes} 分钟 · ${state.schedule_active ? "本轮结束后重新计时" : `距下次 ${remaining}`}`
    : "定时仅在应用运行时生效";
  notify(state.status || "准备就绪", unknown);
  validatePendingConfirm();
}

async function refreshState(background = false) {
  if (background && refreshInFlight) return;
  const seq = ++refreshSeq;
  const epoch = actionEpoch;
  refreshInFlight = true;
  try {
    const nextState = await api().get_state();
    if (seq !== refreshSeq || (background && epoch !== actionEpoch)) return;
    state = nextState;
    render();
  } catch (_) {
    if (seq === refreshSeq && (!background || epoch === actionEpoch)) notify("无法连接应用核心", true);
  } finally {
    if (seq === refreshSeq) refreshInFlight = false;
  }
}

$("refresh").onclick = () => runBusy(async () => { await call("refresh"); });
$("all-profiles").onclick=()=>updateSelection(state.profiles.map(p=>p.uid),new Set(state.rows.map(key)));
$("clear-profiles").onclick=()=>updateSelection([],new Set());
$("all-nodes").onclick=()=>{const s=new Set(state.rows.filter(r=>r.selected).map(key));visible().forEach(r=>s.add(key(r)));updateSelection(state.profiles.filter(p=>p.selected).map(p=>p.uid),s);};
$("clear-nodes").onclick=()=>{const s=new Set(state.rows.filter(r=>r.selected).map(key));visible().forEach(r=>s.delete(key(r)));updateSelection(state.profiles.filter(p=>p.selected).map(p=>p.uid),s);};
$("top").onclick=()=>{const n=Number($("topn").value);if(!Number.isInteger(n)||n<1||n>2000){notify("前 N 项请输入 1 至 2000 的整数",true);return;}updateSelection(state.profiles.filter(p=>p.selected).map(p=>p.uid),new Set(visible().slice(0,n).map(key)));};
$("search").oninput=renderRows;
$("status-filter").onchange=renderRows;
$("topn").oninput=()=>validatePendingConfirm();
for(const button of document.querySelectorAll("button[data-sort]"))button.onclick=()=>{const column=button.dataset.sort;sortAscending=sortColumn===column?!sortAscending:["name","profile","state","latency","jitter"].includes(column);sortColumn=column;for(const other of document.querySelectorAll("button[data-sort]"))other.parentElement.removeAttribute("aria-sort");button.parentElement.setAttribute("aria-sort",sortAscending?"ascending":"descending");renderRows();};
async function parametersChanged(){validatePendingConfirm();if(state.schedule_minutes)await call("set_schedule",{minutes:0});renderRows();await refreshState();}
for(const id of ["parallel","size","mode","group","auto","interval"])$(id).onchange=parametersChanged;

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
  const n = Number($("topn").value), count = selectedNodes().length;
  if (!Number.isInteger(n) || n < 1 || !count) { notify("请先选择节点并输入有效的前 N 项", true); return; }
  const top = Math.min(n, 10), estimate = count + Math.min(count, top) * 20;
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
    message: `先以每节点 1 MiB 初筛，再对最多 ${top} 个节点以 20 MiB 复测。预计最多约 ${estimate} MiB（不是流量上限）。自动切换${auto ? "已开启" : "未开启"}${auto && group ? `，目标策略组“${group}”` : ""}。`,
    trigger: $("funnel"),
    validate: () => {
      const currentN = Number($("topn").value);
      return Boolean(state.outcome_unknown) === snap.outcomeUnknown &&
        Number.isInteger(currentN) && currentN >= 1 &&
        Math.min(currentN, 10) === snap.top &&
        optionsSignature() === snap.optionsSig &&
        selectionSignature() === snap.selectionSig;
    },
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
      return state.rows.some(r => key(r) === snap.rowKey) &&
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
      }
      await refreshState();
      busy = false;
      groupToRestore = null;
      render();
      if (!result?.ok) notify(result?.error || "复测未启动", true);
    }
  });
}

$("retest-best").onclick = () => retestRow(winner(), $("retest-best"));

function confirmManual(trigger = $("manual")) {
  if (busy || state.running || state.outcome_unknown) return;
  const row = state.rows.find(r => key(r) === active);
  const group = $("group").value;
  if (!row || !group) { notify("请先选中节点和策略组", true); return; }
  const snap = {
    rowKey: key(row),
    group,
    groupsSig: groupsSignature()
  };
  openConfirm({
    title: "确认切换策略组",
    message: `将策略组“${group}”切换到“${row.name}”？`,
    trigger,
    validate: () => !state.outcome_unknown &&
      active === snap.rowKey &&
      $("group").value === snap.group &&
      state.rows.some(r => key(r) === snap.rowKey) &&
      groupsSignature() === snap.groupsSig,
    onAccept: async () => {
      await runBusy(async () => {
        await call("switch_manual", {uid: row.uid, name: row.name, group});
      });
    }
  });
}

$("switch-best").onclick = () => { const best = winner(); if (!best) return; active = key(best); renderRows(); confirmManual($("switch-best")); };
$("start").onclick = () => runBusy(async () => { await call("start", options()); });
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
  active = key(row);
  menuTarget = active;
  menuTrigger = trigger;
  const menu = $("row-menu");
  menu.hidden = false;
  $("menu-switch").disabled = state.running || Boolean(state.outcome_unknown) || !$("group").value || !state.profiles.some(p => p.uid === row.uid && p.current);
  $("menu-retest").disabled = state.running;
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - bounds.height - 8))}px`;
  $("menu-copy").focus();
}
function menuRow() { return state.rows.find(row => key(row) === menuTarget); }
function eventRow(target) { const row = target.closest("tr"), index = Array.from($("rows").children).indexOf(row); return index < 0 ? null : visible()[index]; }
$("rows").addEventListener("contextmenu", event => { const row = eventRow(event.target); if (!row) return; event.preventDefault(); openRowMenu(row, event.clientX, event.clientY, event.target); });
$("rows").addEventListener("keydown", event => { if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return; const row = eventRow(event.target); if (!row) return; event.preventDefault(); const bounds = event.target.getBoundingClientRect(); openRowMenu(row, bounds.left, bounds.bottom, event.target); });
$("menu-switch").onclick = () => { const row = menuRow(); const trigger = menuTrigger; closeRowMenu(); if (!row || busy || state.running || state.outcome_unknown) return; active = key(row); renderRows(); confirmManual(trigger || $("manual")); };
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
window.addEventListener("pywebviewready", () => { refreshState(); setInterval(() => refreshState(true), 1200); });
