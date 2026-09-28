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
release=read('cloud-release.json');assert release['models_stopped_before_release'] and release['late_request_rejected_before_release'] and release['flight_still_running']
assert not any(release['own_resources'].values())
failures=read('retained-failures.json');assert len(failures)==1
assert failures[0]['vla_invoked'] is False and failures[0]['wam_invoked'] is False and failures[0]['cloud_cleanup_confirmed']
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
assert read('mission-result.json')['thirteen_holds_passed'] is True
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

from src.runtime.yokohama_payload import fresh_pose
p=read('payload-scenario.json');receipt=read('payload-receipt.json');request=read('payload-request.json')
assert p['mass_kg']==.05 and p['size_m']==[.12,.12,.08]
assert p['pad_acceptance_radius_m']==1.5 and p['rest_height_tolerance_m']==.08
assert p['stability_sim_s']==3 and p['maximum_motion_m']==.05 and p['maximum_speed_mps']==.1
assert p['maximum_observation_age_s']==p['maximum_contact_age_sim_s']==.5
assert p['maximum_sample_gap_sim_s']==1 and p['receipt_max_age_worker_s']==5
assert read('verification-payload.json')['status']=='passed'
assert all(read('verification-payload.json')['checks'].values())
assert summary['payload_delivery_verified'] is True
assert receipt['payload_delivery_verified'] is True and receipt['physical_receipt_verified'] is False
own=dict(receipt);identity=own.pop('receipt_id');assert digest(own)==identity
assert receipt['run_id']==request['run_id']==summary['run_id']
assert receipt['config_sha256']==request['config_sha256']
assert receipt['request_sha256']==digest(request)
assert request['observation_sha256']==digest(request['observation'])
proof=receipt['assessment'];e=proof['evidence'];assert digest(e)==proof['evidence_sha256'] and all(proof['checks'].values())
anchor=request['observation'];pad=p['pad_world_xyz_m'];stable=e['stable_observations']
assert anchor['phase']=='PAYLOAD-LOW' and anchor['nav_state']==4 and anchor['arming_state']==2 and anchor['landed'] is False
assert math.hypot(*anchor['velocity_ned'])<=.3 and math.dist(anchor['vehicle']['xyz'],p['hover_world_xyz_m'])<=.6
assert math.dist(anchor['vehicle']['xyz'],anchor['payload']['xyz'])<=.8
assert fresh_pose(anchor,'vehicle') and fresh_pose(anchor,'payload')
assert e['detached']['state']=='detached' and anchor['sim_s']<=e['detached']['observed_sim_s']<=stable[-1]['sim_s']
assert e['carry_start']['phase']==e['carry_end']['phase']=='SEA-INBOUND-COAST'
assert math.dist(e['carry_start']['payload']['xyz'],e['carry_end']['payload']['xyz'])>900
assert stable[-1]['sim_s']-stable[0]['sim_s']>=p['stability_sim_s']
assert len(stable)==len(e['pad_contacts'])
for row in [e['carry_start'],e['carry_end'],*stable]:
 assert row['run_id']==summary['run_id'] and row['world_sha256']==anchor['world_sha256']
 assert row['vehicle']['id']==anchor['vehicle']['id'] and row['payload']['id']==anchor['payload']['id']
 assert fresh_pose(row,'vehicle') and fresh_pose(row,'payload')
for row,contact in zip(stable,e['pad_contacts']):
 assert row['phase']=='PAYLOAD-VERIFY' and row['nav_state']==4 and row['arming_state']==2 and row['landed'] is False
 assert math.hypot(*row['velocity_ned'])<=.3 and math.dist(row['vehicle']['xyz'],p['hover_world_xyz_m'])<=.6
 xyz=row['payload']['xyz'];assert math.dist(xyz[:2],pad[:2])<=p['pad_acceptance_radius_m']
 assert abs(xyz[2]-pad[2]-p['size_m'][2]/2)<=p['rest_height_tolerance_m']
 assert math.dist(xyz,row['vehicle']['xyz'])>=2
 assert contact['topic']=='delivery_pad'
 assert 'delivery_payload::' in contact['collision1']+contact['collision2'] and 'delivery_pad::' in contact['collision1']+contact['collision2']
 assert 0<=row['sim_s']-contact['sensor_sim_s']<=p['maximum_contact_age_sim_s']
 assert math.dist(xyz,stable[0]['payload']['xyz'])<=p['maximum_motion_m']
for a,b in zip(stable,stable[1:]):
 dt=b['sim_s']-a['sim_s'];assert 0<dt<=p['maximum_sample_gap_sim_s']
 assert math.dist(a['payload']['xyz'],b['payload']['xyz'])/dt<=p['maximum_speed_mps']
events=read('mission-events.json')
def one(kind):
 found=[e for e in events if e['event']==kind];assert len(found)==1;return found[0]
release=one('payload_release_requested');received=one('payload_received');allowed=one('payload_return_authorized')
assert one('city_session_revoked')['wall_s']<release['wall_s']<received['wall_s']<allowed['wall_s']<one('landing_observed')['wall_s']
assert release['request_sha256']==digest(request) and received['receipt_sha256']==digest(receipt)
assert allowed['receipt_id']==identity
assert 0<=received['wall_s']-proof['observed_through_wall_s']<=p['receipt_max_age_worker_s']
assert one('landing_observed')['observation']['landed'] is True
cargo_video=read('cargo-video-metadata.json')
assert cargo_video['run_id']==summary['run_id'] and cargo_video['playback_speed']==1
assert cargo_video['video_sha256']==sha(ROOT/'cargo-delivery.mp4')
assert cargo_video['source_start_sim_s']<cargo_video['release_sim_s']<cargo_video['receipt_sim_s']<cargo_video['source_end_sim_s']
assert cargo_video['no_model_prediction_frames'] is True

trajectory=read('trajectory.json')['trajectory']
assert len(trajectory)>100 and all(b['t']>a['t'] for a,b in zip(trajectory,trajectory[1:]))
assert all(math.isfinite(x) for row in trajectory for x in [row['t'],*row['xyz']])
assert any(row['phase']=='03-DELIVERY' for row in trajectory) and trajectory[-1]['phase']=='return_land'
video=read('video-metadata.json');assert video['video_sha256']==sha(ROOT/'onboard-timelapse.mp4')
manifest=read('files.sha256.json')
for name,h in manifest.items():
 p=ROOT/name;assert p.resolve().is_relative_to(ROOT.resolve()) and sha(p)==h,name
print(json.dumps(dict(status='passed',native_steps=2,forecasts_recomputed=4,cargo_receipt_verified=True,trajectory_points=len(trajectory),files=len(manifest),gpu_requested=False)))
