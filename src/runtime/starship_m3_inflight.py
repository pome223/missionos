"""Host-owned authenticated scope amendment for registered M3 scenarios.

Only the three FIFO endpoints and a source/plan-bound contract cross the worker
boundary. Operator credentials and ledger signing keys remain in the Gateway.
"""
from collections import deque
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from types import MappingProxyType

from .starship_artifacts import write_verified_input
from .starship_replanning import digest
from .starship_operational_trials import SCOPE_CLOSURE_CASE

CASE = "inflight_op_trial_02"
BASE_CASE = "op_trial_02"
CASES = MappingProxyType({CASE: BASE_CASE, "inflight_op_scope_01": SCOPE_CLOSURE_CASE})
NOMINAL_BURN_S = 2280.7000000000407
BINDING_SCHEMA = "missionos.starship_m3_inflight_binding.v1"


def policy(envelope):
    margin = envelope["event_supervision"]["deadline_margin_s"]
    return {"schema": "missionos.starship_m3_inflight_policy.v1",
        "initial_scope": {"commands": ["select_nominal", "keep_reserved_return"],
            "resource_limits": {"maximum_return_delay_s": envelope["maximum_delay_s"]}},
        "maximum_scope_requests": 1, "permitted_amendment": "candidate_bound_select_next_orbit",
        "nominal_burn_s": NOMINAL_BURN_S,
        "simulation_deadline_s": min(NOMINAL_BURN_S-margin-15.0, 1000.7000000000407+1200.0),
        "reservation_cutoff_s": NOMINAL_BURN_S-margin,
        "maximum_wall_duration_s": 1200.0, "maximum_observation_age_s": 5.0,
        "fallback": "keep_reserved_return", "operator_authentication": "configured_bearer_principal",
        "sso_identity_verified": False, "physical_execution_authorized": False}


def binding(authority, *, run_id, plan_sha256, source_sha256, started):
    if (type(authority.get("case")) is not str or authority["case"] not in CASES
            or authority.get("inflight_scope_amendment_enabled") is not True):
        raise ValueError("registered_inflight_authority_required")
    expected = policy(authority["m2_authority"]["return_authority"])
    if authority.get("inflight_policy") != expected or type(started) not in (int, float) or not math.isfinite(started) or started < 0:
        raise ValueError("inflight_policy_or_start_mismatch")
    contract = {"schema": "missionos.inflight_executor_contract.v1",
        "context": {"run_id": run_id+"-return", "mission_id": run_id, "vehicle_id": "ship"},
        "initial_scope": deepcopy(expected["initial_scope"]),
        "bounds": {key: expected[key] for key in ("simulation_deadline_s", "reservation_cutoff_s", "maximum_observation_age_s")}}
    contract["bounds"]["wall_deadline_s"] = started+expected["maximum_wall_duration_s"]
    return {"schema": BINDING_SCHEMA, "run_id": run_id, "plan_sha256": plan_sha256,
        "authority_sha256": digest(authority), "source_sha256": deepcopy(source_sha256),
        "started_monotonic_s": started, "inflight_contract": contract}


def validate_binding(value, authority, *, run_id, sources):
    if type(value) is not dict:
        raise ValueError("inflight_binding_required")
    expected = binding(authority, run_id=run_id, plan_sha256=value.get("plan_sha256"),
        source_sha256=sources, started=value.get("started_monotonic_s"))
    plan_hash = value.get("plan_sha256")
    if (value != expected or type(plan_hash) is not str or len(plan_hash) != 64
            or any(c not in "0123456789abcdef" for c in plan_hash)):
        raise ValueError("inflight_binding_mismatch")
    from .starship_inflight_executor import validate_inflight_executor_contract
    validate_inflight_executor_contract(value["inflight_contract"], authority["m2_authority"]["return_authority"],
        run_id=run_id+"-return", nominal_burn_s=NOMINAL_BURN_S)
    return deepcopy(value["inflight_contract"])


def operator_configuration():
    token = os.environ.get("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN", "")
    identity = os.environ.get("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_ID", "")
    source = os.environ.get("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_SOURCE", "human_ui")
    if len(token) < 24 or not identity.strip() or source not in {"human_ui", "scripted_test_driver"}:
        raise ValueError("inflight_operator_configuration_required")
    return token, identity, source


class Telemetry:
    """Request contents never populate trusted executor observation history."""
    def __init__(self):
        self.latest, self.sequence = None, -1
        self.history, self.lock = deque(maxlen=64), threading.RLock()

    def accept(self, frame):
        if (type(frame) is not dict or set(frame) != {"schema", "sequence", "state"}
                or frame["schema"] != "missionos.m3_executor_telemetry.v1"
                or type(frame["sequence"]) is not int or frame["sequence"] != self.sequence+1):
            raise ValueError("invalid_executor_telemetry_sequence")
        state = frame["state"]
        if (type(state) is not dict or type(state.get("run_active")) is not bool
                or type(state.get("revision")) is not int or type(state.get("observation")) is not dict
                or state["observation"]["simulation_time_s"] != state["simulation_time_s"]):
            raise ValueError("invalid_executor_telemetry_state")
        with self.lock:
            if self.latest and (state["simulation_time_s"] < self.latest["simulation_time_s"]
                    or state["observation"]["wall_time_s"] < self.latest["observation"]["wall_time_s"]):
                raise ValueError("executor_telemetry_clock_regressed")
            if self.latest:
                self.history.append(deepcopy(self.latest["observation"]))
            self.latest, self.sequence = deepcopy(state), frame["sequence"]

    def snapshot(self):
        with self.lock:
            if self.latest is None:
                raise ValueError("executor_telemetry_not_started")
            return {**deepcopy(self.latest), "known_observations": list(deepcopy(self.history))}


def worker_runtime(binding_value, authority, *, run_id, sources, request_fd, grant_fd, telemetry_fd):
    from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, MAX_FRAME_BYTES
    contract = validate_binding(binding_value, authority, run_id=run_id, sources=sources)
    if len({request_fd, grant_fd, telemetry_fd}) != 3 or any(type(fd) is not int or fd < 3 for fd in (request_fd, grant_fd, telemetry_fd)):
        raise ValueError("three_distinct_inherited_channels_required")
    writer, reader = ApprovalPipeWriter(request_fd), ApprovalPipeReader(grant_fd)
    # Validate FIFO ownership before changing blocking mode for this dedicated
    # telemetry stream. Host drains it while physics and model waiting advance.
    telemetry = ApprovalPipeWriter(telemetry_fd)
    os.set_blocking(telemetry.fd, True)
    sequence = 0
    def sink(state):
        nonlocal sequence
        data = json.dumps({"schema": "missionos.m3_executor_telemetry.v1", "sequence": sequence,
            "state": state}, allow_nan=False, separators=(",", ":")).encode()+b"\n"
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("telemetry_frame_too_large")
        while data:
            data = data[os.write(telemetry.fd, data):]
        sequence += 1
    return {"inflight": contract, "request_writer": writer, "grant_reader": reader,
            "snapshot_sink": sink, "clock": time.monotonic}


class LiveApprovalHost:
    """One Gateway registration, one bounded live request, no operator automation."""
    def __init__(self, directory, binding_value, *, signer, service=None):
        from .starship_inflight_runtime import approval_pipe, HostApprovalBridge
        from .starship_inflight_approval import InflightApprovalLedger
        if service is None:
            from src.gateway.starship_inflight_approval import get_inflight_approval_service
            service = get_inflight_approval_service()
        token, identity, self.operator_source = operator_configuration()
        self.directory, self.binding, self.signer = Path(directory), deepcopy(binding_value), signer
        contract = binding_value["inflight_contract"]
        self.request_read, self.request_write = approval_pipe()
        self.grant_read, self.grant_write = approval_pipe()
        self.telemetry_read, self.telemetry_write = approval_pipe()
        self.child_ends = (self.request_write, self.grant_read, self.telemetry_write)
        registry = deepcopy(contract["initial_scope"])
        registry["commands"].append("select_next_orbit")
        self.ledger = InflightApprovalLedger(self.directory/"inflight-private.sqlite",
            signing_key=secrets.token_bytes(32), operator_token=token, operator_id=identity, registry=registry)
        self.telemetry = Telemetry()
        self.bridge = HostApprovalBridge(ledger=self.ledger, context=contract["context"],
            snapshot=self.telemetry.snapshot, request_reader=self.request_read,
            grant_writer=self.grant_write, bounds=contract["bounds"])
        self.service = service
        def snapshot():
            try:
                return self.bridge.snapshot()
            except ValueError:
                from fastapi import HTTPException
                raise HTTPException(409, "executor_telemetry_not_started") from None
        service.register_run(**contract["context"], ledger=self.ledger, snapshot=snapshot)
        self.closed = False

    @property
    def fds(self):
        return tuple(pipe.fd for pipe in self.child_ends)

    def spawned(self, child):
        for pipe in self.child_ends:
            pipe.close()
        self.thread = threading.Thread(target=self.serve, args=(child,), daemon=True)
        self.thread.start()

    def close(self):
        if not self.closed:
            self.service.unregister_run(self.binding["inflight_contract"]["context"]["run_id"])
            for pipe in (self.request_read, self.grant_write, self.telemetry_read):
                pipe.close()
            self.closed = True

    def serve(self, child):
        failure = None
        try:
            while child.poll() is None:
                while (frame := self.telemetry_read.poll()) is not None:
                    self.telemetry.accept(frame)
                if self.telemetry.latest is not None:
                    self.bridge.tick()
                if (self.directory/"results"/"flight-study.json").is_file():
                    break
                time.sleep(.005)
        except Exception as error:
            failure = type(error).__name__
        finally:
            record = self.bridge.record()
            signed = self.ledger.status(self.bridge.request["request_id"]) if self.bridge.request else None
            result = {"schema": "missionos.starship_m3_inflight_host_evidence.v1",
                "binding_sha256": digest(self.binding), "host_record": record, "signed_ledger_record": signed,
                "host_ledger_signatures_verified": signed is not None,
                "operator_response_source": self.operator_source, "human_operator_action_verified": False,
                "sso_identity_verified": False, "failure": failure}
            result["signature"] = self.signer(result)
            write_verified_input(self.directory/"inflight-host-runtime.json", record)
            write_verified_input(self.directory/"inflight-signed-record.json", signed)
            publish_host_evidence(self.directory, result)
            self.close()


def publish_host_evidence(directory, value):
    """Publish a fully flushed host result; readers never see partial JSON."""
    directory = Path(directory)
    temporary, target = directory/"inflight-host-result.pending.json", directory/"inflight-host-result.json"
    serialized = json.dumps(value, indent=2, allow_nan=False)+"\n"
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    if target.exists():
        raise ValueError("inflight_host_evidence_already_published")
    temporary.replace(target)


def await_host_evidence(directory, binding_value, *, timeout_s=5.0):
    path, deadline = Path(directory)/"inflight-host-result.json", time.monotonic()+timeout_s
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise ValueError("inflight_host_evidence_not_ready")
        time.sleep(.01)
    value = json.loads(path.read_text())
    if (value.get("binding_sha256") != digest(binding_value) or value.get("failure") is not None
            or value.get("schema") != "missionos.starship_m3_inflight_host_evidence.v1"):
        raise ValueError("inflight_host_evidence_binding")
    return value
