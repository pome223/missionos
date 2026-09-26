"""Check portable pilot evidence integrity; does not rerun physics or inference."""
from pathlib import Path
import hashlib
import json
import math
import re
import sys


def check(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        assert Path(name).name == name
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    data = json.loads((root / 'summary.json').read_text())
    assert hashlib.sha256((root / 'protocol.md').read_bytes()).hexdigest() == data['protocol_sha256'] == manifest['protocol_sha256']
    html = (root / 'index.html').read_text()
    embedded = re.search(r'const D=(.*?),S=document.querySelector', html).group(1)
    assert json.loads(embedded) == data
    assert data['comparison_to_rules_required'] is False
    assert data['simulation_only'] is True
    assert data['gateway_reobserve_wam_connected'] is False
    assert data['time_aligned_forecast_validated'] is False
    assert data['collision_prediction_validated'] is False
    rows = [c['result'] for c in data['cases']]
    assert {c['scene'] for c in data['cases']} == {'gap', 'climb', 'detour'}
    assert data['planned_cases'] == data['attempted_cases'] == 3
    assert len(data['attempts']) == data['total_attempts']
    assert len(rows) == data['verified_arrivals']
    assert sum(r['governed_bounds_met'] for r in rows) == data['cases_meeting_governed_bounds']
    assert sum(r['raw_choice_admissible'] for r in rows) == data['raw_admissible_model_choices']
    assert sum(r['requested_maneuver_observed'] for r in rows) == data['requested_maneuvers_observed']
    assert sum(r['requested_maneuver_from_admissible_model_top_choice'] for r in rows) == data['requested_maneuvers_from_model_top_choice']
    assert sum(r['model_calls'] for r in data['attempts']) == data['model_calls']
    assert data['latency_target_wall_s'] == 120
    assert data['cases_meeting_latency_target'] == sum(r['latency_target_met'] for r in rows)
    requested = {'gap': ['forward'], 'climb': ['climb'], 'detour': ['left_detour', 'right_detour']}
    for case in data['cases']:
        row = case['result']
        assert row in data['attempts']
        assert row['governed_bounds_met'] == all(row['bounds'].values())
        assert row['requested_maneuver_observed'] == (row['executed_route'] in requested[case['scene']])
        assert row['rules_changed_top_choice'] == (row['raw_model_choice'] != row['executed_route'])
        assert row['requested_maneuver_from_admissible_model_top_choice'] == (row['raw_choice_admissible'] and not row['rules_changed_top_choice'] and row['requested_maneuver_observed'])
        assert row['bounds']['route_within_60m'] == (row['route_distance_m'] <= 60)
        assert row['bounds']['route_within_120_sim_s'] == (row['route_sim_s'] <= 120)
        assert row['bounds']['clearance_at_least_025m'] == (row['minimum_clearance_m'] >= .25)
        assert row['bounds']['contact_notifications_zero'] == (row['building_contact_messages'] == 0)
        assert row['bounds']['observation_age_within_180s'] == all(0 <= row[k] <= 180 for k in ['observation_age_at_selection_wall_s', 'observation_age_at_dispatch_wall_s'])
        assert row['latency_target_met'] == all(0 <= row[k] <= 120 for k in ['observation_age_at_selection_wall_s', 'observation_age_at_dispatch_wall_s'])
        assert row['transport_timing']['remote_transport_invocations'] == 1
        for c in row['candidate_support']:
            for key in ('native_observed_fraction', 'model_grid_mean_observation_weight', 'model_grid_fully_observed_fraction'):
                assert 0 <= c[key] <= 1
            assert c['projection_reconstruction_max_error_8bit'] == 0
            if c['empty_projection']:
                assert c['supported_region_forecast_mse'] is None
        trace = case['trace']
        assert trace[0][0] == 0 and trace[-1][0] == row['route_sim_s']
        assert all(len(p) == 4 and all(math.isfinite(n) for n in p) for p in trace)
        assert all(a[0] < b[0] for a, b in zip(trace, trace[1:]))
        assert math.dist(trace[-1][1:], case['goal']) <= .3
        assert all(len(v) == 64 for v in row['raw_evidence_sha256'].values())
        for image in case['observed_images'] + case['predictions']:
            assert image['file'] in manifest['files']
    resource = data['resources']
    assert resource['created_vm_absent'] and resource['created_boot_disk_absent']
    assert resource['total_task_conservative_estimate_upper_bound_usd'] < resource['user_total_cap_usd'] == 10
    assert resource['invoice_reconciled'] is False
    if (root / 'resident-preflight.json').exists():
        preflight = json.loads((root / 'resident-preflight.json').read_text())
        assert preflight['real_model_forecast_calls'] == preflight['flight_attempts'] == 0
        assert preflight['latency_target_evaluated'] is False
        assert preflight['preload_completion_verified'] is False
        first, second = preflight['infrastructure_attempts']
        assert first['vm_and_disk_verified_deleted'] is True
        assert second['vm_created'] is False and second['new_gpu_cost_usd'] == 0
        assert math.isclose(resource['total_task_conservative_estimate_upper_bound_usd'] + first['estimate_upper_bound_usd'], preflight['cumulative_estimate_upper_bound_usd'])
        assert preflight['cumulative_estimate_upper_bound_usd'] < preflight['user_cap_usd'] == 10
    print('PASS: hashes, embedded data, case denominators, absolute bounds, attribution, time order and cleanup records')
    print('Portable checks only; original-record verification is documented separately.')


if __name__ == '__main__':
    check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent)
