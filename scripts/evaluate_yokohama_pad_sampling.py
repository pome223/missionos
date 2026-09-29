#!/usr/bin/env python3
"""Host-only scoring of complete native sampler traces. Targets stay on host."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.trace_yokohama_pad_sampling import planned, selected_times, sha, timestep_map, validate, write
from scripts.yokohama_pad_wam_study import detect_lead, displacement, lead_mask


def coverage(protocol, summary, records):
    expected = planned(protocol)
    actual = [(r['steps'], r['seed']) for r in records]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('Missing or duplicate native forecasts')
    if (summary['status'] != 'completed' or summary['forecast_pairs'] != len(expected)
            or summary['training_updates'] != 0 or summary['future_input'] is not False
            or not summary.get('runtime_weights_before')
            or summary['runtime_weights_before'] != summary['runtime_weights_after']):
        raise ValueError('Incomplete or changed-weight run')
    for r in records:
        if not np.isfinite(r['parity_max_abs']) or r['parity_max_abs'] > 1e-5:
            raise ValueError('Native tracing parity failed')
        ts = [v['original_t'] for v in r['frames']]
        if ts != selected_times(r['steps'], protocol['display_anchors']) or r['trace_steps'] != r['steps']:
            raise ValueError('Incomplete trace frames')
    for seed in protocol['seeds']:
        a, b = [next(r for r in records if r['seed']==seed and r['steps']==n) for n in protocol['sampling_steps']]
        if a['initial_noise_sha256'] != b['initial_noise_sha256'] or a['conditioning_sha256'] != b['conditioning_sha256']:
            raise ValueError('Different initial noise or conditioning')


def evaluate(root, results, target_path, target_latents):
    import torch
    protocol = validate(root)
    records = json.loads((results/'forecasts.json').read_text())
    summary = json.loads((results/'summary.json').read_text())
    if summary['protocol_sha256'] != sha(root/'trace-protocol.json'):
        raise ValueError('Result protocol mismatch')
    coverage(protocol, summary, records)
    target = np.asarray(Image.open(target_path).convert('RGB'))
    real, mask = detect_lead(target, 'tight'), lead_mask(target)
    if not real['present']:
        raise ValueError('Unusable host target')
    roi = mask.reshape(28, 8, 28, 8).any(axis=(1, 3))
    reference = torch.load(target_latents, map_location='cpu', weights_only=True)['training_target'].float().numpy()

    def score(receipt):
        path = results/receipt['file']
        if sha(path) != receipt['sha256']:
            raise ValueError('Changed image')
        rgb = np.asarray(Image.open(path).convert('RGB'))
        readout = detect_lead(rgb, 'tight')
        difference = np.abs(rgb.astype(float)-target.astype(float)).mean(axis=2)
        error = displacement(readout, real)
        return dict(receipt, raw_color_readout=readout, position_error_original_px=error,
                    within_6px=error is not None and error<=6, rgb_mae=float(difference.mean()),
                    target_color_mae=float(difference[mask].mean()),
                    scope='Colour location only; does not certify aircraft shape')

    output = []
    for r in records:
        trace_path = results/r['trace_file']
        if sha(trace_path) != r['trace_sha256']:
            raise ValueError('Changed trace')
        saved = torch.load(trace_path, map_location='cpu', weights_only=True)
        rows = saved['records']
        if saved['steps'] != r['steps'] or saved['seed'] != r['seed'] or len(rows) != r['steps']:
            raise ValueError('Trace identity/count mismatch')
        errors = []
        for i, row in enumerate(rows):
            if row['index'] != i or row['original_t'] != timestep_map(r['steps'])[-1-i]:
                raise ValueError('Trace time ordering changed')
            for k in ('x_t', 'pred_xstart', 'sample'):
                if row[k].shape != (1, 4, 28, 28) or not torch.isfinite(row[k]).all():
                    raise ValueError('Invalid trace tensor')
            previous = saved['initial_noise'] if i == 0 else rows[i-1]['sample']
            if not torch.equal(row['x_t'], previous):
                raise ValueError('Trace continuity failed')
            squared = (row['pred_xstart'].float().numpy()-reference)**2
            errors.append(dict(original_t=row['original_t'],
                               target_latent_mse=float(squared.mean()),
                               target_body_latent_mse=float(squared[..., roi].mean())))
        if not torch.equal(rows[-1]['sample'], rows[-1]['pred_xstart']):
            raise ValueError('Final zero-noise step changed')
        output.append(dict(steps=r['steps'], seed=r['seed'], native_seconds=r['native_seconds'],
                           parity_max_abs=r['parity_max_abs'], final=score(r['native']), replay=score(r['replay']),
                           frames=[score(f) for f in r['frames']], latent_errors=errors))
    return dict(schema='pad_anwm_sampling_evaluation.v1', complete=True, rows=output,
                runtime_weights_unchanged=True, common_initial_noise_and_conditioning=True,
                host_target_sha256=sha(target_path), target_latents_sha256=sha(target_latents),
                protocol_sha256=sha(root/'trace-protocol.json'),
                native_forecasts=4, traced_replays=4, trace_states=sum(r['trace_steps'] for r in records),
                training_updates=0, generalization_tested=False, delivery_adoption=False,
                scope='One previously trained pair, same two seeds; targets used only for host scoring')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'results', 'target', 'target-latents', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    a = p.parse_args()
    result = evaluate(a.root, a.results, a.target, a.target_latents)
    write(a.output, result)
    print(json.dumps({k: result[k] for k in ('complete', 'native_forecasts', 'traced_replays', 'trace_states')}))
