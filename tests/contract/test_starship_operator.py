"""Operator client checks use DOM/HTTP doubles; browser/Gateway smoke is separate."""
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from src.gateway import server, starship_chat
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime.starship_mission_control import StarshipMissionService
from src.runtime.starship_sixdof_catalog import SIXDOF_SCENARIOS
from src.runtime.starship_mission_director import SCENARIOS as MANAGED_SCENARIOS

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "src/runtime/assets/starship_operator.html"
NODE = shutil.which("node")

BOOTSTRAP = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
global.window=globalThis;
const elements=new Map();
function element(id='') {return {id,textContent:'',value:'',disabled:false,hidden:false,dataset:{},children:[],
 append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=[...nodes];},
 removeAttribute(name){delete this[name];},set innerHTML(_){throw Error('Unsafe HTML assignment');}};}
global.document={getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},
 createElement:element};
global.sessionStorage={values:new Map(),getItem(key){return this.values.get(key)||null;},setItem(key,value){this.values.set(key,value);}};
const timers=new Map();let timerId=0;
global.setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;};
global.clearTimeout=id=>timers.delete(id);global.setInterval=()=>0;
let respond,queue=[],calls=[];
global.fetch=async (url,options)=>{calls.push({url,options,payload:JSON.parse(options.body)});if(respond)return respond(url,options);if(!queue.length)throw Error('Unexpected request');return {ok:true,json:async()=>queue.shift()};};
const html=fs.readFileSync(process.argv[1],'utf8');
const scenarioOptions=[...html.match(/<select id="scenario">([\s\S]*?)<\/select>/)[1].matchAll(/<option value="([^"]+)"/g)].map(x=>x[1]);
document.getElementById('scenario').value=scenarioOptions[0];
const source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInThisContext(source);
const ui=id=>document.getElementById(id),flush=()=>new Promise(resolve=>setImmediate(resolve));
const own=ui('session').textContent,checksum='a'.repeat(64),now=Date.now()/1000;
function response(status='awaiting_approval',session=own,scenario='sixdof_launch_catch') {
 const plan={id:'plan',sha256:checksum,session_id:session,scenario,expires_at_epoch_s:now+900,simulation:{maximum_wall_time_s:900}};
 const context={session_id:session,plan_id:plan.id,plan_sha256:checksum};
 const grant={session_id:session,plan_id:plan.id,plan_sha256:checksum,expires_at_epoch_s:now+300,consumed_by_run:null};
 const result={status,plan,approval:status==='awaiting_approval'?null:grant,execution:{},mission_completed:false,physical_execution:false};
 if(status==='running')result.execution={subprocess_spawned:true,status:'running'};
 return {routing_source:'starship_scoped_mission_control',starship_context:context,operation_result:result,message:'Saved state'};
}
"""


def javascript(code):
    if NODE is None:
        pytest.skip("Node.js is required for the client contract")
    result = subprocess.run(
        [NODE, "-e", BOOTSTRAP + "\n(async()=>{\n" + code +
         "\n})().catch(error=>{console.error(error);process.exitCode=1;});", str(HTML)],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_full_launch_preserves_all_catalog_choices_and_requires_explicit_approval():
    select = re.search(r'<select id="scenario">([\s\S]*?)</select>', HTML.read_text()).group(1)
    options = re.findall(r'<option value="([^"]+)"', select)
    assert options[0] == "sixdof_launch"
    assert len(options) == len(set(options)) and set(options) == set(SIXDOF_SCENARIOS) | set(MANAGED_SCENARIOS)
    javascript("""
assert.equal(ui('scenario').value,'sixdof_launch');assert.equal(calls.length,0);
assert.equal(ui('approve').disabled,true);assert.equal(ui('run').disabled,true);
ui('run').onclick();await flush();assert.equal(calls.length,0);
queue.push(response('awaiting_approval',own,'sixdof_launch'));ui('plan').onclick();await flush();
assert.equal(calls.length,1);assert.equal(calls[0].payload.action,'plan');
assert.equal(calls[0].payload.scenario,'sixdof_launch');assert.equal(calls[0].payload.starship_context,null);
assert.equal(ui('approve').disabled,false);assert.equal(ui('run').disabled,true);
ui('run').onclick();await flush();assert.equal(calls.length,1);
const v=response('approved',own,'sixdof_launch');v.starship_context.plan_sha256='b'.repeat(64);
assert.equal(MissionOSStarshipOperator.permissions(v.operation_result,v.starship_context,own,now).run,false);
assert.equal(timers.size,0);
""")


def test_client_never_dispatches_without_explicit_buttons():
    javascript("""
assert.equal(calls.length,0);assert.equal(ui('approve').disabled,true);assert.equal(ui('run').disabled,true);
ui('scenario').value='sixdof_launch_catch';queue.push(response());ui('plan').onclick();await flush();
assert.equal(calls.length,1);assert.equal(calls[0].url,'/missionos/starship/operator/actions');
assert.equal(calls[0].payload.action,'plan');assert.equal(calls[0].payload.scenario,'sixdof_launch_catch');
assert.equal(calls[0].payload.starship_context,null);assert.equal(ui('approve').disabled,false);
assert.equal(ui('run').disabled,true);assert.equal(timers.size,0);
queue.push(response('approved'));ui('approve').onclick();await flush();
assert.equal(calls.length,2);assert.equal(calls[1].payload.action,'approve');
assert.deepEqual(calls[1].payload.starship_context,response().starship_context);
assert.equal(ui('run').disabled,false);assert.equal(timers.size,0);
queue.push(response('running'));ui('run').onclick();await flush();
assert.equal(calls.length,3);assert.equal(calls[2].payload.action,'run');
assert.equal(ui('run').disabled,true);assert.equal(ui('plan').disabled,true);
assert.equal([...timers.values()].filter(t=>t.ms===2000).length,1);
ui('stop-poll').onclick();assert.equal(timers.size,0);assert.equal(calls.length,3);
""")


def test_busy_request_and_unknown_run_reply_cannot_be_resent():
    javascript("""
ui('scenario').value='sixdof_launch_catch';queue.push(response());ui('plan').onclick();await flush();
queue.push(response('approved'));ui('approve').onclick();await flush();
let rejectPending;respond=()=>new Promise((_,reject)=>{rejectPending=reject;});
ui('run').onclick();assert.equal(ui('run').disabled,true);assert.equal(ui('approve').disabled,true);
ui('run').onclick();assert.equal(calls.length,3);
rejectPending(Error('Connection lost'));await flush();assert.equal(ui('run').disabled,true);
ui('run').onclick();assert.equal(calls.length,3);assert.equal(ui('refresh').disabled,false);
respond=null;queue.push(response('running'));ui('refresh').onclick();await flush();
assert.equal(calls[3].payload.action,'status');assert.equal(ui('status').textContent,'計算中');
""")


TOWER_JAVASCRIPT = r"""
const run='b'.repeat(32),scope='preapproved_local_simulation_capture_or_divert_v1';
function towerResponse(pending=true,ready=true){
 const value=response('running');value.operation_result.plan.tower_supervision={scope,maximum_observation_age_s:2};
 value.operation_result.execution.run_id=run;value.operation_result.execution.tower_supervision_registered=true;
 const x={session_id:own,plan_id:'plan',plan_sha256:checksum,run_id:run,request_id:'request-1'};
 value.operation_result.tower_pending={context:x,pending,run_active:true,fresh:true,expired:false,decision:null,
  server_wall_time_s:1000,maximum_observation_age_s:2,original_wall_deadline_s:1075,
  request:{context:x,sha256:'c'.repeat(64),scope,allowed_actions:['continue_capture','divert']},
  latest_observation:{context:x,evidence_sha256:'d'.repeat(64),wall_time_s:1000,tower_ready:ready}};
 return value;
}
queue.push(towerResponse());ui('refresh').onclick();await flush();
assert.equal(ui('tower-panel').hidden,false);assert.equal(ui('tower-continue').disabled,true);
queue.push(towerResponse());ui('tower-refresh').onclick();await flush();
"""


def test_tower_ui_explicit_choice_is_bound_to_displayed_request_and_evidence():
    javascript(TOWER_JAVASCRIPT + r"""
assert.equal(ui('tower-continue').disabled,false);assert.equal(ui('tower-divert').disabled,false);
assert.equal(calls.length,2);queue.push(towerResponse(false));ui('tower-divert').onclick();await flush();
assert.equal(calls.length,3);assert.equal(calls[2].payload.action,'tower_resolve');
assert.equal(calls[2].payload.run_id,run);assert.equal(calls[2].payload.request_id,'request-1');
assert.equal(calls[2].payload.request_sha256,'c'.repeat(64));assert.equal(calls[2].payload.observed_evidence_sha256,'d'.repeat(64));
assert.equal(calls[2].payload.choice,'divert');assert.equal(ui('tower-divert').disabled,true);
assert.equal(ui('tower-continue').disabled,true);
""")


def test_lost_tower_response_disables_choice_until_status_lookup_and_never_resends():
    javascript(TOWER_JAVASCRIPT + r"""
respond=async()=>{throw Error('Lost tower reply');};ui('tower-divert').onclick();await flush();
assert.equal(calls.length,3);assert.equal(ui('tower-divert').disabled,true);assert.equal(ui('tower-continue').disabled,true);
ui('tower-divert').onclick();await flush();assert.equal(calls.length,3);
respond=null;queue.push(towerResponse(false));ui('tower-refresh').onclick();await flush();
assert.equal(calls[3].payload.action,'tower_status');assert.equal(ui('tower-divert').disabled,true);
""")


def test_tower_ui_source_expiry_unknown_readiness_and_stale_snapshot_disable_capture():
    javascript(TOWER_JAVASCRIPT + r"""
queue.push(towerResponse(true,null));ui('tower-refresh').onclick();await flush();
assert.equal(ui('tower-continue').disabled,true);assert.equal(ui('tower-divert').disabled,false);
const oldNow=Date.now;Date.now=()=>oldNow()+3000;
const result=MissionOSStarshipOperator.towerPermissions(towerResponse().operation_result,towerResponse().operation_result.tower_pending,{session_id:own,plan_id:'plan',plan_sha256:checksum});
assert.equal(result.capture,false);assert.equal(result.divert,false);Date.now=oldNow;
const invalid=towerResponse();invalid.operation_result.tower_pending.run_active=false;
queue.push(invalid);ui('tower-refresh').onclick();await flush();assert.equal(ui('tower-divert').disabled,true);
""")


def test_busy_tower_request_preserves_one_status_poll_and_marks_ignored_choice_unsent():
    javascript(TOWER_JAVASCRIPT + r"""
let releaseTower;respond=()=>new Promise(resolve=>{releaseTower=()=>resolve({ok:true,json:async()=>towerResponse()});});
ui('tower-refresh').onclick();assert.equal(calls.length,3);
const initialPoll=[...timers.entries()].find(([id,t])=>t.ms===2000);assert.ok(initialPoll);
timers.delete(initialPoll[0]);initialPoll[1].fn();await flush();
assert.equal(calls.length,3);assert.equal([...timers.values()].filter(t=>t.ms===2000).length,1);
ui('tower-divert').onclick();await flush();assert.equal(calls.length,3);
assert.ok(ui('tower-message').textContent.includes('未送信'));
releaseTower();await flush();assert.equal([...timers.values()].filter(t=>t.ms===2000).length,1);
respond=null;queue.push(towerResponse());
const resumed=[...timers.entries()].find(([id,t])=>t.ms===2000);timers.delete(resumed[0]);resumed[1].fn();await flush();
assert.equal(calls.length,4);assert.equal(calls[3].payload.action,'status');
assert.equal([...timers.values()].filter(t=>t.ms===2000).length,1);
ui('stop-poll').onclick();assert.equal([...timers.values()].filter(t=>t.ms===2000).length,0);
""")


def test_status_then_choice_click_never_creates_overlapping_requests_or_resubmits_mutation():
    javascript(TOWER_JAVASCRIPT + r"""
let releaseStatus;respond=()=>new Promise(resolve=>{releaseStatus=()=>resolve({ok:true,json:async()=>towerResponse()});});
ui('refresh').onclick();assert.equal(calls.length,3);assert.equal(calls[2].payload.action,'status');
ui('tower-divert').onclick();ui('tower-continue').onclick();await flush();
assert.equal(calls.length,3);assert.ok(ui('tower-message').textContent.includes('未送信'));
releaseStatus();await flush();assert.equal(calls.length,3);
assert.equal([...timers.values()].filter(t=>t.ms===2000).length,1);
respond=null;queue.push(towerResponse(false));ui('tower-refresh').onclick();await flush();
assert.equal(calls[3].payload.action,'tower_status');assert.equal(ui('tower-divert').disabled,true);
assert.equal(calls.filter(x=>x.payload.action==='tower_resolve').length,0);
""")


def test_status_can_recover_same_session_without_old_plan_context():
    javascript("""
queue.push(response('running'));ui('refresh').onclick();await flush();
assert.equal(calls.length,1);assert.equal(calls[0].payload.action,'status');
assert.equal(calls[0].payload.starship_context,null);assert.equal(ui('run').disabled,true);
assert.equal(ui('status').textContent,'計算中');
""")


def test_lost_plan_reply_then_missing_status_allows_fresh_explicit_plan():
    javascript("""
ui('scenario').value='sixdof_launch_catch';respond=async()=>{throw Error('Connection lost');};
ui('plan').onclick();await flush();assert.equal(ui('plan').disabled,true);
respond=null;queue.push({routing_source:'starship_scoped_mission_control',operation_result:{status:'blocked',reason:'no_matching_starship_plan'}});
ui('refresh').onclick();await flush();assert.equal(ui('plan').disabled,false);
assert.equal(ui('approve').disabled,true);assert.equal(ui('run').disabled,true);
assert.equal(calls.length,2);assert.equal(calls[1].payload.action,'status');
""")


@pytest.mark.parametrize("mutation", [
    "value.starship_context.session_id='other';",
    "value.operation_result.plan.session_id='other';",
    "value.starship_context.plan_sha256='b'.repeat(64);",
    "value.operation_result.plan.id='other';",
])
def test_foreign_or_mismatched_response_does_not_enable_approval(mutation):
    javascript("""
ui('scenario').value='sixdof_launch_catch';const value=response();
""" + mutation + """
queue.push(value);ui('plan').onclick();await flush();
assert.equal(ui('approve').disabled,true);assert.equal(ui('run').disabled,true);
assert.match(ui('error').textContent,/結合/);assert.equal(ui('refresh').disabled,false);
""")


def test_grant_session_hash_expiry_and_consumption_gate_run_button():
    javascript("""
const {permissions}=MissionOSStarshipOperator,v=response('approved'),c=v.starship_context,r=v.operation_result;
assert.equal(permissions(r,c,own,now).run,true);
for(const [key,value]of [['session_id','other'],['plan_sha256','b'.repeat(64)],['consumed_by_run','used'],['expires_at_epoch_s',now]]){
 const changed=structuredClone(r);changed.approval[key]=value;
 assert.equal(permissions(changed,c,own,now).run,false,key);
}
assert.equal(permissions(r,c,'other',now).run,false);
r.plan.expires_at_epoch_s=now;assert.equal(permissions(r,c,own,now).run,false);
""")


def test_verified_failed_objectives_remain_separate_and_only_bound_links_embed():
    javascript(r"""
const value=response('verified'),r=value.operation_result,c=value.starship_context;
r.execution={worker_receipt_verified:true,artifact_sha256:{'report.html':'c'.repeat(64)},
 verification:{passed:true,launch_connected_catch_supported:false,booster_recovery:{handoff_reached:false},
 observed_outcomes:[{termination:'surface_impact',orbit_gate_reached:true,payload_released_count:26}]}};
const valid='/missionos/starship/sessions/'+encodeURIComponent(own)+'/plans/plan/artifacts/report.html';
const {artifactPath}=MissionOSStarshipOperator;
assert.equal(artifactPath('report.html',valid,c,r),valid);
for(const bad of ['https://evil.example/','javascript:alert(1)',valid+'?x=1',valid.replace('plans/plan','plans/other')])assert.equal(artifactPath('report.html',bad,c,r),null);
assert.equal(artifactPath('unknown.html',valid,c,r),null);const bad=structuredClone(r);bad.execution.worker_receipt_verified=false;
assert.equal(artifactPath('report.html',valid,c,bad),null);
value.artifact_links={'report.html':valid,'study.json':'https://evil.example/'};
value.message='<img src=x onerror=alert(1)>';queue.push(value);ui('refresh').onclick();await flush();
assert.equal(ui('message').textContent,value.message);assert.equal(ui('artifacts').children.length,1);
const rows=ui('objectives').children.map(row=>row.children.map(c=>c.textContent).join(' ')).join('\n');
assert.match(rows,/26 基/);assert.match(rows,/打ち上げからの連続支持 未達/);assert.match(rows,/mission_completed=false/);
assert.equal(ui('replay').src,undefined);ui('load-replay').onclick();assert.equal(ui('replay').src,valid);
""")


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    for name, filename in (("TASK_STORE_DB_PATH", "tasks.db"), ("MEMORY_DB_PATH", "memory.db"),
                           ("AUDIT_LOG_PATH", "audit.log")):
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-starship-operator-key")
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "fixture")
    service = StarshipMissionService(tmp_path / "state", planner=lambda text: plan_starship_request(text, "fixture"))
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    yield TestClient(server.create_missionos_gateway().app)
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


@pytest.fixture
def loopback_gateway(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    for name, filename in (("TASK_STORE_DB_PATH", "tasks.db"), ("MEMORY_DB_PATH", "memory.db"),
                           ("AUDIT_LOG_PATH", "audit.log")):
        monkeypatch.setenv(name, str(tmp_path / filename))
    monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_PORT", "18822")
    monkeypatch.setenv("GATEWAY_CORS_ALLOWED_ORIGINS", "")
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "fixture")
    service = StarshipMissionService(tmp_path / "state", planner=lambda text: plan_starship_request(text, "fixture"))
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    monkeypatch.setattr(server, "run_missionos_autonomy_conversation", lambda *a, **k: pytest.fail("Generic conversation route invoked"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    yield TestClient(server.create_missionos_gateway().app, base_url="http://127.0.0.1:18822")
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


def browser_headers():
    return {"Origin": "http://127.0.0.1:18822", "Referer": "http://127.0.0.1:18822/missionos/starship/operator",
            "Sec-Fetch-Site": "same-origin", "X-MissionOS-Operator": "starship-v1"}


def operator_payload(action="plan"):
    result = {"action": action, "session_id": "starship-operator-"+"a"*24, "starship_context": None}
    if action == "plan":
        result.update(scenario="sixdof_launch_catch", request="")
    return result


def test_real_same_origin_console_can_plan_and_approve_without_generic_fallback(loopback_gateway):
    url = "/missionos/starship/operator/actions"
    proposed = loopback_gateway.post(url, headers=browser_headers(), json=operator_payload()).json()
    assert proposed["operation_result"]["status"] == "awaiting_approval"
    context = proposed["starship_context"]
    approved = loopback_gateway.post(url, headers=browser_headers(), json={
        **operator_payload("approve"), "starship_context": context,
    })
    assert approved.status_code == 200
    assert approved.json()["operation_result"]["status"] == "approved"
    assert approved.json()["operation_result"]["execution"] == {}
    assert approved.json()["operation_result"]["physical_execution"] is False
    assert approved.json()["operation_result"]["approval"]["authenticated_operator_identity"] is False


def test_default_full_launch_asset_and_plan_only_boundary(loopback_gateway, monkeypatch):
    monkeypatch.setattr("src.runtime.starship_mission_control.subprocess.Popen",
        lambda *a, **k: pytest.fail("Default selection cannot start a worker"))
    asset = loopback_gateway.get("/missionos/starship/operator")
    assert asset.status_code == 200
    options = re.findall(r'<option value="([^"]+)"',
        re.search(r'<select id="scenario">([\s\S]*?)</select>', asset.text).group(1))
    assert options[0] == "sixdof_launch" and set(options) == set(SIXDOF_SCENARIOS) | set(MANAGED_SCENARIOS)
    assert "Launch + 26 payload releases" in asset.text
    payload = {**operator_payload(), "scenario": options[0]}
    value = loopback_gateway.post("/missionos/starship/operator/actions",
        headers=browser_headers(), json=payload).json()
    result = value["operation_result"]
    assert result["status"] == "awaiting_approval" and result["plan"]["scenario"] == "sixdof_launch"
    assert result["approval"] is None and result["execution"] == {}
    assert result["planner_invocation"]["model_inference_invoked"] is False
    assert result["physical_execution"] is False and result["mission_completed"] is False
    denied = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(),
        json={**operator_payload("run"), "starship_context": value["starship_context"]}).json()
    assert denied["operation_result"]["status"] == "blocked"


@pytest.mark.parametrize("scenario", MANAGED_SCENARIOS)
def test_managed_operator_plan_displays_envelope_without_execution(loopback_gateway, monkeypatch, scenario):
    from src.runtime import starship_return_feasibility as qualification
    # Isolate plan rendering from optional numerical qualification availability.
    monkeypatch.setattr(qualification, "readiness", lambda *a, **k: ({}, "fixture", None))
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")
    monkeypatch.setattr("src.runtime.starship_mission_control.subprocess.Popen",
        lambda *a, **k: pytest.fail("Planning cannot start a worker"))
    response = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(),
        json={**operator_payload(), "scenario": scenario})
    assert response.status_code == 200
    value = response.json()
    assert value["operation_result"]["plan"]["scenario"] == scenario
    assert value["operation_result"]["approval"] is None
    assert "各操作ではなく" in value["message"]


def test_full_launch_selected_focus_cannot_be_changed_to_catch_by_planner_text(loopback_gateway, monkeypatch):
    monkeypatch.setattr("src.runtime.starship_mission_control.subprocess.Popen",
        lambda *a, **k: pytest.fail("Mismatched choice cannot start a worker"))
    value = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(),
        json={**operator_payload(), "scenario": "sixdof_launch", "request": "sixdof_launch_catch"}).json()
    assert value["operation_result"]["status"] == "blocked"
    assert value["operation_result"]["reason"] == "planner_scenario_mismatch"
    assert value["operation_result"]["approval"] is None
    assert "starship_context" not in value


def test_selected_gimbal_with_conflicting_explicit_engine_out_id_is_blocked_without_pending_plan(loopback_gateway, monkeypatch, tmp_path):
    service = StarshipMissionService(tmp_path/"restricted-state", planner=lambda text: plan_starship_request(text, "fixture"))
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    payload = {**operator_payload(), "scenario": "sixdof_gimbal_step",
               "request": "追加で sixdof_engine_out のエンジン故障も含めて"}
    result = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json=payload)
    assert result.status_code == 200
    value = result.json()
    assert value["routing_source"] == "starship_scoped_mission_control"
    assert value["operation_result"]["reason"] == "planner_scenario_mismatch"
    assert value["operation_result"]["approval"] is None
    assert "starship_context" not in value
    assert service.current(payload["session_id"]) is None
    denied = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json={
        **operator_payload("approve"), "starship_context": {"session_id": payload["session_id"],
            "plan_id": "unexpected", "plan_sha256": "a"*64}}).json()
    assert denied["operation_result"]["status"] == "blocked"
    assert service.current(payload["session_id"]) is None


def test_selected_gimbal_still_allows_supplementary_engine_out_keywords_when_proposal_matches(loopback_gateway):
    result = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json={
        **operator_payload(), "scenario": "sixdof_gimbal_step", "request": "engine_out engine failure の説明も添える"})
    assert result.status_code == 200
    value = result.json()
    assert value["operation_result"]["status"] == "awaiting_approval"
    assert value["operation_result"]["plan"]["scenario"] == "sixdof_gimbal_step"
    assert value["operation_result"]["approval"] is None


def test_misbehaving_operator_planner_cannot_override_selected_catalog_scenario(loopback_gateway, monkeypatch, tmp_path):
    proposal = {"proposal": {"scenario": "sixdof_engine_out", "rationale": "Misbehaving fixture.",
                             "uncertainties": ["Contract test only."]},
                "invocation": {"model_inference_invoked": False}}
    service = StarshipMissionService(tmp_path/"misbehaving-state", planner=lambda _: proposal)
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    payload = {**operator_payload(), "scenario": "sixdof_gimbal_step"}
    value = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json=payload).json()
    assert value["operation_result"]["status"] == "blocked"
    assert value["operation_result"]["reason"] == "planner_scenario_mismatch"
    assert "starship_context" not in value
    assert service.current(payload["session_id"]) is None


@pytest.mark.parametrize("changed", [
    {"Origin": "https://untrusted.example"},
    {"Origin": "http://127.0.0.1:18823"},
    {"Origin": "http://localhost:18822"},
    {"Referer": "http://127.0.0.1:18822/other"},
    {"Referer": "http://127.0.0.1:18823/missionos/starship/operator"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": ""},
    {"X-MissionOS-Operator": ""},
    {"Host": "untrusted.example:18822"},
])
def test_console_does_not_accept_untrusted_origin_port_host_or_context(loopback_gateway, changed):
    result = loopback_gateway.post("/missionos/starship/operator/actions", headers={**browser_headers(), **changed},
                                   json=operator_payload())
    assert result.status_code == 403
    assert result.json()["detail"] == "Browser origin is not allowed"


def test_origin_exception_does_not_apply_to_generic_or_other_gateway_routes(loopback_gateway):
    result = loopback_gateway.post("/missionos/autonomy-conversation/run", headers=browser_headers(),
                                   json={"operator_instruction": "execute hardware", "session_id": "other"})
    assert result.status_code == 403
    assert loopback_gateway.get("/missionos/current-milestone", headers=browser_headers()).status_code == 403


def test_remote_client_does_not_receive_loopback_console_exception(loopback_gateway):
    remote = TestClient(loopback_gateway.app, base_url="http://127.0.0.1:18822", client=("203.0.113.4", 19001))
    result = remote.post("/missionos/starship/operator/actions", headers=browser_headers(), json=operator_payload())
    assert result.status_code == 403


@pytest.mark.parametrize("payload", [
    {**operator_payload(), "action": "execute_hardware"},
    {**operator_payload(), "scenario": "hardware_live"},
    {**operator_payload(), "operator_instruction": "execute hardware"},
    {**operator_payload(), "starship_context": {}},
    {**operator_payload(), "session_id": "another-domain"},
    {**operator_payload("run"), "requested_action": "deploy"},
    {"action": "run", "session_id": "starship-operator-"+"a"*24},
])
def test_operator_endpoint_refuses_non_catalog_or_extra_authority_fields(loopback_gateway, payload):
    result = loopback_gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json=payload)
    assert result.status_code == 400
    assert result.json()["detail"] == "Invalid bounded Starship operator action"


def test_console_exception_keeps_configured_gateway_key_required(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit
    monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_PORT", "18822")
    monkeypatch.setenv("GATEWAY_API_KEY", "test-protected-console")
    monkeypatch.setenv("GATEWAY_CORS_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "task.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    try:
        gateway = TestClient(server.create_missionos_gateway().app, base_url="http://127.0.0.1:18822")
        result = gateway.post("/missionos/starship/operator/actions", headers=browser_headers(), json=operator_payload())
        assert result.status_code == 401
        assert result.json()["detail"] == "Unauthorized"
        allowed = gateway.post("/missionos/starship/operator/actions", headers={
            **browser_headers(), "X-API-Key": "test-protected-console",
        }, json=operator_payload("status"))
        assert allowed.status_code == 200
        assert allowed.json()["routing_source"] == "starship_scoped_mission_control"
    finally:
        reset_settings()
        reset_task_store()
        audit._audit_logger = None


def test_static_console_keeps_existing_auth_and_security_headers(gateway):
    assert gateway.get("/missionos/starship/operator").status_code == 401
    result = gateway.get("/missionos/starship/operator", headers={"X-API-Key": "test-starship-operator-key"})
    assert result.status_code == 200
    assert result.text == HTML.read_text()
    assert result.headers["content-type"].startswith("text/html")
    assert result.headers["cache-control"] == "no-store"
    assert "connect-src 'self'" in result.headers["content-security-policy"]
    assert "base-uri 'none'" in result.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
    assert "frame-src 'self'" in result.headers["content-security-policy"]
    assert 'sandbox="allow-scripts"' in result.text
    assert "innerHTML" not in result.text
    assert not re.search(r"(?:src|href)=[\"']https?://", result.text)


def test_real_fixture_planner_and_explicit_http_approval_remain_bound(gateway, monkeypatch):
    headers = {"X-API-Key": "test-starship-operator-key"}
    def turn(text, context=None, session="console-session"):
        payload = {"operator_instruction": text, "session_id": session}
        if context is not None:
            payload["starship_context"] = context
        elif text == "/status":
            # Explicitly retain the Starship route even before this session
            # owns a plan; absent context would permit generic chat fallback.
            payload["starship_context"] = None
        return gateway.post("/missionos/autonomy-conversation/run", headers=headers, json=payload).json()
    monkeypatch.setattr("src.runtime.starship_mission_control.subprocess.Popen", lambda *a, **k: pytest.fail("No worker in this plan/approval check"))
    assert turn("/status")["operation_result"]["reason"] == "no_matching_starship_plan"
    proposed = turn("Starship sixdof_launch_catch")
    context = proposed["starship_context"]
    assert proposed["operation_result"]["status"] == "awaiting_approval"
    assert proposed["operation_result"]["planner_invocation"]["model_inference_invoked"] is False
    assert turn("/run", context)["operation_result"]["status"] == "blocked"
    assert turn("/approve", context, "other-session")["operation_result"]["status"] == "blocked"
    assert turn("/approve", {**context, "plan_sha256": "0"*64})["operation_result"]["status"] == "blocked"
    approved = turn("/approve", context)["operation_result"]
    assert approved["status"] == "approved"
    assert approved["approval"]["scope"] == "local_launch_connected_booster_catch_simulation"
    assert approved["mission_completed"] is False
    assert approved["execution"] == {}
    assert turn("/reject", context)["operation_result"]["status"] == "rejected"


def test_backend_mismatch_is_explained_before_operator_approval(loopback_gateway, monkeypatch):
    from src.runtime import starship_return_feasibility as qualification
    monkeypatch.setenv('MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE', 'fixture')
    monkeypatch.setattr(qualification, 'readiness', lambda *a, **k: (None, 'fixture', 'return_qualification_backend_mismatch'))
    response = loopback_gateway.post('/missionos/starship/operator/actions', headers=browser_headers(),
        json={**operator_payload(), 'scenario': 'sixdof_managed_normal'})
    value = response.json()
    assert value['operation_result']['status'] == 'blocked'
    assert value['operation_result']['reason'] == 'return_qualification_backend_mismatch'
    assert value['operation_result']['approval'] is None
    assert value['operation_result']['execution'] is None
    assert 'spaceflight-qualified' in value['message']


def test_unresolved_return_is_not_rendered_as_mission_success():
    javascript(r'''
const value=response('verified');
value.operation_result.execution={verification:{comparison:{comparison_accepted:true,comparison_scope:'inhibit_and_observe_30s_only',ship_return_qualified:false,managed:{orbit:true,released:0,termination:'return_inhibited_unresolved'}}}};
queue.push(value);ui('refresh').onclick();await flush();
const labels=ui('objectives').children.flatMap(row=>row.children.map(cell=>cell.textContent)).join(' ');
assert.match(labels,/帰還未解決/);
assert.match(labels,/全体成功ではない/);
''')
