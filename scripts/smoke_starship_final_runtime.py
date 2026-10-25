#!/usr/bin/env python3
"""Opt-in final-runtime boundary smoke, with no plant or model execution.

This starts one source-bound diagnostic child on the maintained nine-channel
helper, decodes explicitly fixed responses in the real host agents, and calls
the real loopback Gateway. It is not a mission, a qualification campaign, a
native judgment or an approval/dispatch receipt.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"packages/missionos-core/src"),
    str(ROOT/"packages/missionos-cli/src"), str(ROOT/"packages/missionos-gateway/src")]

from src.runtime import starship_final_shared as shared  # noqa: E402
from src.runtime import starship_final_outer as outer  # noqa: E402
from src.runtime import starship_final_protocol as rpc  # noqa: E402
from src.runtime import starship_final_sensor_execution as sensor  # noqa: E402
from src.intelligence import starship_final_supervision_agent as supervision  # noqa: E402

SCRIPT = "scripts/smoke_starship_final_runtime.py"
COVERAGE_REVISION = 3


@contextmanager
def no_plant_or_paid_provider(*, worker=False):
    from src.runtime import starship_sixdof as dyn, starship_physics as physics
    from src.runtime import starship_final_sensor_prediction as prediction
    from src.intelligence.starship_m2_broker import DeadlineTransport
    def forbidden(*_args, **_kwargs):
        raise AssertionError("final_runtime_smoke_cannot_integrate_forecast_or_call_paid_provider")
    with patch.object(dyn, "step", forbidden), patch.object(dyn, "_rk4", forbidden), \
            patch.object(dyn, "_rhs", forbidden), patch.object(physics, "step", forbidden), \
            patch.object(prediction, "forecast_job", forbidden), patch.object(DeadlineTransport, "_invoke", forbidden):
        if worker:
            from src.runtime.starship_restored_outer import offline_worker
            with offline_worker(), patch.object(subprocess, "Popen", forbidden):
                yield
        else:
            yield


def sensor_request(stage):
    actions = [sensor.CONTINUE, sensor.TRACK] if stage == "initial" else [sensor.CONTINUE, sensor.ADOPT, sensor.DELEGATE]
    facts = {"time_s": 500., "altitude_m": 250000., "fuel_gauge_kg": 90000.,
        "accel_factor": .8, "accel_band_low": .788, "accel_band_high": .812,
        "accel_samples": 80, "accel_anomaly": True, "application_time_s": 1100.,
        "tracking_requested": stage == "final", "tracking_delay_s": 30.}
    if stage == "final":
        facts.update(tracking_factor=.8, tracking_band_low=.788, tracking_band_high=.812, tracking_intervals=40)
        for action in (sensor.CONTINUE, sensor.ADOPT):
            for label, factor in (("band_low", .788), ("point_estimate", .8), ("band_high", .812)):
                for key, value in {"sampled_factor": factor, "speed_mps": 3.5 if action == sensor.ADOPT else 9.,
                    "tilt_deg": .4, "body_rate_rad_s": .001, "reserve_kg": 58000.,
                    "limits_met": action == sensor.ADOPT}.items():
                    facts[f"{action}.{label}.{key}"] = value
    return sensor.validate_public_request({"schema": sensor.PUBLIC_SCHEMA, "run_id": "final-runtime-smoke",
        "request_id": "smoke-sensor-"+stage, "stage": stage, "issued_at_s": 500.,
        "deadline_s": 535. if stage == "initial" else 650., "scope_sha256": "c"*64,
        "facts": facts, "allowed_actions": actions, "effects": {action: sensor.EFFECTS[action] for action in actions}})


def requests():
    facts = {"time_s": 100., "source_time_s": 99., "available_time_s": 100.,
        "altitude_m": 30000., "ground_speed_mps": 2000., "propellant_kg": 100000., "body_rate_rad_s": .001}
    return [supervision.decision_request("smoke-ascent", "ascent", 100., 135., scope_sha256="c"*64, facts=facts),
        supervision.decision_request("smoke-booster", "booster", 100., 250., scope_sha256="c"*64,
            facts={**facts, "splashdown_feasible": True, "tower_ready": False, "recovery_control_available": True}),
        *[rpc.request(row["request_id"], "sensor", row["issued_at_s"], row["deadline_s"], row)
            for row in (sensor_request("initial"), sensor_request("final"))]]


def read_until(reader, deadline):
    while time.monotonic() < deadline:
        value = reader.poll()
        if value is not None:
            return value
        time.sleep(.001)
    raise TimeoutError("final_runtime_smoke_pipe_deadline")


def visual_observer_exchange(client, manifest, folder, *, deadline, service=lambda: None):
    """Two real observer/decoder/RPC exchanges, with algebraic fixture inputs.

    Rigid-release arithmetic prepares immutable test observations. No plant
    integration, forecast, native model or execution permission is involved.
    The second request must use the original episode's simulation/wall epoch,
    even though its fresh numerical counter has a later snapshot timestamp.
    """
    from src.runtime import starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import vehicle, payload_inertia
    from src.runtime.starship_sixdof_separation import release_payload
    from src.runtime.starship_final_visual import FinalVisualObserver, PENDING
    from src.runtime.starship_final_native import visual_rpc_request
    profile = shared.read(shared.relative(shared.PROFILE))
    craft = vehicle(profile)
    state = dyn.State6DOF(100., (7e6, 0., 0.), (0., 7500., 0.), dyn.IDENTITY,
        (0., 0., 0.), 45000., tuple(dyn.EngineState() for _ in craft.engines),
        tuple(0. for _ in craft.aero_panels))
    releases = []
    original_inputs = []

    def decide(envelope):
        value = visual_rpc_request(observer, envelope)
        if client.pending is None:
            client.begin(value, started_wall_s=envelope["started_wall_s"])
        shared.require(client.pending["frame"]["request"]["request_id"] == value["request_id"],
            "smoke_visual_foreign_pending_request")
        result = client.poll(state.time_s)
        return PENDING if result is None else result

    observer = FinalVisualObserver(profile, folder, run_id="final-runtime-smoke",
        plan_sha256=manifest["plan_sha256"], source_sha256=manifest["source_sha256"],
        mode="fixture", decide=decide)

    def diagnostic_scene():
        # The maintained DeploymentWorld normally integrates a copied released
        # satellite to the camera time. That is intentionally forbidden in this
        # zero-plant smoke. Only its scene callback is a named static fixture;
        # the observer, collection, PNG rendering, secure reads, host decoder,
        # inherited protocol and original-epoch projection remain production.
        observed = observer.collector.world._state
        observed_craft = observer.collector.world._vehicle
        anchor = [0., 0., profile["payload"]["center_z_m"]]
        return {"schema": "missionos.starship_deployment_world.v1", "time_s": observed.time_s,
            "attachment_body_m": anchor,
            "payload_center_body_m": [3., 0., anchor[2]],
            "payload_rotation_columns": [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
            "payload_dimensions_m": list(profile["payload"]["dimensions_m"]),
            "parent_state_sha256": shared.digest(asdict(observed)),
            "parent_vehicle_sha256": shared.digest(asdict(observed_craft)),
            "target_release_sha256": observer.collector.world._release_digests.get("satellite_13"),
            "child_state_sha256": shared.digest(None), "child_propagation_steps": 0,
            "geometry_scope": "diagnostic_synthetic_static_scene_no_child_dynamics",
            "physical_execution": False}

    observer.collector.world.snapshot = diagnostic_scene

    def tick(now):
        nonlocal state
        shared.require(time.monotonic() < deadline, "smoke_visual_pipe_deadline")
        state = replace(state, time_s=now)
        before = shared.digest({"state": asdict(state), "vehicle": asdict(craft), "releases": releases})
        observer.observe(state, craft, releases, return_deadline_s=1000.)
        after = shared.digest({"state": asdict(state), "vehicle": asdict(craft), "releases": releases})
        shared.require(before == after, "smoke_visual_observer_modified_input")
        original_inputs.append(before)
        service()

    tick(100.)
    for count in range(1, 14):
        when = 100.+count*2
        state = replace(state, time_s=when)
        after = vehicle(profile, payload_count=26-count)
        mass = profile["payload"]["mass_each_kg"]
        parent_mass = dyn.mass_properties(after, state.propellant_kg).mass_kg
        reduced = parent_mass*mass/(parent_mass+mass)
        state, child_state, child_vehicle, receipt = release_payload(state, craft, after, mass,
            (0., 0., profile["payload"]["center_z_m"]), payload_inertia(profile),
            (reduced*profile["payload"]["release_speed_mps"], 0., 0.))
        craft = after
        releases.append({"id": f"satellite_{count:02d}", "release_state": asdict(child_state),
            "vehicle": asdict(child_vehicle), "receipt": receipt})
        for offset in (0., .5, 1., 1.5):
            tick(when+offset)
    tick(128.)
    shared.require(len(observer.requests) == 1 and not observer.release_allowed(128.),
        "smoke_visual_first_request_missing")
    original_epoch = observer.original_epoch_s
    while len(observer.results) < 1:
        tick(128.)
        time.sleep(.001)
    shared.require(observer.results[0]["accepted"] is True
        and observer.observations[0]["action"] == "request_numeric_confirmation"
        and not observer.deployment_decision_ready and not observer.release_allowed(128.),
        "smoke_visual_first_result_did_not_hold_for_new_observation")
    for when in (128.5, 129., 129.5, 130., 130.5):
        tick(when)
    shared.require(len(observer.requests) == 2, "smoke_visual_fresh_followup_missing")
    while len(observer.results) < 2:
        tick(130.5)
        time.sleep(.001)
    first, followup = observer.requests
    completed = client.record()["completed"][-2:]
    shared.require(first["request"]["snapshot_time_s"] < followup["request"]["snapshot_time_s"]
        and followup["request"]["evidence_kind"] == "numeric_followup"
        and followup["request"]["manifest"]["frames"] == []
        and first["started_wall_s"] == followup["started_wall_s"]
        and first["wall_deadline_s"] == followup["wall_deadline_s"]
        and first["request"]["deadline_s"] == followup["request"]["deadline_s"],
        "smoke_visual_followup_original_epoch_changed")
    shared.require(len(completed) == 2 and all(row["frame"]["request"]["domain"] == "visual"
        and row["frame"]["request"]["time_s"] == original_epoch
        and row["frame"]["started_wall_s"] == first["started_wall_s"]
        and row["frame"]["wall_deadline_s"] == first["wall_deadline_s"] for row in completed),
        "smoke_visual_rpc_original_epoch_not_preserved")
    record = observer.record()
    shared.require(record["closed_reason"] == "accepted_visual_proposal"
        and not record["withhold_deployment"] and observer.release_allowed(130.5)
        and len(releases) == 13 and all(row["accepted"] for row in record["results"])
        and record["native_judgments"] == 0 and record["ai_visual_work_completed"] is False,
        "smoke_visual_followup_not_closed_without_native_claim")
    return {"schema": "missionos.starship_final_visual_rpc_boundary_smoke.v1",
        "passed": True, "fixture_observation_origin": "algebraic_rigid_release_not_integrated_flight",
        "camera_scene_source": "diagnostic_synthetic_static_scene_no_child_dynamics",
        "observer_inputs_unchanged": True, "observation_input_sha256": original_inputs,
        "two_requests": True, "same_original_epoch": True, "later_followup_snapshot": True,
        "new_counter_required_before_resume": True, "retained_count": 13,
        "physics_steps": 0, "provider_calls": 0, "execution_permission_granted": False,
        "observer": record}


def child(manifest_path, fds):
    from src.runtime.starship_restored_environment import worker_environment_view
    manifest = shared.read(manifest_path)
    shared.require(os.environ.get(outer.TOKEN) == manifest["plan_sha256"], "smoke_exact_child_opt_in")
    shared.require(set(worker_environment_view()) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", outer.TOKEN,
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"}, "smoke_unexpected_child_environment")
    shared.check_manifest(manifest["source_sha256"])
    shared.require(shared.environment() == manifest["backend"], "smoke_child_backend_changed")
    with no_plant_or_paid_provider(worker=True), outer.worker_channels(fds) as channels:
        deadline = time.monotonic()+40.
        for index in (0, 2, 4, 6):
            channels[outer.CHANNEL_NAMES[index]].send({"schema": "missionos.final_runtime_smoke_probe.v1", "channel": index})
        for index in (1, 3, 5):
            shared.require(read_until(channels[outer.CHANNEL_NAMES[index]], deadline) == {
                "schema": "missionos.final_runtime_smoke_probe.v1", "channel": index}, "smoke_nine_channel_probe")
        client = rpc.FinalObservationClient(channels["observation_request_writer"], channels["observation_response_reader"],
            plan_sha256=manifest["plan_sha256"], source_sha256=shared.digest(manifest["source_sha256"]))
        expected = ["continue_mission", "splashdown", sensor.TRACK, sensor.ADOPT]
        for value, action in zip(requests(), expected):
            client.begin(value)
            result = None
            while result is None and time.monotonic() < deadline:
                result = client.poll(value["time_s"])
                time.sleep(.001)
            shared.require(type(result) is dict and result["action"] == action, "smoke_fixed_decode_or_rpc")
        visual = visual_observer_exchange(client, manifest, manifest_path.parent/"camera", deadline=deadline)
        # Spend and source-binding negatives travel through the real framing;
        # neither is converted into another action or an execution permission.
        try:
            client.begin(requests()[-1])
        except ValueError:
            spent_refused = True
        else:
            raise ValueError("smoke_spent_request_reused")
        value = rpc.request("smoke-wrong-source", "ascent", 100., 135., requests()[0]["observation"])
        now = time.monotonic()
        channels["observation_request_writer"].send({"schema": rpc.REQUEST, "plan_sha256": manifest["plan_sha256"],
            "source_sha256": "f"*64, "sequence": client.sequence+1, "request": value, "request_sha256": rpc.digest(value),
            "started_wall_s": now, "wall_deadline_s": now+35.})
        while not channels["observation_request_writer"].flush() and time.monotonic() < deadline:
            time.sleep(.001)
        print(json.dumps({"passed": True, "distinct_worker_fds": len(set(fds)), "probed_channels": list(range(9)),
            "source_sha256": shared.digest(manifest["source_sha256"]), "backend_sha256": shared.digest(manifest["backend"]),
            "spent_request_refused": spent_refused, "worker_provider_calls": 0, "physics_steps": 0,
            "client": client.record(), "visual": visual, "execution_permission_granted": False}))


def gateway_smoke(folder):
    from src.runtime.starship_final_gateway import loopback_gateway, operator_http
    from src.gateway.starship_inflight_approval import PREFIX
    cases = []
    with loopback_gateway(folder) as (_service, address):
        for name, path, headers, expected in (
            ("operator_ui", PREFIX, {}, 200),
            ("missing_bearer", PREFIX+"/runs/smoke/missions/smoke/vehicles/smoke/pending", {}, 401)):
            # A browser's top-level GET navigation carries no Origin. Fetches
            # from the loaded page still use the narrow same-origin exception.
            request_headers = {} if name == "operator_ui" else {"Origin": address, "Referer": address+PREFIX,
                "Sec-Fetch-Site": "same-origin", "X-MissionOS-Operator": "starship-inflight-v1"}
            request = Request(address+path, headers={**request_headers, **headers})
            try:
                with urlopen(request, timeout=3.) as response:
                    code, body = response.status, response.read(256*1024)
            except HTTPError as error:
                code, body = error.code, error.read(256*1024)
            shared.save(folder/("http-"+name+".json"), {"scenario": name, "status": code,
                "expected_status": expected, "body_sha256": sha256(body).hexdigest(),
                "body_marker": "operator-html" if code == 200 and b"<html" in body.lower()
                    else body.decode("utf-8", errors="replace")[:512],
                "physics_steps": 0, "provider_calls": 0})
            shared.require(code == expected, "smoke_gateway_"+name)
            cases.append({"scenario": name, "status": code, "body_sha256": sha256(body).hexdigest(), "passed": True})
        code, body = operator_http(address, {"run_id": "smoke", "mission_id": "smoke", "vehicle_id": "smoke"},
            "diagnostic-token-with-no-registered-principal")
        shared.save(folder/"http-unregistered-principal.json", {"scenario": "unregistered_principal_refused",
            "status": code, "expected_status": 404, "body_sha256": shared.digest(body), "body_marker": body,
            "physics_steps": 0, "provider_calls": 0})
        shared.require(code == 404, "smoke_gateway_unknown_context_not_approved")
        cases.append({"scenario": "unregistered_principal_refused", "status": code,
            "body_sha256": shared.digest(body), "passed": True})
    return {"schema": "missionos.starship_final_gateway_boundary_smoke.v1", "passed": True,
        "cases": cases, "real_http": True, "real_human_review": False,
        "approval_granted": False, "executor_command_issued": False, "physics_steps": 0, "provider_calls": 0}


def run(folder):
    from src.runtime.starship_final_fixture import FinalFixtureTransport, visual_scripts
    from src.runtime.starship_final_visual import FinalVisualHost
    from src.intelligence.starship_visual_transport import fixture_provider
    from src.intelligence.starship_final_sensor_agent import FinalSensorAgent, verify_saved_session
    folder = Path(folder).absolute()
    shared.require(folder.resolve() == folder and folder.is_relative_to(ROOT/"output"), "smoke_output_must_be_private_root_output")
    folder.mkdir(exist_ok=False)
    source = shared.source_manifest((SCRIPT, "scripts/run_starship_final_mission.py"))
    manifest = {"schema": "missionos.starship_final_runtime_smoke_binding.v1", "source_sha256": source,
        "backend": shared.environment(), "diagnostic_only": True, "execution_authority_granted": False}
    manifest["plan_sha256"] = shared.digest(manifest)
    shared.save(folder/"source-binding.json", manifest)
    (folder/"camera").mkdir()
    stack = ExitStack()
    provider = stack.enter_context(fixture_provider(visual_scripts()))
    agents = {"supervision": supervision.FinalSupervisionAgent(mode="fixture", transport=FinalFixtureTransport()),
        "sensor": FinalSensorAgent(mode="fixture", transport=FinalFixtureTransport()),
        "visual": FinalVisualHost(folder/"camera", run_id="final-runtime-smoke",
            plan_sha256=manifest["plan_sha256"], source_sha256=source, mode="fixture",
            fixture_endpoint=provider["endpoint"])}
    def handler(value, *, remaining_s, outer_deadline_monotonic):
        if value["domain"] == "sensor":
            return agents["sensor"].assess(value["observation"], remaining_s=remaining_s,
                outer_deadline_monotonic=outer_deadline_monotonic)
        if value["domain"] == "visual":
            return agents["visual"].assess(value["observation"])
        return agents["supervision"].assess(value, remaining_s=remaining_s,
            outer_deadline_monotonic=outer_deadline_monotonic)
    pairs = [outer.approval_pipe() for _ in range(9)]
    fds = [pair[1 if index in outer.WRITE_INDICES else 0].fd for index, pair in enumerate(pairs)]
    host = rpc.FinalObservationHost(pairs[7][0], pairs[8][1], plan_sha256=manifest["plan_sha256"],
        source_sha256=shared.digest(source), handler=handler)
    process, closed, probes, source_refused = None, set(), set(), False
    try:
        for index in (1, 3, 5):
            pairs[index][1].send({"schema": "missionos.final_runtime_smoke_probe.v1", "channel": index})
        with (folder/"child.log").open("xb") as log:
            process = subprocess.Popen([sys.executable, str(ROOT/SCRIPT), "--worker", str(folder/"source-binding.json"),
                "--fds", json.dumps(fds)], cwd=ROOT, env=outer.child_environment(manifest["plan_sha256"]),
                pass_fds=fds, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        for index, pair in enumerate(pairs):
            endpoint = pair[1 if index in outer.WRITE_INDICES else 0]
            endpoint.close()
            closed.add(endpoint.fd)
        deadline = time.monotonic()+50.
        while process.poll() is None and time.monotonic() < deadline:
            for index in (0, 2, 4, 6):
                if index not in probes:
                    value = pairs[index][0].poll()
                    if value is not None:
                        shared.require(value == {"schema": "missionos.final_runtime_smoke_probe.v1", "channel": index}, "smoke_parent_probe")
                        probes.add(index)
            try:
                host.tick()
            except ValueError as error:
                shared.require(str(error) == "final_rpc_host_frame_binding", "smoke_unexpected_rpc_refusal")
                source_refused = True
            time.sleep(.001)
        shared.require(process.poll() is not None, "smoke_owned_child_timeout")
        process.wait()
        shared.require(process.returncode == 0, "smoke_owned_child_failed_see_preserved_log")
        if not source_refused:
            try:
                host.tick()
            except ValueError as error:
                shared.require(str(error) == "final_rpc_host_frame_binding", "smoke_unexpected_rpc_refusal")
                source_refused = True
        shared.require(probes == {0, 2, 4, 6} and source_refused, "smoke_nine_channels_or_source_refusal_incomplete")
        child_result = shared.parse((folder/"child.log").read_bytes())
        shared.require(child_result.get("passed") is True and child_result["distinct_worker_fds"] == 9, "smoke_child_result")
        for session in agents["sensor"].record()["sessions"]:
            verify_saved_session(session, session["request"], session["result"], "fixture")
        visual_calls = provider["requests"]
        shared.require(len(visual_calls) == 2
            and [row["image_header"] for row in visual_calls] == [True, False]
            and visual_calls[0]["image_bytes"] > 0
            and len(agents["visual"].record()["attempts"]) == 2
            and child_result["visual"]["passed"] is True,
            "smoke_two_real_loopback_visual_decodes_missing")
        shared.save(folder/"inherited-pipes.json", {"passed": True, "source_bound_parent_child": True,
            "distinct_worker_fds": 9, "all_nine_channels_probed": True, "wrong_source_refused": source_refused,
            "worker_exit_code": process.returncode, "client": child_result["client"], "host": host.record(),
            "agents": {name: agent.record() for name, agent in agents.items()}, "provider_calls": 0,
            "visual_observer": child_result["visual"], "visual_loopback_http_requests": visual_calls,
            "physics_steps": 0, "execution_permission_granted": False})
        shared.save(folder/"gateway.json", gateway_smoke(folder/"gateway"))
        shared.check_manifest(source)
        result = {"schema": "missionos.starship_final_boundary_runtime_smoke.v1", "passed": True,
            "source_sha256": source, "backend": manifest["backend"], "provider_calls": 0, "physics_steps": 0,
            "nine_channel_process_smoke": True, "actual_fixed_agent_decoders": True, "loopback_gateway": True,
            "coverage_revision": COVERAGE_REVISION, "two_visual_rpc_requests": True,
            "visual_original_epoch_preserved": True, "visual_loopback_http": True,
            "local_visual_http_requests": 2,
            "visual_scene_fixture": True, "visual_scene_child_dynamics_executed": False,
            "fixture_only": True, "flight_result": False, "qualification_refreshed": False,
            "execution_permission_granted": False, "automatic_retry": False}
        shared.save(folder/"summary.json", result)
        return result
    finally:
        stack.close()
        if process is not None and process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2.)
        for pair in pairs:
            for endpoint in pair:
                if endpoint.fd not in closed:
                    endpoint.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicit zero-plant boundary smoke opt-in")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fds", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker is not None:
        child(args.worker, json.loads(args.fds))
    else:
        shared.require(args.run and args.output is not None, "smoke_requires_explicit_run_and_new_output")
        with no_plant_or_paid_provider():
            result = run(args.output)
        print(json.dumps({key: value for key, value in result.items() if key not in ("source_sha256", "backend")}, indent=2))


if __name__ == "__main__":
    main()
