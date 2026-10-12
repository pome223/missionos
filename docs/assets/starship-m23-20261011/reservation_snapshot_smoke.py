"""One saved-data snapshot-to-real-pipe smoke; no flight/provider constructor.

The executor wrapper is a fixture initialized with saved candidate/booking data.
The synthetic wrapper clock is the saved candidate's observed-origin time; this
is not a new flight observation or an approval/dispatch/physics result.
"""
from copy import deepcopy
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def reconstruct_saved_candidate(evidence):
    """Same compact construction as prediction_job; never invoke prediction_job."""
    mission = evidence / 'flight/mission'
    inputs = json.loads((mission / 'inputs.json').read_text())
    handoff = json.loads((mission / 'handoff.json').read_text())
    opportunities = json.loads((mission / 'return/opportunities.json').read_text())
    fields = ('time_s', 'r_eci_m', 'v_eci_mps', 'q_body_to_eci', 'omega_body_rad_s', 'propellant_kg')
    trials, artifact_hashes = [], {}
    for n, scale in zip(range(4, 8), (1., 1., 1., .5)):
        path = mission / f'return/forecast-{n}.json'
        raw = path.read_bytes()
        result = json.loads(raw)
        artifact_hashes[path.relative_to(evidence).as_posix()] = sha256(raw).hexdigest()
        trials.append({
            'origin': result['origin'], 'integration_scale': scale,
            'final_state': result['final_state'], 'outcome': result['outcome'],
            'return_time_s': result['return_time_s'], 'wall_time_s': result['wall_time_s'],
            'prediction_sha256': sha256(raw).hexdigest(),
            'coast': [{k: s[k] for k in fields} for s in result['samples'] if s['phase'] == 'orbital_coast'],
        })
    candidate = {
        'id': 'next_orbit', 'return_time_s': opportunities['return_times_s']['next_orbit'],
        'scheduled_s': opportunities['original_scheduled_s'],
        'envelope_sha256': sha256(canonical(inputs['authority']['return_authority'])).hexdigest(),
        'source_sha256': inputs['return_source_sha256'], 'notice_sequence': 1,
        'signs': [0, -1, 1], 'trials': trials,
        'deployment_binding': {k: handoff[k] for k in ('handoff_sha256', 'authority_sha256',
            'profile_sha256', 'generation', 'released_count', 'retained_count')},
    }
    return candidate, artifact_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # The exclusive report handle prevents an accidental second run overwriting
    # the first runtime result. No run retry is performed by this script.
    with args.output.open('x') as report:
        sys.path[:0] = [str(args.root), str(args.root/'packages/missionos-core/src')]
        import src.runtime.starship_inflight_executor as runtime
        from src.runtime.starship_inflight_runtime import approval_pipe

        candidate, artifacts = reconstruct_saved_candidate(args.evidence)
        raw = canonical(candidate)
        identity = sha256(raw).hexdigest()
        saved = json.loads((args.evidence / 'telemetry-summary.json').read_text())['latest']
        reservation = saved['reservation']
        assert identity == reservation['candidate_sha256']
        assert identity == 'd6257f413d19e141b704b0347ed96a4a7ed279a901eb4d2059494bbc73ed8666'
        assert len(raw) == 5271754
        assert candidate['return_time_s'] == reservation['return_time_s']
        observed = deepcopy(candidate['trials'][0]['origin'])
        actor = object.__new__(runtime.InflightReplanningExecutor)
        actor.selected, actor.revision = reservation['candidate_id'], reservation['plan_revision']
        actor.candidates = {actor.selected: candidate}
        actor.revisions = [{'revision': actor.revision, 'candidate_id': actor.selected,
            'candidate_sha256': identity, 'candidate_snapshot': deepcopy(candidate)}]
        actor.times = {actor.selected: candidate['return_time_s']}
        actor.s = SimpleNamespace(time_s=observed['state']['time_s'])
        actor.observation = lambda: deepcopy(observed)
        actor.active_scope, actor.scope_revision = deepcopy(saved['scope']), saved['revision']
        actor._approval_run_active, actor._snapshot_failed, actor.dispatch = True, False, None
        actor._approval_clock = lambda: 100.0
        actor._approval_snapshots = []
        actor.events = []
        actor.event = lambda *_a, **_k: None
        reader, writer = approval_pipe()
        actor._snapshot_sink = writer.send
        original_digest, hashes = runtime.digest, []
        def guarded_digest(value):
            assert value is not candidate
            if isinstance(value, dict):
                assert 'trials' not in value and 'candidate_snapshot' not in value
            hashes.append(len(canonical(value)))
            return original_digest(value)
        try:
            runtime.digest = guarded_digest
            published = actor._publish_snapshot()
            writer.flush()
            received = reader.poll()
            assert received == published == actor._approval_snapshots[-1]
            assert received['reservation'] == reservation
            assert received['reservation_sha256'] == original_digest(reservation)
            assert len(hashes) == 2 and max(hashes) < 16384
            assert not actor._snapshot_failed
            assert reader.poll() is None
        finally:
            runtime.digest = original_digest
            reader.close()
            writer.close()
        result = {
            'schema': 'missionos.m3_reservation_snapshot_pipe_smoke.v1', 'passed': True,
            'invocation_boundary': 'current InflightReplanningExecutor._publish_snapshot -> approval_snapshot -> real anonymous ApprovalPipeWriter/Reader',
            'executor_initialization': 'fixture object with saved candidate, saved booking metadata and saved observed-origin telemetry; no simulator constructor',
            'observed_time_s': actor.s.time_s, 'fixture_wall_time_s': 100.0,
            'candidate_sha256': identity, 'matches_saved_booking': True,
            'canonical_candidate_bytes': len(raw),
            'compact_coast_samples': [len(t['coast']) for t in candidate['trials']],
            'snapshot_digest_calls': len(hashes), 'snapshot_digest_input_bytes': hashes,
            'full_candidate_digest_calls_inside_snapshot': 0,
            'snapshot_wire_bytes': len(canonical(received)), 'pipe_frames_received': 1,
            'saved_snapshot_equals_received': received == actor._approval_snapshots[-1],
            'source_sha256': {p: sha256((args.root/p).read_bytes()).hexdigest() for p in
                ('src/runtime/starship_inflight_executor.py','src/runtime/starship_inflight_runtime.py')},
            'saved_artifact_sha256': artifacts, 'model_calls': 0, 'simulator_runs': 0,
            'new_approvals': 0, 'burn_dispatches': 0, 'retry_performed': False,
            'performance_benchmark': False, 'full_flight_completion_claimed': False,
        }
        report.write(json.dumps(result, sort_keys=True, indent=2)+'\n')
        report.flush()
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
