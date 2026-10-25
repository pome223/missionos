"""Two one-use, credential-free copied-origin forecasts; never execution.

Only start() launches processes. poll() never waits for a running forecast;
completed files are read/validated locally. A failed batch cannot be restarted
or return its other candidate as a success. Terminal cleanup is asynchronous;
close() can explicitly wait for children to be reaped. No SDK is imported here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import gzip
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from .starship_restored_contract import ActualRestoredOrigin, RestoredCandidateSchedule, _manifest
from .starship_restored_return import RestoredReturnRequest, RestoredReturnResult
from .starship_restored_integration import RestoredIntegrationPolicy, restored_integration
from .starship_restored_environment import worker_environment_view

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/forecast_starship_restored_candidate.py"
WORKER_ENV = "MISSIONOS_RESTORED_FORECAST_JOB"
MAXIMUM_BYTES = 2**31
REQUIRED_SOURCES = {"src/runtime/starship_restored_contract.py", "src/runtime/starship_restored_return.py",
    "src/runtime/starship_restored_forecast_pool.py", "src/runtime/starship_restored_integration.py",
    "src/runtime/starship_restored_environment.py", SCRIPT}
ENVIRONMENT_KEYS = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "PYTHONDONTWRITEBYTECODE"}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _wire(value):
    def number(item):
        import numpy as np
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(type(item).__name__)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=number).encode()


def _read(raw):
    def pairs(items):
        value = dict(items)
        _require(len(value) == len(items), "restored_forecast_duplicate_json_key")
        return value
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("restored_forecast_nonfinite_json")))
    _wire(value)
    return value


def _digest(value):
    return sha256(_wire(value)).hexdigest()


def _save(path, value):
    _save_bytes(path, _wire(value))


def _save_bytes(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)


def _restore(packed):
    parts, total = [], 0
    with gzip.GzipFile(fileobj=BytesIO(packed)) as handle:
        while True:
            part = handle.read(min(1024**2, MAXIMUM_BYTES-total+1))
            if not part:
                return b"".join(parts)
            total += len(part)
            _require(total <= MAXIMUM_BYTES, "restored_forecast_raw_output_budget_exceeded")
            parts.append(part)


def _environment():
    import numpy as np
    import scipy
    return {"python": sys.version, "platform": sys.platform, "numpy": np.__version__, "scipy": scipy.__version__,
        "executable_sha256": sha256(Path(sys.executable).read_bytes()).hexdigest()}


def _source_guard(source):
    _manifest(source)
    _require(REQUIRED_SOURCES <= source.keys(), "restored_forecast_worker_sources_missing")
    for name, expected in source.items():
        path = ROOT/name
        _require(not path.is_symlink() and path.resolve().is_relative_to(ROOT.resolve())
            and sha256(path.read_bytes()).hexdigest() == expected, "restored_forecast_source_changed")


def _inside(folder, name):
    relative = PurePosixPath(name)
    _require(type(name) is str and not relative.is_absolute() and len(relative.parts) == 1
        and relative.parts[0] not in (".", ".."), "restored_forecast_invalid_private_path")
    path = folder/name
    _require(not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()), "restored_forecast_unsafe_private_path")
    return path


def child_environment(token):
    return {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS} | {
        WORKER_ENV: token, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}


@contextmanager
def forecast_scope():
    """One captured forecast may integrate; nested forecast/process/network cannot."""
    import socket
    from . import starship_restored_return as returns, starship_return_prediction as old
    def forbidden(*_a, **_kw):
        raise RuntimeError("restored_forecast_network_process_or_nested_call_forbidden")
    with patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), \
            patch.object(socket.socket, "sendto", forbidden), patch.object(socket, "getaddrinfo", forbidden), \
            patch.object(socket, "gethostbyname", forbidden), patch.object(socket, "create_connection", forbidden), \
            patch.object(subprocess, "Popen", forbidden), patch.object(os, "system", forbidden), \
            patch.object(old, "forecast", forbidden), patch.object(returns, "forecast_restored_return", forbidden):
        yield


def _input_guard(job, folder):
    for name, expected in job["input_sha256"].items():
        _require(sha256(_inside(folder, name).read_bytes()).hexdigest() == expected,
            "restored_forecast_private_input_changed")


def _reconstruct(job, folder, candidate):
    _input_guard(job, folder)
    profile = _inside(folder, job["profile_file"]).read_bytes()
    selected = [row for row in job["requests"] if row["candidate_id"] == candidate]
    _require(len(selected) == 1, "restored_forecast_unknown_candidate")
    item, schedule = selected[0], None
    for node in item["schedule_chain"]:
        raw = _inside(folder, node["origin_file"]).read_bytes()
        origin = ActualRestoredOrigin(node["origin_sha256"], raw, profile, job["source_sha256"])
        schedule = RestoredCandidateSchedule(origin, node["maximum_simulation_time_s"], anchor=schedule)
        _require(_wire(schedule.record()) == _wire(node["schedule"]), "restored_forecast_anchor_reconstruction_changed")
    _require(schedule is not None, "restored_forecast_anchor_chain_missing")
    request = RestoredReturnRequest(schedule.origin, schedule, candidate, profile, job["source_sha256"],
        maximum_control_ticks=item["request"]["maximum_control_ticks"])
    _require(_wire(request.record()) == _wire(item["request"]), "restored_forecast_request_reconstruction_changed")
    return request


def _check_prediction(result, request):
    _require(type(result) is RestoredReturnResult, "restored_forecast_typed_result_required")
    row, origin = result.record(), request.origin.record()
    _require(row.get("schema") == "missionos.starship_restored_return_prediction.v1"
        and row.get("prediction_only") is True and _wire(row.get("binding")) == _wire(request.record())
        and _wire(row.get("initial_binding")) == _wire(request.record())
        and _wire(row.get("initial_state")) == _wire(origin["state"])
        and _wire(row.get("vehicle")) == _wire(origin["vehicle"])
        and _wire(row.get("final_vehicle")) == _wire(origin["vehicle"])
        and type(row.get("retained_count")) is int and row["retained_count"] == origin["retained_count"]
        and type(row.get("payload_releases_after_origin")) is int and row["payload_releases_after_origin"] == 0
        and row.get("reservation_changes") == []
        and row.get("actual_state_initialized_from_prediction") is False
        and row.get("source_provenance_authenticated") is False
        and row.get("independent_physical_model") is False
        and type(row.get("model_calls")) is int and row["model_calls"] == 0,
        "restored_forecast_prediction_binding_changed")
    final, outcome = row["final_state"], row["outcome"]
    from . import starship_sixdof as dyn
    state, craft = dyn.state_from_dict(final), dyn.vehicle_from_dict(origin["vehicle"])
    dyn._validate_pair(state, craft, dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad))
    _require(type(final.get("time_s")) in (int, float) and origin["state"]["time_s"] <= final["time_s"]
        <= request.record()["maximum_simulation_time_s"] and final["propellant_kg"] <= origin["state"]["propellant_kg"]+1e-6
        and [x["available"] for x in final["engine_states"]] == [x["available"] for x in origin["state"]["engine_states"]]
        and outcome.get("termination") in ("surface_impact", "low_speed_surface_contact", "angular_rate_envelope_exceeded",
            "time_limit", "control_tick_limit"), "restored_forecast_invalid_or_numerical_result")
    return result


def _integration_policy(value):
    _require(type(value) is dict, "restored_forecast_typed_integration_policy_required")
    policy = RestoredIntegrationPolicy.from_record(value)
    _require(_wire(value) == _wire(policy.record()), "restored_forecast_integration_policy_changed")
    return policy


def _check_integration(value, policy):
    if policy.fuel_event_mode == "outer_bracket_v2":
        from .starship_restored_integration import validate_execution_receipt
        return validate_execution_receipt(value, policy)
    _require(type(value) is dict and type(value.get("trace")) is dict, "restored_forecast_integration_receipt_missing")
    trace = value["trace"]
    _require(type(trace.get("rk4_calls")) is int and trace["rk4_calls"] > 0
        and type(trace.get("rk4_substeps")) is int and trace["rk4_substeps"] == trace["rk4_calls"]*policy.subdivisions
        and type(trace.get("commands_recomputed_inside_rk4")) is int and trace["commands_recomputed_inside_rk4"] == 0
        and all(trace.get(key) is False for key in ("outer_control_cadence_changed", "fuel_event_search_changed", "contact_search_changed")),
        "restored_forecast_integration_trace_invalid")
    expected = {"schema": "missionos.starship_restored_integration_execution.v1", "policy": policy.record(),
        "status": "completed", "failure_class": None, "trace": trace, "process_isolation_required": True,
        "internal_rk4_only": True, "physics_limits_changed": False, "control_policy_changed": False,
        "experimental": True, "qualification_refreshed": False, "mission_authority_granted": False,
        "execution_permission_granted": False, "hosted_model_calls": 0, "nested_sixdof_forecasts": 0}
    _require(_wire(value) == _wire(expected), "restored_forecast_integration_receipt_changed")
    return value


def worker(job_dir, candidate_id, job_sha256):
    """Private subprocess entry; no caller-provided return clocks or checkpoints."""
    folder = Path(job_dir).resolve()
    raw = (folder/"job.json").read_bytes()
    _require(os.environ.get(WORKER_ENV) == job_sha256 == sha256(raw).hexdigest(), "restored_forecast_private_worker_token_required")
    _require(set(worker_environment_view()) <= ENVIRONMENT_KEYS | {WORKER_ENV, "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"},
        "restored_forecast_unsanitized_worker_environment")
    job = _read(raw)
    _require(job.get("schema") == "missionos.starship_restored_forecast_batch.v1"
        and _wire(_read((folder/"batch.claim").read_bytes())) == _wire({"job_sha256": job_sha256, "attempt": 1})
        and _environment() == job["environment"] and time.monotonic() < job["wall_deadline_monotonic"],
        "restored_forecast_invalid_or_expired_job")
    _source_guard(job["source_sha256"])
    policy = _integration_policy(job["integration_policy"])
    request = _reconstruct(job, folder, candidate_id)
    target = folder/candidate_id
    _require(candidate_id in ("nominal", "next_orbit") and target.is_dir() and not target.is_symlink(), "restored_forecast_invalid_worker_directory")
    claim = {"job_sha256": job_sha256, "candidate_id": candidate_id, "request_sha256": _digest(request.record()), "attempt": 1}
    _require(_wire(_read((target/"started.json").read_bytes())) == _wire(claim), "restored_forecast_start_claim_changed")
    _save(target/"worker.claim", claim)
    last, sequence = None, 0
    def observed(tick):
        nonlocal last, sequence
        _require(time.monotonic() < job["wall_deadline_monotonic"], "restored_forecast_worker_deadline_expired")
        now = float(tick.state.time_s)
        if last is None or now >= last+60.-1e-9:
            sequence += 1
            _save(target/f"progress-{sequence:04d}.json", {"schema": "missionos.starship_restored_forecast_progress.v1",
                **claim, "phase": tick.phase, "state": asdict(tick.state), "prediction_only": True,
                "sampled_only": True, "partial_not_completion": True})
            last = now
    integration = None
    try:
        from .starship_restored_return import forecast_restored_return
        with forecast_scope(), restored_integration(policy) as integration:
            result = forecast_restored_return(request, on_tick=observed)
        integration_receipt = _check_integration(integration.record(), policy)
        _save(target/"integration.json", integration_receipt)
        _require(type(result) is RestoredReturnResult, "restored_forecast_typed_result_required")
        packed = gzip.compress(result._record_json, mtime=0)
        _save_bytes(target/"prediction.json.gz", packed)
        _require(_restore((target/"prediction.json.gz").read_bytes()) == result._record_json,
            "restored_forecast_gzip_restore_failed")
        _check_prediction(result, request)
        _source_guard(job["source_sha256"])
        _require(time.monotonic() < job["wall_deadline_monotonic"], "restored_forecast_worker_deadline_expired")
        _save(target/"result.json", {"schema": "missionos.starship_restored_forecast_worker_result.v1", **claim,
            "raw_sha256": result.sha256, "gzip_sha256": sha256(packed).hexdigest(),
            "integration_execution": integration_receipt,
            "prediction_only": True, "model_calls": 0, "execution_initialized": False})
    except BaseException as error:
        _save(target/"failure.json", {"schema": "missionos.starship_restored_forecast_worker_failure.v1", **claim,
            "exception_type": type(error).__name__, "retry_authorized": False, "prediction_only": True})
        if integration is not None and not (target/"integration.json").exists():
            _save(target/"integration.json", integration.record())
        raise


class RestoredForecastBatch:
    """Single-use pair of frozen-opportunity forecasts; construction launches none."""

    def __init__(self, requests, *, output_dir, wall_deadline_monotonic, expected_source_sha256, integration_policy):
        _require(type(requests) is tuple and len(requests) == 2
            and all(type(x) is RestoredReturnRequest for x in requests)
            and tuple(x.candidate_id for x in requests) == ("nominal", "next_orbit"), "restored_forecast_exact_candidate_pair_required")
        _require(type(wall_deadline_monotonic) in (int, float) and math.isfinite(wall_deadline_monotonic)
            and wall_deadline_monotonic > 0,
            "restored_forecast_invalid_outer_deadline")
        _manifest(expected_source_sha256)
        _require(type(integration_policy) is RestoredIntegrationPolicy,
            "restored_forecast_typed_integration_policy_required")
        _require(REQUIRED_SOURCES <= expected_source_sha256.keys(), "restored_forecast_worker_sources_missing")
        first = requests[0]
        for request in requests:
            _require(_wire(request.origin.record()) == _wire(first.origin.record())
                and _wire(request.schedule.record()) == _wire(first.schedule.record())
                and _wire(request.origin.record()["source_sha256"]) == _wire(expected_source_sha256)
                and request.record()["maximum_simulation_time_s"] == 16000.
                and request.maximum_control_ticks == first.maximum_control_ticks
                and request._profile_raw_bytes == first._profile_raw_bytes, "restored_forecast_pair_origin_source_or_anchor_changed")
        self.requests, self.output_dir, self.deadline = requests, Path(output_dir).absolute(), float(wall_deadline_monotonic)
        self._source_wire = _wire(expected_source_sha256)
        self.integration_policy = integration_policy
        self.state, self.processes, self.logs, self.results = "created", [], [], {}
        self._job, self._error, self._cleanup = None, None, None
        self._owns_output = False

    def _fail(self, reason):
        if self.state != "failed":
            self.state, self._error = "failed", reason
            if self._owns_output and not (self.output_dir/"failure.json").exists():
                _save(self.output_dir/"failure.json", {"schema": "missionos.starship_restored_forecast_batch_failure.v1",
                    "reason": reason, "retry_authorized": False, "prediction_only": True})
            active = [p for p in self.processes if p.poll() is None]
            for process in active:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            def reap():
                for process in active:
                    try:
                        process.wait(timeout=5.)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                for log in self.logs:
                    log.close()
            self._cleanup = threading.Thread(target=reap, daemon=True, name="restored-forecast-reap")
            self._cleanup.start()
        raise ValueError(self._error)

    def start(self):
        _require(self.state == "created", "restored_forecast_batch_single_use")
        self.state = "starting"
        try:
            _source_guard(_read(self._source_wire))
            _require(time.monotonic() < self.deadline, "restored_forecast_outer_deadline_expired")
            _require(not self.output_dir.exists() and not self.output_dir.is_symlink(), "restored_forecast_exclusive_output_required")
            self.output_dir.mkdir(parents=True, mode=0o700)
            self._owns_output = True
            inputs, origins, specs = {}, {}, []
            profile = self.requests[0]._profile_raw_bytes
            _save_bytes(self.output_dir/"profile.json", profile)
            inputs["profile.json"] = sha256(profile).hexdigest()
            for request in self.requests:
                chain, schedule = [], request.schedule
                while schedule is not None:
                    _require(len(chain) < 64, "restored_forecast_anchor_chain_budget")
                    chain.append(schedule)
                    schedule = schedule.anchor
                nodes = []
                for schedule in reversed(chain):
                    raw = schedule.origin.source_record_bytes()
                    hashed = sha256(raw).hexdigest()
                    if hashed not in origins:
                        name = f"origin-{len(origins):03d}.json"
                        _require(sum((self.output_dir/f).stat().st_size for f in inputs)+len(raw) <= MAXIMUM_BYTES,
                            "restored_forecast_private_input_budget_exceeded")
                        _save_bytes(self.output_dir/name, raw)
                        inputs[name], origins[hashed] = hashed, name
                    nodes.append({"origin_file": origins[hashed], "origin_sha256": hashed,
                        "maximum_simulation_time_s": schedule.maximum_simulation_time_s, "schedule": schedule.record()})
                specs.append({"candidate_id": request.candidate_id, "request": request.record(), "schedule_chain": nodes})
            job = {"schema": "missionos.starship_restored_forecast_batch.v1", "source_sha256": _read(self._source_wire),
                "integration_policy": self.integration_policy.record(),
                "origin_proves_numerical_policy": False,
                "matching_actual_policy_requires_outer_registered_execution_binding": True,
                "environment": _environment(), "profile_file": "profile.json", "input_sha256": inputs,
                "requests": specs, "wall_deadline_monotonic": self.deadline,
                "maximum_workers": 2, "maximum_output_bytes": MAXIMUM_BYTES, "prediction_only": True,
                "model_calls": 0, "execution_initialized": False}
            self._job = _wire(job)
            token = sha256(self._job).hexdigest()
            _save_bytes(self.output_dir/"job.json", self._job)
            _save(self.output_dir/"batch.claim", {"job_sha256": token, "attempt": 1})
            for request in self.requests:
                _require(time.monotonic() < self.deadline, "restored_forecast_outer_deadline_expired")
                folder = self.output_dir/request.candidate_id
                folder.mkdir(mode=0o700)
                _save(folder/"started.json", {"job_sha256": token, "candidate_id": request.candidate_id,
                    "request_sha256": _digest(request.record()), "attempt": 1})
                log = (folder/"worker.log").open("xb")
                self.logs.append(log)
                self.processes.append(subprocess.Popen([sys.executable, str(ROOT/SCRIPT), "--job-dir", str(self.output_dir.resolve()),
                    "--candidate-id", request.candidate_id, "--job-sha256", token], cwd=ROOT,
                    env=child_environment(token), stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
            self.state = "running"
            return self
        except Exception as error:
            return self._fail(str(error) if isinstance(error, ValueError) else "restored_forecast_start_failed")

    def poll(self):
        if self.state == "failed":
            raise ValueError(self._error)
        _require(self.state in ("running", "completed"), "restored_forecast_batch_not_started")
        try:
            _source_guard(_read(self._source_wire))
            if self.state == "completed":
                return tuple(self.results[x.candidate_id] for x in self.requests)
            _require(time.monotonic() < self.deadline, "restored_forecast_outer_deadline_expired")
            _require((self.output_dir/"job.json").read_bytes() == self._job, "restored_forecast_job_changed")
            job, token = _read(self._job), sha256(self._job).hexdigest()
            _input_guard(job, self.output_dir)
            _require(_wire(_read((self.output_dir/"batch.claim").read_bytes()))
                == _wire({"job_sha256": token, "attempt": 1}), "restored_forecast_parent_claim_changed")
            _require(_environment() == job["environment"], "restored_forecast_environment_changed")
            _require(sum(p.stat().st_size for p in self.output_dir.rglob("*") if p.is_file()) <= MAXIMUM_BYTES,
                "restored_forecast_output_budget_exceeded")
            for process, request, log in zip(self.processes, self.requests, self.logs):
                code = process.poll()
                if code is None:
                    continue
                _require(code == 0, "restored_forecast_worker_failed")
                if request.candidate_id in self.results:
                    continue
                log.close()
                folder = self.output_dir/request.candidate_id
                packed = (folder/"prediction.json.gz").read_bytes()
                raw = _restore(packed)
                result = _check_prediction(RestoredReturnResult(raw), request)
                integration = _check_integration(_read((folder/"integration.json").read_bytes()), self.integration_policy)
                receipt = {"schema": "missionos.starship_restored_forecast_worker_result.v1", "job_sha256": token,
                    "candidate_id": request.candidate_id, "request_sha256": _digest(request.record()), "attempt": 1,
                    "raw_sha256": sha256(raw).hexdigest(), "gzip_sha256": sha256(packed).hexdigest(),
                    "integration_execution": integration,
                    "prediction_only": True, "model_calls": 0, "execution_initialized": False}
                _require(_wire(_read((folder/"result.json").read_bytes())) == _wire(receipt)
                    and _wire(_read((folder/"worker.claim").read_bytes())) == _wire({k: receipt[k] for k in
                        ("job_sha256", "candidate_id", "request_sha256", "attempt")})
                    and _wire(_read((folder/"started.json").read_bytes())) == _wire({k: receipt[k] for k in
                        ("job_sha256", "candidate_id", "request_sha256", "attempt")}), "restored_forecast_result_or_claim_changed")
                self.results[request.candidate_id] = result
            if len(self.results) == 2:
                self.state = "completed"
                return tuple(self.results[x.candidate_id] for x in self.requests)
            return None
        except Exception as error:
            return self._fail(str(error) if isinstance(error, ValueError) else "restored_forecast_saved_result_invalid")

    def close(self):
        if self.state == "created":
            self.state = "closed"
        if self.state in ("starting", "running"):
            try:
                self._fail("restored_forecast_batch_closed_before_completion")
            except ValueError:
                pass
        if self._cleanup is not None:
            self._cleanup.join(timeout=12.)
        for log in self.logs:
            log.close()
