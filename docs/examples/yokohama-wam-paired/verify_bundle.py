"""Recompute published paired image errors. No GPU, cloud or flight access."""
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
R=Path(__file__).resolve().parent

def read(name):return json.loads((R/name).read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    value=read('comparison.json');protocol=read('protocol.json');inference=read('inference-summary.json')
    assert inference['protocol_sha256']==sha(R/'protocol.json')
    assert len(value['cases'])==inference['inference_calls']==len(protocol['cases'])==12
    assert inference['future_images_uploaded'] is False
    assert not inference['flight_invoked'] and not inference['vla_invoked']
    assert {c['id'] for c in value['cases']}=={c['id'] for c in protocol['cases']}
    pairs={p['id']:p for p in value['pairs']}
    for pair in pairs.values():
        assert sha(R/pair['actual_raw'])==pair['actual_rgb_sha256']
        assert sha(R/pair['actual'])==pair['actual_cropped_sha256']
        cropped=Image.open(R/pair['actual_raw']).convert('RGB').crop((80,0,560,360)).resize((224,224),Image.Resampling.BILINEAR)
        np.testing.assert_array_equal(np.asarray(cropped),np.asarray(Image.open(R/pair['actual'])))
        assert pair['target_camera_error_m']<=.3 and pair['target_camera_rotation_error_rad']<=.05
    for case in value['cases']:
        assert sha(R/case['image'])==case['prediction_sha256']
        a=np.asarray(Image.open(R/case['image']).convert('RGB'),dtype=float)
        b=np.asarray(Image.open(R/pairs[case['pair']]['actual']).convert('RGB'),dtype=float)
        e=np.abs(a-b);mae=float(e.mean());bad=float((e.max(2)>40).mean())
        assert abs(mae-case['metrics']['rgb_mae'])<1e-10
        assert abs(bad-case['metrics']['fraction_pixels_max_channel_error_over_40'])<1e-10
        passed=mae<=15 and bad<=.1 and case['elapsed_s']<=75
        assert case['numeric_pass'] is passed
    budget=read('budget.json')
    assert budget['cleanup_confirmed'] and budget['cost_estimate_closed']
    assert value['cumulative_estimated_usd']==budget['cumulative_estimated_usd']<=15
    manifest=read('manifest.json')
    for name,digest in manifest['sha256'].items():assert sha(R/name)==digest,name
    print(json.dumps(dict(status='publication_record_verified',images=12,
                         numeric_passes=sum(c['numeric_pass'] for c in value['cases']),
                         native_flight_qualified=value['native_flight_qualified'],
                         files_hashed=len(manifest['sha256']),full_raw_flight_reverified=False)))

if __name__=='__main__':main()
