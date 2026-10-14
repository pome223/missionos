"""Past failures reach both providers without becoming execution authority."""
from copy import deepcopy
import json
import pytest
from src.intelligence.starship_booster_recovery_evidence import recovery_evidence, model_history, comparison_payload, assess_recovery
from src.intelligence.starship_mission_director import MissionAgent
from src.runtime.starship_mission_director import contract, digest, MODE_ENV
from src.intelligence.space_ops_investigator import JEV_MODEL


def test_reviewed_counts_exclude_offline_partial_and_lost_results():
    e = recovery_evidence()
    assert e['catch']['completed_development_candidates'] == 88
    assert e['catch']['launch_derived_arrival_handoffs_demonstrated'] == 0
    assert e['catch']['launch_derived_supported_catches_demonstrated'] == 0
    assert e['catch']['launch_derived_catch_has_ever_succeeded'] is False
    assert e['catch']['success_rate_estimated'] is False
    assert e['splashdown']['unique_trajectory_count'] == 1
    assert e['splashdown']['nominal_separation_only'] is True
    assert e['splashdown']['perturbation_robustness_qualified'] is False
    assert e['grants_execution_authority'] is False


@pytest.mark.parametrize('choice', ['capture', 'splashdown'])
def test_assessment_can_choose_either_objective_without_dispatch(choice):
    e = recovery_evidence()
    context, identity, _ = comparison_payload(e, 'deepseek-chat')
    class Transport:
        def _invoke(self, provider, payload, receipt, decode):
            assert provider == 'deepseek'
            public = json.loads(payload['messages'][-1]['content'])
            assert public['recovery_history'] == model_history(e)
            answer, _ = decode({'model':'deepseek-chat', 'choices':[{'finish_reason':'stop',
                'message':{'role':'assistant','content':json.dumps({'action':choice,'summary':'Scoped assessment.'})}}]})
            return answer
    r = assess_recovery(Transport(), e, 'deepseek-chat')
    assert r['answer']['action'] == choice
    assert r['context'] == context
    assert r['execution_dispatched'] is False
    assert r['free_text_reasoning_verified'] is False


def test_unrequested_response_fields_are_rejected():
    class Transport:
        def _invoke(self, provider, payload, receipt, decode):
            return decode({'model':'deepseek-chat','choices':[{'finish_reason':'stop',
                'message':{'role':'assistant','content':json.dumps({'action':'splashdown','summary':'Scoped.', 'unapproved_extra':'x'})}}]})
    with pytest.raises(ValueError, match='response'):
        assess_recovery(Transport(), recovery_evidence(), 'deepseek-chat')


@pytest.mark.parametrize("configured_model", ["deepseek-chat", "deepseek-v4-flash"])
def test_existing_director_passes_same_history_to_jev_and_deepseek(monkeypatch, configured_model):
    monkeypatch.setenv(MODE_ENV, 'live')
    monkeypatch.setattr('src.intelligence.starship_mission_director._deepseek_model', lambda:configured_model)
    env = contract('live', splashdown=True)
    row = {'time_s':100.,'phase':'booster_return','released_count':0,'release_acknowledged':False,
        'sequencer_state':'running','fuel_kg':50000.,'return_deadline_s':2000.,'tower_ready':True,
        'operations_notice':'','hold_expires_at_s':None,'numerical_tools':{'orbit_release_feasible':True,
        'retained_payload_present':True,'capture_corridor_certified':False,'mechanism_status':'not_collected'}}
    req = {'schema':'missionos.starship_director_request.v5','request_id':'test:1', 'point':'booster_selection',
        'observation':row,'allowed_actions':['splashdown'],'decision_deadline_s':175.,'envelope_sha256':digest(env)}
    before=deepcopy(req)
    agent=MissionAgent(env)
    states=[]
    def invoke(provider,payload,receipt,decode):
        if provider=='typesafe':
            states.append(payload['state'])
            criteria=payload['questions']['decision']['criteria']
            values={k:float(k=='deep_reasoning') for k in criteria}
            return decode({'model':JEV_MODEL,'answers':{'decision':{'type':'choice','choice':'deep_reasoning',
                'probabilities':values,'confidence':1.}}})[0]
        states.append(json.loads(payload['messages'][-1]['content']))
        assert payload['model'] == ('deepseek-flash' if configured_model == 'deepseek-v4-flash' else configured_model)
        return decode({'model':payload['model'],'choices':[{'finish_reason':'stop',
            'message':{'role':'assistant','content':'{"action":"splashdown"}'}}]})[0]
    monkeypatch.setattr(agent.transport, '_invoke', invoke)
    r=agent.assess(req)
    assert states[0]['recovery_history'] == states[1]['recovery_history'] == model_history()
    assert req==before and r['request_sha256']==digest(req)
    assert states[1]['allowed_actions']==['splashdown']


def test_external_payload_contains_only_authorized_recovery_facts():
    evidence = recovery_evidence()
    _, _, payload = comparison_payload(evidence, 'deepseek-chat')
    wire = json.dumps(payload)
    assert 'source_sha256' not in wire and 'completed_development_candidates' not in wire
    assert 'contact_speed_mps' not in wire and 'remaining_propellant_kg' not in wire
    for source_hash in evidence['source_sha256'].values():
        assert source_hash not in wire
    history = json.loads(payload['messages'][-1]['content'])['recovery_history']
    assert set(history)=={'scope','catch','splashdown'}
    assert history['catch']['launch_derived_supported_catches_demonstrated']==0
