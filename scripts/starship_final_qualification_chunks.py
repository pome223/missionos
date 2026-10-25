"""Bounded full-history requalification companion; legacy v1 is unchanged.

Raw JSON remains subject to the existing 512 MiB record limit. This does not
stream the raw JSON parser. It streams canonical history encoding in contiguous
row windows, each <= the unchanged 32 MiB point limit, and never encodes the
whole logical point/history at once. Every row is checked; hashes alone supplied
by a producer are not evidence. No runtime admission or execution is granted.
"""
from hashlib import sha256

from src.runtime import starship_final_qualification as legacy

POINT_SCHEMA = 'missionos.starship_final_qualification_chunked_point.v2'
CERTIFICATE_SCHEMA = 'missionos.starship_restored_domain_qualification.v2'
CHECK_SCHEMA = 'missionos.starship_final_qualification_chunked_checked_record.v2'
HISTORY_SCHEMA = 'missionos.starship_final_qualification_chunked_history.v1'
# Constants are aliases, not widened replacement limits.
MAXIMUM_RECORD_BYTES = legacy.MAXIMUM_RECORD_BYTES
MAXIMUM_POINT_BYTES = legacy.MAXIMUM_POINT_BYTES
MAXIMUM_HISTORY_ROWS = legacy.MAXIMUM_HISTORY_ROWS
require = legacy._require
canonical = legacy.canonical_bytes


def _row(value, dimensions, previous, final_time):
    require(type(value) is dict and set(value) == {'time_s', 'engine_states', 'flap_angles_rad'}
        and legacy._number(value['time_s']) and 0 <= value['time_s']
        and previous < value['time_s'] <= final_time,
        'chunked_qualification_history_clock_or_shape')
    legacy._actuators(value['engine_states'], value['flap_angles_rad'])
    require((len(value['engine_states']), len(value['flap_angles_rad'])) == dimensions,
        'chunked_qualification_history_dimensions')
    return value['time_s']


def _history(history, state):
    require(type(history) is list and 1 <= len(history) <= MAXIMUM_HISTORY_ROWS,
        'chunked_qualification_history_census')
    dimensions = len(state['engine_states']), len(state['flap_angles_rad'])
    whole, chunks, current = sha256(b'['), [], sha256(b'[')
    whole_size, size, start, previous, first = 2, 2, 0, -1., None

    def finish(stop):
        current.update(b']')
        chunks.append({'start_row': start, 'stop_row': stop, 'row_count': stop-start,
            'canonical_bytes': size, 'sha256': current.hexdigest(),
            'first_time_s': first, 'last_time_s': previous})

    for index, row in enumerate(history):
        when = _row(row, dimensions, previous, state['time_s'])
        encoded = canonical(row)
        require(len(encoded)+2 <= MAXIMUM_POINT_BYTES, 'chunked_qualification_single_row_oversized')
        addition = len(encoded)+(index > start)
        if size+addition > MAXIMUM_POINT_BYTES:
            finish(index)
            current, size, start, first = sha256(b'['), 2, index, None
            addition = len(encoded)
        if index:
            whole.update(b',')
            whole_size += 1
        whole.update(encoded)
        whole_size += len(encoded)
        if index > start:
            current.update(b',')
        current.update(encoded)
        size += addition
        first = when if first is None else first
        previous = when
    finish(len(history))
    whole.update(b']')
    require(history[-1] == {k: state[k] for k in ('time_s', 'engine_states', 'flap_angles_rad')},
        'chunked_qualification_history_endpoint')
    return {'schema': HISTORY_SCHEMA, 'row_count': len(history), 'canonical_bytes': whole_size,
        'sha256': whole.hexdigest(), 'chunk_limit_bytes': MAXIMUM_POINT_BYTES,
        'chunks': chunks}


def make_point(value):
    """Validate complete preimages and bind every history row without a large dump."""
    require(type(value) is dict and set(value) == {'state', 'vehicle', 'inventory', 'actuator_history'},
        'chunked_qualification_point_preimages_required')
    history = value['actuator_history']
    require(type(history) is list and 1 <= len(history) <= MAXIMUM_HISTORY_ROWS,
        'chunked_qualification_history_census')
    # Reuse the unchanged strict metadata/endpoint checks on a bounded header.
    # This is NOT the history evidence: _history below must validate all rows.
    metadata = {k: value[k] for k in ('state', 'vehicle', 'inventory')}
    bounded = legacy.make_point({**metadata, 'actuator_history': [history[-1]]})
    history_proof = _history(history, value['state'])
    point = {**bounded, 'schema': POINT_SCHEMA,
        'actuator_history_sha256': history_proof['sha256'], 'history': history_proof}
    require(len(canonical(point)) <= MAXIMUM_POINT_BYTES, 'chunked_qualification_descriptor_oversized')
    return point


def _bound_point(raw, report):
    pointers = report.get('point_pointers')
    require(type(pointers) is dict and set(pointers) == {'state', 'vehicle', 'inventory', 'actuator_history'},
        'chunked_qualification_point_pointers_required')
    value = legacy._record_json(raw)
    extracted = {k: legacy._pointer(value, path) for k, path in pointers.items()}
    history = extracted['actuator_history']
    require(type(history) is list and 1 <= len(history) <= MAXIMUM_HISTORY_ROWS,
        'chunked_qualification_history_census')
    normalized = []
    for row in history:
        require(type(row) is dict, 'chunked_qualification_raw_history_row')
        state = row.get('state', row)
        require(type(state) is dict and {'time_s', 'engine_states', 'flap_angles_rad'} <= state.keys(),
            'chunked_qualification_raw_history_preimage')
        normalized.append({k: state[k] for k in ('time_s', 'engine_states', 'flap_angles_rad')})
    extracted['actuator_history'] = normalized
    expected = make_point(extracted)
    summary = report.get('point_summary')
    require(type(summary) is dict and len(canonical(summary)) <= MAXIMUM_POINT_BYTES
        and canonical(summary) == canonical(expected), 'chunked_qualification_point_not_rebuilt_from_raw')
    return expected


def _checked(raw, identity, cell, verify_record):
    require(callable(verify_record), 'chunked_qualification_checker_required')
    raw_sha = legacy._raw(raw)
    report = verify_record(raw, expected_identity=legacy._copy(identity), expected_cell=legacy._copy(cell))
    fields = {'schema', 'record_sha256', 'identity_sha256', 'cell_id', 'domain', 'point_summary', 'point_pointers',
        'record_checks_passed', 'actual_state_continuity_verified', 'controller_plant_separation_verified',
        'prediction_only', 'contact', 'new_flights_during_check', 'new_model_calls_during_check'}
    require(type(report) is dict and set(report) == fields and report['schema'] == CHECK_SCHEMA
        and report['record_sha256'] == raw_sha and report['identity_sha256'] == legacy.digest(identity)
        and report['cell_id'] == cell['cell_id'] and legacy._domain(report['domain']) == cell['domain']
        and all(type(report[k]) is bool for k in ('record_checks_passed', 'actual_state_continuity_verified',
            'controller_plant_separation_verified', 'prediction_only'))
        and all(type(report[k]) is int and report[k] == 0 for k in ('new_flights_during_check', 'new_model_calls_during_check')),
        'chunked_qualification_independent_record_binding')
    point = _bound_point(raw, report)
    require(all(point[k] == cell['domain'][k] for k in ('retained_count', 'released_count')),
        'chunked_qualification_inventory_domain')
    contact, passed = report['contact'], False
    if contact is not None:
        require(type(contact) is dict and set(contact) == {'speed_mps', 'tilt_deg', 'body_rate_rad_s', 'propellant_kg'}
            and all(legacy._number(x) and x >= 0 for x in contact.values()), 'chunked_qualification_contact_shape')
        limits = legacy.CONTACT_LIMITS
        passed = (contact['speed_mps'] <= limits['maximum_speed_mps']
            and contact['tilt_deg'] <= limits['maximum_tilt_deg']
            and contact['body_rate_rad_s'] <= limits['maximum_body_rate_rad_s']
            and contact['propellant_kg'] >= limits['minimum_propellant_kg'])
    passed = (passed and report['record_checks_passed'] and report['actual_state_continuity_verified']
        and report['controller_plant_separation_verified'] and not report['prediction_only'])
    return {'cell_id': cell['cell_id'], 'domain': cell['domain'], 'record_sha256': raw_sha,
        'point': point, 'independent_check_sha256': legacy.digest(report), 'contact': legacy._copy(contact),
        'cell_passed': passed}


def build_certificate(*, expected_identity, required_cells, records_raw, verify_record):
    identity, cells = legacy._identity(expected_identity), legacy._cells(required_cells)
    require(type(records_raw) is dict and set(records_raw) == {r['cell_id'] for r in cells},
        'chunked_qualification_complete_raw_cell_census_required')
    checked = [_checked(records_raw[r['cell_id']], identity, r, verify_record) for r in cells]
    return {'schema': CERTIFICATE_SCHEMA, 'identity': identity, 'registered_cells_sha256': legacy.digest(cells),
        'required_cell_ids': [r['cell_id'] for r in cells], 'cells': checked,
        'qualification_scope': 'ship_return_exact_model_points_only',
        'qualification_complete': all(r['cell_passed'] for r in checked),
        'contact_limits': legacy._copy(legacy.CONTACT_LIMITS), 'domain': 'named_exact_finite_model_points_only',
        'encoding': {'point_schema': POINT_SCHEMA, 'maximum_record_bytes': MAXIMUM_RECORD_BYTES,
            'maximum_metadata_chunk_descriptor_bytes': MAXIMUM_POINT_BYTES,
            'maximum_history_rows': MAXIMUM_HISTORY_ROWS, 'logical_history_streamed_without_omission': True},
        'continuous_uncertainty_envelope_verified': False, 'old39_certificate_reused': False,
        'real_vehicle_validated': False, 'physical_recovery_certified': False, 'human_approval_inferred': False,
        'dispatch_authorized': False, 'physical_execution_invoked': False,
        'runtime_execution_inferred_from_artifact': False}
