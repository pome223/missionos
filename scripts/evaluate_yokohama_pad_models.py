#!/usr/bin/env python3
"""Reopen diagnostic model I/O. No claim of AP or dynamic-time prediction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import ship_anwm  # noqa: E402
from scripts.probe_yokohama_pad_models import sha, verify_inputs, write  # noqa: E402
from src.runtime.yokohama_native import forecast_consistency, past_view  # noqa: E402

EXPECTED = {
    "far-busy": "wait",
    "near-busy": "wait",
    "near-clear": "short_forward_step",
    "near-reoccupied": "wait",
}


def evaluate(inputs, results):
    rows = []
    for folder, source in verify_inputs(inputs):
        key = folder.name
        vp, wp = results / "vla" / key, results / "wam" / key
        vla = json.loads((vp / "result.json").read_text())
        if vla["request_sha256"] != sha(folder / "request.json"):
            raise ValueError("Native VLA response was not for this observation")
        if sha(vp / "mosaic.png") != vla["mosaic_sha256"]:
            raise ValueError("Native model input mosaic changed")
        if not vla["vla_inference_invoked"] or vla["dispatch_invoked"]:
            raise ValueError("Wrong runtime boundary")
        request, arrays = ship_anwm.validate(wp / "request.json")
        if request["history_sha256"] != source["history_sha256"]:
            raise ValueError("WAM history does not match VLA source")
        if request["vla_response_sha256"] != sha(vp / "result.json"):
            raise ValueError("WAM does not bind the native VLA output")
        if request["candidates"][1]["delta"] != vla["proposal"]["requested_body_frd_delta_m_rad"]:
            raise ValueError("VLA proposal changed before WAM")
        wam = json.loads((wp / "result.json").read_text())
        if (
            wam["request_sha256"] != sha(wp / "request.json")
            or wam["dispatch_invoked"]
            or not wam["wam_inference_invoked"]
            or wam["aircraft_flown"]
            or vla["aircraft_flown"]
        ):
            raise ValueError("Wrong WAM response boundary")
        forecasts = []
        for forecast, candidate in zip(wam["forecasts"], request["candidates"], strict=True):
            if forecast["candidate"] != candidate:
                raise ValueError("Wrong forecast candidate")
            entry = forecast["files"]["prediction"]
            path = wp / entry["file"]
            if sha(path) != entry["sha256"]:
                raise ValueError("Forecast bytes changed")
            predicted = np.asarray(Image.open(path).convert("RGB"))
            pose = ship_anwm.action_pose(arrays["poses"][-1], candidate["delta"])
            reference, mask = past_view(arrays, pose)
            scores = forecast_consistency(predicted, reference, mask)
            eligibility = forecast_consistency(reference, reference, mask)
            forecasts.append(
                dict(
                    candidate=candidate,
                    consistency=scores,
                    reference_self_check=eligibility,
                    reference_eligible=eligibility["passed"],
                    inference_seconds=forecast["elapsed_s"],
                )
            )
        rows.append(
            dict(
                case=key,
                expected=EXPECTED[key],
                native_text=vla["generated_text"],
                proposed=vla["proposal_kind"],
                choice_matches=vla["proposal_kind"] == EXPECTED[key],
                native_vla_seconds=vla["inference_seconds"],
                forecasts=forecasts,
                cuda_released=(
                    vla["cuda_allocated_after_request_bytes"] == 0
                    and wam["cuda_allocated_after_request_bytes"] == 0
                ),
                vla_result_sha256=sha(vp / "result.json"),
                wam_result_sha256=sha(wp / "result.json"),
            )
        )
    primary = [r for r in rows if r["case"] != "far-busy"]
    choices_pass = all(r["choice_matches"] for r in primary)
    views_pass = all(f["consistency"]["passed"] for r in primary for f in r["forecasts"])
    return dict(
        schema="yokohama_pad_native_probe_evaluation.v1",
        evidence_integrity="passed",
        primary_cases=len(primary),
        primary_choice_matches=sum(r["choice_matches"] for r in primary),
        proposal_discrimination_passed=choices_pass,
        primary_visible_structure_passed=views_pass,
        reference_eligibility_passed=all(
            f["reference_eligible"] for r in primary for f in r["forecasts"]
        ),
        native_flight_admission_passed=choices_pass
        and views_pass
        and all(r["cuda_released"] for r in rows),
        rows=rows,
        vla_invocations=len(rows),
        wam_forecasts=sum(len(r["forecasts"]) for r in rows),
        learned_weights_changed=False,
        same_sortie_native_flight=False,
        dynamic_future_time_prediction_verified=False,
        physical_execution=False,
        comparator="Prior CPU current-occupancy telemetry; no measured benefit comparison",
        forecast_reference="Independent reprojection of last observed RGBD; not a later flown view",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = evaluate(a.inputs, a.results)
    write(a.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
