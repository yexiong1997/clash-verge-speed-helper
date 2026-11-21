"""Exercise Windows row filtering and sorting using synthetic in-memory data."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
DRIVER = """
const model = require(process.argv[1]);
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const x = JSON.parse(input);
  process.stdout.write(JSON.stringify(model.visibleRows(x.rows, x.profiles, x.options).map(r => r.name)));
});
"""
BRIDGE_DRIVER = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
let activeElement = null;
class Element {
  constructor(id = '', tag = 'div') {
    this.id = id; this.tagName = tag.toUpperCase(); this.value = ''; this.checked = false;
    this.disabled = false; this.textContent = ''; this.isConnected = true;
    this.hidden = ['confirm-dialog', 'tun-notice', 'row-menu'].includes(id);
    this.classList = { set: new Set(), toggle(c, f) { f === undefined ? (this.set.has(c) ? this.set.delete(c) : this.set.add(c)) : (f ? this.set.add(c) : this.set.delete(c)); }, add(c) { this.set.add(c); }, contains(c) { return this.set.has(c); } };
    this.dataset = {}; this.attributes = {}; this.parentElement = null; this.style = {}; this.children = []; this.listeners = {};
  }
  replaceChildren(...nodes) {
    for (const c of this.children) c.parentElement = null;
    this.children = [];
    if (this.id === 'group') this.value = '';
    this.append(...nodes);
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n && typeof n === 'object') {
        if (n.parentElement && Array.isArray(n.parentElement.children)) {
          const idx = n.parentElement.children.indexOf(n);
          if (idx >= 0) n.parentElement.children.splice(idx, 1);
        }
        n.parentElement = this;
        this.children.push(n);
      }
    }
  }
  insertBefore(node, ref) {
    if (node.parentElement && Array.isArray(node.parentElement.children)) {
      const oldIdx = node.parentElement.children.indexOf(node);
      if (oldIdx >= 0) node.parentElement.children.splice(oldIdx, 1);
    }
    node.parentElement = this;
    if (!ref) { this.children.push(node); return node; }
    const idx = this.children.indexOf(ref);
    if (idx < 0) this.children.push(node); else this.children.splice(idx, 0, node);
    return node;
  }
  remove() {
    if (this.parentElement && Array.isArray(this.parentElement.children)) {
      const idx = this.parentElement.children.indexOf(this);
      if (idx >= 0) this.parentElement.children.splice(idx, 1);
    }
    this.parentElement = null;
  }
  add(opt) { this.children.push(opt); }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'hidden') this.hidden = true; }
  removeAttribute(k) { delete this.attributes[k]; if (k === 'hidden') this.hidden = false; }
  focus() { if (!this.disabled && this.isConnected !== false) activeElement = this; }
  click() { if (!this.disabled && typeof this.onclick === 'function') return this.onclick(); }
  contains(target) {
    let cur = target;
    while (cur) { if (cur === this) return true; cur = cur.parentElement; }
    return false;
  }
  querySelectorAll(sel) {
    if (sel === 'button:not(:disabled)') return this.children.filter(c => !c.disabled);
    return [];
  }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  closest(tag) {
    let cur = this;
    while (cur) {
      if (cur.tagName && cur.tagName.toLowerCase() === tag.toLowerCase()) return cur;
      cur = cur.parentElement;
    }
    return this;
  }
  getBoundingClientRect() {return {left:0,top:0,bottom:30,width:180,height:100};}
}
const elements = new Map();
const get = id => {if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id);};
get('row-menu').append(
  get('menu-switch'), get('menu-retest'), get('menu-locate'),
  get('menu-only-node'), get('menu-filter-profile'),
  get('menu-copy'), get('menu-copy-summary')
);
const statusBtns = ['all', 'valid', 'testing', 'failed'].map(st => {
  const b = new Element('fp-' + st, 'button');
  b.dataset.status = st;
  if (st === 'all') b.classList.add('active');
  return b;
});
const hwinBtns = ['24h', '7d', '30d', 'peak'].map(w => {
  const b = new Element('hwin-' + w, 'button');
  b.dataset.hwin = w;
  if (w === '7d') b.classList.add('active');
  return b;
});
const docListeners = {};
const doc = {
  getElementById: get,
  createElement: tag => new Element('', tag),
  querySelectorAll: sel => sel === 'button[data-hwin]' ? hwinBtns : (sel === 'button[data-status]' ? statusBtns : []),
  querySelector: () => null,
  addEventListener(type, handler) { (docListeners[type] ||= []).push(handler); },
  dispatch(type, event) { for (const h of (docListeners[type] || [])) h(event); },
  get activeElement() { return activeElement; }
};
const calls = [], dialogs = []; let prevented = false;
const scenario = process.argv[2] || 'funnel';
const initial = {profiles:[{uid:'p',name:'Fixture',selected:true,current:true}],
  rows:[{uid:'p',profile:'Fixture',name:'node',selected:true,state:'完成',eligible:true,
    score:90,speed:1000,latency:10,jitter:1,successes:6},
    {uid:'p',profile:'Fixture',name:'other',selected:true,state:'完成',eligible:true,
    score:50,speed:500,latency:20,jitter:1,successes:6}],
  groups:[{name:'Test',now:'A'}],running:false,status:'ready',tun_enabled:null,outcome_unknown:false};
let gateResolve = null;
const api = {
  get_state: async () => JSON.parse(JSON.stringify(initial)),
  start: async value => { calls.push({fn:'start', value}); if (gateResolve) await new Promise(r => gateResolve = r); return {ok:true}; },
  start_funnel: async value => { calls.push({fn:'start_funnel', value}); if (scenario === 'double-accept') await new Promise(r => { gateResolve = r; }); return {ok:true}; },
  retest_node: async value => {
    calls.push({fn:'retest_node', value});
    if (scenario === 'retest') {
      const saved = initial.groups;
      initial.groups = [];
      await vm.runInContext('refreshState()', context);
      initial.groups = saved;
    }
    return {ok:true};
  },
  switch_manual: async value => { calls.push({fn:'switch_manual', value}); return {ok:true}; },
  undo_switch: async () => { calls.push({fn:'undo_switch'}); return {ok:true}; },
  continue_pending: async value => { calls.push({fn:'continue_pending', value}); return {ok:true}; },
  set_schedule: async value => { calls.push({fn:'set_schedule', value}); return {ok:true}; },
  set_selection: async value => { calls.push({fn:'set_selection', value}); return {ok:true}; },
  copy_node_name: async value => { calls.push({fn:'copy_node_name', value}); return {ok:true}; },
  copy_node_summary: async value => { calls.push({fn:'copy_node_summary', value}); return {ok:true, text:'[Fixture] node'}; },
  probe_active_node: async value => { calls.push({fn:'probe_active_node', value}); return {ok:true}; },
  save_preferences: async value => { calls.push({fn:'save_preferences', value}); return {ok:true}; },
  export_tsv: async () => { calls.push({fn:'export_tsv'}); return {ok:true, cancelled:false}; },
  stop: async () => { calls.push({fn:'stop'}); return {ok:true}; },
  set_patrol: async value => {
    calls.push({fn:'set_patrol', value});
    initial.patrol = {armed: Boolean(value.enabled), quota_bytes: value.quota_bytes, remaining: value.enabled ? 1800 : null, used_bytes_today: 0};
    return {ok:true};
  },
  get_history: async value => {
    calls.push({fn:'get_history', value});
    return {
      ok: true,
      summaries: [
        {uid:'p', profile:'Fixture', name:'node', node_type:'vless', long_term_score:88.5, base_score:90.0,
         health_penalty:1.5, active_failure_streak:0, weighted_median_score:89.0, weighted_p20_score:84.0,
         weighted_median_speed_bps:12582912, weighted_median_latency_ms:42, weighted_median_jitter_ms:3,
         availability_rate:0.95, total_count:10, success_count:9, failure_count:1, is_qualified:true,
         last_tested_at:1700000000, available:true}
      ]
    };
  },
  clear_history: async () => { calls.push({fn:'clear_history'}); return {ok:true}; },
  hide_to_tray: async () => { calls.push({fn:'hide_to_tray'}); return {ok:true}; },
  set_minimize_to_tray: async value => {
    calls.push({fn:'set_minimize_to_tray', value});
    initial.minimize_to_tray = Boolean(value.enabled);
    return {ok:true, minimize_to_tray: initial.minimize_to_tray};
  }
};
const context = vm.createContext({
  document: doc,
  window: {
    pywebview: {api},
    confirm: () => { throw new Error('window.confirm must not be used'); },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    innerWidth: 1200,
    innerHeight: 800
  },
  Option:class {constructor(text,value){this.text=text;this.value=value;}},setInterval(){},setTimeout(){return 1;},clearTimeout(){},initial,console});
get('parallel').value='4'; get('size').value='3145728'; get('mode').value='fast';
get('topn').value='2'; get('status-filter').value='all'; get('interval').value='0';
get('patrol-quota').value='83886080';
if (['retest', 'cancel-retest', 'escape-retest'].includes(scenario)) {get('group').value='Test'; get('auto').checked=true;}
if (scenario === 'countdown') {initial.schedule_minutes=15; initial.schedule_remaining=125; initial.schedule_active=false;}
const root=process.argv[1];
vm.runInContext(fs.readFileSync(path.join(root,'row_model.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8'),context);
vm.runInContext('state = JSON.parse(JSON.stringify(initial)); render();',context);
if (['retest', 'cancel-retest', 'escape-retest'].includes(scenario)) {get('group').value='Test'; get('auto').checked=true;}
function snapDialog() {
  dialogs.push({
    hidden: get('confirm-dialog').hidden,
    title: get('confirm-title').textContent,
    message: get('confirm-message').textContent
  });
}
async function acceptDialog() { snapDialog(); await get('confirm-accept').onclick(); }
function cancelDialog() { snapDialog(); get('confirm-cancel').onclick(); }
(async()=>{
  if (scenario === 'countdown') {
    vm.runInContext('render()',context);
  } else if (scenario === 'funnel') {
    get('funnel').focus();
    get('funnel').onclick();
    await acceptDialog();
  } else if (scenario === 'stress') {
    initial.schedule_minutes = 15;
    vm.runInContext('state.schedule_minutes = 15;', context);
    get('group').value = 'Test'; get('auto').checked = true;
    get('stress').focus();
    get('stress').onclick();
    cancelDialog();
    const preserved = [get('parallel').value, get('size').value, get('mode').value, calls.length];
    get('stress').onclick();
    await acceptDialog();
    process.stdout.write(JSON.stringify({calls, dialogs, preserved, activeId: doc.activeElement?.id}));
    return;
  } else if (scenario === 'retest') {
    get('retest-best').focus();
    get('retest-best').onclick();
    await acceptDialog();
  } else if (scenario === 'cancel-retest') {
    get('retest-best').focus();
    get('retest-best').onclick();
    cancelDialog();
  } else if (scenario === 'escape-retest') {
    get('retest-best').focus();
    get('retest-best').onclick();
    snapDialog();
    const initialFocus = doc.activeElement?.id;
    doc.dispatch('keydown', {key:'Tab', shiftKey:true, preventDefault(){}});
    const afterShiftTab = doc.activeElement?.id;
    doc.dispatch('keydown', {key:'Tab', shiftKey:false, preventDefault(){}});
    const afterTab = doc.activeElement?.id;
    doc.dispatch('keydown', {key:'Escape', preventDefault(){ prevented = true; }});
    process.stdout.write(JSON.stringify({
      calls, dialogs, dialogHidden: get('confirm-dialog').hidden,
      initialFocus, afterShiftTab, afterTab,
      activeId: doc.activeElement?.id || null, prevented
    }));
    return;
  } else if (scenario === 'menu-retest' || scenario === 'menu-copy') {
    const tr = get('rows').children[0];
    const event = {target:tr,clientX:100,clientY:80,key:'ContextMenu',preventDefault(){prevented=true;}};
    get('rows').listeners[scenario === 'menu-retest' ? 'contextmenu' : 'keydown'](event);
    const p = get(scenario).onclick();
    if (scenario === 'menu-retest') await acceptDialog();
    else await p;
  } else if (scenario === 'manual-matrix') {
    get('group').value = 'Test';
    get('switch-best').onclick();
    await acceptDialog();
    const tr1 = get('rows').children[1];
    tr1.ondblclick();
    await acceptDialog();
    tr1.onclick();
    get('manual').onclick();
    await acceptDialog();
    get('rows').listeners.contextmenu({target:tr1, clientX:50, clientY:50, preventDefault(){}});
    get('menu-switch').onclick();
    await acceptDialog();
  } else if (scenario === 'undo-continue-arm') {
    initial.undo = {group:'Test', name:'A'};
    initial.pending = {uid:'p', name:'node', ready:true};
    vm.runInContext('state = JSON.parse(JSON.stringify(initial)); render();', context);
    get('group').value = 'Test';
    get('undo').onclick();
    await acceptDialog();
    get('continue').onclick();
    await acceptDialog();
    get('interval').value = '15';
    get('arm').onclick();
    await acceptDialog();
    const dialogsBeforeDisarm = dialogs.length;
    get('interval').value = '0';
    await get('arm').onclick();
    process.stdout.write(JSON.stringify({calls, dialogs, dialogsBeforeDisarm, dialogHiddenAfterDisarm: get('confirm-dialog').hidden}));
    return;
  } else if (scenario === 'double-accept') {
    get('funnel').onclick();
    snapDialog();
    const p1 = get('confirm-accept').onclick();
    const p2 = get('confirm-accept').onclick();
    gateResolve();
    await Promise.all([p1, p2]);
  } else if (scenario === 'invalidate-matrix') {
    const results = [];
    get('funnel').onclick();
    get('topn').value = '5';
    get('topn').oninput();
    const funnelStayedOpenOnTopn = get('confirm-dialog').hidden === false;
    get('parallel').value = '3';
    await get('parallel').onchange();
    results.push(get('confirm-dialog').hidden);
    await get('confirm-accept').onclick();

    get('stress').onclick();
    get('size').value = '1048576';
    await get('size').onchange();
    results.push(get('confirm-dialog').hidden);
    await get('confirm-accept').onclick();

    get('retest-best').onclick();
    initial.running = true;
    await vm.runInContext('refreshState()', context);
    results.push(get('confirm-dialog').hidden);
    await get('confirm-accept').onclick();

    initial.running = false;
    await vm.runInContext('refreshState()', context);
    get('group').value = 'Test';
    get('switch-best').onclick();
    initial.outcome_unknown = true;
    await vm.runInContext('refreshState()', context);
    results.push(get('confirm-dialog').hidden);
    await get('confirm-accept').onclick();
    process.stdout.write(JSON.stringify({results, calls, funnelStayedOpenOnTopn}));
    return;
  } else if (scenario === 'live-progress') {
    initial.running = true;
    initial.rows = [
      {uid:'p', profile:'Fixture', name:'head-node', selected:true, state:'测速中', eligible:true,
       score:null, speed:null, latency:null, jitter:null, successes:null,
       live:{phase:'head', received_bytes:0, expected_bytes:1048576, bps:0, samples:[]}},
      {uid:'p', profile:'Fixture', name:'dl-node', selected:true, state:'测速中', eligible:true,
       score:null, speed:null, latency:null, jitter:null, successes:null,
       live:{phase:'download', received_bytes:524288, expected_bytes:1048576, bps:10485760, samples:[1000000, 4000000, 10485760]}}
    ];
    await vm.runInContext('refreshState()', context);
    const rowsDom = get('rows').children.map(tr => tr.children.map(td => td.textContent || td.children?.[0]?.children?.[0]?.textContent || ''));
    process.stdout.write(JSON.stringify({
      rowsDom,
      progressActive: get('overall-progress').classList.set.has('active'),
      progressTransform: get('overall-progress-fill').style.transform,
      sparklineHidden: get('best-sparkline').hidden,
      sparklineHasHiddenAttr: 'hidden' in get('best-sparkline').attributes,
      sparklinePath: get('best-sparkline-path').attributes.d || '',
      bestScore: get('best-score').textContent,
      bestSpeed: get('best-speed').textContent
    }));
    return;
  } else if (scenario === 'history-and-patrol') {
    get('view-history').onclick();
    await new Promise(r => setImmediate(r));
    const histToolsHidden = get('history-tools').hidden;
    const liveToolsHidden = get('live-tools').hidden;
    const firstRowTexts = get('rows').children[0]?.children.map(td => td.textContent || td.children?.[0]?.textContent || td.children?.[0]?.children?.[0]?.textContent || '') || [];
    await hwinBtns[3].onclick();
    await new Promise(r => setImmediate(r));
    await get('clear-history').onclick();
    const armedLabel = get('clear-history').textContent;
    await get('clear-history').onclick();
    const resetLabel = get('clear-history').textContent;
    get('patrol-quota').value = '157286400';
    await get('patrol-toggle').onclick();
    process.stdout.write(JSON.stringify({
      calls, histToolsHidden, liveToolsHidden, firstRowTexts, armedLabel, resetLabel,
      patrolNote: get('patrol-note').textContent,
      patrolToggle: get('patrol-toggle').textContent
    }));
    return;
  } else if (scenario === 'tray-controls') {
    initial.minimize_to_tray = false;
    await vm.runInContext('refreshState()', context);
    const syncedFalse = get('minimize-to-tray').checked;
    const idleNote = get('schedule-note').textContent;
    get('minimize-to-tray').checked = true;
    await get('minimize-to-tray').onchange();
    get('hide-tray').onclick();
    process.stdout.write(JSON.stringify({syncedFalse, idleNote, calls, checkedAfter: get('minimize-to-tray').checked}));
    return;
  } else if (scenario === 'tun-notice') {
    const states = [];
    for (const val of [null, false, undefined, true, false]) {
      initial.tun_enabled = val;
      await vm.runInContext('refreshState()', context);
      states.push({hidden: get('tun-notice').hidden, startDisabled: get('start').disabled});
    }
    process.stdout.write(JSON.stringify({states}));
    return;
  } else if (scenario === 'outcome-unknown') {
    get('group').value = 'Test';
    get('auto').checked = true;
    initial.outcome_unknown = true;
    initial.undo = {group:'Test', name:'A'};
    initial.pending = {uid:'p', name:'node', ready:true};
    await vm.runInContext('refreshState()', context);
    const disabled = {};
    for (const id of ['manual', 'switch-best', 'undo', 'continue', 'auto', 'group', 'refresh-groups', 'start', 'stress', 'funnel', 'retest-best']) {
      disabled[id] = get(id).disabled;
    }
    const autoReset = get('auto').checked;
    get('group').value = 'Test';
    await get('start').onclick();
    get('stress').onclick(); await acceptDialog();
    get('funnel').onclick(); await acceptDialog();
    get('retest-best').onclick(); await acceptDialog();
    const tr0 = get('rows').children[0];
    get('rows').listeners.contextmenu({target:tr0, clientX:20, clientY:20, preventDefault(){}});
    const menuSwitchDisabled = get('menu-switch').disabled;
    get('menu-retest').onclick(); await acceptDialog();
    process.stdout.write(JSON.stringify({disabled, menuSwitchDisabled, autoReset, calls, dialogs}));
    return;
  } else if (scenario === 'stale-poll') {
    let resolveSlow;
    let count = 0;
    api.get_state = () => {
      count += 1;
      if (count === 1) {
        return new Promise(r => { resolveSlow = () => r({...JSON.parse(JSON.stringify(initial)), status: 'STALE_POLL'}); });
      }
      return Promise.resolve({...JSON.parse(JSON.stringify(initial)), status: 'FRESH_AFTER_ACTION'});
    };
    const bg = vm.runInContext('refreshState(true)', context);
    await get('start').onclick();
    resolveSlow();
    await bg;
    process.stdout.write(JSON.stringify({status: get('status').textContent, calls}));
    return;
  } else if (scenario === 'incompatible-profile') {
    initial.profiles = [
      {uid:'p', name:'Fixture', selected:true, current:true, compatible:true},
      {uid:'p2', name:'Provider', selected:false, current:false, compatible:false}
    ];
    vm.runInContext('state = JSON.parse(JSON.stringify(initial)); render();', context);
    const chips = get('profiles').children.map(c => ({text: c.textContent, disabled: c.disabled}));
    process.stdout.write(JSON.stringify({chips}));
    return;
  } else if (scenario === 'startup-auto-sync') {
    const emptyInitial = {profiles:[], rows:[], groups:[], suggested_group:null, running:false, status:'ready', tun_enabled:null, outcome_unknown:false};
    let step = 0;
    api.get_state = async () => {
      if (step === 0) return JSON.parse(JSON.stringify(emptyInitial));
      if (step === 1) return {...JSON.parse(JSON.stringify(initial)), groups:[], suggested_group:null};
      return {...JSON.parse(JSON.stringify(initial)), groups:[{name:'GLOBAL',now:'node'},{name:'Main',now:'node'}], suggested_group:'Main'};
    };
    api.refresh = async () => { calls.push({fn:'refresh'}); step = 1; return {ok:true}; };
    api.refresh_groups = async () => { calls.push({fn:'refresh_groups'}); step = 2; return {ok:true}; };
    for (const fn of (context.window.listeners['pywebviewready'] || [])) await fn();
    process.stdout.write(JSON.stringify({calls, group: get('group').value}));
    return;
  } else if (scenario === 'active-node-and-group') {
    initial.groups = [{name:'Test', now:'node'}, {name:'Alt', now:'other'}];
    initial.rows[0].samples = [10.0, 14.5, 18.2];
    await vm.runInContext('refreshState()', context);
    get('group').value = 'Test';
    get('rows').children[1].onclick(); // select 'other'
    const activeBefore = vm.runInContext('active', context);
    const firstIsCurrent = get('rows').children[0].classList.contains('is-current-node');
    const heroReadyUsing = get('best-ready').textContent;
    const heroSubUsing = get('best-profile').textContent;
    const sparkReal = !get('best-sparkline').classList.contains('indicative');
    const sparkTip = get('best-sparkline-title').textContent;
    // Switching group preserves selected row ('other') and moves current badge to 'other'
    get('group').value = 'Alt';
    await get('group').onchange();
    const activeAfter = vm.runInContext('active', context);
    const secondIsCurrentAfter = get('rows').children[1].classList.contains('is-current-node');
    process.stdout.write(JSON.stringify({
      activeBefore, activeAfter, firstIsCurrent, secondIsCurrentAfter,
      heroReadyUsing, heroSubUsing, sparkReal, sparkTip
    }));
    return;
  } else if (scenario === 'prefs-and-lossless-retest') {
    initial.preferences = {parallel: 5, download_bytes: 5242880, mode: 'accurate', interval: 30, patrol_quota: 157286400, top_n: 12};
    await vm.runInContext('refreshState()', context);
    const hydrated = {
      parallel: get('parallel').value, size: get('size').value, mode: get('mode').value,
      interval: get('interval').value, quota: get('patrol-quota').value, topn: get('topn').value
    };
    get('parallel').value = '3'; await get('parallel').onchange();
    get('auto').checked = false; await get('auto').onchange(); // must NOT call save_preferences
    // Retest must NOT overwrite parallel/size/mode dropdowns
    get('group').value = 'Test';
    get('retest-best').onclick();
    await acceptDialog();
    const afterRetest = {parallel: get('parallel').value, size: get('size').value, mode: get('mode').value};
    process.stdout.write(JSON.stringify({hydrated, afterRetest, calls}));
    return;
  } else if (scenario === 'menu-shortcuts-health-export') {
    initial.groups = [{name:'Test', now:'node'}];
    initial.rows.push({uid:'p', profile:'Fixture', name:'queued', selected:true, state:'等待', eligible:true, score:null, speed:null, latency:null, jitter:null, successes:null});
    await vm.runInContext('refreshState()', context);
    get('group').value = 'Test';
    vm.runInContext('render()', context);
    const waitingRowCells = get('rows').children[2].children.map(td => td.textContent);
    const counts = {all: get('fc-all').textContent, valid: get('fc-valid').textContent, testing: get('fc-testing').textContent, failed: get('fc-failed').textContent};
    // Click health pill -> immediate probing text + probe_active_node call
    await get('health-pill').onclick();
    const healthText = get('health-pill').textContent;
    // Export -> ✓ 已复制 feedback
    await get('export').onclick();
    const exportText = get('export').textContent;
    // Live context menu -> copy summary & filter profile
    const firstTr = get('rows').children[0];
    get('rows').listeners.contextmenu({target:{closest:()=>firstTr}, clientX:10, clientY:10, preventDefault(){}});
    const liveLocateHidden = get('menu-locate').hidden;
    await get('menu-copy-summary').onclick();
    // Keyboard shortcut Ctrl+2 -> history view, then menu-locate returns to live view
    doc.dispatch('keydown', {ctrlKey:true, key:'2', target:get('rows'), preventDefault(){}});
    const modeAfterCtrl2 = vm.runInContext('viewMode', context);
    await vm.runInContext('fetchHistory()', context);
    const histTr = get('rows').children[0];
    get('rows').listeners.contextmenu({target:{closest:()=>histTr}, clientX:10, clientY:10, preventDefault(){}});
    const histLocateHidden = get('menu-locate').hidden;
    get('menu-locate').onclick();
    const modeAfterLocate = vm.runInContext('viewMode', context);
    // Space toggles checkbox on active row; Ctrl+Shift+C exports
    doc.dispatch('keydown', {key:' ', target:get('rows'), preventDefault(){}});
    doc.dispatch('keydown', {ctrlKey:true, shiftKey:true, key:'C', target:get('rows'), preventDefault(){}});
    // Empty state when 0 profiles selected -> 0 / 0 & 预计流量 — & quick action button
    initial.profiles[0].selected = false;
    await vm.runInContext('refreshState()', context);
    const zeroCount = get('count').textContent;
    const zeroTraffic = get('traffic').textContent;
    const emptyActions = get('empty-actions').children.map(b => b.textContent);
    process.stdout.write(JSON.stringify({
      waitingRowCells, counts, healthText, exportText, liveLocateHidden,
      modeAfterCtrl2, histLocateHidden, modeAfterLocate, zeroCount, zeroTraffic, emptyActions, calls
    }));
    return;
  }
  process.stdout.write(JSON.stringify({
    calls,
    dialogs,
    dialogHidden: get('confirm-dialog').hidden,
    activeId: doc.activeElement?.id || null,
    note: get('schedule-note').textContent,
    group: get('group').value,
    prevented
  }));
})()
  .catch(error=>{console.error(error);process.exitCode=1;});
"""


@unittest.skipUnless(NODE, "Node.js is required for the Windows UI logic test")
class WindowsRowModelTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"uid": "p1", "profile": "One", "name": "alpha", "state": "完成", "eligible": True,
             "score": 70, "speed": 10, "latency": 90, "jitter": 2, "successes": 6, "selected": True},
            {"uid": "p2", "profile": "Two", "name": "beta", "state": "等待", "eligible": True,
             "score": None, "speed": None, "latency": None, "jitter": None, "successes": None, "selected": True},
            {"uid": "p1", "profile": "One", "name": "gamma", "state": "完成", "eligible": True,
             "score": 90, "speed": 5, "latency": 30, "jitter": 1, "successes": 5, "selected": False},
            {"uid": "p1", "profile": "One", "name": "expiry", "state": "完成", "eligible": False,
             "score": 100, "speed": 15, "latency": 20, "jitter": 0, "successes": 6, "selected": False},
            {"uid": "p1", "profile": "One", "name": "failed", "state": "失败", "eligible": True,
             "score": None, "speed": None, "latency": None, "jitter": None, "successes": None, "selected": False},
        ]
        self.profiles = [{"uid": "p1", "selected": True}, {"uid": "p2", "selected": True}]

    def visible(self, **options):
        data = {"rows": self.rows, "profiles": self.profiles, "options": options}
        result = subprocess.run([NODE, "-e", DRIVER, str(ROOT / "Windows/ui/row_model.js")],
                                input=json.dumps(data).encode(), capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def test_score_default_and_metadata_exclusion(self):
        self.assertEqual(self.visible(), ["gamma", "alpha", "beta", "expiry", "failed"])

    def test_status_filters_and_search_do_not_mutate_selection(self):
        self.assertEqual(self.visible(status="valid"), ["gamma", "alpha"])
        self.assertEqual(self.visible(status="testing"), ["beta"])
        self.assertEqual(self.visible(status="failed"), ["expiry", "failed"])
        self.assertEqual(self.visible(query="  TWO  "), ["beta"])
        self.assertEqual([r["name"] for r in self.rows if r["selected"]], ["alpha", "beta"])

    def test_sorting_keeps_missing_values_last_in_both_directions(self):
        self.assertEqual(self.visible(sort="latency", ascending=True),
                         ["expiry", "gamma", "alpha", "beta", "failed"])
        self.assertEqual(self.visible(sort="latency", ascending=False),
                         ["alpha", "gamma", "expiry", "beta", "failed"])
        self.profiles[1]["selected"] = False
        self.assertNotIn("beta", self.visible())

    def test_funnel_button_requires_confirmation_and_calls_desktop_bridge(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui")],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertFalse(value["dialogs"][0]["hidden"])
        self.assertIn("20 MiB", value["dialogs"][0]["message"])
        self.assertIn("不是流量上限", value["dialogs"][0]["message"])
        self.assertTrue(value["dialogHidden"])
        self.assertEqual(len(value["calls"]), 1)
        self.assertEqual(value["calls"][0]["fn"], "start_funnel")
        self.assertEqual(value["calls"][0]["value"]["top_count"], 10)
        self.assertFalse(value["calls"][0]["value"]["auto_switch"])

    def test_retest_best_preserves_original_group_and_auto_switch(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertEqual(value["calls"], [{"fn": "retest_node", "value": {"uid": "p", "name": "node",
                                           "auto_switch": True, "group": "Test"}}])
        self.assertEqual(value["group"], "Test")

    def test_schedule_countdown_is_visible(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "countdown"],
                                capture_output=True, timeout=5, check=True)
        self.assertIn("2:05", json.loads(result.stdout)["note"])

    def test_cancelled_retest_does_not_call_backend(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "cancel-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertTrue(value["dialogHidden"])
        self.assertEqual(value["activeId"], "retest-best")
        self.assertEqual(value["calls"], [])

    def test_escape_closes_confirm_restores_focus_and_skips_bridge(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "escape-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["prevented"])
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertTrue(value["dialogHidden"])
        self.assertEqual((value["initialFocus"], value["afterShiftTab"], value["afterTab"]),
                         ("confirm-cancel", "confirm-accept", "confirm-cancel"))
        self.assertEqual(value["activeId"], "retest-best")
        self.assertEqual(value["calls"], [])

    def test_context_menu_retests_target_and_requires_confirmation(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "menu-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["prevented"])
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertEqual(value["calls"], [{"fn": "retest_node", "value": {"uid": "p", "name": "node",
                                           "auto_switch": False, "group": ""}}])

    def test_context_menu_copies_only_its_target(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "menu-copy"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["prevented"])
        self.assertEqual(value["calls"], [{"fn": "copy_node_name", "value": {"uid": "p", "name": "node"}}])

    def test_stress_requires_confirmation_preserves_params_on_cancel_and_runs_20m_on_accept(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "stress"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["preserved"], ["4", "3145728", "fast", 0])
        self.assertEqual(len(value["dialogs"]), 2)
        self.assertIn("20 MiB", value["dialogs"][0]["message"])
        self.assertIn("Test", value["dialogs"][0]["message"])
        self.assertEqual(value["calls"], [
            {"fn": "set_schedule", "value": {"minutes": 0}},
            {"fn": "start", "value": {"parallel": 2, "download_bytes": 20971520, "mode": "accurate", "auto_switch": True, "group": "Test"}},
        ])

    def test_all_manual_switch_entrypoints_require_confirmation(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "manual-matrix"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["dialogs"]), 4)
        self.assertEqual([c["value"]["name"] for c in value["calls"]], ["node", "other", "other", "other"])
        self.assertTrue(all(c["fn"] == "switch_manual" and c["value"]["group"] == "Test" for c in value["calls"]))

    def test_undo_continue_and_schedule_arm_confirmation_rules(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "undo-continue-arm"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["dialogsBeforeDisarm"], 3)
        self.assertEqual(len(value["dialogs"]), 3)
        self.assertTrue(value["dialogHiddenAfterDisarm"])
        self.assertEqual([c["fn"] for c in value["calls"]], ["undo_switch", "continue_pending", "set_schedule", "set_schedule"])
        self.assertEqual(value["calls"][2]["value"]["minutes"], 15)
        self.assertEqual(value["calls"][3]["value"]["minutes"], 0)

    def test_double_clicking_confirm_accept_submits_only_once(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "double-accept"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["dialogs"]), 1)
        self.assertEqual(len(value["calls"]), 1)

    def test_parameter_or_state_changes_invalidate_open_confirmation(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "invalidate-matrix"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["funnelStayedOpenOnTopn"])
        self.assertEqual(value["results"], [True, True, True, True])
        self.assertEqual([c for c in value["calls"] if c["fn"] != "save_preferences"], [])

    def test_tun_notice_renders_strictly_for_boolean_true_without_blocking(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "tun-notice"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual([s["hidden"] for s in value["states"]], [True, True, True, False, True])
        self.assertTrue(all(not s["startDisabled"] for s in value["states"]))

    def test_outcome_unknown_disables_writes_and_forces_pure_speedtest_params(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "outcome-unknown"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        for locked_id in ("manual", "switch-best", "undo", "continue", "auto", "group", "refresh-groups"):
            self.assertTrue(value["disabled"][locked_id], locked_id)
        for open_id in ("start", "stress", "funnel", "retest-best"):
            self.assertFalse(value["disabled"][open_id], open_id)
        self.assertTrue(value["menuSwitchDisabled"])
        self.assertFalse(value["autoReset"])
        self.assertEqual(len(value["calls"]), 5)
        for call_entry in value["calls"]:
            self.assertFalse(call_entry["value"]["auto_switch"], call_entry)
            self.assertEqual(call_entry["value"]["group"], "", call_entry)

    def test_stale_background_poll_does_not_overwrite_newer_action_state(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "stale-poll"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "FRESH_AFTER_ACTION")
        self.assertEqual(len(value["calls"]), 1)

    def test_incompatible_profile_chip_is_disabled_with_label(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "incompatible-profile"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["chips"], [
            {"text": "Fixture", "disabled": False},
            {"text": "Provider（不支持）", "disabled": True},
        ])

    def test_startup_auto_loads_subscriptions_and_suggests_group_and_defaults_auto_checked(self):
        html = (ROOT / "Windows/ui/index.html").read_text(encoding="utf-8")
        self.assertIn('id="auto" checked', html)
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "startup-auto-sync"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual([c["fn"] for c in value["calls"]], ["refresh", "refresh_groups"])
        self.assertEqual(value["group"], "Main")

    def test_freeze_order_and_stage1_score_fallback_ranking(self):
        rows = [
            {"uid": "p1", "profile": "One", "name": "first", "state": "测速中", "eligible": True,
             "score": None, "stage1_score": 60.0, "speed": None, "latency": None, "jitter": None, "successes": None},
            {"uid": "p1", "profile": "One", "name": "second", "state": "完成", "eligible": True,
             "score": 95.0, "stage1_score": None, "speed": 100, "latency": 20, "jitter": 1, "successes": 6},
            {"uid": "p1", "profile": "One", "name": "s1_high", "state": "初筛 88.2", "eligible": True,
             "score": None, "stage1_score": 88.2, "speed": None, "latency": None, "jitter": None, "successes": None},
        ]
        profiles = [{"uid": "p1", "selected": True}]
        frozen_data = {"rows": rows, "profiles": profiles, "options": {"freezeOrder": True}}
        frozen = json.loads(subprocess.run(
            [NODE, "-e", DRIVER, str(ROOT / "Windows/ui/row_model.js")],
            input=json.dumps(frozen_data).encode(), capture_output=True, timeout=5, check=True,
        ).stdout)
        self.assertEqual(frozen, ["first", "second", "s1_high"])

        ranked_data = {"rows": rows, "profiles": profiles, "options": {"freezeOrder": False}}
        ranked = json.loads(subprocess.run(
            [NODE, "-e", DRIVER, str(ROOT / "Windows/ui/row_model.js")],
            input=json.dumps(ranked_data).encode(), capture_output=True, timeout=5, check=True,
        ).stdout)
        self.assertEqual(ranked, ["second", "s1_high", "first"])

    def test_history_row_model_filtering_and_sorting(self):
        script = """
const model = require(process.argv[1]);
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const x = JSON.parse(input);
  process.stdout.write(JSON.stringify(model.historyRows(x.summaries, x.profiles, x.options).map(r => r.name)));
});
"""
        summaries = [
            {"uid": "p1", "profile": "SubA", "name": "alpha", "node_type": "vless", "long_term_score": 82.0,
             "weighted_median_speed_bps": 900, "weighted_median_latency_ms": 55, "availability_rate": 0.9,
             "total_count": 5, "success_count": 5},
            {"uid": "p2", "profile": "SubB", "name": "beta", "node_type": "ss", "long_term_score": 91.0,
             "weighted_median_speed_bps": 1500, "weighted_median_latency_ms": 20, "availability_rate": 1.0,
             "total_count": 4, "success_count": 4},
            {"uid": "p1", "profile": "SubA", "name": "dead", "node_type": "trojan", "long_term_score": 0.0,
             "weighted_median_speed_bps": 0, "weighted_median_latency_ms": 0, "availability_rate": 0.0,
             "total_count": 3, "success_count": 0},
        ]
        profiles = [{"uid": "p1", "selected": True}, {"uid": "p2", "selected": False}]
        sel_only = json.loads(subprocess.run(
            [NODE, "-e", script, str(ROOT / "Windows/ui/row_model.js")],
            input=json.dumps({"summaries": summaries, "profiles": profiles, "options": {"selectedOnly": True}}).encode(),
            capture_output=True, timeout=5, check=True,
        ).stdout)
        self.assertEqual(sel_only, ["alpha", "dead"])

        all_by_lat = json.loads(subprocess.run(
            [NODE, "-e", script, str(ROOT / "Windows/ui/row_model.js")],
            input=json.dumps({"summaries": summaries, "profiles": profiles,
                              "options": {"selectedOnly": False, "sort": "weighted_median_latency_ms", "ascending": True}}).encode(),
            capture_output=True, timeout=5, check=True,
        ).stdout)
        self.assertEqual(all_by_lat, ["beta", "alpha", "dead"])

    def test_live_progress_and_sparkline_rendering(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "live-progress"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["rowsDom"][0][3:6], ["探测延迟", "探测中", "探测延迟…"])
        self.assertEqual(value["rowsDom"][1][3:6], ["下载中", "下载 50%", "10.00 MiB/s"])
        self.assertTrue(value["progressActive"])
        self.assertIn("scaleX(", value["progressTransform"])
        self.assertFalse(value["sparklineHidden"])
        self.assertFalse(value["sparklineHasHiddenAttr"])
        self.assertTrue(value["sparklinePath"].startswith("M0.0 "))
        self.assertEqual(value["bestScore"], "50%")
        self.assertEqual(value["bestSpeed"], "10.00 MiB/s")

    def test_history_view_clear_confirm_and_patrol_toggle(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "history-and-patrol"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertFalse(value["histToolsHidden"])
        self.assertTrue(value["liveToolsHidden"])
        self.assertEqual(value["firstRowTexts"][0:3], ["#1", "扣分", "node"])
        self.assertEqual(value["armedLabel"], "确认清空?")
        self.assertEqual(value["resetLabel"], "清空记录")
        self.assertEqual(value["patrolToggle"], "停止巡检")
        self.assertIn("150 MiB", value["patrolNote"])
        self.assertEqual(value["calls"], [
            {"fn": "get_history", "value": {"window": "7d"}},
            {"fn": "get_history", "value": {"window": "peak"}},
            {"fn": "clear_history"},
            {"fn": "set_patrol", "value": {"enabled": True, "quota_bytes": 157286400}},
        ])

    def test_tray_controls_sync_and_schedule_copy(self):
        html = (ROOT / "Windows/ui/index.html").read_text(encoding="utf-8")
        self.assertIn('id="minimize-to-tray" checked', html)
        self.assertIn('id="hide-tray"', html)
        self.assertIn("支持托盘后台运行，退出应用后停止", html)
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "tray-controls"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertFalse(value["syncedFalse"])
        self.assertEqual(value["idleNote"], "支持托盘后台运行，退出应用后停止")
        self.assertTrue(value["checkedAfter"])
        self.assertEqual(value["calls"], [
            {"fn": "set_minimize_to_tray", "value": {"enabled": True}},
            {"fn": "hide_to_tray"},
        ])

    def test_active_node_highlighting_real_sparkline_and_group_switch_preserves_selection(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "active-node-and-group"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["firstIsCurrent"])
        self.assertEqual(value["heroReadyUsing"], "● 当前使用中")
        self.assertIn("· 使用中", value["heroSubUsing"])
        self.assertTrue(value["sparkReal"])
        self.assertIn("真实下载吞吐采样", value["sparkTip"])
        self.assertEqual(value["activeBefore"], value["activeAfter"])
        self.assertTrue(value["secondIsCurrentAfter"])

    def test_preferences_hydration_safe_persistence_and_lossless_retest_ui(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "prefs-and-lossless-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["hydrated"], {
            "parallel": "5", "size": "5242880", "mode": "accurate",
            "interval": "30", "quota": "157286400", "topn": "12",
        })
        self.assertEqual(value["afterRetest"], {"parallel": "3", "size": "5242880", "mode": "accurate"})
        pref_calls = [c for c in value["calls"] if c["fn"] == "save_preferences"]
        self.assertEqual(pref_calls, [{"fn": "save_preferences", "value": {"parallel": 3}}])
        self.assertFalse(any("auto_switch" in c.get("value", {}) for c in pref_calls))

    def test_dynamic_menu_shortcuts_waiting_copy_bucket_counts_health_click_and_empty_state(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "menu-shortcuts-health-export"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["waitingRowCells"][3:6], ["等待", "队列中", "排队等待"])
        self.assertEqual(value["counts"], {"all": "3", "valid": "2", "testing": "1", "failed": "0"})
        self.assertEqual(value["healthText"], "● 探测中…")
        self.assertEqual(value["exportText"], "✓ 已复制")
        self.assertTrue(value["liveLocateHidden"])
        self.assertEqual(value["modeAfterCtrl2"], "history")
        self.assertFalse(value["histLocateHidden"])
        self.assertEqual(value["modeAfterLocate"], "live")
        self.assertEqual(value["zeroCount"], "0 / 0")
        self.assertEqual(value["zeroTraffic"], "预计流量 —")
        self.assertIn("勾选当前订阅", value["emptyActions"])
        self.assertIn("全选全部订阅", value["emptyActions"])
        fns = [c["fn"] for c in value["calls"]]
        self.assertIn("probe_active_node", fns)
        self.assertIn("copy_node_summary", fns)
        self.assertEqual(fns.count("export_tsv"), 2)
        self.assertIn("set_selection", fns)


if __name__ == "__main__":
    unittest.main()
