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
    approach = bool(c.get("decisions", {}).get("pad_approach"))
    checks = dict(
        run_passed=result["status"] == "passed",
        cleanup=result.get("cleanup") is True,
        supervisor_stopped=result.get("pad_supervisor_stopped") is True,
    )
    if approach:
        # Model participation is verified separately by verify_yokohama_decisions.
        checks["no_physical_execution"] = result.get("physical_execution_invoked") is False
    elif c.get("decisions"):
        # City models (verified by verify_yokohama_decisions) must be revoked and
        # stopped before the pad wait; the queue itself stays Rules + advisory.
        names = [e["event"] for e in events]
        checks["no_physical_execution"] = result.get("physical_execution_invoked") is False
        checks["city_models_stopped_before_pad_wait"] = (
            "city_session_revoked" in names
            and "pad_occupied_reported" in names
            and names.index("city_session_revoked") < names.index("pad_occupied_reported")
            and not any(
                e["event"] == "city_request"
                and e["wall_s"] > events[names.index("pad_occupied_reported")]["wall_s"]
                and e.get("operation") != "stop"
                for e in events
            )
        )
    else:
        checks["no_native_models_or_gpu"] = all(
            result.get(k) is False
            for k in ("vla_invoked", "wam_invoked", "gpu_requested", "physical_execution_invoked")
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
        dispatches = selected["pad_entry_dispatch_checked"]
        # A D3 model step and its delivery connector are each dispatch-checked.
        selected["pad_entry_dispatch_checked"] = dispatches[:1] if approach else dispatches
        checks["one_occupied_wait_entry_receipt_return"] = all(
            len(v) == 1 for v in selected.values()
        ) and (len(dispatches) >= 2 if approach else True)
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
        requests, enters, exchanges = [], [], []
        for folder in sorted((root / "pad-decisions").iterdir()):
            req, resp = read(folder / "request.json"), read(folder / "response.json")
            exchanges.append((req, resp))
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
                enters.append((req, resp))
        checks["five_seconds_clear_before_proposal"] = bool(enters) and all(
            clear_window(c, req["observations"], req.get("hold_xyz_m")) for req, _ in enters
        )
        checks["entry_bound_to_response"] = (
            bool(enters)
            and permit["permit"]["request_id"] == enters[0][0]["request_id"]
            and permit["permit"]["response_sha256"] == digest(enters[0][1])
        )
        entries = 3 if approach else 1
        count = len(requests) if c["world"].get("pad_state_advisory") else 1 + entries
        checks["wait_then_continue"] = count >= 1 + entries and [r["action"] for r in requests] == (
            ["wait_at_current_hold"] * (count - entries) + ["enter_delivery_approach"] * entries
        )
        if p.get("mission_judge"):
            judge, judge_summary = judge_checks(p["mission_judge"], root, exchanges)
            checks.update(judge)
            summary.update(judge_summary)
        if approach:
            checks.update(approach_checks(c, events, enters, dispatches))
            regrants = [e for e in events if e["event"] == "pad_entry_reconfirmed"]
            if regrants:
                # Latency qualification: how stale the first permission was when
                # the fresh pre-authority reconfirmation replaced it.
                summary["first_permit_age_at_model_authority_s"] = (
                    regrants[0]["permit"]["rules_checked_at"]["wall_s"]
                    - permit["permit"]["rules_checked_at"]["wall_s"]
                )
        checks["sequences"] = [r["sequence"] for r in requests] == list(range(count))
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


def judge_checks(policy, root, exchanges):
    """A pad mission judge only added bounded waits, and every receipt matches its record."""
    receipts = [resp["mission_judge"] for _, resp in exchanges]
    records = {}
    folder = root / "pad-judge"
    for path in sorted(folder.glob("*/request.json")) if folder.is_dir() else []:
        request = json.loads(path.read_text())
        answer_path = path.parent / "response.json"
        answer = json.loads(answer_path.read_text()) if answer_path.is_file() else None
        content = {k: v for k, v in request.items() if k != "judge_request_id"}
        records[request["judge_request_id"]] = (request, answer, digest(content))
    referenced = {r["judge_request_id"] for r in receipts if r.get("judge_request_id")}
    held = [r for r in receipts if r["status"] in ("pending", "hold")]
    answered = [a for _, a, _ in records.values() if a and a.get("judge_status") == "valid"]
    return (
        dict(
            mission_judge_consulted=bool(records),
            mission_judge_records_bound=referenced == set(records)
            and all(identity == d for identity, (_, _, d) in records.items())
            and all(
                records[r["judge_request_id"]][1] is not None
                and digest(records[r["judge_request_id"]][1]) == r["judgment_sha256"]
                for r in receipts
                if "judgment_sha256" in r
            ),
            mission_judge_only_added_waiting=all(
                r["prior_action"] == "enter_delivery_approach" for r in held
            ),
            mission_judge_within_budget=len(records) <= policy["max_decisions"]
            and all(r["added_wait_s"] <= policy["max_added_wait_s"] for r in held),
        ),
        dict(
            judge_requests=len(records),
            judge_statuses=[r["status"] for r in receipts if r["status"] != "not_consulted"],
            judge_added_wait_s=max((r["added_wait_s"] for r in held), default=0),
            judge_decisions=[
                dict(
                    a["decision"],
                    invocation_kind=a.get("invocation", {}).get("invocation_kind"),
                    model_id=a.get("invocation", {}).get("model_id"),
                )
                for a in answered
            ],
        ),
    )


def approach_checks(c, events, enters, dispatches):
    """Fresh MissionOS reconfirmation around the one D3 model step."""
    reconfirmed = [e for e in events if e["event"] == "pad_entry_reconfirmed"]
    moved = [e for e in events if e["event"] == "pad_hold_moved"]
    out = dict(
        two_reconfirmations=[e["reason"] for e in reconfirmed]
        == ["before_model_approach_authority", "model_endpoint_before_delivery_connector"]
        and len(enters) == 3
        and all(
            e["permit"]["request_id"] == req["request_id"]
            and e["permit"]["response_sha256"] == digest(resp)
            for e, (req, resp) in zip(reconfirmed, enters[1:])
        ),
        one_bound_hold_move=len(moved) == 1,
    )
    if not all(out.values()):
        return out
    before, after = reconfirmed
    hold = moved[0]
    consumed = [
        e
        for e in events
        if e["event"] == "city_permit_consumed" and e["permit"]["permit_id"] == hold["permit_id"]
    ]
    model_upload = [
        e
        for e in events
        if e["event"] == "upload_receipt"
        and consumed
        and e["segment"] == consumed[0]["permit"]["upload_name"]
    ]
    delivery = [e for e in events if e["event"] == "upload_receipt" and e["phase"] == "03-DELIVERY"]
    out["hold_is_consumed_model_endpoint"] = (
        len(consumed) == 1
        and consumed[0]["permit"]["candidate"]["target_world_xyz_m"] == hold["hold_xyz_m"]
        and enters[1][0].get("hold_xyz_m") is None
        and enters[2][0].get("hold_xyz_m") == hold["hold_xyz_m"]
    )
    out["reconfirm_model_move_reconfirm_order"] = (
        len(model_upload) == 1
        and len(delivery) == 1
        and before["wall_s"]
        < model_upload[0]["wall_s"]
        < consumed[0]["wall_s"]
        < hold["wall_s"]
        < after["wall_s"]
        < delivery[0]["wall_s"]
    )
    out["dispatch_checks_within_30s_of_fresh_permit"] = all(
        any(
            g["wall_s"] <= d["wall_s"]
            and d["observation"]["wall_s"] - g["permit"]["rules_checked_at"]["wall_s"] <= 30
            for g in (before, after)
        )
        for d in dispatches
    )
    return out


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
