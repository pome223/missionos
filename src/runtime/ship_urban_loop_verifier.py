"""Reopen control receipts, without trusting the producer's completed flag."""

from __future__ import annotations

import math

from .ship_urban_loop import UrbanLoopPlan, digest, finite


def verify_urban_loop(result):
    reasons = []
    try:
        plan = UrbanLoopPlan(**result["plan"])
        events = result["events"]
        if not events or result["plan_sha256"] != digest(result["plan"]):
            raise ValueError("plan_binding")
        session = events[0]["session_id"]
        if any(
            e["run_id"] != plan.run_id
            or e["session_id"] != session
            or e["plan_sha256"] != result["plan_sha256"]
            or not finite(e["at_s"])
            for e in events
        ) or any(b["at_s"] < a["at_s"] for a, b in zip(events, events[1:])):
            raise ValueError("event_binding_or_time")

        def one(name, cycle=None):
            matches = [
                e for e in events if e["event"] == name and (cycle is None or e["cycle"] == cycle)
            ]
            if len(matches) != 1:
                raise ValueError("missing_or_duplicate:" + name)
            return matches[0]

        def position(row, at_s, *, held=False):
            if (
                row["run_id"] != plan.run_id
                or row["phase"] != "urban"
                or row["position_valid"] is not True
                or not plan.contains(row["position_ned_m"])
                or not 0 <= at_s - row["observed_at_s"] <= plan.observation_age_s
                or not plan.reserve_fraction <= row["battery_fraction"] <= 1
                or (
                    held
                    and (
                        row["ap_mode"] != "hold"
                        or math.hypot(*row["velocity_ned_mps"]) > plan.hold_speed_mps
                    )
                )
            ):
                raise ValueError("invalid_urban_observation")

        started, ready = one("model_start_requested"), one("models_ready")
        revoked, stopped, handoff = (
            one("session_revoked"),
            one("models_stopped"),
            one("ap_return_handoff"),
        )
        if not (
            started["at_s"] <= ready["at_s"] < revoked["at_s"] <= stopped["at_s"] <= handoff["at_s"]
            and ready["identity"]["execution_scope"] == plan.execution_scope
            and stopped["verified"] is True
            and handoff["receipt"]["ap_return_observed"] is True
        ):
            raise ValueError("lifecycle_order_or_shutdown")
        if any(e["epoch"] != (0 if e["at_s"] < revoked["at_s"] else 1) for e in events):
            raise ValueError("session_epoch")
        position(started["observation"], started["at_s"], held=True)
        position(handoff["observation"], handoff["at_s"], held=True)
        if (
            math.dist(started["observation"]["position_ned_m"], plan.entry_ned_m)
            > plan.target_error_m
        ):
            raise ValueError("entry_location")
        observations = [e for e in events if e["event"] == "observation"]
        observed_by_hash = {digest(e["observation"]): e["observation"] for e in observations}
        for e in observations:
            position(e["observation"], e["at_s"])
        for a, b in zip(observations, observations[1:]):
            before, after = a["observation"], b["observation"]
            if (
                after["sequence"] <= before["sequence"]
                or not 0 < after["observed_at_s"] - before["observed_at_s"] <= plan.max_sample_gap_s
            ):
                raise ValueError("observation_sequence_or_gap")
        previous_arrival, used_images = -1.0, set()
        for cycle in range(1, plan.updates + 1):
            requests = [
                e for e in events if e["event"] == "model_requested" and e["cycle"] == cycle
            ]
            received = [e for e in events if e["event"] == "model_received" and e["cycle"] == cycle]
            if len(requests) != 2 or len(received) != 2:
                raise ValueError("two_models_per_update_required")
            for index, model in enumerate(("vla", "wam")):
                sent, got = requests[index], received[index]
                request, response = sent["request"], got["response"]
                if (
                    request["model"] != model
                    or request["cycle"] != cycle
                    or request["epoch"] != 0
                    or request["run_id"] != plan.run_id
                    or request["session_id"] != session
                    or request["plan_sha256"] != result["plan_sha256"]
                    or sent["request_sha256"] != digest(request)
                    or response["request_sha256"] != digest(request)
                    or response["model"] != model
                    or response["session_id"] != session
                    or response["dispatch_allowed"] is not False
                    or got["response_sha256"] != digest(response)
                    or not ready["at_s"] <= sent["at_s"] <= got["at_s"] < revoked["at_s"]
                    or got["at_s"] - sent["at_s"] > plan.inference_timeout_s
                ):
                    raise ValueError("model_request_response_binding")
                row = request["observation"]
                position(row, sent["at_s"], held=True)
                if (
                    digest(row) not in observed_by_hash
                    or row["image_observed_at_s"] <= previous_arrival
                    or not 0 <= sent["at_s"] - row["image_observed_at_s"] <= plan.observation_age_s
                    or row["image_sha256"] in used_images
                ):
                    raise ValueError("fresh_image_after_arrival_required")
                used_images.add(row["image_sha256"])
                for event in observations:
                    if sent["at_s"] <= event["at_s"] <= got["at_s"]:
                        position(event["observation"], event["at_s"], held=True)
                        if (
                            math.dist(
                                event["observation"]["position_ned_m"],
                                row["position_ned_m"],
                            )
                            > plan.hold_drift_m
                        ):
                            raise ValueError("model_wait_hold_drift")
            vla, wam = [e["response"] for e in received]
            if requests[1]["request"]["proposal"] != vla or wam["proposal_sha256"] != digest(vla):
                raise ValueError("wam_candidate_binding")
            authorized, dispatch, arrival = (
                one(name, cycle)
                for name in (
                    "segment_authorized",
                    "segment_dispatched",
                    "segment_arrived",
                )
            )
            permit = authorized["permit"]
            selected = [c for c in vla["candidates"] if c["id"] == wam["selected_candidate_id"]]
            if (
                len(selected) != 1
                or permit["target_ned_m"] != selected[0]["target_ned_m"]
                or permit["vla_response_sha256"] != digest(vla)
                or permit["wam_response_sha256"] != digest(wam)
                or permit["approval_ref"] != plan.approval_ref
                or permit["execution_scope"] != plan.execution_scope
                or permit["plan_sha256"] != result["plan_sha256"]
                or permit["session_id"] != session
                or permit["run_id"] != plan.run_id
                or permit["epoch"] != 0
                or permit["cycle"] != cycle
                or permit["rules"]["allowed"] is not True
                or permit["rules"]["target_ned_m"] != permit["target_ned_m"]
                or permit["rules"]["start_ned_m"] != permit["start_ned_m"]
                or permit["rules"]["observation_sha256"] != permit["observation_sha256"]
                or permit["observation_sha256"] not in observed_by_hash
                or dispatch["permit_sha256"] != digest(permit)
                or dispatch["receipt"] != {"accepted": True, "permit_sha256": digest(permit)}
                or arrival["permit_sha256"] != digest(permit)
                or not plan.contains(permit["target_ned_m"])
                or not 0.5
                <= math.dist(permit["start_ned_m"], permit["target_ned_m"])
                <= plan.max_leg_m
                or not received[1]["at_s"]
                <= authorized["at_s"]
                <= dispatch["at_s"]
                < arrival["at_s"]
                or dispatch["at_s"] > permit["expires_at_s"]
                or arrival["at_s"] - dispatch["at_s"] > plan.segment_timeout_s
            ):
                raise ValueError("candidate_authority_execution_binding")
            permit_observation = observed_by_hash[permit["observation_sha256"]]
            position(permit_observation, dispatch["at_s"], held=True)
            if permit_observation["position_ned_m"] != permit["start_ned_m"]:
                raise ValueError("permit_start_observation_mismatch")
            tail = []
            for event in observations:
                row = event["observation"]
                if dispatch["at_s"] <= event["at_s"] <= arrival["at_s"]:
                    okay = (
                        row["ap_mode"] == "hold"
                        and math.hypot(*row["velocity_ned_mps"]) <= plan.hold_speed_mps
                    )
                    okay = (
                        okay
                        and math.dist(row["position_ned_m"], permit["target_ned_m"])
                        <= plan.target_error_m
                    )
                    tail = [*tail, row] if okay else []
            if (
                len(tail) < 2
                or tail[-1] != arrival["observation"]
                or tail[-1]["observed_at_s"] - tail[0]["observed_at_s"] < plan.settle_s
            ):
                raise ValueError("stable_segment_arrival_missing")
            previous_arrival = arrival["observation"]["observed_at_s"]
        if (
            len([e for e in events if e["event"] == "model_requested"]) != plan.updates * 2
            or result["status"] != "completed"
            or result["failure"] is not None
            or result["completed_updates"] != plan.updates
            or any(e["event"] == "failed" for e in events)
        ):
            raise ValueError("incomplete_control_run")
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": "ship_urban_loop_verification.v1",
        "control_sequence_verified": not reasons,
        "reasons": reasons,
        "native_model_use_verified": False,
        "whole_mission_completion_verified": False,
        "physical_execution_verified": False,
        "energy_savings_verified": False,
    }
