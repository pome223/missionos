"""Normal permission boundary with scripted live owners; no flight or API."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from src.runtime import starship_qualified_return as gate
from src.runtime import starship_final_qualification as old
from src.runtime import starship_final_shared as shared
from scripts import starship_final_qualification_chunks as chunks
from tests.contract import test_starship_final_handoff as owner
from tests.contract.test_starship_final_qualification_chunks import report


@pytest.fixture
def setup(monkeypatch):
    # Production actor opens at its actual origin without taking a physics step;
    # its due state is an explicitly scripted fixture, never flight evidence.
    owner.no_physics_or_provider.__wrapped__(monkeypatch)
    b = owner.bound_fixture('next_orbit')
    execution = owner.actor(b)
    state, vehicle, snapshot = owner.due_owner(execution)
    handoff = execution.handoff_actual_return(b.policy, dispatch=owner.dispatch_proof(b, snapshot))
    h, p = gate._actual(handoff)
    backend = {'python':'fixture','platform':'fixture','numpy':'fixture','scipy':'fixture',
        'executable_sha256':'b'*64,'machine':'fixture'}
    plan = {'plan_sha256':b.policy.plan_sha256,'source_sha256':b.source,'configuration':{'fixture':True},'case_id':'N',
        'composition':{'fault_selection':{'selected_family':'normal'}}}
    identity = old.make_identity(source_sha256=b.source,
        backend={k:v for k,v in backend.items() if k!='machine'},
        profile_sha256=h['snapshot']['binding']['profile_sha256'],
        integration_policy_sha256=old.digest(plan['configuration']))
    domain = {'ascent_condition':'O0','retained_count':13,'released_count':13,
        'return_candidate_id':'next_orbit','fault_family':'nominal','plant_capability_factor':1.,
        'sensor_gain':1.,'controller_capability_factor':1.}
    cells=[{'cell_id':'fixture','domain':domain}]
    raw=old.canonical_bytes({'point':p})
    cert=chunks.build_certificate(expected_identity=identity, required_cells=cells,
        records_raw={'fixture':raw},verify_record=report)
    cert.update(verification_registration_sha256='a'*64,verification_source_commit='c'*40,
        verification_source_sha256={'scripts/starship_final_qualification_chunks.py':'d'*64},
        verification_backend=backend)
    cert_raw=old.canonical_bytes(cert)
    # Only synthetic environment/source facts are injected. Certificate/raw
    # reconstruction, live owner checks and normal permission entry are real.
    monkeypatch.setattr(shared,'environment',lambda:backend)
    monkeypatch.setattr(shared,'check_manifest',lambda value: None)
    from src.runtime import starship_final_plan
    monkeypatch.setattr(starship_final_plan,'verification_inputs',lambda value:value)
    verified=gate.VerifiedReturnCertificate.prepare(cert_raw,expected_sha256=sha256(cert_raw).hexdigest(),
        expected_identity=identity,required_cells=cells,records_raw={'fixture':raw},verify_record=report,
        expected_adapter_source={'src/runtime/starship_qualified_return.py':'e'*64})
    return verified, handoff, p, identity, domain, state, plan


def entry(s, callback):
    return gate.NormalReturnPermissionEntry(s[0],continuation_domain=s[4],current_identity=s[3],permission_entry=callback)


def test_actual_owner_reaches_existing_permission_only_after_match_and_once(setup):
    calls=[]
    def normal(h, *,qualification_receipt):
        assert not h.consumed
        assert qualification_receipt['point_domain_evidence_matched']
        assert qualification_receipt['dispatch_authorized'] is False
        calls.append(h)
        # A trusted existing owner still owns human approval and dispatch.
        return {'permission':'existing_host_fixture_result_not_a_certificate_grant'}
    e=entry(setup,normal)
    assert e.enter(setup[1],current_plan=setup[6])['permission'].startswith('existing_host')
    assert calls == [setup[1]] and not setup[1].consumed
    with pytest.raises(ValueError,match='attempt_already_consumed'):
        e.enter(setup[1],current_plan=setup[6])
    assert len(calls)==1


@pytest.mark.parametrize('bad',['saved_json','consumed','changed_object','changed_history','changed_inventory',
    'changed_candidate','wrong_identity','changed_plan','changed_configuration','fault_domain','fault_plan'])
def test_closed_gate_never_calls_existing_permission_on_mismatch(setup,bad):
    s=list(setup); h=s[1]
    if bad=='saved_json':s[1]=h.record()
    elif bad=='consumed':h.consume()
    elif bad=='changed_object':
        from dataclasses import replace
        h._state=replace(h._state,propellant_kg=h._state.propellant_kg-.01)
    elif bad in ('changed_history','changed_inventory','changed_candidate'):
        # Deliberate fault injection into the owner's private record.
        record=h.record()
        if bad=='changed_history':record['coast_history'][0]['state']['engine_states'][0]['throttle']+=.001
        elif bad=='changed_inventory':record['inventory']['release_receipts'][0]['tampered']=True
        else:record['snapshot']['binding']['candidate_id']='nominal'
        h._record_json=old.canonical_bytes(record)
    elif bad=='wrong_identity':s[3]={**s[3],'profile_sha256':'9'*64}
    elif bad=='changed_plan':s[6]={**s[6],'plan_sha256':'8'*64}
    elif bad=='changed_configuration':s[6]={**s[6],'configuration':{'fixture':False}}
    elif bad=='fault_plan':s[6]={**s[6],'case_id':'T'}
    else:s[4]={**s[4],'fault_family':'return_propulsion_80','plant_capability_factor':.8,
        'controller_capability_factor':.8000296165136251}
    calls=[];e=entry(s,lambda *a,**k:calls.append(a))
    with pytest.raises(ValueError):e.enter(s[1],current_plan=s[6])
    assert not calls
    with pytest.raises(ValueError,match='already_consumed'):e.enter(s[1],current_plan=s[6])


def test_existing_authority_refusal_is_not_replaced_or_retried(setup):
    def deny(*a,**k):raise PermissionError('human_approval_missing')
    e=entry(setup,deny)
    with pytest.raises(PermissionError,match='human_approval_missing'):
        e.enter(setup[1],current_plan=setup[6])
    assert not setup[1].consumed
    with pytest.raises(ValueError,match='already_consumed'):e.enter(setup[1],current_plan=setup[6])


def test_certificate_cannot_be_instantiated_from_a_passed_flag():
    with pytest.raises(ValueError,match='independently_prepared'):
        gate.VerifiedReturnCertificate(core={'qualification_complete':True})


def test_prepared_evidence_rechecks_current_source_and_backend(setup,monkeypatch):
    v,h,p,i,d,_,_=setup
    monkeypatch.setattr(shared,'environment',lambda:{})
    with pytest.raises(ValueError,match='current_backend'):
        v.match_preimages(point_data=p,domain=d,identity=i)


def test_registered_capacity_failure_is_preserved_and_cannot_retry(tmp_path,monkeypatch):
    from scripts import check_starship_current_admission as cli
    from types import SimpleNamespace
    p={'registration_sha256':'a'*64,'bounds':cli.BOUNDS}
    monkeypatch.setattr(cli,'load',lambda path:(p,{'cells':[{'cell_id':'never_started'}]}))
    monkeypatch.setattr(cli.shutil,'disk_usage',lambda path:SimpleNamespace(free=0))
    def forbidden(*a,**k):raise AssertionError('capacity stop must not launch any checker')
    monkeypatch.setattr(cli.subprocess,'run',forbidden)
    result=cli.execute(tmp_path/'registration.json','a'*64)
    assert result['passed'] is False and result['completed_cells']==[]
    assert result['stop_reason']=='current_admission_time_or_capacity_stop_no_retry'
    assert (tmp_path/'attempt.json').is_file() and (tmp_path/'result.json').is_file()
    with pytest.raises(FileExistsError):cli.execute(tmp_path/'registration.json','a'*64)


def test_source_projection_allows_only_the_separately_pinned_additive_gate(setup):
    v,h,p,i,d,_,_=setup
    original=i['source_sha256']
    assert v.current_source_allowed(original)
    assert v.current_source_allowed({**original,'src/runtime/starship_qualified_return.py':'e'*64})
    assert not v.current_source_allowed({**original,'src/runtime/other.py':'f'*64})
    first=next(iter(original))
    assert not v.current_source_allowed({**original,first:'f'*64})
