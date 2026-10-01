"""Reverify frozen onboard runs, then compare matched rule and local-VLM flights."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from missionos_core import canonical_sha256

from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence
from src.runtime.ship_delivery_sitl import _read_jsonl, verify_ship_sitl_run
from src.runtime.ship_onboard import RULE_POLICIES, choose_onboard_action
from src.runtime.ship_onboard_model import ENDPOINT, MODEL, MODEL_DIGEST, MODEL_POLICIES


def reverify_onboard_run(directory):
    root = Path(directory)
    report = json.loads((root / "result.json").read_text())
    config = json.loads((root / "config.json").read_text())
    if report.get("config") != config or report.get("run_id") != config["run_id"]:
        raise ValueError("Run configuration or identity mismatch")
    if not config.get("urban", {}).get("onboard_camera"):
        raise ValueError("This comparator requires actual onboard RGB")
    world = report["world_files_sha256"]
    if canonical_sha256(world) != config["world_sha256"]:
        raise ValueError("World manifest binding mismatch")
    for name, digest in world.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid world artifact path")
        if hashlib.sha256((root / "models" / path).read_bytes()).hexdigest() != digest:
            raise ValueError("World artifact hash mismatch")
    plan = {
        "scenario": {"backend": "kinematic_fixture", **report["scenario_parameters"]},
        "missions": report["missions"],
        "world": world,
        "urban": config["urban"],
    }
    if canonical_sha256(plan) != config["plan_sha256"]:
        raise ValueError("Scenario/mission approval binding mismatch")
    invocation = dict(report["runtime_invocation_evidence"])
    # Relocation does not change evidence identity. Always check the copied
    # bytes under this root, not an old absolute path left in the receipt.
    for stream in ("stdout", "stderr"):
        if Path(invocation[f"{stream}_artifact_path"]).name != f"worker.{stream}":
            raise ValueError("Unexpected worker output artifact name")
        invocation[f"{stream}_artifact_path"] = str(root.resolve() / f"worker.{stream}")
    invocation = validate_runtime_invocation_evidence(invocation)
    if invocation["run_id"] != config["run_id"] or invocation["execution_scope"] != "sim":
        raise ValueError("Runtime invocation binding mismatch")
    if invocation["worker_sha256"] != hashlib.sha256((root / "worker.py").read_bytes()).hexdigest():
        raise ValueError("Worker artifact hash mismatch")
    source_root = Path(__file__).resolve().parents[2]
    for name, digest in config["urban"]["onboard_source_sha256"].items():
        relative = "scripts" if (source_root / "scripts" / name).exists() else "src/runtime"
        if hashlib.sha256((source_root / relative / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Restore the recorded implementation before reverifying: " + name)
    for deployed, original in {
        "worker.py": "ship_delivery_sitl_worker.py",
        "urban_policy.py": "ship_urban_decision.py",
        "ship_urban_sitl_stage.py": "ship_urban_sitl_stage.py",
        "ship_onboard_camera.py": "ship_onboard_camera.py",
        "ship_onboard_entrypoint.sh": "ship_onboard_entrypoint.sh",
        "ship_urban_camera_worker.py": "ship_urban_camera_worker.py",
    }.items():
        if (
            hashlib.sha256((root / deployed).read_bytes()).hexdigest()
            != config["urban"]["onboard_source_sha256"][original]
        ):
            raise ValueError("Deployed worker/support code binding mismatch")
    samples, events = _read_jsonl(root / "telemetry.jsonl"), _read_jsonl(root / "events.jsonl")
    verified = verify_ship_sitl_run(samples, events, config, evidence_root=root)
    if (
        report.get("status") != "completed"
        or not verified["verified"]
        or invocation["invocation_exit_code"] != 0
    ):
        raise ValueError(
            "Incomplete or unverified flight: " + root.name + ": " + str(verified["reasons"])
        )
    event = next(e for e in events if e["event"] == "urban_decision")
    urban = verified["urban_verification"]
    entry = next(e["elapsed_s"] for e in events if e["event"] == "urban_entry_observed")
    end = next(e["elapsed_s"] for e in events if e["event"] == "delivery_hover_observed")
    points = [
        s["vehicle"]["xyz"] for s in samples if entry <= s["elapsed_s"] <= end and s.get("vehicle")
    ]
    decision = event["decision"]
    model_evidence = None
    if config["urban"]["policy"] in MODEL_POLICIES:
        receipt = json.loads((root / "model-invocation.json").read_text())
        model_evidence = validate_runtime_invocation_evidence(
            {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "http_loopback",
                "invocation_target": ENDPOINT + "/api/chat:" + MODEL,
                "invocation_started_at": receipt["started_at"],
                "invocation_completed_at": receipt["completed_at"],
                "invocation_exit_code": 0,
                "invocation_stdout_sha256": receipt["response_sha256"],
                "invocation_stdout_preimage": (root / "model-response.json").read_text(),
                "invocation_stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "invocation_stderr_preimage": "",
                "run_id": config["run_id"],
                "execution_scope": "sim",
                "request_sha256": receipt["request_sha256"],
                "model_digest": receipt["model_digest"],
            }
        )
    return {
        "run_id": config["run_id"],
        "case": config["urban"]["case"],
        "policy": config["urban"]["policy"],
        "action": urban["action"],
        "urban_elapsed_s": urban["urban_elapsed_s"],
        "sampled_urban_distance_m": sum(math.dist(a, b) for a, b in zip(points, points[1:])),
        "delivery_verified": verified["delivery_verified"],
        "recovery_verified": verified["recovery_verified"],
        "collision_observed": urban["collision_observed"],
        "geometric_clearance_verified": urban["geometric_clearance_verified"],
        "image_verification": verified["onboard_verification"],
        "inference_elapsed_s": decision.get("inference_elapsed_s", 0),
        "model_proposal": decision.get("model_proposal"),
        "matched_image_rule_replay": {
            p: choose_onboard_action(event["history"], p, airspeed_mps=config["airspeed_mps"])
            for p in RULE_POLICIES
        },
        "observation_history": event["history"],
        "world_sha256": config["world_sha256"],
        "sources_sha256": config["urban"]["onboard_source_sha256"],
        "image_id": report["image_id"],
        "scenario_parameters": report["scenario_parameters"],
        "vision_language_model_invoked": config["urban"]["policy"] in MODEL_POLICIES,
        "model_runtime_invocation_evidence": model_evidence,
        "native_flight_vla_invoked": report.get("vla_invoked", False),
        "action_conditioned_wam_invoked": report.get("wam_invoked", False),
        "native_integration_verification": verified.get("native_integration_verification"),
    }


def compare_onboard_runs(directories):
    runs = [reverify_onboard_run(path) for path in directories]
    expected = {
        (c, p)
        for c in ("short_clear", "long_block", "brake_stop")
        for p in ("onboard_stopping", "onboard_vlm_forecast")
    }
    if (
        len(runs) != 6
        or {(r["case"], r["policy"]) for r in runs} != expected
        or len({r["run_id"] for r in runs}) != 6
    ):
        raise ValueError("Exactly one frozen rule/model pair per case is required")
    for key in ("world_sha256", "sources_sha256", "image_id", "scenario_parameters"):
        if any(r[key] != runs[0][key] for r in runs):
            raise ValueError("Matched comparison condition differs: " + key)
    pairs = []
    for case in ("short_clear", "long_block", "brake_stop"):
        baseline = next(r for r in runs if r["case"] == case and r["policy"] == "onboard_stopping")
        model = next(r for r in runs if r["case"] == case and r["policy"] == "onboard_vlm_forecast")
        pairs.append(
            {
                "case": case,
                "rule_action": baseline["action"],
                "model_action": model["action"],
                "rule_urban_elapsed_s": baseline["urban_elapsed_s"],
                "model_urban_elapsed_s": model["urban_elapsed_s"],
                "model_minus_rule_s": model["urban_elapsed_s"] - baseline["urban_elapsed_s"],
                "inference_elapsed_s": model["inference_elapsed_s"],
                "model_differs_from_rule_on_same_images": model["action"]
                != model["matched_image_rule_replay"]["onboard_stopping"]["action"],
            }
        )
    return {
        "schema_version": "ship_onboard_comparison.v1",
        "status": "verified_local_vlm_comparison",
        "step2_native_vla_wam_completed": False,
        "runs": runs,
        "pairs": pairs,
        "model_digest": MODEL_DIGEST,
        "mean_model_minus_rule_s": sum(p["model_minus_rule_s"] for p in pairs) / 3,
        "limitations": [
            "One run per case and policy; exploratory matched-scenario comparison, not statistical superiority evidence.",
            "Separate flights have different actual images; saved-frame rule replay uses exactly the model's recorded observations.",
            "Known red obstacle, mapped depth plane, surveyed cyan visual marker and PX4 ego state; not general urban perception.",
            "General vision-language model proposes obstacle clearance time; no native flight-trained VLA or action-conditioned WAM is invoked.",
            "Wall time includes inference and simulator resource contention; it is not an onboard-compute latency measurement.",
            "One vehicle, stationary simulated ship, compact 100m offshore / 200m inland route; no physical flight or 10-drone operation.",
        ],
    }
