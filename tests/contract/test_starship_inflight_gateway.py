"""Real production routes with fixture ledger/state; no provider or physics."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.gateway.starship_inflight_approval import (
    InflightApprovalService, PREFIX, build_inflight_approval_router, same_origin_inflight_operator,
)
from src.runtime.starship_inflight_approval import InflightApprovalError, InflightApprovalLedger

TOKEN = "explicit-fixture-operator-token-not-secret"
SCOPE = {"commands": ["hold"], "resource_limits": {"hold_seconds": 15.}}
REGISTRY = {"commands": ["hold", "resume"], "resource_limits": {"hold_seconds": 30.}}
BASE = PREFIX+"/runs/run1/missions/mission1/vehicles/ship"
AUTH = {"Authorization": "Bearer "+TOKEN}


@pytest.fixture
def item(tmp_path):
    clock = [100.]
    obs = {"state_sha256": "a"*64, "simulation_time_s": 10., "wall_time_s": 100.}
    ledger = InflightApprovalLedger(tmp_path/"ledger.sqlite", signing_key=b"fixture-only-integrity-key-32-bytes",
        operator_token=TOKEN, operator_id="local-test-operator", registry=REGISTRY, clock=lambda: clock[0])
    request = ledger.create(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2, scope=SCOPE,
        delta={"add_commands": ["resume"], "resource_limits": {}}, observation=obs, simulation_time_s=10.,
        simulation_deadline_s=25., wall_deadline_s=115., fallback="hold")
    state = {"run_active": True, "simulation_time_s": 10., "revision": 2, "scope": deepcopy(SCOPE),
             "observation": deepcopy(obs), "pending_request_id": "request1"}
    service = InflightApprovalService(clock=lambda: clock[0])
    service.register_run(run_id="run1", mission_id="mission1", vehicle_id="ship", ledger=ledger, snapshot=lambda: deepcopy(state))
    app = FastAPI()
    app.include_router(build_inflight_approval_router(service))
    return SimpleNamespace(clock=clock, ledger=ledger, request=request, state=state, service=service,
        client=TestClient(app), body={"request_sha256": request["sha256"], "observed_state_sha256": obs["state_sha256"]})


def consume(item):
    return item.ledger.consume(request_id="request1", mission_id="mission1", vehicle_id="ship", revision=2,
        scope=SCOPE, observation=item.state["observation"], simulation_time_s=item.state["simulation_time_s"], run_active=True)


def test_pending_is_authenticated_and_concrete(item):
    assert item.client.get(BASE+"/pending").status_code == 401
    assert item.client.get(BASE+"/pending", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = item.client.get(BASE+"/pending", headers=AUTH)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["request"] == item.request and response.json()["resolvable"]
    assert TOKEN not in response.text


def test_approve_http_then_independent_single_consume(item):
    response = item.client.post(BASE+"/requests/request1/approve", headers=AUTH, json=item.body)
    assert response.status_code == 200 and not response.json()["executor_command_issued"]
    assert item.ledger.status("request1")["consumption"] is None
    assert consume(item)["approved"]
    with pytest.raises(InflightApprovalError, match="already_consumed"):
        consume(item)
    assert item.client.post(BASE+"/requests/request1/approve", headers=AUTH, json=item.body).status_code == 409


def test_reject_http_returns_original_fallback(item):
    assert item.client.post(BASE+"/requests/request1/reject", headers=AUTH, json=item.body).status_code == 200
    assert consume(item)["fallback"] == "hold"


@pytest.mark.parametrize("base", [BASE.replace("mission1", "mission2"), BASE.replace("ship", "booster"), BASE.replace("run1", "run2")])
def test_cross_context_http_cannot_resolve(item, base):
    assert item.client.get(base+"/pending", headers=AUTH).status_code == 404
    assert item.client.post(base+"/requests/request1/approve", headers=AUTH, json=item.body).status_code == 404
    assert item.ledger.status("request1")["receipt"] is None


def test_expired_http_rejects_and_worker_takes_fallback(item):
    item.clock[0] = 115.
    item.state.update(simulation_time_s=25., observation={"state_sha256": "b"*64, "simulation_time_s": 25., "wall_time_s": 115.})
    response = item.client.post(BASE+"/requests/request1/approve", headers=AUTH, json=item.body)
    assert response.status_code == 409 and response.json()["detail"] == "approval_deadline_expired"
    result = consume(item)
    assert result["reason"] == "expired" and result["fallback"] == "hold"


@pytest.mark.parametrize("change", [{"run_active": False}, {"revision": 3}, {"pending_request_id": "request2"},
    {"scope": {"commands": ["hold", "resume"], "resource_limits": {"hold_seconds": 15.}}},
    {"observation": {"state_sha256": "a"*64, "simulation_time_s": 1., "wall_time_s": 100.}}])
def test_current_worker_state_is_rechecked_before_response(item, change):
    item.state.update(change)
    assert item.client.post(BASE+"/requests/request1/approve", headers=AUTH, json=item.body).status_code == 409
    assert item.ledger.status("request1")["receipt"] is None


def test_client_cannot_supply_time_deadline_scope_or_request_creation(item):
    for key in ("simulation_time_s", "wall_deadline_s", "scope", "operator_id"):
        assert item.client.post(BASE+"/requests/request1/approve", headers=AUTH, json={**item.body, key: 1000}).status_code == 400
    assert item.client.post(BASE+"/requests", headers=AUTH, json=item.request).status_code in (404, 405)
    assert item.client.post(BASE+"/requests/request1/create", headers=AUTH, json=item.request).status_code == 404
    assert item.client.post(BASE+"/requests/request1/consume", headers=AUTH, json=item.body).status_code == 404


def test_duplicate_json_keys_rejected(item):
    raw = '{"request_sha256":"'+item.request["sha256"]+'","request_sha256":"'+item.request["sha256"]+'","observed_state_sha256":"'+'a'*64+'"}'
    assert item.client.post(BASE+"/requests/request1/approve", headers={**AUTH, "Content-Type": "application/json"}, content=raw).status_code == 400


def test_request_hash_binding_and_operator_auth(item):
    assert item.client.post(BASE+"/requests/request1/approve", headers=AUTH,
        json={**item.body, "request_sha256": "b"*64}).status_code == 409
    assert item.client.post(BASE+"/requests/request1/approve", json=item.body).status_code == 401


def test_same_origin_exception_is_narrow(item):
    from starlette.requests import Request
    def request(path=BASE+"/requests/request1/approve", origin="http://127.0.0.1:18859", host="127.0.0.1:18859"):
        return Request({"type": "http", "method": "POST", "scheme": "http", "path": path,
            "server": ("127.0.0.1", 18859), "client": ("127.0.0.1", 3), "query_string": b"",
            "headers": [(b"host", host.encode()), (b"origin", origin.encode()),
                (b"referer", b"http://127.0.0.1:18859/missionos/starship/inflight"),
                (b"sec-fetch-site", b"same-origin"), (b"x-missionos-operator", b"starship-inflight-v1"),
                (b"content-type", b"application/json")]})
    assert same_origin_inflight_operator(request(), "127.0.0.1", 18859)
    assert not same_origin_inflight_operator(request(path="/missionos/starship/operator/actions"), "127.0.0.1", 18859)
    assert not same_origin_inflight_operator(request(origin="http://other.test"), "127.0.0.1", 18859)
    assert not same_origin_inflight_operator(request(host="other.test:18859"), "127.0.0.1", 18859)


def test_operator_ui_binds_displayed_request_and_keeps_token_out_of_body():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for operator client contract")
    html = Path(__file__).resolve().parents[2]/"src/runtime/assets/starship_inflight_operator.html"
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
global.document={getElementById(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:false,disabled:false});return elements.get(id);}};
global.window={addEventListener(){}};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
const calls=[],replies=[];
global.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>replies.shift()};};
const code=fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0];vm.runInThisContext(code);
const ui=id=>document.getElementById(id),flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  ui('run').value='run1';ui('mission').value='mission1';ui('vehicle').value='ship';ui('token').value='fixture-token';
  const request={request_id:'req',mission_id:'mission1',vehicle_id:'ship',current_revision:2,observed_state_sha256:'a'.repeat(64),
    sha256:'b'.repeat(64),simulation_deadline_s:25,wall_deadline_s:115,fallback:'hold',current_scope:{commands:['hold']},requested_scope_delta:{add_commands:['resume']}};
  replies.push({request,receipt:null,resolvable:true,reason:'awaiting_operator',current_simulation_time_s:10,current_wall_time_s:100});
  ui('connect').onclick();await flush();
  assert.equal(ui('token').value,'');assert.equal(ui('approve').disabled,false);
  assert(ui('delta').textContent.includes('resume'));assert.equal(ui('state').textContent,'a'.repeat(64));
  replies.push({receipt:{choice:'approve'},executor_command_issued:false});
  await ui('approve').onclick();
  const sent=calls[1];assert(sent.url.endsWith('/requests/req/approve'));
  assert.deepEqual(JSON.parse(sent.options.body),{request_sha256:'b'.repeat(64),observed_state_sha256:'a'.repeat(64)});
  assert.equal(sent.options.headers.Authorization,'Bearer fixture-token');assert(!sent.options.body.includes('fixture-token'));
  assert.equal(ui('approve').disabled,true);ui('disconnect').onclick();assert.equal(ui('pending').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script, str(html)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout+result.stderr
