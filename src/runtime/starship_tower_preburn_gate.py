"""Fixture contract for one site choice before a site-specific burn.

This isolated Rules gate does not validate a physical qualification record,
integrate a vehicle, activate a scenario, or apply a goal. Path and directive
hashes identify untrusted caller assertions only; their provenance is not
verified. Future activation must bind an independently checked supervision
record and actor integrity, plus both independently checked physical paths.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from threading import Lock

from .starship_tower_supervision import _context, _observation, _state

SCHEMA = "missionos.starship_tower_preburn_fixture.v1"
PATH_SCHEMA = "missionos.starship_tower_preburn_path_assertion.v1"
BINDING_FIELDS = frozenset({"plan_sha256", "source_sha256", "profile_sha256", "catch_profile_sha256",
    "return_sites_sha256", "origin_state_sha256", "fault_history_sha256", "origin_controller_context_sha256",
    "approval_record_sha256"})
CONTRACT_FIELDS = frozenset({"schema", "mode", "context", "bindings", "issued_simulation_time_s", "issued_wall_time_s",
    "original_simulation_deadline_s", "original_wall_deadline_s", "latest_preburn_choice_time_s",
    "maximum_observation_age_s", "expected_phase"})
PATH_FIELDS = frozenset({"schema", "mode", "site_id", "bindings", "raw_record_sha256", "verifier_receipt_sha256",
    "declared_status", "declared_terminal_objective_achieved", "common_preburn_prefix_sha256", "applicable_states"})
APPLICABLE_FIELDS = frozenset({"state_sha256", "time_s", "controller_context_sha256", "fault_prefix_sha256"})
DIRECTIVE_FIELDS = frozenset({"schema", "context", "source_sha256", "approval_record_sha256", "action", "reason",
    "simulation_time_s", "wall_time_s", "observed_evidence_sha256", "state_sha256", "consumed_once",
    "numeric_flight_authority", "requires_executor_rules_check", "executor_application_reported",
    "physical_effect_verified", "sha256"})


class PreburnGateError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise PreburnGateError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(_canonical(value)).hexdigest()


def _copy(value):
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise PreburnGateError("invalid_preburn_json") from None


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _clock(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _bindings(value):
    _require(type(value) is dict and set(value) == BINDING_FIELDS
        and all(_hash(item) for item in value.values()), "invalid_preburn_bindings")


def validate_contract(value):
    value = _copy(value)
    _require(type(value) is dict and set(value) == CONTRACT_FIELDS and value["schema"] == SCHEMA
        and value["mode"] == "fixture" and value["expected_phase"] == "recovery_boostback_slew",
        "preburn_fixture_contract_required")
    _bindings(value["bindings"])
    _context(value["context"])
    _require(value["context"]["plan_sha256"] == value["bindings"]["plan_sha256"], "preburn_contract_context_mismatch")
    _require(all(_clock(value[key]) for key in CONTRACT_FIELDS-{"schema", "mode", "context", "bindings", "expected_phase"})
        and value["issued_simulation_time_s"] < value["original_simulation_deadline_s"]
        and value["issued_wall_time_s"] < value["original_wall_deadline_s"]
        and value["issued_simulation_time_s"] < value["latest_preburn_choice_time_s"]
        <= value["original_simulation_deadline_s"] and 0 < value["maximum_observation_age_s"] <= 2.,
        "invalid_preburn_original_deadlines")
    return value


def validate_path_assertion(value, *, bindings, site_id):
    """Check shape/bindings, without asserting that the path was integrated."""
    value = _copy(value)
    _require(type(value) is dict and set(value) == PATH_FIELDS and value["schema"] == PATH_SCHEMA
        and value["mode"] == "fixture" and value["site_id"] == site_id
        and value["bindings"] == bindings and value["declared_status"] in ("complete", "partial", "failed")
        and type(value["declared_terminal_objective_achieved"]) is bool
        and all(_hash(value[key]) for key in ("raw_record_sha256", "verifier_receipt_sha256", "common_preburn_prefix_sha256"))
        and type(value["applicable_states"]) is list and 1 <= len(value["applicable_states"]) <= 2048,
        "invalid_preburn_path_assertion")
    seen, last_time = set(), -1.
    for row in value["applicable_states"]:
        _require(type(row) is dict and set(row) == APPLICABLE_FIELDS
            and _clock(row["time_s"]) and row["time_s"] > last_time
            and all(_hash(row[key]) for key in APPLICABLE_FIELDS-{"time_s"})
            and row["state_sha256"] not in seen, "invalid_preburn_applicable_state")
        seen.add(row["state_sha256"])
        last_time = row["time_s"]
    return value


class FixturePreburnGate:
    """One-use fixture eligibility receipt; never an executor or approval gate."""

    def __init__(self, contract, path_assertions):
        self._contract = validate_contract(contract)
        paths = _copy(path_assertions)
        _require(type(paths) is list and len(paths) == 2, "both_preburn_path_assertions_required")
        self._paths = [validate_path_assertion(path, bindings=self._contract["bindings"], site_id=site)
            for path, site in zip(paths, ("capture", "divert"))]
        _require(self._paths[0]["common_preburn_prefix_sha256"] == self._paths[1]["common_preburn_prefix_sha256"],
            "preburn_paths_do_not_share_common_prefix")
        self._lock = Lock()
        self._last = self._contract["issued_simulation_time_s"], self._contract["issued_wall_time_s"]
        self._receipt = {"schema": SCHEMA, "mode": "fixture", "contract": self._contract,
            "path_assertions": self._paths, "checks": [], "fixture_choice": None,
            "first_site_specific_command_report": None, "later_observation_report": None,
            "status": "pending", "qualification_evidence_status": "caller_assertions_unchecked",
            "directive_evidence_status": "untrusted_caller_assertion",
            "directive_provenance_verified": False, "approval_independently_verified": False,
            "supervision_record_bound": False, "actor_integrity_verified": False,
            "qualification_verified": False, "physical_default_available": False,
            "physical_activation_allowed": False, "goal_application_reported": False,
            "physical_effect_verified": False, "state_written": False, "new_authority_created": False}

    def _applicable(self, state, controller_context_sha256, fault_prefix_sha256):
        marker = {"state_sha256": digest(state), "time_s": state["time_s"],
            "controller_context_sha256": controller_context_sha256, "fault_prefix_sha256": fault_prefix_sha256}
        return all(path["declared_status"] == "complete" and path["declared_terminal_objective_achieved"] is True
            and marker in path["applicable_states"] for path in self._paths)

    def consider(self, observation, *, simulation_time_s, wall_time_s, phase, source_sha256,
                 plan_sha256, controller_context_sha256, fault_prefix_sha256,
                 first_site_specific_command_issued, directive=None):
        """Check an untrusted directive-shaped assertion, never its provenance."""
        observation = _copy(observation)
        _observation(observation)
        directive = _copy(directive) if directive is not None else None
        if directive is not None:
            _require(type(directive) is dict and set(directive) == DIRECTIVE_FIELDS
                and directive.get("schema") == "missionos.starship_tower_directive.v1"
                and _hash(directive.get("sha256")), "preburn_typed_directive_required")
            _context(directive["context"])
            _require(directive["sha256"] == digest({key: value for key, value in directive.items() if key != "sha256"})
                and all(_hash(directive[key]) for key in ("source_sha256", "approval_record_sha256",
                    "observed_evidence_sha256", "state_sha256"))
                and directive["action"] in ("continue_capture", "divert")
                and _clock(directive["simulation_time_s"]) and _clock(directive["wall_time_s"])
                and directive["consumed_once"] is True and directive["numeric_flight_authority"] is False
                and directive["requires_executor_rules_check"] is True
                and directive["executor_application_reported"] is False and directive["physical_effect_verified"] is False,
                "preburn_directive_assertion_format_invalid")
        _require(all(_hash(item) for item in (source_sha256, plan_sha256, controller_context_sha256, fault_prefix_sha256))
            and type(first_site_specific_command_issued) is bool and type(phase) is str,
            "invalid_preburn_current_evidence")
        with self._lock:
            _require(_clock(simulation_time_s) and _clock(wall_time_s)
                and simulation_time_s >= self._last[0] and wall_time_s >= self._last[1]
                and observation["simulation_time_s"] <= simulation_time_s and observation["wall_time_s"] <= wall_time_s,
                "preburn_clock_regressed_or_future_observation")
            _require(len(self._receipt["checks"]) < 2048, "preburn_check_budget_exhausted")
            reason, requested_site = "awaiting_choice", None
            c = self._contract
            fresh = 0 <= simulation_time_s-observation["simulation_time_s"] <= c["maximum_observation_age_s"] and \
                0 <= wall_time_s-observation["wall_time_s"] <= c["maximum_observation_age_s"]
            if self._receipt["fixture_choice"] is not None:
                reason = "choice_already_consumed"
            elif first_site_specific_command_issued or self._receipt["first_site_specific_command_report"] is not None:
                reason = "first_site_specific_command_already_issued"
            elif simulation_time_s >= c["latest_preburn_choice_time_s"]:
                reason = "latest_preburn_boundary_reached"
            elif phase != c["expected_phase"]:
                reason = "phase_outside_preburn_gate"
            elif source_sha256 != c["bindings"]["source_sha256"] or plan_sha256 != c["bindings"]["plan_sha256"]:
                reason = "source_or_plan_changed"
            elif observation["context"] != c["context"]:
                reason = "observation_cross_context"
            elif not fresh:
                reason = "fresh_preburn_observation_required"
            elif simulation_time_s != observation["simulation_time_s"]:
                reason = "same_time_actual_preburn_state_required"
            elif not self._applicable(observation["state"], controller_context_sha256, fault_prefix_sha256):
                reason = "both_path_assertions_not_applicable"
            elif simulation_time_s >= c["original_simulation_deadline_s"] or wall_time_s >= c["original_wall_deadline_s"]:
                requested_site, reason = "divert", "original_deadline_fixture_default"
            elif observation["tower_ready"] is False:
                requested_site, reason = "divert", "fresh_tower_unavailable_fixture_default"
            elif directive is not None:
                _require(type(directive) is dict and set(directive) == DIRECTIVE_FIELDS
                    and directive["schema"] == "missionos.starship_tower_directive.v1"
                    and directive["sha256"] == digest({key: value for key, value in directive.items() if key != "sha256"})
                    and directive["context"] == observation["context"]
                    and directive["approval_record_sha256"] == c["bindings"]["approval_record_sha256"]
                    and directive["source_sha256"] == source_sha256
                    and directive["action"] in ("continue_capture", "divert")
                    and directive["observed_evidence_sha256"] == observation["evidence_sha256"]
                    and directive["state_sha256"] == observation["state_sha256"]
                    and directive["simulation_time_s"] == simulation_time_s and directive["wall_time_s"] == wall_time_s
                    and directive["consumed_once"] is True and directive["numeric_flight_authority"] is False
                    and directive["requires_executor_rules_check"] is True
                    and directive["executor_application_reported"] is False and directive["physical_effect_verified"] is False,
                    "preburn_typed_directive_binding_mismatch")
                if directive["action"] == "continue_capture" and observation["tower_ready"] is not True:
                    reason = "fresh_positive_tower_readiness_required"
                else:
                    requested_site = "capture" if directive["action"] == "continue_capture" else "divert"
                    reason = "untrusted_directive_assertion_fixture_eligible"
            check = {"simulation_time_s": simulation_time_s, "wall_time_s": wall_time_s,
                "state_sha256": observation["state_sha256"], "observation_evidence_sha256": observation["evidence_sha256"],
                "controller_context_sha256": controller_context_sha256, "fault_prefix_sha256": fault_prefix_sha256,
                "reason": reason, "fixture_requested_site_id": requested_site,
                "directive_sha256": directive["sha256"] if directive is not None else None,
                "directive_evidence_status": "untrusted_caller_assertion",
                "directive_provenance_verified": False, "approval_independently_verified": False,
                "supervision_record_bound": False, "actor_integrity_verified": False,
                "physical_application_allowed": False}
            # Invalid directive validation must not advance retained clocks or
            # alter the receipt. Commit only a fully constructed valid check.
            self._last = simulation_time_s, wall_time_s
            self._receipt["checks"].append(check)
            if requested_site is not None:
                self._receipt["fixture_choice"] = {**check, "sha256": digest(check)}
                self._receipt["status"] = "fixture_choice_consumed_without_application"
            elif reason in ("latest_preburn_boundary_reached", "first_site_specific_command_already_issued"):
                self._receipt["status"] = "closed_without_applicable_physical_default"
            return _copy(check)

    def record_first_command_assertion(self, *, choice_sha256, state, site_id, command_sha256):
        """Bind an external fixture command report, without issuing a command."""
        state = _copy(state)
        _state(state)
        with self._lock:
            choice = self._receipt["fixture_choice"]
            _require(choice is not None and choice_sha256 == choice["sha256"]
                and self._receipt["first_site_specific_command_report"] is None and _hash(command_sha256)
                and site_id == choice["fixture_requested_site_id"] and state["time_s"] == choice["simulation_time_s"]
                and state["time_s"] == self._last[0]
                and digest(state) == choice["state_sha256"], "preburn_first_command_assertion_mismatch")
            self._receipt["first_site_specific_command_report"] = {"choice_sha256": choice_sha256,
                "state_sha256": digest(state), "time_s": state["time_s"], "site_id": site_id,
                "command_sha256": command_sha256, "caller_assertion_unchecked": True,
                "directive_evidence_status": "untrusted_caller_assertion", "approval_independently_verified": False,
                "directive_provenance_verified": False, "supervision_record_bound": False,
                "actor_integrity_verified": False, "command_issued_by_gate": False}
            return _copy(self._receipt["first_site_specific_command_report"])

    def record_later_observation_assertion(self, *, choice_sha256, state, site_id, controller_context_sha256):
        """A later supplied state remains separate from applied-goal/effect proof."""
        state = _copy(state)
        _state(state)
        with self._lock:
            choice = self._receipt["fixture_choice"]
            _require(choice is not None and choice_sha256 == choice["sha256"]
                and self._receipt["first_site_specific_command_report"] is not None
                and self._receipt["later_observation_report"] is None and _hash(controller_context_sha256)
                and site_id == choice["fixture_requested_site_id"] and state["time_s"] > choice["simulation_time_s"]
                and state["time_s"] >= self._last[0]
                and digest(state) != choice["state_sha256"], "preburn_later_observation_assertion_mismatch")
            self._receipt["later_observation_report"] = {"choice_sha256": choice_sha256, "state_sha256": digest(state),
                "time_s": state["time_s"], "site_id": site_id, "controller_context_sha256": controller_context_sha256,
                "caller_assertion_unchecked": True, "goal_application_verified": False, "physical_effect_verified": False}
            self._receipt["later_observation_report"].update(directive_provenance_verified=False,
                directive_evidence_status="untrusted_caller_assertion", approval_independently_verified=False,
                supervision_record_bound=False, actor_integrity_verified=False)
            return _copy(self._receipt["later_observation_report"])

    def receipt(self):
        with self._lock:
            return _copy(self._receipt)
