"""Explicit process-local held-command RK4 refinement for restored experiments.

The default strict_v1 wrapper changes only each existing internal RK4 call.
Explicit outer_bracket_v2 additionally reports an interior fuel crossing to the
existing outer root search, without returning a partial state or changing held
commands, actuator limits or contact criteria. It grants no approval or qualification. Its source
equivalence check is a prerequisite for separately registered evidence reuse,
not a numerical convergence result or permission to reuse a consumed trial.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading

from . import starship_sixdof as dyn

_BASELINE_RK4 = dyn._rk4
_PROCESS_ID = os.getpid()
_HOOK_LOCK = threading.Lock()
_PHYSICAL_SOURCE_COUNT = 187


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class RestoredIntegrationPolicy:
    subdivisions: int = 2
    fuel_event_mode: str = "strict_v1"

    def __post_init__(self):
        _require(type(self.subdivisions) is int and self.subdivisions in (2, 4, 8),
            "restored_integration_invalid_subdivision")
        _require(type(self.fuel_event_mode) is str and self.fuel_event_mode in
            ("strict_v1", "outer_bracket_v2"), "restored_integration_invalid_fuel_event_mode")

    @classmethod
    def from_record(cls, value):
        _require(type(value) is dict, "restored_integration_complete_policy_record_required")
        schema = value.get("schema")
        _require(schema in ("missionos.starship_restored_integration_policy.v1",
            "missionos.starship_restored_integration_policy.v2"), "restored_integration_policy_schema")
        # Upgrade is never inferred from subdivisions or a partial dictionary.
        policy = cls(value.get("subdivisions"), "strict_v1" if schema.endswith(".v1") else "outer_bracket_v2")
        _require(_wire(value) == _wire(policy.record()), "restored_integration_policy_roundtrip_changed")
        return policy

    def record(self):
        if self.fuel_event_mode == "outer_bracket_v2":
            return {"schema": "missionos.starship_restored_integration_policy.v2",
                "subdivisions": self.subdivisions,
                "held_command_policy": "existing_outer_commands_no_inner_recompute",
                "fuel_zero_inside_inner_step": "typed_crossing_bracket_then_existing_outer_root_and_dry_coast",
                "experimental": True, "qualification_refreshed": False,
                "mission_authority_granted": False}
        return {"schema": "missionos.starship_restored_integration_policy.v1",
            "subdivisions": self.subdivisions,
            "held_command_policy": "existing_outer_commands_no_inner_recompute",
            "fuel_zero_inside_inner_step": "positive_to_nonpositive_unsupported_zero_to_zero_allowed",
            "experimental": True, "qualification_refreshed": False,
            "mission_authority_granted": False}


class _Execution:
    def __init__(self, policy):
        self._policy = policy
        self._status = "active"
        self._failure_class = None
        self._trace = {"rk4_calls": 0, "rk4_substeps": 0,
            "commands_recomputed_inside_rk4": 0, "outer_control_cadence_changed": False,
            "fuel_event_search_changed": False, "contact_search_changed": False}
        if policy.fuel_event_mode == "outer_bracket_v2":
            self._trace.update(fuel_event_search_changed=True, interrupted_rk4_calls=0, final_endpoint_crossings=0,
                skipped_rk4_substeps=0, failed_rk4_calls=0, invalid_endpoint_rk4_calls=0, incomplete_failure_substeps=0)

    def record(self):
        version = "v1" if self._policy.fuel_event_mode == "strict_v1" else "v2"
        return {"schema": "missionos.starship_restored_integration_execution."+version,
            "policy": self._policy.record(), "status": self._status,
            "failure_class": self._failure_class, "trace": dict(self._trace),
            "process_isolation_required": True, "internal_rk4_only": self._policy.fuel_event_mode == "strict_v1",
            "physics_limits_changed": False, "control_policy_changed": False,
            "experimental": True, "qualification_refreshed": False,
            "mission_authority_granted": False, "execution_permission_granted": False,
            "hosted_model_calls": 0, "nested_sixdof_forecasts": 0}


@contextmanager
def restored_integration(policy):
    """Install one exclusive worker-process hook; restore it on every exit.

    Every command and availability object is held through the inner steps. A
    positive-to-zero or any negative fuel endpoint before the final inner step
    is unsupported and fails closed under strict_v1. Explicit outer_bracket_v2
    instead emits a current-trial-only typed bracket for the outer root search.
    Already inert zero-fuel bodies are valid. Under v2 every negative endpoint
    is a typed bracket (including final-inner endpoints with zero skipped work);
    the existing outer root solver still chooses the accepted physical endpoint.
    This global hook must be used in an isolated simulation worker, not alongside
    any other plant thread. The lock rejects concurrent/nested hook ownership;
    it cannot make unrelated, uncoordinated plant threads safe.
    """
    _require(type(policy) is RestoredIntegrationPolicy, "restored_integration_typed_policy_required")
    _require(os.getpid() == _PROCESS_ID, "restored_integration_forked_process_requires_fresh_import")
    _require(_HOOK_LOCK.acquire(blocking=False), "restored_integration_hook_already_active")
    original = _BASELINE_RK4
    try:
        _require(dyn._rk4 is original and getattr(original, "__module__", None) == dyn.__name__
            and getattr(original, "__qualname__", None) == "_rk4",
            "restored_integration_foreign_hook_active")
        execution = _Execution(policy)
        trace, subdivisions = execution._trace, policy.subdivisions

        # Keep this function's AST identical to the separately registered
        # diagnostic_hooks.refined implementation, including its corrected guard.
        def refined(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                force, torque, gradient, time_s=0.):
            trace["rk4_calls"] += 1
            if subdivisions == 1:
                out = original(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                    force, torque, gradient, time_s)
                trace["rk4_substeps"] += 1
                return out
            out = y
            for index in range(subdivisions):
                before_fuel = out[13]
                out = original(out, vehicle, command, available, dt/subdivisions, gravity, j2, atmosphere,
                    force, torque, gradient, time_s+index*dt/subdivisions)
                trace["rk4_substeps"] += 1
                if index < subdivisions-1 and (out[13] < 0 or before_fuel > 0 and out[13] == 0):
                    raise ValueError("numerics_subdivision_crossed_fuel_zero_unsupported")
            return out

        def refined_with_fuel_event(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                force, torque, gradient, time_s=0.):
            trace["rk4_calls"] += 1
            out = y
            for index in range(subdivisions):
                before_fuel = out[13]
                try:
                    out = original(out, vehicle, command, available, dt/subdivisions, gravity, j2, atmosphere,
                        force, torque, gradient, time_s+index*dt/subdivisions)
                except BaseException as error:
                    trace["failed_rk4_calls"] += 1
                    trace["incomplete_failure_substeps"] += subdivisions-index
                    execution._status, execution._failure_class = "failed", type(error).__name__
                    raise
                trace["rk4_substeps"] += 1
                if before_fuel <= 0 and out[13] < 0:
                    # A returned invalid endpoint is not a depletion bracket.
                    # The last inner endpoint can fail with zero uncomputed work.
                    trace["failed_rk4_calls"] += 1
                    trace["invalid_endpoint_rk4_calls"] += 1
                    trace["incomplete_failure_substeps"] += subdivisions-index-1
                    execution._status, execution._failure_class = "failed", "ValueError"
                    raise ValueError("refined_dry_start_negative_fuel_invalid")
                if before_fuel > 0 and (out[13] < 0 or index < subdivisions-1 and out[13] == 0):
                    trace["interrupted_rk4_calls"] += 1
                    trace["final_endpoint_crossings"] += int(index == subdivisions-1)
                    trace["skipped_rk4_substeps"] += subdivisions-index-1
                    raise dyn._FuelDepletionCrossing(y, vehicle, command, available, dt, time_s,
                        subdivisions=subdivisions, completed_substeps=index+1,
                        fuel_before=before_fuel, fuel_after=out[13])
            return out

        active_hook = refined if policy.fuel_event_mode == "strict_v1" else refined_with_fuel_event
        dyn._rk4 = active_hook
        try:
            yield execution
            if policy.fuel_event_mode == "strict_v1" or execution._status == "active":
                execution._status = "completed"
        except BaseException as error:
            execution._status = "failed"
            execution._failure_class = type(error).__name__
            raise
        finally:
            foreign_hook = dyn._rk4 is not active_hook
            dyn._rk4 = original
            if foreign_hook:
                execution._status = "failed"
                execution._failure_class = "ValueError"
                raise ValueError("restored_integration_hook_replaced_during_scope")
    finally:
        _HOOK_LOCK.release()


def validate_execution_receipt(value, policy, *, require_completed=True):
    """Recheck exact v1/v2 counter contracts; never install a hook or fly."""
    _require(type(policy) is RestoredIntegrationPolicy and type(require_completed) is bool,
        "restored_integration_typed_receipt_policy_required")
    row = json.loads(_wire(value))
    _require(type(row) is dict and type(row.get("trace")) is dict,
        "restored_integration_receipt_shape")
    trace = row["trace"]
    keys = {"rk4_calls", "rk4_substeps", "commands_recomputed_inside_rk4",
        "outer_control_cadence_changed", "fuel_event_search_changed", "contact_search_changed"}
    v2 = policy.fuel_event_mode == "outer_bracket_v2"
    if v2:
        keys |= {"interrupted_rk4_calls", "final_endpoint_crossings", "skipped_rk4_substeps", "failed_rk4_calls", "invalid_endpoint_rk4_calls", "incomplete_failure_substeps"}
    _require(set(trace) == keys and all(type(trace[key]) is int and trace[key] >= 0
        for key in keys-{ "outer_control_cadence_changed", "fuel_event_search_changed", "contact_search_changed"})
        and trace["rk4_calls"] > 0 and trace["commands_recomputed_inside_rk4"] == 0
        and trace["outer_control_cadence_changed"] is False and trace["contact_search_changed"] is False
        and trace["fuel_event_search_changed"] is v2, "restored_integration_receipt_trace_shape")
    if v2:
        calls, stopped = trace["rk4_calls"], trace["interrupted_rk4_calls"]
        skipped, failed = trace["skipped_rk4_substeps"], trace["failed_rk4_calls"]
        incomplete, invalid = trace["incomplete_failure_substeps"], trace["invalid_endpoint_rk4_calls"]
        primitive, final_crossings = failed-invalid, trace["final_endpoint_crossings"]
        interior = stopped-final_crossings
        _require(0 <= invalid <= failed and 0 <= final_crossings <= stopped and stopped+failed <= calls
            and interior <= skipped <= interior*(policy.subdivisions-1)
            and primitive <= incomplete <= primitive*policy.subdivisions+invalid*(policy.subdivisions-1)
            and trace["rk4_substeps"]+skipped+incomplete == calls*policy.subdivisions,
            "restored_integration_v2_counter_accounting")
        if row.get("status") == "completed":
            _require(failed == invalid == incomplete == 0, "restored_integration_failed_trial_claimed_completed")
    else:
        _require(trace["rk4_substeps"] == trace["rk4_calls"]*policy.subdivisions,
            "restored_integration_v1_complete_substeps_required")
    status = row.get("status")
    _require(status in ("completed", "failed") and (not require_completed or status == "completed")
        and ((status == "completed" and row.get("failure_class") is None)
            or (status == "failed" and type(row.get("failure_class")) is str
                and 0 < len(row["failure_class"]) <= 96)), "restored_integration_receipt_status")
    expected = {"schema": "missionos.starship_restored_integration_execution."+("v2" if v2 else "v1"),
        "policy": policy.record(), "status": status, "failure_class": row["failure_class"], "trace": trace,
        "process_isolation_required": True, "internal_rk4_only": not v2,
        "physics_limits_changed": False, "control_policy_changed": False, "experimental": True,
        "qualification_refreshed": False, "mission_authority_granted": False,
        "execution_permission_granted": False, "hosted_model_calls": 0, "nested_sixdof_forecasts": 0}
    _require(_wire(row) == _wire(expected), "restored_integration_receipt_roundtrip_changed")
    return row


def _algorithm(source, container):
    _require(type(source) is bytes, "restored_integration_source_bytes_required")
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeError, ValueError) as error:
        raise ValueError("restored_integration_source_invalid") from error
    parents = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == container]
    _require(len(parents) == 1, "restored_integration_algorithm_container_missing")
    functions = [node for node in ast.walk(parents[0])
        if isinstance(node, ast.FunctionDef) and node.name == "refined"]
    _require(len(functions) == 1, "restored_integration_algorithm_missing")
    return ast.dump(functions[0], include_attributes=False)


def _source_manifest(value, *, exact_count=None):
    _require(type(value) is dict and value, "restored_integration_source_manifest_required")
    if exact_count is not None:
        _require(len(value) == exact_count, "restored_integration_physical_source_count_changed")
    for name, digest in value.items():
        path = PurePosixPath(name) if type(name) is str else None
        _require(path is not None and not path.is_absolute() and ".." not in path.parts
            and name.startswith("src/runtime/starship") and path.suffix == ".py"
            and type(digest) is str and re.fullmatch("[0-9a-f]{64}", digest),
            "restored_integration_invalid_source_manifest")


def integration_source_equivalence(diagnostic_source, *, expected_diagnostic_sha256,
        prior_physical_sources, current_physical_sources):
    """Pure source comparison, with this wrapper's actual source read for AST.

    The host must obtain and bind both source manifests and the prior diagnostic
    bytes from separately reviewed plans. Matching supplied hashes does not
    authenticate those plans, verify the environment/profile, authorize reuse,
    rerun a numerical experiment or assert that any convergence threshold passed.
    Added modules are reported, but cannot replace any of the 187 old sources.
    """
    _require(type(expected_diagnostic_sha256) is str
        and re.fullmatch("[0-9a-f]{64}", expected_diagnostic_sha256),
        "restored_integration_diagnostic_pin_required")
    _require(type(diagnostic_source) is bytes
        and sha256(diagnostic_source).hexdigest() == expected_diagnostic_sha256,
        "restored_integration_diagnostic_source_changed")
    current_source = Path(__file__).read_bytes()
    first, second = _algorithm(diagnostic_source, "diagnostic_hooks"), _algorithm(current_source, "restored_integration")
    _require(first == second, "restored_integration_algorithm_ast_changed")
    _source_manifest(prior_physical_sources, exact_count=_PHYSICAL_SOURCE_COUNT)
    _source_manifest(current_physical_sources)
    _require(all(current_physical_sources.get(name) == digest for name, digest in prior_physical_sources.items()),
        "restored_integration_physical_source_changed")
    return {"schema": "missionos.starship_restored_integration_source_equivalence.v1",
        "diagnostic_sha256": expected_diagnostic_sha256,
        "integration_source_sha256": sha256(current_source).hexdigest(),
        "algorithm_ast_sha256": sha256(first.encode()).hexdigest(), "algorithm_ast_identical": True,
        "prior_physical_sources_sha256": sha256(_wire(prior_physical_sources)).hexdigest(),
        "physical_source_files_unchanged": _PHYSICAL_SOURCE_COUNT,
        "additional_runtime_source_files": len(set(current_physical_sources)-set(prior_physical_sources)),
        "profile_or_environment_equivalence_checked": False, "prior_record_validated": False,
        "evidence_reuse_authorized": False, "numerical_gate_passed": False,
        "qualification_refreshed": False, "mission_authority_granted": False}
