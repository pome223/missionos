import json

import numpy as np
from PIL import Image
import pytest

from scripts.diagnose_yokohama_pad_latents import CaptureDiffusion, expected_forecasts, sha, validate, write
from scripts.evaluate_yokohama_pad_latents import evaluate


def fixture(root):
    root.mkdir()
    stamps = np.arange(16, dtype=np.int64)*250_000_000
    np.savez(root/'pair.npz', rgb=np.zeros((16, 224, 224, 3), np.uint8), stamps_ns=stamps)
    Image.new('RGB', (224, 224)).save(root/'training-target.png')
    protocol = dict(schema='pad_anwm_one_pair_latent.v1', steps=1024,
                    evaluation_at_steps=[0, 256, 1024], forecast_seeds=[42, 43], offset=12,
                    cutoff_stamp_ns=int(stamps[-1]),
                    files={n: sha(root/n) for n in ('pair.npz', 'training-target.png')})
    write(root/'latent-protocol.json', protocol)
    return protocol


def test_one_pair_input_and_exact_forecast_plan(tmp_path):
    root = tmp_path/'run'
    protocol = fixture(root)
    assert validate(root) == protocol
    assert len(expected_forecasts(protocol)) == 18
    assert (1024, 43, 'cached') in expected_forecasts(protocol)
    with np.load(root/'pair.npz') as a:
        rgb, stamps = a['rgb'].copy(), a['stamps_ns'].copy()
    stamps[-1] += 250_000_000
    np.savez(root/'pair.npz', rgb=rgb, stamps_ns=stamps)
    protocol['files']['pair.npz'] = sha(root/'pair.npz')
    write(root/'latent-protocol.json', protocol)
    with pytest.raises(ValueError, match='cutoff'):
        validate(root)


def test_changed_target_cannot_enter_training(tmp_path):
    root = tmp_path/'run'
    fixture(root)
    Image.new('RGB', (224, 224), 'red').save(root/'training-target.png')
    with pytest.raises(ValueError, match='Changed input'):
        validate(root)


def test_native_sampler_capture_preserves_arguments_and_rng():
    class Tensor:
        def __init__(self, value):
            self.value = value
        def detach(self):
            return self
        def clone(self):
            return Tensor(self.value)
    class Sampler:
        def p_sample_loop(self, model, shape, noise, **kwargs):
            self.args = (model, shape, noise, kwargs)
            return 'native-output'
    sampler = Sampler()
    capture = CaptureDiffusion(sampler, lambda: 'rng-before-native-sampling')
    kwargs = {'x_cond': Tensor('fresh-context'), 'y': Tensor('action')}
    noise = Tensor('initial-noise')
    assert capture.p_sample_loop('model', (1, 4, 28, 28), noise, model_kwargs=kwargs) == 'native-output'
    assert capture.receipt['rng'] == 'rng-before-native-sampling'
    assert capture.receipt['noise'] is not noise
    assert capture.receipt['kwargs']['x_cond'].value == 'fresh-context'
    assert sampler.args[2] is noise and sampler.args[3]['model_kwargs'] is kwargs
    with pytest.raises(ValueError, match='Multiple'):
        capture.p_sample_loop('model', (1,), noise, model_kwargs=kwargs)


def test_partial_or_nonmatching_replays_never_count_as_fit(tmp_path):
    root = tmp_path/'run'
    protocol = fixture(root)
    image = np.full((224, 224, 3), 120, np.uint8)
    image[100:110, 90:110] = (240, 128, 20)
    Image.fromarray(image).save(root/'training-target.png')
    results = root/'results'
    (results/'vae').mkdir(parents=True)
    for name in ('target-mean', 'target-sample', 'latest-sample'):
        Image.fromarray(image).save(results/'vae'/f'{name}.png')
    records = []
    for step, seed, route in sorted(expected_forecasts(protocol)):
        rel = f'{step}-{seed}-{route}.png'
        Image.fromarray(image).save(results/rel)
        records.append(dict(step=step, seed=seed, route=route, file=rel,
                            sha256=sha(results/rel), parity_max_abs=0 if route=='fresh_replay' else None))
    summary = dict(status='completed', updates=1024, forecasts=len(records),
                   protocol_sha256=sha(root/'latent-protocol.json'))
    for key in ('initial_weights', 'final_weights'):
        path = results/(key+'.fixture')
        path.write_text('test receipt')
        summary[key] = dict(file=path.name, sha256=sha(path))
    write(results/'summary.json', summary)
    write(results/'forecasts.json', records)
    assert evaluate(root, results)['fit'] == dict(native=True, cached=True)
    write(results/'forecasts.json', records[:-1])
    assert evaluate(root, results)['fit'] == dict(native=False, cached=False)
    next(r for r in records if r['route']=='fresh_replay')['parity_max_abs'] = .1
    write(results/'forecasts.json', records)
    assert evaluate(root, results)['complete'] is False
