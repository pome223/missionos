"""Independent record checks for observed inventory -> one forecast, not flight.

No tracker, handoff builder, predictor or plant executes here. Exact stored JSON
bytes bind the compact forecast; scalar checks bind inventory and state. This
does not independently validate dynamics, sensors, landing or return authority.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math

STATE = {"time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s",
         "propellant_kg", "engine_states", "flap_angles_rad"}
COAST = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg")
ACTIONS = {"hold", "continue", "stop_deployment", "collect_status"}
SEQUENCERS = {"running", "held", "skipped", "inhibited"}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _same(a, b):
    return _digest(a) == _digest(b)  # JSON booleans must not equal numeric counts.


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _strict(value):
    if type(value) is dict:
        return all(type(k) is str and _strict(v) for k, v in value.items())
    if type(value) is list:
        return all(_strict(v) for v in value)
    return value is None or type(value) in (str, bool, int) or type(value) is float and math.isfinite(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_forecast_json_key")
        result[key] = value
    return result


def _state(state):
    _require(type(state) is dict and set(state) == STATE, "complete_state_required")
    _require(_number(state["time_s"]) and state["time_s"] >= 0
             and _number(state["propellant_kg"]) and state["propellant_kg"] >= 0, "invalid_state_scalar")
    for key, size in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(type(state[key]) is list and len(state[key]) == size and all(_number(x) for x in state[key]), "invalid_state_vector")
    _require(abs(sum(x*x for x in state["q_body_to_eci"])-1.) <= 1e-8, "nonunit_attitude")
    _require(type(state["engine_states"]) is list and len(state["engine_states"]) > 0
             and type(state["flap_angles_rad"]) is list and all(_number(x) for x in state["flap_angles_rad"]), "incomplete_actuators")
    for engine in state["engine_states"]:
        _require(set(engine) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
                 and type(engine["available"]) is bool and all(_number(engine[k]) for k in ("throttle", "gimbal_x_rad", "gimbal_y_rad"))
                 and 0 <= engine["throttle"] <= 1, "invalid_engine_state")


def _history(trace, total):
    """Derive only observable transitions; ACK and tool booleans are ignored."""
    history, releases, latest, pending, generation, clock = [], [], None, None, 0, -1.
    for entry in trace:
        _require(type(entry) is dict, "invalid_trace_entry")
        kind = entry["kind"]
        if kind == "observation":
            _require(set(entry) == {"kind", "row"}, "invalid_observation_entry")
            row = entry["row"]
            _require(row["phase"] == "orbital_coast", "noncoast_observation")
            obs = {k: row[k] for k in ("time_s", "released_count", "sequencer_state")}
            count, sequence, now = obs["released_count"], obs["sequencer_state"], obs["time_s"]
            _require(type(count) is int and 0 <= count <= total and sequence in SEQUENCERS
                     and _number(now) and now >= 0, "invalid_observation_channel")
            if obs == latest:
                continue  # Repeated confirm on the same published sample.
            _require(now >= clock and (latest is None or now > latest["time_s"]), "nonmonotonic_observation")
            prior = 0 if latest is None else latest["released_count"]
            _require((latest is not None or count == 0) and prior <= count <= prior+1, "observed_counter_gap_or_decrease")
            if latest is not None and (count != prior or sequence != latest["sequencer_state"]):
                generation += 1
            if count != prior:
                releases.append({"released_count": count, "observed_time_s": now, "previous_observation_time_s": latest["time_s"]})
            if pending is not None and now > pending["time_s"]:
                expected = {"hold": {"held"}, "continue": {"running"}, "stop_deployment": {"skipped"}, "collect_status": {"held", "skipped", "inhibited"}}
                if sequence in expected[pending["action"]]:
                    pending = None
            history.append({"kind": kind, **obs})
            latest, clock = obs, now
        elif kind == "command":
            _require(set(entry) == {"kind", "time_s", "action"} and entry["action"] in ACTIONS
                     and latest is not None and pending is None and _number(entry["time_s"])
                     and entry["time_s"] >= clock, "invalid_or_unconfirmed_command")
            pending, clock = entry, entry["time_s"]
            generation += 1
            history.append(deepcopy(entry))
        else:
            raise ValueError("unsupported_trace_kind")
    _require(latest is not None and pending is None and history[-1]["kind"] == "observation", "later_command_observation_missing")
    _require(latest["sequencer_state"] != "held" and (latest["released_count"] == total
             or latest["sequencer_state"] in {"skipped", "inhibited"}), "deployment_not_closed")
    return history, releases, latest, generation


def _observed_origin(state, retained):
    """Independent scalar form of the declared synthetic telemetry contract."""
    observed = deepcopy(state)
    for key, quantum in (("r_eci_m", 1.), ("v_eci_mps", .001), ("omega_body_rad_s", 1e-6)):
        observed[key] = [round(x/quantum)*quantum for x in state[key]]
    q = [round(x, 6) for x in state["q_body_to_eci"]]
    norm = math.sqrt(sum(x*x for x in q))
    observed["q_body_to_eci"] = [x/norm for x in q]
    observed["propellant_kg"] = round(state["propellant_kg"]/10)*10.
    return {"schema": "missionos.ship_return_prediction_origin.v1", "basis": "synthetic_navigation_estimate_v1",
            "retained_count": retained, "state": observed}


def _sequence_evidence(run, trace, *, ascent_withhold_verified=False):
    """Core events back sequence state; Director copies back decision inputs."""
    ascent_events = [e for e in run["events"] if e["event"] == "supervised_deployment_withheld"]
    _require(not ascent_events or ascent_withhold_verified is True, "ascent_withhold_requires_verified_prefix")
    _require(len(ascent_events) <= 1, "duplicate_ascent_withhold")
    rows = [entry["row"] for entry in trace if entry["kind"] == "observation"]
    observed = {_digest(row) for row in rows}
    events = [event for event in run["events"] if event["event"] in {
        "managed_hold_expired", "managed_deployment_resumed", "deployment_interlock_inhibited",
        "supervised_deployment_withheld"}
        or event["event"] == "managed_mission_command" and event["point"].startswith("deployment")]
    _require(all(_number(e["time_s"]) and e["time_s"] >= 0 for e in events)
             and all(a["time_s"] <= b["time_s"] for a, b in zip(events, events[1:])), "sequencer_event_chronology")
    # Ascent latch and hold expiry run before confirm; commands/interlocks run after
    # confirm at the same plant tick. Keep original event order within a tick.
    timeline = [(e["time_s"], 0 if e["event"] in {"managed_hold_expired", "supervised_deployment_withheld"} else 2, i, "event", e)
                for i, e in enumerate(events)]
    timeline += [(row["time_s"], 1, i, "observation", row) for i, row in enumerate(rows)]
    sequence, last_resume, ascent_latched = "running", None, False
    expected_resumes, observed_resumes = [], []
    for now, _, _, kind, entry in sorted(timeline):
        if kind == "observation":
            if ascent_latched:
                sequence = "skipped"
            _require(entry["sequencer_state"] == sequence, "sequencer_state_not_backed_by_core_events")
        elif entry["event"] == "supervised_deployment_withheld":
            ascent_latched, sequence = True, "skipped"
        elif entry["event"] == "managed_hold_expired":
            _require(not ascent_latched and sequence == "held", "hold_expiry_without_held_sequence")
            sequence = "skipped"
        elif entry["event"] == "deployment_interlock_inhibited":
            sequence = "inhibited"
        elif entry["event"] == "managed_deployment_resumed":
            _require(not ascent_latched and sequence == "running" and last_resume == now, "resume_event_without_continue")
            observed_resumes.append(now)
        else:
            action = entry["action"]
            _require(action in ACTIONS, "unsupported_core_deployment_command")
            if action == "hold":
                sequence = "held"
            elif action == "stop_deployment":
                sequence = "skipped"
            elif action == "collect_status" and sequence not in {"skipped", "inhibited"}:
                sequence = "held"
            elif action == "continue" and sequence == "held":
                sequence, last_resume = "running", now
                expected_resumes.append(now)
    _require(("skipped" if ascent_latched else sequence) == rows[-1]["sequencer_state"], "unobserved_final_sequence_transition")
    _require(_same(expected_resumes, observed_resumes), "resume_event_missing_or_duplicated")
    dispatched = []
    for record in run["mission_director"]["records"]:
        if record["clock_id"] != "ship":
            continue
        request, dispatch = record["request"], record["dispatch"]
        _require(_digest(request["observation"]) in observed, "director_request_observation_mismatch")
        _require(dispatch is not None and record["later_observation"] is not None, "director_later_observation_missing")
        _require(_digest(dispatch["observation"]) in observed
                 and _digest(record["later_observation"]) in observed, "director_execution_observation_mismatch")
        _require(dispatch["time_s"] == dispatch["observation"]["time_s"]
                 and request["observation"]["time_s"] <= dispatch["time_s"] < record["later_observation"]["time_s"], "director_observation_timeframe")
        if request["point"].startswith("deployment"):
            dispatched.append({"point": request["point"], "action": dispatch["action"], "time_s": dispatch["time_s"]})
    _require(_same(dispatched, [{k: e[k] for k in ("point", "action", "time_s")}
                              for e in events if e["event"] == "managed_mission_command"]), "director_dispatch_core_command_mismatch")


def _vehicle_inventory(profile, actual, retained):
    # Scalar dry mass/centroid checks, not a second dynamics implementation.
    ship, payload = profile["ship"], profile["payload"]
    dry = ship["dry_mass_kg"] + retained*payload["mass_each_kg"]
    com_z = (ship["dry_mass_kg"]*ship["dry_com_z_m"]
             + retained*payload["mass_each_kg"]*payload["center_z_m"])/dry
    _require(math.isclose(actual["dry_mass_kg"], dry, rel_tol=1e-12, abs_tol=1e-6)
             and len(actual["dry_com_body_m"]) == 3
             and all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-8) for a, b in zip(actual["dry_com_body_m"], [0., 0., com_z])), "vehicle_inventory_mass_or_centroid")
    return dry


def _check(study, forecast_bytes):
    _require(type(study) is dict and _strict(study), "strict_study_json_required")
    _require(set(study) == {"schema", "authority", "profile", "deployment_run", "trace", "handoff", "prediction", "source_sha256", "model_calls", "ship_return_dispatched", "physical_execution", "mission_completed"}
             and study["schema"] == "missionos.starship_m2_handoff_study.v1", "invalid_study_schema")
    _require(type(study["model_calls"]) is int and study["model_calls"] == 0 and all(study[k] is False for k in ("ship_return_dispatched", "physical_execution", "mission_completed")), "unsupported_execution_claim")
    authority, profile, run, handoff, prediction = (study[k] for k in ("authority", "profile", "deployment_run", "handoff", "prediction"))
    expected_authority = {"schema": "missionos.m2_handoff_fixture_authority.v1", "case": authority["case"],
        "deployment_authority": authority["deployment_authority"], "checkpoint_time_s": 1000.7000000000407,
        "maximum_forecasts": 1, "forecast_wall_timeout_s": 180., "prediction_integration_scale": 1.,
        "prediction_variant": "quantized_navigation_center_only", "maximum_model_calls": 0,
        "ship_return_dispatch_authorized": False, "physical_execution_authorized": False}
    _require(_same(authority, expected_authority) and authority["case"] in {"normal", "operations_notice", "release_fault", "hold_resume"}, "authority_scope")
    envelope = authority["deployment_authority"]
    _require(_same(envelope, run["mission_director"]["envelope"]) and envelope["mode"] == "fixture"
             and type(envelope["maximum_jev_calls"]) is int and type(envelope["maximum_llm_calls"]) is int
             and envelope["maximum_jev_calls"] == envelope["maximum_llm_calls"] == 0
             and envelope["physical_execution_authorized"] is False, "deployment_authority_binding")
    _require(run["mission_management_case"] == ("normal" if authority["case"] == "hold_resume" else authority["case"])
             and run["mission_director"]["response_fault"] == ("hold_deployment_start" if authority["case"] == "hold_resume" else None), "fixture_case_or_response_fault_mismatch")
    _require(type(study["source_sha256"]) is dict and bool(study["source_sha256"])
             and all(type(k) is str and not k.startswith(("/", "../")) and _hash(v) for k, v in study["source_sha256"].items()), "source_manifest_shape")
    _require(set(handoff) == {"schema", "inventory_basis", "development_only", "dispatch_authorized", "authority_sha256", "profile_sha256", "generation", "total_count", "released_count", "retained_count", "execution_state", "origin", "history", "release_observations", "handoff_sha256"}
             and handoff["schema"] == "missionos.deployment_return_handoff.v1"
             and handoff["inventory_basis"] == "synthetic_confirmed_separation_counter_v1"
             and handoff["development_only"] is True and handoff["dispatch_authorized"] is False, "handoff_schema")
    _require(handoff["handoff_sha256"] == _digest({k: v for k, v in handoff.items() if k != "handoff_sha256"})
             and handoff["authority_sha256"] == _digest(authority) and handoff["profile_sha256"] == _digest(profile), "handoff_identity")
    total = profile["payload"]["count"]
    _require(type(total) is int and 1 <= total <= 26 and type(handoff["total_count"]) is int and handoff["total_count"] == total, "total_inventory")
    history, releases, latest, generation = _history(study["trace"], total)
    _sequence_evidence(run, study["trace"])
    _require(_same(handoff["history"], history) and _same(handoff["release_observations"], releases)
             and type(handoff["generation"]) is int and handoff["generation"] == generation, "history_or_generation_mismatch")
    released = latest["released_count"]
    _require(type(handoff["released_count"]) is int and type(handoff["retained_count"]) is int
             and handoff["released_count"] == released and handoff["retained_count"] == total-released
             and type(run["outcome"]["payload_released_count"]) is int
             and run["outcome"]["payload_released_count"] == released, "observed_inventory_mismatch")
    state = handoff["execution_state"]
    _state(state)
    _require(_same(state, run["final_state"]) and run["outcome"]["phase"] == "orbital_coast"
             and run["samples"][-1]["phase"] == "orbital_coast" and all(_same(run["samples"][-1][k], state[k]) for k in STATE), "execution_state_reset_or_phase")
    _require(0 <= state["time_s"]-latest["time_s"] <= 2.001
             and math.isclose(state["time_s"], authority["checkpoint_time_s"], rel_tol=0., abs_tol=1e-8), "handoff_timeframe")
    events = [e for e in run["events"] if e["event"] == "payload_released"]
    _require(len(events) == len(releases) == len(run["satellites"]) == released, "separation_event_count")
    for event, observation, satellite in zip(events, releases, run["satellites"]):
        # The loop observes before release; both can share one tick timestamp.
        _require(observation["previous_observation_time_s"] <= event["time_s"] <= observation["observed_time_s"]
                 and satellite["release_state"]["time_s"] == event["time_s"], "separation_event_timeframe")
    commands = [e for e in run["events"] if e["event"] == "managed_mission_command" and e["point"].startswith("deployment")]
    _require(_same([{ "kind": "command", "action": e["action"], "time_s": e["time_s"]} for e in commands],
                  [e for e in history if e["kind"] == "command"]), "executed_command_trace_mismatch")
    origin = _observed_origin(state, total-released)
    _require(_same(handoff["origin"], origin) and _same(prediction["origin"], origin), "observed_origin_or_count_mismatch")
    dry = _vehicle_inventory(profile, run["final_vehicle"], total-released)
    binding = {"schema": "missionos.deployment_forecast_binding.v1", "handoff_sha256": handoff["handoff_sha256"],
        "authority_sha256": handoff["authority_sha256"], "profile_sha256": handoff["profile_sha256"],
        "generation": generation, "retained_count": total-released, "origin_sha256": _digest(origin), "dispatch_authorized": False}
    _require(_same(prediction["deployment_handoff"], binding), "forecast_generation_or_binding")
    _require(type(forecast_bytes) is bytes and sha256(forecast_bytes).hexdigest() == prediction["prediction_sha256"], "persisted_forecast_hash")
    forecast = json.loads(forecast_bytes, object_pairs_hook=_pairs)
    _require(_strict(forecast) and forecast["schema"] == "missionos.ship_return_prediction.v1"
             and _same(forecast["origin"], origin) and all(forecast[k] is False for k in ("prediction_is_execution", "runtime_admission", "observation_uncertainty_qualified", "recovery_area_qualified", "waiting_endurance_qualified")), "forecast_scope_or_origin")
    cutoffs = [e["time_s"] for e in run["events"] if e["event"] == "orbit_cutoff_command"]
    _require(len(cutoffs) == 1, "original_schedule_missing")
    scheduled = cutoffs[0]+profile["guidance"]["coast_before_return_s"]
    _require(forecast["return_time_s"] == prediction["return_time_s"] == scheduled
             and forecast["opportunity_window"] == {"schema": "missionos.ship_return_opportunity_window.v1", "scheduled_return_time_s": scheduled, "maximum_delay_s": 6000.}
             and forecast["horizon_s"] == 10000. and _number(prediction["integration_scale"])
             and prediction["integration_scale"] == 1., "forecast_schedule_or_scale")
    _require(set(prediction) == {"origin", "integration_scale", "final_state", "outcome", "return_time_s", "wall_time_s", "prediction_sha256", "coast", "deployment_handoff"}
             and all(_same(prediction[k], forecast[k]) for k in ("final_state", "outcome", "return_time_s", "wall_time_s"))
             and _same(prediction["coast"], [{k: row[k] for k in COAST} for row in forecast["samples"] if row["phase"] == "orbital_coast"]), "compact_forecast_mismatch")
    first = forecast["samples"][0]
    _require(all(_same(first[k], origin["state"][k]) for k in STATE)
             and math.isclose(first["mass_kg"], dry+origin["state"]["propellant_kg"], rel_tol=1e-12, abs_tol=1e-6), "forecast_initial_state_or_physical_inventory")
    return {"released_count": released, "retained_count": total-released, "generation": generation,
            "forecast_sha256": prediction["prediction_sha256"]}


def verify(study, forecast_record):
    """Check saved records only. ``forecast_record`` is the exact file bytes."""
    result = {"schema": "missionos.starship_m2_handoff_verification.v1", "passed": False, "issues": [],
              "scope": "observed_inventory_state_to_forecast_only", "return_admission": False,
              "mission_completed": False, "physical_execution": False, "independent_physics_validation": False,
              "source_manifest_validated_against_external_commit": False}
    try:
        result["evidence"] = _check(study, forecast_record)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, ZeroDivisionError, UnicodeError, RecursionError) as error:
        result["issues"].append(str(error) if type(error) is ValueError else f"malformed_record:{type(error).__name__}")
    else:
        result["passed"] = True
    return result
