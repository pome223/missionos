#!/usr/bin/env python3
"""Opt-in, one-pair ANWM diagnosis of cached versus runtime VAE conditioning.

An unmodified native rollout supplies the fresh latents, initial noise and RNG
state. Replaying it checks parity; replacing only the conditioning with the
training cache isolates that difference. Teacher-forced denoising is labelled
separately and is never counted as a forecast. No aircraft or VLA calls.
"""
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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def expected_forecasts(protocol):
    return {(step, seed, route) for step in protocol['evaluation_at_steps']
            for seed in protocol['forecast_seeds']
            for route in ('native', 'fresh_replay', 'cached')}


def validate(root):
    protocol = json.loads((root / 'latent-protocol.json').read_text())
    if protocol['schema'] != 'pad_anwm_one_pair_latent.v1':
        raise ValueError('Wrong protocol')
    if protocol['steps'] != 1024 or protocol['evaluation_at_steps'] != [0, 256, 1024]:
        raise ValueError('Unreviewed training schedule')
    if protocol['forecast_seeds'] != [42, 43] or protocol['offset'] != 12:
        raise ValueError('Unreviewed forecast conditions')
    for rel, digest in protocol['files'].items():
        path = root / rel
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe input path')
        if sha(path) != digest:
            raise ValueError('Changed input: ' + rel)
    with np.load(root / 'pair.npz', allow_pickle=False) as data:
        hist, stamps = data['rgb'], data['stamps_ns']
        if hist.dtype != np.uint8 or hist.shape != (16, 224, 224, 3):
            raise ValueError('Exactly 16 RGB history frames required')
        if stamps.shape != (16,) or stamps[-1] != protocol['cutoff_stamp_ns']:
            raise ValueError('History cutoff mismatch')
        if not np.all(np.abs(np.diff(stamps) / 1e9 - 0.25) < .004001):
            raise ValueError('History cadence mismatch')
    if Image.open(root / 'training-target.png').size != (224, 224):
        raise ValueError('Target shape mismatch')
    return protocol


class CaptureDiffusion:
    """Capture exactly what the upstream wrapper passes to its sampler."""
    def __init__(self, diffusion, get_rng):
        self.diffusion, self.get_rng = diffusion, get_rng
        self.receipt = None

    def p_sample_loop(self, model, shape, noise, **kwargs):
        if self.receipt is not None:
            raise ValueError('Multiple sampler calls')
        self.receipt = dict(shape=shape, noise=noise.clone(), rng=self.get_rng(),
                            kwargs={k: v.detach().clone() for k, v in kwargs['model_kwargs'].items()})
        return self.diffusion.p_sample_loop(model, shape, noise, **kwargs)


def run(root):
    protocol = validate(root)
    import torch
    from ship_anwm_server import NativeModel
    import ship_anwm as native
    from yokohama_wam_profile import ADAPTER_NAMES

    out = root / 'results'
    out.mkdir(exist_ok=False)
    for name in ('weights', 'vae', 'forecasts', 'teacher'):
        (out / name).mkdir()
    began = time.monotonic()
    model = NativeModel(root / 'upstream', root / 'assets/0200000.pth.tar',
                        motion_adapter=root / 'motion-adapter.pt')
    from anwm.diffusion import create_diffusion
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data

    with np.load(root / 'pair.npz', allow_pickle=False) as data:
        hist = data['rgb'].copy()
    target = np.asarray(Image.open(root / 'training-target.png').convert('RGB'))
    records, losses, diagnostics = [], [], []
    summary = dict(status='failed', native_anwm=True, native_vla=False,
                   dispatch_invoked=False, protocol_sha256=sha(root / 'latent-protocol.json'),
                   sample_id=protocol['sample_id'], base_sha256=native.MODEL_SHA256,
                   adapter_sha256=sha(root / 'motion-adapter.pt'))

    def deadline():
        if time.monotonic() - began > protocol['model_work_seconds_max']:
            raise TimeoutError('One-pair model deadline')

    def seed(value):
        torch.manual_seed(value)
        torch.cuda.manual_seed_all(value)

    def tensor(rgb):
        return torch.from_numpy(np.array(rgb, copy=True)).permute(0, 3, 1, 2).float().cuda()/127.5-1

    def pixels(value):
        return ((value.detach().float().cpu().permute(1, 2, 0).numpy()+1)*127.5).round().clip(0, 255).astype(np.uint8)

    def save_image(value, path):
        Image.fromarray(pixels(value)).save(path)

    def decode(value):
        with torch.no_grad(), torch.amp.autocast('cuda', dtype=torch.bfloat16):
            return model.vae.decode(value / .18215).sample.clamp(-1, 1)

    def snapshot(label, parameters):
        path = out / 'weights' / (label + '.pt')
        torch.save(dict(schema='anwm_one_pair_parameters.v1',
                        state={n: p.detach().cpu().clone() for n, p in parameters},
                        protocol_sha256=summary['protocol_sha256'], label=label), path)
        return dict(file=str(path.relative_to(out)), sha256=sha(path), bytes=path.stat().st_size)

    action = np.zeros(4, np.float32)
    action[:3] = normalize_data(action[:3]/3.30,
                               {'min': np.array([-2.5, -4, -3]), 'max': np.array([5, 4, 3])})
    action = torch.as_tensor(action)[None].cuda()
    context_pixels, projection_pixels = tensor(hist)[None], tensor(hist[-1:])[None]
    rel_t = torch.tensor([protocol['offset']/128.0], device='cuda')
    for name, p in model.model.named_parameters():
        p.requires_grad_(name in ADAPTER_NAMES or name.startswith('blocks.26.'))
    model.vae.requires_grad_(False)
    parameters = [(n, p) for n, p in model.model.named_parameters() if p.requires_grad]
    optimizer = None
    try:
        summary['trainable_parameters'] = sum(p.numel() for _, p in parameters)
        summary['initial_weights'] = snapshot('initial', parameters)
        seed(protocol['cache_seed'])
        with torch.no_grad(), torch.amp.autocast('cuda', dtype=torch.bfloat16):
            context_dist = model.vae.encode(tensor(hist)).latent_dist
            context = context_dist.sample() * .18215
            projection_dist = model.vae.encode(tensor(hist[-1:])).latent_dist
            projection = projection_dist.sample() * .18215
            target_dist = model.vae.encode(tensor(target[None])).latent_dist
            latent = target_dist.sample() * .18215
            target_mean = target_dist.mode() * .18215
        cached = dict(y=action, x_cond=context[None], x_supervised=projection, rel_t=rel_t)
        torch.save(dict(cached={k: v.detach().cpu() for k, v in cached.items()},
                        training_target=latent.detach().cpu()), out / 'training-latents.pt')
        save_image(decode(latent)[0], out / 'vae/target-sample.png')
        save_image(decode(target_mean)[0], out / 'vae/target-mean.png')
        save_image(decode(projection)[0], out / 'vae/latest-sample.png')
        diffusion = create_diffusion('')
        sampler = create_diffusion(str(protocol['evaluation_steps']))
        # This mask is for diagnostic errors only. It adds no training loss.
        r, g, b = [target[..., k].astype(int) for k in range(3)]
        mask = (r>150)&(g>60)&(g<200)&(b<90)&(r-b>110)
        roi = torch.nn.functional.max_pool2d(torch.as_tensor(mask.astype(np.float32))[None, None].cuda(), 8)

        def teacher(step):
            # Correct future is deliberately supplied here: this is not a forecast.
            seed(77)
            for time_step in (100, 500, 900):
                t = torch.tensor([time_step], device='cuda')
                noise = torch.randn_like(latent)
                x_t = diffusion.q_sample(latent, t, noise=noise)
                with torch.no_grad(), torch.amp.autocast('cuda', dtype=torch.bfloat16):
                    pred = diffusion.p_mean_variance(model.model, x_t, t,
                                                    clip_denoised=False, model_kwargs=cached)
                    eps = diffusion._predict_eps_from_xstart(x_t, t, pred['pred_xstart'])
                squared = (eps-noise).square()
                def mse(region):
                    return float((squared*region).sum()/(region.sum()*4)) if region.sum()>0 else None
                save_image(decode(pred['pred_xstart'])[0], out / f'teacher/step-{step}-t-{time_step}.png')
                diagnostics.append(dict(kind='teacher_forced', step=step, timestep=time_step,
                                        roi_noise_mse=mse(roi), background_noise_mse=mse(1-roi)))

        def evaluate(step):
            teacher(step)
            for forecast_seed in protocol['forecast_seeds']:
                deadline()
                capture = CaptureDiffusion(sampler, torch.cuda.get_rng_state)
                seed(forecast_seed)
                tick = time.monotonic()
                native_prediction = model_forward_wrapper(
                    (model.model, capture, model.vae), context_pixels, action[:, None],
                    protocol['offset'], 28, 'cuda', 16, x_supervised=projection_pixels)[0]
                torch.cuda.synchronize()
                native_seconds = time.monotonic()-tick
                receipt = capture.receipt
                metrics = {k: float((receipt['kwargs'][k]-cached[k]).float().square().mean())
                           for k in ('x_cond', 'x_supervised')}
                for route in ('native', 'fresh_replay', 'cached'):
                    tick = time.monotonic()
                    if route == 'native':
                        prediction = native_prediction
                    else:
                        kwargs = receipt['kwargs'] if route == 'fresh_replay' else cached
                        torch.cuda.set_rng_state(receipt['rng'])
                        with torch.no_grad(), torch.amp.autocast('cuda', dtype=torch.bfloat16):
                            generated = sampler.p_sample_loop(
                                model.model.forward, receipt['shape'], receipt['noise'].clone(),
                                clip_denoised=False, model_kwargs=kwargs, progress=False, device='cuda')
                            prediction = model.vae.decode(generated/.18215).sample.clamp(-1, 1)[0]
                        torch.cuda.synchronize()
                    parity = float((prediction-native_prediction).abs().max()) if route=='fresh_replay' else None
                    if parity is not None and parity > 1e-5:
                        raise ValueError('Native replay parity failed')
                    path = out / f'forecasts/step-{step}-seed-{forecast_seed}-{route}.png'
                    save_image(prediction, path)
                    records.append(dict(step=step, seed=forecast_seed, route=route,
                                        file=str(path.relative_to(out)), sha256=sha(path),
                                        seconds=native_seconds if route=='native' else time.monotonic()-tick,
                                        parity_max_abs=parity, latent_mse=metrics))
                    write(out / 'forecasts.json', records)
            write(out / 'diagnostics.json', diagnostics)

        evaluate(0)
        optimizer = torch.optim.AdamW([p for _, p in parameters], lr=protocol['lr'], weight_decay=0)
        seed(protocol['train_seed'])
        for step in range(1, protocol['steps']+1):
            deadline()
            optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast('cuda', dtype=torch.bfloat16):
                t = torch.randint(0, diffusion.num_timesteps, (1,), device='cuda')
                terms = diffusion.training_losses(model.model, latent, t, cached)
                loss = terms['loss'].mean()
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite loss')
            loss.backward()
            grad = torch.nn.utils.clip_grad_norm_([p for _, p in parameters], 1, error_if_nonfinite=True)
            optimizer.step()
            losses.append(dict(step=step, timestep=int(t.item()), loss=float(loss.detach()),
                               mse=float(terms['mse'].mean().detach()), gradient=float(grad)))
            if step % 64 == 0:
                write(out / 'training.json', losses)
            if step in protocol['evaluation_at_steps']:
                rng_state = torch.cuda.get_rng_state()
                evaluate(step)
                torch.cuda.set_rng_state(rng_state)
        summary['final_weights'] = snapshot('after-1024', parameters)
        summary['status'] = 'completed'
    finally:
        write(out / 'training.json', losses)
        write(out / 'diagnostics.json', diagnostics)
        summary.update(elapsed_model_seconds=time.monotonic()-began, forecasts=len(records),
                       updates=len(losses))
        write(out / 'summary.json', summary)
        del optimizer
        model.model.to('cpu')
        model.vae.to('cpu')
        gc.collect()
        native.clear_cuda_workspaces(torch)
        torch.cuda.empty_cache()
        write(out / 'shutdown.json', dict(dispatch_invoked=False, completed=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--execute-training', action='store_true')
    args = parser.parse_args()
    if args.execute_training:
        run(args.root)
    else:
        checked = validate(args.root)
        print(json.dumps(dict(status='passed', gpu_requested=False,
                              expected_forecasts=len(expected_forecasts(checked)))))
