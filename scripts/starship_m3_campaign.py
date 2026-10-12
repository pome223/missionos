#!/usr/bin/env python3
"""Freeze or independently verify the M3 campaign; never launch or retry flights.

Private Gateway keys are read only in memory for local receipt verification.
All output is metrics, hashes and limitations, never credentials or model text.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input
from src.runtime.starship_m3_completion import (FLIGHT_CASES, FLIGHT_MATRICES, build_protocol,
    produce_ascent_runtime_evidence, verify_completion)
from src.runtime.starship_m3_live import SCENARIOS, contract, sources
from src.runtime.starship_replanning import digest, source_hashes as return_sources


def freeze(directory, *, matrix="original_v1"):
    directory = Path(directory)
    if (directory / 'protocol.json').exists():
        raise ValueError('campaign_already_frozen')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = sources()
    bindings = {role: {'authority': contract('live', case),
        'scenario': next(name for name, registered in SCENARIOS.items() if registered == case)}
        for role, case in FLIGHT_MATRICES[matrix].items()}
    protocol = build_protocol(bindings, source_sha256=source, return_source_sha256=return_sources(), matrix=matrix)
    write_verified_input(directory / 'protocol.json', protocol)
    checksum = digest(protocol)
    (directory / 'protocol.sha256').write_text(checksum + '\n')
    write_verified_input(directory / 'ascent-runtime-evidence.json',
                         produce_ascent_runtime_evidence(source))
    return {'protocol_sha256': checksum, 'roles': list(bindings),
            'flights_started': 0, 'model_calls': 0}


def verify(directory, *, verification_name="completion.json"):
    directory = Path(directory)
    if Path(verification_name).name != verification_name or not verification_name.endswith(".json"):
        raise ValueError("verification_output_must_be_a_json_basename")
    output = directory / verification_name
    if output.exists():
        raise ValueError("verification_output_already_exists_choose_a_new_name")
    protocol = json.loads((directory / 'protocol.json').read_text())
    expected = (directory / 'protocol.sha256').read_text().strip()
    if digest(protocol) != expected or sources() != protocol['source_sha256']:
        raise ValueError('campaign_protocol_or_current_source_changed')
    studies, gateway, keys, missing = {}, {}, {}, []
    for role in FLIGHT_CASES:
        # Each role has one isolated Gateway, one session, one consumed grant.
        run_root = directory / role / 'gateway' / 'runs'
        sessions = sorted(run_root.glob('session-*.json'))
        if len(sessions) != 1:
            missing.append(role + ':one_session_required')
            continue
        state = json.loads(sessions[0].read_text())
        run_id = state.get('execution', {}).get('run_id')
        run_dir = run_root / ('run-' + str(run_id))
        if not (run_dir / 'result.json').is_file() or not (run_dir / 'results/study.json').is_file():
            missing.append(role + ':no_completed_record')
            continue
        required_private = [run_root / 'approval-signing.key']
        if role == 'scope_amendment':
            required_private.extend([run_dir / 'inflight-binding.json',
                run_dir / 'inflight-host-result.json',
                directory / role / 'operator/operator-http.json'])
        if any(not path.is_file() for path in required_private):
            missing.append(role + ':private_binding_record_missing')
            continue
        studies[role] = (run_dir / 'results/study.json').read_bytes()
        gateway[role] = {key: state[key] for key in ('plan', 'approval', 'execution')}
        gateway[role]['receipt'] = json.loads((run_dir / 'result.json').read_text())
        keys[role] = (run_root / 'approval-signing.key').read_bytes()
        if role == 'scope_amendment':
            signed_binding = json.loads((run_dir / 'inflight-binding.json').read_text())
            gateway[role]['inflight_verification_inputs'] = {
                'expected_inflight_binding': {k: v for k, v in signed_binding.items() if k != 'signature'},
                'expected_host_evidence': json.loads((run_dir / 'inflight-host-result.json').read_text())}
            gateway[role]['operator_http_record'] = json.loads(
                (directory / role / 'operator/operator-http.json').read_text())
    runtime = {'ascent': json.loads((directory / 'ascent-runtime-evidence.json').read_text())}
    scope_file = directory / 'scope-http/scope-runtime-evidence.json'
    if scope_file.exists():
        runtime['scope'] = json.loads(scope_file.read_text())
    result = verify_completion(protocol, studies, runtime, expected_protocol_sha256=expected,
                               gateway_records=gateway, host_signing_key=keys)
    result['missing_private_records'] = missing
    result['protocol_sha256'] = expected
    result['raw_study_sha256'] = {role: sha256(raw).hexdigest() for role, raw in studies.items()}
    write_verified_input(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'verify'))
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--matrix', choices=tuple(FLIGHT_MATRICES), default='original_v1',
                        help='Registered fresh-flight matrix; never changes a frozen campaign.')
    parser.add_argument('--verification-output', default='completion.json',
                        help='New result filename within the campaign; existing evidence is never replaced.')
    args = parser.parse_args()
    result = (freeze(args.output_dir, matrix=args.matrix) if args.action == 'freeze' else
              verify(args.output_dir, verification_name=args.verification_output))
    print(json.dumps(result, indent=2))
    return 0 if args.action == 'freeze' or result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
