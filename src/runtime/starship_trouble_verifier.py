"""Check trouble records against frozen schedules and saved plant observations.

This does not run the fault engine or dynamics. A pass establishes saved-record
consistency, not continuous-time fault fidelity, recovery, or AI effectiveness.
The shared manifest validator regenerates the seeded schedule, not the flight.
"""
from hashlib import sha256
import json
import math

from .starship_trouble_engine import manifest_digest, validate_manifest


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _engine_map(vehicle, state):
    engines, states = vehicle["engines"], state["engine_states"]
    _need(type(engines) is list and type(states) is list and len(engines) == len(states), "engine_inventory_mismatch")
    names = [row["name"] for row in engines]
    _need(len(set(names)) == len(names), "duplicate_engine_name")
    _need(all(type(row["available"]) is bool for row in states), "invalid_engine_availability")
    return {name: row["available"] for name, row in zip(names, states)}


def _verify(run):
    audit = run["trouble"]
    _need(type(audit) is dict and set(audit) == {"schema", "manifest_sha256", "manifest", "phase_starts", "receipts", "unapplied_event_ids"}, "invalid_trouble_audit_shape")
    _need(audit["schema"] == "missionos.starship_trouble_audit.v1", "invalid_trouble_audit_schema")
    manifest = validate_manifest(audit["manifest"])
    _need(audit["manifest_sha256"] == manifest_digest(manifest), "trouble_manifest_hash_mismatch")
    events = {row["id"]: row for row in manifest["events"]}
    parts = {"ship": run}
    if run.get("booster_run") is not None:
        parts["booster"] = run["booster_run"]
    starts = {}
    _need(type(audit["phase_starts"]) is list and len(audit["phase_starts"]) <= 64, "invalid_phase_starts")
    for row in audit["phase_starts"]:
        _need(type(row) is dict and set(row) == {"body", "phase", "time_s"}, "invalid_phase_start")
        body, phase, time = row["body"], row["phase"], row["time_s"]
        _need(body in parts and type(phase) is str and phase and _number(time), "invalid_phase_start_value")
        _need((body, phase) not in starts, "duplicate_phase_start")
        part = parts[body]
        _need(part["initial_state"]["time_s"] <= time <= part["final_state"]["time_s"], "phase_start_outside_flight")
        starts[(body, phase)] = time
    receipts = audit["receipts"]
    _need(type(receipts) is list and len(receipts) <= len(events), "invalid_trouble_receipts")
    seen, times, chain, active = set(), {}, audit["manifest_sha256"], []
    for row in receipts:
        _need(type(row) is dict and set(row) == {"event_id", "family", "body", "phase", "scheduled_at_s", "applied_at_s", "effects", "previous_sha256", "receipt_sha256"}, "invalid_trouble_receipt_shape")
        event_id = row["event_id"]
        _need(event_id in events and event_id not in seen, "invalid_or_repeated_fault_application")
        event = events[event_id]
        _need(all(row[key] == event[key] for key in ("family", "body", "phase")), "receipt_event_mismatch")
        body, phase, time = row["body"], row["phase"], row["applied_at_s"]
        _need((body, phase) in starts and _number(time) and _number(row["scheduled_at_s"]), "receipt_phase_missing")
        due = starts[(body, phase)] + event["delay_s"]
        _need(abs(row["scheduled_at_s"]-due) <= 1e-9 and abs(time-due) <= 1e-8, "fault_not_applied_at_trigger_boundary")
        _need(time >= times.get(body, time) and time <= parts[body]["final_state"]["time_s"], "fault_application_clock_invalid")
        payload = {key: value for key, value in row.items() if key != "receipt_sha256"}
        _need(row["previous_sha256"] == chain and row["receipt_sha256"] == _hash(payload), "fault_receipt_hash_mismatch")
        _effects(row, event)
        if row["family"] == "flap_jam":
            panels = [panel for panel in parts[body]["final_vehicle"]["aero_panels"] if panel["max_deflection_rad"] > 0]
            ordinal = int(event["target"].rsplit("_", 1)[1])
            effect = row["effects"][0]
            _need(ordinal < len(panels) and effect["component"] == panels[ordinal]["name"]
                  and abs(effect["held_angle_rad"]) <= panels[ordinal]["max_deflection_rad"], "jam_receipt_target_mismatch")
        chain, times[body] = row["receipt_sha256"], time
        seen.add(event_id)
        active.append(row)
    _need(audit["unapplied_event_ids"] == [event_id for event_id in events if event_id not in seen], "applied_unapplied_partition_mismatch")
    for event_id in audit["unapplied_event_ids"]:
        event = events[event_id]
        start = starts.get((event["body"], event["phase"]))
        if start is not None:
            due = start+event["delay_s"]
            _need(not any(sample["phase"] == event["phase"] and sample["time_s"] > due
                          for sample in parts[event["body"]]["samples"]), "due_fault_missing_from_receipts")
    sample_checks = _observations(parts, active)
    _payload_separations(run, active)
    _separation(run, active)
    return dict(applied_event_count=len(receipts), unapplied_event_count=len(events)-len(receipts),
                sampled_effect_checks=sample_checks, manifest_sha256=audit["manifest_sha256"])


def _effects(row, event):
    family, target, effects = row["family"], event["target"], row["effects"]
    _need(type(effects) is list, "invalid_fault_effects")
    if family in ("ascent_engine_out", "ship_engine_out", "booster_restart_failure", "rcs_channel_loss"):
        names = [f"{target}_{side}" for side in (-1, 1)] if family == "rcs_channel_loss" else [target]
        _need(len(effects) == len(names), "fault_engine_effect_count")
        for effect, name in zip(effects, names):
            _need(type(effect) is dict and set(effect) == {"component", "available_before", "available_after", "throttle_at_injection"}, "invalid_engine_effect_shape")
            _need(effect["component"] == name and type(effect["available_before"]) is bool and effect["available_after"] is False,
                  "invalid_engine_effect_availability")
            _need(_number(effect["throttle_at_injection"]) and 0 <= effect["throttle_at_injection"] <= 1, "invalid_engine_effect_throttle")
    elif family == "flap_jam":
        _need(len(effects) == 1 and type(effects[0]) is dict and set(effects[0]) == {"component", "actuator_before", "actuator_after", "held_angle_rad"}, "invalid_jam_effect_shape")
        effect = effects[0]
        _need(type(effect["component"]) is str and effect["actuator_before"] == "free" and effect["actuator_after"] == "jammed"
              and _number(effect["held_angle_rad"]), "invalid_jam_effect")
    else:
        expected = dict(component=target, signal="payload_release_blocked" if family == "payload_jam" else "tower_ready",
                        before=family == "tower_unavailable", after=family == "payload_jam")
        _need(effects == [expected], "invalid_external_signal_effect")


def _observations(parts, active):
    checks = 0
    for body, part in parts.items():
        final_availability = _engine_map(part["final_vehicle"], part["final_state"])
        previous_time = -1.
        for sample in part["samples"]:
            time = sample["time_s"]
            _need(_number(time) and time >= previous_time, "sample_clock_reversed")
            previous_time = time
            physical_vehicle = (parts["booster"]["final_vehicle"] if body == "ship" and sample["phase"] == "stack_ascent" and "booster" in parts
                                else part["final_vehicle"])
            availability = _engine_map(physical_vehicle, sample)
            health = sample.get("controller", {}).get("component_health")
            if health is not None:
                _need(type(health) is dict and set(health) == {"observed_at_s", "channel", "engine_health", "actuator_health", "payload_mechanism", "tower_ready"}, "invalid_health_observation_fields")
                _need(health["observed_at_s"] == time and health["channel"] == "synthetic_component_health_v1", "health_time_or_channel_invalid")
                _need(health["engine_health"] == [dict(component=name, available=ok) for name, ok in availability.items()], "health_disagrees_with_engine_states")
                _need(all(type(row["available"]) is bool for row in health["engine_health"]), "invalid_engine_health_flag")
                realized = [row for row in active if row["applied_at_s"] <= time]
                blocked = any(row["family"] == "payload_jam" for row in realized)
                unavailable = any(row["family"] == "tower_unavailable" for row in realized)
                _need(health["payload_mechanism"] == ("blocked" if blocked else "clear"), "payload_health_disagrees_with_applied_fault")
                _need(health["tower_ready"] is (not unavailable), "tower_health_disagrees_with_applied_fault")
                panels = part["final_vehicle"]["aero_panels"]
                if body == "ship" and sample["phase"] == "stack_ascent" and "booster" in parts:
                    panels = parts["booster"]["final_vehicle"]["aero_panels"] + [
                        {**panel, "name": "upper_"+panel["name"]} for panel in panels]
                _need(sample["aero_panel_names"] == [panel["name"] for panel in panels], "sample_panel_inventory_mismatch")
                jammed = {effect["component"] for row in realized if row["body"] == body and row["family"] == "flap_jam"
                          for effect in row["effects"]}
                expected = [dict(component=panel["name"], jammed=panel["name"] in jammed)
                            for panel in panels if panel["max_deflection_rad"] > 0]
                _need(health["actuator_health"] == expected, "actuator_health_disagrees_with_applied_jams")
                _need(all(type(row["jammed"]) is bool for row in health["actuator_health"]), "invalid_actuator_health_flag")
            for row in active:
                if time <= row["applied_at_s"]:
                    continue  # A same-timestamp phase sample can precede injection.
                for effect in row["effects"]:
                    component = effect["component"]
                    applies = row["body"] == body or (row["family"] == "ascent_engine_out" and body == "booster")
                    if not applies:
                        continue
                    if "available_after" in effect and component in availability:
                        _need(availability[component] is False, "failed_engine_reenabled_in_sample")
                        checks += 1
                    if row["family"] == "flap_jam":
                        names = sample["aero_panel_names"]
                        _need(component in names, "jammed_panel_missing_from_sample")
                        index = names.index(component)
                        _need(abs(sample["flap_angles_rad"][index]-effect["held_angle_rad"]) <= 1e-10, "jammed_panel_moved")
                        command = sample.get("command")
                        if command is not None:
                            _need(abs(command["flap_angles_rad"][index]-effect["held_angle_rad"]) <= 1e-10, "jammed_panel_command_not_held")
                            checks += 1
        for row in active:
            applies = row["body"] == body or (row["family"] == "ascent_engine_out" and body == "booster")
            if applies:
                for effect in row["effects"]:
                    if "available_after" in effect and effect["component"] in final_availability:
                        _need(final_availability[effect["component"]] is False, "failed_engine_reenabled_in_final_state")
    return checks


def _payload_separations(run, active):
    jams = [row["applied_at_s"] for row in active if row["family"] == "payload_jam"]
    if not jams:
        return
    blocked_at = min(jams)
    # The production hook applies a due jam before testing/performing release
    # at that tick. A same-time physical release is therefore inconsistent too.
    for event in run.get("events", []):
        if event.get("event") == "payload_released":
            _need(_number(event.get("time_s")) and event["time_s"] < blocked_at, "payload_released_after_jam")
    for satellite in run.get("satellites", []):
        time = satellite["release_state"]["time_s"]
        _need(_number(time) and time < blocked_at, "satellite_separated_after_jam")
        receipt = satellite.get("receipt")
        if receipt is not None:
            _need(_number(receipt.get("time_s")) and receipt["time_s"] < blocked_at, "separation_receipt_after_jam")


def _separation(run, active):
    separation, booster = run.get("booster_separation_state"), run.get("booster_run")
    if separation is None:
        return
    _need(type(booster) is dict, "separated_booster_record_missing")
    names = booster["final_vehicle"]["engines"]
    availability = _engine_map(booster["final_vehicle"], separation)
    for source in (booster["booster_separation_state"], booster["initial_state"]):
        _need(len(source["engine_states"]) == len(names), "separation_engine_count_changed")
        _need([row["available"] for row in source["engine_states"]] == list(availability.values()), "separation_availability_not_preserved")
    for row in active:
        if row["family"] == "ascent_engine_out" and row["applied_at_s"] <= separation["time_s"]:
            for effect in row["effects"]:
                _need(availability.get(effect["component"]) is False, "ascent_fault_lost_at_separation")


def verify_run(run):
    try:
        details = _verify(run)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as error:
        return dict(passed=False, errors=[str(error)],
                    claim="record_consistency_only_not_recovery_or_ai_success")
    return dict(passed=True, errors=[], **details,
                claim="record_consistency_only_not_recovery_or_ai_success")
