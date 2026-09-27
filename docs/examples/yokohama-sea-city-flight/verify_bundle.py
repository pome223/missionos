#!/usr/bin/env python3
"""Recheck portable image gates, measured steps and publication hashes; no cloud/GPU."""
from pathlib import Path
import hashlib,json,math,sys
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[2]
sys.path.insert(0,str(REPO))
from src.runtime.yokohama_native import forecast_consistency,digest
from src.runtime.ship_vla_adapter import decode_action
from scripts.yokohama_wam_profile import MOTION_ADAPTER_SHA256,MOTION_CONTRACT

def read(name):return json.loads((ROOT/name).read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
summary=read('summary.json');steps=read('model-steps.json');cost=read('cost.json')
assert summary['status']=='passed' and summary['model_updates_observed']==2
assert summary['native_vla_invoked'] and summary['native_wam_invoked']
assert len(steps)==2 and [s['cycle'] for s in steps]==[1,2]
assert read('verification-decisions.json')['native_model_flight_verified']
assert read('verification-flight.json')['status']=='passed'
assert cost['cleanup_confirmed'] and cost['cost_estimate_closed'] and cost['cumulative_estimated_usd']<=cost['authorized_total_usd']==17
assert abs(cost['prior_estimated_usd']+cost['new_estimated_usd']-cost['cumulative_estimated_usd'])<1e-10
assert read('verification-flight.json')['checks']['one_km_sea_both_directions']
assert read('verification-flight.json')['checks']['sea_model_sessions_off']
assert read('mission-result.json')['sea_leg_verified'] is True
for step in steps:
 cycle=step['cycle'];request=step['input_request'];identity=step['service_identity']
 assert request['schema_version']==MOTION_CONTRACT and request['adapter_sha256']==identity['adapter_sha256']==MOTION_ADAPTER_SHA256
 assert request['num_timesteps']==identity['model_time_index']==1
 vla=step['native_vla_response'];service=vla['service']
 assert digest(vla)==step['permit']['vla_response_sha256']
 assert service['decoding_policy']=='aerovla_compact_city_grammar.v2'
 assert service['hold_bin_allowed'] is False and service['terminal_proposal_allowed'] is False
 assert vla['vla_inference_invoked'] is True and vla['dispatch_invoked'] is False
 assert list(decode_action(vla['generated_text'])[0])==step['permit']['candidate']['bins']
 assert request['future_ground_truth_used_for_forecast'] is False and request['dispatch_allowed'] is False
 assert step['native_cuda_allocated_after_request_bytes']==0
 assert math.dist(step['arrival_xyz_m'],step['permit']['candidate']['target_world_xyz_m'])==step['observed_target_error_m']<=.25
 assert step['permit']['rules']['allowed'] is True
 for check in step['checks']:
  name=check['candidate']['id'];prefix=ROOT/'images'/f'cycle-{cycle}-{name}'
  prediction=Path(str(prefix)+'-prediction.png')
  assert sha(prediction)==step['original_forecast_sha256'][name]
  actual=forecast_consistency(np.asarray(Image.open(prediction)), np.asarray(Image.open(str(prefix)+'-reference.png')),np.asarray(Image.open(str(prefix)+'-mask.png'))>0)
  assert actual['passed'] and actual=={k:v for k,v in check.items() if k!='candidate'}
 assert sha(ROOT/'images'/f'cycle-{cycle}-arrival-observed.png')==step['actual_camera']['source_sha256']
 assert 0<=step['actual_camera']['before_settled_arrival_s']<=2
 assert step['actual_camera']['exact_future_image_alignment_verified'] is False
trajectory=read('trajectory.json')['trajectory']
assert len(trajectory)>100 and all(b['t']>a['t'] for a,b in zip(trajectory,trajectory[1:]))
assert all(math.isfinite(x) for row in trajectory for x in [row['t'],*row['xyz']])
assert any(row['phase']=='03-DELIVERY' for row in trajectory) and trajectory[-1]['phase']=='return_land'
video=read('video-metadata.json');assert video['video_sha256']==sha(ROOT/'onboard-timelapse.mp4')
manifest=read('files.sha256.json')
for name,h in manifest.items():
 p=ROOT/name;assert p.resolve().is_relative_to(ROOT.resolve()) and sha(p)==h,name
print(json.dumps(dict(status='passed',native_steps=2,forecasts_recomputed=4,trajectory_points=len(trajectory),files=len(manifest),gpu_requested=False)))
