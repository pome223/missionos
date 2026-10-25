"""Separate unqualified suffix admission for a verified, settled v7 source.

The full source bytes and saved-step checker are consumed once at construction.
Only small immutable handoff facts remain cached. No controller or dynamics is
run here; a source receipt is neither mission authority nor return qualification.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, InitVar
from hashlib import sha256
import re

from . import starship_physics as env, starship_sixdof as dyn
from .starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
    AscentScreenSettings, AscentShutdown, StateInsertionOptions)
from .starship_ascent_state_metrics import verify_state_insertion_record
from .starship_experimental_continuation import (_encode, _read, _continuation_steps,
                                                _verify_continuation_record)

POLICY = "state_insertion_v7"
ORIGIN_SCHEMA = "missionos.starship_state_insertion_continuation_origin.v1"
VERIFICATION_SCHEMA = "missionos.starship_state_insertion_continuation_verification.v1"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _settings(value):
    _require(type(value) is dict and value.get("policy") == POLICY, "state_origin_requires_explicit_v7_settings")
    try:
        settings = AscentScreenSettings(stack_pitch_offset_deg=value["stack_pitch_offset_deg"],
            ship_pitch_offset_deg=value["ship_pitch_offset_deg"], finite_mode=value["finite_mode"], policy=value["policy"],
            shutdowns=tuple(AscentShutdown(**row) for row in value["shutdowns"]),
            objectives=AscentCoastOrbitObjectives(**value["objectives"]),
            control_options=AscentControlOptions(**value["control_options"]),
            insertion_options=StateInsertionOptions(**value["insertion_options"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid_state_origin_settings") from error
    _require(settings.record() == value and settings.insertion_options.shutdown_settle_s == 5.,
             "state_origin_settings_or_settle_scope_changed")
    return settings


def _validated_handoff(source, profile, settings):
    _require(type(source) is dict and type(profile) is dict and profile.get("payload", {}).get("count") == 26,
             "state_origin_requires_profile_and_full_inventory")
    typed = _settings(settings)
    _require(profile["guidance"]["target_perigee_m"] == 220000.
        and profile["ship"]["ascent_reserve_kg"] == 75000., "state_origin_mission_gates_changed")
    verification = verify_state_insertion_record(source, profile, expected_settings=typed)
    _require(verification.get("passed") is True, "state_origin_full_source_verification_failed")
    outcome, screen = source["outcome"], source["ascent_screen"]
    _require(outcome["termination"] == "insertion_gate_reached" and outcome["phase"] == "ascent_screen_shutdown"
        and outcome["payload_released_count"] == 0 and source["satellites"] == []
        and screen["mission_dispatch"] is False and screen["return_qualified"] is False,
        "state_origin_requires_completed_unqualified_insertion")
    state = dyn.state_from_dict(source["final_state"])
    dyn.vehicle_from_dict(source["final_vehicle"])
    orbit = env.orbital_elements(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    cutoff = screen["cutoff_measurement"]["time_s"]
    _require(state.time_s == screen["insertion_shutdown_until_s"] == cutoff+5.
        and orbit["status"] == "bound" and orbit["perigee_altitude_m"] >= 220000.
        and orbit["apogee_altitude_m"] <= 600000. and state.propellant_kg >= 75000.
        and env.norm(state.omega_body_rad_s) <= 5. and outcome["max_body_rate_rad_s"] <= 5.
        and len(state.engine_states) >= 3 and all(status.available for status in state.engine_states[:3]),
        "state_origin_actual_settled_envelope_rejected")
    release = cutoff+profile["guidance"]["orbit_release_delay_s"]
    return_time = cutoff+profile["guidance"]["coast_before_return_s"]
    _require(state.time_s < release < return_time, "state_origin_pending_schedule_not_supported")
    return {"source_policy": POLICY, "source_phase": outcome["phase"], "retained_count": 26,
        "state": deepcopy(source["final_state"]), "vehicle": deepcopy(source["final_vehicle"]),
        "final_gate_time_s": cutoff, "settled_handoff_time_s": state.time_s,
        "schedule_anchor_time_s": cutoff, "schedule_anchor": "verified_actual_cutoff_before_five_second_settle",
        "first_release_time_s": release, "return_request_time_s": return_time,
        "source_full_control_trace_verified": True, "source_settled_envelope_verified": True,
        "source_verified_control_ticks": verification["verified_control_ticks"],
        "qualified_return": False, "mission_authority_granted": False, "physical_execution": False}


@dataclass(frozen=True)
class StateInsertionOrigin:
    """Exact v7-only type; callers cannot initialize a receipt without proof."""
    source_record_sha256: str
    _raw_record_bytes: InitVar[bytes]
    _profile_json: str = field(repr=False)
    _settings_json: str = field(repr=False)
    _expected_record: InitVar[dict | None] = None
    _origin_json: str = field(init=False, repr=False)

    def __post_init__(self, _raw_record_bytes, _expected_record):
        _require(type(self.source_record_sha256) is str and re.fullmatch("[0-9a-f]{64}", self.source_record_sha256)
            and type(_raw_record_bytes) is bytes and sha256(_raw_record_bytes).hexdigest() == self.source_record_sha256
            and type(self._profile_json) is str and type(self._settings_json) is str, "state_origin_source_hash_mismatch")
        source = _read(_raw_record_bytes)
        _require(_expected_record is None or source == _expected_record, "state_origin_raw_record_differs")
        handoff = _validated_handoff(source, _read(self._profile_json), _read(self._settings_json))
        handoff.update(schema=ORIGIN_SCHEMA, source_record_sha256=self.source_record_sha256,
            source_profile_sha256=sha256(self._profile_json.encode()).hexdigest(),
            source_settings_sha256=sha256(self._settings_json.encode()).hexdigest())
        object.__setattr__(self, "_origin_json", _encode(handoff).decode())

    @property
    def time_s(self):
        return _read(self._origin_json)["settled_handoff_time_s"]

    @property
    def final_gate_time_s(self):
        return _read(self._origin_json)["final_gate_time_s"]

    def validate_profile(self, profile):
        _require(_encode(profile) == self._profile_json.encode(), "state_origin_profile_changed")

    def record(self):
        return _read(self._origin_json)


def state_insertion_origin(record, profile, *, record_sha256, expected_settings, raw_record_bytes=None):
    raw = _encode(record) if raw_record_bytes is None else raw_record_bytes
    return StateInsertionOrigin(record_sha256, raw, _encode(profile).decode(),
                                _encode(expected_settings).decode(), record)


def state_insertion_continuation_steps(origin, profile, *, duration_s=6000.):
    return _continuation_steps(origin, profile, duration_s=duration_s, expected_origin_type=StateInsertionOrigin)


def verify_state_insertion_continuation_record(record, origin, profile, *, expected_duration_s=6000.):
    return _verify_continuation_record(record, origin, profile, expected_duration_s=expected_duration_s,
        expected_origin_type=StateInsertionOrigin, verification_schema=VERIFICATION_SCHEMA)
