"""Explicit unqualified continuation of a verified v5 ascent checkpoint.

The source byte/profile/settings bindings identify an experimental simulator
checkpoint, not a dispatch permit or physical execution. Source/process claims
must also be bound by the separately registered caller. Neither this module nor
constructing an origin advances dynamics, calls a provider, or reads a secret.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import re

from . import starship_physics as env, starship_sixdof as dyn
from .starship_ascent_phase_metrics import COAST_ORBIT_POLICY, verify_ascent_record
from .starship_retained_return import TRIMMED_POLICY_ID


ORIGIN_SCHEMA = "missionos.experimental_ascent_continuation_origin.v1"


def _encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


def _read(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_experimental_origin_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite_experimental_origin")))


def development_settings():
    """Existing opt-in experimental allocation; never the39-case certificate."""
    return {"release_limit": 26, "bounded_ship_flaps": True, "application": "wind_trim_state_return_v2"}


def _validate_source(source, profile, settings):
    if (type(source) is not dict or type(profile) is not dict or type(settings) is not dict
            or settings.get("policy") != COAST_ORBIT_POLICY or profile.get("payload", {}).get("count") != 26):
        raise ValueError("experimental_origin_requires_registered_v5_ascent")
    verification = verify_ascent_record(source, expected_settings=settings, profile=profile)
    if not verification["passed"]:
        raise ValueError("experimental_origin_source_record_rejected:"+json.dumps(verification["issues"], sort_keys=True))
    outcome, screen = source["outcome"], source["ascent_screen"]
    if (outcome["termination"] != "insertion_gate_reached" or outcome["phase"] != "ascent_screen_shutdown"
            or outcome["payload_released_count"] != 0 or source["satellites"] != []
            or screen["mission_dispatch"] is not False or screen["return_qualified"] is not False):
        raise ValueError("experimental_origin_requires_actual_completed_insertion")
    state = dyn.state_from_dict(source["final_state"])
    vehicle = dyn.vehicle_from_dict(source["final_vehicle"])
    orbit = env.orbital_elements(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    cutoff_time = screen["cutoff_measurement"]["time_s"]
    if (state.time_s != 1537. or orbit["status"] != "bound"
            or orbit["perigee_altitude_m"] < profile["guidance"]["target_perigee_m"]
            or state.propellant_kg <= profile["ship"]["return_reserve_kg"]
            or not all(status.available for status in state.engine_states[:3])
            or cutoff_time+profile["guidance"]["orbit_release_delay_s"] <= state.time_s
            or cutoff_time+profile["guidance"]["coast_before_return_s"] <= state.time_s):
        raise ValueError("experimental_origin_actual_state_or_pending_schedule_not_supported")
    return state, vehicle


@dataclass(frozen=True)
class ExperimentalAscentOrigin:
    source_record_sha256: str
    _source_json: str = field(repr=False)
    _profile_json: str = field(repr=False)
    _settings_json: str = field(repr=False)

    def __post_init__(self):
        if (type(self.source_record_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.source_record_sha256)
                or any(type(value) is not str for value in (self._source_json, self._profile_json, self._settings_json))
                or sha256(self._source_json.encode()).hexdigest() != self.source_record_sha256):
            raise ValueError("experimental_origin_source_hash_mismatch")
        _validate_source(_read(self._source_json), _read(self._profile_json), _read(self._settings_json))

    @property
    def time_s(self):
        return _read(self._source_json)["final_state"]["time_s"]

    @property
    def final_gate_time_s(self):
        return _read(self._source_json)["ascent_screen"]["cutoff_measurement"]["time_s"]

    def validate_profile(self, profile):
        if _encode(profile) != self._profile_json.encode():
            raise ValueError("experimental_origin_profile_changed")

    def record(self):
        source, profile = _read(self._source_json), _read(self._profile_json)
        return {"schema": ORIGIN_SCHEMA, "source_record_sha256": self.source_record_sha256,
            "source_profile_sha256": sha256(self._profile_json.encode()).hexdigest(),
            "source_settings_sha256": sha256(self._settings_json.encode()).hexdigest(),
            "source_policy": COAST_ORBIT_POLICY, "source_phase": source["outcome"]["phase"],
            "retained_count": 26, "state": deepcopy(source["final_state"]),
            "vehicle": deepcopy(source["final_vehicle"]), "final_gate_time_s": self.final_gate_time_s,
            "first_release_time_s": self.final_gate_time_s+profile["guidance"]["orbit_release_delay_s"],
            "return_request_time_s": self.final_gate_time_s+profile["guidance"]["coast_before_return_s"],
            "qualified_return": False, "mission_authority_granted": False, "physical_execution": False}


def experimental_ascent_origin(record, profile, *, record_sha256, expected_settings, raw_record_bytes=None):
    """Bind complete source JSON bytes; never replace its state or vehicle."""
    raw = _encode(record) if raw_record_bytes is None else raw_record_bytes
    if type(raw) is not bytes or _read(raw) != record:
        raise ValueError("experimental_origin_raw_record_differs")
    return ExperimentalAscentOrigin(record_sha256, raw.decode("utf-8"),
        _encode(profile).decode("utf-8"), _encode(expected_settings).decode("utf-8"))


def experimental_continuation_steps(origin, profile, *, duration_s=6000.):
    """Prepare the shared deployment/return generator; iteration needs a trial.

    The elapsed horizon is converted to an absolute final clock. There are no
    hooks for AI, visual decisions, arbitrary faults, qualification or booster
    execution in this first physical continuation boundary.
    """
    return _continuation_steps(origin, profile, duration_s=duration_s,
        expected_origin_type=ExperimentalAscentOrigin)


def _continuation_steps(origin, profile, *, duration_s, expected_origin_type):
    """Shared suffix machinery; public callers supply a separate exact type."""
    if (type(origin) is not expected_origin_type or type(duration_s) not in (int, float)
            or not math.isfinite(duration_s) or not 0 < duration_s <= 6000.):
        raise ValueError("invalid_experimental_continuation_scope")
    origin.validate_profile(profile)
    if origin.time_s+duration_s <= origin.record()["return_request_time_s"]:
        raise ValueError("experimental_continuation_horizon_precedes_return_request")
    from .starship_sixdof_mission import _simulate_steps
    return _simulate_steps(profile, scenario="launch", duration_s=origin.time_s+duration_s,
        return_policy=TRIMMED_POLICY_ID, _development_return_qualification=development_settings(),
        _defer_booster=True, _experimental_ascent_origin=origin)


def verify_experimental_continuation_record(record, origin, profile, *, expected_duration_s=6000.):
    """Check saved handoff/releases/contact separately from physical success.

    The source attests the earlier orbital gate. The existing payload checker
    receives that prior context as a separate argument, never as a fabricated
    event inserted into the serialized continuation. No old launch/certificate
    validator is relaxed or asked to treat this suffix as a flight from time0.
    """
    return _verify_continuation_record(record, origin, profile, expected_duration_s=expected_duration_s,
        expected_origin_type=ExperimentalAscentOrigin,
        verification_schema="missionos.experimental_ascent_continuation_verification.v1")


def _verify_continuation_record(record, origin, profile, *, expected_duration_s, expected_origin_type,
                                verification_schema):
    """Shared saved suffix checks; no change to either source admission gate."""
    from . import starship_sixdof_verifier as saved
    from . import starship_sixdof_mission as mission
    from .starship_ascent_phase_metrics import STATE_FIELDS
    result = {"schema": verification_schema, "passed": False,
        "issues": [], "physical_success": False, "deployment_completed": False,
        "scope": "saved source continuity, payload accounting, availability and contact; no integration or real-flight qualification"}
    def require(condition, detail):
        saved._require(condition, "$", detail)
    def same(left, right):
        return type(left) is dict and type(right) is dict and all(left.get(k) == right.get(k) for k in STATE_FIELDS)
    try:
        require(type(origin) is expected_origin_type, "A typed experimental source is required")
        require(type(expected_duration_s) in (int, float) and math.isfinite(expected_duration_s)
            and 0 < expected_duration_s <= 6000., "Explicit bounded elapsed duration is required")
        origin.validate_profile(profile)
        before = origin.record()
        require(origin.time_s+expected_duration_s > before["return_request_time_s"],
            "Registered elapsed horizon precedes return request")
        saved._json_tree(record)
        require(record.get("scenario") == "experimental_ascent_continuation", "Suffix cannot claim a flight from time0")
        first, last, outcome, events, speed = saved._run_basics(record, "$", [0], profile)
        require(outcome["termination"] != "numerical_failure", "Numerical failure is invalid, not a physical negative")
        require(same(first, before["state"]) and first["phase"] == before["source_phase"]
            and same(record.get("initial_state"), before["state"]) and same(last, record.get("final_state")),
            "Full source/final state or finite actuator history changed")
        require(outcome.get("start_time_s") == origin.time_s and outcome.get("end_time_s") == last["time_s"]
            and origin.time_s <= last["time_s"] <= origin.time_s+expected_duration_s
            and outcome.get("orbit_gate_reached") is True
            and type(outcome.get("integration_steps")) is int and outcome["integration_steps"] > 0,
            "Absolute clock or elapsed continuation horizon changed")
        metadata = record.get("experimental_ascent_continuation", {})
        require(metadata.get("origin") == before and metadata.get("initial_retained_count") == 26
            and type(metadata.get("requested_elapsed_duration_s")) in (int, float)
            and metadata["requested_elapsed_duration_s"] == expected_duration_s,
            "Continuation source identity/inventory differs")
        if outcome["termination"] == "time_limit":
            require(last["time_s"] == origin.time_s+expected_duration_s,
                "Time-limit record ended before registered elapsed horizon")
        for key, value in (("source_clock_preserved", True), ("state_reset", False),
            ("controller_phase_transition_only", True), ("shared_production_deployment_and_return_loop", True),
            ("qualified_return", False), ("mission_authority_granted", False), ("physical_execution", False)):
            require(metadata.get(key) is value, "Continuation authority/phase claim differs")
        require(record.get("development_return_qualification") == development_settings()
            and "return_qualification_sha256" not in record and "mission_director" not in record
            and "visual_observation" not in record and record.get("booster_run") is None
            and record.get("booster_separation_state") is None, "Experimental suffix borrowed another scope")
        handoffs = events.get("experimental_ascent_continuation", [])
        require(len(handoffs) == 1, "Missing same-state controller transition")
        handoff = handoffs[0]
        require(handoff["time_s"] == origin.time_s and handoff.get("source_record_sha256") == origin.source_record_sha256
            and handoff.get("source_phase") == before["source_phase"] and handoff.get("controller_phase") == "orbital_coast"
            and handoff.get("state_before") == handoff.get("state_after") == before["state"]
            and handoff.get("first_release_time_s") == before["first_release_time_s"]
            and handoff.get("return_request_time_s") == before["return_request_time_s"],
            "Controller transition reset a state or changed pending schedule")
        count = outcome.get("payload_released_count")
        require(type(count) is int and 0 <= count <= 26 and metadata.get("final_retained_count") == 26-count,
            "Retained inventory does not follow actual releases")
        # Source record and checker already established this gate before the
        # suffix started. This context is not appended to record['events'].
        payload_context = {**events, "orbit_cutoff_command": [{"time_s": before["final_gate_time_s"]}]}
        saved._payloads(record, profile, "$", [len(record["samples"])], payload_context, True)
        releases = events.get("payload_released", [])
        require(not releases or releases[0]["time_s"] >= before["first_release_time_s"], "Payload release precedes pending release time")
        flags = [row["available"] for row in before["state"]["engine_states"]]
        vehicles = {}
        previous_fuel = before["state"]["propellant_kg"]
        for sample in record["samples"]:
            already = sum(event["time_s"] < sample["time_s"] or event["time_s"] == sample["time_s"]
                and same(sample, event["receipt"]["parent_after_state"]) for event in releases)
            retained = 26-already
            if retained not in vehicles:
                vehicles[retained] = mission.vehicle(profile, payload_count=retained)
            actual = dyn.state_from_dict({key: sample[key] for key in STATE_FIELDS})
            require([a.available for a in actual.engine_states] == flags, "Inherited engine availability changed")
            require(actual.propellant_kg <= previous_fuel+1e-6, "Continuation added unregistered propellant")
            previous_fuel = actual.propellant_kg
            observation = dyn.observe(actual, vehicles[retained])
            for key in ("altitude_m", "mass_kg", "com_z_m"):
                observed = observation["com_body_m"][2] if key == "com_z_m" else observation[key]
                require(saved._near(sample.get(key), observed, 1e-5), "Observation/inventory differs from actual full state")
        final_vehicle = mission.vehicle(profile, payload_count=26-count)
        require(record.get("final_vehicle") == json.loads(json.dumps(asdict(final_vehicle))),
            "Final vehicle lost retained mass or changed engine specifications")
        returns = events.get("return_requested", [])
        require(len(returns) <= 1, "Repeated return request")
        coast_step = profile["integration"]["coast_dt_s"]
        if returns:
            require(0 <= returns[0]["time_s"]-before["return_request_time_s"] <= coast_step+1e-7,
                "Return request changed actual insertion-relative timing")
            require(not releases or releases[-1]["time_s"] <= returns[0]["time_s"], "Payload release continued after return start")
        if last["time_s"] > before["return_request_time_s"]+coast_step+1e-7:
            require(len(returns) == 1, "Missing due return request")
        retained = record.get("retained_return", {})
        require(retained.get("policy_id") == TRIMMED_POLICY_ID and retained.get("landing_verified") is False
            and retained.get("starship_vehicle_validated") is False, "Return policy/claim differs from experimental scope")
        activation = retained.get("activation")
        if returns:
            require(type(activation) is dict and activation.get("time_s") == returns[0]["time_s"]
                and activation.get("payload_retained_count") == 26-count
                and activation.get("payload_retained_mass_kg") == (26-count)*profile["payload"]["mass_each_kg"],
                "Return activation does not use actual retained inventory")
        tilt, rate = None, env.norm(last["omega_body_rad_s"])
        envelope = False
        receipt = outcome.get("contact_receipt")
        if receipt is not None:
            axis = dyn.rotate(last["q_body_to_eci"], (0., 0., 1.))
            tilt = math.degrees(math.acos(max(-1., min(1., env.dot(axis, receipt["surface_normal_eci"])))))
            envelope = (not receipt["initial_overlap"] and receipt["surface_normal_speed_mps"] <= 0
                and speed <= 5. and tilt <= 5. and rate <= .02
                and last["propellant_kg"] >= profile["ship"]["return_reserve_kg"])
        result.update(passed=True, physical_success=bool(count == 26 and returns and envelope),
            deployment_completed=count == 26, payload_released_count=count, retained_count=26-count,
            termination=outcome["termination"], return_requested=bool(returns), contact_envelope_met=envelope,
            contact_point_speed_mps=speed, final_tilt_deg=tilt, final_body_rate_rad_s=rate,
            final_propellant_kg=last["propellant_kg"], source_state_preserved=True,
            physical_reexecution=False, old_certificate_admitted=False)
    except saved._Invalid as exc:
        result["issues"].append(exc.issue)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        result["issues"].append({"code": "invalid_experimental_continuation", "detail": str(exc)})
    return result
