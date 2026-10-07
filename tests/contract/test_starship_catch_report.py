"""Catch replay labels, recorded-frame coordinates and report claim boundaries."""
import json
from copy import deepcopy
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from src.runtime.starship_booster_catch import simulate_catch
from src.runtime.starship_sixdof_report import build_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def study():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    config = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    config["duration_s"] = .2
    return {"schema": "missionos.starship_sixdof_study.v1", "profile": profile,
            "runs": [simulate_catch(profile, config)], "provenance": {"physical_execution_invoked": False}}


def display(study):
    page = build_report(study)
    return page, json.loads(re.search(r'<script id="evidence" type="application/json">(.*?)</script>', page, re.S).group(1))


def test_terminal_catch_report_preserves_recorded_frames_and_booster_identity(study):
    page, view = display(study)
    assert "終端状態から開始した独立試験" in page
    assert "打ち上げ・帰還からキャッチまでを通した成功ではありません" in page
    assert 'id="booster-catch"' in page
    assert view["runs"][0]["scenario"].endswith(" / BOOSTER")
    source, shown = study["runs"][0]["catch_record"], view["runs"][0]["catch_record"]
    assert shown["initialization"]["launch_connected"] is False
    assert len(shown["frames"]) == len(source["frames"])
    for original, recorded in zip(source["frames"], shown["frames"]):
        assert original["arm_half_span_m"] == recorded["arm_half_span_m"]
        assert original["pins"] == recorded["pins"]
        assert original["q_body_to_eci"] == recorded["q_body_to_eci"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")
def test_production_replay_maps_actual_enu_support_points_without_pose_reset(study, tmp_path):
    _, view = display(study)
    payload = tmp_path/"view.json"
    payload.write_text(json.dumps(view))
    js = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const data=JSON.parse(fs.readFileSync(process.argv[1],'utf8')),elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'0',innerHTML:'',textContent:'',dataset:{}});return elements.get(id);};
element('evidence').textContent=JSON.stringify(data);
global.document={getElementById:element};global.window=globalThis;global.requestAnimationFrame=()=>{};window.addEventListener=()=>{};
let frame;window.createMissionOSStarshipCG=()=>({render:f=>{frame=f;},setCamera:()=>{},getDiagnostics:()=>({})});
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
const expected=data.runs[0].catch_record.frames[0];
assert.equal(frame.bodyId,'booster');assert.deepEqual(frame.q_body_to_eci,expected.q_body_to_eci);
assert.deepEqual(frame.r_eci_m,expected.r_eci_m);assert.equal(frame.catch_geometry.frame_time_s,expected.time_s);
assert.ok(element('scope').textContent.includes('NOT LAUNCH TO CATCH'));assert.equal(element('clock').textContent,'TEST +0.00s');
const multiply=(m,p)=>[0,1,2].map(i=>m[i]*p[0]+m[4+i]*p[1]+m[8+i]*p[2]+m[12+i]);
for(const [i,pin] of expected.pins.entries()){
  const body=pin.position_body_m,com=expected.com_body_m;
  const point=multiply(frame.attitude_matrix_local,[body[0]-com[0],body[2]-com[2],-(body[1]-com[1])]);
  const shown=frame.catch_geometry.pins[i].position_local_m;
  assert.ok(Math.hypot(...point.map((x,j)=>x-shown[j]))<1e-6,`${point} != ${shown}`);
}
element('time').value=String(data.runs[0].samples.at(-1).time_s);element('time').oninput();
assert.equal(frame.catch_geometry.arm_half_span_m,data.runs[0].catch_record.frames.at(-1).arm_half_span_m);
"""
    result = subprocess.run([shutil.which("node"), "-e", js, str(payload), str(ROOT/"src/runtime/assets/starship_sixdof_replay.js")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr


def continuation_display_fixture(study, *, contact=True):
    """Synthetic display-only sequence, not a verified launch/catch trajectory."""
    result = deepcopy(study)
    caught = result["runs"][0]
    start = caught["samples"][0]
    assert start["time_s"] > 136
    stack = deepcopy(start)
    stack.update(time_s=0., phase="stack_ascent", body_id="stack", q_body_to_eci=[1., 0., 0., 0.])
    stack["r_eci_m"][0] += 1000.
    separated = deepcopy(start)
    separated.update(time_s=135., phase="booster_boostback", q_body_to_eci=[0., 1., 0., 0.])
    separated["r_eci_m"][1] += 500.
    before = deepcopy(start)
    before.update(time_s=start["time_s"]-.1, phase="booster_terminal", q_body_to_eci=[0., 0., 1., 0.])
    before["r_eci_m"][2] += 20.
    booster = {"scenario": "booster_return", "samples": [separated, before, deepcopy(start)],
               "events": [{"time_s": 135., "event": "separation_state_inherited"}],
               "outcome": {**caught["outcome"], "termination": "catch_handoff" if contact else "surface_impact",
                           "max_altitude_m": 180_000., "return_site_distance_m": 0. if contact else 1234.}}
    run = {"scenario": "launch", "samples": [stack], "events": [{"time_s": 0., "event": "initial_state"}],
           "outcome": deepcopy(caught["outcome"]), "booster_run": booster,
           "booster_catch_run": caught if contact else None,
           "booster_recovery": {"policy_id": "predictive_return_v1", "separation_state_preserved": True,
               "handoff_reached": contact, "catch_invoked": contact, "launch_connected_catch_supported": False}}
    result["runs"] = [run]
    return result


def test_failed_return_panel_preserves_failure_without_claiming_contact(study):
    page, view = display(continuation_display_fixture(study, contact=False))
    assert 'id="booster-recovery"' in page and 'id="booster-catch"' not in page
    assert '<th>帰還の終了</th><td>surface_impact</td>' in page
    assert '<th>キャッチ進入条件</th><td>未達</td>' in page
    assert '<th>接触機構の実行</th><td>未実行</td>' in page
    assert '<th>連続支持の計算結果</th><td>未達</td>' in page
    assert "1,234.000 m" in page
    assert "catch_record" not in view["runs"][1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required")
def test_launch_timeline_does_not_snap_to_future_catch_and_keeps_launch_clock(study, tmp_path):
    source = continuation_display_fixture(study)
    page, view = display(source)
    assert "帰還の最終状態を変更せず" in page
    assert view["runs"][1]["scenario"].endswith(" / STACK → BOOSTER")
    assert view["runs"][1]["samples"][0] == view["runs"][0]["samples"][0]
    payload = tmp_path/"launch-view.json"
    payload.write_text(json.dumps(view))
    js = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const data=JSON.parse(fs.readFileSync(process.argv[1],'utf8')),elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'0',innerHTML:'',textContent:'',dataset:{}});return elements.get(id);};
element('evidence').textContent=JSON.stringify(data);
global.document={getElementById:element};global.window=globalThis;global.requestAnimationFrame=()=>{};window.addEventListener=()=>{};
let frame;window.createMissionOSStarshipCG=()=>({render:f=>{frame=f;},setCamera:()=>{},getDiagnostics:()=>({})});
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
assert.equal(data.default_run_index,1);assert.equal(element('run').value,'1');
assert.equal(window.sixdofReplayDiagnostics.scenario,data.runs[1].scenario);
element('run').value='1';element('run').onchange();
const run=data.runs[1],start=run.catch_record.frames[0];
assert.equal(run.launch_continuation,true);assert.equal(frame.stacked,true);
assert.ok(element('scope').textContent.includes('RECORDED STATES'));assert.equal(element('clock').textContent,'T+00:00:00');
// These are distinct saved poses before contact, not the first dense catch frame.
for(const expected of run.samples.filter(s=>s.time_s<start.time_s)){
  element('time').value=String(expected.time_s);element('time').oninput();
  assert.deepEqual(frame.r_eci_m,expected.r_eci_m);assert.deepEqual(frame.q_body_to_eci,expected.q_body_to_eci);
  assert.equal(frame.catch_geometry,null);assert.equal(window.sixdofReplayDiagnostics.catchFrameTime_s,null);
  assert.equal(frame.stacked,expected.phase==='stack_ascent');
}
// At handoff the exact dense recorded state becomes authoritative, at the
// inherited mission time. It must not restart an initialized TEST clock.
for(const expected of [start,run.catch_record.frames.at(-1)]){
  element('time').value=String(expected.time_s);element('time').oninput();
  assert.deepEqual(frame.r_eci_m,expected.r_eci_m);assert.deepEqual(frame.q_body_to_eci,expected.q_body_to_eci);
  assert.equal(frame.bodyId,'booster');assert.equal(frame.stacked,false);
  assert.equal(frame.catch_geometry.frame_time_s,expected.time_s);
  assert.equal(frame.catch_geometry.arm_half_span_m,expected.arm_half_span_m);
  assert.ok(element('clock').textContent.startsWith('T+'));assert.ok(!element('clock').textContent.includes('TEST'));
}
"""
    result = subprocess.run([shutil.which("node"), "-e", js, str(payload), str(ROOT/"src/runtime/assets/starship_sixdof_replay.js")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
