"""Host-only inherited judgment/approval plumbing for a registered restored run.

This boundary reconstructs the actual closed origin and observes worker JSON;
it never integrates, installs forecast state, calls an operator automatically,
or gives signing keys/provider credentials to a worker. A recorded source pin
is not an authenticated flight attestation. Fixture agents remain explicitly
synthetic; live agents are instantiated only by the inherited DecisionHost.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import time

from . import starship_sixdof as dyn
from . import starship_restored_contract as contract
from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, MAX_FRAME_BYTES, _context
from .starship_restored_approval import RestoredApprovalBinding, RestoredApprovalHost
from .starship_restored_decision_pipe import RestoredDecisionHost
from .starship_restored_integration import RestoredIntegrationPolicy
from .starship_restored_return import RestoredReturnRequest

SCHEMA = "missionos.starship_restored_native_host.v1"
READY_SCHEMA = "missionos.starship_restored_origin_ready.v1"
ACK_SCHEMA = "missionos.starship_restored_origin_ready_ack.v1"
TELEMETRY_SCHEMA = "missionos.starship_restored_worker_telemetry.v1"
MAX_ORIGIN_BYTES = 32*2**20
MAXIMUM_OBSERVATION_AGE_S = 5.


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return sha256(_wire(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _full_state(value):
    def finite(item):
        return type(item) in (int, float) and math.isfinite(item)
    _require(type(value) is dict and set(value) == contract.STATE_KEYS, "restored_host_full_actual_state_required")
    for key, count in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4), ("omega_body_rad_s", 3)):
        _require(type(value[key]) is list and len(value[key]) == count and all(finite(x) for x in value[key]),
            "restored_host_nonfinite_or_untyped_actual_vector")
    _require(abs(sum(x*x for x in value["q_body_to_eci"])-1.) <= 1e-8
        and type(value["engine_states"]) is list and type(value["flap_angles_rad"]) is list
        and all(finite(x) for x in value["flap_angles_rad"]), "restored_host_actual_attitude_or_actuators_invalid")
    for engine in value["engine_states"]:
        _require(type(engine) is dict and set(engine) == {"throttle", "gimbal_x_rad", "gimbal_y_rad", "available"}
            and type(engine["available"]) is bool and all(finite(engine[key]) for key in
                ("throttle", "gimbal_x_rad", "gimbal_y_rad")), "restored_host_actual_engine_values_invalid")


def _read_artifact(directory, relative, maximum_bytes):
    """Regular-file read through no-follow FDs, including root ancestors."""
    _require(type(relative) is str and type(maximum_bytes) is int and 0 < maximum_bytes <= MAX_ORIGIN_BYTES,
        "restored_host_invalid_artifact_reference")
    path = PurePosixPath(relative)
    _require(relative == path.as_posix() and not path.is_absolute()
        and path.parts and all(part not in (".", "..", "") for part in path.parts)
        and "\\" not in relative, "restored_host_unsafe_artifact_path")
    directory = Path(directory).absolute()
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(directory.anchor, flags | os.O_DIRECTORY)
    try:
        for part in (*directory.parts[1:], *path.parts[:-1]):
            _require(part not in (".", ".."), "restored_host_unsafe_artifact_path")
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.parts[-1], flags, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            _require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum_bytes,
                "restored_host_artifact_size_or_type_invalid")
            chunks, size = [], 0
            while True:
                chunk = os.read(file_fd, min(1024*1024, maximum_bytes+1-size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                _require(size <= maximum_bytes, "restored_host_artifact_size_or_type_invalid")
            raw, after = b"".join(chunks), os.fstat(file_fd)
            _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) and len(raw) == before.st_size,
                "restored_host_artifact_changed_or_hash_mismatch")
            return raw
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)


def read_origin_artifact(directory, relative, *, expected_sha256, maximum_bytes=MAX_ORIGIN_BYTES):
    """Bounded hash-pinned no-follow read; no content is returned on mismatch."""
    _require(contract._hash(expected_sha256), "restored_host_invalid_artifact_reference")
    raw = _read_artifact(directory, relative, maximum_bytes)
    _require(sha256(raw).hexdigest() == expected_sha256, "restored_host_artifact_changed_or_hash_mismatch")
    return raw


class RestoredNativeHost:
    """Host pump. The outer runner owns Gateway lifetime and pipe endpoints.

    The ledger is created only after independently checked READY and the first
    exact source-bound actual telemetry snapshot. ``tick`` drains telemetry
    before the approval bridge, preserving the original worker observation
    triple in bounded host-known history. It never refreshes worker wall time.
    """

    def __init__(self, *, context, plan_sha256, expected_source_sha256, profile_raw_bytes,
            integration_policy, mode, governance_dir, ledger_path, signing_key, operator_token, operator_id,
            decision_request_reader, decision_response_writer, ready_reader, ack_writer,
            approval_request_reader, approval_grant_writer, telemetry_reader, service,
            agent_factory=None, active=lambda: True, clock=time.monotonic, environment=None,
            return_review_policy=None):
        _context(context)
        contract._manifest(expected_source_sha256)
        if return_review_policy is not None:
            from .starship_restored_return_review import validate_policy
            return_review_policy = validate_policy(return_review_policy)
            _require(expected_source_sha256.get("src/runtime/starship_restored_return_review.py")
                == sha256(Path(__file__).with_name("starship_restored_return_review.py").read_bytes()).hexdigest(),
                "restored_host_return_review_source_opt_in_required")
        _require(contract._hash(plan_sha256) and type(profile_raw_bytes) is bytes
            and sha256(profile_raw_bytes).hexdigest() == contract.PROFILE_SHA256
            and type(integration_policy) is RestoredIntegrationPolicy and integration_policy.subdivisions == 2
            and mode in ("fixture", "live") and callable(active) and callable(clock)
            and "src/runtime/starship_restored_native_host.py" in expected_source_sha256,
            "restored_host_source_profile_or_policy_required")
        _require((mode == "fixture" and callable(agent_factory)) or (mode == "live" and agent_factory is None),
            "restored_host_provider_mode_must_be_explicit")
        readers = (decision_request_reader, ready_reader, approval_request_reader, telemetry_reader)
        writers = (decision_response_writer, ack_writer, approval_grant_writer)
        _require(all(isinstance(channel, ApprovalPipeReader) for channel in readers)
            and all(isinstance(channel, ApprovalPipeWriter) for channel in writers),
            "restored_host_inherited_channels_required")
        _require(len({(os.fstat(channel.fd).st_dev, os.fstat(channel.fd).st_ino)
            for channel in (*readers, *writers)}) == 7, "restored_host_channels_must_be_distinct")
        self.context, self.plan, self.source = deepcopy(context), plan_sha256, json.loads(_wire(expected_source_sha256))
        self.return_review_policy = deepcopy(return_review_policy)
        self.profile, self.policy, self.mode = profile_raw_bytes, integration_policy, mode
        self.directory, self.ledger_path = Path(governance_dir).absolute(), Path(ledger_path).absolute()
        _require(not self.ledger_path.exists() and not self.ledger_path.is_symlink(), "restored_host_new_ledger_required")
        self._signing_key, self._operator_token, self._operator_id = signing_key, operator_token, operator_id
        self.service, self.clock, self.active = service, clock, active
        self.ready_reader, self.ack_writer = ready_reader, ack_writer
        self.approval_reader, self.grant_writer, self.telemetry_reader = approval_request_reader, approval_grant_writer, telemetry_reader
        self.origin = self.binding = self.initial_request = self.approval_host = None
        self.ready = self.ack = self._state = None
        self._known, self.telemetry, self.events = [], [], []
        self._sequence, self._revision, self.closed, self.failed = 0, 0, False, False
        self._revised_request, self._revised_origin_sha = None, None
        self.environment = None if environment is None else json.loads(_wire(environment))
        self.decision_host = RestoredDecisionHost(context=context, plan_sha256=plan_sha256,
            source_sha256=_hash(self.source), reader=decision_request_reader, writer=decision_response_writer,
            agent_factory=agent_factory, active=self._active, clock=clock,
            **({"return_review_policy": return_review_policy} if return_review_policy is not None else {}))

    def _active(self):
        return not self.closed and not self.failed and self.active() is True

    def _ready(self, frame):
        _require(self.ready is None and type(frame) is dict and contract._hash(frame.get("nonce"))
            and _number(frame.get("started_wall_s")) and frame["started_wall_s"] <= self.clock()
            and frame.get("origin_file") == "closed-origin.json", "restored_host_invalid_or_replayed_ready")
        raw = read_origin_artifact(self.directory, frame["origin_file"], expected_sha256=frame.get("source_record_sha256"))
        origin = contract.ActualRestoredOrigin(frame["source_record_sha256"], raw, self.profile, self.source)
        anchor = contract.RestoredCandidateSchedule(origin)
        initial = RestoredReturnRequest(origin, anchor, "nominal", self.profile, self.source)
        binding = RestoredApprovalBinding(origin, context=self.context, plan_sha256=self.plan,
            expected_source_sha256=self.source, started_wall_s=frame["started_wall_s"])
        expected = {"schema": READY_SCHEMA, "context": self.context, "plan_sha256": self.plan,
            "source_sha256": _hash(self.source), "profile_sha256": contract.PROFILE_SHA256,
            "origin_file": "closed-origin.json", "source_record_sha256": origin.source_record_sha256,
            "origin_sha256": _hash(origin.record()), "binding_sha256": _hash(binding.record()),
            "opportunity_anchor_sha256": _hash(anchor.opportunity_anchor_record()),
            "initial_reservation_sha256": _hash(initial.record()), "integration_policy_sha256": _hash(self.policy.record()),
            "started_wall_s": frame["started_wall_s"], "nonce": frame["nonce"]}
        _require(_wire(frame) == _wire(expected), "restored_host_ready_binding_changed")
        ready_raw = read_origin_artifact(self.directory, "origin-ready.json", expected_sha256=_hash(frame), maximum_bytes=MAX_FRAME_BYTES)
        _require(_wire(contract._read(ready_raw)) == _wire(frame), "restored_host_ready_artifact_differs")
        self.origin, self.initial_request, self.binding, self.ready = origin, initial, binding, deepcopy(frame)

    def _reservation(self, frame, state):
        bound, revision = self.binding.record(), state["revision"]
        _require(type(revision) is int and self._revision <= revision <= 1
            and _wire(state["scope"]) == _wire(bound["initial_scope"] if revision == 0 else bound["registry"]),
            "restored_host_telemetry_scope_or_revision_changed")
        if revision == 0:
            return self.initial_request.record()
        _require(self.approval_host is not None and self.approval_host.bridge.result is not None
            and self.approval_host.bridge.result.get("consumption", {}).get("approved") is True,
            "restored_host_revision_without_approved_consumption")
        if self._revised_request is None:
            # Binding digest authenticates the reconstructed request, including
            # its full actual-origin file hash; it is not permission to execute.
            raw = _read_artifact(self.directory, "reservation-change-origin.json", MAX_ORIGIN_BYTES)
            token = sha256(raw).hexdigest()
            current = contract.ActualRestoredOrigin(token, raw, self.profile, self.source)
            before, after = self.origin.record(), current.record()
            _require(after["vehicle"] == before["vehicle"] and after["schedule"] == before["schedule"]
                and after["inventory_receipt_chain_sha256"] == before["inventory_receipt_chain_sha256"]
                and current.time_s <= frame["actual_state"]["time_s"]
                and any(_hash(row["frame"]["actual_state"]) == _hash(after["state"]) for row in self.telemetry),
                "restored_host_revised_origin_not_observed_actual_family")
            self._revised_request = RestoredReturnRequest(current,
                contract.RestoredCandidateSchedule(current, anchor=contract.RestoredCandidateSchedule(self.origin)),
                "next_orbit", self.profile, self.source).record()
            self._revised_origin_sha = token
        return self._revised_request

    def _telemetry(self, frame):
        _require(self.origin is not None and type(frame) is dict and set(frame) == {
            "schema", "context", "plan_sha256", "source_sha256", "sequence", "runtime_state", "actual_state", "phase",
            "vehicle_sha256", "binding_sha256", "return_controller_started", "integration_policy_sha256", "state_assigned"}
            and frame["schema"] == TELEMETRY_SCHEMA and _wire(frame["context"]) == _wire(self.context)
            and frame["plan_sha256"] == self.plan and frame["source_sha256"] == _hash(self.source)
            and type(frame["sequence"]) is int and frame["sequence"] == self._sequence+1
            and frame["integration_policy_sha256"] == _hash(self.policy.record()) and frame["state_assigned"] is False,
            "restored_host_telemetry_source_sequence_or_schema_changed")
        state, actual, now = frame["runtime_state"], frame["actual_state"], self.clock()
        _require(type(state) is dict and set(state) == {"run_active", "simulation_time_s", "restored_origin_sha256",
                "revision", "scope", "reservation_sha256", "observation"}
            and type(state["run_active"]) is bool and state["restored_origin_sha256"] == self.binding.record()["restored_origin_sha256"]
            and type(actual) is dict and set(actual) == contract.STATE_KEYS
            and _number(actual["time_s"]) and self.origin.time_s <= actual["time_s"] <= 16000.
            and state["simulation_time_s"] == actual["time_s"] and type(state["simulation_time_s"]) in (int, float),
            "restored_host_actual_snapshot_clock_or_origin_changed")
        _full_state(actual)
        observation = state["observation"]
        _require(type(observation) is dict and set(observation) == {"state_sha256", "simulation_time_s", "wall_time_s"}
            and observation["state_sha256"] == _hash(actual) and observation["simulation_time_s"] == actual["time_s"]
            and _number(observation["wall_time_s"]) and 0 <= now-observation["wall_time_s"] <= MAXIMUM_OBSERVATION_AGE_S,
            "restored_host_worker_observation_spoofed_future_or_stale")
        original = self.origin.record()
        craft, decoded = dyn.vehicle_from_dict(original["vehicle"]), dyn.state_from_dict(actual)
        _require(all(type(engine.get("available")) is bool for engine in actual["engine_states"])
            and [item["available"] for item in actual["engine_states"]] == [item["available"] for item in original["state"]["engine_states"]]
            and _number(actual["propellant_kg"]) and actual["propellant_kg"] <= original["state"]["propellant_kg"]+1e-6
            and type(frame["phase"]) is str and 0 < len(frame["phase"]) <= 100
            and type(frame["return_controller_started"]) is bool
            and (frame["return_controller_started"] or frame["phase"] == "orbital_coast")
            and frame["vehicle_sha256"] == _hash(original["vehicle"]), "restored_host_vehicle_availability_or_phase_changed")
        dyn._validate_pair(decoded, craft, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), decoded.flap_angles_rad))
        if self._state is None:
            _require(_wire(actual) == _wire(original["state"]) and state["run_active"] is True
                and frame["phase"] == "orbital_coast" and frame["return_controller_started"] is False,
                "restored_host_first_telemetry_not_exact_closed_origin")
        else:
            prior = self.telemetry[-1]["frame"]["actual_state"]
            _require(actual["time_s"] >= prior["time_s"] and actual["propellant_kg"] <= prior["propellant_kg"]+1e-6
                and (actual["time_s"] != prior["time_s"] or _wire(actual) == _wire(prior)),
                "restored_host_actual_clock_fuel_or_same_time_reset")
        reservation = self._reservation(frame, state)
        _require(state["reservation_sha256"] == frame["binding_sha256"] == _hash(reservation),
            "restored_host_telemetry_reservation_binding_changed")
        self._state, self._sequence, self._revision = deepcopy(state), frame["sequence"], state["revision"]
        self._known.append(deepcopy(observation))
        self._known = self._known[-64:]
        self.telemetry.append({"frame": deepcopy(frame), "received_wall_s": now})

    def snapshot(self):
        _require(self._state is not None, "restored_host_actual_telemetry_not_ready")
        state = deepcopy(self._state)
        state["known_observations"] = deepcopy(self._known)
        if not self._active():
            state["run_active"] = False
        return state

    def tick(self):
        _require(not self.closed and not self.failed, "restored_host_closed_or_failed")
        try:
            self.ack_writer.flush()
            frame = self.ready_reader.poll()
            if frame is not None:
                self._ready(frame)
            while self.origin is not None:
                frame = self.telemetry_reader.poll()
                if frame is None:
                    break
                self._telemetry(frame)
            if self.ready is not None and self._state is not None and self.approval_host is None:
                self.approval_host = RestoredApprovalHost(self.binding, ledger_path=self.ledger_path,
                    signing_key=self._signing_key, operator_token=self._operator_token, operator_id=self._operator_id,
                    initial_reservation_sha256=_hash(self.initial_request.record()), snapshot=self.snapshot,
                    request_reader=self.approval_reader, grant_writer=self.grant_writer, service=self.service, clock=self.clock)
                self.ack = {"schema": ACK_SCHEMA, "ready_sha256": _hash(self.ready), "nonce": self.ready["nonce"],
                    "context": self.context, "plan_sha256": self.plan, "source_sha256": _hash(self.source),
                    "binding_sha256": self.ready["binding_sha256"],
                    "initial_reservation_sha256": self.ready["initial_reservation_sha256"], "ledger_ready": True}
                self.ack_writer.send(self.ack)
            self.decision_host.tick()
            # All original worker triples have been observed and stored above.
            if self.approval_host is not None:
                self.approval_host.tick()
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            self.failed = True
            self.events.append({"event": "native_host_boundary_rejected", "exception_type": type(error).__name__})
            raise

    def record(self):
        ledger = []
        if self.approval_host is not None and self.approval_host.bridge.request is not None:
            ledger.append(self.approval_host.ledger.status(self.approval_host.bridge.request["request_id"]))
        value = {"schema": SCHEMA, "context": deepcopy(self.context), "plan_sha256": self.plan,
            "source_sha256": _hash(self.source), "mode": self.mode, "integration_policy": self.policy.record(),
            "handshake": {"ready": deepcopy(self.ready), "ack": deepcopy(self.ack)},
            "decision_host": self.decision_host.record(), "approval_host": None if self.approval_host is None else self.approval_host.record(),
            "operator_ledger": ledger, "telemetry": deepcopy(self.telemetry), "events": deepcopy(self.events)}
        if self.environment is not None:
            value["environment"] = deepcopy(self.environment)
        if self.return_review_policy is not None:
            from .starship_restored_return_review import policy_digest
            value.update(return_review_policy=deepcopy(self.return_review_policy),
                return_review_policy_sha256=policy_digest(self.return_review_policy))
        return json.loads(_wire(value))

    def close(self):
        if self.approval_host is not None:
            self.approval_host.close()
        self.closed = True
