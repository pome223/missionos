"""Saved qualification must recheck raw evidence, not cached success flags."""
from copy import deepcopy
from pathlib import Path

import pytest
from scripts import qualify_starship_final_saved as c


def record():
    state = {'time_s': 2000., 'engine_states': [], 'flap_angles_rad': []}
    return {'handoff': {'snapshot': {'state': state, 'vehicle': {'engines': []},
            'binding': {'candidate_id': 'next_orbit'}},
        'inventory': {'retained_count': 13, 'released_count': 13},
        'coast_history': [{'state': state}]},
        'sensor_return': {'application': {'factor': .8123}, 'complete_estimate': {'factor_estimate': .8}}}


def test_domain_uses_once_applied_numerical_factor_and_keeps_fault_hypotheses_distinct():
    value = record()
    assert c.domain(value, 'T')['controller_capability_factor'] == .8123
    assert c.domain(value, 'A')['controller_capability_factor'] == 1.
    assert c.domain(value, 'A')['plant_capability_factor'] == 1.
    assert c.domain(value, 'A')['sensor_gain'] == .8
    assert c.domain(value, 'N')['fault_family'] == 'nominal'


def test_actuator_history_is_extracted_from_actual_pre_dispatch_coast_not_forecast():
    value = record()
    value['prediction'] = {'state': {'time_s': 9999.}}
    result = c.point_data(value)
    assert result['state'] is value['handoff']['snapshot']['state']
    assert result['actuator_history'] == [{'time_s': 2000., 'engine_states': [], 'flap_angles_rad': []}]
    assert set(result) == {'state', 'vehicle', 'inventory', 'actuator_history'}


def test_current_raw_verification_failure_is_not_replaced_by_cached_pass(monkeypatch):
    registration = {'identity': {}}
    row = {'plan': {}, 'outer_receipt': {}, 'packed': {}, 'raw_sha256': c.sha256(b'{}').hexdigest(), 'folder': 'output/invented'}
    monkeypatch.setattr(c, 'checked_ref', lambda ref: Path('unused'))
    monkeypatch.setattr(c, 'validate_plan', lambda *args: None)
    monkeypatch.setattr(c.shared, 'read', lambda path: {'case_id': 'T'})
    monkeypatch.setattr(c.runner, 'restore_record', lambda path: b'{}')
    monkeypatch.setattr(c.runner, 'verify_saved', lambda *args: {'passed': False, 'issues': ['actual_record_invalid']})
    with pytest.raises(ValueError, match='saved_mission_verification_failed'):
        c.check_cell(registration, row)


def test_changed_original_raw_hash_refuses_before_saved_verifier(monkeypatch):
    row = {'plan': {}, 'outer_receipt': {}, 'packed': {}, 'raw_sha256': 'f'*64}
    monkeypatch.setattr(c, 'checked_ref', lambda ref: Path('unused'))
    monkeypatch.setattr(c, 'validate_plan', lambda *args: None)
    monkeypatch.setattr(c.shared, 'read', lambda path: {})
    monkeypatch.setattr(c.runner, 'restore_record', lambda path: b'{}')
    monkeypatch.setattr(c.runner, 'verify_saved', lambda *args: pytest.fail('must reject changed raw before rechecking'))
    with pytest.raises(ValueError, match='raw_record_changed'):
        c.check_cell({}, row)
