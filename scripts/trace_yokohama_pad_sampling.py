#!/usr/bin/env python3
"""Opt-in frozen-weight ANWM sampling trace. Default is CPU input validation."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(8*1024**2), b''):
            h.update(b)
    return h.hexdigest()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n')


def timestep_map(count):
    # Pinned upstream space_timesteps(1000, str(count)), one section.
    current, result = 0., []
    for _ in range(count):
        result.append(round(current))
        current += 999/(count-1)
    return result


def selected_times(count, anchors):
    schedule = timestep_map(count)
    return sorted({min(schedule, key=lambda t: abs(t-a)) for a in anchors}, reverse=True)


def planned(protocol):
    return {(steps, seed) for steps in protocol['sampling_steps'] for seed in protocol['seeds']}


def validate(root):
    protocol = json.loads((root/'trace-protocol.json').read_text())
    if protocol['schema'] != 'pad_anwm_sampling_trace.v1':
        raise ValueError('Wrong trace protocol')
    if protocol['sampling_steps'] != [50, 250] or protocol['seeds'] != [42, 43]:
        raise ValueError('Unreviewed sampling conditions')
    if protocol['offset'] != 12 or protocol['training_allowed'] is not False:
        raise ValueError('Wrong horizon or training requested')
    for name, digest in protocol['files'].items():
        path = root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe input path')
        if sha(path) != digest:
            raise ValueError('Changed input: '+name)
    if any((root/name).exists() for name in ('training-target.png', 'training-latents.pt', 'pair.npz')):
        raise ValueError('Future-bearing diagnostic files must remain on host')
    with np.load(root/'history.npz', allow_pickle=False) as data:
        if set(data.files) != {'rgb', 'stamps_ns'}:
            raise ValueError('History must contain only observations and timestamps')
        rgb, stamps = data['rgb'], data['stamps_ns']
        if rgb.shape != (16, 224, 224, 3) or rgb.dtype != np.uint8:
            raise ValueError('Exactly 16 RGB frames required')
        if stamps.shape != (16,) or stamps.dtype.kind not in 'iu' or stamps[-1] != protocol['cutoff_stamp_ns']:
            raise ValueError('History cutoff mismatch')
        if not np.all(np.abs(np.diff(stamps)/1e9-.25) < .004001):
            raise ValueError('History cadence mismatch')
    return protocol


class TraceSampler:
    """Mirror upstream p_sample_loop, retaining outputs without extra model calls."""
    def __init__(self, diffusion, deadline=lambda: None):
        self.diffusion, self.deadline = diffusion, deadline
        self.records = []
        self.initial_noise = None
        self.conditioning = None

    def p_sample_loop(self, model, shape, noise, **kwargs):
        if self.initial_noise is not None:
            raise ValueError('Multiple sampler calls')
        self.initial_noise = noise.detach().cpu().clone()
        self.conditioning = {k: v.detach().cpu().clone() for k, v in kwargs['model_kwargs'].items()}
        previous = noise
        for i, result in enumerate(self.diffusion.p_sample_loop_progressive(model, shape, noise=noise, **kwargs)):
            self.deadline()
            index = self.diffusion.num_timesteps - 1 - i
            self.records.append(dict(index=i, respaced_t=index, original_t=self.diffusion.timestep_map[index],
                                     x_t=previous.detach().cpu().clone(),
                                     pred_xstart=result['pred_xstart'].detach().cpu().clone(),
                                     sample=result['sample'].detach().cpu().clone()))
            previous = result['sample']
        if len(self.records) != self.diffusion.num_timesteps:
            raise ValueError('Incomplete sampler trace')
        return previous


def run(root):
    protocol = validate(root)
    import torch
    from ship_anwm_server import NativeModel
    import ship_anwm as native
    from yokohama_wam_profile import ADAPTER_NAMES

    out = root/'results'
    out.mkdir(exist_ok=False)
    for name in ('forecasts', 'traces', 'frames'):
        (out/name).mkdir()
    summary = dict(status='failed', native_anwm=True, native_vla=False, training_updates=0,
                   dispatch_invoked=False, future_input=False, protocol_sha256=sha(root/'trace-protocol.json'))
    start = time.monotonic()
    records = []
    model = None

    def deadline():
        if time.monotonic()-start > protocol['model_work_seconds_max']:
            raise TimeoutError('Sampling trace deadline')

    def seed(value):
        torch.manual_seed(value)
        torch.cuda.manual_seed_all(value)

    def thash(value):
        return hashlib.sha256(value.detach().float().cpu().contiguous().numpy().tobytes()).hexdigest()

    def weights_hash(parameters):
        h = hashlib.sha256()
        for name in sorted(parameters):
            h.update(name.encode())
            h.update(parameters[name].detach().cpu().contiguous().numpy().tobytes())
        return h.hexdigest()

    def tensor(rgb):
        return torch.from_numpy(np.array(rgb, copy=True)).permute(0, 3, 1, 2).float().cuda()/127.5-1

    def save_image(value, path):
        pixels = ((value.detach().float().cpu().permute(1, 2, 0).numpy()+1)*127.5).round().clip(0, 255).astype(np.uint8)
        Image.fromarray(pixels).save(path)
        return dict(file=str(path.relative_to(out)), sha256=sha(path))

    try:
        model = NativeModel(root/'upstream', root/'assets/0200000.pth.tar', motion_adapter=root/'motion-adapter.pt')
        from anwm.diffusion import create_diffusion
        from anwm.rollout import model_forward_wrapper
        from anwm.utils import normalize_data

        parameters = dict(model.model.named_parameters())
        names = {n for n in parameters if n in ADAPTER_NAMES or n.startswith('blocks.26.')}
        saved = torch.load(root/'after-1024.pt', map_location='cpu', weights_only=True)
        if saved['schema'] != 'anwm_one_pair_parameters.v1' or saved['protocol_sha256'] != protocol['training_protocol_sha256']:
            raise ValueError('Retained weight provenance mismatch')
        if set(saved['state']) != names or saved['label'] != 'after-1024':
            raise ValueError('Retained parameter scope mismatch')
        with torch.no_grad():
            for name, value in saved['state'].items():
                if value.shape != parameters[name].shape or not torch.isfinite(value).all():
                    raise ValueError('Invalid retained weight')
                parameters[name].copy_(value)
        del saved
        model.model.requires_grad_(False)
        model.vae.requires_grad_(False)
        selected = {n: parameters[n] for n in names}
        summary['runtime_weights_before'] = weights_hash(selected)
        summary['retained_weights_sha256'] = sha(root/'after-1024.pt')
        with np.load(root/'history.npz', allow_pickle=False) as data:
            history = data['rgb'].copy()
        context, projection = tensor(history)[None], tensor(history[-1:])[None]
        action = np.zeros(4, np.float32)
        action[:3] = normalize_data(action[:3]/3.30, {'min': np.array([-2.5, -4, -3]), 'max': np.array([5, 4, 3])})
        action = torch.as_tensor(action)[None, None].cuda()
        for steps in protocol['sampling_steps']:
            sampler = create_diffusion(str(steps))
            if sampler.timestep_map != timestep_map(steps):
                raise ValueError('Unexpected upstream timestep map')
            for value in protocol['seeds']:
                deadline()
                seed(value)
                tick = time.monotonic()
                prediction = model_forward_wrapper((model.model, sampler, model.vae), context, action,
                                                   protocol['offset'], 28, 'cuda', 16, x_supervised=projection)[0]
                torch.cuda.synchronize()
                seconds = time.monotonic()-tick
                native_image = save_image(prediction, out/f'forecasts/steps-{steps}-seed-{value}-native.png')
                seed(value)
                trace = TraceSampler(sampler, deadline)
                tick = time.monotonic()
                replay = model_forward_wrapper((model.model, trace, model.vae), context, action,
                                               protocol['offset'], 28, 'cuda', 16, x_supervised=projection)[0]
                torch.cuda.synchronize()
                traced_seconds = time.monotonic()-tick
                parity = float((prediction-replay).abs().max())
                if not np.isfinite(parity) or parity > 1e-5:
                    raise ValueError('Tracing changed native output')
                replay_image = save_image(replay, out/f'forecasts/steps-{steps}-seed-{value}-replay.png')
                trace_file = out/f'traces/steps-{steps}-seed-{value}.pt'
                torch.save(dict(schema='anwm_sampler_trace.v1', steps=steps, seed=value,
                                initial_noise=trace.initial_noise, conditioning=trace.conditioning,
                                records=trace.records), trace_file)
                frames = []
                for row in trace.records:
                    if row['original_t'] not in selected_times(steps, protocol['display_anchors']):
                        continue
                    deadline()
                    with torch.no_grad(), torch.amp.autocast('cuda', dtype=torch.bfloat16):
                        decoded = model.vae.decode(row['pred_xstart'].cuda()/.18215).sample.clamp(-1, 1)[0]
                    frame = save_image(decoded, out/f"frames/steps-{steps}-seed-{value}-t-{row['original_t']}.png")
                    frames.append(dict(frame, original_t=row['original_t'], respaced_t=row['respaced_t']))
                records.append(dict(steps=steps, seed=value, native=native_image, replay=replay_image,
                                    native_seconds=seconds, traced_seconds=traced_seconds, parity_max_abs=parity,
                                    trace_file=str(trace_file.relative_to(out)), trace_sha256=sha(trace_file),
                                    trace_steps=len(trace.records), frames=frames,
                                    initial_noise_sha256=thash(trace.initial_noise),
                                    conditioning_sha256={k: thash(v) for k, v in trace.conditioning.items()}))
                write(out/'forecasts.json', records)
                print(json.dumps(dict(steps=steps, seed=value, native_seconds=seconds, parity=parity)), flush=True)
        summary['runtime_weights_after'] = weights_hash(selected)
        if summary['runtime_weights_before'] != summary['runtime_weights_after']:
            raise ValueError('Frozen runtime weights changed')
        summary['status'] = 'completed'
    finally:
        summary.update(elapsed_model_seconds=time.monotonic()-start, forecast_pairs=len(records),
                       native_calls=len(records), traced_calls=len(records))
        write(out/'summary.json', summary)
        if model is not None:
            model.model.to('cpu')
            model.vae.to('cpu')
            gc.collect()
            native.clear_cuda_workspaces(torch)
            torch.cuda.empty_cache()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--execute-inference', action='store_true')
    a = p.parse_args()
    if a.execute_inference:
        run(a.root)
    else:
        v = validate(a.root)
        print(json.dumps(dict(status='passed', gpu_requested=False, planned_forecasts=len(planned(v)))))
