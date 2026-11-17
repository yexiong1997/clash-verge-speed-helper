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
class Element {
  constructor(id = '') { this.id=id; this.value=''; this.checked=false; this.disabled=false;
    this.classList={toggle(){},add(){}}; this.dataset={}; this.parentElement=this; this.style={}; this.children=[]; this.listeners={}; }
  replaceChildren() {if(this.id==='group')this.value='';} append() {} add() {} setAttribute() {} removeAttribute() {} focus() {}
  addEventListener(type,handler) {this.listeners[type]=handler;} closest() {return this;}
  getBoundingClientRect() {return {left:0,top:0,bottom:30,width:180,height:100};}
}
const elements = new Map();
const get = id => {if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id);};
const calls = [], confirms = []; let prevented = false;
const scenario = process.argv[2] || 'funnel';
const initial = {profiles:[{uid:'p',name:'Fixture',selected:true,current:true}],
  rows:[{uid:'p',profile:'Fixture',name:'node',selected:true,state:'完成',eligible:true,
    score:90,speed:1000,latency:10,jitter:1,successes:6},
    {uid:'p',profile:'Fixture',name:'other',selected:true,state:'完成',eligible:true,
    score:50,speed:500,latency:20,jitter:1,successes:6}],
  groups:[{name:'Test',now:'A'}],running:false,status:'ready'};
const api = {get_state:async()=>initial,start_funnel:async value=>{calls.push(value);return {ok:true};},
  retest_node:async value=>{calls.push(value);if(scenario==='retest'){initial.groups=[];await vm.runInContext('refreshState()',context);initial.groups=[{name:'Test',now:'A'}];}return {ok:true};},
  copy_node_name:async value=>{calls.push(value);return {ok:true};}};
const context = vm.createContext({document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[],addEventListener(){}},
  window:{pywebview:{api},confirm:message=>{confirms.push(message);return scenario!=='cancel-retest';},addEventListener(){},innerWidth:1200,innerHeight:800},
  Option:class {constructor(text,value){this.text=text;this.value=value;}},setInterval(){},initial,console});
get('parallel').value='4'; get('size').value='3145728'; get('mode').value='fast';
get('topn').value='2'; get('status-filter').value='all';
if (scenario === 'retest' || scenario === 'cancel-retest') {get('group').value='Test'; get('auto').checked=true;}
if (scenario === 'countdown') {initial.schedule_minutes=15; initial.schedule_remaining=125; initial.schedule_active=false;}
const root=process.argv[1];
vm.runInContext(fs.readFileSync(path.join(root,'row_model.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8'),context);
vm.runInContext('state = initial',context);
(async()=>{if(scenario === 'countdown') vm.runInContext('render()',context);
  else if(scenario === 'menu-retest' || scenario === 'menu-copy') {
    const row = new Element('fixture-row'); get('rows').children=[row];
    const event = {target:row,clientX:100,clientY:80,key:'ContextMenu',preventDefault(){prevented=true;}};
    get('rows').listeners[scenario === 'menu-retest' ? 'contextmenu' : 'keydown'](event);
    await get(scenario).onclick();
  } else await get(scenario === 'retest' || scenario === 'cancel-retest' ? 'retest-best' : 'funnel').onclick();
  process.stdout.write(JSON.stringify({calls,confirms,note:get('schedule-note').textContent,group:get('group').value,prevented}));})()
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
        self.assertEqual(len(value["confirms"]), 1)
        self.assertIn("20 MiB", value["confirms"][0])
        self.assertEqual(len(value["calls"]), 1)
        self.assertEqual(value["calls"][0]["top_count"], 2)
        self.assertFalse(value["calls"][0]["auto_switch"])

    def test_retest_best_preserves_original_group_and_auto_switch(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["confirms"]), 1)
        self.assertEqual(value["calls"], [{"uid": "p", "name": "node",
                                           "auto_switch": True, "group": "Test"}])
        self.assertEqual(value["group"], "Test")

    def test_schedule_countdown_is_visible(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "countdown"],
                                capture_output=True, timeout=5, check=True)
        self.assertIn("2:05", json.loads(result.stdout)["note"])

    def test_cancelled_retest_does_not_call_backend(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "cancel-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(len(value["confirms"]), 1)
        self.assertEqual(value["calls"], [])

    def test_context_menu_retests_target_and_requires_confirmation(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "menu-retest"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["prevented"])
        self.assertEqual(len(value["confirms"]), 1)
        self.assertEqual(value["calls"], [{"uid": "p", "name": "node",
                                           "auto_switch": False, "group": ""}])

    def test_context_menu_copies_only_its_target(self):
        result = subprocess.run([NODE, "-e", BRIDGE_DRIVER, str(ROOT / "Windows/ui"), "menu-copy"],
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["prevented"])
        self.assertEqual(value["calls"], [{"uid": "p", "name": "node"}])


if __name__ == "__main__":
    unittest.main()
