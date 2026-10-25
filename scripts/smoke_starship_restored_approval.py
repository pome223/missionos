"""Real loopback Gateway approval / pipe / reservation smoke, no model or plant.

The closed origin and operator are synthetic fixtures. A confirmed reservation
is not a dispatched burn, physical return, qualified route or human review.
"""
import argparse
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def synthetic_origin():
    """Analytic retained-26 DTO, never initialized by a flight or saved run."""
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime import starship_restored_contract as contract
    from src.runtime import starship_sixdof_mission as mission
    from src.runtime.starship_ascent import (AscentControlOptions, AscentCoastOrbitObjectives,
        AscentScreenSettings, StateInsertionOptions)
    from src.runtime.starship_state_mission import ExperimentalStateMissionAscentSettings
    from src.runtime.starship_booster_terminal_control import BoosterTerminalRecoverySettings

    profile_raw = (Path(__file__).resolve().parents[1]/
        "examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    profile = json.loads(profile_raw)
    source = {name: "1"*64 for name in contract.CRITICAL_SOURCES}
    source["examples/spaceflight/starship-sixdof-profile.json"] = contract.PROFILE_SHA256
    settings = ExperimentalStateMissionAscentSettings(AscentScreenSettings(policy="state_insertion_v7",
        objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
        control_options=AscentControlOptions(True, False), insertion_options=StateInsertionOptions()))
    control = BoosterTerminalRecoverySettings(powered_terminal_alignment=False, shutdown_tail_only=True,
        low_pressure_reference_capture=True, powered_reference_capture=True, residual_authority_capture=True,
        energy_terminal_descent=True, planned_capture_impulse_compensation=True, terminal_horizontal_braking=True)
    craft = mission.vehicle(profile, payload_count=26)
    radius = env.EARTH_EQUATORIAL_RADIUS_M+300000.
    state = dyn.State6DOF(1800., (radius, 0., 0.), (0., math.sqrt(env.EARTH_MU_M3_S2/radius), 0.),
        dyn.IDENTITY, (.0001, .0002, 0.), 81000.,
        tuple(dyn.EngineState(throttle=.00001 if i == 0 else 0.,
            gimbal_x_rad=.001 if i == 0 else 0.) for i in range(len(craft.engines))),
        tuple(0. for _ in craft.aero_panels))
    def native(value):
        return json.loads(_encode(value))
    cutoff = native(asdict(state))
    state = replace(state, time_s=1805., propellant_kg=80000.)
    settled = native(asdict(state))
    state = replace(state, time_s=1810., propellant_kg=79999.)
    current = native(asdict(state))
    record = {"schema": contract.RECORD_SCHEMA, "source_sha256": source,
        "profile_sha256": contract.PROFILE_SHA256,
        "experimental_mission_ascent": {"schema": "missionos.experimental_state_mission_ascent.v1",
            "settings": settings.record(), "cutoff_measurement": {"time_s": 1800., "state": cutoff,
                "gate_reached": True}, "insertion_shutdown_until_s": 1805.,
            "settled_measurement": {"time_s": 1805., "state": settled,
                "vehicle": native(asdict(craft)), "orbit": native(env.orbital_elements(
                    env.State3D(1805., tuple(settled["r_eci_m"]), tuple(settled["v_eci_mps"]), 80000.))),
                "gate_reached": True}, "schedule": {"final_gate_time_s": 1800.,
                    "first_release_time_s": 1845., "return_request_time_s": 3600.}},
        "booster_control_settings": control.record(), "phase": "orbital_coast", "final_state": current,
        "final_vehicle": native(asdict(craft)), "state_history": [
            {"state": settled, "retained_count": 26}, {"state": current, "retained_count": 26}],
        "inventory": {"schema": contract.INVENTORY_SCHEMA, "total_count": 26, "released_count": 0,
            "retained_count": 26, "generation": 0, "sequencer_state": "skipped", "pending_action": None,
            "observed_time_s": state.time_s, "release_receipts": []}}
    raw = _encode(record)
    return contract.ActualRestoredOrigin(sha256(raw).hexdigest(), raw, profile_raw, source), source


class Clock:
    def __init__(self):
        self.now = 1000.

    def __call__(self):
        return self.now


class Scenario:
    """Test-only trusted telemetry and reservation callbacks, no executor."""

    def __init__(self, name, directory, origin, source, service, clock):
        from src.runtime.starship_inflight_approval import digest
        from src.runtime.starship_inflight_runtime import approval_pipe
        from src.runtime.starship_restored_approval import (
            RestoredApprovalBinding, RestoredApprovalHost, RestoredApprovalWorker,
        )
        self.clock, self.updates, self.admissions = clock, [], []
        self.candidate = digest({"fixture_candidate": name, "origin_sha256": digest(origin.record()),
            "burn_time_s": origin.record()["schedule"]["return_request_time_s"]+origin.record()["orbit"]["period_s"]})
        self.bound = RestoredApprovalBinding(origin, context={"run_id": name,
            "mission_id": "restored-smoke", "vehicle_id": "ship"}, plan_sha256="b"*64,
            expected_source_sha256=source, started_wall_s=clock())
        record = self.bound.record()
        self.state = {"run_active": True, "simulation_time_s": origin.time_s, "revision": 0,
            "scope": record["initial_scope"], "restored_origin_sha256": record["restored_origin_sha256"],
            "reservation_sha256": digest({"fixture_nominal_reservation": name,
                "origin_sha256": record["restored_origin_sha256"]})}
        self.observe()
        self.check_candidate = self.candidate
        self.fallback_accepted = True
        request_reader, request_writer = approval_pipe()
        grant_reader, grant_writer = approval_pipe()
        self.endpoints = (request_reader, request_writer, grant_reader, grant_writer)
        self.host = RestoredApprovalHost(self.bound, ledger_path=directory/(name+".sqlite"),
            signing_key=b"synthetic-smoke-only-key-not-live-32bytes", operator_token=TOKEN,
            operator_id="scripted-smoke-operator", initial_reservation_sha256=self.state["reservation_sha256"],
            snapshot=lambda: deepcopy(self.state), request_reader=request_reader, grant_writer=grant_writer,
            service=service, clock=clock)
        self.worker = RestoredApprovalWorker(self.bound,
            initial_reservation_sha256=self.state["reservation_sha256"], request_writer=request_writer,
            grant_reader=grant_reader, admission=self.admission, command=self.command, clock=clock)

    def observe(self, advance=0.):
        from src.runtime.starship_inflight_approval import digest
        self.clock.now += advance
        self.state["simulation_time_s"] += advance
        self.state["observation"] = {"state_sha256": digest({k: v for k, v in self.state.items()
            if k != "observation"}), "simulation_time_s": self.state["simulation_time_s"],
            "wall_time_s": self.clock()}

    def admission(self, action, state):
        self.admissions.append({"action": action, "observation": deepcopy(state["observation"])})
        return {"accepted": self.fallback_accepted if action == "keep_reserved_return" else True,
            "candidate_sha256": self.check_candidate, "fixture_fresh_check": True}

    def command(self, action, state, consumption):
        from src.runtime.starship_inflight_approval import digest
        self.updates.append({"action": action, "observation": deepcopy(state["observation"]),
            "consumption_sha256": consumption["sha256"]})
        self.state.update(revision=consumption["revision"], scope=deepcopy(consumption["scope"]),
            reservation_sha256=digest({"candidate": self.candidate, "revision": consumption["revision"]}))
        return {"applied": True, "revision": self.state["revision"],
            "reservation_sha256": self.state["reservation_sha256"]}

    def request(self):
        self.worker.request_change(request_id="change1", candidate_sha256=self.candidate, state=self.state)
        self.host.tick()
        assert self.host.bridge.request is not None

    def body(self):
        request = self.host.bridge.request
        return {"request_sha256": request["sha256"],
            "observed_state_sha256": request["observed_state_sha256"], "candidate_sha256": self.candidate}

    def path(self):
        from src.gateway.starship_inflight_approval import PREFIX
        return PREFIX+"/runs/"+self.bound.record()["context"]["run_id"]+"/missions/restored-smoke/vehicles/ship"

    def close(self):
        self.host.close()
        for endpoint in self.endpoints:
            endpoint.close()


TOKEN = "synthetic-http-smoke-principal-not-a-live-secret"


def run_smoke(output_dir):
    """Run once in an exclusive directory; models / plant remain forbidden."""
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime import starship_sixdof_mission as mission
    from contextlib import ExitStack
    output_dir.mkdir(parents=True, exist_ok=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    configuration = {"MISSIONOS_LLM_BACKEND": "off", "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED": "0",
        "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED": "0", "MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED": "0",
        "GATEWAY_API_KEY": "", "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port),
        "TASK_STORE_DB_PATH": str(output_dir/"tasks.db"), "MEMORY_DB_PATH": str(output_dir/"memory.db"),
        "AUDIT_LOG_PATH": str(output_dir/"audit.log")}
    counters = {"forbidden_calls": 0}

    def forbidden(*_a, **_kw):
        counters["forbidden_calls"] += 1
        raise AssertionError("Approval smoke forbids physics, controllers and model providers")

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, configuration))
        for owner, name in ((dyn, "step"), (dyn, "_rhs"), (dyn, "_rk4"), (env, "step"),
                (mission, "advance_plant"), (mission, "control")):
            stack.enter_context(patch.object(owner, name, forbidden))
        from src.config.settings import reset_settings
        reset_settings()
        from src.gateway import server
        from src.gateway import starship_inflight_approval as routes
        from src.intelligence import starship_m2_broker
        stack.enter_context(patch.object(starship_m2_broker, "_send_once", forbidden))
        clock = Clock()
        service = routes.InflightApprovalService(clock=clock)
        stack.enter_context(patch.object(routes, "_SERVICE", service))
        import uvicorn
        gateway = server.create_missionos_gateway()
        http = uvicorn.Server(uvicorn.Config(gateway.app, host="127.0.0.1", port=port,
            log_level="error", lifespan="off"))
        thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic()+15
        while not http.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert http.started, "gateway_did_not_start"
        address = f"http://127.0.0.1:{port}"
        checks, transcript, entries = {}, [], []

        def call(path, body=None, authenticated=True):
            headers = {"Origin": address, "Referer": address+routes.PREFIX,
                "Sec-Fetch-Site": "same-origin", "X-MissionOS-Operator": "starship-inflight-v1"}
            if authenticated:
                headers["Authorization"] = "Bearer "+TOKEN
            if body is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(address+path, data=_encode(body) if body is not None else None,
                headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    status, value = response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                status, value = exc.code, json.load(exc)
            transcript.append({"path": path, "method": "POST" if body is not None else "GET",
                "request_body": body, "status": status, "response_body": value,
                "authenticated_scripted_principal": authenticated})
            return status, value

        origin, source = synthetic_origin()

        def make(name):
            value = Scenario(name, output_dir, origin, source, service, clock)
            entries.append(value)
            value.request()
            return value

        try:
            approve = make("approve")
            path = approve.path()
            code, pending = call(path+"/pending")
            checks["candidate_bound_pending"] = code == 200 and pending["resolvable"] is True \
                and pending["request"]["candidate_sha256"] == approve.candidate
            checks["no_operator_token_rejected"] = call(path+"/pending", authenticated=False)[0] == 401
            checks["wrong_context_rejected"] = call(path.replace("restored-smoke", "wrong-mission")+"/pending")[0] == 404
            checks["missing_candidate_rejected"] = call(path+"/requests/change1/approve",
                {k: v for k, v in approve.body().items() if k != "candidate_sha256"})[0] == 400
            checks["mismatched_candidate_rejected"] = call(path+"/requests/change1/approve",
                approve.body() | {"candidate_sha256": "0"*64})[0] == 409
            code, response = call(path+"/requests/change1/approve", approve.body())
            checks["approval_is_not_command"] = code == 200 and response["executor_command_issued"] is False \
                and not approve.updates
            checks["http_replay_rejected"] = call(path+"/requests/change1/approve", approve.body())[0] == 409
            approve.host.tick()
            result = approve.worker.tick(approve.state)
            checks["fresh_admission_then_one_reservation_change"] = result["reservation_changed"] is True \
                and len(approve.updates) == 1 and len(approve.admissions) == 1
            checks["requires_later_observation"] = approve.worker.tick(approve.state) is None \
                and approve.worker.status == "reservation_changed_waiting_observation"
            approve.observe(.1)
            approve.worker.tick(approve.state)
            checks["later_fresh_observation_confirms_reservation"] = approve.worker.status == "reservation_confirmed"
            approve.worker.tick(approve.state)
            checks["worker_does_not_reapply"] = len(approve.updates) == 1
            from src.runtime.starship_inflight_approval import InflightApprovalError
            try:
                approve.host.ledger.consume(request_id="change1", mission_id="restored-smoke", vehicle_id="ship",
                    revision=0, scope=approve.bound.record()["initial_scope"], observation=approve.state["observation"],
                    simulation_time_s=approve.state["simulation_time_s"], run_active=True)
                checks["ledger_consumption_one_use"] = False
            except InflightApprovalError as exc:
                checks["ledger_consumption_one_use"] = str(exc) == "request_already_consumed"
            reject = make("reject")
            checks["http_rejection"] = call(reject.path()+"/requests/change1/reject", reject.body())[0] == 200
            reject.host.tick()
            reject.worker.tick(reject.state)
            checks["rejection_preserves_reservation"] = reject.worker.status == "fallback_preserved" and not reject.updates
            stale = make("changed-candidate")
            checks["candidate_initially_approved"] = call(stale.path()+"/requests/change1/approve", stale.body())[0] == 200
            stale.check_candidate = "c"*64
            stale.host.tick()
            stale.worker.tick(stale.state)
            checks["fresh_candidate_change_blocks_reservation"] = stale.worker.status == "fallback_preserved" and not stale.updates
            unsafe = make("unsafe-fallback")
            call(unsafe.path()+"/requests/change1/reject", unsafe.body())
            unsafe.fallback_accepted = False
            unsafe.host.tick()
            unsafe.worker.tick(unsafe.state)
            checks["fallback_still_needs_fresh_admission"] = unsafe.worker.status == "fallback_not_admitted" and not unsafe.updates
            expire = make("expire")
            clock.now = expire.bound.record()["bounds"]["wall_deadline_s"]+.1
            expire.observe()
            checks["expired_http_approval_rejected"] = call(expire.path()+"/requests/change1/approve", expire.body())[0] == 409
            expire.host.tick()
            expire.worker.tick(expire.state)
            checks["expiry_does_not_change_reservation"] = expire.worker.status == "fallback_preserved" and not expire.updates
            checks["forbidden_physics_controller_provider_calls_zero"] = counters["forbidden_calls"] == 0
            evidence = {"schema": "missionos.starship_restored_approval_smoke.v1",
                "passed": all(checks.values()), "checks": checks, "real_gateway_http": True,
                "inherited_approval_pipes": True, "synthetic_closed_origin": True,
                "deadline_clock": "controlled_fixture_monotonic_not_elapsed_performance",
                "source_manifest_is_synthetic_fixture": True, "operator_response_source": "scripted_test_driver",
                "human_review_verified": False, "browser_interaction_verified": False,
                "sso_identity_verified": False, "model_calls": 0, "physics_steps": 0,
                "burn_dispatch_issued": False, "physical_return_verified": False,
                "qualification_refreshed": False, "forbidden_call_counters": counters,
                "implementation_source_sha256": {name: sha256((Path(__file__).resolve().parents[1]/name)
                    .read_bytes()).hexdigest() for name in (
                        "src/runtime/starship_restored_approval.py", "src/runtime/starship_restored_contract.py",
                        "src/runtime/starship_inflight_runtime.py", "src/runtime/starship_inflight_approval.py",
                        "src/gateway/starship_inflight_approval.py", "src/gateway/server.py",
                        "scripts/smoke_starship_restored_approval.py")},
                "cases": [{"host": item.host.record(), "worker": item.worker.record(),
                    "admissions": item.admissions, "reservation_updates": item.updates} for item in entries],
                "http": transcript}
            (output_dir/"evidence.json").write_bytes(_encode(evidence)+b"\n")
            summary = {k: v for k, v in evidence.items() if k not in {"cases", "http"}}
            (output_dir/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
            return summary
        finally:
            http.should_exit = True
            thread.join(timeout=5)
            sock.close()
            for item in entries:
                item.close()
            reset_settings()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = run_smoke(args.output_dir)
    print(json.dumps(result))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
