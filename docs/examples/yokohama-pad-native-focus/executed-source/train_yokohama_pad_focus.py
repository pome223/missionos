#!/usr/bin/env python3
"""Opt-in native ANWM regional, short-horizon post-training. No aircraft API."""
from __future__ import annotations
import argparse
import gc
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

if __package__:
    from scripts.yokohama_pad_focus_data import validate, sha, write, classify
else:
    from yokohama_pad_focus_data import validate, sha, write, classify


def validate_protocol(root):
    p=json.loads((root/'focus-protocol.json').read_text())
    expected=dict(schema='pad_native_focus_protocol.v1',steps=4096,lr=0.00005,seed=42,
        evaluation_steps=50,model_work_seconds_max=2600,roi_extra_weight=8.0,
        x0_roi_weight=.1,flight_admitted=False,vla_training=False)
    if any(p.get(k)!=v for k,v in expected.items()): raise ValueError('Unreviewed training protocol')
    if sha(root/'dataset/dataset.json')!=p['dataset_sha256']: raise ValueError('Dataset changed')
    for name,digest in p['source_sha256'].items():
        if Path(name).name!=name or sha(root/name)!=digest: raise ValueError('Source changed')
    if (sha(root/'pad-adapter.pt') != '5b78a8c97e6ed42ece769bf1d5bdeb35e8352ae8b64f7f4c27b0edc6b7518542'
            or sha(root/'pad-adapter.pt') != p['initial_adapter_sha256']):
        raise ValueError('Adapter changed')
    return p,validate(root/'dataset')


def run(root):
    protocol,data=validate_protocol(root)
    import torch
    if __package__:
        from scripts.ship_anwm_server import NativeModel
        from scripts import ship_anwm as native
        from scripts.yokohama_wam_profile import ADAPTER_NAMES
    else:
        from ship_anwm_server import NativeModel
        import ship_anwm as native
        from yokohama_wam_profile import ADAPTER_NAMES
    out=root/'results';out.mkdir(exist_ok=False)
    started=time.monotonic()
    model=NativeModel(root/'upstream',root/'assets/0200000.pth.tar',motion_adapter=root/'motion-adapter.pt')
    from anwm.diffusion import create_diffusion
    from anwm.rollout import model_forward_wrapper
    from anwm.utils import normalize_data
    saved=torch.load(root/'pad-adapter.pt',map_location='cpu',weights_only=True)
    if saved['base_checkpoint_sha256']!=native.MODEL_SHA256 or set(saved['state'])!=ADAPTER_NAMES:
        raise ValueError('Initial native adapter scope')
    parameters=dict(model.model.named_parameters())
    with torch.no_grad():
        for name,value in saved['state'].items():
            if value.shape!=parameters[name].shape or not torch.isfinite(value).all():raise ValueError('Invalid native weights')
            parameters[name].copy_(value)
    del saved
    with np.load(root/'dataset/reader.npz',allow_pickle=False) as a:reader={k:a[k] for k in a.files}
    background=np.asarray(Image.open(root/'dataset/background.png')).astype(float)
    summary=dict(schema='native_pad_focus_training.v1',status='failed',base_sha256=native.MODEL_SHA256,
        initial_adapter_sha256=sha(root/'pad-adapter.pt'),protocol_sha256=sha(root/'focus-protocol.json'),
        native_anwm=True,native_vla=False,cpu_surrogate=False,flight_admitted=False,dispatch_invoked=False,
        experimental_regional_profile=True,action='hold_only',test_targets_uploaded=False,model_load_seconds=model.load_seconds)
    records=[];cache={}
    action=np.zeros(4,np.float32)
    action[:3]=normalize_data(action[:3]/3.30,{'min':np.array([-2.5,-4,-3]),'max':np.array([5,4,3])})
    action=torch.as_tensor(action)[None].cuda()

    def deadline():
        if time.monotonic()-started>protocol['model_work_seconds_max']:raise TimeoutError('Model work deadline')

    def tensor(rgb):
        return torch.from_numpy(np.array(rgb,copy=True)).permute(0,3,1,2).float().cuda()/127.5-1

    def pixels(t):
        return ((t.detach().float().cpu().permute(1,2,0).numpy()+1)*127.5).round().clip(0,255).astype(np.uint8)

    def predict(sample,stage,adapter_hash):
        deadline();dest=out/stage/sample['id'];dest.mkdir(parents=True,exist_ok=False)
        with np.load(root/'dataset'/sample['history'],allow_pickle=False) as a:rgb=a['rgb']
        context=tensor(rgb)[None]
        # Exact zero-motion, fixed-camera view: the last observed regional image
        # conditions geometry. No future frame or lead pose is a model input.
        projection=context[:,-1:]
        torch.manual_seed(42);torch.cuda.manual_seed_all(42);np.random.seed(42)
        began=time.monotonic()
        prediction=model_forward_wrapper((model.model,create_diffusion('50'),model.vae),
            context,action[:,None],sample['offset'],28,'cuda',16,x_supervised=projection)[0]
        torch.cuda.synchronize();elapsed=time.monotonic()-began
        im=pixels(prediction);Image.fromarray(im).save(dest/'prediction.png')
        value=dict(schema='native_pad_focus_forecast.v1',sample_id=sample['id'],stage=stage,
            history_sha256=sha(root/'dataset'/sample['history']),prediction_sha256=sha(dest/'prediction.png'),
            cutoff_stamp_ns=sample['cutoff_stamp_ns'],future_stamp_ns=sample['cutoff_stamp_ns']+sample['offset']*250000000,
            frame_offset=sample['offset'],horizon_sim_s=sample['offset']/4,adapter_sha256=adapter_hash,
            base_sha256=native.MODEL_SHA256,seed=42,diffusion_steps=50,seconds=elapsed,
            reader=classify(im,reader),native_anwm=True,dispatch_invoked=False,flight_admitted=False)
        write(dest/'result.json',value);records.append(dict(file=str((dest/'result.json').relative_to(out)),sha256=sha(dest/'result.json')))
        write(out/'forecast-manifest.json',dict(records=records))
        print(json.dumps(dict(stage=stage,sample=sample['id'],seconds=elapsed,reader=value['reader']['state'])),flush=True)
        del context,projection,prediction
        return im

    def state_hash(training):
        h=hashlib.sha256()
        for name,p in model.model.named_parameters():
            if p.requires_grad==training:
                h.update(name.encode());h.update(p.detach().cpu().contiguous().numpy().tobytes())
        return h.hexdigest()

    try:
        # Predictions and VAE round trips use train-only observations for the
        # conditional learning decision; evaluation truth is absent on this VM.
        dev=[s for s in data['samples'] if s['id'] in protocol['development_ids']]
        if len(dev)!=len(protocol['development_ids']) or any(s['split']!='train' for s in dev):raise ValueError('Development split')
        development=[]
        for s in dev:
            target=np.asarray(Image.open(root/'dataset'/s['target']))
            pred=predict(s,'development',summary['initial_adapter_sha256'])
            with torch.no_grad(),torch.amp.autocast('cuda',dtype=torch.bfloat16):
                reconstructed=model.vae.decode(model.vae.encode(tensor(target[None])).latent_dist.mode()).sample[0]
            Image.fromarray(pixels(reconstructed)).save(out/'development'/s['id']/'vae-target.png')
            want=classify(target,reader)['state'];got=classify(pred,reader)['state']
            development.append(dict(id=s['id'],target=want,predicted=got,passed=want!='unknown' and got==want,
                                    vae_reader=classify(pixels(reconstructed),reader)))
        training_needed=not all(r['passed'] for r in development)
        write(out/'development.json',dict(training_needed=training_needed,rows=development))
        for s in data['samples']:
            if s['split']=='test':predict(s,'before',summary['initial_adapter_sha256'])
        if not training_needed:
            summary.update(status='completed',training_performed=False,inference_calls=len(records));return
        for name,p in model.model.named_parameters():
            p.requires_grad_(name in ADAPTER_NAMES or name.startswith('blocks.26.'))
        model.vae.requires_grad_(False)
        trainable=[(n,p) for n,p in model.model.named_parameters() if p.requires_grad]
        initial={n:p.detach().cpu().clone() for n,p in trainable}
        frozen=state_hash(False);initial_hash=state_hash(True)
        summary.update(trainable_names=[n for n,_ in trainable],trainable_parameters=sum(p.numel() for _,p in trainable),frozen_before_sha256=frozen)
        write(out/'identity.json',summary)
        prepared=[]
        torch.manual_seed(42)
        global_roi=torch.nn.functional.max_pool2d(torch.as_tensor(reader['mask'].astype(np.float32))[None,None].cuda(),8)
        for s in data['samples']:
            if s['split']!='train':continue
            deadline()
            if s['history'] not in cache:
                with np.load(root/'dataset'/s['history'],allow_pickle=False) as a:rgb=a['rgb']
                with torch.no_grad(),torch.amp.autocast('cuda',dtype=torch.bfloat16):
                    context=model.vae.encode(tensor(rgb)).latent_dist.sample()*0.18215
                    projection=model.vae.encode(tensor(rgb[-1:])).latent_dist.sample()*0.18215
                cache[s['history']]=(context[None],projection)
            target=np.asarray(Image.open(root/'dataset'/s['target']))
            motion=(np.max(np.abs(target.astype(float)-background),axis=2)>20).astype(np.float32)
            roi=torch.nn.functional.max_pool2d(torch.as_tensor(motion)[None,None].cuda(),8)
            if roi.sum()<1:roi=global_roi
            with torch.no_grad(),torch.amp.autocast('cuda',dtype=torch.bfloat16):
                latent=model.vae.encode(tensor(target[None])).latent_dist.sample()*0.18215
            prepared.append(dict(sample=s,latent=latent,context=cache[s['history']][0],projection=cache[s['history']][1],roi=roi))
        optimizer=torch.optim.AdamW([p for _,p in trainable],lr=protocol['lr'],weight_decay=0)
        diffusion=create_diffusion('');alphas=torch.as_tensor(diffusion.alphas_cumprod,device='cuda',dtype=torch.float32)
        rng=np.random.default_rng(42);torch.manual_seed(42);losses=[];began=time.monotonic()
        for step in range(protocol['steps']):
            deadline();row=prepared[int(rng.integers(len(prepared)))];optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast('cuda',dtype=torch.bfloat16):
                t=torch.randint(0,diffusion.num_timesteps,(1,),device='cuda');noise=torch.randn_like(row['latent']);captured={}
                def forward(x,t,**kwargs):
                    value=model.model(x,t,**kwargs);captured.update(eps=value[:,:4],x=x);return value
                kwargs=dict(y=action,x_cond=row['context'],x_supervised=row['projection'],
                            rel_t=torch.tensor([row['sample']['offset']/128],device='cuda'))
                ordinary=diffusion.training_losses(forward,row['latent'],t,kwargs,noise=noise)['loss'].mean()
                roi_loss=((captured['eps']-noise).square()*row['roi']).sum()/(row['roi'].sum()*4)
                alpha=alphas[t].reshape(1,1,1,1)
                estimated=(captured['x']-(1-alpha).sqrt()*captured['eps'])/alpha.sqrt()
                x0_loss=((estimated-row['latent']).abs()*row['roi']).sum()/(row['roi'].sum()*4)
                weighted_x0=(alpha/(1-alpha+.1)).clamp(max=5).mean()*x0_loss
                loss=ordinary+protocol['roi_extra_weight']*roi_loss+protocol['x0_roi_weight']*weighted_x0
            if not torch.isfinite(loss):raise ValueError('Nonfinite loss')
            loss.backward();grad=torch.nn.utils.clip_grad_norm_([p for _,p in trainable],1,error_if_nonfinite=True);optimizer.step()
            losses.append(dict(step=step+1,sample_id=row['sample']['id'],loss=float(loss.detach()),ordinary=float(ordinary.detach()),roi=float(roi_loss.detach()),x0=float(x0_loss.detach()),gradient=float(grad)))
            if step==0 or (step+1)%128==0:
                write(out/'training.json',losses);print(json.dumps(dict(stage='training',**losses[-1])),flush=True)
        final={n:p.detach().cpu().clone() for n,p in trainable}
        delta=sum(float((final[n]-initial[n]).square().sum()) for n in initial)**.5
        if delta<=0 or state_hash(False)!=frozen:raise ValueError('Missing update or frozen base changed')
        adapter=out/'adapter.pt'
        torch.save(dict(schema='native_pad_focus_adapter.v1',base_sha256=native.MODEL_SHA256,state=final,protocol_sha256=sha(root/'focus-protocol.json')),adapter)
        with torch.no_grad():
            for n,p in trainable:p.copy_(initial[n])
        loaded=torch.load(adapter,map_location='cpu',weights_only=True)
        with torch.no_grad():
            for n,p in trainable:
                p.copy_(loaded['state'][n])
                if not torch.equal(p.detach().cpu(),final[n]):raise ValueError('Reload mismatch')
        if state_hash(True)==initial_hash:raise ValueError('Reloaded weights unchanged')
        summary.update(training_performed=True,steps=len(losses),training_seconds=time.monotonic()-began,
            head_change_l2=delta,checkpoint_reloaded=True,frozen_base_unchanged=True,final_adapter_sha256=sha(adapter))
        write(out/'training-receipt.json',summary)
        del optimizer,prepared,cache,initial,final,loaded,row,latent,context,projection,captured,loss,ordinary,roi_loss,x0_loss,weighted_x0,estimated,kwargs,forward
        gc.collect();torch.cuda.empty_cache()
        for s in data['samples']:
            if s['split']=='test':predict(s,'after',summary['final_adapter_sha256'])
        summary['status']='completed'
    finally:
        summary.update(total_model_seconds=time.monotonic()-started,inference_calls=len(records))
        write(out/'summary.json',summary)
        model.model.to('cpu');model.vae.to('cpu');gc.collect();native.clear_cuda_workspaces(torch);torch.cuda.empty_cache()
        write(out/'shutdown.json',dict(cuda_allocated_bytes=torch.cuda.memory_allocated(),dispatch_invoked=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--execute-training',action='store_true');a=p.parse_args()
    if a.execute_training:run(a.root)
    else:
        _,d=validate_protocol(a.root);print(json.dumps(dict(status='passed',samples=len(d['samples']),gpu_requested=False)))
