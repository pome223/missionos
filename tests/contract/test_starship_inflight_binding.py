"""Candidate-bound approval HTTP and signed ledger; no flight or model calls."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.gateway.starship_inflight_approval import InflightApprovalService, PREFIX, build_inflight_approval_router
from src.runtime.starship_inflight_approval import InflightApprovalError, InflightApprovalLedger

TOKEN = 'explicit-fixture-operator-token-not-secret'
SCOPE = {'commands': ['select_nominal', 'keep_reserved_return'], 'resource_limits': {'delay_s': 6000.}}
REGISTRY = {'commands': SCOPE['commands']+['select_next_orbit'], 'resource_limits': SCOPE['resource_limits']}
BASE = PREFIX+'/runs/run1/missions/mission1/vehicles/ship'
AUTH = {'Authorization': 'Bearer '+TOKEN}
CANDIDATE = 'c'*64


@pytest.fixture
def item(tmp_path):
    clock = [100.]
    obs = {'state_sha256': 'a'*64, 'simulation_time_s': 10., 'wall_time_s': 100.}
    ledger = InflightApprovalLedger(tmp_path/'ledger.sqlite', signing_key=b'fixture-only-integrity-key-32-bytes',
        operator_token=TOKEN, operator_id='local-test-operator', registry=REGISTRY, clock=lambda: clock[0])
    request = ledger.create(request_id='request1', mission_id='mission1', vehicle_id='ship', revision=2, scope=SCOPE,
        delta={'add_commands': ['select_next_orbit'], 'resource_limits': {}}, observation=obs, simulation_time_s=10.,
        simulation_deadline_s=25., wall_deadline_s=115., fallback='keep_reserved_return', candidate_sha256=CANDIDATE)
    state = {'run_active': True, 'simulation_time_s': 10., 'revision': 2, 'scope': deepcopy(SCOPE),
        'observation': deepcopy(obs), 'pending_request_id': 'request1'}
    service = InflightApprovalService(clock=lambda: clock[0])
    service.register_run(run_id='run1', mission_id='mission1', vehicle_id='ship', ledger=ledger, snapshot=lambda: deepcopy(state))
    app = FastAPI()
    app.include_router(build_inflight_approval_router(service))
    return SimpleNamespace(ledger=ledger, state=state, request=request, client=TestClient(app),
        body={'request_sha256': request['sha256'], 'observed_state_sha256': obs['state_sha256'], 'candidate_sha256': CANDIDATE})


def test_http_display_and_grant_bind_same_candidate(item):
    shown = item.client.get(BASE+'/pending', headers=AUTH).json()
    assert shown['approval_binding'] == 'candidate_bound_scope_amendment'
    assert shown['request']['candidate_sha256'] == CANDIDATE
    result = item.client.post(BASE+'/requests/request1/approve', headers=AUTH, json=item.body)
    assert result.status_code == 200 and result.json()['receipt']['candidate_sha256'] == CANDIDATE
    grant = item.ledger.consume(request_id='request1', mission_id='mission1', vehicle_id='ship', revision=2,
        scope=SCOPE, observation=item.state['observation'], simulation_time_s=10., run_active=True)
    assert grant['candidate_sha256'] == CANDIDATE and grant['approved'] and not grant['executor_command_issued']
    with pytest.raises(InflightApprovalError, match='already_consumed'):
        item.ledger.consume(request_id='request1', mission_id='mission1', vehicle_id='ship', revision=2,
            scope=SCOPE, observation=item.state['observation'], simulation_time_s=10., run_active=True)


@pytest.mark.parametrize('value,status', [(None,400), ('d'*64,409)])
def test_http_cannot_approve_missing_or_different_displayed_candidate(item, value, status):
    body = dict(item.body)
    if value is None:
        body.pop('candidate_sha256')
    else:
        body['candidate_sha256'] = value
    response = item.client.post(BASE+'/requests/request1/approve', headers=AUTH, json=body)
    assert response.status_code == status
    assert item.ledger.status('request1')['receipt'] is None


def test_direct_ledger_resolve_also_requires_candidate(item):
    with pytest.raises(InflightApprovalError, match='displayed_candidate_mismatch'):
        item.ledger.resolve(request_id='request1', request_sha256=item.request['sha256'], observed_state_sha256='a'*64,
            choice='approve', bearer_token=TOKEN, simulation_time_s=10.)
    assert item.ledger.status('request1')['receipt'] is None


def test_candidate_operator_ui_posts_exactly_what_it_displayed():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required for operator client contract')
    from src.gateway import starship_inflight_approval as gateway
    html = Path(gateway.__file__).resolve().parents[1]/'runtime/assets/starship_inflight_operator.html'
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();global.document={getElementById(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:false,disabled:false});return elements.get(id);}};
global.window={addEventListener(){}};global.setTimeout=()=>1;global.clearTimeout=()=>{};
const calls=[],replies=[];global.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>replies.shift()};};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0]);
const ui=id=>document.getElementById(id),flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{ui('run').value='run1';ui('mission').value='mission1';ui('vehicle').value='ship';ui('token').value='fixture-token';
const request={request_id:'req',mission_id:'mission1',vehicle_id:'ship',current_revision:2,observed_state_sha256:'a'.repeat(64),candidate_sha256:'c'.repeat(64),sha256:'b'.repeat(64),simulation_deadline_s:25,wall_deadline_s:115,fallback:'hold',current_scope:{commands:['hold']},requested_scope_delta:{add_commands:['select_next_orbit']}};
replies.push({request,resolvable:true,reason:'awaiting_operator',current_simulation_time_s:10,current_wall_time_s:100});ui('connect').onclick();await flush();
assert.equal(ui('candidate').textContent,'c'.repeat(64));replies.push({receipt:{choice:'approve'}});await ui('approve').onclick();
assert.deepEqual(JSON.parse(calls[1].options.body),{request_sha256:'b'.repeat(64),observed_state_sha256:'a'.repeat(64),candidate_sha256:'c'.repeat(64)});
assert(!calls[1].options.body.includes('fixture-token'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script, str(html)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout+result.stderr
