#!/usr/bin/env python3
"""Reopen aircraft/MissionOS exchanges and recorded lead motion; no simulator launch."""

import argparse
import json
import math
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_pad_queue import clearance, clear_window, digest, require_response  # noqa: E402
from src.runtime.yokohama_payload import read_jsonl  # noqa: E402


def verify(root):
    def read(p):
        return json.loads(p.read_text())

    c = read(root / "config.json")
    result = read(root / "result.json")
    rows = read_jsonl(root / "flight-trajectory.jsonl")
    events = read_jsonl(root / "flight-events.jsonl")
    checks = dict(
        run_passed=result["status"] == "passed",
        cleanup=result.get("cleanup") is True,
        supervisor_stopped=result.get("pad_supervisor_stopped") is True,
        no_models_or_gpu=all(
            result.get(k) is False
            for k in ("vla_invoked", "wam_invoked", "gpu_requested", "physical_execution_invoked")
        ),
    )
    summary = dict(run_id=c["run_id"], checks=checks)
    try:
        p = c["world"]["pad_queue"]
        selected = {
            name: [e for e in events if e["event"] == name]
            for name in (
                "pad_occupied_reported",
                "pad_wait_executed",
                "pad_entry_authorized",
                "pad_entry_dispatch_checked",
                "payload_received",
                "landing_observed",
            )
        }
        checks["one_occupied_wait_entry_receipt_return"] = all(
            len(v) == 1 for v in selected.values()
        )
        if not checks["one_occupied_wait_entry_receipt_return"]:
            raise ValueError("Missing or repeated mission boundary")
        start, wait, permit, dispatch, received, landed = (selected[k][0] for k in selected)
        checks["event_order"] = (
            start["wall_s"]
            < wait["wall_s"]
            < permit["wall_s"]
            < dispatch["wall_s"]
            < received["wall_s"]
            < landed["wall_s"]
        )
        window = [
            r
            for r in rows
            if start["observation"]["wall_s"]
            <= r["wall_s"]
            <= permit["permit"]["rules_checked_at"]["wall_s"]
        ]
        checks["ap_wait_before_entry"] = bool(window) and all(
            r["nav_state"] == 4
            and r["arming_state"] == 2
            and r["landed"] is False
            and math.dist(r["vehicle"]["xyz"], p["wait_xyz_m"]) <= 0.6
            and math.hypot(*r["velocity_ned"]) <= 0.3
            for r in window
        )
        checks["pad_initially_occupied"] = clearance(c, window[0])["pad_clear"] is False
        row_hashes = {digest(r) for r in rows}
        requests = []
        for folder in sorted((root / "pad-decisions").iterdir()):
            req, resp = read(folder / "request.json"), read(folder / "response.json")
            observed = [
                e["observation"]
                for e in events
                if e["event"] == "missionos_advice_received" and e["response"] == resp
            ]
            if len(observed) != 1:
                raise ValueError("Response is not bound to one observed receipt")
            action = require_response(c, req, resp, observed[0])
            if not all(digest(r) in row_hashes for r in [*req["observations"], observed[0]]):
                raise ValueError("Decision used observations absent from trajectory")
            requests.append(
                dict(sequence=req["sequence"], request_id=req["request_id"], action=action)
            )
            if action == "enter_delivery_approach":
                checks["five_seconds_clear_before_proposal"] = clear_window(c, req["observations"])
                checks["entry_bound_to_response"] = permit["permit"]["request_id"] == req[
                    "request_id"
                ] and permit["permit"]["response_sha256"] == digest(resp)
        checks["wait_then_continue"] = [r["action"] for r in requests] == [
            "wait_at_current_hold",
            "enter_delivery_approach",
        ]
        checks["sequences"] = [r["sequence"] for r in requests] == [0, 1]
        after = [r for r in rows if r["wall_s"] >= permit["permit"]["rules_checked_at"]["wall_s"]]
        checks["clear_through_delivery_and_return"] = bool(after) and all(
            all(clearance(c, r)[k] for k in ("pad_clear", "approach_clear")) for r in after
        )
        minsep = min(
            math.dist(r["vehicle"]["xyz"], r["queue_lead"]["xyz"])
            for r in rows
            if r.get("queue_lead")
        )
        checks["sampled_separation_gt_3m"] = minsep > 3
        end = after[-1]["queue_lead"]["xyz"]
        checks["lead_departure_observed"] = (
            math.dist(end, p["lead_end_xyz_m"]) < 0.05
            and math.dist(end, window[0]["queue_lead"]["xyz"]) > 10
        )
        checks["own_cargo_received"] = received["receipt"]["payload_delivery_verified"] is True
        checks["returned_landed_disarmed"] = (
            landed["observation"]["landed"] is True and landed["observation"]["arming_state"] == 1
        )
        checks["no_early_approach_dispatch"] = all(
            e["wall_s"] > permit["wall_s"]
            for e in events
            if e["event"] == "upload_receipt" and e["phase"] == "03-DELIVERY"
        )
        summary.update(
            wait_sim_s=permit["permit"]["rules_checked_at"]["sim_s"]
            - start["observation"]["sim_s"],
            minimum_sampled_separation_m=minsep,
            requests=requests,
            observed_wait_samples=len(window),
            lead_motion_m=math.dist(end, window[0]["queue_lead"]["xyz"]),
        )
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        checks["evidence_complete"] = False
        summary["reason"] = str(exc)
    summary["status"] = "passed" if all(checks.values()) else "failed"
    summary["scope"] = (
        "CPU PX4 cargo flight; scripted lead, pose-based occupancy, fixture MissionOS judge; no visual/model capability claim"
    )
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    r = verify(a.run)
    a.output.write_text(json.dumps(r, indent=2) + "\n")
    print(json.dumps(r, indent=2))
    return 0 if r["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
