#!/usr/bin/env python3
"""Register and recheck exact saved final-mission points; no new flight or API.

This creates a finite-point companion, never refreshes the legacy 39-corridor
admission or grants dispatch. Original source-commit labels are never changed.
"""
from hashlib import sha256
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), *[str(ROOT/'packages'/p/'src') for p in
    ('missionos-core', 'missionos-cli', 'missionos-gateway')]]
from src.runtime import starship_final_shared as shared
from src.runtime import starship_final_qualification as legacy
from scripts import starship_final_qualification_chunks as q
from scripts import run_starship_final_mission as runner

SCRIPT = 'scripts/qualify_starship_final_saved.py'
CHUNKS = 'scripts/starship_final_qualification_chunks.py'
SCHEMA = 'missionos.starship_final_saved_qualification_registration.v2'
FAMILIES = {'N': 'nominal', 'T': 'return_propulsion_80', 'A': 'return_sensor_gain_80'}
POINTERS = {'state': '/handoff/snapshot/state', 'vehicle': '/handoff/snapshot/vehicle',
    'inventory': '/handoff/inventory', 'actuator_history': '/handoff/coast_history'}


def file_ref(path):
    p = shared.relative(path)
    shared.require(p.is_file() and not p.is_symlink(), 'qualification_input_not_regular')
    return {'path': path, 'sha256': sha256(p.read_bytes()).hexdigest()}


def checked_ref(ref):
    shared.require(set(ref) == {'path', 'sha256'} and file_ref(ref['path']) == ref,
        'qualification_frozen_input_changed')
    return shared.relative(ref['path'])


def identity(plan):
    b = plan['backend']
    return legacy.make_identity(source_sha256=plan['source_sha256'],
        backend={k: b[k] for k in ('python', 'platform', 'numpy', 'scipy', 'executable_sha256')},
        profile_sha256=plan['input_sha256'][shared.PROFILE],
        integration_policy_sha256=shared.digest(plan['configuration']))


def validate_plan(plan, registration):
    from src.runtime.starship_final_plan import verification_inputs
    verification_inputs(plan)
    shared.require(plan['case_id'] in FAMILIES and identity(plan) == registration['identity']
        and plan['backend'] == registration['backend'], 'qualification_execution_identity_changed')
    shared.check_manifest(plan['source_sha256'])
    shared.require(shared.environment() == registration['backend'], 'qualification_backend_changed')


def point_data(record):
    h = record['handoff']
    return {'state': h['snapshot']['state'], 'vehicle': h['snapshot']['vehicle'],
        'inventory': h['inventory'], 'actuator_history': [
            {k: row['state'][k] for k in ('time_s', 'engine_states', 'flap_angles_rad')}
            for row in h['coast_history']]}


def domain(record, case):
    h, s = record['handoff'], record['sensor_return']
    application = s['application']
    factor = (s['complete_estimate']['factor_estimate'] if case == 'T' else 1.)
    # Adopted capability comes from the numerical application receipt, never AI.
    if application is not None and case == 'T':
        factor = application['factor']
    return {'ascent_condition': 'O0', 'retained_count': h['inventory']['retained_count'],
        'released_count': h['inventory']['released_count'],
        'return_candidate_id': h['snapshot']['binding']['candidate_id'],
        'fault_family': FAMILIES[case], 'plant_capability_factor': .8 if case == 'T' else 1.,
        'sensor_gain': .8 if case == 'A' else 1., 'controller_capability_factor': factor}


def register(output, specimens):
    commit = shared.clean_commit()
    output.mkdir(exist_ok=False)
    rows, frozen = [], None
    for index, (plan_name, folder_name) in enumerate(specimens):
        plan_ref = file_ref(plan_name)
        plan = shared.read(checked_ref(plan_ref))
        value = identity(plan)
        if frozen is None:
            frozen = {'identity': value, 'backend': plan['backend']}
        shared.require(value == frozen['identity'] and plan['backend'] == frozen['backend'],
            'qualification_sample_identity_disagreement')
        validate_plan(plan, frozen)
        folder = shared.relative(folder_name)
        shared.require(folder.is_dir() and not folder.is_symlink() and folder.is_relative_to(ROOT/'output'),
            'qualification_private_record_directory_required')
        packed = file_ref(folder_name+'/record.json.gz')
        raw = runner.restore_record(checked_ref(packed))
        record = shared.parse(raw)
        d = domain(record, plan['case_id'])
        q.make_point(point_data(record))
        rows.append({'cell_id': f'{plan["mode"]}-{plan["case_id"]}-{index}', 'domain': d,
            'original_source_commit_label': plan['source_commit'], 'plan': plan_ref,
            'folder': folder_name, 'packed': packed, 'raw_sha256': sha256(raw).hexdigest(),
            'outer_receipt': file_ref(folder_name+'/outer-receipt.json')})
    shared.require({r['domain']['fault_family'] for r in rows} == set(FAMILIES.values()),
        'qualification_three_condition_census_required')
    registration = {'schema': SCHEMA, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'verification_source_commit': commit, **frozen, 'checker': file_ref(SCRIPT),
        'chunk_checker': file_ref(CHUNKS), 'cells': rows,
        'chunk_policy': {'point_schema': q.POINT_SCHEMA, 'maximum_point_bytes': q.MAXIMUM_POINT_BYTES,
            'maximum_record_bytes': q.MAXIMUM_RECORD_BYTES, 'maximum_history_rows': q.MAXIMUM_HISTORY_ROWS},
        'bounds': {'per_cell_s': 420., 'total_s': 1800., 'minimum_free_bytes': 10*2**30,
            'automatic_retry': False, 'maximum_new_flights': 0, 'maximum_provider_calls': 0},
        'scope': 'new_saved_only_exact_pre_fault_dispatch_point_requalification',
        'point_semantics': 'actual_accepted_dispatch_before_return_fault_injection_with_named_subsequent_fault_branch',
        'legacy39_refreshed': False, 'runtime_admission': False, 'prior_records_relabelled': False}
    registration['registration_sha256'] = shared.digest(registration)
    shared.save(output/'registration.json', registration)
    return registration


def load_registration(path):
    value = shared.read(path)
    shared.require(value.get('schema') == SCHEMA and value.get('registration_sha256') ==
        shared.digest({k: v for k, v in value.items() if k != 'registration_sha256'}),
        'qualification_registration_changed')
    checked_ref(value['checker'])
    checked_ref(value['chunk_checker'])
    shared.check_manifest(value['identity']['source_sha256'])
    shared.require(value['chunk_policy'] == {'point_schema': q.POINT_SCHEMA,
        'maximum_point_bytes': q.MAXIMUM_POINT_BYTES, 'maximum_record_bytes': q.MAXIMUM_RECORD_BYTES,
        'maximum_history_rows': q.MAXIMUM_HISTORY_ROWS}, 'qualification_chunk_policy_changed')
    shared.require(shared.clean_commit() == value['verification_source_commit'],
        'qualification_committed_verification_source_changed')
    return value


def check_cell(registration, row):
    plan = shared.read(checked_ref(row['plan']))
    checked_ref(row['outer_receipt'])
    validate_plan(plan, registration)
    raw = runner.restore_record(checked_ref(row['packed']))
    shared.require(sha256(raw).hexdigest() == row['raw_sha256'], 'qualification_raw_record_changed')
    # The production saved verifier checks all external artifact pins, the
    # actual trace, plant/controller separation, fault assignment, host records
    # and contact; producer booleans or cached verification.json are not used.
    verified = runner.verify_saved(plan, shared.relative(row['folder']))
    shared.require(verified['passed'] is True and not verified['issues'],
        'qualification_saved_mission_verification_failed')
    record = shared.parse(raw)
    shared.require(domain(record, plan['case_id']) == row['domain'], 'qualification_sample_domain_changed')
    physical = verified['outcome']
    shared.require(physical['all_required_physical_gates_met'] is True,
        'qualification_physical_outcome_failed')
    c = physical['contact']
    report = {'schema': q.CHECK_SCHEMA, 'record_sha256': row['raw_sha256'],
        'identity_sha256': shared.digest(registration['identity']), 'cell_id': row['cell_id'],
        'domain': row['domain'], 'point_summary': q.make_point(point_data(record)), 'point_pointers': POINTERS,
        'record_checks_passed': True, 'actual_state_continuity_verified': True,
        'controller_plant_separation_verified': True, 'prediction_only': False,
        'contact': {'speed_mps': c['speed_mps'], 'tilt_deg': c['tilt_deg'],
            'body_rate_rad_s': c['body_rate_rad_s'], 'propellant_kg': c['reserve_kg']},
        'new_flights_during_check': 0, 'new_model_calls_during_check': 0}
    return report, verified


def execute(registration_path, expected_hash):
    registration = load_registration(registration_path)
    shared.require(expected_hash == registration['registration_sha256'], 'qualification_explicit_plan_hash_required')
    root = registration_path.parent
    shared.save(root/'attempt.json', {'registration_sha256': expected_hash,
        'started_utc': datetime.now(timezone.utc).isoformat(), 'automatic_retry': False})
    started, utc = time.monotonic(), time.time()
    completed, raw_records, reports = [], {}, {}
    result = {'schema': 'missionos.starship_final_saved_qualification_result.v1', 'passed': False,
        'registration_sha256': expected_hash, 'completed_cells': completed,
        'new_flights': 0, 'provider_calls': 0, 'legacy39_refreshed': False, 'runtime_admission': False}
    try:
        for index, row in enumerate(registration['cells']):
            elapsed = max(time.monotonic()-started, time.time()-utc)
            remaining = registration['bounds']['total_s']-elapsed
            shared.require(remaining > 0 and shutil.disk_usage(ROOT).free >= registration['bounds']['minimum_free_bytes'],
                'qualification_time_or_capacity_preflight_failed')
            command = [sys.executable, str(ROOT/SCRIPT), 'check-cell', '--registration',
                str(registration_path), '--index', str(index)]
            with (root/(row['cell_id']+'.log')).open('xb') as log:
                done = subprocess.run(command, cwd=ROOT, env={k: v for k, v in os.environ.items()
                    if k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR')}, stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(remaining, registration['bounds']['per_cell_s']), check=False)
            shared.require(done.returncode == 0, 'qualification_independent_cell_failed:'+row['cell_id'])
            report = shared.read(root/(row['cell_id']+'-checked.json'), 32*2**20)
            reports[row['cell_id']] = report
            raw_records[row['cell_id']] = runner.restore_record(checked_ref(row['packed']))
            completed.append(row['cell_id'])
        cells = [{k: row[k] for k in ('cell_id', 'domain')} for row in registration['cells']]
        def independent(raw, *, expected_identity, expected_cell):
            report = reports[expected_cell['cell_id']]
            shared.require(report['record_sha256'] == sha256(raw).hexdigest()
                and report['identity_sha256'] == shared.digest(expected_identity),
                'qualification_child_receipt_binding_changed')
            return report
        certificate = q.build_certificate(expected_identity=registration['identity'], required_cells=cells,
            records_raw=raw_records, verify_record=independent)
        shared.require(certificate['qualification_complete'], 'qualification_complete_census_failed')
        shared.check_manifest(registration['identity']['source_sha256'])
        checked_ref(registration['checker'])
        checked_ref(registration['chunk_checker'])
        shared.require(shared.environment() == registration['backend'], 'qualification_backend_changed_at_finish')
        elapsed = max(time.monotonic()-started, time.time()-utc)
        shared.require(elapsed <= registration['bounds']['total_s'], 'qualification_total_time_exceeded')
        certificate.update(verification_registration_sha256=expected_hash,
            verification_source_commit=registration['verification_source_commit'],
            verification_source_sha256={registration[k]['path']: registration[k]['sha256']
                for k in ('checker', 'chunk_checker')},
            verification_backend=registration['backend'])
        result.update(passed=True, certificate_sha256=shared.save(root/'certificate.json', certificate),
            qualification_scope=certificate['qualification_scope'], elapsed_s=elapsed)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        result['failure'] = {'type': type(error).__name__, 'reason': str(error)[:400]}
    finally:
        result['elapsed_s'] = max(time.monotonic()-started, time.time()-utc)
        shared.save(root/'result.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('register'); a.add_argument('--output', required=True)
    a.add_argument('--specimen', nargs=2, action='append', required=True, metavar=('PLAN', 'FOLDER'))
    a = sub.add_parser('inspect-point'); a.add_argument('--record', required=True); a.add_argument('--expected-record-sha256', required=True); a.add_argument('--output', required=True)
    a = sub.add_parser('execute'); a.add_argument('--registration', required=True); a.add_argument('--allow-registration-sha256', required=True)
    a = sub.add_parser('check-cell'); a.add_argument('--registration', required=True); a.add_argument('--index', type=int, required=True)
    args = p.parse_args()
    if args.command == 'inspect-point':
        path = shared.relative(args.record)
        raw = runner.restore_record(path)
        shared.require(sha256(raw).hexdigest() == args.expected_record_sha256, 'qualification_inspect_raw_changed')
        result = {'schema': 'missionos.starship_final_chunked_point_smoke.v1',
            'record_sha256': args.expected_record_sha256, 'point': q.make_point(point_data(shared.parse(raw))),
            'provider_calls': 0, 'new_flights': 0, 'qualification_complete': False}
        shared.save(shared.relative(args.output), result)
        print(json.dumps({'record_sha256': result['record_sha256'],
            'rows': result['point']['history']['row_count'],
            'chunks': len(result['point']['history']['chunks']), 'qualification_complete': False}))
        return 0
    if args.command == 'register':
        result = register(shared.relative(args.output), args.specimen)
        print(json.dumps({'registration_sha256': result['registration_sha256'], 'cells': len(result['cells']),
            'new_flights': 0, 'provider_calls': 0}))
        return 0
    path = Path(args.registration).resolve()
    shared.require(path.is_relative_to(ROOT/'output') and not path.is_symlink(), 'qualification_private_registration_required')
    if args.command == 'execute':
        result = execute(path, args.allow_registration_sha256)
        print(json.dumps(result, sort_keys=True)); return 0 if result['passed'] else 2
    registration = load_registration(path)
    report, verified = check_cell(registration, registration['cells'][args.index])
    shared.save(path.parent/(report['cell_id']+'-checked.json'), report)
    shared.save(path.parent/(report['cell_id']+'-verification.json'), verified)
    print(json.dumps({'cell_id': report['cell_id'], 'passed': True, 'new_flights': 0, 'provider_calls': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
