from pathlib import Path
import json,hashlib
import torch
r=Path.home()/"wam-adapt"
if not (r/"results/adapter.pt").exists():
 print("adapter_not_ready");raise SystemExit(0)
torch.set_num_threads(1)
a=torch.load(r/"results/adapter.pt",map_location="cpu",weights_only=True)
h=torch.load(r/"initial-adapter.pt",map_location="cpu",weights_only=True)
base=torch.load(r/"assets/0200000.pth.tar",map_location="cpu",weights_only=False,mmap=True)
rows=[]
for name,value in a["state"].items():
 initial=base["ema"][name] if name.startswith("blocks.27.") else h["state"][name]
 delta=(value-initial).float()
 rows.append(dict(name=name,parameters=value.numel(),initial_sha256=hashlib.sha256(initial.contiguous().numpy().tobytes()).hexdigest(),final_sha256=hashlib.sha256(value.contiguous().numpy().tobytes()).hexdigest(),change_l2=float(delta.square().sum().sqrt()),all_finite=bool(torch.isfinite(value).all())))
assert all(x["all_finite"] and x["change_l2"]>0 for x in rows)
record=dict(schema_version="anwm_changed_parameters_audit.v1",status="verified",source="separate CPU process; saved adapter compared with released EMA final block and prior attention adapter",adapter_sha256=hashlib.sha256((r/"results/adapter.pt").read_bytes()).hexdigest(),protocol_sha256=hashlib.sha256((r/"protocol.json").read_bytes()).hexdigest(),cuda_initialized=torch.cuda.is_initialized(),parameters=rows)
(r/"results/parameter-change-audit.json").write_text(json.dumps(record,indent=2)+"\n")
print(json.dumps(record))
