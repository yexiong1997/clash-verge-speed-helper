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
    this.classList = { set: new Set(), toggle(c, f) { f === undefined ? (this.set.has(c) ? this.set.delete(c) : this.set.add(c)) : (f ? this.set.add(c) : this.set.delete(c)); }, add(c) { this.set.add(c); } };
    this.dataset = {}; this.parentElement = null; this.style = {}; this.children = []; this.listeners = {};
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
  setAttribute() {}
  removeAttribute() {}
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
get('row-menu').append(get('menu-switch'), get('menu-retest'), get('menu-copy'));
const docListeners = {};
const doc = {
  getElementById: get,
  createElement: tag => new Element('', tag),
  querySelectorAll: () => [],
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
  copy_node_name: async value => { calls.push({fn:'copy_node_name', value}); return {ok:true}; }
};
const context = vm.createContext({
  document: doc,
  window: {
    pywebview: {api},
    confirm: () => { throw new Error('window.confirm must not be used'); },
    addEventListener() {},
    innerWidth: 1200,
    innerHeight: 800
  },
  Option:class {constructor(text,value){this.text=text;this.value=value;}},setInterval(){},initial,console});
get('parallel').value='4'; get('size').value='3145728'; get('mode').value='fast';
get('topn').value='2'; get('status-filter').value='all'; get('interval').value='0';
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
    process.stdout.write(JSON.stringify({results, calls}));
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
        self.assertEqual(value["calls"][0]["value"]["top_count"], 2)
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
        self.assertEqual(value["results"], [True, True, True, True])
        self.assertEqual(value["calls"], [])

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


if __name__ == "__main__":
    unittest.main()
