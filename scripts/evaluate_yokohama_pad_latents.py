#!/usr/bin/env python3
"""Host-only readout of the one-pair diagnostic, never a delivery adoption gate."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.diagnose_yokohama_pad_latents import expected_forecasts, sha, write
from scripts.yokohama_pad_wam_study import detect_lead, displacement, lead_mask


def evaluate(root, results):
    protocol = json.loads((root / 'latent-protocol.json').read_text())
    summary = json.loads((results / 'summary.json').read_text())
    records = json.loads((results / 'forecasts.json').read_text())
    expected = expected_forecasts(protocol)
    actual = [(r['step'], r['seed'], r['route']) for r in records]
    complete = (summary['status'] == 'completed' and summary['updates'] == protocol['steps']
                and summary['forecasts'] == len(records)
                and summary['protocol_sha256'] == sha(root / 'latent-protocol.json')
                and set(actual) == expected and len(actual) == len(expected))
    target = np.asarray(Image.open(root / 'training-target.png').convert('RGB'))
    real = detect_lead(target, 'tight')
    if not real['present']:
        raise ValueError('Unusable target readout')
    roi = lead_mask(target)
    rows = []
    for r in records:
        path = results / r['file']
        if sha(path) != r['sha256']:
            raise ValueError('Forecast changed')
        image = np.asarray(Image.open(path).convert('RGB'))
        readout = detect_lead(image, 'tight')
        error = displacement(readout, real)
        diff = np.abs(image.astype(float) - target.astype(float)).mean(axis=2)
        rows.append(dict(r, readout=readout, position_error_px=error,
                         within_6px=error is not None and error <= 6,
                         rgb_mae=float(diff.mean()), roi_rgb_mae=float(diff[roi].mean()),
                         background_rgb_mae=float(diff[~roi].mean())))
    weights = {}
    for label in ('initial_weights', 'final_weights'):
        receipt = summary.get(label)
        weights[label] = bool(receipt and (results / receipt['file']).exists()
                              and sha(results / receipt['file']) == receipt['sha256'])
    parity = all(r['parity_max_abs'] is not None and r['parity_max_abs'] <= 1e-5
                 for r in rows if r['route'] == 'fresh_replay')
    complete = complete and all(weights.values()) and parity
    vae = {}
    for name in ('target-mean', 'target-sample', 'latest-sample'):
        path = results / 'vae' / (name + '.png')
        found = detect_lead(np.asarray(Image.open(path).convert('RGB')), 'tight')
        vae[name] = dict(readout=found, target_error_px=displacement(found, real))
    fit = {route: complete and all(r['within_6px'] for r in rows
                                  if r['step'] == protocol['steps'] and r['route'] == route)
           for route in ('native', 'cached')}
    if not complete:
        diagnosis = 'incomplete_or_invalid_run'
    elif fit['native']:
        diagnosis = 'one_pair_native_fit_demonstrated_generalization_unchecked'
    elif fit['cached']:
        diagnosis = 'conditioning_cache_difference_affects_this_pair'
    else:
        diagnosis = 'cache_match_alone_did_not_restore_this_pair'
    return dict(schema='pad_anwm_one_pair_evaluation.v1', complete=complete,
                diagnosis=diagnosis, fit=fit, weights_retained=weights,
                native_replay_parity=parity, vae=vae, rows=rows,
                delivery_adoption=False, generalization_tested=False,
                missing=sorted(expected-set(actual)),
                scope='One training pair, fixed action and 3 s horizon, two generation seeds')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--results', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = evaluate(a.root, a.results)
    write(a.output, result)
    print(json.dumps({k: result[k] for k in ('complete', 'diagnosis', 'fit', 'weights_retained')}))
    raise SystemExit(0 if result['complete'] else 1)
