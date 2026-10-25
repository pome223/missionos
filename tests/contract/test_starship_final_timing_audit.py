"""Small immutable saved metadata, never physical trajectories or providers."""
from hashlib import sha256
import json
import os

import pytest
from scripts.audit_starship_final_timing import audit


def setup_saved(tmp_path, seconds):
    folder=tmp_path/'saved';folder.mkdir()
    plan={'run_id':'invented-timing-only','source_commit':'a'*40,'configuration':{'bounds':{'per_mission_wall_s':1800.}}}
    plan['plan_sha256']=sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    common={'plan_sha256':plan['plan_sha256']}
    records={'plan.json':plan,'started.json':common,'worker.claim.json':common,'worker-result.json':common,
        'verification.json':{**common,'passed':True}}
    rows=[];base=1700000000000000000
    for name,value in records.items():
        path=folder/name;raw=json.dumps(value).encode();path.write_bytes(raw)
        delta=seconds if name=='worker-result.json' else seconds+5 if name=='verification.json' else 0
        ns=base+int(delta*1e9);os.utime(path,ns=(ns,ns))
        rows.append({'path':name,'kind':'file','mtime_ns':ns,'bytes':len(raw),'sha256':sha256(raw).hexdigest()})
    manifest=tmp_path/'manifest.json';raw=json.dumps({'schema':'missionos.private_live_T_tree_manifest.v2',
        'plan_sha256':plan['plan_sha256'],'files':rows}).encode();manifest.write_bytes(raw)
    return folder,manifest,sha256(raw).hexdigest(),plan['plan_sha256']


@pytest.mark.parametrize('seconds,within',[(20.,True),(2143.796402484,False)])
def test_saved_elapsed_clock_is_reported_without_relabeling_original_verifier(tmp_path,seconds,within):
    args=setup_saved(tmp_path,seconds)
    original=(args[0]/'verification.json').read_bytes()
    value=audit(*args)
    assert value['elapsed_s']==pytest.approx(seconds)
    assert value['sleep_inclusive_registered_limit_met'] is within
    assert value['saved_verifier_passed_unchanged'] is True
    assert not value['complete_monotonic_clock_pair_available']
    assert not value['changes_original_evidence'] and not value['grants_execution_authority']
    assert (args[0]/'verification.json').read_bytes()==original


@pytest.mark.parametrize('changed',['bytes','mtime','manifest_hash','plan'])
def test_timestamp_audit_refuses_rewritten_metadata_or_external_pins(tmp_path,changed):
    args=list(setup_saved(tmp_path,20.));path=args[0]/'worker-result.json'
    if changed=='bytes':path.write_bytes(b'{}')
    elif changed=='mtime':os.utime(path,ns=(1,1))
    elif changed=='manifest_hash':args[2]='0'*64
    else:args[3]='0'*64
    with pytest.raises(ValueError):audit(*args)
