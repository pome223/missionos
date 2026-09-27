"""Reopen city requests, predictions, permits and measured arrivals; never run models."""

from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import ship_anwm  # noqa: E402
from src.runtime.yokohama_native import (  # noqa: E402
    digest,
    forecast_consistency,
    geometry_rules,
    load_capture,
    past_view,
    vla_candidate,
)


def read(path):
    return json.loads(path.read_text())


def verify(root):
    config, result = read(root / "config.json"), read(root / "result.json")
    events = [json.loads(s) for s in (root / "flight-events.jsonl").read_text().splitlines()]
    rows = [json.loads(s) for s in (root / "flight-trajectory.jsonl").read_text().splitlines()]
    native = config["decisions"]["backend"] == "native"
    checks, details = {}, []
    requests = []
    for path in sorted((root / "decisions").glob("*-request.json")):
        request = read(path)
        response = read(path.with_name(path.name.replace("-request", "-response")))
        assert response["request_sha256"] == digest(request)
        assert request["run_id"] == config["run_id"] and request["config_sha256"] == digest(config)
        requests.append((path, request, response))
    checks["mailboxes_bound"] = bool(requests)
    checks["successful_run_and_shutdown"] = (
        result["status"] == "passed"
        and result.get("cleanup") is True
        and result.get("model_shutdown", {}).get("session_revoked") is True
    )
    checks["two_observed_updates"] = (
        len([e for e in events if e["event"] == "city_segment_arrived"]) == 2
    )
    starts = [e for e in events if e["event"] == "city_request" and e["operation"] == "start"]
    entry = [e for e in events if e["event"] == "hold_measured" and e["phase"] == "00-D1"]
    checks["startup_after_inland_hold"] = (
        len(starts) == len(entry) == 1 and starts[0]["wall_s"] > entry[0]["wall_s"]
    )
    stopped = [e for e in events if e["event"] == "city_response" and e["operation"] == "stop"]
    revoked = [e for e in events if e["event"] == "city_session_revoked"]
    replay = [e for e in events if e["event"] == "city_late_response_rejected"]
    ap_after = [e for e in events if e["event"] == "upload_receipt" and e["phase"] == "02-D3"]
    checks["shutdown_before_ap_continuation"] = (
        len(stopped) == len(revoked) == len(replay) == len(ap_after) == 1
        and revoked[0]["wall_s"]
        < stopped[0]["wall_s"]
        <= replay[0]["wall_s"]
        < ap_after[0]["wall_s"]
    )
    checks["native_invocation_labels"] = all(
        result[k] is native for k in ("vla_invoked", "wam_invoked", "gpu_requested")
    )
    checks["native_remote_shutdown"] = (
        not native or result["model_shutdown"].get("remote_model_processes_absent") is True
    )
    last_arrival = -1
    for cycle in (1, 2):
        group = {r["operation"]: (path, r, s) for path, r, s in requests if r["cycle"] == cycle}
        vpath, vr, vs = group["vla"]
        wpath, wr, ws = group["wam"]
        apath, ar, ars = group["activate"]
        assert not any(
            "error" in s
            for _, _, s in (group["vla"], group["wam"], group["authorize"], group["activate"])
        )
        for req in (vr, wr):
            entry = req["capture"]
            capture = root / entry["file"]
            assert ship_anwm.digest(capture) == entry["sha256"]
            _, a = load_capture(capture)
            assert a["stamps_ns"][0] / 1e9 > last_arrival
            assert 0 <= req["observation"]["sim_s"] - a["stamps_ns"][-1] / 1e9 <= 2
        assert vr["capture"] != wr["capture"]
        vfolder = vpath.with_name(vpath.name.removesuffix("-request.json"))
        raw_vla = read(vfolder / "native-response.json")
        candidate = vla_candidate(raw_vla["generated_text"], vr["observation"])
        assert candidate == vs["value"]["candidate"]
        assert digest(raw_vla) == vs["value"]["vla_response_sha256"]
        if native:
            assert raw_vla["vla_inference_invoked"] is True
            assert raw_vla["request_sha256"] == digest(read(vfolder / "native-request.json"))
        wfolder = wpath.with_name(wpath.name.removesuffix("-request.json"))
        request, arrays = ship_anwm.validate(wfolder / "input/request.json")
        assert request["vla_response_sha256"] == digest(raw_vla)
        if native:
            raw_wam = read(wfolder / "native-response.json")
            assert raw_wam["wam_inference_invoked"] is True
            assert raw_wam["request_sha256"] == ship_anwm.digest(wfolder / "input/request.json")
            assert raw_wam["history_sha256"] == request["history_sha256"]
        recomputed = []
        for c in request["candidates"]:
            reference, mask = past_view(
                arrays, ship_anwm.action_pose(arrays["poses"][-1], c["delta"])
            )
            prediction = np.array(Image.open(wfolder / (c["id"] + "-prediction.png")))
            recomputed.append(
                dict(candidate=c, **forecast_consistency(prediction, reference, mask))
            )
        assert recomputed == ws["value"]["checks"] and all(c["passed"] for c in recomputed)
        permit = ars["value"]
        assert permit["prepared_permit_sha256"] == digest(group["authorize"][2]["value"])
        assert permit["candidate"] == candidate
        assert permit["vla_response_sha256"] == digest(raw_vla)
        assert permit["wam_assessment_sha256"] == digest(ws["value"])
        assert permit["observation_sha256"] == digest(ar["observation"])
        assert permit["rules"] == geometry_rules(
            ar["observation"]["vehicle"]["xyz"],
            candidate["target_world_xyz_m"],
            group["authorize"][1]["next_target_world_xyz_m"],
            config,
            REPO / "docs/examples/yokohama-urban-scene",
        )
        for key in ("upload", "connector"):
            assert (
                ship_anwm.digest(root / (permit[key + "_name"] + "-upload.py"))
                == permit[key + "_sha256"]
            )
        consume = next(
            e
            for e in events
            if e["event"] == "city_permit_consumed"
            and e["permit"]["permit_id"] == permit["permit_id"]
        )
        dispatch = next(
            e
            for e in events
            if e["event"] == "city_segment_dispatched" and e["permit_id"] == permit["permit_id"]
        )
        arrival = next(
            e
            for e in events
            if e["event"] == "city_segment_arrived" and e["permit_id"] == permit["permit_id"]
        )
        assert (
            consume["permit"] == permit
            and consume["wall_s"] < dispatch["wall_s"] < arrival["wall_s"]
        )
        assert dispatch["wall_s"] <= permit["expires_at_worker_wall_s"] + 0.2
        assert any(
            e["event"] == "upload_receipt"
            and e["segment"] == permit["upload_name"]
            and e["receipt"]["mission_ack_type"] == 0
            and e["wall_s"] < consume["wall_s"]
            for e in events
        )
        end = arrival["observation"]
        error = math.dist(end["vehicle"]["xyz"], candidate["target_world_xyz_m"])
        assert error <= 0.25 and end["nav_state"] == 4 and math.hypot(*end["velocity_ned"]) <= 0.3
        moved = math.dist(end["vehicle"]["xyz"], ar["observation"]["vehicle"]["xyz"])
        assert moved >= 0.25
        samples = [
            r for r in rows if vr["observation"]["wall_s"] <= r["wall_s"] <= consume["wall_s"]
        ]
        assert len(samples) >= 2
        assert all(
            r["nav_state"] == 4
            and math.dist(r["vehicle"]["xyz"], vr["observation"]["vehicle"]["xyz"]) <= 0.5
            for r in samples
        )
        assert max(b["wall_s"] - a["wall_s"] for a, b in zip(samples, samples[1:])) <= 2
        last_arrival = end["sim_s"]
        details.append(
            dict(
                cycle=cycle,
                vla_bins=candidate["bins"],
                observed_movement_m=moved,
                final_target_error_m=error,
                forecast_checks=recomputed,
                vla_host_seconds=vs["elapsed_s"],
                wam_host_seconds=ws["elapsed_s"],
            )
        )
    checks["reopened_model_permit_and_motion_chains"] = len(details) == 2
    return dict(
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        cycles=details,
        native_model_flight_verified=native and all(checks.values()),
        generic_obstacle_recognition_verified=False,
        payload_delivery_verified=False,
        sea_leg_verified=False,
        physical_execution_verified=False,
        energy_savings_verified=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = verify(args.run)
    except Exception as exc:
        value = dict(status="failed", reason=type(exc).__name__ + ": " + str(exc))
    args.output.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    print(json.dumps(value))
    sys.exit(0 if value["status"] == "passed" else 1)
