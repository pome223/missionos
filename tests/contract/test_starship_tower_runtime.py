"""Opt-in runtime plumbing; fixture clocks/states, no physical integration."""
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control
from src.runtime import starship_tower_runtime as runtime
from src.runtime.starship_return_sites import ReturnSites

RUN = "a"*32
KEY = b"k"*32  # Public fixture integrity material, not a provider credential.
SESSION = "starship-operator-"+"d"*24


def public_inputs():
    profile = json.loads((control.REPO/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((control.REPO/"examples/spaceflight/starship-catch-profile.json").read_text())
    sites = ReturnSites.from_dict(json.loads((control.REPO/runtime.SITES_PATH).read_text()), profile=profile, catch_config=catch)
    return profile, catch, sites


def future_plan(service, session=SESSION):
    service.plan(session, "Starship sixdof_launch_catch", expected_scenario="sixdof_launch_catch")
    _, _, sites = public_inputs()
    with service._lock():
        stored = service._load(session)
        plan = stored["plan"]
        plan["tower_supervision"] = control.tower_contract(return_sites_sha256=sites.sha256)
        plan["simulation"]["booster_policy"] = runtime.POLICY_ID
        plan["tower_runtime"] = runtime.runtime_contract(profile_sha256=plan["simulation"]["profile_sha256"],
            catch_profile_sha256=plan["simulation"]["catch_profile_sha256"], return_sites_sha256=sites.sha256)
        plan["source_sha256"] = control._plan_sources(plan)
        plan["sha256"] = control._digest({k: v for k, v in plan.items() if k != "sha256"})
        service._save(session, stored)
    return plan


@pytest.fixture
def service(tmp_path):
    return control.StarshipMissionService(tmp_path/"state", planner=lambda text: plan_starship_request(text, mode="fixture"))


def body(time_s=153.9):
    return {"time_s": time_s, "r_eci_m": [6378137., 0., 20000.], "v_eci_mps": [1., 2., 3.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 78000.,
        "engine_states": [{"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": True} for _ in range(45)],
        "flap_angles_rad": [0.]*6}


def hook(service, tmp_path):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    with service._lock():
        stored = service._load(SESSION)
        approval = stored["approval"]
        approval["consumed_by_run"] = RUN
        approval["consumed_at_epoch_s"] = service.clock()
        approval["execution_deadline_epoch_s"] = approval["consumed_at_epoch_s"]+900.
        approval["signature"] = service._signature({k: v for k, v in approval.items() if k != "signature"})
    profile, catch, sites = public_inputs()
    clock = SimpleNamespace(now=1000., sleeps=[], current=True)
    def sleep(duration):
        clock.sleeps.append(duration)
        clock.now += duration
    (tmp_path/"run").mkdir(mode=0o700, parents=True)
    context = runtime.TowerWorkerContext(tmp_path/"run", plan, approval, RUN, profile, catch, sites,
        local_integrity_key=KEY, source_is_current=lambda: clock.current, clock=lambda: clock.now, sleep=sleep)
    return context, clock


def test_existing_catalog_and_launch_run_have_no_new_authority(service):
    before = tuple(control.CATALOG)
    state = service.plan(SESSION, "Starship sixdof_launch_catch", expected_scenario="sixdof_launch_catch")
    assert "tower_runtime" not in state["plan"] and "tower_supervision" not in state["plan"]
    assert runtime.POLICY_ID not in control.CATALOG and tuple(control.CATALOG) == before


def test_closed_runtime_contract_bound_to_source_and_separate_approval(service):
    plan = future_plan(service)
    before = deepcopy(plan)
    contract = runtime.validate_runtime_contract(plan["tower_runtime"], plan)
    assert contract["maximum_jev_calls"] == contract["maximum_llm_calls"] == 0
    assert contract["state_reset_allowed"] is False and plan == before
    approved = service.approve(SESSION, plan["id"], plan["sha256"])
    assert approved["approval"]["tower_runtime"] == contract
    assert approved["approval"]["scope"] == control.TOWER_GRANT_SCOPE
    assert set(runtime.RUNTIME_SOURCES).issubset(plan["source_sha256"])


@pytest.mark.parametrize("field,value", [("mode", "live"), ("maximum_jev_calls", 1),
    ("state_reset_allowed", True), ("maximum_wall_time_s", float("inf")), ("unbounded_callback", "eval")])
def test_runtime_contract_rejects_authority_or_schema_mutation(service, field, value):
    plan = future_plan(service)
    plan["tower_runtime"][field] = value
    with pytest.raises(ValueError):
        runtime.validate_runtime_contract(plan["tower_runtime"], plan)


@pytest.mark.parametrize("change", ["profile", "sites", "policy", "duration", "scenario"])
def test_runtime_contract_rejects_cross_input_binding(service, change):
    plan = future_plan(service)
    if change == "profile":
        plan["simulation"]["profile_sha256"] = "f"*64
    elif change == "sites":
        plan["tower_supervision"]["return_sites_sha256"] = "f"*64
    elif change == "policy":
        plan["simulation"]["booster_policy"] = "fixed_v1"
    elif change == "duration":
        plan["simulation"]["duration_override_s"] = 1.
    else:
        plan["simulation"]["scenario"] = "gimbal_step"
    with pytest.raises(runtime.TowerRuntimeError, match="binding_mismatch"):
        runtime.validate_runtime_contract(plan["tower_runtime"], plan)


def test_runtime_off_does_not_consume_approval_or_spawn(service, monkeypatch):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.delenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", raising=False)
    with pytest.raises(control.StarshipMissionError, match="explicit_fixture"):
        service.execute(SESSION, plan["id"], plan["sha256"])
    assert service.current(SESSION)["approval"]["consumed_by_run"] is None
    assert not (service.root/"tower-active-run.json").exists()


def test_foreign_global_lease_prevents_spawn_and_consumption(service, monkeypatch):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    control._write(service.root/"tower-active-run.json", {"unknown": "ownership"})
    with pytest.raises(control.StarshipMissionError, match="another_tower_run"):
        service.execute(SESSION, plan["id"], plan["sha256"])
    assert service.current(SESSION)["approval"]["consumed_by_run"] is None


def test_spawn_failure_consumes_once_and_releases_only_own_reservation(service, monkeypatch):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    def fail(*args, **kwargs):
        lease = control._read(service.root/"tower-active-run.json")
        assert lease["schema"] == "missionos.starship_tower_run_reservation.v1"
        assert lease["actor_pid"] is None
        assert not set(runtime.PROVIDER_NAMES) & set(kwargs["env"])
        raise OSError("fixture spawn denial")
    monkeypatch.setattr(control.subprocess, "Popen", fail)
    state = service.execute(SESSION, plan["id"], plan["sha256"])
    assert state["status"] == "failed" and state["approval"]["consumed_by_run"] is not None
    assert state["execution"]["failure_reason"] == "worker_spawn_failed"
    assert not (service.root/"tower-active-run.json").exists()
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval"):
        service.execute(SESSION, plan["id"], plan["sha256"])


def test_activation_signed_fresh_after_planning_without_state_mutation(service, tmp_path):
    context, clock = hook(service, tmp_path)
    physical = body()
    original = deepcopy(physical)
    scope = context.activate(physical, phase="recovery_boostback_burn")
    assert physical == original and context.actor.receipt()["state_assigned"] is False
    assert scope["issued_simulation_time_s"] == physical["time_s"] and scope["issued_wall_time_s"] == clock.now
    assert scope["original_simulation_deadline_s"] == physical["time_s"]+75.
    read = runtime.read_activation(context._root, plan=context.plan, approval=context.approval, run_id=RUN,
        process_pid=os.getpid(), local_integrity_key=KEY)
    assert read["scope"] == scope and read["blocking_planning_finished_assertion"] is True
    context.close()


@pytest.mark.parametrize("phase", ["recovery_boostback_slew", "recovery_entry_coast", "recovery_landing_13", "catch"])
def test_activation_refuses_wrong_or_late_phase(service, tmp_path, phase):
    context, _ = hook(service, tmp_path)
    with pytest.raises(runtime.TowerRuntimeError, match="fresh_early"):
        context.activate(body(), phase=phase)
    assert context.actor is None


def test_activation_context_copies_and_original_deadlines_do_not_mutate(service, tmp_path):
    context, _ = hook(service, tmp_path)
    scope = context.activate(body(), phase="recovery_boostback_burn")
    scope["original_simulation_deadline_s"] += 900.
    exposed_plan, exposed_profile = context.plan, context.profile
    exposed_plan["tower_supervision"]["decision_expiry_s"] = 900.
    exposed_profile["launch"]["longitude_deg"] = 10.
    assert context.scope["original_simulation_deadline_s"] == body()["time_s"]+75.
    assert context.plan["tower_supervision"]["decision_expiry_s"] == 75.
    assert context.profile["launch"]["longitude_deg"] != 10.
    with pytest.raises(runtime.TowerRuntimeError):
        context.activate(body(154.), phase="recovery_boostback_burn")
    context.close()


@pytest.mark.parametrize("binding", ["run_id", "process_pid", "approval", "key"])
def test_activation_rejects_cross_run_pid_grant_or_key(service, tmp_path, binding):
    context, _ = hook(service, tmp_path)
    context.activate(body(), phase="recovery_boostback_burn")
    arguments = dict(plan=context.plan, approval=context.approval, run_id=RUN, process_pid=os.getpid(), local_integrity_key=KEY)
    if binding == "run_id":
        arguments["run_id"] = "b"*32
    elif binding == "process_pid":
        arguments["process_pid"] += 1
    elif binding == "approval":
        arguments["approval"]["nonce"] = "different"
    else:
        arguments["local_integrity_key"] = b"x"*32
    with pytest.raises(runtime.TowerRuntimeError):
        runtime.read_activation(context._root, **arguments)
    context.close()


def test_pending_tick_paces_actual_caller_time_and_keeps_physical_inputs(service, tmp_path):
    context, clock = hook(service, tmp_path)
    context.activate(body(), phase="recovery_boostback_burn")
    first = context.tick(body(), tower_ready=None, return_mode="undecided", active_site_id="capture")
    assert first.routing_request is not None and clock.sleeps == []
    later = body(155.9)
    original = deepcopy(later)
    context.tick(later, tower_ready=None, return_mode="undecided", active_site_id="capture")
    assert clock.now == pytest.approx(1002.) and sum(clock.sleeps) == pytest.approx(2.)
    assert max(clock.sleeps) <= .1 and later == original
    assert context.actor.receipt()["site_observations"][-1]["simulation_time_s"] == 155.9
    context.close()


def test_pacing_never_extends_original_deadline_and_source_change_stops_wait(service, tmp_path):
    context, clock = hook(service, tmp_path)
    context.activate(body(), phase="recovery_boostback_burn")
    context.tick(body(1000.), tower_ready=None, return_mode="undecided", active_site_id="capture")
    assert clock.now == pytest.approx(1075.) and sum(clock.sleeps) == pytest.approx(75.)
    context.close()
    other, other_clock = hook(service, tmp_path/"other")
    other.activate(body(), phase="recovery_boostback_burn")
    other_clock.current = False
    other.tick(body(160.), tower_ready=True, return_mode="capture", active_site_id="capture")
    assert other_clock.sleeps == [] and other.actor.receipt()["supervision"]["status"] == "blocked_source_or_plan"
    other.close()


def test_private_integrity_key_rejects_public_permissions_symlink_or_wrong_length(tmp_path):
    path = tmp_path/"tower-integrity.key"
    path.write_bytes(b"x"*32)
    path.chmod(0o644)
    with pytest.raises(runtime.TowerRuntimeError):
        runtime.read_local_key(tmp_path)
    path.chmod(0o600)
    assert runtime.read_local_key(tmp_path) == b"x"*32
    path.write_bytes(b"x"*31)
    with pytest.raises(runtime.TowerRuntimeError):
        runtime.read_local_key(tmp_path)
    path.unlink()
    target = tmp_path/"target"
    target.write_bytes(b"x"*32)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(runtime.TowerRuntimeError):
        runtime.read_local_key(tmp_path)


def test_fixed_entrypoints_cannot_be_provided_as_a_callback_or_path(service):
    plan = future_plan(service)
    # This deliberately incomplete map cannot authorize execution. It keeps
    # the unit rejection test independent of future physical-module presence.
    plan["source_sha256"].pop(runtime.DRIVER_PATH, None)
    plan["source_sha256"].pop(runtime.VERIFIER_PATH, None)
    with pytest.raises(runtime.TowerRuntimeError, match="driver_unavailable"):
        runtime._load_entrypoints(plan)
    assert runtime.DRIVER_PATH not in plan["source_sha256"]


class StaticProcessFixture:
    """Declared PID fixture, not proof of an integrated spacecraft worker."""
    pid = os.getpid()
    ended = False
    def poll(self):
        return 0 if self.ended else None
    def wait(self, timeout=None):
        return 0


def test_reserved_run_upgrades_only_to_its_signed_current_actor(service, monkeypatch):
    from src.runtime import starship_tower_broker
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    process = StaticProcessFixture()
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(control.Thread, "start", lambda self: None)
    started = service.execute(SESSION, plan["id"], plan["sha256"])
    run_id = started["execution"]["run_id"]
    run_dir = service.root/("run-"+run_id)
    with service._lock():
        stored = service._load(SESSION)
    profile, catch, sites = public_inputs()
    context = runtime.TowerWorkerContext(run_dir, plan, stored["approval"], run_id, profile, catch, sites,
        local_integrity_key=runtime.read_local_key(run_dir), source_is_current=lambda: True)
    context.activate(body(), phase="recovery_boostback_burn")
    context.tick(body(), tower_ready=True, return_mode="capture", active_site_id="capture")
    original_state = deepcopy(body())
    monkeypatch.setattr(starship_tower_broker.TowerBroker, "serve", lambda self: self.step())
    service._connect_tower_runtime(SESSION, plan["id"], plan["sha256"], run_id, process)
    state = service.current(SESSION)
    lease = control._read(service.root/"tower-active-run.json")
    assert state["execution"]["tower_supervision_registered"] is True
    assert lease["schema"] == "missionos.starship_tower_run_lease.v1"
    assert lease["actor_pid"] == process.pid and lease["context"] == context.scope["context"]
    assert original_state == body() and context.actor.receipt()["application_evidence"] is None
    assert service.tower_status(SESSION, plan["id"], plan["sha256"], run_id)["tower_pending"]["run_active"] is True
    context.close()
    process.ended = True
    service._observe_tower_exit(run_id, process)
    service._observe_exit(SESSION, run_id, process)
    assert not (service.root/"tower-active-run.json").exists()


@pytest.mark.parametrize("axis", ["wall", "simulation"])
@pytest.mark.parametrize("issued", [200.1, 1000.])
@pytest.mark.parametrize("exceeds_deadline", [False, True])
def test_registered_actor_expiry_uses_constructed_deadline_without_ulp_extension(
    service, monkeypatch, axis, issued, exceeds_deadline,
):
    from src.runtime.starship_operator_resolution import OperatorResolutionLedger
    from src.runtime.starship_tower_broker import TowerBroker

    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    process = StaticProcessFixture()
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(control.Thread, "start", lambda self: None)
    started = service.execute(SESSION, plan["id"], plan["sha256"])
    run_id = started["execution"]["run_id"]
    run_dir = service.root/("run-"+run_id)
    with service._lock():
        stored = service._load(SESSION)
    profile, catch, sites = public_inputs()
    key = runtime.read_local_key(run_dir)
    wall = issued if axis == "wall" else 1000.
    simulation = issued if axis == "simulation" else 153.9
    actor_type = runtime.TowerActor

    def signed_actor_with_boundary(mailbox, scope, *args, **kwargs):
        # Produce an otherwise genuine signed identity, so an over-budget
        # rejection exercises the deadline check rather than bad signatures.
        if exceeds_deadline:
            field = "original_"+axis+"_deadline_s"
            scope[field] = math.nextafter(scope[field], math.inf)
        return actor_type(mailbox, scope, *args, **kwargs)

    monkeypatch.setattr(runtime, "TowerActor", signed_actor_with_boundary)
    context = runtime.TowerWorkerContext(run_dir, plan, stored["approval"], run_id, profile, catch, sites,
        local_integrity_key=key, source_is_current=lambda: True, clock=lambda: wall)
    context.activate(body(simulation), phase="recovery_boostback_burn")
    scope = context.scope
    broker = TowerBroker(run_dir/"tower", run_id, scope, process,
        OperatorResolutionLedger(key, store_path=run_dir/"tower-resolution.sqlite3"),
        runtime.FixtureTowerJudge(), lambda: True, clock=lambda: wall)
    try:
        if exceeds_deadline:
            with pytest.raises(control.StarshipMissionError, match="tower_broker_scope_binding_mismatch"):
                service.register_tower_broker(SESSION, plan["id"], plan["sha256"], run_id, broker=broker)
            assert service.current(SESSION)["execution"]["tower_supervision_registered"] is False
            assert control._read(service.root/"tower-active-run.json")["schema"] == "missionos.starship_tower_run_reservation.v1"
        else:
            assert scope["original_"+axis+"_deadline_s"] == issued+75.
            service.register_tower_broker(SESSION, plan["id"], plan["sha256"], run_id, broker=broker)
            assert service.current(SESSION)["execution"]["tower_supervision_registered"] is True
    finally:
        context.close()
        process.ended = True
        service._observe_tower_exit(run_id, process)
        service._observe_exit(SESSION, run_id, process)


def test_second_approved_run_is_refused_before_any_second_spawn(service, monkeypatch):
    first = future_plan(service)
    service.approve(SESSION, first["id"], first["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    process, calls = StaticProcessFixture(), []
    def spawn(*args, **kwargs):
        calls.append(args)
        assert (service.root/"tower-active-run.json").is_file()
        return process
    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    monkeypatch.setattr(control.Thread, "start", lambda self: None)
    started = service.execute(SESSION, first["id"], first["sha256"])
    second_session = "starship-operator-"+"e"*24
    second_service = control.StarshipMissionService(service.root, planner=service.planner)
    second = future_plan(second_service, second_session)
    second_service.approve(second_session, second["id"], second["sha256"])
    with pytest.raises(control.StarshipMissionError, match="another_tower_run"):
        second_service.execute(second_session, second["id"], second["sha256"])
    assert len(calls) == 1
    assert second_service.current(second_session)["approval"]["consumed_by_run"] is None
    process.ended = True
    service._observe_exit(SESSION, started["execution"]["run_id"], process)


def test_timeout_stops_owned_process_group_without_any_control_command(monkeypatch):
    killed = []
    class TimeoutProcess:
        pid = 12345
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("fixture-worker", timeout)
    monkeypatch.setattr(control.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    control.StarshipMissionService._limit_tower_runtime(TimeoutProcess(), 900.)
    assert killed == [(12345, control.signal.SIGTERM), (12345, control.signal.SIGKILL)]


def test_real_subprocess_disabled_guard_retains_signed_failure_and_releases_lease(service, monkeypatch):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    # Always reduce authority BEFORE creating the child. The ordinary suite
    # must never become a full-flight trial when future modules are installed.
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_DISABLE_DRIVER", "1")
    state = service.execute(SESSION, plan["id"], plan["sha256"])
    run_id = state["execution"]["run_id"]
    assert state["execution"]["process_role"] == "credential_free_tower_simulation_worker"
    deadline = time.monotonic()+10.
    while time.monotonic() < deadline:
        state = service.status(SESSION, plan["id"])
        if state["status"] == "failed" and not (service.root/"tower-active-run.json").exists():
            break
        time.sleep(.02)
    assert state["status"] == "failed" and state["execution"]["worker_receipt_verified"] is True
    assert state["execution"]["failure_reason"] == "tower_runtime_driver_disabled"
    assert state["execution"]["provider_credentials_present"] is False
    assert state["execution"]["simulator_provider_credentials_present"] is False
    assert state["execution"]["tower_supervision_registered"] is False
    assert not (service.root/"tower-active-run.json").exists()
    result = control._read(service.root/("run-"+run_id)/"result.json")
    assert result["worker_pid"] == state["execution"]["worker_pid"]
    assert result["signature"] == service._signature({k: v for k, v in result.items() if k != "signature"})
    assert result["artifact_sha256"] == {} and result["mission_completed"] is False
    assert result["runtime_driver_disabled"] is True and result["physical_driver_entrypoint_called"] is False
    with pytest.raises(control.StarshipMissionError, match="verified_artifact"):
        service.read_artifact(SESSION, plan["id"], "report.html")


def test_cli_requires_private_worker_context_not_operator_raw_arguments():
    result = subprocess.run([str(Path(control.sys.executable)), str(control.REPO/"scripts/run_starship_tower_mission_worker.py"), "--help"],
        cwd=control.REPO, env=control.worker_environment(), capture_output=True, timeout=10.)
    assert result.returncode == 0 and b"--state-dir" in result.stdout and b"--run-id" in result.stdout
    assert b"--disable-driver" in result.stdout
    assert b"--thrust" not in result.stdout and b"--gimbal" not in result.stdout


def test_source_manifest_does_not_import_generated_baseline_or_credentials(service):
    plan = future_plan(service)
    names = tuple(plan["source_sha256"])
    assert not any(name.startswith("output/") or Path(name).is_absolute() or "secret" in name for name in names)
    assert all(sha256((control.REPO/name).read_bytes()).hexdigest() == value for name, value in plan["source_sha256"].items())


def consumed_state(service, monkeypatch):
    clock = SimpleNamespace(now=1000.)
    service.clock = lambda: clock.now
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    with service._lock():
        state = service._load(SESSION)
        state["approval"].update(consumed_by_run=RUN, consumed_at_epoch_s=1000., execution_deadline_epoch_s=1900.)
        state["approval"]["signature"] = service._signature({k: v for k, v in state["approval"].items() if k != "signature"})
        state.update(status="running", execution={"run_id": RUN, "started_at_epoch_s": 1000., "status": "running"})
        service._save(SESSION, state)
    return state, clock


def test_original_dispatch_ttl_separate_from_fixed_consumed_execution_window(service, monkeypatch):
    state, clock = consumed_state(service, monkeypatch)
    original_grant = deepcopy(state["approval"])
    clock.now = 1350.
    service._valid_grant(state, consumed_by_run=RUN)
    assert state["approval"] == original_grant
    clock.now = 1899.999
    service._valid_grant(state, consumed_by_run=RUN)
    for expired in (1900., 1900.0001):
        clock.now = expired
        with pytest.raises(control.StarshipMissionError, match="expired_or_consumed"):
            service._valid_grant(state, consumed_by_run=RUN)
    with pytest.raises(control.StarshipMissionError):
        service._valid_grant(state, consumed_by_run="b"*32)


@pytest.mark.parametrize("field,value", [("consumed_at_epoch_s", 1300.), ("consumed_at_epoch_s", 999.),
    ("consumed_at_epoch_s", "1000"), ("execution_deadline_epoch_s", 1900.1),
    ("execution_deadline_epoch_s", 1800.), ("execution_deadline_epoch_s", True)])
def test_signed_timing_mutation_cannot_extend_or_reissue_authority(service, monkeypatch, field, value):
    state, clock = consumed_state(service, monkeypatch)
    clock.now = 1350.
    state["approval"][field] = value
    state["approval"]["signature"] = service._signature({k: v for k, v in state["approval"].items() if k != "signature"})
    with pytest.raises(control.StarshipMissionError):
        service._valid_grant(state, consumed_by_run=RUN)


def test_unconsumed_original_expiry_and_legacy_consumed_expiry_unchanged(service, monkeypatch):
    clock = SimpleNamespace(now=1000.)
    service.clock = lambda: clock.now
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    clock.now = 1300.
    with pytest.raises(control.StarshipMissionError):
        service._valid_grant(service._load(SESSION))
    clock.now = 1000.
    other = "starship-operator-"+"f"*24
    plan = service.plan(other, "Starship sixdof_gimbal_step", expected_scenario="sixdof_gimbal_step")["plan"]
    service.approve(other, plan["id"], plan["sha256"])
    state = service._load(other)
    state["approval"]["consumed_by_run"] = RUN
    state["approval"]["signature"] = service._signature({k: v for k, v in state["approval"].items() if k != "signature"})
    clock.now = 1300.
    with pytest.raises(control.StarshipMissionError):
        service._valid_grant(state, consumed_by_run=RUN)


def test_activation_caps_75_second_deadlines_by_original_execution_budget(service, tmp_path, monkeypatch):
    state, clock = consumed_state(service, monkeypatch)
    clock.now = 1890.
    profile, catch, sites = public_inputs()
    path = tmp_path/"late-actor"
    path.mkdir(mode=0o700)
    context = runtime.TowerWorkerContext(path, state["plan"], state["approval"], RUN, profile, catch, sites,
        local_integrity_key=KEY, source_is_current=lambda: True, clock=lambda: 5000., epoch_clock=lambda: clock.now)
    scope = context.activate(body(), phase="recovery_boostback_burn")
    assert scope["original_simulation_deadline_s"] == body()["time_s"]+10.
    assert scope["original_wall_deadline_s"] == 5010.
    assert state["approval"]["execution_deadline_epoch_s"] == 1900.
    runtime.read_activation(path, plan=state["plan"], approval=state["approval"], run_id=RUN,
        process_pid=os.getpid(), local_integrity_key=KEY)
    context.close()
    expired_path = tmp_path/"expired-actor"
    expired_path.mkdir(mode=0o700)
    expired = runtime.TowerWorkerContext(expired_path, state["plan"], state["approval"], RUN, profile, catch, sites,
        local_integrity_key=KEY, source_is_current=lambda: True, clock=lambda: 5010., epoch_clock=lambda: 1900.)
    with pytest.raises(runtime.TowerRuntimeError, match="original_run_deadline_expired"):
        expired.activate(body(), phase="recovery_boostback_burn")
    assert expired.actor is None


def test_original_run_expiry_blocks_late_directive_without_resetting_state(service, tmp_path, monkeypatch):
    state, epoch = consumed_state(service, monkeypatch)
    profile, catch, sites = public_inputs()
    path = tmp_path/"deadline-actor"
    path.mkdir(mode=0o700)
    wall = SimpleNamespace(now=5000.)
    context = runtime.TowerWorkerContext(path, state["plan"], state["approval"], RUN, profile, catch, sites,
        local_integrity_key=KEY, source_is_current=lambda: True, clock=lambda: wall.now, epoch_clock=lambda: epoch.now)
    context.activate(body(), phase="recovery_boostback_burn")
    context.tick(body(), tower_ready=False, return_mode="capture", active_site_id="capture")
    epoch.now, wall.now = 1900., 5001.
    physical = body(154.)
    before = deepcopy(physical)
    result = context.tick(physical, tower_ready=False, return_mode="capture", active_site_id="capture")
    assert result.directive is None and physical == before
    assert context.actor.receipt()["supervision"]["status"] == "run_ended"
    context.close()


def test_signed_execution_context_or_approved_budget_tamper_rejected(service, monkeypatch):
    state, clock = consumed_state(service, monkeypatch)
    clock.now = 1350.
    changed = deepcopy(state)
    changed["execution"]["started_at_epoch_s"] = 1000.001
    with pytest.raises(control.StarshipMissionError):
        service._valid_grant(changed, consumed_by_run=RUN)
    changed = deepcopy(state)
    changed["plan"]["tower_runtime"]["maximum_wall_time_s"] = 901.
    with pytest.raises(ValueError):
        service._valid_grant(changed, consumed_by_run=RUN)


@pytest.mark.parametrize("field,value", [("actor_pid", 1), ("approval_record_sha256", "a"*64),
    ("source_sha256", "a"*64), ("context", {}), ("return_sites_sha256", "a"*64)])
def test_presigned_reservation_cannot_be_rebound_to_other_worker_or_scope(service, monkeypatch, field, value):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    process = StaticProcessFixture()
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(control.Thread, "start", lambda self: None)
    started = service.execute(SESSION, plan["id"], plan["sha256"])
    run_id = started["execution"]["run_id"]
    with service._lock():
        state = service._load(SESSION)
    service._valid_tower_reservation(state, run_id, process.pid)
    path = service.root/"tower-active-run.json"
    original = control._read(path)
    modified = deepcopy(original)
    modified[field] = value
    modified["signature"] = service._signature({k: v for k, v in modified.items() if k != "signature"})
    control._write(path, modified)
    with pytest.raises(control.StarshipMissionError, match="reservation_binding"):
        service._valid_tower_reservation(state, run_id, process.pid)
    control._write(path, original)
    process.ended = True
    service._observe_exit(SESSION, run_id, process)


def test_activation_file_duplicate_keys_or_nonfinite_values_fail_closed(service, tmp_path):
    context, _ = hook(service, tmp_path)
    context.activate(body(), phase="recovery_boostback_burn")
    path = context._root/"tower-activation.json"
    original = path.read_bytes()
    path.write_bytes(b'{"schema":"duplicate",'+original[1:])
    with pytest.raises(runtime.TowerRuntimeError, match="duplicate"):
        runtime.read_activation(context._root, plan=context.plan, approval=context.approval, run_id=RUN,
            process_pid=os.getpid(), local_integrity_key=KEY)
    path.write_bytes(b'{"unknown":NaN}')
    with pytest.raises(runtime.TowerRuntimeError, match="nonfinite"):
        runtime.read_activation(context._root, plan=context.plan, approval=context.approval, run_id=RUN,
            process_pid=os.getpid(), local_integrity_key=KEY)
    context.close()


def test_reducing_guard_authenticates_grant_source_and_lease_before_import(service, monkeypatch):
    plan = future_plan(service)
    service.approve(SESSION, plan["id"], plan["sha256"])
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE", "fixture")
    monkeypatch.setenv("MISSIONOS_STARSHIP_TOWER_RUNTIME_DISABLE_DRIVER", "1")
    process = StaticProcessFixture()
    seen = []
    def spawn(args, **kwargs):
        assert args[-1] == "--disable-driver"
        assert "MISSIONOS_STARSHIP_TOWER_RUNTIME_DISABLE_DRIVER" not in kwargs["env"]
        return process
    def forbidden(plan):
        seen.append("entrypoint_import")
        raise AssertionError("The guarded worker must not even import the physical producer/checker.")
    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    monkeypatch.setattr(control.Thread, "start", lambda self: None)
    monkeypatch.setattr(runtime, "_load_entrypoints", forbidden)
    started = service.execute(SESSION, plan["id"], plan["sha256"])
    run_id = started["execution"]["run_id"]
    assert runtime.execute_tower_worker(service.root, run_id, disable_driver=True) == 2
    result = control._read(service.root/("run-"+run_id)/"result.json")
    assert result["failure_reason"] == "tower_runtime_driver_disabled" and seen == []
    assert result["physical_driver_entrypoint_called"] is False
    assert result["signature"] == service._signature({k: v for k, v in result.items() if k != "signature"})
    process.ended = True
    service._observe_exit(SESSION, run_id, process)
