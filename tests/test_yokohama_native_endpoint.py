"""Dedicated endpoint admission and simulated callback contracts; no flight/cloud."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import yokohama_native_endpoint_contract as contract
from scripts import yokohama_native_endpoint_trial as controller
from scripts import yokohama_native_trial as cloud
from scripts import yokohama_sitl as sitl
from scripts.verify_yokohama_native_endpoint import verify
from scripts.yokohama_candidate_recovery import LIMITS, policy
from scripts.yokohama_endpoint_feedback import FIXED, feedback_policy
from scripts.yokohama_flight_worker import execute_model_segments
from scripts.yokohama_goal_distance_adapter import POLICY, adapt_candidate, validate_adaptation
from src.runtime import yokohama_execution_service as vehicle
from src.runtime.yokohama_native import vla_candidate

IMAGE = "sha256:" + "a" * 64


def config(backend="fixture"):
    home = [0.0, 0.0, 15.0]
    return dict(
        run_id="inert-endpoint",
        timeout_s=900,
        operator_approval_manifest_sha256="a" * 64,
        endpoint_adapter_trial=deepcopy(contract.CONTRACT),
        candidate_recovery=deepcopy(LIMITS),
        world=dict(world_sha256="a" * 64, source_sha256={"collision-footprints.geojson": "b" * 64}),
        flight_stages=[
            dict(name=n, target_world_xyz_m=home.copy()) for n in ("00-D1", "01-FEEDBACK-EXIT")
        ],
        decisions=dict(
            backend=backend,
            goal_distance_adapter=deepcopy(POLICY),
            wam_profile="motion-v4",
            points=["D1"],
            sea_leg_present=False,
            payload_release_present=False,
            endpoint_feedback=dict(
                FIXED,
                entry_world_xyz_m=home.copy(),
                exit_world_xyz_m=home.copy(),
                goal_world_xyz_m=[4.0, 0.0, 15.0],
                map_sha256="b" * 64,
            ),
        ),
    )


def proposal(root, backend="fixture", service=None):
    p = dict(
        schema="yokohama.adapted-endpoint-proposal.v1",
        proposal_id="inert-test",
        contract=deepcopy(contract.CONTRACT),
        limits=deepcopy(LIMITS),
        feedback_limits=deepcopy(FIXED),
        adapter_policy=deepcopy(POLICY),
        physical_execution_invoked=False,
        image_id=IMAGE,
        city_models=backend,
        native_service_config=str(service) if service else None,
        native_service_config_sha256=hashlib.sha256(service.read_bytes()).hexdigest()
        if service
        else None,
        input_sha256=vehicle.recovery_input_hashes(root),
        simulator_arguments=contract.arguments(backend, IMAGE, service),
    )
    a = dict(
        approved_proposal_sha256=vehicle.proposal_digest(p),
        maximum_actual_flight_trials=1,
        operator_approval_ref="inert-test",
        actor_session_id="inert-test",
        approved_at="2026-10-09T00:00:00Z",
    )
    return dict(proposal=p, approval=a)


@pytest.mark.parametrize("backend", ["fixture", "native"])
def test_exact_separate_contract_admits_both_backends(tmp_path, backend):
    service = tmp_path / "service.json" if backend == "native" else None
    if service:
        service.write_text('{"vla_port":18117,"wam_port":18118}')
    p = proposal(sitl.REPO, backend, service)
    manifest, captured = contract.admit(
        json.dumps(p).encode(), sitl.REPO, p["proposal"]["simulator_arguments"]
    )
    assert manifest == p
    assert captured == (service.read_bytes() if service else None)
    c = config(backend)
    contract.validate_config(c)
    assert policy(c) == LIMITS and feedback_policy(c)["max_cycles"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_s", 901),
        ("candidate_recovery", {}),
        ("endpoint_adapter_trial", None),
        ("fixture_reject_second_candidate", True),
        ("operator_approval_manifest_sha256", ""),
    ],
)
def test_unapproved_recovery_or_deadline_never_enters_adapter(field, value):
    c = config()
    c[field] = value
    with pytest.raises(ValueError):
        feedback_policy(c)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("proposal", "input_sha256", {}),
        ("proposal", "limits", {}),
        ("proposal", "physical_execution_invoked", True),
        ("approval", "maximum_actual_flight_trials", 2),
        ("approval", "actor_session_id", ""),
    ],
)
def test_scope_or_source_change_rejected(tmp_path, section, field, value):
    p = proposal(sitl.REPO)
    p[section][field] = value
    p["approval"]["approved_proposal_sha256"] = vehicle.proposal_digest(p["proposal"])
    with pytest.raises(ValueError):
        contract.admit(json.dumps(p).encode(), sitl.REPO, p["proposal"]["simulator_arguments"])


def test_service_toctou_rejected_before_runtime(tmp_path):
    service = tmp_path / "service.json"
    service.write_text("old")
    p = proposal(sitl.REPO, "native", service)
    service.write_text("changed")
    with pytest.raises(ValueError, match="changed since approval"):
        contract.admit(json.dumps(p).encode(), sitl.REPO, p["proposal"]["simulator_arguments"])


def test_replay_or_extra_route_flag_rejected_before_any_subprocess(tmp_path, monkeypatch):
    p = proposal(sitl.REPO)
    approval = tmp_path / "execution-approval.json"
    approval.write_text(json.dumps(p))
    (tmp_path / "flight-attempt-claimed.json").write_text("occupied")
    monkeypatch.setattr(
        sitl.subprocess, "run", lambda *a, **k: pytest.fail("No subprocess permitted")
    )
    args = p["proposal"]["simulator_arguments"] + [
        "--approve-sitl",
        "--approval-manifest",
        str(approval),
        "--output-dir",
        str(tmp_path / "run"),
    ]
    with pytest.raises(FileExistsError):
        sitl.main(args)
    with pytest.raises(SystemExit):
        sitl.main(args + ["--sea-round-trip"])
    assert (tmp_path / "flight-attempt-claimed.json").read_text() == "occupied"


class Decisions:
    def __init__(self, cfg, *, no_progress=False, reject=False):
        self.cfg, self.completed, self.active, self.closed = cfg, [], True, False
        self.no_progress, self.reject, self.current = no_progress, reject, None

    def prepare_segment(self, obs, target, upload, **kwargs):
        if self.reject and self.completed:
            raise ValueError("Candidate rejected")
        anchor = deepcopy(obs())
        raw = vla_candidate("58 49 49" if not self.completed else "43 49 49", anchor)
        candidate, receipt = adapt_candidate(self.cfg, raw, anchor, "c" * 64)
        if self.no_progress and self.completed:
            # Still valid progress, but does not arrive within goal tolerance.
            candidate["target_world_xyz_m"] = [3.47, 0.0, 15.0]
        if not self.no_progress:
            validate_adaptation(
                self.cfg,
                dict(
                    candidate=candidate,
                    vehicle_distance_adjustment=receipt,
                    input_observation=anchor,
                    vla_response_sha256="c" * 64,
                ),
            )
        self.current = candidate
        return dict(
            candidate=candidate,
            permit_id=str(len(self.completed)),
            expires_at_worker_wall_s=1000,
            rules=dict(
                start_world_xyz_m=anchor["vehicle"]["xyz"],
                proposal_origin_world_xyz_m=anchor["vehicle"]["xyz"],
            ),
            connector_name="approved-exit",
            connector_sha256="d" * 64,
        )

    def record_arrival(self, *args):
        pass

    def stop(self):
        self.active, self.closed = False, True


def callbacks(c, **options):
    state = dict(t=100.0, xyz=[0.0, 0.0, 15.0], sim=100.0)
    d = Decisions(c, **options)
    events = []

    def obs():
        return dict(
            run_id=c["run_id"],
            world_sha256="a" * 64,
            wall_s=state["t"],
            sim_s=state["sim"],
            vehicle=dict(xyz=state["xyz"].copy(), quat_wxyz=[1, 0, 0, 0], age_s=0.01),
            velocity_ned=[0, 0, 0],
            heading_ned_rad=1.5707963267948966,
            battery_fraction=0.9,
            position_valid=True,
            arming_state=2,
            landed=False,
            reset_counters=[0, 0, 0],
            nav_state=4,
        )

    def wait_for(predicate, timeout):
        state["xyz"] = d.current["target_world_xyz_m"].copy()
        for _ in range(5):
            state["sim"] += 1
            state["t"] += 1
            r = obs()
            if predicate(r):
                return r
        pytest.fail("Stable endpoint did not settle")

    kw = dict(
        config=c,
        decisions=d,
        obs=obs,
        next_target=[4, 0, 15],
        upload=lambda *a: None,
        activate=lambda **k: events.append("activate"),
        wait_for=wait_for,
        run=lambda *a: None,
        event=lambda name, **k: events.append(name),
        clock=lambda: state["t"],
    )
    return d, state, events, kw


def test_two_selected_endpoints_then_irrevocable_stop_without_intermediate_ap():
    d, _, events, kw = callbacks(config())
    execute_model_segments(**kw)
    assert len(d.completed) == 2 and d.closed and not d.active
    assert d.completed[-1]["arrived"]["vehicle"]["xyz"] == [4.0, 0.0, 15.0]
    assert [e for e in events if e == "activate"] == ["activate", "activate"]
    assert events[-1] == "city_endpoint_feedback_completed"
    assert all(p["stable_samples"] >= 3 for p in d.completed)


def test_two_non_goal_arrivals_cannot_be_marked_success():
    d, _, events, kw = callbacks(config(), no_progress=True)
    with pytest.raises(ValueError, match="goal not observed"):
        execute_model_segments(**kw)
    assert len(d.completed) == 2 and "city_endpoint_feedback_completed" not in events


def test_rejected_second_candidate_never_dispatched():
    _, _, events, kw = callbacks(config(), reject=True)
    with pytest.raises(ValueError, match="Candidate rejected"):
        execute_model_segments(**kw)
    assert events.count("city_segment_dispatched") == 1


def test_model_deadline_preserves_absolute_220_second_return_reserve():
    d, state, _, kw = callbacks(config())
    state["t"] = 700
    with pytest.raises(TimeoutError):
        execute_model_segments(**kw)
    assert d.endpoint_deadline == 665 and not d.completed


@pytest.fixture
def plan(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in cloud.PAYLOAD:
        source = cloud.REPO / "scripts" / name
        (payload / name).write_bytes(source.read_bytes() if source.is_file() else b"inert")
    return dict(
        schema=controller.SCHEMA,
        contract=deepcopy(contract.CONTRACT),
        project="inert-project",
        instance="inert-owned-endpoint",
        zone="us-west1-a",
        image=cloud.IMAGES[1],
        max_runtime_s=3600,
        max_attempts=1,
        reserve_usd=8,
        aggregate_budget_usd=8,
        budget_scope="single_native_endpoint_attempt",
        guaranteed_spend_cap=False,
        hard_network_byte_cap=True,
        external_egress_limit_bytes=1024**3,
        external_ingress_limit_bytes=64 * 1024**3,
        budget_id="a" * 32,
        trial_directory=str(tmp_path),
        local_image_id=IMAGE,
        source_sha256=vehicle.recovery_input_hashes(cloud.REPO),
        cost_estimate=dict(usd_before_tax_fx=1.41),
        payload_directory=str(payload),
        payload_sha256={n: cloud.digest_file(payload / n) for n in cloud.PAYLOAD},
        cpu_qualification_run=str(tmp_path / "missing-cpu-flight"),
        cpu_verdict_sha256=None,
        gcloud="inert-not-executable",
        ssh_key="inert",
        python="inert",
    )


def approval(p):
    return dict(
        approved_plan_sha256=vehicle.proposal_digest(p),
        reference="inert",
        actor="inert",
        approved_at="2026-10-09T00:00:00Z",
        approved_budget_usd=8,
        approve_ephemeral_cleanup=True,
        acknowledge_network_quota_cutoff=True,
        acknowledge_estimate_not_spend_cap=True,
    )


def test_offline_description_works_but_missing_cpu_qualification_prevents_paid_action(
    plan, monkeypatch, tmp_path
):
    controller.validate_plan(plan, require_cpu=False)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(
        cloud.subprocess, "run", lambda *a, **k: pytest.fail("No subprocess permitted")
    )
    assert controller.main(["--plan", str(path)]) == 0
    with pytest.raises(FileNotFoundError):
        controller.EndpointTrial(plan, tmp_path).execute(approval(plan))
    assert not (tmp_path / "attempt.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("reserve_usd", 9),
        ("max_attempts", 2),
        ("zone", "us-west1-b"),
        ("max_runtime_s", 3601),
        ("source_sha256", {}),
    ],
)
def test_broader_or_changed_cloud_plan_rejected(plan, field, value):
    plan[field] = value
    with pytest.raises(ValueError):
        controller.validate_plan(plan, require_cpu=False)


def test_old_budget_approval_never_reused(plan, tmp_path):
    a = approval(plan)
    a["approved_budget_usd"] = 2
    with pytest.raises(ValueError, match="USD8"):
        controller.EndpointTrial(plan, tmp_path).validate_execution_binding(a)


def test_cleanup_reserve_prevents_tunnel_or_flight_start(plan, tmp_path, monkeypatch):
    trial = controller.EndpointTrial(plan, tmp_path)
    monkeypatch.setattr(trial, "validate_execution_binding", lambda a: None)
    monkeypatch.setattr(trial, "qualify_local", lambda: None)
    trial.local_preparation = SimpleNamespace(assert_fresh=lambda: None, assert_age=lambda: None)
    monkeypatch.setattr(
        cloud.subprocess, "Popen", lambda *a, **k: pytest.fail("No process permitted")
    )
    with pytest.raises(TimeoutError, match="cleanup reserve"):
        trial.fly(approval(plan), cloud.now() + cloud.timedelta(seconds=1499))


def test_independent_verifier_rejects_legacy_config_and_missing_runtime_proofs(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(config()))
    (tmp_path / "result.json").write_text("{}")
    verdict = verify(tmp_path)
    assert verdict["status"] == "failed" and not verdict["native_model_control_verified"]
    assert (
        not verdict["adapter_goal_control_verified"]
        and not verdict["raw_model_output_control_verified"]
    )


@pytest.fixture
def proof(tmp_path, monkeypatch):
    """Isolate the wrapper's bindings; trajectory verifiers are independently tested."""
    from scripts import verify_yokohama_candidate_recovery as recovery
    from src.runtime.yokohama_native import digest

    job = tmp_path / "job"
    job.mkdir()
    run = job / "run"
    run.mkdir()
    (run / "decisions").mkdir()
    cfg = config()
    p = proposal(sitl.REPO)
    raw = json.dumps(p).encode()
    (job / "execution-approval.json").write_bytes(raw)
    cfg["operator_approval_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    (run / "config.json").write_text(json.dumps(cfg))
    names = [
        "yokohama_native_endpoint_contract.py",
        "yokohama_goal_distance_adapter.py",
        "yokohama_candidate_recovery.py",
        "yokohama_flight_worker.py",
        "verify_yokohama_native_endpoint.py",
        "verify_yokohama_endpoint_feedback.py",
    ]
    for name in names:
        (run / name).write_bytes((sitl.REPO / "scripts" / name).read_bytes())
    result = dict(
        config_sha256=digest(cfg),
        image_id=IMAGE,
        source_sha256={n: cloud.digest_file(run / n) for n in names},
        worker_reaped=True,
        cleanup=True,
        observed=dict(status="failed_recovered"),
    )
    (run / "result.json").write_text(json.dumps(result))
    (run / "container-inspect.json").write_text(json.dumps([dict(Image=IMAGE)]))
    events = [dict(event="city_session_revoked", wall_s=2)]
    for i, op in enumerate(["start", "stop"]):
        request = dict(config_sha256=digest(cfg), run_id=cfg["run_id"], operation=op, cycle=i)
        response = dict(
            request_sha256=digest(request),
            run_id=cfg["run_id"],
            operation=op,
            value=dict(session_revoked=True, fixture_stopped=True),
        )
        stem = run / "decisions" / f"{i:03d}"
        stem.with_name(stem.name + "-request.json").write_text(json.dumps(request))
        stem.with_name(stem.name + "-response.json").write_text(json.dumps(response))
        events += [
            dict(
                event="city_request",
                operation=op,
                cycle=i,
                wall_s=i + 3,
                request_sha256=digest(request),
            ),
            dict(
                event="city_response",
                operation=op,
                cycle=i,
                wall_s=i + 3.1,
                response_sha256=digest(response),
            ),
        ]
    (run / "flight-events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    monkeypatch.setattr(
        recovery,
        "verify",
        lambda *a, **k: dict(status="passed", mission_outcome="failed", recovery_outcome="passed"),
    )
    return run


def mutate(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def test_safe_failure_never_infers_native_or_goal_success(proof):
    r = verify(proof)
    assert r["status"] == "passed" and r["mission_outcome"] == "failed"
    assert r["recovery_outcome"] == "passed"
    assert not any(
        r[k]
        for k in (
            "native_model_control_verified",
            "adapter_goal_control_verified",
            "native_model_inference",
        )
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "cleanup",
        "config",
        "source",
        "source_missing",
        "approval",
        "image",
        "response",
        "stop",
        "dispatch",
    ],
)
def test_independent_wrapper_rejects_tampered_bindings(proof, tamper):
    if tamper == "cleanup":
        mutate(proof / "result.json", lambda r: r.update(cleanup=False))
    elif tamper == "config":
        mutate(proof / "result.json", lambda r: r.update(config_sha256="bad"))
    elif tamper == "source":
        (proof / "yokohama_flight_worker.py").write_text("tampered")
    elif tamper == "source_missing":
        mutate(proof / "result.json", lambda r: r["source_sha256"].pop("yokohama_flight_worker.py"))
    elif tamper == "approval":
        mutate(
            proof.parent / "execution-approval.json",
            lambda r: r["approval"].update(maximum_actual_flight_trials=2),
        )
    elif tamper == "image":
        mutate(proof / "result.json", lambda r: r.update(image_id="sha256:" + "b" * 64))
    elif tamper == "response":
        mutate(proof / "decisions/001-response.json", lambda r: r.update(request_sha256="bad"))
    elif tamper == "stop":
        (proof / "decisions/001-response.json").unlink()
    else:
        with (proof / "flight-events.jsonl").open("a") as f:
            f.write("\n" + json.dumps(dict(event="city_segment_dispatched", wall_s=3)))
    result = verify(proof)
    assert result["status"] == "failed" and not result["native_model_control_verified"]


def test_producer_success_without_measured_goal_fails(proof):
    mutate(
        proof / "result.json",
        lambda r: r.update(observed=dict(status="passed", model_goal_reached=False)),
    )
    assert "goal_not_observed" in verify(proof)["reason"]


def test_native_fly_catalog_uses_endpoint_without_sea_or_payload(plan, tmp_path, monkeypatch):
    trial = controller.EndpointTrial(plan, tmp_path)
    monkeypatch.setattr(trial, "validate_execution_binding", lambda a: None)
    monkeypatch.setattr(trial, "qualify_local", lambda: None)
    trial.local_preparation = SimpleNamespace(assert_fresh=lambda: None, assert_age=lambda: None)
    commands = []

    class Child:
        def __init__(self, args):
            self.args = args

        def poll(self):
            return None if "-N" in self.args else 1

    def popen(args, **kwargs):
        commands.append(args)
        if "--endpoint-adapter-trial" in args:
            run = Path(args[args.index("--output-dir") + 1])
            run.mkdir()
            (run / "result.json").write_text('{"observed":{"status":"failed_recovered"}}')
        return Child(args)

    monkeypatch.setattr(cloud.subprocess, "Popen", popen)
    job, p, a = trial.fly(approval(plan), cloud.now() + cloud.timedelta(seconds=1800))
    assert len(commands) == 2
    assert "--endpoint-adapter-trial" in commands[1]
    assert "--sea-round-trip" not in commands[1] and "--deliver-payload" not in commands[1]
    assert commands[1][commands[1].index("--timeout-seconds") + 1] == "900"
    raw = (job / "execution-approval.json").read_bytes()
    admitted, service = contract.admit(raw, sitl.REPO, p["simulator_arguments"])
    assert (
        admitted["approval"] == a
        and hashlib.sha256(service).hexdigest() == p["native_service_config_sha256"]
    )
    assert trial.flight.poll() == 1  # Recovered nonzero is retained for independent verification.


def test_cpu_host_emits_fixed_bins_and_shortens_second_action_before_rules(tmp_path, monkeypatch):
    from scripts import yokohama_decision_host as module
    from src.runtime.yokohama_native import digest

    cfg = config()
    _, state, _, kw = callbacks(cfg)
    host = object.__new__(module.DecisionHost)
    host.config, host.backend, host.pending, host.bundle = cfg, "fixture", {}, tmp_path
    host.feedback_guard = lambda *a, **k: cfg["decisions"]["endpoint_feedback"]
    host.feedback_capture = lambda *a: None
    host.pad_cycle = lambda *a: False
    capture = dict(
        frames=[dict(stamp_ns=100_000_000_000, assets=dict(onboard_rgb_png={}, down_rgb_png={}))]
    )
    host.capture = lambda *a: (tmp_path / "capture.json", capture, {})
    selected = []
    monkeypatch.setattr(module, "read_asset", lambda *a: b"inert-image")
    monkeypatch.setattr(
        module, "geometry_rules", lambda start, target, *a: selected.append(target) or {}
    )
    host.feedback_rules = lambda *a: None
    for cycle in (1, 2):
        output = tmp_path / str(cycle)
        output.mkdir()
        row = kw["obs"]()
        response = host.vla(
            dict(cycle=cycle, observation=row, next_target_world_xyz_m=[4, 0, 15]), output
        )
        raw = json.loads((output / "native-response.json").read_text())
        assert raw["generated_text"] == ("58 49 49" if cycle == 1 else "43 49 49")
        assert raw["fixture"] and not raw["vla_inference_invoked"]
        assert response["vla_response_sha256"] == digest(raw)
        validate_adaptation(cfg, response)
        state["xyz"] = response["candidate"]["target_world_xyz_m"]
    assert selected[-1] == [4.0, 0.0, 15.0]
    assert response["vehicle_distance_adjustment"]["scale"] < 1


@pytest.mark.parametrize("confirmed", [True, False])
def test_owned_vm_network_quota_must_be_confirmed_before_bootstrap(
    plan, tmp_path, monkeypatch, confirmed
):
    trial = controller.EndpointTrial(plan, tmp_path)
    commands = []

    def ssh(command, name, **kwargs):
        commands.append(command)
        text = (
            (
                "-N MISSIONOS_EGRESS\n-A MISSIONOS_EGRESS -m quota --quota 1073741824 -j RETURN\n"
                "-A MISSIONOS_EGRESS -j REJECT\n-N MISSIONOS_INGRESS\n"
                "-A MISSIONOS_INGRESS -m quota --quota 68719476736 -j RETURN\n"
                "-A MISSIONOS_INGRESS -j REJECT\n"
            )
            if confirmed
            else "unconfirmed"
        )
        (tmp_path / (name + ".stdout")).write_text(text)

    monkeypatch.setattr(trial, "ssh", ssh)
    if confirmed:
        trial.prepare_network_budget()
        assert json.loads((tmp_path / "network-budget.json").read_text())["installed"]
    else:
        with pytest.raises(ValueError, match="quota not confirmed"):
            trial.prepare_network_budget()
        assert not (tmp_path / "network-budget.json").exists()
    assert "OUTPUT 1 ! -o lo" in commands[0] and "INPUT 1 ! -i lo" in commands[0]
    assert "ip -6 -o addr show scope global" in commands[0]
