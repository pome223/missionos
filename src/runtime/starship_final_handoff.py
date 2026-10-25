"""One-use transfer of the yielded actual return plant, before controller start.

This is an opt-in ownership boundary, not another state loader, authorization,
completed v1 flight, or authenticated worker receipt. Only the held production
actor can create the sealed token. The final host/verifier must independently
bind the saved dispatch proof and later continuation to the external transcript.
"""
from dataclasses import InitVar, asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re


SOURCE = "src/runtime/starship_final_handoff.py"
_OWNER_SEAL = object()


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_wire(value)).hexdigest()


@dataclass(frozen=True)
class FinalActualReturnHandoffPolicy:
    """Frozen opt-in; the outer registration still owns execution permission."""

    plan_sha256: str
    expected_source_sha256: InitVar[dict]
    _source_json: bytes = field(init=False, repr=False)

    def __post_init__(self, expected_source_sha256):
        from .starship_restored_contract import _manifest
        _manifest(expected_source_sha256)
        _require(type(self.plan_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", self.plan_sha256),
            "final_handoff_plan_hash_required")
        root = Path(__file__).resolve().parents[2]
        for name in (SOURCE, "src/runtime/starship_restored_return.py", "src/runtime/starship_restored_coordinator.py"):
            _require(expected_source_sha256.get(name) == sha256((root/name).read_bytes()).hexdigest(),
                "final_handoff_explicit_current_source_opt_in_required")
        object.__setattr__(self, "_source_json", _wire(expected_source_sha256))

    def source_manifest(self):
        return json.loads(self._source_json)

    def record(self):
        return {"schema": "missionos.starship_final_actual_handoff_policy.v1",
            "plan_sha256": self.plan_sha256, "source_sha256": sha256(self._source_json).hexdigest(),
            "phase": "orbital_coast", "before_return_controller_start": True,
            "old_actor_must_stop_permanently": True, "execution_permission_granted": False}

    def validate_binding(self, source, plan_sha256):
        _require(_wire(source) == self._source_json and plan_sha256 == self.plan_sha256,
            "final_handoff_policy_source_or_plan_changed")


class FinalActualReturnHandoff:
    """Sealed actual references, consumed once; JSON evidence cannot create one."""

    __slots__ = ("_request", "_state", "_vehicle", "_record_json", "_consumed")

    def __init__(self, *, _seal=None, request=None, state=None, vehicle=None, record=None):
        _require(_seal is _OWNER_SEAL, "final_handoff_requires_live_actual_owner_not_saved_record")
        self._request, self._state, self._vehicle = request, state, vehicle
        self._record_json, self._consumed = _wire(record), False

    @property
    def origin(self):
        """Typed pre-dispatch origin; the due state lives in record()['snapshot']."""
        return self._request.origin

    @property
    def request(self):
        return self._request

    def record(self):
        return json.loads(self._record_json)

    @property
    def consumed(self):
        return self._consumed

    def consume(self):
        """Transfer exact held objects; never deserialize or advance a state."""
        _require(self._consumed is False, "final_handoff_already_consumed")
        snapshot = json.loads(self._record_json)["snapshot"]
        _require(_wire(asdict(self._state)) == _wire(snapshot["state"])
            and _wire(asdict(self._vehicle)) == _wire(snapshot["vehicle"]),
            "final_handoff_actual_objects_changed_before_consumption")
        self._consumed = True
        return self._state, self._vehicle


class _AcceptedActualDispatch:
    """Internal typed proof from a freshly admitted numerical candidate."""

    __slots__ = ("_record_json",)

    def __init__(self, *, _seal=None, record=None):
        _require(_seal is _OWNER_SEAL, "final_handoff_dispatch_requires_coordinator_not_json")
        self._record_json = _wire(record)

    def record(self):
        return json.loads(self._record_json)


def _accepted_actual_dispatch(policy, *, candidate, admission, snapshot):
    from .starship_restored_admission import RestoredCandidate
    _require(type(policy) is FinalActualReturnHandoffPolicy and type(candidate) is RestoredCandidate,
        "final_handoff_typed_admitted_candidate_required")
    row = candidate.record()
    binding, proposal = snapshot["binding"], row["proposal"]
    _require(proposal["source_sha256"] == policy.record()["source_sha256"]
        and all(proposal[key] == binding[key] for key in ("candidate_id", "return_time_s", "profile_sha256",
            "inventory_receipt_chain_sha256", "retained_count")), "final_handoff_candidate_binding_changed")
    return _AcceptedActualDispatch(_seal=_OWNER_SEAL, record={"actual_snapshot": snapshot,
        "candidate": row, "admission": admission, "gate_met": True, "refusal_reason": None})


def _capture_actual_owner(*, policy, request, actual_provider, published_snapshot, dispatch, coast_history,
                          initial_binding, reservation_changes):
    """Private actor entry: re-read the yielded closure, never accept plant JSON."""
    from . import starship_sixdof as dyn
    from .starship_restored_return import RestoredReturnRequest
    _require(type(policy) is FinalActualReturnHandoffPolicy and type(request) is RestoredReturnRequest
        and callable(actual_provider) and type(dispatch) is _AcceptedActualDispatch,
        "final_handoff_exact_policy_request_and_actual_owner_required")
    dispatch = dispatch.record()
    policy.validate_binding(request.origin.record()["source_sha256"], policy.plan_sha256)
    actual = actual_provider()
    _require(type(actual) is tuple and len(actual) == 3, "final_handoff_actual_owner_tuple_required")
    state, vehicle, controller = actual
    _require(type(state) is dyn.State6DOF and type(vehicle) is dyn.Vehicle6DOF and controller is None,
        "final_handoff_requires_actual_precontroller_plant")
    snapshot = json.loads(_wire(published_snapshot))
    binding, origin = request.record(), request.origin.record()
    _require(set(snapshot) == {"state", "vehicle", "phase", "binding", "return_controller", "completed"}
        and snapshot["phase"] == "orbital_coast" and snapshot["return_controller"] is None
        and snapshot["completed"] is False and _wire(snapshot["state"]) == _wire(asdict(state))
        and _wire(snapshot["vehicle"]) == _wire(asdict(vehicle))
        and _wire(snapshot["vehicle"]) == _wire(origin["vehicle"])
        and _wire(snapshot["binding"]) == _wire(binding)
        and 0 <= state.time_s-binding["return_time_s"] <= binding["maximum_start_clock_slip_s"]+1e-7,
        "final_handoff_not_exact_first_due_actual_snapshot")
    _require(type(dispatch) is dict and set(dispatch) == {"actual_snapshot", "candidate", "admission", "gate_met", "refusal_reason"}
        and dispatch["gate_met"] is True and dispatch["refusal_reason"] is None
        and _wire(dispatch["actual_snapshot"]) == _wire(snapshot), "final_handoff_accepted_actual_dispatch_required")
    candidate, admission = dispatch["candidate"], dispatch["admission"]
    _require(type(candidate) is dict and type(admission) is dict
        and admission.get("schema") == "missionos.starship_restored_candidate_admission.v1"
        and admission.get("accepted") is True and admission.get("stage") == "dispatch"
        and admission.get("reasons") == [] and admission.get("time_s") == state.time_s
        and admission.get("candidate_sha256") == candidate.get("candidate_sha256")
        and admission.get("proposal_sha256") == candidate.get("proposal_sha256")
        and candidate.get("proposal", {}).get("candidate_id") == binding["candidate_id"]
        and candidate["proposal"].get("return_time_s") == binding["return_time_s"],
        "final_handoff_dispatch_proof_or_selected_opportunity_changed")
    dyn._validate_pair(state, vehicle, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in vehicle.engines), state.flap_angles_rad))
    history = json.loads(_wire(coast_history))
    _require(type(history) is list and history and history[-1] == {
        "state": snapshot["state"], "retained_count": origin["retained_count"]},
        "final_handoff_current_actual_coast_history_required")
    initial, changes = json.loads(_wire(initial_binding)), json.loads(_wire(reservation_changes))
    _require(type(initial) is dict and type(changes) is list and len(changes) <= 1,
        "final_handoff_actual_reservation_history_required")
    current = initial
    for change in changes:
        _require(type(change) is dict and set(change) == {"time_s", "state", "from_binding", "to_binding",
                "actual_state_assigned", "dispatch_authorized"}
            and _wire(change["from_binding"]) == _wire(current)
            and type(change["state"]) is dict and set(change["state"]) == set(snapshot["state"])
            and change["time_s"] == change["state"]["time_s"] < state.time_s
            and change["actual_state_assigned"] is change["dispatch_authorized"] is False,
            "final_handoff_reservation_change_not_actual_or_contiguous")
        current = change["to_binding"]
    _require(_wire(current) == _wire(binding), "final_handoff_reservation_history_lost_selected_binding")
    source_record = json.loads(request.origin.source_record_bytes())
    record = {"schema": "missionos.starship_final_actual_dispatch_handoff.v1", "policy": policy.record(),
        "origin": origin, "source_record_sha256": request.origin.source_record_sha256,
        "inventory": source_record["inventory"], "snapshot": snapshot, "coast_history": history,
        "initial_binding": initial, "reservation_changes": changes,
        "dispatch": dispatch, "old_actor_suspended_permanently": True,
        "actual_state_assigned": False, "plant_advanced": False, "return_controller_started": False,
        "forecast_state_used": False, "completed_flight": False,
        "source_provenance_authenticated": False, "host_transcript_binding_required": True,
        "old_certificate_admitted": False, "qualification_refreshed": False,
        "mission_authority_granted": False, "dispatch_authorized": False, "physical_execution": False}
    record["handoff_sha256"] = _digest(record)
    return FinalActualReturnHandoff(_seal=_OWNER_SEAL, request=request, state=state, vehicle=vehicle, record=record)
