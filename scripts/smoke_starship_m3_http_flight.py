#!/usr/bin/env python3
"""One explicitly opted-in fixture flight with host-only local HTTP approval."""

import argparse
from collections import deque
from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def package_paths(root):
    return [
        str(root / part)
        for part in (
            ".",
            "packages/missionos-core/src",
            "packages/missionos-gateway/src",
            "packages/missionos-cli/src",
        )
    ]


sys.path[:0] = package_paths(ROOT)
BUDGET = {
    "attempts": 1,
    "model_calls": 0,
    "maximum_forecasts": 20,
    "maximum_workers": 2,
    "wall_time_s": 2400.0,
    "retry_performed": False,
    "maximum_output_bytes": 1_200_000_000,
    "minimum_free_bytes": 1_073_741_824,
}


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def check_storage_budget(output_dir):
    """Bound this attempt only; never remove or overwrite previous evidence."""
    used = 0
    for path in output_dir.rglob("*"):
        try:
            if path.is_file():
                used += path.stat().st_size
        except FileNotFoundError:
            # Atomic publication may rename a temporary artifact during polling.
            continue
    free = shutil.disk_usage(output_dir).free
    checks = {"output_within_budget": used < BUDGET["maximum_output_bytes"],
              "free_reserve_preserved": free >= BUDGET["minimum_free_bytes"]}
    record = {"output_bytes": used, "free_bytes": free, "checks": checks,
              "observed_wall_time_s": time.monotonic(), "files_deleted": 0}
    save(output_dir / "storage-budget.json", record)
    if not all(checks.values()):
        raise OSError("fixed_flight_storage_budget_exhausted")
    return record


def clean_environment(root):
    values = {
        k: os.environ[k]
        for k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if k in os.environ
    }
    values.update(
        PYTHONPATH=os.pathsep.join(package_paths(root)),
        PYTHONDONTWRITEBYTECODE="1",
        MISSIONOS_LLM_BACKEND="off",
        MISSIONOS_AGENT_RUNTIME_ADK_ENABLED="0",
        MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED="0",
        MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED="0",
    )
    return values


def failure_diagnostic(error, reason):
    """No arbitrary exception text: import name is the only optional detail."""
    value = {"type": type(error).__name__, "reason": reason, "retry_performed": False}
    missing = getattr(error, "name", None)
    if (
        isinstance(error, ModuleNotFoundError)
        and type(missing) is str
        and len(missing) <= 128
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", missing)
    ):
        value["missing_module"] = missing
    return value


@contextmanager
def gateway_runtime(output_dir):
    """Import, construct and check the real local host before any worker starts."""
    old_environment, old_directory = dict(os.environ), Path.cwd()
    output_dir = output_dir.resolve()
    sock, server, thread = None, None, None
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        clean = clean_environment(ROOT)
        os.environ.clear()
        os.environ.update(clean)
        os.environ.update(
            GATEWAY_API_KEY="",
            GATEWAY_HOST="127.0.0.1",
            GATEWAY_PORT=str(port),
            TASK_STORE_DB_PATH=str(output_dir / "gateway-tasks.sqlite"),
            MEMORY_DB_PATH=str(output_dir / "gateway-memory.sqlite"),
            AUDIT_LOG_PATH=str(output_dir / "gateway-audit.log"),
        )
        # Settings must not load a repository .env during a fixture preflight.
        os.chdir(output_dir)
        from src.config.settings import reset_settings

        reset_settings()
        from src.gateway.server import create_missionos_gateway
        from src.gateway.starship_inflight_approval import PREFIX, get_inflight_approval_service
        import uvicorn

        service = get_inflight_approval_service()
        server = uvicorn.Server(
            uvicorn.Config(
                create_missionos_gateway().app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="off",
            )
        )
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 15.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("gateway_start_timeout")
        origin = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(origin + "/health", timeout=5) as response:
            healthy = response.status == 200 and json.load(response).get("status") == "healthy"
        with urllib.request.urlopen(origin + PREFIX, timeout=5) as response:
            html = response.read(262145)
            operator_ready = (
                response.status == 200
                and len(html) <= 262144
                and b'id="approve"' in html
                and b'id="reject"' in html
            )
        if not healthy or not operator_ready:
            raise RuntimeError("gateway_runtime_readiness_failed")
        yield {
            "service": service,
            "prefix": PREFIX,
            "origin": origin,
            "checks": {
                "gateway_http_started": True,
                "health_ready": healthy,
                "operator_route_ready": operator_ready,
            },
        }
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.ident is not None:
            thread.join(timeout=5)
        if sock is not None:
            sock.close()
        os.chdir(old_directory)
        os.environ.clear()
        os.environ.update(old_environment)
        if "src.config.settings" in sys.modules:
            sys.modules["src.config.settings"].reset_settings()


def check_runtime(args):
    """Production Gateway startup only: no worker, forecast, approval or model."""
    args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {
        "schema": "missionos.starship_m3_http_runtime_check.v1",
        "passed": False,
        "worker_spawned": False,
        "simulator_runs": 0,
        "forecasts": 0,
        "model_calls": 0,
        "operator_approvals": 0,
        "retry_performed": False,
        "m3_completed": False,
        "physical_execution": False,
    }
    started = time.monotonic()
    try:
        with gateway_runtime(args.output_dir) as runtime:
            result.update(passed=True, checks=runtime["checks"])
    except Exception as error:
        result["diagnostic"] = failure_diagnostic(error, "host_runtime_initialization_failed")
    result["wall_time_s"] = time.monotonic() - started
    save(args.output_dir / "runtime-check.json", result)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["passed"] else 2


def host(args):
    # Also protect direct callers before Gateway preflight changes cwd.
    args.output_dir = args.output_dir.expanduser().resolve()
    if getattr(args, "reference_prefix", None) is not None:
        args.reference_prefix = args.reference_prefix.expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    runtime_ready = False
    try:
        with gateway_runtime(args.output_dir) as runtime:
            runtime_ready = True
            save(
                args.output_dir / "runtime-check.json",
                {
                    "schema": "missionos.starship_m3_http_runtime_check.v1",
                    "passed": True,
                    "checks": runtime["checks"],
                    "worker_spawned": False,
                    "model_calls": 0,
                    "simulator_runs": 0,
                    "forecasts": 0,
                    "operator_approvals": 0,
                    "wall_time_s": time.monotonic() - started,
                    "physical_execution": False,
                },
            )
            return flight_host(args, runtime, started)
    except Exception as error:
        reason = "host_preflight_failed" if runtime_ready else "host_runtime_initialization_failed"
        save(args.output_dir / "host-failure.json", failure_diagnostic(error, reason))
        save(
            args.output_dir / "summary.json",
            {
                "declared_fixture_accepted": False,
                "m3_completed": False,
                "model_calls": 0,
                "retry_performed": False,
                "host_runtime_ready": runtime_ready,
                "reason": reason,
            },
        )
        return 2


class Telemetry:
    """History comes exclusively from the inherited executor telemetry pipe."""

    def __init__(self):
        self.latest, self.sequence = None, -1
        self.history = deque(maxlen=64)
        self.lock = threading.RLock()

    def accept(self, frame):
        if (
            type(frame) is not dict
            or set(frame) != {"schema", "sequence", "state"}
            or frame["schema"] != "missionos.m3_executor_telemetry.v1"
            or type(frame["sequence"]) is not int
            or frame["sequence"] != self.sequence + 1
        ):
            raise ValueError("invalid_executor_telemetry_sequence")
        state = frame["state"]
        if (
            type(state) is not dict
            or type(state.get("run_active")) is not bool
            or type(state.get("revision")) is not int
            or type(state.get("observation")) is not dict
            or state["observation"]["simulation_time_s"] != state["simulation_time_s"]
        ):
            raise ValueError("invalid_executor_telemetry_state")
        with self.lock:
            if self.latest and (
                state["simulation_time_s"] < self.latest["simulation_time_s"]
                or state["observation"]["wall_time_s"] < self.latest["observation"]["wall_time_s"]
            ):
                raise ValueError("executor_telemetry_clock_regressed")
            if self.latest:
                self.history.append(deepcopy(self.latest["observation"]))
            self.latest, self.sequence = deepcopy(state), frame["sequence"]

    def snapshot(self):
        with self.lock:
            if self.latest is None:
                raise ValueError("executor_telemetry_not_started")
            return {**deepcopy(self.latest), "known_observations": list(deepcopy(self.history))}


def inflight_contract(authority, reference, run_id, started):
    from src.runtime.starship_inflight_executor import (
        FIXTURE_POLICY,
        validate_inflight_executor_contract,
    )
    from src.runtime.starship_replanning import digest

    launch = reference["prefix_run"]
    if (
        reference["authority"]["case"] != "normal"
        or digest(reference["profile"]) != authority["profile_sha256"]
    ):
        raise ValueError("normal_prefix_reference_profile_mismatch")
    cutoffs = [
        event["time_s"] for event in launch["events"] if event["event"] == "orbit_cutoff_command"
    ]
    if len(cutoffs) != 1:
        raise ValueError("reference_cutoff_required")
    nominal = cutoffs[0] + reference["profile"]["guidance"]["coast_before_return_s"]
    envelope = authority["return_authority"]
    contract = {
        "schema": "missionos.inflight_executor_contract.v1",
        "context": {"run_id": run_id + "-return", "mission_id": run_id, "vehicle_id": "ship"},
        "initial_scope": {
            "commands": ["select_nominal", "keep_reserved_return"],
            "resource_limits": {"maximum_return_delay_s": envelope["maximum_delay_s"]},
        },
        "bounds": {
            "simulation_deadline_s": min(
                nominal - envelope["event_supervision"]["deadline_margin_s"] - 15.0,
                authority["checkpoint_time_s"] + 1200.0,
            ),
            "reservation_cutoff_s": nominal - envelope["event_supervision"]["deadline_margin_s"],
            "wall_deadline_s": started + 1200.0,
            "maximum_observation_age_s": 5.0,
        },
        "fixture_policy": deepcopy(FIXTURE_POLICY),
    }
    validate_inflight_executor_contract(
        contract, envelope, run_id=run_id + "-return", nominal_burn_s=nominal
    )
    if envelope["maximum_forecasts"] > 20 or envelope["maximum_workers"] > 2:
        raise ValueError("registered_numerical_budget_exceeds_smoke_budget")
    return contract


def worker(args):
    from src.runtime.starship_inflight_runtime import (
        ApprovalPipeReader,
        ApprovalPipeWriter,
        MAX_FRAME_BYTES,
    )
    from src.runtime.starship_m3_fixture import run_fixture

    contract = json.loads(args.contract.read_text())
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    request_writer, grant_reader = (
        ApprovalPipeWriter(args.request_fd),
        ApprovalPipeReader(args.grant_fd),
    )
    # A distinct one-way pipe is trusted executor telemetry, never request data.
    os.set_blocking(args.telemetry_fd, True)
    sequence = 0

    def snapshot_sink(state):
        nonlocal sequence
        raw = (
            json.dumps(
                {
                    "schema": "missionos.m3_executor_telemetry.v1",
                    "sequence": sequence,
                    "state": state,
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError("executor_snapshot_too_large")
        view = memoryview(raw)
        while view:
            view = view[os.write(args.telemetry_fd, view) :]
        sequence += 1

    try:
        result = run_fixture(
            profile,
            "normal",
            args.output_dir / "flight",
            run_id=args.run_id,
            inflight_runtime={
                "inflight": contract,
                "request_writer": request_writer,
                "grant_reader": grant_reader,
                "snapshot_sink": snapshot_sink,
                "clock": time.monotonic,
            },
        )
        save(
            args.output_dir / "worker-result.json",
            {
                "same_state_continuation_checked": result["same_state_continuation_checked"],
                "m3_completed": False,
                "model_calls": 0,
                "worker_pid": os.getpid(),
            },
        )
        return 0 if result["same_state_continuation_checked"] else 2
    except Exception as error:
        save(
            args.output_dir / "worker-failure.json",
            {"type": type(error).__name__, "reason": str(error), "retry_performed": False},
        )
        return 2
    finally:
        request_writer.close()
        grant_reader.close()
        os.close(args.telemetry_fd)


def http_call(origin, path, token, body=None):
    from src.gateway.starship_inflight_approval import PREFIX

    headers = {
        "Origin": origin,
        "Referer": origin + PREFIX,
        "Sec-Fetch-Site": "same-origin",
        "X-MissionOS-Operator": "starship-inflight-v1",
        "Authorization": "Bearer " + token,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def approval_body(pending):
    """Bind the exact request and candidate displayed by the production route."""
    request = pending["request"]
    return {
        "request_sha256": request["sha256"],
        "observed_state_sha256": request["observed_state_sha256"],
        "candidate_sha256": request["candidate_sha256"],
    }


def manifest():
    from src.runtime.starship_m3_fixture import sources

    result = sources(include_inflight=True)
    for name in (
        "scripts/smoke_starship_m3_http_flight.py",
        "src/gateway/server.py",
        "src/gateway/starship_inflight_approval.py",
        "src/runtime/assets/starship_inflight_operator.html",
    ):
        result[name] = sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def combined_verdict(composition, approval, checks):
    inner = composition["m2_study"]
    returning, handoff = inner.get("return_study"), inner.get("handoff")
    record_ok = bool(composition["same_state_continuation_checked"] and approval["passed"])
    dispatch = returning.get("dispatch") if returning else None
    numerical = (composition.get("return_verification") or {}).get("return_verification") or {}
    # M2 normal equality/nominal-plan case acceptance is deliberately unchanged.
    fixture_accepted = bool(
        record_ok
        and all(checks.values())
        and handoff
        and handoff["released_count"] == 26
        and dispatch
        and dispatch["candidate_id"] == "next_orbit"
        and approval["scope_amendment_applied"]
        and approval["reservation_observed"]
        and numerical.get("outcome") == "contact_and_recovery_area_met"
    )
    return {
        "schema": "missionos.starship_m3_http_fixture_smoke.v1",
        "record_checks_passed": record_ok,
        "declared_fixture_accepted": fixture_accepted,
        "m2_normal_case_accepted": composition["return_verification"].get("case_accepted", False),
        "approval_verification": approval,
        "http_and_process_checks": checks,
        "booster_outcome": composition.get("booster_outcome"),
        "model_calls": 0,
        "operator_response_source": "scripted_test_operator",
        "sso_identity_verified": False,
        "human_direct_flight_commands": 0,
        "retry_performed": False,
        "m3_completed": False,
        "mission_completed": False,
        "physical_execution": False,
    }


def flight_host(args, runtime, started):
    from src.runtime.starship_m2_contract import m2_contract, PROFILE
    from src.runtime.starship_return_feasibility import readiness
    from src.runtime.starship_inflight_runtime import approval_pipe, HostApprovalBridge
    from src.runtime.starship_inflight_approval import InflightApprovalLedger, InflightApprovalError
    from src.runtime.starship_inflight_executor import verify_inflight_return

    authority = m2_contract("fixture", "normal")
    reference_bytes = args.reference_prefix.read_bytes()
    reference = json.loads(reference_bytes)
    contract = inflight_contract(authority, reference, args.run_id, started)
    profile = json.loads((ROOT / PROFILE).read_text())
    _, _, error = readiness(profile)
    if error:
        raise ValueError(error)
    frozen = manifest()
    initial_storage = check_storage_budget(args.output_dir)
    save(
        args.output_dir / "preflight.json",
        {
            "schema": "missionos.starship_m3_http_fixture_budget.v1",
            "budget": BUDGET,
            "initial_storage": initial_storage,
            "case": "normal",
            "source_sha256": frozen,
            "contract": contract,
            "reference_sha256": sha256(reference_bytes).hexdigest(),
            "reference_only_for_fixed_cutoff": True,
            "reference_state_used_as_execution_origin": False,
            "provider_invocations_authorized": False,
        },
    )
    contract_file = args.output_dir / "inflight-contract.json"
    save(contract_file, contract)
    request_read, request_write = approval_pipe()
    grant_read, grant_write = approval_pipe()
    telemetry_read, telemetry_write = approval_pipe()
    log = (args.output_dir / "worker.log").open("w")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--approve-simulation",
        "--output-dir",
        str(args.output_dir),
        "--run-id",
        args.run_id,
        "--contract",
        str(contract_file),
        "--request-fd",
        str(request_write.fd),
        "--grant-fd",
        str(grant_read.fd),
        "--telemetry-fd",
        str(telemetry_write.fd),
    ]
    child = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=clean_environment(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        pass_fds=(request_write.fd, grant_read.fd, telemetry_write.fd),
        start_new_session=True,
    )
    request_write.close()
    grant_read.close()
    telemetry_write.close()
    try:
        # Created only after spawn; not an environment, argument, pipe or artifact.
        token, key = secrets.token_urlsafe(40), secrets.token_bytes(32)
        registry = deepcopy(contract["initial_scope"])
        registry["commands"].append("select_next_orbit")
        ledger = InflightApprovalLedger(
            args.output_dir / "operator-private.sqlite",
            signing_key=key,
            operator_token=token,
            operator_id="scripted-local-test-operator",
            registry=registry,
        )
        telemetry = Telemetry()
        bridge = HostApprovalBridge(
            ledger=ledger,
            context=contract["context"],
            snapshot=telemetry.snapshot,
            request_reader=request_read,
            grant_writer=grant_write,
            bounds=contract["bounds"],
        )
        service = runtime["service"]
        service.register_run(**contract["context"], ledger=ledger, snapshot=bridge.snapshot)
    except Exception as exc:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        for pipe in (request_read, grant_write, telemetry_read):
            pipe.close()
        log.close()
        save(
            args.output_dir / "host-failure.json",
            failure_diagnostic(exc, "host_initialization_failed"),
        )
        save(
            args.output_dir / "summary.json",
            {"declared_fixture_accepted": False, "m3_completed": False, "model_calls": 0},
        )
        return 2
    origin = runtime["origin"]
    base = (
        runtime["prefix"]
        + f"/runs/{contract['context']['run_id']}/missions/{args.run_id}/vehicles/ship"
    )
    transcript, checks, approved, signed = [], {}, False, None
    pending_request = None

    def call(path, body=None, **options):
        status, result = http_call(origin, path, options.get("token", token), body)
        transcript.append(
            {
                "wall_time_s": time.monotonic(),
                "method": "POST" if body is not None else "GET",
                "path": path,
                "status": status,
                "response": result,
            }
        )
        return status, result

    try:
        checks["real_gateway_started"] = True
        next_storage_check = time.monotonic()
        while child.poll() is None:
            if time.monotonic() - started >= BUDGET["wall_time_s"]:
                raise TimeoutError("fixed_flight_wall_budget_exhausted")
            if time.monotonic() >= next_storage_check:
                check_storage_budget(args.output_dir)
                next_storage_check = time.monotonic() + 5.0
            for _ in range(512):
                frame = telemetry_read.poll()
                if frame is None:
                    break
                telemetry.accept(frame)
            if telemetry.latest is not None:
                bridge.tick()
            if bridge.request is not None and not approved:
                pending_request = bridge.request
                status, pending = call(base + "/pending")
                checks["pending_request_delivered"] = (
                    status == 200 and pending.get("resolvable") is True
                )
                checks["operator_auth_required"] = (
                    call(base + "/pending", token="invalid-test-token")[0] == 401
                )
                request_id = pending_request["request_id"]
                body = approval_body(pending)
                status, response = call(base + f"/requests/{request_id}/approve", body)
                checks["http_approved_not_dispatched"] = (
                    status == 200 and response.get("executor_command_issued") is False
                )
                checks["http_replay_rejected"] = (
                    call(base + f"/requests/{request_id}/approve", body)[0] == 409
                )
                approved = True  # One response attempt only, including failed HTTP.
            if bridge.finished and signed is None and bridge.request:
                signed = ledger.status(bridge.request["request_id"])
                state = telemetry.snapshot()
                try:
                    ledger.consume(
                        request_id=bridge.request["request_id"],
                        mission_id=args.run_id,
                        vehicle_id="ship",
                        revision=0,
                        scope=contract["initial_scope"],
                        observation=state["observation"],
                        simulation_time_s=state["simulation_time_s"],
                        run_active=state["run_active"],
                    )
                    checks["second_consumption_rejected"] = False
                except InflightApprovalError as exc:
                    checks["second_consumption_rejected"] = str(exc) == "request_already_consumed"
                status, pending = call(base + "/pending")
                checks["consumed_request_not_resolvable"] = (
                    status == 200
                    and pending.get("resolvable") is False
                    and pending.get("consumption") == signed["consumption"]
                )
                save(
                    args.output_dir / "host-consumption-checkpoint.json",
                    {
                        "host_record": bridge.record(),
                        "ledger_record": signed,
                        "later_reservation_observation_checked": False,
                        "burn_dispatch_claimed": False,
                    },
                )
            time.sleep(0.005)
        check_storage_budget(args.output_dir)
        checks["storage_budget_preserved"] = True
        while True:
            frame = telemetry_read.poll()
            if frame is None:
                break
            telemetry.accept(frame)
        checks["separate_worker_exit_zero"] = child.returncode == 0 and child.pid != os.getpid()
    except Exception as exc:
        save(
            args.output_dir / "host-failure.json",
            failure_diagnostic(exc, "host_runtime_operation_failed"),
        )
        checks["host_completed"] = False
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        service.unregister_run(contract["context"]["run_id"])
        for pipe in (request_read, grant_write, telemetry_read):
            pipe.close()
        log.close()
        save(args.output_dir / "host-runtime.json", bridge.record())
        save(args.output_dir / "http-transcript.json", transcript)
        if signed is not None:
            save(args.output_dir / "host-verified-ledger.json", signed)
        save(
            args.output_dir / "telemetry-summary.json",
            {
                "frames": telemetry.sequence + 1,
                "latest": telemetry.latest,
                "request_data_used_as_telemetry": False,
                "host_pid": os.getpid(),
                "worker_pid": child.pid,
            },
        )
    checks["sources_unchanged"] = manifest() == frozen
    checks["one_scripted_operator_response"] = approved and pending_request is not None
    study_path = args.output_dir / "flight/study.json"
    if study_path.is_file():
        composition = json.loads(study_path.read_text())
        returning = composition["m2_study"].get("return_study")
        approval = (
            verify_inflight_return(
                returning,
                expected_contract=contract,
                host_record=bridge.record(),
                signed_ledger_record=signed,
            )
            if returning
            else {"passed": False, "issues": ["no_ship_return_study"]}
        )
        result = combined_verdict(composition, approval, checks)
    else:
        result = {
            "schema": "missionos.starship_m3_http_fixture_smoke.v1",
            "declared_fixture_accepted": False,
            "http_and_process_checks": checks,
            "m3_completed": False,
            "retry_performed": False,
            "model_calls": 0,
        }
    save(args.output_dir / "summary.json", result)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["declared_fixture_accepted"] else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approve-simulation", action="store_true")
    p.add_argument(
        "--check-runtime",
        action="store_true",
        help="Start and check real local Gateway only; no flight or model",
    )
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--reference-prefix", type=Path)
    p.add_argument("--run-id", default="m3-http-fixture")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--contract", type=Path, help=argparse.SUPPRESS)
    for name in ("request", "grant", "telemetry"):
        p.add_argument("--" + name + "-fd", type=int, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    args.output_dir = args.output_dir.expanduser().absolute()
    if args.reference_prefix is not None:
        args.reference_prefix = args.reference_prefix.expanduser().absolute()
    if args.check_runtime:
        if (
            args.worker
            or args.approve_simulation
            or args.reference_prefix is not None
            or args.output_dir.exists()
        ):
            p.error("runtime check requires a fresh output and no flight arguments")
        return check_runtime(args)
    if args.worker:
        if not args.approve_simulation or any(
            getattr(args, key) is None
            for key in ("contract", "request_fd", "grant_fd", "telemetry_fd")
        ):
            p.error("worker requires inherited private pipes and contract")
        return worker(args)
    if not args.approve_simulation or args.reference_prefix is None or args.output_dir.exists():
        p.error("explicit simulation opt-in, reference prefix and fresh output required")
    return host(args)


if __name__ == "__main__":
    raise SystemExit(main())
