"""Verify published raster/metric consistency without GPU or private raw logs."""
import argparse,hashlib,json,math
from pathlib import Path
import numpy as np
from PIL import Image
from frozen_metrics import forecast_consistency

def equal_metrics(actual,expected):
 assert actual.keys()==expected.keys()
 for key in actual:
  if isinstance(expected[key],float):assert math.isclose(actual[key],expected[key],rel_tol=0,abs_tol=1e-10),key
  else:assert actual[key]==expected[key],key

def main():
 p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args();r=a.bundle
 manifest=json.loads((r/'manifest.json').read_text())
 for entry in manifest['files']:
  path=r/entry['path'];assert path.resolve().is_relative_to(r.resolve())
  assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256'],str(path)
 summary=json.loads((r/'summary.json').read_text());case_ids=[]
 def im(name):return np.asarray(Image.open(r/'images'/name).convert('RGB'))
 mask=np.load(r/'images/city-known-mask.npy',allow_pickle=False)
 for case in summary['cases']:
  pred=im(case['id']+'.png');city=case['scene']=='city'
  reference=im('city-past-reference.png') if city else im('reference-observed.png')
  known=mask if city else np.ones((224,224),bool)
  calculated=forecast_consistency(pred,reference,known)
  equal_metrics(calculated,case['visible_consistency'])
  observed=im('city-observed.png' if city else 'reference-observed.png')
  assert math.isclose(float(np.abs(pred.astype(float)-observed).mean()),case['full_frame_rgb_mae_to_observed'],rel_tol=0,abs_tol=1e-10)
  assert case['dispatch_allowed'] is False;case_ids.append(case['id'])
 assert len(case_ids)==len(set(case_ids))==6
 for item in summary['vae_reconstruction']:
  name=item['input'];original={'city':'city-observed.png','projection':'hold-projection.png','reference':'reference-observed.png'}[name]
  pred=im('vae-'+name+'-'+item['posterior']+'.png');obs=im(original)
  assert math.isclose(float(np.abs(pred.astype(float)-obs).mean()),item['rgb_mae'],rel_tol=0,abs_tol=1e-10)
  equal_metrics(forecast_consistency(pred,obs,np.ones((224,224),bool)),item['visible_consistency'])
 old=im('original-flight-hold.png');reproduced=im('city_original_t4.png')
 assert float(np.abs(old.astype(float)-reproduced).mean())==summary['original_reproduction']['rgb_mae']
 assert summary['flight_invoked'] is False and summary['vla_invoked'] is False
 print(json.dumps({'status':'publication_record_verified','cases':6,'vae_reconstructions':6,'gpu_used':False,'flight_qualification':False,'files_hashed':len(manifest['files'])}))
if __name__=='__main__':main()
