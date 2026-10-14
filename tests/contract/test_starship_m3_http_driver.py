"""Driver bounds and transport helpers; no flight, forecast or provider call."""

import importlib.util
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import ModuleType

import pytest

SPEC = importlib.util.spec_from_file_location(
    "m3_smoke", Path(__file__).resolve().parents[2] / "scripts/smoke_starship_m3_http_flight.py"
)
# Staging tests live under tests/contract, matching the repository layout.
if not Path(SPEC.origin).is_file():
    SPEC = importlib.util.spec_from_file_location(
        "m3_smoke",
        Path(__file__).resolve().parents[2].parent / "scripts/smoke_starship_m3_http_flight.py",
    )
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def frame(sequence=0, simulation=1000.0, wall=10.0):
    return {
        "schema": "missionos.m3_executor_telemetry.v1",
        "sequence": sequence,
        "state": {
            "run_active": True,
            "revision": 0,
            "scope": {"commands": ["select_nominal"], "resource_limits": {}},
            "simulation_time_s": simulation,
            "reservation_sha256": "a" * 64,
            "reservation": None,
            "observation": {
                "state_sha256": "b" * 64,
                "simulation_time_s": simulation,
                "wall_time_s": wall,
            },
        },
    }


def test_snapshot_history_is_only_observed_pipe_state_not_caller_mutable():
    t = smoke.Telemetry()
    first = frame()
    t.accept(first)
    first["state"]["revision"] = 99
    for i in range(1, 70):
        t.accept(frame(i, 1000.0 + i, 10.0 + i))
    state = t.snapshot()
    assert state["revision"] == 0 and len(state["known_observations"]) == 64
    state["scope"]["commands"].append("arbitrary")
    assert "arbitrary" not in t.snapshot()["scope"]["commands"]


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": "request-not-telemetry", "sequence": 0, "state": {}},
        frame(2),
        frame(True),
        frame(1, 999.0, 9.0),
    ],
)
def test_replayed_request_or_regressed_telemetry_is_rejected(bad):
    t = smoke.Telemetry()
    t.accept(frame())
    with pytest.raises(ValueError):
        t.accept(bad)


def test_clean_worker_environment_does_not_forward_host_secrets(monkeypatch):
    for name in (
        "DEEPSEEK_API_KEY",
        "GATEWAY_API_KEY",
        "OPERATOR_TOKEN",
        "SIGNING_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, "must-not-pass")
    values = smoke.clean_environment(Path("/test/source"))
    assert all("must-not-pass" != value for value in values.values())
    assert values["MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED"] == "0"


def test_separate_case_acceptance_and_failure_outcome_are_preserved():
    composition = {
        "same_state_continuation_checked": True,
        "m2_study": {
            "handoff": {"released_count": 26},
            "return_study": {"dispatch": {"candidate_id": "next_orbit"}},
        },
        "return_verification": {
            "case_accepted": False,
            "return_verification": {"outcome": "contact_and_recovery_area_met"},
        },
        "booster_outcome": {"simulated_contact_envelope_met": False},
    }
    approval = {"passed": True, "scope_amendment_applied": True, "reservation_observed": True}
    result = smoke.combined_verdict(composition, approval, {"checks": True})
    assert result["declared_fixture_accepted"] is True
    assert (
        result["m2_normal_case_accepted"]
        is result["m3_completed"]
        is result["mission_completed"]
        is False
    )
    assert result["booster_outcome"]["simulated_contact_envelope_met"] is False
    for name in approval:
        changed = {**approval, name: False}
        assert (
            smoke.combined_verdict(composition, changed, {"checks": True})[
                "declared_fixture_accepted"
            ]
            is False
        )


def test_http_helper_uses_real_network_without_logging_bearer(monkeypatch):
    fake = ModuleType("src.gateway.starship_inflight_approval")
    fake.PREFIX = "/missionos/starship/inflight"
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"request_already_resolved"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = smoke.http_call(
            f"http://127.0.0.1:{server.server_port}",
            "/approve",
            "test-only-bearer",
            {"request_sha256": "a" * 64},
        )
        assert status == 409 and "test-only-bearer" not in json.dumps(body)
        assert received == [("/approve", "Bearer test-only-bearer", {"request_sha256": "a" * 64})]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_no_flight_without_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "host", lambda _: pytest.fail("no flight allowed"))
    with pytest.raises(SystemExit):
        smoke.main(["--output-dir", str(tmp_path / "result")])
    assert not (tmp_path / "result").exists()


def test_one_attempt_numerical_budget_is_fixed():
    assert smoke.BUDGET == {
        "attempts": 1,
        "model_calls": 0,
        "maximum_forecasts": 20,
        "maximum_workers": 2,
        "wall_time_s": 2400.0,
        "retry_performed": False,
        "maximum_output_bytes": 1_200_000_000,
        "minimum_free_bytes": 1_073_741_824,
    }


def test_driver_candidate_body_matches_production_gateway_contract(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.gateway.starship_inflight_approval import (
        InflightApprovalService,
        PREFIX,
        build_inflight_approval_router,
    )
    from src.runtime.starship_inflight_approval import InflightApprovalLedger

    token = "fixture-only-local-operator-bearer-token"
    scope = {
        "commands": ["select_nominal", "keep_reserved_return"],
        "resource_limits": {"maximum_return_delay_s": 6000.0},
    }
    registry = {
        "commands": [*scope["commands"], "select_next_orbit"],
        "resource_limits": scope["resource_limits"],
    }
    observation = {"state_sha256": "a" * 64, "simulation_time_s": 10.0, "wall_time_s": 100.0}
    ledger = InflightApprovalLedger(
        tmp_path / "ledger.sqlite",
        signing_key=b"fixture-integrity-key-32bytes-only",
        operator_token=token,
        operator_id="fixture-operator",
        registry=registry,
        clock=lambda: 100.0,
    )
    request = ledger.create(
        request_id="request1",
        mission_id="mission1",
        vehicle_id="ship",
        revision=0,
        scope=scope,
        delta={"add_commands": ["select_next_orbit"], "resource_limits": {}},
        observation=observation,
        simulation_time_s=10.0,
        simulation_deadline_s=30.0,
        wall_deadline_s=120.0,
        fallback="keep_reserved_return",
        candidate_sha256="b" * 64,
    )
    state = {
        "run_active": True,
        "simulation_time_s": 10.0,
        "revision": 0,
        "scope": scope,
        "observation": observation,
        "pending_request_id": "request1",
    }
    service = InflightApprovalService(clock=lambda: 100.0)
    service.register_run(
        run_id="run1",
        mission_id="mission1",
        vehicle_id="ship",
        ledger=ledger,
        snapshot=lambda: state,
    )
    app = FastAPI()
    app.include_router(build_inflight_approval_router(service))
    base = PREFIX + "/runs/run1/missions/mission1/vehicles/ship"
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + token}
        pending = client.get(base + "/pending", headers=headers).json()
        body = smoke.approval_body(pending)
        assert body["candidate_sha256"] == request["candidate_sha256"]
        legacy = {k: v for k, v in body.items() if k != "candidate_sha256"}
        rejected = client.post(base + "/requests/request1/approve", headers=headers, json=legacy)
        assert (
            rejected.status_code == 400
            and rejected.json()["detail"] == "displayed_request_hashes_required"
        )
        assert (
            client.post(
                base + "/requests/request1/approve",
                headers=headers,
                json={**body, "candidate_sha256": "c" * 64},
            ).status_code
            == 409
        )
        accepted = client.post(base + "/requests/request1/approve", headers=headers, json=body)
        assert accepted.status_code == 200 and accepted.json()["executor_command_issued"] is False
        assert ledger.status("request1")["consumption"] is None
        assert ledger.status("request1")["receipt"]["candidate_sha256"] == "b" * 64


@pytest.mark.parametrize(
    "elapsed,old_limit", [(0.0, False), (100.0, False), (599.0, False), (0.0, True)]
)
def test_driver_fixed_deadlines_pass_real_worker_request_and_host_ledger(
    tmp_path, elapsed, old_limit
):
    from copy import deepcopy
    from src.runtime.starship_replanning import digest
    from src.runtime.starship_inflight_approval import InflightApprovalLedger
    from src.runtime.starship_inflight_runtime import (
        approval_pipe,
        HostApprovalBridge,
        WorkerApprovalActor,
    )

    profile = {"guidance": {"coast_before_return_s": 1800.0}}
    authority = {
        "profile_sha256": digest(profile),
        "checkpoint_time_s": 1000.7,
        "return_authority": {
            "mode": "fixture",
            "maximum_delay_s": 6000.0,
            "maximum_forecasts": 20,
            "maximum_workers": 2,
            "event_supervision": {"deadline_margin_s": 60.0},
        },
    }
    reference = {
        "profile": profile,
        "authority": {"case": "normal"},
        "prefix_run": {"events": [{"event": "orbit_cutoff_command", "time_s": 480.7}]},
    }
    contract = smoke.inflight_contract(authority, reference, "deadline-test", 100.0)
    assert contract["bounds"]["wall_deadline_s"] == 1300.0
    assert contract["bounds"]["simulation_deadline_s"] == 2200.7
    if old_limit:
        contract["bounds"]["wall_deadline_s"] = 1900.0
    now = 100.0 + elapsed
    state = {
        "run_active": True,
        "simulation_time_s": 1000.7 + elapsed,
        "revision": 0,
        "scope": deepcopy(contract["initial_scope"]),
        "reservation_sha256": digest(None),
        "observation": {
            "state_sha256": "a" * 64,
            "simulation_time_s": 1000.7 + elapsed,
            "wall_time_s": now,
        },
    }
    token = "fixture-only-local-operator-bearer-token"
    registry = deepcopy(contract["initial_scope"])
    registry["commands"].append("select_next_orbit")
    ledger = InflightApprovalLedger(
        tmp_path / "deadline.sqlite",
        signing_key=b"fixture-integrity-key-32bytes-only",
        operator_token=token,
        operator_id="fixture-operator",
        registry=registry,
        clock=lambda: now,
    )
    request_reader, request_writer = approval_pipe()
    grant_reader, grant_writer = approval_pipe()
    try:
        worker = WorkerApprovalActor(
            context=contract["context"],
            scope=contract["initial_scope"],
            revision=0,
            request_writer=request_writer,
            grant_reader=grant_reader,
            bounds=contract["bounds"],
            admission=lambda *a: {"accepted": True},
            command=lambda *a: pytest.fail("no execution in this test"),
            clock=lambda: now,
        )
        host = HostApprovalBridge(
            ledger=ledger,
            context=contract["context"],
            snapshot=lambda: deepcopy(state),
            request_reader=request_reader,
            grant_writer=grant_writer,
            bounds=contract["bounds"],
            clock=lambda: now,
        )
        sent = worker.request_change(request_id="request1", candidate_sha256="b" * 64, state=state)
        host.tick()
        if old_limit:
            assert host.request is None and host.finished is True
            assert host.result["reason"] == "invalid_worker_scope_request"
        else:
            stored = ledger.status("request1")["request"]
            assert host.request == stored and stored["candidate_sha256"] == "b" * 64
            assert (
                stored["wall_deadline_s"]
                == sent["wall_deadline_s"]
                == contract["bounds"]["wall_deadline_s"]
            )
            assert (
                stored["simulation_deadline_s"]
                == sent["simulation_deadline_s"]
                == contract["bounds"]["simulation_deadline_s"]
            )
    finally:
        for pipe in (request_reader, request_writer, grant_reader, grant_writer):
            pipe.close()


@pytest.mark.parametrize("verified,code", [(True, 0), (False, 2)])
def test_worker_reports_failed_composition_as_nonzero(tmp_path, monkeypatch, verified, code):
    import os
    from types import SimpleNamespace
    from src.runtime import starship_m3_fixture
    monkeypatch.setattr(starship_m3_fixture, "run_fixture", lambda *a, **k: {
        "same_state_continuation_checked": verified})
    contract = tmp_path / "contract.json"
    contract.write_text("{}")
    request_read, request_write = os.pipe()
    grant_read, grant_write = os.pipe()
    telemetry_read, telemetry_write = os.pipe()
    args = SimpleNamespace(contract=contract, output_dir=tmp_path, run_id="exit-status",
        request_fd=request_write, grant_fd=grant_read, telemetry_fd=telemetry_write)
    try:
        assert smoke.worker(args) == code
        record = json.loads((tmp_path / "worker-result.json").read_text())
        assert record["same_state_continuation_checked"] is verified
        assert record["model_calls"] == 0 and record["m3_completed"] is False
    finally:
        for fd in (request_read, grant_write, telemetry_read):
            os.close(fd)
