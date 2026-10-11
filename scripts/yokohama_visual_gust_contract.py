"""Separate CPU-only eight-metre, four-observation inland experiment.

The old four-metre/native/delivery catalogs are unchanged. No model credential
or remote endpoint is accepted by this experiment.
"""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

CONTRACT = {
    "schema": "yokohama.visual-gust-cpu.v1",
    "goal_distance_m": 8,
    "maximum_cycles": 4,
    "timeout_s": 900,
    "model_timeout_s": 600,
    "maximum_flight_attempts": 1,
    "native_models_allowed": False,
    "gust_velocity_enu_mps": [0.5, 0, 0],
    "gust_duration_sim_s": 2,
    "gust_after_cycle": 1,
    "maximum_gusts": 1,
}
SCENARIOS = ("reach", "reject", "timeout")


def build_config(base, scenario):
    from scripts.yokohama_sitl import build_endpoint_feedback_config
    from scripts.yokohama_candidate_recovery import LIMITS
    from scripts.yokohama_goal_distance_adapter import POLICY

    if scenario not in SCENARIOS:
        raise ValueError("Unknown CPU gust scenario")
    config = build_endpoint_feedback_config(base)
    config["visual_gust_trial"] = dict(contract=deepcopy(CONTRACT), scenario=scenario)
    config["candidate_recovery"] = deepcopy(LIMITS)
    config["decisions"]["goal_distance_adapter"] = deepcopy(POLICY)
    p = config["decisions"]["endpoint_feedback"]
    p.update(
        schema_version="yokohama.eight-metre-feedback.v1",
        max_cycles=4,
        max_vla_requests=4,
        max_wam_requests=4,
    )
    a, b = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
    p["goal_world_xyz_m"] = [x + 2 * (y - x) for x, y in zip(a, b)]
    return config


def validate_config(config):
    if __package__:
        from .yokohama_endpoint_feedback import FIXED, feedback_policy
        from .yokohama_candidate_recovery import LIMITS
        from .yokohama_goal_distance_adapter import POLICY
    else:
        from yokohama_endpoint_feedback import FIXED, feedback_policy
        from yokohama_candidate_recovery import LIMITS
        from yokohama_goal_distance_adapter import POLICY

    marker = config.get("visual_gust_trial", {})
    p = config.get("decisions", {}).get("endpoint_feedback", {})
    expected = {
        **FIXED,
        "schema_version": "yokohama.eight-metre-feedback.v1",
        "max_cycles": 4,
        "max_vla_requests": 4,
        "max_wam_requests": 4,
    }
    if (
        set(marker) != {"contract", "scenario"}
        or marker["contract"] != CONTRACT
        or marker["scenario"] not in SCENARIOS
        or any(p.get(k) != v for k, v in expected.items())
        or config.get("timeout_s") != 900
        or config["decisions"].get("backend") != "fixture"
        or config.get("delivery_trial")
        or config.get("endpoint_adapter_trial")
        or config.get("fixture_reject_second_candidate", False)
        or config.get("candidate_recovery") != LIMITS
        or config["decisions"].get("goal_distance_adapter") != POLICY
        or config["decisions"].get("fixture_delay_s", {}) != {}
        or not config.get("operator_approval_manifest_sha256")
        or not math.isclose(
            math.dist(p["entry_world_xyz_m"], p["goal_world_xyz_m"]), 8, abs_tol=1e-8
        )
    ):
        raise ValueError("Unapproved eight-metre CPU experiment")
    wind = config["world"].get("wind")
    if wind and (
        wind.get("velocity_enu_mps") != [0.5, 0, 0]
        or wind.get("after_takeoff") is not True
        or wind.get("profile")
        or wind.get("gusts")
        or config.get("wind_mps") != 0.5
    ):
        raise ValueError("Unsupported one-pulse wind physics")
    proxy = deepcopy(config)
    proxy.pop("visual_gust_trial")
    proxy.pop("candidate_recovery")
    proxy["world"].pop("wind", None)
    proxy["wind_mps"] = 0
    q = proxy["decisions"]["endpoint_feedback"]
    q.update(FIXED)
    a, b = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
    q["goal_world_xyz_m"] = [x + (y - x) / 2 for x, y in zip(a, b)]
    feedback_policy(proxy)  # Preserve every old geometry/state/lifecycle bound.
    points = config["world"]["points"]
    target = points[1]["world_xyz_m"]
    length = math.dist(a, target)
    goal = [x + 8 * (y - x) / length for x, y in zip(a, target)]
    if math.dist(goal, p["goal_world_xyz_m"]) > 1e-8:
        raise ValueError("Eight-metre goal differs from the frozen D1/D2 direction")
    return p


def arguments(image, scenario):
    from scripts.yokohama_local_preflight import image_id

    if scenario not in SCENARIOS:
        raise ValueError("Unsupported CPU-only scenario")
    return [
        "--phase",
        "flight",
        "--visual-gust-trial",
        scenario,
        "--decision-backend",
        "fixture",
        "--wam-profile",
        "motion-v4",
        "--timeout-seconds",
        "900",
        "--local-image-id",
        image_id(image),
    ]


def admit(raw, repo, supplied):
    from src.runtime.yokohama_execution_service import recovery_input_hashes, proposal_digest

    manifest = json.loads(raw)
    p, a = manifest["proposal"], manifest["approval"]
    if (
        p.get("schema") != CONTRACT["schema"]
        or p.get("contract") != CONTRACT
        or p.get("simulator_arguments") != arguments(p.get("image_id"), p.get("scenario"))
        or supplied != p["simulator_arguments"]
        or p.get("input_sha256") != recovery_input_hashes(Path(repo))
        or p.get("city_models") != "fixture"
        or p.get("native_service_config") is not None
        or a.get("approved_proposal_sha256") != proposal_digest(p)
        or a.get("maximum_actual_flight_trials") != 1
        or not all(a.get(k) for k in ("operator_approval_ref", "actor_session_id", "approved_at"))
    ):
        raise ValueError("CPU experiment source/approval/catalog mismatch")
    return manifest


def validate_previous(config, message):
    """Bind every cycle to the immediately preceding independently settled leg."""
    if __package__:
        from .yokohama_endpoint_feedback import validate_feedback_observation
    else:
        from yokohama_endpoint_feedback import validate_feedback_observation
    validate_config(config)
    cycle = message["cycle"]
    if type(cycle) is not int or not 1 <= cycle <= 4:
        raise ValueError("Unapproved extended cycle")
    previous = message.get("previous_segment")
    row = message["observation"]
    if cycle == 1:
        if (
            previous is not None
            or math.dist(
                row["vehicle"]["xyz"], config["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]
            )
            > 0.25
        ):
            raise ValueError("Extended first cycle is not at its approved entry")
        return
    if not isinstance(previous, dict) or set(previous) != {
        "permit",
        "arrival",
        "stable_since_sim_s",
        "stable_samples",
    }:
        raise ValueError("Extended cycle lacks prior stable evidence")
    permit, arrival = previous["permit"], previous["arrival"]
    validate_feedback_observation(config, arrival, held=True)
    since, count = previous["stable_since_sim_s"], previous["stable_samples"]
    if (
        permit.get("cycle") != cycle - 1
        or permit.get("run_id") != config["run_id"]
        or permit.get("config_sha256")
        != hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        or type(count) is not int
        or count < 3
        or not isinstance(since, (int, float))
        or not math.isfinite(since)
        or arrival["sim_s"] - since < 2
        or math.dist(arrival["vehicle"]["xyz"], permit["candidate"]["target_world_xyz_m"]) > 0.25
        or row["sim_s"] < arrival["sim_s"]
        or row["wall_s"] < arrival["wall_s"]
        or math.dist(row["vehicle"]["xyz"], arrival["vehicle"]["xyz"]) > 0.5
        or row["reset_counters"] != arrival["reset_counters"]
        or abs(math.remainder(row["heading_ned_rad"] - arrival["heading_ned_rad"], 2 * math.pi))
        > 0.03
    ):
        raise ValueError("Extended cycle has stale/cross-cycle prior evidence")


def pulse(config, obs, sample, wait_for, event, clock, check_deadline):
    """One weak physics pulse between legs; PX4 alone stabilizes the aircraft."""
    validate_config(config)
    if __package__:
        from .yokohama_endpoint_feedback import validate_feedback_observation
    else:
        from yokohama_endpoint_feedback import validate_feedback_observation
    before = sample()
    rows, receipts = [before], []
    for velocity in ([0.5, 0, 0], [0, 0, 0]):
        check_deadline()
        receipt = obs.activate_wind(velocity)
        if not receipt["confirmed"]:
            raise ValueError("Weak gust transport unconfirmed")
        receipts.append(receipt)
        event("weak_gust_transport_confirmed", receipt=receipt)
        if velocity[0]:
            end = receipt["end_sim_s"] + 2

            def elapsed(row):
                check_deadline()
                validate_feedback_observation(config, row)
                rows.append(row)
                return row["sim_s"] >= end

            wait_for(elapsed, 15)
    stable_since = last = None
    stable_samples = 0

    def settled(row):
        nonlocal stable_since, last, stable_samples
        check_deadline()
        validate_feedback_observation(config, row)
        rows.append(row)
        if math.dist(row["vehicle"]["xyz"], before["vehicle"]["xyz"]) > 0.5:
            raise ValueError("Gust lost the unchanged hold envelope")
        if math.hypot(*row["velocity_ned"]) > 0.3 or row["nav_state"] != 4:
            stable_since = None
            last = None
            stable_samples = 0
            return False
        if last is not None:
            if (
                row["sim_s"] <= last["sim_s"]
                or row["vehicle"]["sensor_sim_s"] <= last["vehicle"]["sensor_sim_s"]
            ):
                return False
            if row["sim_s"] - last["sim_s"] > 2 or row["wall_s"] - last["wall_s"] > 3:
                stable_since = None
                stable_samples = 0
        if stable_since is None:
            stable_since = row["sim_s"]
        last = row
        stable_samples += 1
        return stable_samples >= 3 and row["sim_s"] - stable_since >= 2

    after = wait_for(settled, 20)
    record = dict(
        schema="yokohama.weak-gust-evidence.v1",
        receipts=receipts,
        before=before,
        after=after,
        rows=rows,
        wall_s=clock(),
        stable_samples=stable_samples,
        stable_since_sim_s=stable_since,
        control="PX4 hold; no VLA/WAM gust control",
        pulse_count=1,
    )
    (Path("/mission") / "weak-gust.json").write_text(
        json.dumps(record, indent=2, allow_nan=False) + "\n"
    )
    event("weak_gust_settled_before_reobservation", observation=after)
    return after


def distinct_landing_predicate(config, anchor, *, landing=True):
    """The new reach scenario requires the same distinct terminal evidence as recovery."""
    if __package__:
        from .yokohama_candidate_recovery import validate_sample
    else:
        from yokohama_candidate_recovery import validate_sample
    first = last = None
    count = 0
    home = config["decisions"]["endpoint_feedback"]["entry_world_xyz_m"]

    def check(row):
        nonlocal first, last, count
        validate_sample(config, row, anchor, landing=landing)
        at_target = (
            (
                row["landed"] is True
                and row["arming_state"] == 1
                and math.dist(row["vehicle"]["xyz"][:2], home[:2]) < 1.5
                and -0.2 <= row["vehicle"]["xyz"][2] <= 0.5
            )
            if landing
            else (
                math.dist(row["vehicle"]["xyz"][:2], home[:2]) <= 0.25
                and abs(row["vehicle"]["xyz"][2] - home[2]) <= 0.15
            )
        )
        if not (at_target and math.hypot(*row["velocity_ned"]) <= 0.3):
            first = last = None
            count = 0
            return False
        if last is not None:
            if (
                row["sim_s"] <= last["sim_s"]
                or row["vehicle"]["sensor_sim_s"] <= last["vehicle"]["sensor_sim_s"]
            ):
                return False
            if row["sim_s"] - last["sim_s"] > 2 or row["wall_s"] - last["wall_s"] > 3:
                first = None
                count = 0
        first = row if first is None else first
        last = row
        count += 1
        return count >= 3 and row["sim_s"] - first["sim_s"] >= 2

    return check
