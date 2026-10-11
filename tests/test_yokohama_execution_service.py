"""Vehicle-service admission and evidence checks; no Docker, GPU or API calls."""

import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_yokohama_vehicle_service import execute
from scripts import run_yokohama_vehicle_service as service_cli
from src.runtime import yokohama_execution_service as contract

ROOT = Path(__file__).resolve().parents[1]


def request(root=ROOT, models="fixture", service=None):
    proposal = dict(
        schema_version="yokohama_chat_proposal.v1", proposal_id="offline-contract-test",
        execution_target=contract.TARGET, route={"route_id": contract.ROUTE_ID},
        destination_id="yokohama_harbour_pad", physical_execution_invoked=False,
        city_models=models,
        pad_queue={"mission_judge": dict(may_only_add_wait=True, max_decisions=2,
                                         max_added_wait_s=30)},
        simulator_arguments=contract.arguments(models, service, "fixture"),
        input_sha256=contract.input_hashes(root, service),
    )
    return dict(proposal=proposal, approval=dict(
        approved_proposal_sha256=contract.proposal_digest(proposal),
        operator_approval_ref="offline-test-only", actor_session_id="test-session",
        approved_at="2026-10-02T00:00:00+00:00",
    ))


def encoded(value):
    return json.dumps(value).encode()


@pytest.mark.parametrize("mutation", ["hash", "goal", "arguments", "authority", "approval"])
def test_admission_rejects_modified_or_unsupported_requests(tmp_path, mutation):
    value = request()
    p = value["proposal"]
    if mutation == "hash":
        p["input_sha256"][contract.SOURCES[0]] = "0" * 64
    elif mutation == "goal":
        p["destination_id"] = "different-pad"
    elif mutation == "arguments":
        p["simulator_arguments"] += ["--timeout-seconds", "9999"]
    elif mutation == "authority":
        p["pad_queue"]["mission_judge"]["max_added_wait_s"] = 300
    if mutation != "approval":
        # Even an internally consistent hash cannot authorize expanded scope.
        value["approval"]["approved_proposal_sha256"] = contract.proposal_digest(p)
    else:
        value["approval"]["approved_proposal_sha256"] = "0" * 64
    path = tmp_path / "approved.json"
    path.write_bytes(encoded(value))
    with pytest.raises(ValueError):
        execute(path, tmp_path / "run", runner=lambda _: pytest.fail("must not start"))
    assert not (tmp_path / "vehicle-service").exists()


def test_native_config_change_is_rejected_before_execution(tmp_path):
    config = tmp_path / "native.json"
    config.write_text('{"fixture":"not a live service"}')
    value = request(models="native", service=config)
    assert contract.validate_request(encoded(value), ROOT) == value
    config.write_text('{"fixture":"changed"}')
    with pytest.raises(ValueError, match="inputs changed"):
        contract.validate_request(encoded(value), ROOT)


def test_native_bytes_are_snapshotted_even_if_original_changes_after_admission(tmp_path, monkeypatch):
    config = tmp_path / "native.json"
    approved_bytes = b'{"start_argv":["approved-start"],"vla_port":18117,"wam_port":18118}'
    config.write_bytes(approved_bytes)
    value = request(models="native", service=config)
    path = tmp_path / "approved.json"
    path.write_bytes(encoded(value))
    admit = service_cli.admit_request

    def mutate_after_validation(raw, root):
        admitted = admit(raw, root)
        config.write_text('{"start_argv":["UNAPPROVED"],"vla_port":19999}')
        return admitted

    monkeypatch.setattr(service_cli, "admit_request", mutate_after_validation)

    def inspect_runner(args):
        used = Path(args[args.index("--native-service-config") + 1])
        assert used != config
        assert used.read_bytes() == approved_bytes
        assert sha256(used.read_bytes()).hexdigest() == value["proposal"]["input_sha256"]["native_service_config"]
        assert json.loads(used.read_bytes())["start_argv"] == ["approved-start"]
        return 1  # No model or flight is invoked by this regression.

    assert execute(path, tmp_path / "run", runner=inspect_runner) == 1
    receipt = json.loads((tmp_path / "vehicle-service/status.json").read_text())
    assert receipt["native_service_config_sha256"] == sha256(approved_bytes).hexdigest()


def fixture_runner(args):
    """Emulates only runner evidence, never flight or model inference."""
    folder = Path(args[args.index("--output-dir") + 1])
    manifest = Path(args[args.index("--approval-manifest") + 1])
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({
        "operator_approval_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
    }))
    (folder / "result.json").write_text(json.dumps(dict(
        status="passed", cleanup=True, decision_backend="fixture",
    )))
    return 0


def test_snapshot_receipt_replay_and_result_tampering(tmp_path):
    value = request()
    path, folder = tmp_path / "approved.json", tmp_path / "run"
    raw = encoded(value)
    path.write_bytes(raw)
    assert execute(path, folder, runner=fixture_runner) == 0
    assert (tmp_path / "vehicle-service/approved.json").read_bytes() == raw
    assert contract.verify_receipt(folder, value["proposal"], value["approval"])["status"] == "passed"
    with pytest.raises(ValueError, match="fresh run"):
        execute(path, folder, runner=lambda _: pytest.fail("replayed"))
    (folder / "result.json").write_text('{"status":"passed"}')
    assert contract.verify_receipt(folder, value["proposal"], value["approval"])["status"] == "failed"


@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt])
def test_failed_or_interrupted_runner_never_reports_success_or_restarts(tmp_path, fault):
    path, folder = tmp_path / "approved.json", tmp_path / "run"
    path.write_bytes(encoded(request()))

    def failed(_):
        raise fault()

    with pytest.raises(fault):
        execute(path, folder, runner=failed)
    receipt = json.loads((tmp_path / "vehicle-service/status.json").read_text())
    assert receipt["state"] == "finished" and receipt["exit_code"] != 0
    assert contract.verify_receipt(folder, request()["proposal"], request()["approval"])["status"] == "failed"
    with pytest.raises(FileExistsError):
        execute(path, folder, runner=lambda _: pytest.fail("replayed after failure"))


def test_native_receipt_requires_real_invocation_flags(tmp_path):
    value = request()
    path, folder = tmp_path / "approved.json", tmp_path / "run"
    path.write_bytes(encoded(value))
    execute(path, folder, runner=fixture_runner)
    native = copy.deepcopy(value["proposal"])
    native["city_models"] = "native"
    assert contract.verify_receipt(folder, native, value["approval"])["status"] == "failed"


def test_real_cli_boundary_with_isolated_runner_fixture(tmp_path):
    # Exercise the production CLI and contract in a separate process. Only the
    # flight runner is a fixture; it cannot import or launch the real simulator.
    repo = tmp_path / "repo"
    for name in contract.SOURCES:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# isolated test source\n")
    for name in (contract.SOURCES[0], contract.SOURCES[1]):
        (repo / name).write_bytes((ROOT / name).read_bytes())
    import inspect
    runner_source = inspect.getsource(fixture_runner).replace("fixture_runner", "main", 1)
    (repo / "scripts/yokohama_sitl.py").write_text(
        "import json\nfrom pathlib import Path\nfrom hashlib import sha256\n" + runner_source
    )
    job = tmp_path / "job"
    job.mkdir()
    path = job / "approved.json"
    value = request(repo)
    path.write_bytes(encoded(value))
    args = [sys.executable, str(repo / contract.SOURCES[0]),
            "--approval-manifest", str(path)]
    env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
    validation = subprocess.run(args + ["--validate-only"], capture_output=True, text=True,
                                timeout=15, env=env, cwd=tmp_path)
    assert validation.returncode == 0, validation.stderr
    assert json.loads(validation.stdout)["status"] == "valid"
    assert not (job / "vehicle-service").exists()
    run_args = args + ["--approve-sitl", "--output-dir", str(job / "run")]
    run = subprocess.run(run_args, capture_output=True, text=True, timeout=15,
                         env=env, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    receipt = json.loads((job / "vehicle-service/status.json").read_text())
    assert receipt["pid"] != os.getpid()
    assert contract.verify_receipt(job / "run", value["proposal"], value["approval"])["status"] == "passed"
    replay = subprocess.run(run_args, capture_output=True, text=True, timeout=15,
                            env=env, cwd=tmp_path)
    assert replay.returncode != 0
