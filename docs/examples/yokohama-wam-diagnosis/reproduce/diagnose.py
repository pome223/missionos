"""Frozen ANWM conditioning diagnostic. Offline only; never dispatches a mission."""
import argparse,hashlib,json,sys,time,traceback
from pathlib import Path
import numpy as np
from PIL import Image
import ship_anwm as native
from ship_anwm_server import NativeModel
from frozen_metrics import past_view,forecast_consistency
R=Path(__file__).resolve().parent;O=R/'results'
def write(p,v):p.write_text(json.dumps(v,indent=2,allow_nan=False)+'\n')
def digest(p):return native.digest(p)
def seed(torch):
 torch.manual_seed(42);torch.cuda.manual_seed_all(42);np.random.seed(42)
def pixels(t):return ((t.detach().float().cpu().permute(1,2,0).numpy()+1)*127.5).round().clip(0,255).astype(np.uint8)
def save(name,t):
 a=pixels(t);Image.fromarray(a).save(O/name);return a
def main():
 p=argparse.ArgumentParser();p.add_argument('--validate-only',action='store_true');args=p.parse_args()
 protocol=json.loads((R/'protocol.json').read_text())
 for name,sha in protocol['payload_sha256'].items():assert digest(R/name)==sha,(name,'changed')
 request,arrays=native.validate(R/'request.json')
 assert request['history_sha256']=='bf603ee0363d949874d6f47dce8798e38c816d24302cbe48c2ace589d0b9dc11'
 assert len(protocol['cases'])==6 and protocol['seed']==42
 Image.open(R/'reference.jpg').verify()
 if args.validate_only:
  print(json.dumps({'status':'passed','boundary':'input contract and frozen payload hashes; no GPU invocation','cases':len(protocol['cases'])}));return
 O.mkdir(exist_ok=False)
 model=NativeModel(R/'upstream',R/'assets/0200000.pth.tar');torch=model.torch
 from anwm.utils import transform
 from anwm.rollout import model_forward_wrapper
 context=torch.stack([transform(Image.fromarray(im)) for im in arrays['rgb']])[None].to('cuda')
 reference=transform(Image.open(R/'reference.jpg').convert('RGB')).to('cuda')
 reference_context=reference[None,None].repeat(1,16,1,1,1)
 past,mask=past_view(arrays,arrays['poses'][-1]);Image.fromarray(past).save(O/'city-past-reference.png');np.save(O/'city-known-mask.npy',mask)
 save('city-observed.png',context[0,-1]);save('reference-observed.png',reference)
 # Reuse the production wrapper and its exact RGBD projection for the first case.
 hold_request={**request,'candidates':[request['candidates'][0]]}
 baseline=model._predict(hold_request,arrays,O)
 for forecast in baseline['forecasts']:
  for asset in forecast['files'].values():asset.pop('png_base64')
 write(O/'production-response.json',baseline)
 projection=torch.from_numpy(np.asarray(Image.open(O/'hold-projection.png').convert('RGB')).copy()).permute(2,0,1).float().div(255).sub(.5).div(.5)[None,None].to('cuda')
 assert digest(O/'hold-projection.png')==digest(R/'hold-projection.png'),'Original projection changed'
 hold=torch.tensor([[[-1/3,0,0,0]]],dtype=torch.float32,device='cuda')
 records=[]
 for case in protocol['cases']:
  begin=time.monotonic();name=case['id'];src=context if case['scene']=='city' else reference_context
  target=projection if case['projection']=='rgbd' else src[:,-1:]
  if name=='city_original_t4':
   out=np.asarray(Image.open(O/'hold-prediction.png').convert('RGB'));elapsed=baseline['forecasts'][0]['elapsed_s']
  else:
   seed(torch)
   with torch.no_grad():pred=model_forward_wrapper((model.model,model.diffusion,model.vae),src,hold,case['time_steps'],28,'cuda',16,num_goals=1,x_supervised=target)[0]
   torch.cuda.synchronize();out=save(name+'.png',pred);elapsed=time.monotonic()-begin;del pred
  if name=='city_original_t4':Image.fromarray(out).save(O/(name+'.png'))
  ref=past if case['scene']=='city' else pixels(reference);known=mask if case['scene']=='city' else np.ones((224,224),bool)
  full=pixels(src[0,-1]);metrics=forecast_consistency(out,ref,known)
  row={**case,'elapsed_s':elapsed,'image_sha256':digest(O/(name+'.png')),'visible_consistency':metrics,'full_frame_rgb_mae_to_observed':float(np.abs(out.astype(float)-full).mean()),'hold_action_normalized':hold.detach().cpu().tolist(),'rel_t':case['time_steps']/128,'dispatch_allowed':False}
  records.append(row);write(O/'cases.json',records);print(json.dumps({'case':name,'mae':metrics['luminance_mae'],'consistency_passed':metrics['passed']}),flush=True)
 # Compression-only diagnostics, both posterior mean and fixed-seed sample.
 recon=[]
 for name,tensor in [('city',context[0,-1]),('projection',projection[0,0]),('reference',reference)]:
  for mode in ['mode','sample']:
   seed(torch)
   with torch.no_grad(),torch.amp.autocast('cuda',dtype=torch.bfloat16):
    posterior=model.vae.encode(tensor[None]).latent_dist;z=posterior.mode() if mode=='mode' else posterior.sample();prediction=model.vae.decode(z).sample[0].clamp(-1,1)
   out=save('vae-'+name+'-'+mode+'.png',prediction);ref=pixels(tensor)
   recon.append({'input':name,'posterior':mode,'rgb_mae':float(np.abs(out.astype(float)-ref).mean()),'visible_consistency':forecast_consistency(out,ref,np.ones((224,224),bool))});del posterior,z,prediction
 write(O/'vae-reconstruction.json',recon)
 old=np.asarray(Image.open(R/'hold-prediction.png').convert('RGB'));reproduced=np.asarray(Image.open(O/'city_original_t4.png').convert('RGB'))
 summary={'schema_version':'anwm_offline_diagnostic.v1','cases':records,'vae_reconstruction':recon,'original_reproduction':{'old_sha256':digest(R/'hold-prediction.png'),'new_sha256':digest(O/'city_original_t4.png'),'rgb_mae':float(np.abs(old.astype(float)-reproduced).mean()),'max_abs_channel_difference':int(np.abs(old.astype(int)-reproduced).max())},'gpu':torch.cuda.get_device_name(0),'torch_version':torch.__version__,'load_seconds':model.load_seconds,'max_cuda_allocated_bytes':torch.cuda.max_memory_allocated(),'inference_calls':6,'vla_invoked':False,'flight_invoked':False,'dispatch_allowed':False,'source_commit':protocol['source_commit'],'protocol_sha256':digest(R/'protocol.json')}
 write(O/'summary.json',summary);print('DIAGNOSTIC_COMPLETE',flush=True)
if __name__=='__main__':
 try:main()
 except BaseException as exc:
  write(R/'diagnostic-error.json',{'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()});raise
