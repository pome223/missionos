"""Source-bound reservation amendments for the restored experimental mission.

The typed closed origin is evidence, not qualification or approval. This adapter
only narrows the existing host ledger / inherited pipe protocol to that origin.
The caller still supplies fresh numerical admission and reservation callbacks;
neither a human response nor an amendment dispatches a burn. Keys and bearer
tokens belong exclusively to the host. No old mission clock or certificate is
used here.
"""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import time

from .starship_inflight_approval import InflightApprovalError, InflightApprovalLedger, digest
from .starship_inflight_runtime import (
    HostApprovalBridge, WorkerApprovalActor, _context, _hash,
)
from .starship_restored_contract import ActualRestoredOrigin, RestoredCandidateSchedule

SCHEMA = "missionos.starship_restored_approval_binding.v1"
MAXIMUM_RETURN_DELAY_S = 6000.0
MAXIMUM_REQUEST_WINDOW_S = 1200.0
NOMINAL_RESERVATION_GUARD_S = 60.0
REQUEST_COMPLETION_RESERVE_S = 15.0
MAXIMUM_OBSERVATION_AGE_S = 5.0


def _require(condition, reason):
    if not condition:
        raise InflightApprovalError(reason)


def _copy(value):
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@dataclass(frozen=True, init=False)
class RestoredApprovalBinding:
    """Immutable, reconstructed from a typed origin and host-frozen manifest.

    The manifest is compared with the origin's independently supplied expected
    manifest, not accepted merely because a caller has hashed a JSON document.
    This does not authenticate an outer worker receipt or prove flight dynamics.
    """

    _wire: bytes = field(repr=False)

    def __init__(self, origin, *, context, plan_sha256, expected_source_sha256, started_wall_s):
        _require(type(origin) is ActualRestoredOrigin, "restored_typed_closed_origin_required")
        _context(context)
        _require(_hash(plan_sha256) and type(expected_source_sha256) is dict
            and _number(started_wall_s), "restored_invalid_approval_binding_input")
        actual = origin.record()
        _require(_copy(expected_source_sha256) == actual["source_sha256"],
            "restored_approval_source_manifest_mismatch")
        schedule = RestoredCandidateSchedule(origin).record()
        nominal, next_orbit = schedule["nominal_s"], schedule["next_orbit_s"]
        _require(0 < next_orbit-nominal <= MAXIMUM_RETURN_DELAY_S,
            "restored_next_orbit_exceeds_approval_delay_limit")
        cutoff = actual["schedule"]["final_gate_time_s"]
        reservation_cutoff = nominal-NOMINAL_RESERVATION_GUARD_S
        deadline = min(origin.time_s+MAXIMUM_REQUEST_WINDOW_S,
            reservation_cutoff-REQUEST_COMPLETION_RESERVE_S)
        _require(origin.time_s < deadline, "restored_approval_window_closed")
        scope = {"commands": ["select_nominal", "keep_reserved_return"],
            "resource_limits": {"return_delay_s": MAXIMUM_RETURN_DELAY_S}}
        registry = {"commands": scope["commands"]+["select_next_orbit"],
            "resource_limits": scope["resource_limits"]}
        value = {"schema": SCHEMA, "context": _copy(context), "plan_sha256": plan_sha256,
            "source_sha256": actual["source_sha256"], "profile_sha256": actual["profile_sha256"],
            "restored_origin_sha256": digest(actual),
            "source_record_sha256": actual["source_record_sha256"],
            "inventory_receipt_chain_sha256": actual["inventory_receipt_chain_sha256"],
            "release_generation": actual["release_generation"],
            "retained_count": actual["retained_count"], "released_count": actual["released_count"],
            "actual_cutoff_time_s": cutoff, "closed_origin_time_s": origin.time_s,
            "nominal_burn_time_s": nominal, "next_orbit_burn_time_s": next_orbit,
            "started_wall_s": started_wall_s, "initial_revision": 0,
            "initial_scope": scope, "registry": registry,
            "bounds": {"simulation_deadline_s": deadline,
                "wall_deadline_s": started_wall_s+MAXIMUM_REQUEST_WINDOW_S,
                "maximum_observation_age_s": MAXIMUM_OBSERVATION_AGE_S,
                "reservation_cutoff_s": reservation_cutoff},
            "maximum_amendment_requests": 1,
            "maximum_return_delay_s": MAXIMUM_RETURN_DELAY_S,
            "nominal_reservation_guard_s": NOMINAL_RESERVATION_GUARD_S,
            "request_completion_reserve_s": REQUEST_COMPLETION_RESERVE_S,
            "old_certificate_admitted": False, "qualification_refreshed": False,
            "physical_execution_authorized": False,
            "source_provenance_is_not_authenticated_host_receipt": True}
        object.__setattr__(self, "_wire", json.dumps(value, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode())

    def record(self):
        return json.loads(self._wire)


def binding(origin, **kwargs):
    """Detached JSON binding for evidence; runtime constructors need the type."""
    return RestoredApprovalBinding(origin, **kwargs).record()


def validate_binding(value, origin, **kwargs):
    """Return a trusted handle only after exact host-side reconstruction."""
    expected = RestoredApprovalBinding(origin, **kwargs)
    _require(type(value) is dict and _copy(value) == expected.record(),
        "restored_approval_binding_mismatch")
    return expected


def _handle(value):
    _require(type(value) is RestoredApprovalBinding, "restored_typed_approval_binding_required")
    return value.record()


def _check_state(state, bound, initial_reservation_sha256):
    _require(type(state) is dict
        and state.get("restored_origin_sha256") == bound["restored_origin_sha256"]
        and _number(state.get("simulation_time_s"))
        and state["simulation_time_s"] >= bound["closed_origin_time_s"],
        "restored_approval_state_origin_mismatch")
    revision = state.get("revision")
    _require(type(revision) is int and revision in (0, 1)
        and state.get("scope") == (bound["initial_scope"] if revision == 0 else bound["registry"]),
        "restored_approval_state_scope_mismatch")
    _require(revision != 0 or state.get("reservation_sha256") == initial_reservation_sha256,
        "restored_initial_reservation_changed")
    return state


class RestoredApprovalHost:
    """Host-only local operator route, ledger and nonblocking pipe bridge.

    The caller owns the pipe endpoints and clock. A new exclusive ledger is
    required; this scope does not implement crash resume or reissue requests.
    The snapshot callback must be host-observed telemetry, never model output.
    """

    def __init__(self, binding, *, ledger_path, signing_key, operator_token, operator_id,
                 initial_reservation_sha256, snapshot, request_reader, grant_writer,
                 service, event=lambda *_a, **_kw: None, clock=time.monotonic):
        bound = _handle(binding)
        _require(_hash(initial_reservation_sha256), "restored_initial_reservation_hash_required")
        _require(not Path(ledger_path).exists(), "restored_approval_ledger_must_be_new")
        self.binding, self.service = binding, service
        self._state = lambda: _check_state(snapshot(), bound, initial_reservation_sha256)
        _check_state(self._state(), bound, initial_reservation_sha256)
        self.ledger = InflightApprovalLedger(ledger_path, signing_key=signing_key,
            operator_token=operator_token, operator_id=operator_id, registry=bound["registry"], clock=clock)
        self.bridge = HostApprovalBridge(ledger=self.ledger, context=bound["context"],
            snapshot=self._state, request_reader=request_reader, grant_writer=grant_writer,
            bounds=bound["bounds"], event=event, clock=clock)
        service.register_run(**bound["context"], ledger=self.ledger, snapshot=self.bridge.snapshot)
        self.closed = False

    def tick(self):
        _require(not self.closed, "restored_approval_host_closed")
        return self.bridge.tick()

    def record(self):
        return {"schema": "missionos.starship_restored_approval_host.v1",
            "binding": self.binding.record(), "runtime": self.bridge.record(),
            "model_calls": 0, "burn_dispatch_issued": False, "physical_return_verified": False}

    def close(self):
        if not self.closed:
            self.service.unregister_run(self.binding.record()["context"]["run_id"])
            self.closed = True


class RestoredApprovalWorker(WorkerApprovalActor):
    """Existing one-use actor with restored origin and initial reservation pins."""

    def __init__(self, binding, *, initial_reservation_sha256, request_writer, grant_reader,
                 admission, command, event=lambda *_a, **_kw: None, clock=time.monotonic):
        bound = _handle(binding)
        _require(_hash(initial_reservation_sha256), "restored_initial_reservation_hash_required")
        self.binding, self._bound = binding, bound
        self._initial_reservation_sha256 = initial_reservation_sha256
        super().__init__(context=bound["context"], scope=bound["initial_scope"], revision=0,
            request_writer=request_writer, grant_reader=grant_reader, bounds=bound["bounds"],
            admission=admission, command=command, event=event, clock=clock)

    def request_change(self, *, request_id, candidate_sha256, state, fallback="keep_reserved_return"):
        _check_state(state, self._bound, self._initial_reservation_sha256)
        return super().request_change(request_id=request_id, candidate_sha256=candidate_sha256,
            state=state, fallback=fallback)

    def tick(self, state):
        _check_state(state, self._bound, self._initial_reservation_sha256)
        return super().tick(state)

    def confirm(self, state):
        _check_state(state, self._bound, self._initial_reservation_sha256)
        return super().confirm(state)

    def record(self):
        return super().record() | {"restored_binding": self.binding.record()}
