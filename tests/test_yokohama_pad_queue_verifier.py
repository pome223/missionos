"""Repeated judge waits preserve order, entry count, and contiguous receipt sequences."""
import pytest

from scripts.verify_yokohama_pad_queue import response_sequence_checks

WAIT = "wait_at_current_hold"
ENTER = "enter_delivery_approach"


@pytest.mark.parametrize("actions,entries,repeated,passed", [
    ([WAIT, ENTER], 1, False, True),
    ([WAIT, WAIT, ENTER], 1, False, False),
    ([WAIT, WAIT, ENTER], 1, True, True),
    ([WAIT, WAIT, WAIT, ENTER], 1, True, True),
    ([ENTER], 1, True, False),
    ([WAIT, ENTER, WAIT], 1, True, False),
    ([WAIT, ENTER, ENTER], 1, True, False),
    ([WAIT, WAIT, ENTER, ENTER, ENTER], 3, True, True),
    ([WAIT, ENTER, ENTER], 3, True, False),
    ([WAIT, "unknown", ENTER], 1, True, False),
    ([], 1, True, False),
])
def test_wait_and_entry_receipt_order(actions, entries, repeated, passed):
    requests = [dict(sequence=i, action=action) for i, action in enumerate(actions)]
    assert all(response_sequence_checks(requests, entries, repeated).values()) is passed


@pytest.mark.parametrize("sequences", [[0, 2, 3], [0, 1, 1], [1, 2, 3]])
def test_gaps_replays_and_wrong_start_rejected(sequences):
    requests = [dict(sequence=i, action=action) for i, action in zip(sequences, [WAIT, WAIT, ENTER])]
    checks = response_sequence_checks(requests, 1, True)
    assert checks["wait_then_continue"] is True
    assert checks["sequences"] is False


# Synthetic offline fixture based on the observed native-r2 pattern: one upload,
# one entry permission, HOLD samples for >3s, then a second mode send. No private
# task paths, operator identity, model weights, or generated run files are used.
def activation_fixture(attempts=2):
    from scripts.yokohama_altitude_contract import compile_mission, digest
    from scripts.verify_yokohama_pad_queue import REVIEWED_RETRY_PX4

    def row(t):
        return dict(
            run_id='offline', world_sha256='world', phase='03-DELIVERY', wall_s=t, sim_s=t,
            nav_state=4, arming_state=2, landed=False, position_valid=True,
            reset_counters=[0, 0, 0],
            velocity_ned=[0, 0, 0], battery_fraction=.8,
            mission_valid=True, mission_id=42, mission_count=2, px4_relative_altitude_m=15,
            altitude_capture=dict(begin_worker_wall_s=t-.02, end_worker_wall_s=t, sim_before_s=t-.02),
            vehicle=dict(id=1, xyz=[0, 0, 15.1], age_s=0, sensor_sim_s=t),
            queue_lead=dict(id=2, xyz=[20, 20, 0], age_s=0, sensor_sim_s=t),
            raw_px4=dict(
                vehicle_local_position=f''' timestamp: {int(t*1e6)} (0.000 seconds ago)
 z: -15
 ref_alt: 1
 ref_timestamp: 1000000
 z_reset_counter: 0
 z_global: True
 z_valid: True
''',
                vehicle_global_position=f''' timestamp: {int(t*1e6)} (0.000 seconds ago)
 alt: 16
 alt_valid: True
 alt_reset_counter: 0
''',
                home_position=''' timestamp: 1000000 (99.000 seconds ago)
 alt: 1
 z: 0
 update_count: 1
 valid_alt: True
''',
                vehicle_status=f''' timestamp: {int(t*1e6)} (0.000 seconds ago)
 nav_state_timestamp: 90000000
 nav_state: 4
 nav_state_user_intention: 4
 arming_state: 2
 executor_in_charge: 0
 failsafe: False
 failsafe_and_user_took_over: False
 failsafe_defer_state: 0
''',
                mission_result=''' timestamp: 99000000 (4.000 seconds ago)
 mission_id: 42
 seq_total: 2
 seq_current: 0
 seq_reached: -1
 valid: True
 finished: False
 failure: False
''',
            ),
        )

    config = dict(run_id='offline', operator_approval='offline-fixture', world=dict(world_sha256='world',
        pad_queue=dict(wait_xyz_m=[0, 0, 15.1], approach_xyz_m=[2, 0, 15.1],
                       pad_xyz_m=[4, 0, 0], trigger_phase='02-D3',
                       maximum_observation_age_s=1, maximum_sample_gap_sim_s=2,
                       minimum_battery_fraction=.2, pad_exclusion_radius_m=6,
                       approach_exclusion_radius_m=3),
        payload_delivery=dict(hover_world_xyz_m=[4, 0, 3])),
        simulator_heading_runtime=dict(px4_git=REVIEWED_RETRY_PX4))
    item = dict(seq=0, command=16, latitude_deg=35, longitude_deg=139,
                world_z_m=15.1, current=1, frame=6, param2=.5)
    mapping = compile_mission([item, dict(item, seq=1, command=17, current=0)],
                              row(99), segment='03-DELIVERY', run_id='offline',
                              world_sha256='world', now_worker_wall_s=99.01)
    response = dict(request_id='one-entry', proposed_action='enter_delivery_approach')
    grant = dict(event='pad_entry_authorized', phase='02-D3', wall_s=98.01,
                 permit=dict(request_id='one-entry', rules_checked_at=row(98),
                             response_sha256=digest(response), operator_approval=config['operator_approval']))
    advice = dict(event='missionos_advice_received', wall_s=98.005, response=response,
                  observation=grant['permit']['rules_checked_at'])
    events = [dict(event='simulator_heading_runtime_verified', wall_s=1, px4=REVIEWED_RETRY_PX4),
              advice, grant,
              dict(event='altitude_transport_prepared', phase='03-DELIVERY', segment='03-DELIVERY',
                   wall_s=99.02, mapping=mapping),
              dict(event='upload_receipt', phase='03-DELIVERY', segment='03-DELIVERY', wall_s=99.03)]
    rows = [grant['permit']['rules_checked_at']]
    for i in range(attempts):
        current = row(100+i*3.2)
        rows.append(current)
        events += [dict(event='pad_entry_dispatch_checked', phase='03-DELIVERY', wall_s=current['wall_s']+.001,
                        observation=current, request_id='one-entry'),
                   dict(event='altitude_transport_command_sent', phase='03-DELIVERY', segment='03-DELIVERY',
                        mapping_sha256=digest(mapping), observation=current,
                        dispatched_at_worker_wall_s=current['wall_s']+.002, wall_s=current['wall_s']+.003)]
        if i < attempts-1:
            rows += [row(current['wall_s']+j*.4) for j in range(1, 8)]
    accepted = row(rows[-1]['wall_s']+.4)
    accepted['nav_state'] = 3
    accepted['raw_px4']['vehicle_status'] = (accepted['raw_px4']['vehicle_status']
        .replace('nav_state: 4', 'nav_state: 3')
        .replace('nav_state_user_intention: 4', 'nav_state_user_intention: 3')
        .replace('nav_state_timestamp: 90000000', f"nav_state_timestamp: {int(accepted['wall_s']*1e6)}"))
    rows.append(accepted)
    return config, events, rows, grant


@pytest.mark.parametrize('attempts', [1, 2, 4])
def test_one_upload_one_entry_with_every_mode_send_verified(attempts):
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    checks, count = fixed_route_dispatch_checks(*activation_fixture(attempts))
    assert all(checks.values()) and count == attempts


def test_mode_send_count_is_bounded_even_with_fresh_permission():
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    with pytest.raises(ValueError, match='at most four'):
        fixed_route_dispatch_checks(*activation_fixture(5))


@pytest.mark.parametrize('fault', ['raw_hold', 'stale_status', 'old_transition', 'failsafe', 'missing_status',
                                 'foreign_run', 'foreign_world', 'foreign_vehicle', 'stale_capture', 'reset'])
def test_final_activation_requires_fresh_raw_transition_evidence(fault):
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    final = rows[-1]
    if fault == 'foreign_run':
        final['run_id'] = 'other'
    elif fault == 'foreign_world':
        final['world_sha256'] = 'other'
    elif fault == 'foreign_vehicle':
        final['vehicle']['id'] += 1
    elif fault == 'stale_capture':
        final['altitude_capture']['begin_worker_wall_s'] -= 3
    elif fault == 'reset':
        final['reset_counters'][0] += 1
    else:
        old, new = {
            'raw_hold': ('nav_state: 3', 'nav_state: 4'),
            'stale_status': ('(0.000 seconds ago)', '(3.000 seconds ago)'),
            'old_transition': (f"nav_state_timestamp: {int(final['wall_s']*1e6)}", 'nav_state_timestamp: 90000000'),
            'failsafe': ('failsafe: False', 'failsafe: True'),
            'missing_status': ('arming_state: 2', 'missing_arming_state: 2'),
        }[fault]
        final['raw_px4']['vehicle_status'] = final['raw_px4']['vehicle_status'].replace(old, new)
    with pytest.raises(ValueError):
        fixed_route_dispatch_checks(c, events, rows, grant)


def test_recorded_transition_between_observation_and_retry_send_is_rejected():
    import copy
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    send = events[-1]
    send['dispatched_at_worker_wall_s'] += .1
    send['wall_s'] += .1
    changed = copy.deepcopy(send['observation'])
    changed['wall_s'] += .05
    changed['nav_state'] = 3
    rows.insert(-1, changed)
    with pytest.raises(ValueError, match='Aircraft cannot wait'):
        fixed_route_dispatch_checks(c, events, rows, grant)


def test_raw_status_age_includes_delay_until_actual_mode_send():
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    send = events[-1]
    status = send['observation']['raw_px4']['vehicle_status']
    send['observation']['raw_px4']['vehicle_status'] = status.replace('(0.000 seconds ago)', '(1.800 seconds ago)')
    send['dispatched_at_worker_wall_s'] += .3
    send['wall_s'] += .3
    with pytest.raises(ValueError, match='stale'):
        fixed_route_dispatch_checks(c, events, rows, grant)


@pytest.mark.parametrize('fault', ['advanced_start', 'reached_start', 'grant_order', 'upload_order',
                                 'vehicle_identity', 'lead_identity', 'reset', 'wrong_hold', 'early_retry'])
def test_retry_preserves_uploaded_start_and_grant_identity(fault):
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    sends = [e for e in events if e['event'] == 'altitude_transport_command_sent']
    if fault in ('advanced_start', 'reached_start'):
        old, new = ('seq_current: 0', 'seq_current: 1') if fault == 'advanced_start' else ('seq_reached: -1', 'seq_reached: 1')
        for row in rows:
            row['raw_px4']['mission_result'] = row['raw_px4']['mission_result'].replace(old, new)
    elif fault in ('grant_order', 'upload_order'):
        entry = grant if fault == 'grant_order' else next(e for e in events if e['event'] == 'upload_receipt')
        events.remove(entry)
        events.append(entry)
    elif fault == 'vehicle_identity':
        rows[4]['vehicle']['id'] += 1
    elif fault == 'lead_identity':
        rows[4]['queue_lead']['id'] += 1
    elif fault == 'reset':
        rows[4]['reset_counters'][0] += 1
    elif fault == 'wrong_hold':
        grant['permit']['hold_xyz_m'] = [500, 500, 500]
    else:
        sends[0]['wall_s'] += .3
    with pytest.raises(ValueError):
        fixed_route_dispatch_checks(c, events, rows, grant)


@pytest.mark.parametrize('fault', ['changed_approval', 'reanchored_observation', 'missing_advice',
                                 'duplicate_advice', 'advice_order', 'future_receipt', 'response_digest'])
def test_permission_clock_cannot_be_reanchored_without_a_new_observed_grant(fault):
    import copy
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    advice = next(e for e in events if e['event'] == 'missionos_advice_received')
    if fault == 'changed_approval':
        grant['permit']['operator_approval'] = False
    elif fault == 'reanchored_observation':
        changed = copy.deepcopy(grant['permit']['rules_checked_at'])
        changed['wall_s'] += .001
        changed['altitude_capture']['end_worker_wall_s'] += .001
        rows.insert(1, changed)
        grant['permit']['rules_checked_at'] = changed
    elif fault == 'missing_advice':
        events.remove(advice)
    elif fault == 'duplicate_advice':
        events.append(copy.deepcopy(advice))
    elif fault == 'advice_order':
        events.remove(advice)
        events.append(advice)
    elif fault == 'future_receipt':
        advice['wall_s'] = grant['wall_s'] + .001
    else:
        grant['permit']['response_sha256'] = 'other'
    with pytest.raises(ValueError, match='grant|journal order'):
        fixed_route_dispatch_checks(c, events, rows, grant)


@pytest.mark.parametrize('fault', [
    'missing_check', 'missing_send', 'duplicate_check', 'reordered_send', 'changed_permit',
    'second_grant', 'foreign_phase', 'changed_mapping', 'reupload', 'unbound_observation',
    'stale_observation', 'expired_permission', 'future_permission', 'trajectory_gap',
    'trajectory_reversed', 'no_activation', 'unreviewed_px4', 'missing_runtime_proof',
])
def test_corrupt_activation_evidence_is_not_accepted_as_a_retry(fault):
    import copy
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    checks = [e for e in events if e['event'] == 'pad_entry_dispatch_checked']
    sends = [e for e in events if e['event'] == 'altitude_transport_command_sent']
    if fault == 'missing_check':
        events.remove(checks[1])
    elif fault == 'missing_send':
        events.remove(sends[1])
    elif fault == 'duplicate_check':
        events.append(copy.deepcopy(checks[1]))
    elif fault == 'reordered_send':
        events[-2:] = events[-2:][::-1]
    elif fault == 'changed_permit':
        checks[1]['request_id'] = 'another-entry'
    elif fault == 'second_grant':
        events.append(copy.deepcopy(grant))
    elif fault == 'foreign_phase':
        sends[1]['phase'] = 'other'
    elif fault == 'changed_mapping':
        sends[1]['mapping_sha256'] = 'another-mission'
    elif fault == 'reupload':
        events.append(copy.deepcopy(next(e for e in events if e['event'] == 'upload_receipt')))
    elif fault == 'unbound_observation':
        rows.remove(sends[1]['observation'])
    elif fault == 'stale_observation':
        sends[1]['dispatched_at_worker_wall_s'] += 2.1
        sends[1]['wall_s'] += 2.1
    elif fault == 'expired_permission':
        grant['permit']['rules_checked_at']['wall_s'] -= 40
    elif fault == 'future_permission':
        grant['permit']['rules_checked_at']['wall_s'] += 40
    elif fault == 'trajectory_gap':
        rows[:] = [rows[0], rows[1], rows[-2], rows[-1]]
    elif fault == 'trajectory_reversed':
        rows.reverse()
    elif fault == 'no_activation':
        rows[-1]['nav_state'] = 4
    elif fault == 'unreviewed_px4':
        c['simulator_heading_runtime']['px4_git'] = 'unknown'
    elif fault == 'missing_runtime_proof':
        del events[0]
    with pytest.raises(ValueError):
        fixed_route_dispatch_checks(c, events, rows, grant)


@pytest.mark.parametrize('fault', [
    'observed_mission', 'transitioned_then_hold', 'intention_changed', 'failsafe',
    'takeover', 'deferred_failsafe', 'external_executor', 'mission_id', 'mission_count',
    'mission_advanced', 'mission_rewound', 'mission_finished', 'mission_failure',
    'stale_status', 'missing_failsafe_status', 'occupied_pad', 'stale_pose',
    'altitude_reference_changed',
])
def test_every_intermediate_retry_sample_must_preserve_the_guard(fault):
    from scripts.verify_yokohama_pad_queue import fixed_route_dispatch_checks
    c, events, rows, grant = activation_fixture()
    middle = rows[4]
    if fault == 'observed_mission':
        middle['nav_state'] = 3
    elif fault == 'occupied_pad':
        middle['queue_lead']['xyz'] = [4, 0, 0]
    elif fault == 'stale_pose':
        middle['vehicle']['age_s'] = 2
    elif fault == 'mission_id':
        middle['mission_id'] = 43
        middle['raw_px4']['mission_result'] = middle['raw_px4']['mission_result'].replace('mission_id: 42', 'mission_id: 43')
    elif fault == 'mission_count':
        middle['mission_count'] = 3
    else:
        topic, old, new = {
            'transitioned_then_hold': ('vehicle_status', 'nav_state_timestamp: 90000000', 'nav_state_timestamp: 100000000'),
            'intention_changed': ('vehicle_status', 'nav_state_user_intention: 4', 'nav_state_user_intention: 3'),
            'failsafe': ('vehicle_status', 'failsafe: False', 'failsafe: True'),
            'takeover': ('vehicle_status', 'failsafe_and_user_took_over: False', 'failsafe_and_user_took_over: True'),
            'deferred_failsafe': ('vehicle_status', 'failsafe_defer_state: 0', 'failsafe_defer_state: 1'),
            'external_executor': ('vehicle_status', 'executor_in_charge: 0', 'executor_in_charge: 1'),
            'mission_advanced': ('mission_result', 'seq_current: 0', 'seq_current: 1'),
            'mission_rewound': ('mission_result', 'seq_reached: -1', 'seq_reached: -2'),
            'mission_finished': ('mission_result', 'finished: False', 'finished: True'),
            'mission_failure': ('mission_result', 'failure: False', 'failure: True'),
            'stale_status': ('vehicle_status', '(0.000 seconds ago)', '(3.000 seconds ago)'),
            'missing_failsafe_status': ('vehicle_status', ' failsafe: False\n', ''),
            'altitude_reference_changed': ('home_position', 'update_count: 1', 'update_count: 2'),
        }[fault]
        middle['raw_px4'][topic] = middle['raw_px4'][topic].replace(old, new)
    with pytest.raises(ValueError):
        fixed_route_dispatch_checks(c, events, rows, grant)
