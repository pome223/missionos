"""Verify curated simulation records and public artifact integrity; never execute a model."""
from pathlib import Path
import hashlib
import json
import math
import re
import runpy

ROOT = Path(__file__).resolve().parent


def verify(root=ROOT):
    manifest = json.loads((root / 'manifest.json').read_text())
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()
              and p.name != 'manifest.json' and '__pycache__' not in p.parts}
    assert actual == set(manifest), 'Unexpected or missing public file'
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    data = json.loads((root / 'data/replay.json').read_text())
    results = json.loads((root / 'data/results.json').read_text())
    assert {c['id'] for c in data['cases']} == {'moving', 'agent'}
    for c in data['cases']:
        r = results[c['id']]
        m, s, end = c['metrics'], c['samples'], r['terminal_state']
        assert all(b['t'] > a['t'] for a, b in zip(s, s[1:]))
        assert all(math.isfinite(v) for p in s for v in [p['t'], *p['xy'], p['speed']])
        assert len(s) == m['telemetry_samples'] + 1
        assert s[-1]['xy'] == end['ground_truth_xy']
        assert s[-1]['t'] == m['sim_duration_s'] == end['sim_time_s']
        assert m['terminal_speed_mps'] == end['measured_speed_mps']
        assert math.isclose(m['terminal_home_error_m'], math.dist(end['ground_truth_xy'], [-2.5, 0]))
        assert m['physics_steps'] == end['physics_steps']
        assert m['policy_calls'] == end['policy_inference_calls']
        assert m['status'] == r['status'] == 'completed'
        assert r['receipt']['received'] and r['completion_claimed']
        assert r['completion_scope'] == 'simulated_delivery_and_return'
        assert not r['physical_execution_invoked'] and not end['safety_violation_observed']
        assert end['obstacle_contacts'] == []
        assert m['judgments'] == len(c['decisions']) == len(r['decision_chain'])
        assert c['legs'] == r['legs'] and c['decisions'] == r['decision_chain']
        events = [e['event'] for e in c['events']]
        assert events.index('terminal_hold_verified') < events.index('mission_completed')
        assert m['yield_count'] == r['dynamic_avoidance']['yield_count']
        assert m['min_clearance_m'] == r['dynamic_avoidance']['minimum_conservative_clearance_m']
        if c['id'] == 'agent':
            assert r['llm_judgment_invoked']
            assert [x['proposal']['action'] for x in c['decisions']] == ['wait', 'reroute']
            assert any(x['source_decision_id'] == 'decision_2' and x['verified'] for x in c['legs'])
            assert any(not x['verified'] for x in c['legs']), 'Preserve the interrupted attempt'
            assert all(x['rules'] == 'allowed' for x in c['decisions'])
        else:
            assert not r['llm_judgment_invoked'] and m['judgments'] == 0 and m['yield_count'] == 2
        assert 'source_run_id' not in c
    for name in ['index.html', 'index-en.html']:
        page = (root / name).read_text()
        embedded = re.search(r'<script type="application/json" id="replay-data">(.*?)</script>', page, re.S).group(1)
        assert json.loads(embedded) == data
        assert page.count('<video ') == 2
        for link in re.findall(r'(?:href|src|poster)="([^"]+)"', page):
            if not link.startswith(('http://', 'https://', '#')):
                assert (root / link).exists(), link
    runpy.run_path(str(root / 'verify_translation.py'))
    print(json.dumps({'status': 'passed', 'files': len(manifest), 'cases': 2,
                      'scope': 'Published artifact integrity and curated-record consistency; no simulator rerun'}))


if __name__ == '__main__':
    verify()
