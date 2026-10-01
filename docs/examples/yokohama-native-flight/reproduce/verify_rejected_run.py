"""Reopen the recorded rejection, without models, GPU, dispatch or a success claim."""

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--repo", required=True, type=Path)
p.add_argument("--run", required=True, type=Path)
p.add_argument("--output", required=True, type=Path)
a = p.parse_args()
sys.path.insert(0, str(a.repo))
from scripts import ship_anwm  # noqa: E402
from src.runtime.yokohama_native import (  # noqa: E402
    digest,
    forecast_consistency,
    load_capture,
    past_view,
    vla_candidate,
)


def read(path):
    return json.loads(path.read_text())


r = a.run
cfg, result = read(r / "config.json"), read(r / "result.json")
events = [json.loads(s) for s in (r / "flight-events.jsonl").read_text().splitlines()]
rows = [json.loads(s) for s in (r / "flight-trajectory.jsonl").read_text().splitlines()]
assert cfg["decisions"]["backend"] == "native"
assert result["status"] == "failed" and result["cleanup"] is True
assert (
    result["observed"]["reason"]
    == "ValueError: WAM visible-structure consistency rejected; no segment dispatched"
)
assert result["vla_invoked"] is True and result["wam_invoked"] is True
pairs = []
for path in sorted((r / "decisions").glob("*-request.json")):
    q, s = read(path), read(path.with_name(path.name.replace("-request", "-response")))
    assert s["request_sha256"] == digest(q)
    assert q["run_id"] == cfg["run_id"] and q["config_sha256"] == digest(cfg)
    pairs.append((path, q, s))
assert [q["operation"] for _, q, _ in pairs] == ["start", "vla", "wam", "stop"]
identity = pairs[0][2]["value"]["services"]
_, vq, vs = pairs[1]
wp, wq, ws = pairs[2]
vf = pairs[1][0].with_name(pairs[1][0].name.removesuffix("-request.json"))
wf = wp.with_name(wp.name.removesuffix("-request.json"))
v, w = read(vf / "native-response.json"), read(wf / "native-response.json")
assert v["vla_inference_invoked"] and w["wam_inference_invoked"]
assert v["service"] == identity["vla"] and w["identity"] == identity["wam"]
assert v["request_sha256"] == digest(read(vf / "native-request.json"))
assert vs["value"]["vla_response_sha256"] == digest(v)
assert vla_candidate(v["generated_text"], vq["observation"]) == vs["value"]["candidate"]
for value in [v, w]:
    assert value["cuda_workspaces_cleared"] is True
    assert value["cuda_allocated_after_request_bytes"] == 0
for q in [vq, wq]:
    cap = r / q["capture"]["file"]
    assert ship_anwm.digest(cap) == q["capture"]["sha256"]
    load_capture(cap)
assert vq["capture"] != wq["capture"]
request, arrays = ship_anwm.validate(wf / "input/request.json")
_, original = load_capture(r / wq["capture"]["file"])
assert all(np.array_equal(arrays[k], original[k]) for k in arrays)
assert w["request_sha256"] == ship_anwm.digest(wf / "input/request.json")
assert w["history_sha256"] == request["history_sha256"]
assert request["vla_response_sha256"] == digest(v)
checks = []
for c in request["candidates"]:
    forecast = next(f for f in w["forecasts"] if f["candidate"] == c)
    path = wf / (c["id"] + "-prediction.png")
    assert ship_anwm.digest(path) == forecast["files"]["prediction"]["sha256"]
    ref, mask = past_view(arrays, ship_anwm.action_pose(arrays["poses"][-1], c["delta"]))
    checks.append(
        dict(candidate=c, **forecast_consistency(np.asarray(Image.open(path)), ref, mask))
    )
assert checks == ws["value"]["checks"] and not all(c["passed"] for c in checks)
assert not any(
    e["event"] in {"city_permit_consumed", "city_segment_dispatched", "city_segment_arrived"}
    for e in events
)
assert any(e["event"] == "city_session_revoked" for e in events)
shutdown = result["model_shutdown"]
assert shutdown["remote_model_processes_absent"] is True
assert shutdown["gpu_compute_processes"] == [] and shutdown["session_revoked"] is True
start = next(e for e in events if e["event"] == "city_request" and e["operation"] == "start")
held = next(e for e in events if e["event"] == "hold_measured")
stop = next(e for e in events if e["event"] == "city_response" and e["operation"] == "stop")
assert held["wall_s"] < start["wall_s"] < stop["wall_s"]
samples = [x for x in rows if start["wall_s"] <= x["wall_s"] <= stop["wall_s"]]
origin = pairs[0][1]["observation"]["vehicle"]["xyz"]
max_drift = max(math.dist(x["vehicle"]["xyz"], origin) for x in samples)
max_speed = max(math.hypot(*x["velocity_ned"]) for x in samples)
max_gap = max(b["wall_s"] - a["wall_s"] for a, b in zip(samples, samples[1:]))
assert max_drift <= 0.5 and max_speed <= 0.3 and max_gap <= 2
assert all(x["nav_state"] == 4 for x in samples)
out = {
    "status": "failure_record_verified",
    "native_model_flight_verified": False,
    "run_id": cfg["run_id"],
    "vla_flight_input_calls": 1,
    "wam_flight_input_calls": 1,
    "wam_candidate_predictions": len(checks),
    "dispatches": 0,
    "observed_model_updates": 0,
    "startup_after_inland_hold": True,
    "remote_model_shutdown_verified": True,
    "cuda_after_both_requests_bytes": 0,
    "forecast_checks": checks,
    "host_seconds": {q["operation"]: s["elapsed_s"] for _, q, s in pairs},
    "model_wait": {
        "wall_s": stop["wall_s"] - start["wall_s"],
        "max_drift_3d_m": max_drift,
        "max_speed_mps": max_speed,
        "max_observation_gap_s": max_gap,
        "samples": len(samples),
    },
    "raw_result_sha256": ship_anwm.digest(r / "result.json"),
    "vla_response_sha256": ship_anwm.digest(vf / "native-response.json"),
    "wam_response_sha256": ship_anwm.digest(wf / "native-response.json"),
    "complete_flight_qualified": False,
    "warmup_counted_as_flight_input": False,
}
a.output.write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out))
