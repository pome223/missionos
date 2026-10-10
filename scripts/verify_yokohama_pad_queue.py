#!/usr/bin/env python3
"""Reopen aircraft/MissionOS exchanges and recorded lead motion; no simulator launch."""

import argparse
import json
import math
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_pad_queue import (  # noqa: E402
    clearance, clear_window, digest, require_response, require_wait,
)
from src.runtime.yokohama_payload import read_jsonl  # noqa: E402
from scripts.yokohama_altitude_contract import (  # noqa: E402
    field, listener_age, recheck_mapping, verify_transport_evidence,
)

REVIEWED_RETRY_PX4 = "381149fb012762f5e38c4a7fdc1b905b28038970"


def pad_activation_state(row, nav_state=4, now_wall_s=None):
    """Fresh status plus event-driven mission progress; absent fields fail closed."""
    status, mission = (row["raw_px4"][k] for k in ("vehicle_status", "mission_result"))

    def flag(raw, name, expected):
        match = re.search(r"^\s*" + name + r":\s*(True|False)\s*$", raw, re.M)
        return match is not None and (match[1] == "True") is expected

    age, stamp, changed = listener_age(status), field(status, "timestamp"), field(status, "nav_state_timestamp")
    capture = row["altitude_capture"]
    now = row["wall_s"] if now_wall_s is None else now_wall_s
    if not (age is not None and age >= 0
            and now >= capture["end_worker_wall_s"] >= capture["begin_worker_wall_s"]
            and 0 <= age + now - capture["begin_worker_wall_s"] <= 2
            and stamp is not None and changed is not None and 0 < changed <= stamp
            and field(status, "nav_state") == field(status, "nav_state_user_intention") == row["nav_state"] == nav_state
            and field(status, "arming_state") == row["arming_state"] == 2
            and field(status, "executor_in_charge") == field(status, "failsafe_defer_state") == 0
            and flag(status, "failsafe", False) and flag(status, "failsafe_and_user_took_over", False)
            and flag(mission, "valid", True) and flag(mission, "finished", False)
            and flag(mission, "failure", False)
            and field(mission, "mission_id") == row["mission_id"]
            and field(mission, "seq_total") == row["mission_count"]):
        raise ValueError("Uncertain, stale, failsafe or transitioned pad activation state")
    current, reached = field(mission, "seq_current"), field(mission, "seq_reached")
    if (current is None or reached is None or current != int(current) or reached != int(reached)
            or not (0 <= current < row["mission_count"] and -1 <= reached < row["mission_count"])):
        raise ValueError("Missing pad mission progress")
    return changed, current, reached


def fixed_route_dispatch_checks(config, events, rows, grant):
    """Verify every mode send for one fixed-route entry, including bounded retries.

    This is sampled simulation evidence, not an acknowledgement of a mode change.
    The caller separately verifies the complete upload/altitude transport chain.
    """
    checked = [e for e in events if e["event"] == "pad_entry_dispatch_checked"]
    if [e for e in events if e["event"] == "pad_entry_authorized"] != [grant]:
        raise ValueError("Pad retries require exactly one unchanged entry grant")
    sent = [e for e in events if e["event"] == "altitude_transport_command_sent"
            and (e.get("phase") == "03-DELIVERY" or e.get("segment") == "03-DELIVERY")]
    if not 1 <= len(checked) == len(sent) <= 4:
        raise ValueError("Pad entry requires one checked send per attempt, at most four")
    if len(sent) > 1:
        runtime = config.get("simulator_heading_runtime", {})
        verified = [e for e in events if e["event"] == "simulator_heading_runtime_verified"]
        if (runtime.get("px4_git") != REVIEWED_RETRY_PX4 or len(verified) != 1
                or REVIEWED_RETRY_PX4 not in verified[0].get("px4", "")
                or verified[0]["wall_s"] >= grant["wall_s"]):
            raise ValueError("Unreviewed PX4 mode retry semantics")
    paired = [e for e in events if e in checked or e in sent]
    if paired != [e for pair in zip(checked, sent) for e in pair]:
        raise ValueError("Pad check/send order changed")
    prepared = [e for e in events if e["event"] == "altitude_transport_prepared"
                and (e.get("phase") == "03-DELIVERY" or e.get("segment") == "03-DELIVERY")]
    uploaded = [e for e in events if e["event"] == "upload_receipt"
                and (e.get("phase") == "03-DELIVERY" or e.get("segment") == "03-DELIVERY")]
    if len(prepared) != 1 or len(uploaded) != 1:
        raise ValueError("Pad entry must use one prepared and uploaded mission")
    permission = grant["permit"]
    advice = [e for e in events if e["event"] == "missionos_advice_received"
              and e["response"].get("request_id") == permission["request_id"]]
    if (len(advice) != 1 or advice[0]["response"]["proposed_action"] != "enter_delivery_approach"
            or digest(advice[0]["response"]) != permission["response_sha256"]
            or advice[0]["observation"] != permission["rules_checked_at"]
            or not config.get("operator_approval")
            or permission.get("operator_approval") != config["operator_approval"]
            or not permission["rules_checked_at"]["wall_s"] <= advice[0]["wall_s"] <= grant["wall_s"]):
        raise ValueError("Pad entry grant differs from its approved observed response")
    boundaries = [advice[0], grant, prepared[0], uploaded[0], *paired]
    if [e for e in events if e in boundaries] != boundaries:
        raise ValueError("Pad grant/upload/send journal order changed")
    mapping, receipt = prepared[0]["mapping"], uploaded[0]
    if not grant["wall_s"] < prepared[0]["wall_s"] <= receipt["wall_s"] < checked[0]["wall_s"]:
        raise ValueError("Pad upload precedes permission or follows dispatch")
    row_hashes = {digest(r) for r in rows}
    approved = permission["rules_checked_at"]
    if permission.get("hold_xyz_m") is not None or digest(approved) not in row_hashes:
        raise ValueError("Fixed-route entry grant has a foreign hold or observation")
    require_wait(config, approved)
    mission = sent[0]["observation"]["mission_id"]
    mission_count = len(mapping["mission_items"])
    if not isinstance(mission, (int, float)) or isinstance(mission, bool) or not math.isfinite(mission) or mission <= 0:
        raise ValueError("Missing pad mission identity")
    initial_state = pad_activation_state(sent[0]["observation"])
    current_items = [item[0] for item in mapping["mission_items"] if item[5] == 1]
    if current_items != [initial_state[1]] or initial_state[2] != -1:
        raise ValueError("Pad mission progress differs from the fresh uploaded start")
    first = sent[0]["observation"]

    def guarded(row, now_wall_s=None):
        require_wait(config, row)
        recheck_mapping(mapping, row, row["wall_s"])
        clear = clearance(config, row)
        if not (clear["pad_clear"] and clear["approach_clear"]
                and math.dist(row["vehicle"]["xyz"], row["queue_lead"]["xyz"]) > 3
                and row["mission_valid"] is True and row["mission_id"] == mission
                and row["mission_count"] == mission_count
                and row["reset_counters"] == first["reset_counters"] == approved["reset_counters"]
                and all(row[k]["id"] == first[k]["id"] == approved[k]["id"] for k in ("vehicle", "queue_lead"))
                and pad_activation_state(row, now_wall_s=now_wall_s) == initial_state):
            raise ValueError("Pad retry lost clearance or changed mission")

    for i, (check, command) in enumerate(zip(checked, sent)):
        row, at = command["observation"], command["dispatched_at_worker_wall_s"]
        if not (check["phase"] == command["phase"] == row["phase"] == "03-DELIVERY"
                and command["segment"] == mapping["segment"] == "03-DELIVERY"
                and command["mapping_sha256"] == digest(mapping)
                and check["request_id"] == permission["request_id"]
                and check["observation"] == row and digest(row) in row_hashes
                and row["wall_s"] <= check["wall_s"] <= at <= command["wall_s"]
                and 0 <= at - row["wall_s"] <= 2
                and 0 <= at - permission["rules_checked_at"]["wall_s"] <= 30):
            raise ValueError("Unbound, stale or reordered pad mode command")
        recheck_mapping(mapping, row, at)
        guarded(row, at)
        for observed in rows:
            if row["wall_s"] < observed["wall_s"] <= at:
                guarded(observed)
        if i:
            previous = sent[i - 1]
            if row["wall_s"] - previous["wall_s"] < 3:
                raise ValueError("Pad mode retry precedes the three-second observation wait")
            window = [r for r in rows if previous["observation"]["wall_s"] <= r["wall_s"] <= at]
            if (len(window) < 3 or window[0] != previous["observation"] or row not in window
                    or any(not (0 < b["wall_s"] - a["wall_s"] <= 2
                                and 0 <= b["sim_s"] - a["sim_s"]
                                <= config["world"]["pad_queue"]["maximum_sample_gap_sim_s"])
                           for a, b in zip(window, window[1:]))):
                raise ValueError("Incomplete pad retry observation window")
            for observed in window:
                # In particular, a recorded transition followed by a return to
                # hold does not authorize another entry attempt.
                guarded(observed)
    activated = [r for r in rows if r["phase"] == "03-DELIVERY"
                 and sent[-1]["wall_s"] < r["wall_s"] <= sent[-1]["wall_s"] + 5
                 and r["nav_state"] == 3 and r["mission_valid"] is True
                 and r["mission_id"] == mission and r["mission_count"] == mission_count]
    if not activated:
        # Three seconds of waiting plus the existing two-second observation
        # capture bound. A successful CLI return is not mode acceptance.
        raise ValueError("Pad mission activation was not observed after the final send")
    # CLI success alone is not acceptance. Bind a fresh raw mode transition to
    # this run, mission and aircraft; mission_result itself is event-driven.
    final = activated[0]
    clearance(config, final)
    recheck_mapping(mapping, final, final["wall_s"])
    if (not all(final[k]["id"] == first[k]["id"] for k in ("vehicle", "queue_lead"))
            or final["reset_counters"] != first["reset_counters"]
            or pad_activation_state(final, nav_state=3)[0]
            <= field(sent[-1]["observation"]["raw_px4"]["vehicle_status"], "timestamp")):
        raise ValueError("Pad activation lacks a new PX4 transition timestamp or identity")
    return dict(every_pad_mode_send_checked=True, bounded_pad_activation_retries=True,
                pad_mission_activation_observed=True), len(sent)


def response_sequence_checks(requests, entries, repeated_waits):
    # Pending/hold judge responses add bounded waits; safety and budget evidence
    # are independently verified below, so receipt count is not fixed at two.
    count = len(requests) if repeated_waits else 1 + entries
    return dict(
        wait_then_continue=count >= 1 + entries
        and [r["action"] for r in requests]
        == ["wait_at_current_hold"] * (count - entries) + ["enter_delivery_approach"] * entries,
        sequences=[r["sequence"] for r in requests] == list(range(count)),
    )


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
        # Logical entry count and physical mode-send count are separate. The
        # fixed-route transport path below must verify ALL retries, never drop them.
        transport = "altitude_transport_contract" in c and not approach
        selected["pad_entry_dispatch_checked"] = dispatches[:1] if approach or transport else dispatches
        checks["one_occupied_wait_entry_receipt_return"] = all(
            len(v) == 1 for v in selected.values()
        ) and (len(dispatches) >= 2 if approach else True)
        if not checks["one_occupied_wait_entry_receipt_return"]:
            raise ValueError("Missing or repeated mission boundary")
        start, wait, permit, dispatch, received, landed = (selected[k][0] for k in selected)
        if transport:
            verify_transport_evidence(root, c, events)
            dispatch_checks, attempts = fixed_route_dispatch_checks(c, events, rows, permit)
            checks.update(dispatch_checks)
            summary["pad_mode_send_attempts"] = attempts
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
        received_wall_s = {}
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
            received_wall_s[req["request_id"]] = observed[0]["wall_s"]
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
        checks.update(response_sequence_checks(
            requests, entries,
            bool(c["world"].get("pad_state_advisory") or p.get("mission_judge")),
        ))
        if p.get("mission_judge"):
            judge, judge_summary = judge_checks(
                p["mission_judge"], root, exchanges, received_wall_s
            )
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


def judge_checks(policy, root, exchanges, received_wall_s=None):
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
    # Reconstruct judge-caused wall time from observations: each run of pending/hold
    # responses lasts until the release is received, or until a Rules wait (pad not
    # clear) ends it. Never trust the receipts' own totals.
    elapsed, start, last = 0.0, None, None
    for request, response in exchanges:
        receipt = response["mission_judge"]
        now = request["observations"][-1]["wall_s"]
        received = max(now, (received_wall_s or {}).get(request["request_id"], now))
        if receipt["status"] in ("pending", "hold"):
            start = now if start is None else start
            last = received
        elif start is not None:
            released = receipt["prior_action"] == "enter_delivery_approach"
            elapsed += (received if released else now) - start
            start = None
    if start is not None:
        elapsed += last - start
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
            mission_judge_within_budget=elapsed <= policy["max_added_wait_s"]
            and len(records) <= policy["max_decisions"]
            and all(
                r["added_wait_s"] <= policy["max_added_wait_s"]
                for r in receipts
                if "added_wait_s" in r
            ),
        ),
        dict(
            judge_requests=len(records),
            judge_statuses=[r["status"] for r in receipts if r["status"] != "not_consulted"],
            judge_added_wait_s=elapsed,
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
