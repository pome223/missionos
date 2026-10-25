#!/usr/bin/env python3
"""Explicit post-hoc audit of immutable final mission bytes with a newer checker.

Unlike the ordinary saved verifier this CLI is never runtime readiness evidence.
Every historical source blob is read from its exact Git commit and checked;
current drift must be separately reviewed and byte-pinned in an audit policy.
Only the current-source binding step is replaced, inside a saved-only context.
No record, plan, threshold, provider response or original verdict is rewritten.
"""
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'packages/missionos-core/src'),
    str(ROOT/'packages/missionos-cli/src'), str(ROOT/'packages/missionos-gateway/src')]
from scripts import run_starship_final_mission as runner  # noqa: E402
from src.runtime import starship_final_shared as shared  # noqa: E402
from src.runtime import starship_final_verifier as verifier  # noqa: E402

SCRIPT = 'scripts/audit_starship_final_saved.py'
SCHEMA = 'missionos.starship_final_historical_audit.v1'
POLICY_SCHEMA = 'missionos.starship_final_historical_audit_policy.v1'
REQUIRED_CHECKERS = frozenset({SCRIPT, 'scripts/run_starship_final_mission.py',
    'src/runtime/starship_final_shared.py', 'src/runtime/starship_final_verifier.py',
    'src/runtime/starship_restored_verifier.py', 'src/runtime/starship_restored_native_verifier.py'})


def require(value, reason):
    if not value:
        raise ValueError(reason)


def hash_value(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def relative_name(name):
    require(type(name) is str and name and not any(c in name for c in ('\x00', '\n', '\r', '\\', ':')),
        'audit_plain_relative_name')
    value = PurePosixPath(name)
    require(not value.is_absolute() and value.as_posix() == name and '..' not in value.parts,
        'audit_plain_relative_name')
    return name


def file_hash(path):
    path = Path(path)
    require(path.resolve() == path.absolute() and path.is_file() and not path.is_symlink(),
        'audit_plain_file_required')
    digest = sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(folder):
    folder = Path(folder).absolute()
    require(folder.is_dir() and folder.resolve() == folder and not folder.is_symlink(),
        'audit_plain_record_directory')
    result = {}
    for path in sorted(folder.rglob('*')):
        require(not path.is_symlink(), 'audit_record_symlink')
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), 'audit_special_record_file')
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = {'sha256': file_hash(path),
                'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
    require(result, 'audit_empty_record_directory')
    return result


def validate_policy(policy, *, plan):
    require(type(policy) is dict and set(policy) == {'schema', 'source_commit', 'plan_sha256',
        'plan_file_sha256', 'record_gzip_sha256', 'original_verification_sha256',
        'outer_receipt_sha256', 'allowed_source_drift', 'additional_runtime_sources', 'checker_sha256'},
        'audit_policy_closed_shape')
    require(policy['schema'] == POLICY_SCHEMA and policy['source_commit'] == plan['source_commit']
        and policy['plan_sha256'] == plan['plan_sha256'], 'audit_policy_original_identity')
    for name in ('plan_sha256', 'plan_file_sha256', 'record_gzip_sha256',
                 'original_verification_sha256', 'outer_receipt_sha256'):
        require(hash_value(policy[name]), 'audit_policy_pin')
    for name in ('allowed_source_drift', 'additional_runtime_sources', 'checker_sha256'):
        require(type(policy[name]) is dict and all(hash_value(v) for v in policy[name].values()),
            'audit_policy_manifest')
        for key in policy[name]:
            relative_name(key)
    require(REQUIRED_CHECKERS <= policy['checker_sha256'].keys(), 'audit_checker_pins_incomplete')
    require(set(policy['allowed_source_drift']) <= plan['source_sha256'].keys(), 'audit_drift_not_in_original_manifest')
    require(not (set(policy['additional_runtime_sources']) & plan['source_sha256'].keys()),
        'audit_added_source_already_historical')
    return policy


def historical_git_manifest(root, commit, manifest):
    """Read only Git blobs, never a checkout or an executable historical module."""
    require(type(commit) is str and re.fullmatch(r'[0-9a-f]{40}', commit), 'audit_exact_commit_required')
    require(type(manifest) is dict and manifest and len(manifest) <= 10000
        and all(hash_value(v) for v in manifest.values()), 'audit_historical_manifest')
    names = sorted(relative_name(name) for name in manifest)
    request = ''.join(f'{commit}:{name}\n' for name in names).encode()
    metadata = subprocess.run(['git', '--no-replace-objects', 'cat-file', '--batch-check'],
        input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root, timeout=60, check=False)
    require(metadata.returncode == 0 and len(metadata.stdout) <= 2*1024**2, 'audit_historical_git_read')
    headers = [line.split() for line in metadata.stdout.splitlines()]
    require(len(headers) == len(names) and all(len(h) == 3 and h[1] == b'blob'
        and h[2].isdigit() for h in headers), 'audit_git_blob_missing')
    sizes = [int(h[2]) for h in headers]
    require(all(n <= 32*1024**2 for n in sizes) and sum(sizes) <= 120*1024**2,
        'audit_git_blob_size')
    process = subprocess.run(['git', '--no-replace-objects', 'cat-file', '--batch'],
        input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root, timeout=60, check=False)
    require(process.returncode == 0 and len(process.stdout) <= 128*1024**2, 'audit_historical_git_read')
    data, cursor = process.stdout, 0
    for name in names:
        end = data.find(b'\n', cursor)
        require(end >= cursor, 'audit_git_blob_header')
        header = data[cursor:end].split()
        require(len(header) == 3 and header[1] == b'blob' and header[2].isdigit(), 'audit_git_blob_missing')
        size = int(header[2])
        cursor = end+1
        require(size <= 32*1024**2 and cursor+size < len(data), 'audit_git_blob_size')
        require(sha256(data[cursor:cursor+size]).hexdigest() == manifest[name], 'audit_historical_blob_mismatch')
        cursor += size
        require(data[cursor:cursor+1] == b'\n', 'audit_git_blob_terminator')
        cursor += 1
    require(cursor == len(data), 'audit_git_extra_blob')
    return {'source_commit': commit, 'files': len(names), 'manifest_sha256': shared.digest(manifest),
        'historical_git_blob_bytes_match': True, 'git_origin_authenticated': False}


def current_sources(root, manifest, policy):
    """Reject unreviewed drift/new runtime modules; never relabel old inputs."""
    root = Path(root)
    current = {name: file_hash(root/relative_name(name)) for name in manifest}
    drift = {name: identity for name, identity in current.items() if identity != manifest[name]}
    require(drift == policy['allowed_source_drift'], 'audit_unreviewed_source_drift')
    runtime = {p.relative_to(root).as_posix() for p in (root/'src').rglob('*.py')}
    for package in ('missionos-core', 'missionos-cli', 'missionos-gateway'):
        runtime |= {p.relative_to(root).as_posix() for p in (root/'packages'/package/'src').rglob('*.py')}
    added = {name: file_hash(root/name) for name in runtime-manifest.keys()}
    require(added == policy['additional_runtime_sources'], 'audit_unreviewed_added_runtime_source')
    checkers = {name: file_hash(root/name) for name in policy['checker_sha256']}
    require(checkers == policy['checker_sha256'], 'audit_checker_changed')
    return {'source_sha256': current, 'additional_runtime_sources': added, 'checker_sha256': checkers,
        'reviewed_drift': {name: {'historical_sha256': manifest[name], 'checker_tree_sha256': value}
            for name, value in drift.items()}}


def loaded_project_modules(root):
    """Check actual imports, including namespace paths, against this checkout."""
    roots = {'src': root/'src', 'scripts': root/'scripts'}
    for package in ('core', 'cli', 'gateway'):
        roots['missionos_'+package] = root/'packages'/('missionos-'+package)/'src'/('missionos_'+package)
    loaded = {}
    for name, module in tuple(sys.modules.items()):
        top = name.split('.', 1)[0]
        if top not in roots or module is None:
            continue
        expected = roots[top].joinpath(*name.split('.')[1:])
        require(expected.resolve().is_relative_to(root.resolve()), 'audit_foreign_loaded_module')
        filename = getattr(module, '__file__', None)
        paths = getattr(module, '__path__', None)
        if filename is None:
            require(expected.is_dir() and paths is not None and list(paths)
                and all(Path(path).resolve() == expected.resolve() for path in paths),
                'audit_foreign_loaded_module')
            loaded[name] = expected.relative_to(root).as_posix()+'/'
        else:
            wanted = expected/'__init__.py' if paths is not None else expected.with_suffix('.py')
            require(Path(filename).resolve() == wanted.resolve() and wanted.is_file()
                and wanted.resolve().is_relative_to(root.resolve()),
                'audit_foreign_loaded_module')
            if paths is not None:
                require(list(paths) and all(Path(path).resolve() == expected.resolve() for path in paths),
                    'audit_foreign_loaded_module')
            loaded[name] = wanted.relative_to(root).as_posix()
    require(loaded, 'audit_no_loaded_project_modules')
    return loaded


def audit_saved(plan, folder, policy, *, root=ROOT):
    """Return a distinct diagnostic envelope; never write an ordinary verdict."""
    validate_policy(policy, plan=plan)
    require(root.resolve() == ROOT.resolve() == shared.ROOT.resolve() == runner.ROOT.resolve(),
        'audit_foreign_checkout')
    folder = Path(folder).absolute()
    require(folder.is_relative_to(root/'output') and folder.resolve() == folder, 'audit_owned_record_folder')
    before = tree_manifest(folder)
    pins = {'plan.json': 'plan_file_sha256', 'record.json.gz': 'record_gzip_sha256',
        'verification.json': 'original_verification_sha256', 'outer-receipt.json': 'outer_receipt_sha256'}
    for name, key in pins.items():
        require(name in before and before[name]['sha256'] == policy[key], 'audit_original_record_pin')
    require(shared.wire(shared.read(folder/'plan.json')) == shared.wire(plan), 'audit_plan_not_original_file')
    require(shared.digest({k:v for k,v in plan.items() if k != 'plan_sha256'}) == plan['plan_sha256'],
        'audit_original_plan_digest')
    original_manifest = dict(plan['source_sha256'])
    for name, identity in plan['input_sha256'].items():
        require(original_manifest.get(name) == identity, 'audit_input_not_historical_source')
    history = historical_git_manifest(root, plan['source_commit'], original_manifest)
    current = current_sources(root, original_manifest, policy)
    require(shared.environment() == plan['backend'], 'audit_backend_changed')
    loaded_before = loaded_project_modules(root)
    with runner.saved_only():
        expected_inputs = runner.planning.verification_inputs(plan)
    projection_pin = shared.digest(expected_inputs)
    calls = []

    def historical_binding(inputs):
        require(shared.digest(inputs) == projection_pin, 'audit_projection_rebound')
        require(not calls, 'audit_binding_repeated')
        require(shared.environment() == plan['backend'], 'audit_backend_changed')
        loaded_project_modules(root)
        calls.append(True)
        return {'schema': 'missionos.starship_final_historical_binding.v1', **history,
            'current_source_bytes_match_historical': not current['reviewed_drift']
                and not current['additional_runtime_sources'],
            'reviewed_checker_drift': current['reviewed_drift'],
            'backend_matches': True, 'loaded_checker_paths_match': True,
            'runtime_admission': False, 'source_commit_label_authenticated': False}

    old_binding = verifier.verify_current_binding
    # No producer fields are normalized and no physics/provider operation can run.
    with runner.saved_only(), patch.object(verifier, 'verify_current_binding', historical_binding):
        checked = runner.verify_saved(plan, folder)
    require(verifier.verify_current_binding is old_binding, 'audit_binding_not_restored')
    require(calls == [True], 'audit_binding_not_checked')
    require(current_sources(root, original_manifest, policy) == current, 'audit_checker_changed_during_read')
    loaded_after = loaded_project_modules(root)
    require(all(loaded_after.get(name) == path for name, path in loaded_before.items()),
        'audit_loaded_module_changed_during_read')
    require(tree_manifest(folder) == before, 'audit_original_tree_changed')
    return {'schema': SCHEMA, 'audit_completed': True, 'consistency_passed': checked.get('passed') is True,
        'historical_plan_sha256': plan['plan_sha256'], 'historical_source_commit': plan['source_commit'],
        'audit_policy_sha256': shared.digest(policy), 'historical_source_check': history,
        'current_checker_identity': current,
        'loaded_project_modules': {'paths_match': True, 'count_before': len(loaded_before),
            'count_after': len(loaded_after), 'path_manifest_sha256': shared.digest(loaded_after)},
        'original_file_manifest_sha256': shared.digest(before),
        'original_verification_sha256': policy['original_verification_sha256'],
        'originals_unchanged': True, 'consistency_result': checked,
        'execution_code_matches_current': not current['reviewed_drift'] and not current['additional_runtime_sources'],
        'new_flight': False, 'model_calls': 0, 'plant_steps': 0,
        'runtime_admission': False, 'qualification_refreshed': False, 'execution_authority_granted': False,
        'original_verdict_replaced': False, 'new_live_success_established': False,
        'limits': ['Historical evidence evaluated by a newer explicitly pinned checker.',
            'Saved consistency, physical limits and native role coverage remain separate.',
            'This envelope cannot satisfy the ordinary runtime/readiness verifier schema.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', required=True, help='Existing immutable attempt under output/')
    parser.add_argument('--policy', required=True, help='Reviewed explicit audit policy under output/')
    parser.add_argument('--policy-file-sha256', required=True)
    parser.add_argument('--output', required=True, help='Exclusive separate directory under output/')
    args = parser.parse_args(argv)
    require(sys.dont_write_bytecode, 'audit_requires_python_B')
    folder, policy_path, output = (runner.output_path(name) for name in (args.folder, args.policy, args.output))
    require(not output.exists() and not output.is_relative_to(folder) and not folder.is_relative_to(output),
        'audit_separate_exclusive_output')
    require(hash_value(args.policy_file_sha256) and file_hash(policy_path) == args.policy_file_sha256,
        'audit_reviewed_policy_changed')
    policy, plan = shared.read(policy_path), shared.read(folder/'plan.json')
    output.mkdir(mode=0o700, parents=False)
    try:
        result = audit_saved(plan, folder, policy)
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as error:
        result = {'schema': SCHEMA, 'audit_completed': False, 'consistency_passed': False,
            'error': {'type': type(error).__name__, 'reason': str(error)[:240]},
            'new_flight': False, 'model_calls': 0, 'plant_steps': 0, 'runtime_admission': False,
            'qualification_refreshed': False, 'execution_authority_granted': False, 'original_verdict_replaced': False}
    result['audit_policy_file_sha256'] = args.policy_file_sha256
    shared.save(output/'audit.json', result)
    print(json.dumps({k:result.get(k) for k in ('schema','audit_completed','consistency_passed','runtime_admission','error')},
        sort_keys=True))
    return 0 if result['audit_completed'] and result['consistency_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
