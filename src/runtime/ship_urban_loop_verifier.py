"""Reopen control receipts, without trusting the producer's completed flag."""

from __future__ import annotations

import math
import re

from .ship_urban_loop import UrbanLoopPlan, digest, finite, vector


def verify_urban_loop(result):
    if result.get("plan", {}).get("fixture_goal_policy") is True:
        return _verify_goal_loop(result)
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


def _clear_observed_segment(start, target, context, margin):
    """Independent slab intersection, never a producer clearance flag.

    Obstacles are observed fixture boxes. Intersecting their margin-expanded
    closed volume, including a tangent, blocks a leg. This does not predict
    obstacle motion or establish continuous physical-world clearance.
    """
    coverage = context["coverage"]
    if any(point[axis] - margin < coverage["lower_ned_m"][axis]
           or point[axis] + margin > coverage["upper_ned_m"][axis]
           for point in (start, target) for axis in range(3)):
        return False
    for obstacle in context["obstacles"]:
        enter, leave = 0.0, 1.0
        for axis in range(3):
            center = obstacle["center_ned_m"][axis]
            half = obstacle["half_size_m"][axis] + margin
            lower, upper = center - half, center + half
            origin, delta = start[axis], target[axis] - start[axis]
            if delta == 0:
                if not lower <= origin <= upper:
                    break
            else:
                first, last = sorted(((lower - origin) / delta, (upper - origin) / delta))
                enter, leave = max(enter, first), min(leave, last)
                if enter > leave:
                    break
        else:
            return False
    return True


def _verify_goal_loop(result):
    """Reconstruct goal completion and observed geometry independently.

    Shared helpers validate wire schemas and canonical binding only; geometry
    uses the independent slab implementation above. Old public receipts remain
    on the unchanged legacy path.
    """
    from .ship_urban_goal_contract import (
        context_digest,
        validate_goal_forecast,
        validate_goal_observation,
    )

    reasons = []
    try:
        plan = UrbanLoopPlan(**result["plan"])
        events = result["events"]
        if not events or result["plan_sha256"] != digest(result["plan"]):
            raise ValueError("goal_plan_binding")
        session = events[0]["session_id"]
        if any(
            e["run_id"] != plan.run_id
            or e["session_id"] != session
            or e["plan_sha256"] != result["plan_sha256"]
            or not finite(e["at_s"])
            for e in events
        ) or any(b["at_s"] < a["at_s"] for a, b in zip(events, events[1:])):
            raise ValueError("goal_event_binding_or_time")
        if events[-1]["at_s"] - events[0]["at_s"] > plan.total_timeout_s:
            raise ValueError("goal_total_time_budget")

        def one(name, cycle=None):
            found = [e for e in events if e["event"] == name
                     and (cycle is None or e["cycle"] == cycle)]
            if len(found) != 1:
                raise ValueError("goal_missing_or_duplicate:" + name)
            return found[0]

        def position(row, at_s, *, held=False):
            context = validate_goal_observation(
                row, now_s=at_s, max_age_s=plan.observation_age_s,
                lower_ned_m=plan.lower_ned_m, upper_ned_m=plan.upper_ned_m,
            )
            if (
                row["run_id"] != plan.run_id or row["phase"] != "urban"
                or row["position_valid"] is not True
                or not plan.contains(row["position_ned_m"])
                or not vector(row["velocity_ned_mps"])
                or not finite(row["observed_at_s"])
                or not 0 <= at_s - row["observed_at_s"] <= plan.observation_age_s
                or type(row["sequence"]) is not int
                or not finite(row["battery_fraction"])
                or not plan.reserve_fraction <= row["battery_fraction"] <= 1
                or row["ap_mode"] not in {"hold", "mission"}
                or (held and (row["ap_mode"] != "hold"
                              or math.hypot(*row["velocity_ned_mps"]) > plan.hold_speed_mps))
            ):
                raise ValueError("goal_invalid_observation")
            return context

        started, ready = one("model_start_requested"), one("models_ready")
        revoked, stopped, handoff = (
            one("session_revoked"), one("models_stopped"), one("ap_return_handoff")
        )
        goal, exit_hold = one("goal_arrival_observed"), one("ap_exit_hold_observed")
        if not (
            started["at_s"] <= ready["at_s"] < goal["at_s"] <= exit_hold["at_s"] <= revoked["at_s"]
            <= stopped["at_s"] <= handoff["at_s"]
            and ready["identity"]["execution_scope"] == "fixture"
            and stopped["verified"] is True
            and handoff["receipt"]["ap_return_observed"] is True
        ):
            raise ValueError("goal_lifecycle_order_or_shutdown")
        revoked_index = events.index(revoked)
        if any(e["epoch"] != (0 if i < revoked_index else 1) for i, e in enumerate(events)):
            raise ValueError("goal_session_epoch")
        position(started["observation"], started["at_s"], held=True)
        position(exit_hold["observation"], exit_hold["at_s"], held=True)
        position(handoff["observation"], handoff["at_s"], held=True)
        if math.dist(started["observation"]["position_ned_m"], plan.entry_ned_m) > plan.target_error_m:
            raise ValueError("goal_entry_location")

        observations = [e for e in events if e["event"] == "observation"]
        if not observations:
            raise ValueError("goal_observations_missing")
        observed_by_hash = {digest(e["observation"]): e for e in observations}
        for e in observations:
            position(e["observation"], e["at_s"])
        for event in (started, exit_hold, handoff):
            observed = observed_by_hash.get(digest(event["observation"]))
            if observed is None or observed["at_s"] > event["at_s"]:
                raise ValueError("goal_unrecorded_lifecycle_observation")
        for a, b in zip(observations, observations[1:]):
            old, new = a["observation"], b["observation"]
            if (new["sequence"] <= old["sequence"]
                    or not 0 < new["observed_at_s"] - old["observed_at_s"] <= plan.max_sample_gap_s):
                raise ValueError("goal_observation_sequence_or_gap")

        authorized_events = [e for e in events if e["event"] == "segment_authorized"]
        completed = len(authorized_events)
        if (not 1 <= completed <= plan.updates
                or [e["cycle"] for e in authorized_events] != list(range(1, completed + 1))):
            raise ValueError("goal_action_budget_or_cycle")
        for name, count in (("model_requested", completed * 2), ("model_received", completed * 2),
                            ("segment_dispatched", completed), ("segment_arrived", completed)):
            matches = [e for e in events if e["event"] == name]
            if len(matches) != count or any(e["cycle"] not in range(1, completed + 1) for e in matches):
                raise ValueError("goal_finite_counts:" + name)
        approved_goal = dict(position_ned_m=list(plan.goal_ned_m), tolerance_m=plan.target_error_m,
                             max_leg_m=plan.max_leg_m, clearance_m=plan.clearance_m)
        request_fields = {"schema_version", "request_id", "run_id", "session_id", "plan_sha256",
                          "epoch", "cycle", "model", "observation", "proposal", "approved_goal"}
        request_ids, image_ids, permits = set(), set(), set()
        previous_arrival, previous_dispatch = -1.0, ready["at_s"]
        previous_position = list(plan.entry_ned_m)
        final_arrival = None
        for cycle in range(1, completed + 1):
            requests = [e for e in events if e["event"] == "model_requested" and e["cycle"] == cycle]
            responses = [e for e in events if e["event"] == "model_received" and e["cycle"] == cycle]
            if len(requests) != 2 or len(responses) != 2:
                raise ValueError("goal_two_models_per_cycle")
            if not (previous_dispatch <= requests[0]["at_s"] <= responses[0]["at_s"]
                    <= requests[1]["at_s"] <= responses[1]["at_s"]):
                raise ValueError("goal_model_order")
            for model, sent, got in zip(("vla", "wam"), requests, responses):
                request, response = sent["request"], got["response"]
                if (set(request) != request_fields
                        or request["schema_version"] != "ship_urban_loop_request.v1"
                        or request["approved_goal"] != approved_goal
                        or request["model"] != model or request["cycle"] != cycle
                        or request["run_id"] != plan.run_id or request["session_id"] != session
                        or request["plan_sha256"] != result["plan_sha256"] or request["epoch"] != 0
                        or not isinstance(request["request_id"], str) or not request["request_id"]
                        or request["request_id"] in request_ids
                        or sent["request_sha256"] != digest(request)
                        or response["request_sha256"] != digest(request)
                        or response["session_id"] != session or response["model"] != model
                        or response["dispatch_allowed"] is not False
                        or got["response_sha256"] != digest(response)
                        or not ready["at_s"] <= sent["at_s"] <= got["at_s"] < revoked["at_s"]
                        or got["at_s"] - sent["at_s"] > plan.inference_timeout_s):
                    raise ValueError("goal_model_request_response_binding")
                request_ids.add(request["request_id"])
                row = request["observation"]
                position(row, sent["at_s"], held=True)
                seen = observed_by_hash.get(digest(row))
                image = row["image_sha256"]
                if (seen is None or seen["at_s"] > sent["at_s"]
                        or not finite(row["image_observed_at_s"])
                        or not 0 <= sent["at_s"] - row["image_observed_at_s"] <= plan.observation_age_s
                        or row["image_observed_at_s"] <= previous_arrival
                        or row["observed_at_s"] <= previous_arrival
                        or not isinstance(image, str) or re.fullmatch(r"[a-f0-9]{64}", image) is None
                        or image in image_ids):
                    raise ValueError("goal_fresh_observation_after_arrival")
                image_ids.add(image)
                for event in observations:
                    if sent["at_s"] <= event["at_s"] <= got["at_s"]:
                        position(event["observation"], event["at_s"], held=True)
                        if context_digest(event["observation"]) != context_digest(row):
                            raise ValueError("goal_context_changed_during_inference")
                        if math.dist(event["observation"]["position_ned_m"], row["position_ned_m"]) > plan.hold_drift_m:
                            raise ValueError("goal_model_wait_hold_drift")
            vla, wam = [e["response"] for e in responses]
            vla_row = requests[0]["request"]["observation"]
            wam_row = requests[1]["request"]["observation"]
            if (wam_row["sequence"] <= vla_row["sequence"]
                    or wam_row["observed_at_s"] <= vla_row["observed_at_s"]
                    or wam_row["image_observed_at_s"] <= vla_row["image_observed_at_s"]):
                raise ValueError("goal_wam_observation_not_fresh")
            if (requests[0]["request"]["proposal"] is not None
                    or requests[1]["request"]["proposal"] != vla):
                raise ValueError("goal_wam_candidate_binding")
            validate_goal_forecast(vla, wam, wam_row)
            selected = [c for c in vla["candidates"] if c["id"] == wam["selected_candidate_id"]]
            if len(selected) != 1:
                raise ValueError("goal_selected_candidate")
            authorized, dispatch, arrival = (one(name, cycle) for name in (
                "segment_authorized", "segment_dispatched", "segment_arrived"))
            permit = authorized["permit"]
            permit_event = observed_by_hash.get(permit["observation_sha256"])
            if permit_event is None:
                raise ValueError("goal_unrecorded_permit_observation")
            permit_row = permit_event["observation"]
            context = position(permit_row, dispatch["at_s"], held=True)
            start, target = permit["start_ned_m"], permit["target_ned_m"]
            if (permit["run_id"] != plan.run_id or permit["session_id"] != session
                    or permit["plan_sha256"] != result["plan_sha256"] or permit["epoch"] != 0
                    or permit["cycle"] != cycle or permit["approval_ref"] != plan.approval_ref
                    or permit["execution_scope"] != "fixture"
                    or permit["permit_id"] in permits
                    or start != permit_row["position_ned_m"]
                    or math.dist(start, previous_position) > plan.hold_drift_m
                    or target != selected[0]["target_ned_m"] or not plan.contains(target)
                    or not 0.5 <= math.dist(start, target) <= plan.max_leg_m
                    or permit["vla_response_sha256"] != digest(vla)
                    or permit["wam_response_sha256"] != digest(wam)
                    or wam["observation_context_sha256"] != context_digest(context)
                    or permit["rules"]["allowed"] is not True
                    or permit["rules"]["start_ned_m"] != start
                    or permit["rules"]["target_ned_m"] != target
                    or permit["rules"]["observation_sha256"] != digest(permit_row)
                    or permit["rules"]["observation_context_sha256"] != context_digest(context)
                    or permit["rules"]["constraint_source"] != "observed_fixture_boxes"
                    or permit["rules"]["blocking_obstacle_ids"] != []
                    or permit_event["at_s"] > authorized["at_s"]
                    or not responses[1]["at_s"] <= authorized["at_s"] <= dispatch["at_s"] < arrival["at_s"]
                    or not finite(permit["issued_at_s"]) or not finite(permit["expires_at_s"])
                    or not permit["issued_at_s"] <= dispatch["at_s"] <= permit["expires_at_s"]
                    or permit["expires_at_s"] - permit["issued_at_s"] > plan.observation_age_s + 0.001
                    or arrival["at_s"] - dispatch["at_s"] > plan.segment_timeout_s
                    or dispatch["permit_sha256"] != digest(permit)
                    or dispatch["receipt"] != {"accepted": True, "permit_sha256": digest(permit)}
                    or arrival["permit_sha256"] != digest(permit)):
                raise ValueError("goal_candidate_authority_execution_binding")
            permits.add(permit["permit_id"])
            if not _clear_observed_segment(start, target, context, plan.clearance_m):
                raise ValueError("goal_selected_leg_intersects_observed_obstacle")
            preceding = [e for e in observations if e["at_s"] <= dispatch["at_s"]]
            if (not preceding or context_digest(preceding[-1]["observation"]) != context_digest(context)):
                raise ValueError("goal_context_changed_before_dispatch")
            position(preceding[-1]["observation"], dispatch["at_s"], held=True)
            path_rows = [e["observation"] for e in observations
                         if dispatch["at_s"] <= e["at_s"] <= arrival["at_s"]]
            if not path_rows:
                raise ValueError("goal_segment_observations_missing")
            last = permit_row
            tail = []
            delta = [b - a for a, b in zip(start, target)]
            length2 = sum(x * x for x in delta)
            for row in path_rows:
                if context_digest(row) != context_digest(context):
                    raise ValueError("goal_context_changed_after_dispatch")
                fraction = max(0.0, min(1.0, sum((x - a) * d for x, a, d in
                                                zip(row["position_ned_m"], start, delta)) / length2))
                nearest = [a + fraction * d for a, d in zip(start, delta)]
                if math.dist(nearest, row["position_ned_m"]) > plan.hold_drift_m:
                    raise ValueError("goal_sampled_segment_tracking")
                for geometry in (last["goal_context"], row["goal_context"]):
                    if not _clear_observed_segment(last["position_ned_m"], row["position_ned_m"],
                                                   geometry, plan.clearance_m):
                        raise ValueError("goal_observed_sweep_intersects_obstacle")
                okay = (row["ap_mode"] == "hold"
                        and math.hypot(*row["velocity_ned_mps"]) <= plan.hold_speed_mps
                        and math.dist(row["position_ned_m"], target) <= plan.target_error_m)
                tail = [*tail, row] if okay else []
                last = row
            if (len(tail) < 2 or tail[-1] != arrival["observation"]
                    or tail[-1]["observed_at_s"] - tail[0]["observed_at_s"] < plan.settle_s):
                raise ValueError("goal_stable_segment_arrival_missing")
            if (cycle < completed
                    and math.dist(arrival["observation"]["position_ned_m"], plan.goal_ned_m)
                    <= plan.target_error_m):
                raise ValueError("goal_action_after_goal_already_reached")
            final_arrival = arrival
            previous_arrival, previous_dispatch = arrival["observation"]["observed_at_s"], arrival["at_s"]
            previous_position = target

        if (final_arrival is None or goal["observation"] != final_arrival["observation"]
                or goal["at_s"] < final_arrival["at_s"]
                or goal["cycle"] != completed
                or math.dist(goal["observation"]["position_ned_m"], plan.goal_ned_m) > plan.target_error_m
                or math.dist(exit_hold["observation"]["position_ned_m"], plan.goal_ned_m) > plan.target_error_m
                or math.dist(handoff["observation"]["position_ned_m"], plan.goal_ned_m) > plan.target_error_m
                or result["status"] != "completed" or result["failure"] is not None
                or result["completed_updates"] != completed or result["goal_reached"] is not True
                or any(e["event"] == "failed" for e in events)):
            raise ValueError("goal_completion_not_observed")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        reasons.append(str(exc))
    return {
        "schema_version": "ship_urban_loop_verification.v1",
        "control_sequence_verified": not reasons,
        "goal_completion_verified": not reasons,
        "observed_geometry_verified": not reasons,
        "reasons": reasons,
        "native_model_use_verified": False,
        "whole_mission_completion_verified": False,
        "physical_execution_verified": False,
        "energy_savings_verified": False,
    }
