import copy
import errno
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tarfile
import time

import pytest

from scripts import yokohama_offline_vla_lifecycle as safety
from scripts import yokohama_offline_vla_guest as guest


def broken_display(text):
    raise OSError(errno.EIO, "CPU fixture broken TTY")


def test_logging_failure_does_not_abort_or_hide_archive(tmp_path):
    evidence = tmp_path / "evidence"
    log = safety.DurableEvents(evidence, display=broken_display, journal=broken_display)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        Path(kwargs["output"]).write_text("CPU fixture process log\n")

    archive = tmp_path / "evidence.tar.gz"
    assert safety.supervise_guest(
        ["CPU_PREP"],
        ["CPU_INFER"],
        events=log,
        prepare_deadline=time.time() + 10,
        infer_deadline=time.time() + 20,
        archive_path=archive,
        runner=runner,
    )
    assert calls == [["CPU_PREP"], ["CPU_INFER"]]
    assert json.loads((evidence / "status.json").read_text())["stage"] == "completed"
    assert (evidence / "log-channel-errors.jsonl").is_file()
    with tarfile.open(archive) as restored:
        assert "status.json" in restored.getnames()
        assert "bootstrap.log" in restored.getnames()


def test_prepare_failure_has_independent_file_receipt_and_no_inference(tmp_path):
    events = safety.DurableEvents(tmp_path / "evidence", display=broken_display)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        raise RuntimeError("CPU fixture failed preparation")

    archive = tmp_path / "evidence.tar.gz"
    assert not safety.supervise_guest(
        ["CPU_PREP"],
        ["CPU_INFER"],
        events=events,
        prepare_deadline=time.time() + 10,
        infer_deadline=time.time() + 20,
        archive_path=archive,
        runner=runner,
    )
    assert calls == [["CPU_PREP"]]
    assert json.loads((events.output / "abort.json").read_text())["type"] == "RuntimeError"
    with tarfile.open(archive) as restored:
        assert "abort.json" in restored.getnames()
        assert "supervisor-error.log" in restored.getnames()


def test_file_evidence_failure_is_not_promoted_to_success(tmp_path, monkeypatch):
    def disk_full(*args):
        raise OSError(errno.ENOSPC, "CPU fixture full disk")

    monkeypatch.setattr(safety, "write_json", disk_full)
    events = safety.DurableEvents(tmp_path, display=lambda text: None)
    with pytest.raises(OSError):
        events.emit("preparing")


def test_local_subprocess_timeout_stops_without_retry(tmp_path):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        safety.run_child(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            output=tmp_path / "child.log",
            deadline_unix_s=time.time() + 0.15,
        )
    assert time.monotonic() - started < 3


def test_local_subprocess_smoke(tmp_path):
    safety.run_child(
        [sys.executable, "-c", "print('CPU_ONLY_PROCESS_SMOKE')"],
        output=tmp_path / "child.log",
        deadline_unix_s=time.time() + 5,
    )
    assert "CPU_ONLY_PROCESS_SMOKE" in (tmp_path / "child.log").read_text()


def owned():
    vm = safety.OwnedVM(
        "cpu-project", "us-west1-b", "cpu-instance", "123", "cpu-instance", "456", "abcdefabcdef"
    )
    prefix = "https://compute.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b"
    instance = {
        "id": "123",
        "name": "cpu-instance",
        "selfLink": prefix + "/instances/cpu-instance",
        "labels": {"trial-id": "abcdefabcdef", "managed-by": "yokohama-offline-vla"},
        "disks": [{"boot": True, "autoDelete": True, "source": prefix + "/disks/cpu-instance"}],
    }
    disk = {
        "id": "456",
        "name": "cpu-instance",
        "selfLink": prefix + "/disks/cpu-instance",
        "users": [instance["selfLink"]],
    }
    return vm, instance, disk


def control(tmp_path, observation=None, sdk=None, rest=None, watchdog_observe=None):
    vm, instance, disk = owned()
    return safety.CleanupController(
        vm,
        events=safety.DurableEvents(tmp_path),
        observe=observation or (lambda: (instance, disk, True)),
        watchdog_observe=watchdog_observe or (lambda: (instance, disk, True)),
        sdk_delete=sdk or (lambda vm, argv: None),
        rest_delete=rest or (lambda vm, request: None),
        cleanup_at=375,
        watchdog_at=385,
        absence_deadline=600,
    )


def test_sdk_arguments_match_stable_delete_interface():
    argv = safety.sdk_delete_arguments(owned()[0])
    assert argv[:3] == ["compute", "instances", "delete"]
    assert "--async" not in argv
    assert "--delete-disks=boot" in argv
    assert "--quiet" in argv


@pytest.mark.parametrize(
    "guest,serial",
    [
        ({"stage": "failed"}, ""),
        (
            None,
            'google_metadata_script_runner: Script "startup-script" failed with error: exit status 120',
        ),
    ],
)
def test_startup_failure_immediately_triggers_cleanup(tmp_path, guest, serial):
    calls = []
    controller = control(tmp_path, sdk=lambda vm, args: calls.append(args))
    assert controller.tick(240, guest_status=guest, serial_text=serial) == "pending"
    controller.tick(250, guest_status=guest, serial_text=serial)
    assert len(calls) == 1


def test_completed_comparison_deletes_early(tmp_path):
    calls = []
    controller = control(tmp_path, sdk=lambda vm, args: calls.append(args))
    controller.tick(290, comparison_finished=True)
    assert len(calls) == 1


def test_watchdog_survives_primary_sdk_and_observer_failure(tmp_path):
    calls = []

    def rejected(vm, args):
        raise RuntimeError("CPU fixture SDK rejected unsupported --async")

    controller = control(tmp_path, sdk=rejected, rest=lambda vm, request: calls.append(request))
    controller.tick(375)
    controller.observe = lambda: (_ for _ in ()).throw(RuntimeError("Primary SDK unavailable"))
    controller.watchdog_tick(385)
    controller.watchdog_tick(390)
    assert len(calls) == 1
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/instances/cpu-instance")
    assert "--async" not in str(calls)


def test_broken_cleanup_logger_cannot_disable_either_delete_transport(tmp_path):
    calls = []
    controller = control(
        tmp_path,
        sdk=lambda vm, args: calls.append("SDK"),
        rest=lambda vm, request: calls.append("REST"),
    )
    controller.events.emit = lambda *args, **kwargs: broken_display("")
    controller.tick(375)
    controller.watchdog_tick(385)
    assert calls == ["SDK", "REST"]


@pytest.mark.parametrize("foreign", ["instance", "disk", "label", "user", "missing_disk"])
def test_cleanup_refuses_foreign_or_unproven_identity(tmp_path, foreign):
    vm, instance, disk = owned()
    instance, disk = copy.deepcopy(instance), copy.deepcopy(disk)
    if foreign == "instance":
        instance["id"] = "999"
    elif foreign == "disk":
        disk["id"] = "999"
    elif foreign == "label":
        instance["labels"]["trial-id"] = "ffffffffffff"
    elif foreign == "user":
        disk["users"] = ["another-instance"]
    else:
        disk = None
    calls = []
    controller = control(
        tmp_path,
        observation=lambda: (instance, disk, True),
        watchdog_observe=lambda: (instance, disk, True),
        sdk=lambda *args: calls.append(args),
        rest=lambda *args: calls.append(args),
    )
    with pytest.raises(ValueError):
        controller.tick(375)
    with pytest.raises(ValueError):
        controller.watchdog_tick(385)
    assert not calls


def test_orphan_owned_boot_disk_uses_disk_deletion(tmp_path):
    vm, instance, disk = owned()
    disk["users"] = []
    calls = []
    controller = control(
        tmp_path,
        observation=lambda: (None, disk, True),
        watchdog_observe=lambda: (None, disk, True),
        sdk=lambda vm, args: calls.append(args),
        rest=lambda vm, req: calls.append(req),
    )
    controller.tick(375)
    controller.watchdog_tick(385)
    assert calls[0][:3] == ["compute", "disks", "delete"]
    assert calls[1]["url"].endswith("/disks/cpu-instance")


def test_pending_insert_is_not_resource_absence(tmp_path):
    calls = []
    controller = control(
        tmp_path,
        observation=lambda: (None, None, False),
        watchdog_observe=lambda: (None, None, False),
        sdk=lambda *args: calls.append(args),
        rest=lambda *args: calls.append(args),
    )
    assert controller.tick(100) == "pending"
    controller.tick(400)
    controller.watchdog_tick(400)
    assert not calls


def test_late_deletion_does_not_erase_deadline_failure(tmp_path):
    controller = control(tmp_path)
    controller.tick(601)
    controller.observe = lambda: (None, None, True)
    assert controller.tick(620) == "absent"
    assert controller.deadline_failed
    assert json.loads((tmp_path / "status.json").read_text())["deadline_failed"]


def test_watchdog_keeps_observing_after_delete_and_retains_deadline_failure(tmp_path):
    calls = []
    controller = control(tmp_path, rest=lambda *args: calls.append(args))
    assert controller.watchdog_tick(385) == "pending"
    assert controller.watchdog_tick(601) == "pending"
    controller.watchdog_observe = lambda: (None, None, True)
    assert controller.watchdog_tick(620) == "absent"
    assert len(calls) == 1
    assert controller.deadline_failed
    assert json.loads((tmp_path / "status.json").read_text())["deadline_failed"]


def test_budget_keeps_prior_spending_and_no_paid_authorization():
    result = safety.budget_plan(
        spent_usd=0.4568919543032134, hourly_usd=1.179605584, egress_usd_per_gib=0.23
    )
    assert result["remaining_group_budget_USD"] == pytest.approx(0.5431080456967866)
    assert result["cost_fits_remaining_budget"]
    assert not result["current_turn_paid_execution_authorized"]
    assert not result["strict_600_s_deletion_guaranteed"]
    assert result["preparation_s"] + result["comparison_s"] + result["collection_delete_s"] == 600


@pytest.mark.parametrize("spent", [float("nan"), -1, 1.1])
def test_invalid_or_exhausted_ledger_not_reset(spent):
    with pytest.raises(ValueError):
        safety.budget_plan(spent_usd=spent, hourly_usd=1.179605584, egress_usd_per_gib=0.23)


def test_plan_cli_smoke_no_cloud_or_model(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.yokohama_offline_vla_lifecycle",
            "--spent-usd",
            "0.4568919543032134",
            "--hourly-usd",
            "1.179605584",
            "--egress-usd-per-gib",
            "0.23",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not json.loads(result.stdout)["current_turn_paid_execution_authorized"]


def test_guest_default_has_no_process_firewall_or_model(monkeypatch, tmp_path):
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **k: pytest.fail("No process allowed"))
    assert not guest.run_guest(tmp_path, {})["execution"]
    assert not list(tmp_path.iterdir())


def test_startup_template_uses_guest_agent_output_no_tty_redirect():
    script = safety.startup_script(["/usr/bin/python3", "-u", "sources/guest.py"])
    assert "/dev/tty" not in script and "exec >" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.mark.parametrize(
    "initial",
    [
        {"rx_bytes": -1, "tx_bytes": 0},
        {"rx_bytes": guest.RX_LIMIT, "tx_bytes": 0},
        {"rx_bytes": 0, "tx_bytes": guest.TX_LIMIT},
    ],
)
def test_packet_caps_are_not_relaxed(initial):
    with pytest.raises(ValueError):
        guest.packet_quota_arguments(initial)


def test_packet_quota_blueprint_preserves_boot_traffic_and_ipv6_drop():
    commands = guest.packet_quota_arguments({"rx_bytes": 100, "tx_bytes": 200})
    quota = [c for c in commands if "--quota" in c]
    assert [int(c[c.index("--quota") + 1]) for c in quota] == [
        guest.RX_LIMIT - 100,
        guest.TX_LIMIT - 200,
    ]
    assert not any(c[0] == "ip6tables" and "RETURN" in c for c in commands)


def test_model_readiness_keeps_preparation_deadline(tmp_path):
    with pytest.raises(TimeoutError):
        safety.run_child(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            output=tmp_path / "model.log",
            deadline_unix_s=time.time() + 10,
            preparation_deadline=time.time() + 0.15,
            ready_path=tmp_path / "ready.json",
        )


def test_missing_source_closure_cannot_start_guest(tmp_path):
    now = time.time()
    value = {
        "approved_execution_reference": "CPU_FIXTURE_ONLY",
        "submission_unix_s": now,
        "preparation_deadline_unix_s": now + 330,
        "comparison_deadline_unix_s": now + 375,
        "source_sha256": {},
    }
    with pytest.raises(ValueError, match="source closure"):
        guest.run_guest(tmp_path, value, execute=True)
    assert not list(tmp_path.iterdir())


def test_successful_exit_without_readiness_is_not_success(tmp_path):
    with pytest.raises(RuntimeError, match="verified model readiness"):
        safety.run_child(
            [sys.executable, "-c", "pass"],
            output=tmp_path / "model.log",
            deadline_unix_s=time.time() + 10,
            preparation_deadline=time.time() + 5,
            ready_path=tmp_path / "ready.json",
        )


def test_wall_clock_jump_cannot_extend_ready_comparison(tmp_path, monkeypatch):
    from types import SimpleNamespace

    clock = {"wall": 100.0, "mono": 0.0}
    monkeypatch.setattr(
        safety,
        "time",
        SimpleNamespace(
            time=lambda: clock["wall"],
            monotonic=lambda: clock["mono"],
            sleep=lambda seconds: clock.update(mono=clock["mono"] + 10),
        ),
    )

    class Child:
        pid, returncode = 123, None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = 0

    monkeypatch.setattr(safety.subprocess, "Popen", lambda *args, **kw: Child())
    monkeypatch.setattr(safety.os, "killpg", lambda *args: None)
    safety.write_json(tmp_path / "ready.json", {"ready_unix_s": 100.0})

    def health():
        clock["wall"] = -900
        return True

    with pytest.raises(TimeoutError):
        safety.run_child(
            ["CPU_FIXTURE"],
            output=tmp_path / "model.log",
            deadline_unix_s=1000,
            preparation_deadline=110,
            ready_path=tmp_path / "ready.json",
            health=health,
        )
    assert clock["mono"] == 50


@pytest.mark.parametrize("maximum", [600, 900])
def test_guest_failure_retains_failed_status_and_duplicate_guard(tmp_path, monkeypatch, maximum):
    import hashlib

    begin = time.time()
    prep, compare = (420, 465) if maximum == 900 else (330, 375)
    for name in guest.REQUIRED_SOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CPU fixture source; never executed\n")
    value = {
        "approved_execution_reference": "CPU_FIXTURE_ONLY",
        "submission_unix_s": begin,
        "preparation_deadline_unix_s": begin + prep,
        "comparison_deadline_unix_s": begin + compare,
        "maximum_elapsed_s": maximum,
        "source_sha256": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in guest.REQUIRED_SOURCES
        },
        "prepare_argv": ["bash", str(tmp_path / "bootstrap-vla.sh")],
        "infer_argv": [
            str(tmp_path / "vla-venv/bin/python"),
            str(tmp_path / "sources/yokohama_offline_vla_remote.py"),
            "--base",
            str(tmp_path / "openvla-7b"),
            "--adapter",
            str(tmp_path / "aerovla/aero_vla"),
            "--inputs",
            str(tmp_path / "inputs"),
            "--output",
            str(tmp_path / "evidence"),
            "--preparation-deadline",
            str(begin + prep),
            "--comparison-deadline",
            str(begin + compare),
        ],
    }
    monkeypatch.setattr(guest, "counters", lambda: {"rx_bytes": 100, "tx_bytes": 20})
    quota_calls = []
    monkeypatch.setattr(guest.subprocess, "run", lambda argv, **kw: quota_calls.append(argv))

    def supervisor(*args, events, **kwargs):
        events.failed(RuntimeError("CPU fixture model startup failed"))
        return False

    monkeypatch.setattr(guest, "supervise_guest", supervisor)
    monkeypatch.setattr(guest, "print", broken_display, raising=False)
    assert guest.run_guest(tmp_path, value, execute=True) is False
    evidence = tmp_path / "evidence"
    assert json.loads((evidence / "status.json").read_text())["stage"] == "failed"
    assert (evidence / "network-at-guest-exit.json").exists()
    with tarfile.open(tmp_path / "remote-evidence.tar.gz") as archive:
        assert "abort.json" in archive.getnames()
    calls_before = len(quota_calls)
    with pytest.raises(FileExistsError):
        guest.run_guest(tmp_path, value, execute=True)
    assert len(quota_calls) == calls_before
    quotas = [int(c[c.index("--quota") + 1]) for c in quota_calls if "--quota" in c]
    assert quotas[-1] == (448 * 1024**2 if maximum == 900 else guest.TX_LIMIT) - 20


def test_guest_hashing_works_without_python_311_file_digest(tmp_path, monkeypatch):
    monkeypatch.delattr(safety.hashlib, "file_digest", raising=False)
    path = tmp_path / "payload.py"
    path.write_bytes(b"CPU Python 3.10 compatibility fixture")
    assert safety.sha256_file(path) == safety.hashlib.sha256(path.read_bytes()).hexdigest()
    events = safety.DurableEvents(tmp_path / "evidence")
    events.emit("CPU_fixture")
    assert len(events.archive(tmp_path / "evidence.tar.gz")) == 64


def _independent_watchdog_fixture(output):
    def rest(vm, request):
        safety.write_json(Path(output) / "rest-receipt.json", request)

    controller = control(
        Path(output),
        observation=lambda: (_ for _ in ()).throw(RuntimeError("No primary reader")),
        rest=rest,
    )
    controller.watchdog_tick(385)


def test_watchdog_separate_CPU_process_survives_primary_failure(tmp_path):
    worker = multiprocessing.get_context("spawn").Process(
        target=_independent_watchdog_fixture, args=(str(tmp_path),)
    )
    worker.start()
    try:
        primary = control(
            tmp_path / "primary",
            observation=lambda: (_ for _ in ()).throw(RuntimeError("Primary process failed")),
        )
        with pytest.raises(RuntimeError, match="Primary process failed"):
            primary.tick(385)
        worker.join(timeout=5)
        assert worker.exitcode == 0
        request = json.loads((tmp_path / "rest-receipt.json").read_text())
        assert request["method"] == "DELETE"
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
