"""Bounded orchestration over the real controller with inert GCP/flight fixtures."""

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import yokohama_capacity_retry as group
from scripts import yokohama_native_trial as single
from test_yokohama_native_trial import approval, operation, plan  # noqa: F401


@pytest.fixture
def bundle(plan, tmp_path):  # noqa: F811
    directory = tmp_path / "group"
    directory.mkdir()
    plan.update(budget_scope=single.GROUP_SCOPE, reserve_usd=5, aggregate_budget_usd=5,
                capacity_group_id="b" * 32,
                group_controller_sha256=single.digest_file(
                    single.REPO / "scripts/yokohama_capacity_retry.py"))
    children = []
    for index in range(1, 7):
        child = copy.deepcopy(plan)
        child.update(instance=f"offline-attempt-{index}",
                     trial_directory=str(directory / f"attempt-{index:02d}"),
                     zone=single.ZONES[(index - 1) % 2])
        children.append(child)
    value = dict(schema=group.SCHEMA, group_directory=str(directory), group_id="b" * 32,
                 budget_id=plan["budget_id"], aggregate_budget_usd=5, max_attempts=6,
                 min_interval_s=600, max_flights=1, stop_after_resource_success=True,
                 abort_on_unknown=True, guaranteed_spend_cap=False, hard_network_byte_cap=False,
                 children=children)
    (directory / "plan.json").write_text(json.dumps(value))
    (directory / "approval.json").write_text(json.dumps(approval(value)))
    return group.Group(directory / "plan.json", directory / "approval.json")


@pytest.fixture
def harness(monkeypatch):
    class Harness:
        modes = ["capacity"] * 6
        events = []
        trials = []
        clock = 0.0
        live = 0

        def sleep(self, seconds):
            assert 0 <= seconds <= 30
            self.clock += seconds

    state = Harness()

    class FixtureTrial(single.Trial):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.mode = state.modes[len(state.trials)]
            state.trials.append(self)
            self.live = False
            self.op = None

        def preflight(self):
            single.validate_plan(self.plan)
            assert not self.attempt.exists()
            assert state.live == 0
            return {"fixture": True}

        def call(self, args, name, **kwargs):
            if name == "create":
                self.submitted_deadline = args[args.index("--termination-time") + 1]
                assert state.live == 0
                state.events.append(("create", state.clock))
                self.op = operation(self, "DONE")
                if self.mode != "success" and self.mode != "bootstrap_failure":
                    self.op["error"] = {"errors": [{"code": "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"}]}
                if self.mode in ("success", "bootstrap_failure", "allocation_then_capacity"):
                    self.live = True
                    state.live += 1
                if self.mode == "mixed_error":
                    self.op["error"]["errors"].append({"code": "PERMISSION_DENIED"})
                (self.directory / "create.stdout").write_text(json.dumps([self.op]))
                if self.mode == "interrupt":
                    raise KeyboardInterrupt("fixture interrupt")
                if self.mode == "unknown":
                    self.op["status"] = "RUNNING"
                    raise TimeoutError("fixture unknown create")
            elif name == "delete-owned-instance":
                state.events.append(("delete", state.clock))
                self.live = False
                state.live -= 1
            else:
                raise AssertionError("Unexpected cloud call: " + name)
            return 0

        def query(self, args, name, **kwargs):
            if args[0] == "operations":
                return [self.op] if args[1] == "list" else self.op
            if self.mode == "inventory_error":
                raise RuntimeError("fixture inventory read failure")
            if args[0] == "instances" and self.live:
                return [dict(id="101", name=self.plan["instance"], zone="zones/" + self.plan["zone"],
                             labels=single.labels(self.plan), disks=[dict(autoDelete=True)],
                             scheduling=dict(terminationTime=self.submitted_deadline,
                                             instanceTerminationAction="DELETE", automaticRestart=False))]
            return []

        def settle_create(self, *args, **kwargs):
            if self.mode == "unknown":
                raise TimeoutError("fixture unresolved insert")
            return super().settle_create(*args, **kwargs)

        def bootstrap(self):
            state.events.append(("bootstrap", state.clock))
            if self.mode == "bootstrap_failure":
                raise RuntimeError("fixture bootstrap failed")

        def fly(self, *args):
            state.events.append(("fly", state.clock))
            return None  # flight/verifier production boundaries have separate tests

    monkeypatch.setattr(single, "Trial", FixtureTrial)
    monkeypatch.setattr(group.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(group.time, "sleep", state.sleep)
    return state


def test_six_capacity_failures_have_one_shared_reserve_and_no_overlap(bundle, harness):
    assert bundle.execute() == 1
    assert bundle.state["status"] == "capacity_exhausted"
    assert [e for e in harness.events if e[0] == "create"] == [
        ("create", float(n * 600)) for n in range(6)]
    assert len(bundle.state["completed"]) == 6 and harness.live == 0
    assert bundle.state["reserved_usd"] == 5 and bundle.state["reservation_is_shared"] is True
    for trial in harness.trials:
        assert group.read(trial.attempt)["reservation_is_shared"] is True
        assert group.read(trial.directory / "capacity-retry-confirmation.json")["retry_eligible"] is True
    before = bundle.ledger_path.read_bytes()
    with pytest.raises(FileExistsError):
        group.Group(bundle.plan_path, bundle.approval_path).execute()
    assert bundle.ledger_path.read_bytes() == before
    assert len(harness.trials) == 6


@pytest.mark.parametrize("mode,expected", [("success", "passed"),
    ("bootstrap_failure", "stopped_nonretryable"), ("allocation_then_capacity", "stopped_nonretryable"),
    ("mixed_error", "stopped_nonretryable"), ("interrupt", "stopped_nonretryable"),
    ("unknown", "stopped_nonretryable"), ("inventory_error", "stopped_nonretryable")])
def test_first_noncapacity_or_any_allocation_stops_group(bundle, harness, mode, expected):
    harness.modes = ["capacity", mode, "success", "success", "success", "success"]
    assert bundle.execute() == (0 if mode == "success" else 1)
    assert bundle.state["status"] == expected
    assert len(harness.trials) == 2
    assert len([e for e in harness.events if e[0] == "fly"]) == (1 if mode == "success" else 0)
    assert harness.live == 0
    if mode in ("success", "bootstrap_failure", "allocation_then_capacity"):
        assert (harness.trials[-1].directory / "resource-observed.json").is_file()


def test_group_child_cannot_run_via_standalone_controller(bundle, monkeypatch):
    child = bundle.plan["children"][0]
    directory = Path(child["trial_directory"])
    directory.mkdir()
    trial = single.Trial(child, directory)
    monkeypatch.setattr(trial, "preflight", lambda: pytest.fail("must reject before cloud reads"))
    with pytest.raises(ValueError, match="owning group"):
        trial.execute(approval(child))
    assert not trial.attempt.exists()


@pytest.mark.parametrize("fault", ["approval", "plan", "ledger"])
def test_changed_authorization_between_attempts_aborts(bundle, harness, fault):
    def mutate(_):
        if fault == "approval":
            value = group.read(bundle.approval_path)
            value["actor"] = "replaced"
            bundle.approval_path.write_text(json.dumps(value))
        elif fault == "plan":
            value = group.read(bundle.plan_path)
            value["children"][1]["zone"] = "us-west1-a"
            bundle.plan_path.write_text(json.dumps(value))
        else:
            value = group.read(bundle.ledger_path)
            value["reserved_usd"] = 50
            bundle.ledger_path.write_text(json.dumps(value))

    bundle.wait_interval = mutate
    assert bundle.execute() == 1
    assert len([e for e in harness.events if e[0] == "create"]) == 1
    assert bundle.state["status"] == "aborted"


@pytest.mark.parametrize(("key", "value"), [("max_attempts", 7), ("min_interval_s", 599),
    ("aggregate_budget_usd", 6), ("max_flights", 2), ("abort_on_unknown", False),
    ("stop_after_resource_success", False), ("guaranteed_spend_cap", True)])
def test_envelope_cannot_expand(bundle, key, value):
    bundle.plan[key] = value
    with pytest.raises(ValueError, match="envelope"):
        group.validate_plan(bundle.plan)


@pytest.mark.parametrize("fault", ["same_name", "directory", "budget", "group", "config", "source"])
def test_child_identity_and_source_bindings(bundle, fault):
    child = bundle.plan["children"][1]
    if fault == "same_name":
        child["instance"] = bundle.plan["children"][0]["instance"]
    elif fault == "directory":
        child["trial_directory"] += "-copied"
    elif fault == "budget":
        child["budget_id"] = "c" * 32
    elif fault == "group":
        child["capacity_group_id"] = "c" * 32
    elif fault == "config":
        child["image"] = single.IMAGES[0]
    else:
        child["group_controller_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        group.validate_plan(bundle.plan)


def test_group_default_real_cli_is_offline(bundle):
    argv = [sys.executable, str(single.REPO / "scripts/yokohama_capacity_retry.py"),
            "--plan", str(bundle.plan_path)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["paid_resources_started"] is False
    refusal = subprocess.run([*argv, "--execute"], capture_output=True, text=True, timeout=30)
    assert refusal.returncode == 2 and "requires --operator-approval" in refusal.stderr
    assert not bundle.ledger_path.exists()


@pytest.mark.parametrize("fault", ["cleanup_false", "terminal_false", "submitted_false",
    "unknown_resources", "plan_digest", "budget", "empty_errors", "wrong_target",
    "bootstrap_marker", "resource_marker", "extra_error", "missing_receipt"])
def test_incomplete_or_contradictory_receipts_never_retry(bundle, harness, monkeypatch, fault):
    original = group.retry_eligible

    def tamper(trial, result):
        directory = trial.directory
        if fault in ("cleanup_false", "terminal_false", "unknown_resources"):
            path = directory / "cleanup.json"
            value = group.read(path)
            key, replacement = {
                "cleanup_false": ("cleanup_confirmed", False),
                "terminal_false": ("create_operation_terminal", False),
                "unknown_resources": ("resources", {}),
            }[fault]
            value[key] = replacement
            single.write(path, value)
        elif fault in ("submitted_false", "plan_digest", "budget"):
            value = group.read(trial.attempt)
            key, replacement = {"submitted_false": ("create_submitted", False),
                                "plan_digest": ("plan_sha256", "0" * 64),
                                "budget": ("budget_id", "c" * 32)}[fault]
            value[key] = replacement
            single.write(trial.attempt, value)
        elif fault in ("empty_errors", "wrong_target"):
            if fault == "empty_errors":
                trial.op["error"]["errors"] = []
            else:
                trial.op["targetLink"] += "-other"
            single.write(directory / "create-operation.json", trial.op)
        elif fault == "bootstrap_marker":
            (directory / "bootstrap-start.stdout").write_text("")
        elif fault == "resource_marker":
            (directory / "resource-observed.json").write_text("{}")
        elif fault == "extra_error":
            path = directory / "controller-result.json"
            value = group.read(path)
            value["errors"].append("RuntimeError: unknown diagnostic failure")
            single.write(path, value)
        else:
            (directory / "cleanup.json").unlink()
        return original(trial, result)

    monkeypatch.setattr(group, "retry_eligible", tamper)
    assert bundle.execute() == 1
    assert len(harness.trials) == 1
    assert bundle.state["status"] in ("stopped_nonretryable", "aborted")


def test_inventory_observation_survives_later_read_failure(bundle, monkeypatch):
    child = bundle.plan["children"][0]
    directory = Path(child["trial_directory"])
    directory.mkdir()
    trial = single.Trial(child, directory)

    def query(args, name):
        if args[0] == "instances":
            return [{"id": "observed-before-disk-read-error"}]
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(trial, "query", query)
    with pytest.raises(RuntimeError, match="disk read failed"):
        trial.resources()
    assert group.read(directory / "resource-observed.json")["kind"] == "instances"
    assert (directory / "inventory-history-0001.json").is_file()


def test_reconstructed_trial_appends_inventory_history_without_resuming_create(bundle, harness):
    assert bundle.execute() == 1
    previous = harness.trials[0]
    histories = {p.name: p.read_bytes() for p in previous.directory.glob("inventory-history-*.json")}
    # Use the real class underlying the inert subclass. Only read/deletion paths
    # are reconstructed; execute remains forbidden by the consumed ledger.
    real = type(previous).__bases__[0]
    recovered = real(previous.plan, previous.directory)
    recovered.query = previous.query
    recovered.cleanup()
    assert group.read(previous.directory / "cleanup.json")["cleanup_confirmed"] is True
    assert len(list(previous.directory.glob("inventory-history-*.json"))) > len(histories)
    assert all((previous.directory / name).read_bytes() == raw for name, raw in histories.items())
    assert recovered.create_submitted is True


def test_approval_change_during_create_cleans_without_bootstrap(bundle, harness, monkeypatch):
    harness.modes = ["success"] * 6
    original = single.Trial.confirm_created

    def changed(trial, deadline):
        original(trial, deadline)
        value = group.read(bundle.approval_path)
        value["actor"] = "changed during create"
        bundle.approval_path.write_text(json.dumps(value))

    monkeypatch.setattr(single.Trial, "confirm_created", changed)
    assert bundle.execute() == 1
    assert len(harness.trials) == 1 and harness.live == 0
    assert not any(event[0] in ("bootstrap", "fly") for event in harness.events)


def test_approval_change_during_upload_prevents_remote_bootstrap(bundle, harness, monkeypatch):
    harness.modes = ["success"] * 6
    real_bootstrap = single.Trial.__bases__[0].bootstrap
    original_call = single.Trial.call

    def call(trial, args, name, **kwargs):
        if name == "upload":
            value = group.read(bundle.approval_path)
            value["actor"] = "changed during upload"
            bundle.approval_path.write_text(json.dumps(value))
            return 0
        return original_call(trial, args, name, **kwargs)

    def ssh(trial, command, name, **kwargs):
        assert name != "bootstrap-start", "must recheck approval after upload"
        if name == "payload-hashes":
            (trial.directory / "payload-hashes.stdout").write_text("\n".join(
                f"{digest} {name}" for name, digest in trial.plan["payload_sha256"].items()))
        return 0

    monkeypatch.setattr(single.Trial, "bootstrap", real_bootstrap)
    monkeypatch.setattr(single.Trial, "call", call)
    monkeypatch.setattr(single.Trial, "ssh", ssh)
    monkeypatch.setattr(single.Trial, "collect", lambda self: None)
    assert bundle.execute() == 1
    assert len(harness.trials) == 1 and harness.live == 0
    assert not any(event[0] == "fly" for event in harness.events)
