"""Frozen offline ANWM paired-view diagnostic; payload contains no future images."""
import argparse,json,time,traceback
from pathlib import Path
import numpy as np
from PIL import Image
import ship_anwm as native
from ship_anwm_server import NativeModel
from paired_appearance import fill_infinite_appearance
R=Path(__file__).resolve().parent;O=R/'results'
def write(p,v):p.write_text(json.dumps(v,indent=2,allow_nan=False)+'\n')
def pixels(t):return ((t.detach().float().cpu().permute(1,2,0).numpy()+1)*127.5).round().clip(0,255).astype(np.uint8)
def main():
 p=argparse.ArgumentParser();p.add_argument('--validate-only',action='store_true');args=p.parse_args()
 protocol=json.loads((R/'protocol.json').read_text())
 for name,sha in protocol['payload_sha256'].items():assert native.digest(R/name)==sha,name
 data={}
 for city in ['D1','D2']:
  request,arrays=native.validate(R/'inputs'/city/'request.json')
  depth=np.load(R/'inputs'/city/'last-raw-depth.npy',allow_pickle=False)
  assert depth.shape==(360,640)
  data[city]=(request,arrays,depth)
 assert len(protocol['cases'])==12 and protocol['seed']==42
 assert protocol['future_images_uploaded'] is False
 if args.validate_only:
  print(json.dumps({'status':'passed','cases':12,'future_images_uploaded':False}));return
 O.mkdir(exist_ok=False)
 model=NativeModel(R/'upstream',R/'assets/0200000.pth.tar');torch=model.torch
 from anwm.projection import reproject_depth_to_other_pose_seq2seq,project_to_2d_image_seq2seq
 from anwm.utils import transform,normalize_data
 from anwm.rollout import model_forward_wrapper
 prepared={};records=[]
 for city,(request,arrays,depth) in data.items():
  context=torch.stack([transform(Image.fromarray(im)) for im in arrays['rgb']])[None].to('cuda')
  for candidate in request['candidates']:
   name=city+'-'+candidate['id'];delta=np.array(candidate['delta'],dtype=np.float32)
   target=native.action_pose(arrays['poses'][-1],delta)
   points,colors=reproject_depth_to_other_pose_seq2seq(arrays['intrinsics'],arrays['depth'],arrays['rgb'],arrays['poses'],target[None])
   geometry=project_to_2d_image_seq2seq(arrays['intrinsics'],points,colors,(360,640))[0]
   known=project_to_2d_image_seq2seq(arrays['intrinsics'],points,[np.full_like(c,255) for c in colors],(360,640))[0][...,0]>0
   filled,appearance_mask=fill_infinite_appearance(geometry,known,arrays['rgb'][-1],depth,arrays['intrinsics'],arrays['poses'][-1],target)
   assert np.array_equal(filled[known],geometry[known])
   assert not (appearance_mask & known).any()
   projections={}
   for mode,im in [('native',geometry),('appearance',filled)]:
    tensor=transform(Image.fromarray(im))[None,None].to('cuda');projections[mode]=tensor
    Image.fromarray(pixels(tensor[0,0])).save(O/(name+'-'+mode+'-projection.png'))
   Image.fromarray((appearance_mask*255).astype(np.uint8)).save(O/(name+'-appearance-mask.png'))
   delta[:3]=normalize_data(delta[:3]/3.30,{'min':np.array([-2.5,-4,-3]),'max':np.array([5,4,3])})
   prepared[name]=(context,torch.as_tensor(delta)[None,None].to('cuda'),projections)
   write(O/(name+'-conditioning.json'),{'candidate':candidate,'metric_geometry_changed':False,'known_fraction':float(known.mean()),'appearance_added_fraction':float(appearance_mask.mean()),'remaining_unknown_fraction':float((~known & ~appearance_mask).mean()),'appearance_is_free_space_evidence':False,'target_pose_from_preflight_request':target.tolist()})
 for case in protocol['cases']:
  context,action,projections=prepared[case['pair']]
  torch.manual_seed(42);torch.cuda.manual_seed_all(42);np.random.seed(42)
  started=time.monotonic()
  with torch.no_grad():prediction=model_forward_wrapper((model.model,model.diffusion,model.vae),context,action,case['time_steps'],28,'cuda',16,num_goals=1,x_supervised=projections[case['projection']])[0]
  torch.cuda.synchronize();elapsed=time.monotonic()-started
  image=pixels(prediction);Image.fromarray(image).save(O/(case['id']+'.png'))
  row={**case,'elapsed_s':elapsed,'prediction_sha256':native.digest(O/(case['id']+'.png')),'normalized_action':action.cpu().tolist(),'rel_t':case['time_steps']/128,'model_time_alignment_verified':False,'dispatch_allowed':False}
  records.append(row);write(O/'cases.json',records);print(json.dumps(row),flush=True);del prediction
 write(O/'summary.json',{'schema_version':'anwm_paired_offline_inference.v1','cases':records,'inference_calls':len(records),'gpu':torch.cuda.get_device_name(0),'torch_version':torch.__version__,'load_seconds':model.load_seconds,'max_cuda_allocated_bytes':torch.cuda.max_memory_allocated(),'protocol_sha256':native.digest(R/'protocol.json'),'future_images_uploaded':False,'flight_invoked':False,'vla_invoked':False,'dispatch_allowed':False})
 print('DIAGNOSTIC_COMPLETE',flush=True)
if __name__=='__main__':
 try:main()
 except BaseException as exc:
  write(R/'diagnostic-error.json',{'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()});raise
