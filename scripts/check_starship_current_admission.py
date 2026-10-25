#!/usr/bin/env python3
"""One registered saved-only current-point/normal-permission verification.

No flight/API/retry. The synthetic owner smoke is explicitly not a flown
current observation; saved replay never instantiates an actual handoff owner.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *[str(ROOT/'packages'/p/'src') for p in
    ('missionos-core','missionos-cli','missionos-gateway')]]
from src.runtime import starship_final_shared as shared
from src.runtime import starship_qualified_return as gate
from scripts import qualify_starship_final_saved as saved
from scripts import run_starship_final_mission as runner

SCRIPT = 'scripts/check_starship_current_admission.py'
SOURCES = (SCRIPT, 'src/runtime/starship_qualified_return.py',
    'tests/contract/test_starship_qualified_return.py',
    'tests/contract/test_starship_final_handoff.py',
    'tests/contract/test_starship_restored_admission.py',
    'tests/contract/test_starship_final_qualification_chunks.py')
SCHEMA = 'missionos.starship_current_admission_registration.v1'
BOUNDS = {'per_cell_s':420., 'total_s':1800., 'minimum_free_bytes':10*2**30,
    'maximum_new_flights':0,'maximum_provider_calls':0,'automatic_retry':False}


def register(folder, qualification, certificate):
    commit = shared.clean_commit()
    qref,cref = saved.file_ref(qualification),saved.file_ref(certificate)
    q,c = shared.read(saved.checked_ref(qref)),shared.read(saved.checked_ref(cref))
    shared.require(q['registration_sha256'] == c['verification_registration_sha256']
        and q['identity'] == c['identity'] and len(q['cells']) == 4
        and q['schema'] == saved.SCHEMA and q['bounds'] == BOUNDS
        and q['registration_sha256'] == shared.digest({k:v for k,v in q.items() if k!='registration_sha256'}),
        'current_admission_certificate_registration_census')
    for name in ('checker','chunk_checker'):saved.checked_ref(q[name])
    for row in q['cells']:
        for name in ('plan','packed','outer_receipt'):saved.checked_ref(row[name])
    source = {n:sha256(shared.relative(n).read_bytes()).hexdigest() for n in SOURCES}
    shared.check_manifest(q['identity']['source_sha256'])
    shared.check_manifest(c['verification_source_sha256'])
    shared.require(shared.environment() == c['verification_backend'], 'current_admission_backend_changed')
    value = {'schema':SCHEMA,'source_commit':commit,'adapter_source_sha256':source,
        'qualification_registration':qref,'certificate':cref,'bounds':BOUNDS,
        'required_cells':[row['cell_id'] for row in q['cells']],
        'checks':['fresh_independent_saved_cell_recheck','complete_raw_certificate_rebuild',
            'four_exact_saved_preimage_matches','changed_state_and_history_refused',
            'normal_permission_scripted_owner_smoke'],
        'normal_permission_scope':'nominal_exact_point_only_fault_continuation_application_is_future',
        'saved_records_are_live_owners':False,'legacy39_refreshed':False,
        'human_approval_or_dispatch_inferred':False,'prior_records_relabelled':False}
    value['registration_sha256']=shared.digest(value)
    folder.mkdir(exist_ok=False)
    shared.save(folder/'registration.json',value)
    return value


def load(path):
    p=shared.read(path)
    shared.require(p['schema']==SCHEMA and p['registration_sha256']==shared.digest(
        {k:v for k,v in p.items() if k!='registration_sha256'}) and p['bounds']==BOUNDS,
        'current_admission_registration_or_bounds_changed')
    shared.require(shared.clean_commit()==p['source_commit'],'current_admission_source_commit_changed')
    shared.check_manifest(p['adapter_source_sha256'])
    q=shared.read(saved.checked_ref(p['qualification_registration']))
    saved.checked_ref(p['certificate'])
    shared.check_manifest(q['identity']['source_sha256'])
    shared.require(shared.environment()==q['backend'],'current_admission_backend_changed')
    return p,q


def child(path,index):
    p,q=load(path)
    row=q['cells'][index]
    report,verification=saved.check_cell(q,row)
    shared.save(path.parent/(row['cell_id']+'-checked.json'),report)
    shared.save(path.parent/(row['cell_id']+'-verification.json'),verification)
    return {'cell_id':row['cell_id'],'passed':True}


def execute(path,expected):
    p,q=load(path)
    shared.require(p['registration_sha256']==expected,'current_admission_explicit_plan_hash_required')
    root=path.parent
    shared.save(root/'attempt.json',{'registration_sha256':expected,
        'started_utc':datetime.now(timezone.utc).isoformat(),'automatic_retry':False})
    started,utc=time.monotonic(),time.time()
    result={'schema':'missionos.starship_current_admission_result.v1','passed':False,
        'registration_sha256':expected,'completed_cells':[],'new_flights':0,'provider_calls':0,
        'runtime_live_observation_verified':False,'operational_dispatch':False,'legacy39_refreshed':False}
    def budget():
        elapsed=max(time.monotonic()-started,time.time()-utc)
        shared.require(elapsed < BOUNDS['total_s'] and shutil.disk_usage(ROOT).free >= BOUNDS['minimum_free_bytes'],
            'current_admission_time_or_capacity_stop_no_retry')
        return BOUNDS['total_s']-elapsed
    try:
        reports,records={},{}
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','TMPDIR')}
        for i,row in enumerate(q['cells']):
            timeout=min(BOUNDS['per_cell_s'],budget())
            with (root/(row['cell_id']+'.log')).open('xb') as log:
                done=subprocess.run([sys.executable,str(ROOT/SCRIPT),'check-cell','--registration',str(path),
                    '--index',str(i)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                    timeout=timeout,check=False)
            shared.require(done.returncode==0,'current_admission_cell_failed:'+row['cell_id'])
            reports[row['cell_id']]=shared.read(root/(row['cell_id']+'-checked.json'))
            records[row['cell_id']]=runner.restore_record(saved.checked_ref(row['packed']))
            result['completed_cells'].append(row['cell_id'])
        budget()
        verified=gate.VerifiedReturnCertificate.prepare(saved.checked_ref(p['certificate']).read_bytes(),
            expected_sha256=p['certificate']['sha256'],expected_identity=q['identity'],
            required_cells=[{k:row[k] for k in ('cell_id','domain')} for row in q['cells']],
            records_raw=records, verify_record=lambda raw,**kw:reports[kw['expected_cell']['cell_id']],
            expected_adapter_source=p['adapter_source_sha256'])
        matches=[]
        for row in q['cells']:
            budget()
            data=saved.point_data(shared.parse(records[row['cell_id']]))
            match=verified.match_preimages(point_data=data,domain=row['domain'],identity=q['identity'])
            shared.require(match['point_domain_evidence_matched'],'current_admission_exact_replay_mismatch')
            changed={**data,'state':{**data['state'],'propellant_kg':data['state']['propellant_kg']-.01}}
            denied=verified.match_preimages(point_data=changed,domain=row['domain'],identity=q['identity'])
            shared.require(not denied['point_domain_evidence_matched'],'current_admission_changed_state_accepted')
            dropped={**data,'actuator_history':data['actuator_history'][1:]}
            denied=verified.match_preimages(point_data=dropped,domain=row['domain'],identity=q['identity'])
            shared.require(not denied['point_domain_evidence_matched'],'current_admission_omitted_history_accepted')
            matches.append({'cell_id':row['cell_id'],'match':match,
                'changed_state_refused':True,'omitted_history_refused':True,
                'saved_record_not_live_owner':True})
        with (root/'normal-permission-fixture.log').open('xb') as log:
            smoke=subprocess.run([sys.executable,str(ROOT/SCRIPT),'smoke',
                '--output',str(root/'normal-permission-fixture.json')],
                cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=min(420.,budget()),check=False)
        shared.require(smoke.returncode==0,'current_admission_normal_permission_fixture_failed')
        verified.check_environment(); budget()
        result.update(passed=True,matches=matches,normal_permission_scripted_owner_smoke_passed=True,
            ordinary_fault_branch_admitted=False,ordinary_launch_legacy_gate_changed=False)
    except Exception as exc:
        result['stop_reason']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        result['automatic_retry']=False
    result['elapsed_s']=max(time.monotonic()-started,time.time()-utc)
    shared.save(root/'result.json',result)
    return result


def smoke(output):
    """Exercise the production normal permission entry with scripted ownership.

    No historical JSON is promoted into a live owner. This separate synthetic
    generator fixture starts a production owner without any integration step;
    all propagation, socket and provider paths are forbidden by its harness.
    """
    import pytest
    from tests.contract import test_starship_qualified_return as fixture
    with pytest.MonkeyPatch.context() as mp:
        values=fixture.setup.__wrapped__(mp)
        calls=[]
        def existing_permission(handoff, *,qualification_receipt):
            shared.require(qualification_receipt['point_domain_evidence_matched']
                and qualification_receipt['dispatch_authorized'] is False,
                'current_admission_fixture_permission_boundary')
            calls.append(handoff)
            return {'fixture_host_permission_reached':True,'physical_execution':False}
        entry=fixture.entry(values,existing_permission)
        answer=entry.enter(values[1],current_plan=values[6])
        shared.require(answer['fixture_host_permission_reached'] and len(calls)==1
            and not values[1].consumed,'current_admission_fixture_owner_or_authority')
        for bad in (values[1],values[1].record()):
            try:entry.enter(bad,current_plan=values[6])
            except ValueError:pass
            else:raise ValueError('current_admission_fixture_reused_entry')
        saved_json_entry=fixture.entry(values,existing_permission)
        try:saved_json_entry.enter(values[1].record(),current_plan=values[6])
        except ValueError:pass
        else:raise ValueError('current_admission_fixture_saved_json_became_owner')
        shared.require(len(calls)==1,'current_admission_fixture_permission_called_after_rejection')
        result={'schema':'missionos.starship_normal_return_permission_runtime_smoke.v1',
            'passed':True,'production_entry':'NormalReturnPermissionEntry.enter',
            'fixture_host_permission_invocations':len(calls),'scripted_yielded_owner':True,
            'saved_json_owner_refused':True,'duplicate_entry_refused':True,
            'source_and_backend_fixture_injection':True,'human_approval_verified':False,
            'live_observation_verified':False,'operational_dispatch':False,
            'new_flights':0,'provider_calls':0,'entry_record':entry.record()}
    shared.save(output,result)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='command',required=True)
    s=sub.add_parser('smoke');s.add_argument('--output',required=True)
    r=sub.add_parser('register');r.add_argument('--output',required=True);r.add_argument('--qualification',required=True);r.add_argument('--certificate',required=True)
    for cmd in ('execute','check-cell'):
        p=sub.add_parser(cmd);p.add_argument('--registration',required=True)
        p.add_argument('--authorization-sha256' if cmd=='execute' else '--index',required=True,type=str if cmd=='execute' else int)
    a=ap.parse_args()
    if a.command=='smoke':value=smoke(shared.relative(a.output))
    elif a.command=='register':value=register(shared.relative(a.output),a.qualification,a.certificate)
    elif a.command=='execute':value=execute(shared.relative(a.registration),a.authorization_sha256)
    else:value=child(shared.relative(a.registration),a.index)
    print(shared.wire(value).decode())
    return 0 if value.get('passed',True) else 1

if __name__=='__main__':raise SystemExit(main())
