"""Fixed, model-free D1/coast/ship abort for the separate full-delivery trial."""

from __future__ import annotations

import math


def validate_sample(config, row, anchor, *, landing=False):
    if __package__:
        from .yokohama_delivery_contract import validate_config
        from .yokohama_endpoint_feedback import corridor_distance
    else:
        from yokohama_delivery_contract import validate_config
        from yokohama_endpoint_feedback import corridor_distance
    policy = validate_config(config)
    sea = config["world"]["sea_extension"]
    xyz = row["vehicle"]["xyz"]
    phase = row["phase"]
    if phase not in {"00-D1", "delivery_recovery_entry", "delivery_recovery_coast",
                     "delivery_recovery_ship", "delivery_recovery_land"} or landing != (phase == "delivery_recovery_land"):
        raise ValueError("Unapproved recovery phase")
    if phase in {"00-D1", "delivery_recovery_entry"}:
        lateral = corridor_distance(policy, xyz)
    else:
        a, b = ((policy["entry_world_xyz_m"], sea["coast_world_xyz_m"])
                if phase == "delivery_recovery_coast"
                else (sea["coast_world_xyz_m"], sea["ship_hold_world_xyz_m"]))
        delta = [b[i] - a[i] for i in range(2)]
        fraction = max(0, min(1, sum((xyz[i] - a[i]) * delta[i] for i in range(2))
                              / sum(d * d for d in delta)))
        lateral = math.hypot(*(xyz[i] - a[i] - fraction * delta[i] for i in range(2)))
    if (
        not all(type(v) in (int, float) and math.isfinite(v)
                for v in [*xyz, *row["velocity_ned"], row["sim_s"], row["wall_s"],
                          row["vehicle"]["age_s"], row["vehicle"]["sensor_sim_s"],
                          row["battery_fraction"]])
        or row["run_id"] != config["run_id"]
        or row["world_sha256"] != config["world"]["world_sha256"]
        or not 0 <= row["vehicle"]["age_s"] <= 2 or row["position_valid"] is not True
        or row["battery_fraction"] < 0.2 or row["reset_counters"] != anchor["reset_counters"]
        or lateral > 1.5
        or (not landing and (row["arming_state"] != 2 or row["landed"] is not False
                            or abs(xyz[2] - policy["entry_world_xyz_m"][2]) > 0.5))
        or (landing and ((row["arming_state"], row["landed"]) not in ((2, False), (2, True), (1, True))
                        or not -0.2 <= xyz[2] <= sea["ship_hold_world_xyz_m"][2] + 0.5
                        or math.dist(xyz[:2], sea["ship_hold_world_xyz_m"][:2]) > 1.5))
    ):
        raise ValueError("Ship recovery observation leaves approved envelope/state")


def recover(config, decisions, reason, *, sample, upload, activate, wait_for,
            land, contacts, event, clock, set_phase, set_speed):
    if __package__:
        from .yokohama_delivery_contract import CONTRACT, validate_config
    else:
        from yokohama_delivery_contract import CONTRACT, validate_config
    validate_config(config)
    begin = clock()
    deadline = begin + CONTRACT["recovery_timeout_s"]
    if config["timeout_s"] - 10 < deadline:
        raise ValueError("Insufficient fixed ship-return reserve")
    anchor = sample()
    validate_sample(config, anchor, anchor)
    event("candidate_rejected", reason=type(reason).__name__ + ": " + str(reason),
          cycle=decisions.cycle, observation=anchor, recovery_begin_wall_s=begin,
          recovery_deadline_wall_s=deadline)
    receipt = decisions.stop()
    if not decisions.closed or decisions.active or receipt.get("session_revoked") is not True:
        raise ValueError("Model authority revocation unconfirmed before ship return")
    event("model_authority_revoked", receipt=receipt)

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError("Fixed ship recovery deadline exceeded")
        return left

    def stable(predicate, landing=False):
        first = last = None
        count = 0

        def check(row):
            nonlocal first, last, count
            remaining()
            validate_sample(config, row, anchor, landing=landing)
            if not predicate(row):
                first = last = None
                count = 0
                return False
            if last is not None and (row["sim_s"] <= last["sim_s"]
                    or row["vehicle"]["sensor_sim_s"] <= last["vehicle"]["sensor_sim_s"]):
                return False
            if last is not None and (row["sim_s"] - last["sim_s"] > 2
                                    or row["wall_s"] - last["wall_s"] > 3):
                first = None
                count = 0
            first = row if first is None else first
            last = row
            count += 1
            return count >= 3 and row["sim_s"] - first["sim_s"] >= 2

        return check

    stages = {s["name"]: s for s in config["flight_stages"]}
    for phase, name in (("delivery_recovery_entry", "01-FEEDBACK-EXIT"),
                        ("delivery_recovery_coast", "SEA-OUTBOUND-COAST"),
                        ("delivery_recovery_ship", "SEA-RETURN")):
        set_phase(phase)
        stage = stages[name]
        target = stage["target_world_xyz_m"]
        event("delivery_recovery_leg_authorized", upload_name=name,
              target_world_xyz_m=target, deadline_wall_s=deadline)
        remaining()
        set_speed(stage["airspeed_mps"])
        upload(name)
        remaining()
        activate(expires_at_wall_s=deadline)
        returned = wait_for(stable(lambda r: (
            math.dist(r["vehicle"]["xyz"][:2], target[:2]) <= 0.25
            and abs(r["vehicle"]["xyz"][2] - target[2]) <= 0.15
            and math.hypot(*r["velocity_ned"]) <= 0.3
            and r["arming_state"] == 2 and r["landed"] is False
        )), remaining())
        event("delivery_recovery_leg_observed", upload_name=name, observation=returned)
    set_phase("delivery_recovery_land")
    remaining()
    land()
    home = config["world"]["sea_extension"]["ship_hold_world_xyz_m"]
    final = wait_for(stable(lambda r: (
        r["landed"] is True and r["arming_state"] == 1
        and math.dist(r["vehicle"]["xyz"][:2], home[:2]) < 1.5
        and -0.2 <= r["vehicle"]["xyz"][2] <= 0.5 and math.hypot(*r["velocity_ned"]) <= 0.3
    ), landing=True), min(90, remaining()))
    if not any(c["topic"] == "launch_pad" and "x500" in c["collision1"] + c["collision2"]
               and final["sim_s"] - 2 <= c["sensor_sim_s"] <= final["sim_s"] for c in contacts()):
        raise ValueError("No fresh independent ship-deck contact")
    remaining()
    event("recovery_landing_disarm_observed", observation=final)
    return dict(status="failed_recovered", mission_outcome="failed",
                recovery_outcome="observed_return_landed_disarmed",
                recovery_duration_wall_s=clock() - begin, recovery_deadline_wall_s=deadline,
                physical_execution_invoked=False, payload_delivery_verified=False,
                vla_invoked=config["decisions"]["backend"] == "native",
                wam_invoked=config["decisions"]["backend"] == "native")
