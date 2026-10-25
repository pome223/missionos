"""Synthetic full-history binding tests, never mission or model evidence."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from scripts import starship_final_qualification_chunks as c
from src.runtime import starship_final_qualification as old


def point(count=50, engines=1):
    e = {'throttle': 0., 'gimbal_x_rad': 0., 'gimbal_y_rad': 0., 'available': True}
    states = [deepcopy(e) for _ in range(engines)]
    state = {'time_s': 2000., 'r_eci_m': [6500000., 0., 0.], 'v_eci_mps': [0., 7000., 0.],
        'q_body_to_eci': [1., 0., 0., 0.], 'omega_body_rad_s': [0., 0., 0.], 'propellant_kg': 90000.,
        'engine_states': states, 'flap_angles_rad': [0.]}
    return {'state': state, 'vehicle': {'engines': [{'max_thrust_n': 1000000.} for _ in states],
            'aero_panels': [{'name': 'invented_panel'}]},
        'inventory': {'total_count': 26, 'retained_count': 13, 'released_count': 13,
            'release_receipts': [{'id': i} for i in range(13)]},
        'actuator_history': [{'time_s': i*2000./(count-1), 'engine_states': states, 'flap_angles_rad': [0.]}
            for i in range(count)]}


def identity():
    return old.make_identity(source_sha256={'src/runtime/invented.py':'a'*64},
        backend={'python':'fixture','platform':'fixture','numpy':'fixture','scipy':'fixture',
            'executable_sha256':'b'*64},profile_sha256='c'*64,integration_policy_sha256='d'*64)


def domain():
    return {'ascent_condition':'O0','retained_count':13,'released_count':13,
        'return_candidate_id':'next_orbit','fault_family':'nominal','plant_capability_factor':1.,
        'sensor_gain':1.,'controller_capability_factor':1.}


def report(raw, *, expected_identity, expected_cell):
    p = json.loads(raw)['point']
    return {'schema':c.CHECK_SCHEMA,'record_sha256':sha256(raw).hexdigest(),
        'identity_sha256':old.digest(expected_identity),'cell_id':expected_cell['cell_id'],
        'domain':expected_cell['domain'],'point_summary':c.make_point(p),
        'point_pointers':{k:'/point/'+k for k in p},'record_checks_passed':True,
        'actual_state_continuity_verified':True,'controller_plant_separation_verified':True,
        'prediction_only':False,'contact':{'speed_mps':4.4,'tilt_deg':2.,
            'body_rate_rad_s':.001,'propellant_kg':55000.},
        'new_flights_during_check':0,'new_model_calls_during_check':0}


def build(checker=report):
    return c.build_certificate(expected_identity=identity(),required_cells=[{'cell_id':'fixture','domain':domain()}],
        records_raw={'fixture':old.canonical_bytes({'point':point()})},verify_record=checker)


def test_unchanged_limits_and_same_canonical_full_history_hash():
    p=point(); actual=c.make_point(p); legacy=old.make_point(p)
    assert c.MAXIMUM_POINT_BYTES == old.MAXIMUM_POINT_BYTES == 32*2**20
    assert c.MAXIMUM_RECORD_BYTES == old.MAXIMUM_RECORD_BYTES == 512*2**20
    assert c.MAXIMUM_HISTORY_ROWS == old.MAXIMUM_HISTORY_ROWS == 100000
    for k in ('state_sha256','vehicle_sha256','inventory_sha256','actuator_history_sha256'):
        assert actual[k] == legacy[k]
    assert actual['history']['canonical_bytes'] == len(old.canonical_bytes(p['actuator_history']))


def test_real_size_boundary_splits_without_raising_v1_limit_or_omitting_rows():
    p=point(16000,32)
    with pytest.raises(ValueError,match='point_preimages_required'):
        old.make_point(p)
    result=c.make_point(p); h=result['history']
    assert h['canonical_bytes'] > old.MAXIMUM_POINT_BYTES
    assert len(h['chunks']) == 2
    assert h['sha256'] == old.digest(p['actuator_history'])
    assert sum(x['row_count'] for x in h['chunks']) == 16000
    last=0
    for chunk in h['chunks']:
        assert chunk['start_row'] == last
        assert chunk['stop_row']-last == chunk['row_count']
        rows=p['actuator_history'][last:chunk['stop_row']]
        assert chunk['canonical_bytes'] == len(old.canonical_bytes(rows)) <= old.MAXIMUM_POINT_BYTES
        assert chunk['sha256'] == old.digest(rows)
        last=chunk['stop_row']
    assert last == 16000


@pytest.mark.parametrize('bad',['duplicate','reorder','nan','bool_time','bad_engine','dimensions','endpoint'])
def test_full_row_validation_including_chunk_boundary(monkeypatch,bad):
    monkeypatch.setattr(c,'MAXIMUM_POINT_BYTES',4096)
    p=point(); rows=p['actuator_history']; i=25
    if bad=='duplicate':rows[i]=deepcopy(rows[i-1])
    elif bad=='reorder':rows[i],rows[i+1]=rows[i+1],rows[i]
    elif bad=='nan':rows[i]['flap_angles_rad']=[float('nan')]
    elif bad=='bool_time':rows[i]['time_s']=True
    elif bad=='bad_engine':rows[i]['engine_states']=[{**rows[i]['engine_states'][0],'available':1}]
    elif bad=='dimensions':rows[i]['engine_states']=rows[i]['engine_states']*2
    else: rows[-1]['flap_angles_rad']=[.1]
    with pytest.raises(ValueError):c.make_point(p)


@pytest.mark.parametrize('bad',['drop_chunk','reorder_chunks','range','row_count','chunk_hash','whole_hash','bytes','state'])
def test_report_manifest_is_rebuilt_from_raw_not_trusted(monkeypatch,bad):
    monkeypatch.setattr(c,'MAXIMUM_POINT_BYTES',4096)
    def invalid(raw,**expected):
        r=report(raw,**expected); p=r['point_summary'];h=p['history']
        if bad=='drop_chunk':h['chunks'].pop()
        elif bad=='reorder_chunks':h['chunks'].reverse()
        elif bad=='range':h['chunks'][0]['stop_row']+=1
        elif bad=='row_count':h['row_count']-=1
        elif bad=='chunk_hash':h['chunks'][0]['sha256']='f'*64
        elif bad=='whole_hash':h['sha256']='f'*64
        elif bad=='bytes':h['canonical_bytes']-=1
        else:p['state_sha256']='f'*64
        return r
    with pytest.raises(ValueError,match='not_rebuilt_from_raw'):build(invalid)


def test_encoder_never_dumps_the_whole_logical_history(monkeypatch):
    p=point()
    original=c.canonical
    def bounded(value):
        assert value is not p and value is not p['actuator_history']
        return original(value)
    monkeypatch.setattr(c,'canonical',bounded)
    assert c.make_point(p)['history']['row_count']==50


@pytest.mark.parametrize('flag',['record_checks_passed','actual_state_continuity_verified',
    'controller_plant_separation_verified','prediction_only','contact'])
def test_incomplete_or_prediction_evidence_never_completes_certificate(flag):
    def invalid(raw,**expected):
        r=report(raw,**expected);r[flag]=None if flag=='contact' else flag=='prediction_only';return r
    result=build(invalid)
    assert not result['qualification_complete']
    assert not result['dispatch_authorized']
    assert not result['physical_execution_invoked']


def test_complete_certificate_retains_exact_scope_and_no_permission():
    result=build()
    assert result['qualification_complete']
    assert result['schema']==c.CERTIFICATE_SCHEMA
    assert result['cells'][0]['point']['history']['row_count']==50
    for flag in ('old39_certificate_reused','dispatch_authorized','physical_recovery_certified',
            'continuous_uncertainty_envelope_verified','runtime_execution_inferred_from_artifact'):
        assert result[flag] is False
