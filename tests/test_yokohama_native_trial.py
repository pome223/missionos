"""Cloud-controller contracts with inert resource fixtures; never calls GCP."""

from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import yokohama_native_trial as controller
from src.runtime import yokohama_execution_service as vehicle


@pytest.fixture
def plan(tmp_path, monkeypatch):
    # Cloud/lifecycle contracts use inert local prerequisites. The production
    # local Docker boundary and admission failures have dedicated regressions.
    def qualify_local(trial):
        trial.local_preparation = SimpleNamespace(assert_fresh=lambda: None, assert_age=lambda: None)
    monkeypatch.setattr(controller.Trial, "qualify_local", qualify_local)
    payload = tmp_path / "reviewed-payload"
    payload.mkdir()
    for name in controller.PAYLOAD:
        source = controller.REPO / "scripts" / name
        (payload / name).write_bytes(source.read_bytes() if source.is_file() else b"offline fixture")
    cpu = tmp_path / "cpu"
    cpu.mkdir()
    names = ["result.json", *(f"verification-{name}.json" for name in (
        *controller.VERIFIERS, "vehicle_service"))]
    for name in names:
        (cpu / name).write_text(json.dumps(dict(status="passed", image_id="sha256:" + "a" * 64)))
    return dict(
        schema=controller.SCHEMA, project="offline-project", instance="offline-owned-attempt",
        trial_directory=str(tmp_path),
        zone="us-west1-a", image=controller.IMAGES[1], max_runtime_s=3600, max_attempts=1,
        reserve_usd=2, aggregate_budget_usd=2, hard_network_byte_cap=False,
        budget_id="0123456789abcdef0123456789abcdef", budget_scope="incremental_single_attempt",
        cost_estimate={"usd_before_tax_fx": 1.41},
        guaranteed_spend_cap=False,
        local_image_id="sha256:" + "a" * 64,
        local_preflight_sha256=controller.digest_file(controller.REPO / "scripts/yokohama_local_preflight.py"),
        controller_sha256=controller.digest_file(controller.REPO / "scripts/yokohama_native_trial.py"),
        lifecycle_helper_sha256=controller.digest_file(controller.REPO / "scripts/yokohama_cloud_lifecycle.py"),
        source_sha256=vehicle.input_hashes(controller.REPO), payload_directory=str(payload),
        verifier_sha256={name: controller.digest_file(controller.REPO / name)
                         for name in controller.VERIFIER_SOURCES},
        payload_sha256={name: controller.digest_file(payload / name) for name in controller.PAYLOAD},
        cpu_run_directory=str(cpu),
        cpu_qualification_sha256={name: controller.digest_file(cpu / name) for name in names},
        gcloud="not-an-executable", ssh_key="offline-placeholder", python="not-an-executable",
    )


def approval(plan):
    return dict(approved_plan_sha256=vehicle.proposal_digest(plan), reference="offline-test",
                actor="offline-test", approved_at="2026-10-02T00:00:00Z",
                approve_ephemeral_cleanup=True, acknowledge_no_hard_network_byte_cap=True,
                acknowledge_estimate_not_spend_cap=True)


def test_plan_is_bound_to_sources_payload_and_cpu_evidence(plan):
    controller.validate_plan(plan)
    (Path(plan["payload_directory"]) / "bootstrap.sh").write_text("changed")
    with pytest.raises(ValueError, match="Payload changed"):
        controller.validate_plan(plan)


@pytest.mark.parametrize(("field", "value"), [
    ("schema", "missionos.yokohama-native-trial.v1"),
    ("reserve_usd", 2.5), ("aggregate_budget_usd", 5),
    ("budget_scope", "reuse_previous_attempt"), ("budget_id", ""),
    ("max_attempts", 2), ("max_runtime_s", 3601),
])
def test_incremental_budget_does_not_accept_old_or_broader_envelope(plan, field, value):
    plan[field] = value
    with pytest.raises(ValueError):
        controller.validate_plan(plan)


@pytest.mark.parametrize("estimate", [None, True, "1.41", -1, 0, 2.01, float("nan"), float("inf")])
def test_estimate_must_fit_new_planning_reserve(plan, estimate):
    plan["cost_estimate"]["usd_before_tax_fx"] = estimate
    with pytest.raises(ValueError, match="estimate"):
        controller.validate_plan(plan)


def test_new_budget_cannot_reuse_old_approval_or_consumed_attempt(plan, tmp_path):
    old_approval = approval(plan)
    ledger = dict(plan_sha256=vehicle.proposal_digest(plan), budget_id=plan["budget_id"],
                  reserved_usd=2, reserved_at=controller.now().isoformat(), create_submitted=True)
    attempt = tmp_path / "attempt.json"
    attempt.write_text(json.dumps(ledger))
    original = attempt.read_bytes()
    plan["budget_id"] = "a" * 32
    with pytest.raises(ValueError, match="approval"):
        controller.validate_approval(plan, old_approval)
    with pytest.raises(ValueError, match="another plan"):
        controller.Trial(plan, tmp_path)
    assert attempt.read_bytes() == original


@pytest.mark.parametrize("field", ["budget_id", "reserved_usd"])
def test_reconstruction_refuses_inconsistent_budget_ledger(plan, tmp_path, field):
    ledger = dict(plan_sha256=vehicle.proposal_digest(plan), budget_id=plan["budget_id"],
                  reserved_usd=2, reserved_at=controller.now().isoformat(), create_submitted=True)
    ledger.pop(field)
    (tmp_path / "attempt.json").write_text(json.dumps(ledger))
    with pytest.raises(ValueError, match="budget reservation"):
        controller.Trial(plan, tmp_path)


@pytest.mark.parametrize("field", ["approved_plan_sha256", "approve_ephemeral_cleanup",
                                   "acknowledge_no_hard_network_byte_cap", "reference",
                                   "acknowledge_estimate_not_spend_cap"])
def test_paid_execution_rejects_missing_exact_approval_without_cloud_calls(plan, tmp_path, field):
    value = approval(plan)
    value.pop(field)
    trial = controller.Trial(plan, tmp_path)
    trial.preflight = lambda: pytest.fail("approval must precede even read-only cloud preflight")
    with pytest.raises(ValueError, match="approval"):
        trial.execute(value)
    assert not trial.attempt.exists()


def test_create_uses_absolute_deadline_owned_labels_and_auto_delete(plan):
    deadline = controller.now() + timedelta(seconds=3600)
    args = controller.create_arguments(plan, deadline)
    assert args[args.index("--termination-time") + 1] == deadline.isoformat()
    assert args[args.index("--instance-termination-action") + 1] == "DELETE"
    assert "--max-run-duration" not in args
    assert "--async" in args and controller.RUNTIME_S == 3600
    assert "--no-restart-on-failure" in args and "--boot-disk-auto-delete" in args
    assert "--no-service-account" in args and "--no-scopes" in args
    assert controller.labels(plan)["missionos-attempt"] in args[args.index("--labels") + 1]


@pytest.mark.parametrize("zone", ["us-west1-a", "us-west1-b"])
def test_approved_zone_binds_create_operation_and_cleanup(plan, tmp_path, zone):
    plan["zone"] = zone
    controller.validate_plan(plan)
    trial = controller.Trial(plan, tmp_path)
    trial.create_reserved_at = controller.now()
    args = controller.create_arguments(plan, trial.create_reserved_at + timedelta(seconds=3600))
    assert args[args.index("--zone") + 1] == zone
    op = operation(trial, "DONE")
    assert trial.operation_matches(op)
    other = "us-west1-b" if zone == "us-west1-a" else "us-west1-a"
    op["zone"] = "zones/" + other
    assert not trial.operation_matches(op)
    # Even another allowed zone is not ownership of this exact attempt.
    row = dict(id="101", zone="zones/" + other, labels=controller.labels(plan))
    trial.resources = lambda: {"instances": [row], "disks": []}
    trial.call = lambda *a, **k: pytest.fail("must not delete across the approved zone")
    with pytest.raises(RuntimeError, match="Refusing deletion"):
        trial.cleanup()


@pytest.mark.parametrize("zone", ["us-west1-c", "us-west2-b", "us-central1-a"])
def test_no_automatic_zone_expansion(plan, zone):
    plan["zone"] = zone
    with pytest.raises(ValueError, match="reviewed Oregon"):
        controller.validate_plan(plan)


def test_other_allowed_zone_still_requires_its_own_approval(plan):
    old = approval(plan)
    plan["zone"] = "us-west1-b"
    controller.validate_plan(plan)
    with pytest.raises(ValueError, match="approval"):
        controller.validate_approval(plan, old)


def test_approval_cannot_be_replayed_by_copying_plan_to_fresh_directory(plan, tmp_path):
    with pytest.raises(ValueError, match="different trial directory"):
        controller.Trial(plan, tmp_path / "copied-plan")


@pytest.mark.parametrize("fault", ["deadline", "auto_restart", "disk", "labels"])
def test_provider_settings_must_confirm_deadline_and_ownership(plan, tmp_path, fault):
    deadline = controller.now() + timedelta(seconds=5400)
    row = dict(id="101", zone="zones/us-west1-a", labels=controller.labels(plan),
               disks=[{"autoDelete": True}], scheduling=dict(terminationTime=deadline.isoformat(),
               instanceTerminationAction="DELETE", automaticRestart=False))
    if fault == "deadline":
        row["scheduling"]["terminationTime"] = (deadline + timedelta(seconds=1)).isoformat()
    elif fault == "auto_restart":
        row["scheduling"]["automaticRestart"] = True
    elif fault == "disk":
        row["disks"][0]["autoDelete"] = False
    else:
        row["labels"] = {"owner": "somebody-else"}
    trial = controller.Trial(plan, tmp_path)
    trial.resources = lambda: {"instances": [row], "disks": []}
    with pytest.raises(RuntimeError):
        trial.confirm_created(deadline)


def test_execute_binds_floor_millisecond_deadline_before_provider_roundtrip(plan, tmp_path, monkeypatch):
    fixed = datetime(2026, 10, 3, 6, 7, 17, 792761, tzinfo=timezone.utc)
    monkeypatch.setattr(controller, "now", lambda: fixed)
    trial = controller.Trial(plan, tmp_path)
    trial.preflight = lambda: {"offline": True}
    deadlines = []

    def create(args, name, **kwargs):
        deadline = datetime.fromisoformat(args[args.index("--termination-time") + 1])
        deadlines.append(deadline)
        (tmp_path / "create.stdout").write_text(json.dumps([operation(trial, "DONE")]))
        row = dict(id="101", zone="zones/us-west1-a", labels=controller.labels(plan),
                   disks=[{"autoDelete": True}], scheduling=dict(
                       terminationTime=deadline.isoformat(timespec="milliseconds"),
                       instanceTerminationAction="DELETE", automaticRestart=False))
        trial.resources = lambda: {"instances": [row], "disks": []}

    trial.call = create
    trial.settle_create = lambda: operation(trial, "DONE")
    trial.bootstrap = lambda: (_ for _ in ()).throw(RuntimeError("stop after deadline acceptance"))
    trial.cleanup = lambda: None
    assert trial.execute(approval(plan)) == 1
    assert (tmp_path / "created.json").is_file()
    expected = (fixed + timedelta(seconds=3600)).replace(microsecond=792000)
    assert deadlines == [expected]
    assert datetime.fromisoformat(json.loads(trial.attempt.read_text())["deadline"]) == expected
    assert expected <= fixed + timedelta(seconds=3600)


@pytest.mark.parametrize("fault", ["labels", "id", "zone"])
def test_cleanup_refuses_unrelated_resource(plan, tmp_path, fault):
    row = dict(id="101", zone="zones/us-west1-a", labels=controller.labels(plan))
    trial = controller.Trial(plan, tmp_path)
    trial.instance_id = "101"
    if fault == "labels":
        row["labels"] = {"owner": "unrelated"}
    elif fault == "id":
        row["id"] = "102"
    else:
        row["zone"] = "zones/us-central1-a"
    trial.resources = lambda: {"instances": [row], "disks": []}
    trial.call = lambda *a, **k: pytest.fail("must not delete an unrelated resource")
    with pytest.raises(RuntimeError, match="Refusing deletion"):
        trial.cleanup()


def test_cleanup_requires_both_instance_and_disk_absence(plan, tmp_path):
    trial = controller.Trial(plan, tmp_path)
    trial.resources = lambda: {"instances": [], "disks": [{"id": "orphan", "name": "unknown"}]}
    with pytest.raises(RuntimeError, match="not confirmed"):
        trial.cleanup()
    assert not json.loads((tmp_path / "cleanup.json").read_text())["instance_and_boot_disk_absent"]


@pytest.mark.parametrize("zone", ["us-west1-a", "us-west1-b"])
def test_cleanup_deletes_only_recorded_unattached_boot_disk(plan, tmp_path, zone):
    plan["zone"] = zone
    trial = controller.Trial(plan, tmp_path)
    trial.disk_ids = {plan["instance"]: "201"}
    disk = dict(id="201", name=plan["instance"], zone="zones/" + zone, users=[])
    inventories = iter([{"instances": [], "disks": [disk]},
                        {"instances": [], "disks": [disk]},
                        {"instances": [], "disks": []}])
    trial.resources = lambda: next(inventories)
    calls = []
    trial.call = lambda *a, **k: calls.append(a[0])
    trial.cleanup()
    assert len(calls) == 1 and calls[0][:4] == ["compute", "disks", "delete", plan["instance"]]
    assert calls[0][calls[0].index("--zone") + 1] == zone
    assert json.loads((tmp_path / "cleanup.json").read_text())["instance_and_boot_disk_absent"]


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_bootstrap_or_shutdown_failure_still_cleans_up_once(plan, tmp_path, failure):
    trial = controller.Trial(plan, tmp_path)
    calls = []
    trial.preflight = lambda: {"offline": True}
    def create(args, name, **kwargs):
        calls.append(name)
        (tmp_path / "create.stdout").write_text(json.dumps([operation(trial, "DONE")]))

    trial.call = create
    trial.settle_create = lambda: operation(trial, "DONE")
    trial.confirm_created = lambda _: None

    def bootstrap():
        trial.remote_accessible = True
        raise failure("offline bootstrap failure")

    def diagnostic(*args, **kwargs):
        raise RuntimeError("offline diagnostic failure")

    trial.bootstrap = bootstrap
    trial.ssh = trial.collect = diagnostic
    trial.cleanup = lambda: calls.append("cleanup")
    assert trial.execute(approval(plan)) == 1
    assert calls == ["create", "cleanup"]
    assert trial.attempt.is_file()
    ledger_before = trial.attempt.read_bytes()
    ledger = json.loads(ledger_before)
    result = json.loads((tmp_path / "controller-result.json").read_text())
    assert ledger["reserved_usd"] == result["reserved_usd"] == 2
    assert ledger["budget_id"] == result["budget_id"] == plan["budget_id"]
    with pytest.raises(FileExistsError):
        trial.execute(approval(plan))
    assert calls == ["create", "cleanup"]
    assert trial.attempt.read_bytes() == ledger_before


def test_payload_mutated_after_preflight_does_not_start_paid_resource(plan, tmp_path):
    trial = controller.Trial(plan, tmp_path)

    def preflight():
        (Path(plan["payload_directory"]) / "bootstrap.sh").write_text("changed after validation")
        return {"offline": True}

    trial.preflight = preflight
    trial.call = lambda *a, **k: pytest.fail("must not create")
    with pytest.raises(ValueError, match="during admission"):
        trial.execute(approval(plan))
    assert not trial.attempt.exists()


def test_default_real_cli_is_offline_and_execution_requires_explicit_approval(plan, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    argv = [sys.executable, str(controller.REPO / "scripts/yokohama_native_trial.py"),
            "--plan", str(path)]
    # The plan's gcloud is intentionally not executable. Description must work
    # without cloud access, while accidental --execute cannot pass admission.
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    description = json.loads(result.stdout)
    assert description["paid_resources_started"] is False
    assert description["reserve_usd"] == 2 and description["budget_id"] == plan["budget_id"]
    refusal = subprocess.run([*argv, "--execute"], capture_output=True, text=True, timeout=30)
    assert refusal.returncode == 2 and "requires --operator-approval" in refusal.stderr
    assert not (tmp_path / "attempt.json").exists()


def operation(trial, status):
    return dict(name="operation-offline", operationType="insert", status=status,
                insertTime=(trial.create_reserved_at + timedelta(seconds=1)).isoformat(),
                targetLink=(f"https://www.googleapis.com/compute/v1/projects/{trial.plan['project']}"
                            f"/zones/{trial.plan['zone']}/instances/{trial.plan['instance']}"),
                zone="zones/" + trial.plan["zone"])


def source_mutation_fixture(plan, tmp_path, monkeypatch):
    """Real file mutation in an isolated source tree, never this checkout."""
    root = tmp_path / "sources"
    names = {*vehicle.SOURCES, *controller.VERIFIER_SOURCES,
             "scripts/yokohama_native_trial.py", "scripts/yokohama_cloud_lifecycle.py",
             *("scripts/" + p for p in controller.PAYLOAD if p.endswith(".py") and p != "remote_lifecycle.py")}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((controller.REPO / name).read_bytes())
    plan["source_sha256"] = vehicle.input_hashes(root)
    validate = controller.validate_plan
    monkeypatch.setattr(controller, "validate_plan", lambda p: validate(p, root))
    return root / "scripts/yokohama_decision_worker.py"


def test_source_mutation_after_preflight_prevents_create(plan, tmp_path, monkeypatch):
    source = source_mutation_fixture(plan, tmp_path, monkeypatch)
    trial = controller.Trial(plan, tmp_path)

    def preflight():
        source.write_text(source.read_text() + "\n# changed after preflight\n")
        return {"offline": True}

    trial.preflight = preflight
    trial.call = lambda *a, **k: pytest.fail("must not create after source mutation")
    trial.cleanup = lambda: None
    assert trial.execute(approval(plan)) == 1
    assert not trial.create_submitted
    assert "sources changed" in (tmp_path / "controller-result.json").read_text()


def test_changed_verifier_after_flight_is_not_used(plan, tmp_path, monkeypatch):
    source = source_mutation_fixture(plan, tmp_path, monkeypatch)
    verifier = source.parent / "verify_yokohama_pad_queue.py"
    verifier.write_text(verifier.read_text() + "\n# changed after approval\n")
    trial = controller.Trial(plan, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not use changed verifier"))
    with pytest.raises(ValueError, match="Verifier entrypoints changed"):
        trial.verify_flight(tmp_path / "flight", {}, {})


@pytest.mark.parametrize("after_verifier", [1, len(controller.VERIFIERS)])
def test_source_drift_between_verifiers_stops_remaining_verification(plan, tmp_path, monkeypatch,
                                                                  after_verifier):
    source = source_mutation_fixture(plan, tmp_path, monkeypatch)
    target = source.parent / "verify_yokohama_pad_queue.py" if after_verifier == 1 else source
    trial = controller.Trial(plan, tmp_path)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) == after_verifier:
            target.write_text(target.read_text() + "\n# changed during verification\n")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(vehicle, "verify_receipt", lambda *a: pytest.fail("must not verify changed sources"))
    with pytest.raises(ValueError, match="changed since planning"):
        trial.verify_flight(tmp_path / "flight", {}, {})
    assert len(calls) == after_verifier


def test_source_mutation_during_bootstrap_prevents_flight(plan, tmp_path, monkeypatch):
    source = source_mutation_fixture(plan, tmp_path, monkeypatch)
    trial = controller.Trial(plan, tmp_path)
    trial.preflight = lambda: {"offline": True}
    calls = []

    def create(args, name, **kwargs):
        calls.append(name)
        (tmp_path / "create.stdout").write_text(json.dumps([operation(trial, "DONE")]))

    trial.call = create
    trial.settle_create = lambda: operation(trial, "DONE")
    trial.confirm_created = lambda _: None
    trial.bootstrap = lambda: source.write_text(source.read_text() + "\n# changed during bootstrap\n")
    trial.cleanup = lambda: calls.append("cleanup")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch flight/tunnel"))
    assert trial.execute(approval(plan)) == 1
    assert calls == ["create", "cleanup"]
    assert not (tmp_path / "flight").exists()


def test_compiled_flight_preserves_original_plan_hashes(plan, tmp_path, monkeypatch):
    trial = controller.Trial(plan, tmp_path)
    trial.validate_execution_binding = lambda _: None  # revalidation is covered above
    monkeypatch.setattr(vehicle, "input_hashes", lambda *a: pytest.fail("must not reapprove current files"))

    class Finished:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Finished())
    _, proposal, _ = trial.fly(approval(plan), controller.now() + timedelta(seconds=3600))
    assert {k: v for k, v in proposal["input_sha256"].items() if k != "native_service_config"} == plan["source_sha256"]


@pytest.mark.parametrize("slow_tunnel", [False, True])
def test_flight_keeps_pinned_image_endpoint_and_rechecks_age(plan, tmp_path, monkeypatch, slow_tunnel):
    trial = controller.Trial(plan, tmp_path)
    trial.validate_execution_binding = lambda _: None
    trial.local_environment = {"PATH": "fixture", "DOCKER_HOST": "unix:///verified.sock"}
    monkeypatch.setenv("DOCKER_CONTEXT", "changed-after-qualification")
    clock, calls = [0], []

    def age():
        if clock[0] > 30:
            raise ValueError("Local prerequisite check is stale")

    def qualify():
        trial.local_preparation = SimpleNamespace(assert_fresh=age, assert_age=age)

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1 and slow_tunnel:
            clock[0] = 31
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    trial.qualify_local = qualify
    monkeypatch.setattr(subprocess, "Popen", popen)
    if slow_tunnel:
        with pytest.raises(ValueError, match="stale"):
            trial.fly(approval(plan), controller.now() + timedelta(seconds=3600))
        assert len(calls) == 1
    else:
        _, proposal, _ = trial.fly(approval(plan), controller.now() + timedelta(seconds=3600))
        args = proposal["simulator_arguments"]
        assert args[args.index("--local-image-id") + 1] == plan["local_image_id"]
        assert len(calls) == 2
        assert calls[1][1]["env"] == trial.local_environment
        assert "DOCKER_CONTEXT" not in calls[1][1]["env"]


def test_compiled_native_config_does_not_reapprove_replaced_output_file(plan, tmp_path, monkeypatch):
    trial = controller.Trial(plan, tmp_path)
    trial.validate_execution_binding = lambda _: None
    original_write = controller.write
    compiled = []

    def replace_output(path, value):
        original_write(path, value)
        if Path(path).name == "native-service.json":
            compiled.append(Path(path).read_bytes())
            Path(path).write_text('{"start_argv":["unapproved-command"]}')

    class Finished:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(controller, "write", replace_output)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Finished())
    _, proposal, _ = trial.fly(approval(plan), controller.now() + timedelta(seconds=3600))
    assert proposal["input_sha256"]["native_service_config"] == hashlib.sha256(compiled[0]).hexdigest()
    assert proposal["input_sha256"]["native_service_config"] != controller.digest_file(tmp_path / "native-service.json")


@pytest.mark.parametrize("mode", ["already_exited", "sigint_exit", "ignores_sigint"])
def test_stop_child_terminates_stubborn_descendant_and_preserves_sibling(tmp_path, mode):
    marker = tmp_path / "descendant.pid"
    parent_ready = tmp_path / "parent.ready"
    child_code = ("import os,signal,time;from pathlib import Path;"
                  "signal.signal(signal.SIGINT,signal.SIG_IGN);"
                  f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)")
    parent_code = ("import subprocess,sys,time,signal;from pathlib import Path\n"
                   f"subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                   f"while not Path({str(marker)!r}).exists():time.sleep(.01)\n")
    if mode == "ignores_sigint":
        parent_code += "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
    parent_code += f"Path({str(parent_ready)!r}).write_text('ready')\n"
    if mode != "already_exited":
        parent_code += "while True:time.sleep(.05)\n"
    parent = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sibling = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                               start_new_session=True)
    try:
        end = time.monotonic() + 5
        while not parent_ready.exists():
            assert time.monotonic() < end
            time.sleep(.01)
        descendant = int(marker.read_text())
        if mode == "already_exited":
            parent.wait(timeout=5)
        controller.Trial.stop_child(parent, grace=.2)
        assert parent.poll() is not None and sibling.poll() is None
        end = time.monotonic() + 5
        while True:
            probe = subprocess.run(["ps", "-p", str(descendant), "-o", "stat="], capture_output=True, text=True)
            assert not probe.stderr.strip(), probe.stderr
            if probe.returncode or not probe.stdout.strip() or probe.stdout.strip().startswith("Z"):
                break
            assert time.monotonic() < end, "descendant survived group cleanup"
            time.sleep(.02)
    finally:
        for owned in (parent, sibling):
            try:
                os.killpg(owned.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            owned.wait(timeout=5)


def test_uncertain_create_waits_for_terminal_operation_before_deletion(plan, tmp_path, monkeypatch):
    trial = controller.Trial(plan, tmp_path)
    trial.preflight = lambda: {"offline": True}
    calls, discovered = [], []
    terminal = False
    deleted = False

    def query(args, name, **kwargs):
        nonlocal terminal
        if "list" in args:
            discovered.append(True)
            return [] if len(discovered) == 1 else [operation(trial, "RUNNING")]
        terminal = True
        return operation(trial, "DONE")

    def resources():
        assert terminal, "resource absence must not be considered before insert is terminal"
        row = dict(id="101", labels=controller.labels(plan), zone="zones/us-west1-a", disks=[])
        return {"instances": [] if deleted else [row], "disks": []}

    def call(args, name, **kwargs):
        nonlocal deleted
        calls.append(name)
        if name == "create":
            raise subprocess.TimeoutExpired(args, 180)
        assert name == "delete-owned-instance"
        deleted = True

    trial.query, trial.resources, trial.call = query, resources, call
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert trial.execute(approval(plan)) == 1  # uncertainty is not converted into a flight pass
    assert calls == ["create", "delete-owned-instance"]
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert receipt["create_operation_terminal"] and receipt["cleanup_confirmed"]


@pytest.mark.parametrize("status", [None, "RUNNING"])
def test_unknown_or_pending_create_never_confirms_empty_inventory(plan, tmp_path, status):
    trial = controller.Trial(plan, tmp_path)
    trial.create_submitted = True
    trial.create_reserved_at = controller.now()
    trial.query = lambda *a, **k: ([] if status is None else (
        [operation(trial, status)] if "list" in a[0] else operation(trial, status)))
    trial.resources = lambda: pytest.fail("must not accept early absence")
    settle = trial.settle_create
    trial.settle_create = lambda: settle(timeout=0)
    with pytest.raises(TimeoutError, match="absence is not final"):
        trial.cleanup()
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert not receipt["cleanup_confirmed"] and not receipt["instance_and_boot_disk_absent"]


def test_reconstructed_controller_preserves_uncertain_submission(plan, tmp_path):
    (tmp_path / "attempt.json").write_text(json.dumps(dict(
        plan_sha256=vehicle.proposal_digest(plan), reserved_at=controller.now().isoformat(),
        budget_id=plan["budget_id"], reserved_usd=2,
        create_submitted=True)))
    trial = controller.Trial(plan, tmp_path)
    assert trial.create_submitted
    trial.query = lambda *a, **k: []
    trial.resources = lambda: pytest.fail("empty inventory cannot settle a recovered submission")
    settle = trial.settle_create
    trial.settle_create = lambda: settle(timeout=0)
    with pytest.raises(TimeoutError):
        trial.cleanup()
    assert not json.loads((tmp_path / "cleanup.json").read_text())["cleanup_confirmed"]


@pytest.mark.parametrize("grow_after_pack", [False, True])
def test_evidence_transfer_is_sender_bounded_even_if_archive_changes(plan, tmp_path, monkeypatch, grow_after_pack):
    monkeypatch.setattr(controller, "EVIDENCE_LIMIT", 1024)
    home = tmp_path / "fake-remote-home"
    remote = home / "yokohama-native"
    remote.mkdir(parents=True)
    (remote / "response.txt").write_text("bounded test evidence")
    transport = tmp_path / "gcloud-offline-transport"
    transport.write_text(
        "#!" + sys.executable + "\nimport os,shlex,subprocess,sys\n"
        "args=shlex.split(sys.argv[sys.argv.index('--command')+1])\n"
        "args[0]=sys.executable\n"
        f"raise SystemExit(subprocess.run(args,env=dict(os.environ,HOME={str(home)!r})).returncode)\n"
    )
    transport.chmod(0o755)
    plan["gcloud"] = str(transport)
    trial = controller.Trial(plan, tmp_path)

    def pack(command, name, **kwargs):
        args = shlex.split(command)
        args[0] = sys.executable
        packed = subprocess.run(args, capture_output=True, check=True,
                                env=dict(os.environ, HOME=str(home)))
        (tmp_path / "collect.stdout").write_bytes(packed.stdout)
        if grow_after_pack:
            with (remote / "evidence.tar.gz").open("ab") as stream:
                stream.write(b"X" * 2048)

    trial.ssh = pack
    if grow_after_pack:
        with pytest.raises(ValueError, match="digest mismatch"):
            trial.collect()
    else:
        trial.collect()
    assert (tmp_path / "remote-evidence.tar.gz").stat().st_size <= 1024
