"""Verify published individual evidence, causal order, summaries and asset hashes.

Run with --statistics to additionally regenerate bootstrap/sign-flip calculations
(requires NumPy). This validates the released record, not the private simulator.
"""
from pathlib import Path
import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics


def close(a, b):
    assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), (a, b)


def quantile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    lo = int(index)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)


def outcome(record):
    assert record['horizon_steps'] in (284, 568)
    assert record['collapsed'] == (record['max_drop_m'] > .03)
    close(record['max_drop_m'], max(record['per_object_drop']))
    expected = None if record['technical_failure'] else (0 if record['collapsed'] else record['count_after'])
    assert record['score'] == expected


def verify(root, recompute_statistics=False):
    data = json.loads((root / 'benchmark.json').read_text())
    data['decisions'] = json.loads(gzip.decompress((root / 'decision-evidence.json.gz').read_bytes()))
    with (root / 'decisions.csv').open(newline='') as handle:
        compact_rows = list(csv.DictReader(handle))
    assert len(compact_rows) == len(data['decisions'])
    for compact, full in zip(compact_rows, data['decisions']):
        assert compact == {k: '' if full.get(k) is None else str(full[k]) for k in compact}
    protocol = json.loads((root / 'protocol.json').read_text())
    methods = protocol['methods']
    expected = {(s, m) for s in protocol['seeds'] for m in methods}
    physics = json.loads((root / 'cohort-physics.json').read_text())
    assert len(physics) == 40 and {c['seed'] for c in physics} == set(protocol['seeds'])
    for config in physics:
        assert len(config['objects']) == 10 and config['all_five_arms_identical']
        group = 'wide' if statistics.mean(c['half_size'][0]*2 for c in config['objects']) >= protocol['width_rule']['group_threshold_m'] else 'narrow'
        assert all(g['width_group'] == group for g in data['games'] if g['seed'] == config['seed'])
    games = data['games']
    assert len(games) == 200
    assert {(g['seed'], g['method']) for g in games} == expected
    assert len({(g['seed'], g['method']) for g in games}) == len(games)
    decisions = data['decisions']
    assert len({(r['seed'], r['method'], r['next_count']) for r in decisions}) == len(decisions)
    all_ids = []
    remote_lines = []
    for g in games:
        rows = [r for r in decisions if (r['seed'], r['method']) == (g['seed'], g['method'])]
        assert len(rows) == g['decision_count']
        assert [r['next_count'] for r in rows] == list(range(1, len(rows) + 1))
        assert all(r['decision'] == 'place' for r in rows[:-1])
        assert g['decision_motor_steps'] == sum(r['actual_motor_steps'] for r in rows)
        assert g['actual_motor_steps'] == g['decision_motor_steps'] + g['terminal_extra_hold_steps']
        assert g['actual_vla_inference_count'] == sum(len(r['actual_vla_inference_ids']) for r in rows)
        if g['technical_failure'] is None:
            terminal = g['terminal_outcome']
            outcome(terminal)
            assert g['terminal_hold_verified'] is True
            assert g['terminal_extra_hold_steps'] == (284 if rows[-1]['decision'] == 'place' else 0)
            assert terminal['horizon_steps'] == (284 if rows[-1]['decision'] == 'bank' else 568)
            assert g['final_score'] == terminal['score']
            assert g['collapsed'] == terminal['collapsed']
            assert g['bank_collapse'] == (rows[-1]['decision'] == 'bank' and terminal['collapsed'])
        for r in rows:
            assert r['decision_committed_at'] <= r['execution_started_at'] <= r['execution_finished_at']
            assert r['vla_outputs_match_executed_actions'] is True
            assert r['applied_vla_motor_steps'] in (0, 60, 72)
            receipts = r['inference_receipts']
            assert [x['id'] for x in receipts] == r['actual_vla_inference_ids']
            for x in receipts:
                assert x['actual_remote_inference'] and x['device'] == 'CUDA'
                assert r['decision_committed_at'] <= x['request_started_at'] <= x['response_received_at'] <= r['execution_finished_at']
                remote = x['remote_inference']
                assert remote['input_sha256'] == x['input_sha256']
                assert remote['model_sha256'] == protocol['vla_sha256']
                assert x['request_started_at'] - 1 <= remote['started_at'] <= remote['finished_at'] <= x['response_received_at'] + 1
                remote_lines.append(remote['log_line'])
                all_ids.append(x['id'])
            if r['decision'] == 'bank':
                assert r['counterfactual_started_at'] >= r['execution_finished_at']
            for key in ('actual_outcome', 'continue_outcome', 'long_continue_outcome'):
                outcome(r[key])
            assert r['continue_collapsed'] == r['continue_outcome']['collapsed']
            assert r['long_continue_collapsed'] == r['long_continue_outcome']['collapsed']
            if g['method'] == 'acwm':
                backend = r['backend_inference']
                close(backend['readout_output'], r['risk'])
                assert backend['model_sha256'] == protocol['acwm_freeze']['checkpoint_sha256']
                assert backend['readout_sha256'] == protocol['acwm_freeze']['readout_sha256']
                assert backend['future_action_tape_access'] is False
                assert backend['prediction_finished_at'] <= r['decision_committed_at'] + 1
                assert len(r['generated_array_sha256']) == 64
                assert r['generated_shape'] == [37, 240, 240, 3]
                forecast = r['forecast']
                assert forecast['status'] == 'available'
                assert forecast['verification_basis'] == 'model_inferred'
                assert all(forecast[k] is False for k in ('approval_recorded', 'dispatch_authority_created', 'physical_execution_invoked', 'completion_claimed'))
                assert forecast['binding']['model_sha256'] == protocol['acwm_freeze']['checkpoint_sha256']
                assert forecast['binding']['policy_sha256'] == protocol['vla_sha256']
                assert forecast['binding']['input_schema'] == 'stacking.current_image_macro.v1'
                f = forecast['forecasts'][0]
                assert f['future_state']['readout_sha256'] == protocol['acwm_freeze']['readout_sha256']
                assert f['future_state']['generation_invocation_id'] == r['prediction_invocation_id']
                close(f['risk_score'], r['risk'])
                assert f['horizon_seconds'] == 14.2
                assert (r['decision'] == 'bank') == (r['risk'] >= protocol['acwm_freeze']['readout_threshold'])
            elif g['method'] == 'extratrees':
                assert r['predictor_model_sha256'] == protocol['extratrees_sha256']
                assert (r['decision'] == 'bank') == (r['risk'] >= r['stopping_threshold'] and r['bank_risk'] < r['risk'])
            elif g['method'] == 'rule':
                assert (r['decision'] == 'bank') == (r['tilt_deg'] > 5 or r['drift_m'] > .003)
            elif g['method'] == 'width_rule':
                assert (r['decision'] == 'bank') == (r['next_count'] - 1 >= protocol['width_rule'][g['width_group']])
            elif g['method'] == 'vla':
                assert r['decision'] == 'place'
    assert len(all_ids) == len(set(all_ids)) == data['actual_vla_inferences']
    assert len(remote_lines) == len(set(remote_lines)) == len(all_ids)
    assert data['actual_motor_steps'] == sum(g['actual_motor_steps'] for g in games)
    assert data['applied_vla_motor_steps'] == sum(r['applied_vla_motor_steps'] for r in decisions)
    for method in methods:
        gs = [g for g in games if g['method'] == method]
        rs = [r for r in decisions if r['method'] == method]
        valid = [g for g in gs if g['technical_failure'] is None]
        summary = data['summary'][method]
        assert len(gs) == summary['attempts'] == 40
        assert sum(g['width_group'] == 'wide' for g in gs) == 20
        assert len(valid) == summary['valid_games']
        assert 40 - len(valid) == summary['technical_failures']
        assert sum(r['decision'] == 'bank' for r in rs) == summary['bank_decisions']
        assert sum(r['diagnostic_technical_failure'] is not None for r in rs) == summary['diagnostic_technical_failures']
        assert sum(g['final_score'] == 10 for g in gs) == summary['completed_ten_games']
        assert sum(g['final_score'] for g in valid) == summary['total_score']
        if valid:
            close(statistics.mean(g['final_score'] for g in valid), summary['mean_valid_score'])
        assert sum(g['collapsed'] for g in gs) == summary['collapse_games']
        assert sum(g['bank_collapse'] for g in gs) == summary['bank_collapse_games']
        confusion = dict.fromkeys(('tp', 'fp', 'tn', 'fn'), 0)
        for r in rs:
            if r['diagnostic_technical_failure'] is not None:
                continue
            actual = r['long_continue_collapsed' if method == 'extratrees' else 'continue_collapsed']
            predicted = r['decision'] == 'bank'
            confusion[('tp' if actual else 'fp') if predicted else ('fn' if actual else 'tn')] += 1
        assert confusion == summary['confusion']
        extended = dict.fromkeys(('tp', 'fp', 'tn', 'fn'), 0)
        for r in rs:
            if r['diagnostic_technical_failure'] is not None:
                continue
            actual = r['long_continue_collapsed']
            predicted = r['decision'] == 'bank'
            extended[('tp' if actual else 'fp') if predicted else ('fn' if actual else 'tn')] += 1
        assert extended == summary['diagnostic_confusion_28_4_seconds']
        latency = [r['decision_latency_seconds'] for r in rs]
        close(statistics.mean(latency), summary['mean_decision_latency_seconds'])
        close(quantile(latency, .95), summary['p95_decision_latency_seconds'])
    assert len(data['paired_rows']) == 40
    for pair in data['paired_rows']:
        assert pair['scores'] == {g['method']: g['final_score'] for g in games if g['seed'] == pair['seed']}
    audits = []
    for seed in protocol['seeds']:
        for count in range(1, 11):
            rs = [r for r in decisions if r['seed'] == seed and r['next_count'] == count]
            if rs:
                audits.append({'seed': seed, 'count': count, 'states_match': len({r['state_sha256'] for r in rs}) == 1, 'placed_actions_match': len({r['action_sha256'] for r in rs if r['decision'] == 'place'}) <= 1, 'counterfactual_actions_match': len({r['action_sha256'] if r['decision'] == 'place' else r['counterfactual_action_sha256'] for r in rs}) <= 1, 'arms': len(rs)})
    assert audits == data['prefix_audit']
    assert data['all_prefixes_match'] == all(a['states_match'] and a['placed_actions_match'] and a['counterfactual_actions_match'] for a in audits)
    paired = [r for r in data['paired_rows'] if all(v is not None for v in r['scores'].values())]
    for key, comparison in data['comparisons'].items():
        other = key.removeprefix('acwm_minus_')
        differences = [r['scores']['acwm'] - r['scores'][other] for r in paired]
        assert comparison['n'] == len(differences)
        close(comparison['total_difference'], sum(differences))
        close(comparison['mean_difference'], statistics.mean(differences))
        assert comparison['wins'] == sum(d > 0 for d in differences)
        assert comparison['ties'] == sum(d == 0 for d in differences)
        assert comparison['losses'] == sum(d < 0 for d in differences)
        if recompute_statistics:
            import numpy as np
            d = np.array(differences, float)
            rng = np.random.default_rng(protocol['statistics']['rng'])
            boot = np.zeros(20000)
            for group in ('wide', 'narrow'):
                v = d[[r['width_group'] == group for r in paired]]
                if len(v):
                    boot += v[rng.integers(0, len(v), (20000, len(v)))].sum(1)
            boot /= len(d)
            for a, b in zip(np.quantile(boot, [.025, .975]), comparison['bootstrap95']):
                close(a, b)
            null = (d * rng.choice([-1, 1], (100000, len(d)))).mean(1)
            close((1 + sum(abs(null) >= abs(d.mean()) - 1e-12)) / 100001, comparison['p'])
    last = 0
    for rank, key in enumerate(sorted(data['comparisons'], key=lambda k: data['comparisons'][k]['p'])):
        c = data['comparisons'][key]
        last = max(last, min(1, c['p'] * (4 - rank)))
        close(last, c['holm_p'])
    phase1 = json.loads((root / 'phase1-phase1-evaluation.json').read_text())
    for tag in ('before-seed195', 'after-seed195', 'after-seed196'):
        measured = json.loads((root / ('phase1-' + tag + '.json')).read_text())
        result = phase1[tag]
        rows = result['rows']
        assert len(rows) == len(measured) == 16
        assert len({r['name'] for r in rows}) == len({r['name'] for r in measured}) == 16
        assert {r['name'] for r in rows} == {r['name'] for r in measured}
        assert all(r['predicted_collapse'] == (r['probability'] >= .5) for r in rows)
        for key, pred, truth in (('tp', True, True), ('fp', True, False), ('tn', False, False), ('fn', False, True)):
            assert sum(r['predicted_collapse'] == pred and r['collapsed'] == truth for r in rows) == result[key]
        close(statistics.mean(r['block_mask_iou'] for r in measured), result['mean_iou'])
        close(statistics.mean(r['late3_iou'] for r in measured), result['late3_iou'])
        centroids = [r['block_centroid_error_pixels'] for r in measured if r['block_centroid_error_pixels'] is not None]
        close(statistics.mean(centroids), result['centroid_error_px'])
        assert 16 - len(centroids) == result['missing_centroid_cases']
    for name, digest in json.loads((root / 'sha256.json').read_text()).items():
        assert Path(name).name == name
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    return {'games': len(games), 'decisions': len(decisions), 'actual_vla_inferences': len(all_ids), 'status': 'PASS'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--statistics', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.statistics), indent=2))
