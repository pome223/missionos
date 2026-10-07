"""Public-only, one-way fault observation boundary with unchanged execution."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import run_starship_jev_shadow_worker as worker
from src.runtime.starship_dispenser_experiment import (
    ActionProposal,
    FiniteModelSolver,
    digest,
    run_policy_tape,
)
from src.runtime.starship_dispenser_verifier import verify_dispenser_experiment


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_starship_jev_shadow_worker.py"


def _environment() -> dict[str, str]:
    return {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if name in os.environ
    }


def _invoke(output: Path, *, approved: bool = True, env: dict | None = None):
    args = [sys.executable, str(SCRIPT), "--output-dir", str(output)]
    if approved:
        args.append("--approve-synthetic")
    return subprocess.run(
        args,
        cwd=output.parent,
        env=_environment() if env is None else env,
        input='{"action":"abort","approval":true,"seed":999999}\n',
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    output = tmp_path_factory.mktemp("jev-shadow-worker") / "recorded"
    result = _invoke(output)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return output, result


def test_cli_emits_only_public_fault_frames_and_complete(recorded):
    output, result = recorded
    study = json.loads((output / "study.json").read_text())
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    expected_frames = []
    for run in study["policy_runs"]:
        if run["split"] != "eval" or run["policy"] != "history_rule":
            continue
        fault = next(
            (
                step
                for step in run["steps"]
                if any(item["success"] is False for item in step["public_history"]["retry_results"])
            ),
            None,
        )
        if fault is None:
            continue
        expected_frames.append(
            {
                "kind": "fault_observation",
                "case_ref": sha256(run["world_id"].encode()).hexdigest(),
                "step_index": fault["index"],
                "public_history": fault["public_history"],
                "public_budget": fault["public_budget"],
            }
        )
    assert frames[:-1] == expected_frames
    assert len(expected_frames) == 22
    assert len({frame["case_ref"] for frame in expected_frames}) == 22
    complete = frames[-1]
    assert set(complete) == {
        "kind",
        "worker_pid",
        "provider_credentials_present",
        "emitted_count",
        "evaluation_count",
    }
    assert complete["kind"] == "complete"
    assert type(complete["worker_pid"]) is int and complete["worker_pid"] > 0
    assert complete["provider_credentials_present"] is False
    assert complete["emitted_count"] == 22
    assert complete["evaluation_count"] == 60
    for line in result.stdout.splitlines(keepends=True):
        assert len(line.encode()) <= worker.MAX_FRAME_BYTES
    forbidden = {
        "world_id",
        "seed",
        "group",
        "recovery_time_s",
        "sensor_tape",
        "proposal",
        "terminal",
        "tape_sha256",
        "incumbent_action",
    }

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    for frame in frames[:-1]:
        inspect(frame)
        tick = frame["public_budget"]["time_ticks"]
        assert all(row["time_ticks"] <= tick for row in frame["public_history"]["observations"])
        assert all(row["end_ticks"] <= tick for row in frame["public_history"]["retry_results"])


def test_actual_cli_keeps_all_execution_and_wait_times_unchanged(recorded):
    output, _ = recorded
    study = json.loads((output / "study.json").read_text())
    saved_verdict = json.loads((output / "verification.json").read_text())
    assert saved_verdict["verified"] and saved_verdict["runs_checked"] == 870
    assert verify_dispenser_experiment(study) == saved_verdict
    runs = json.loads((output / "execution-runs.json").read_text())
    expected = [
        run
        for run in study["policy_runs"]
        if run["split"] == "eval" and run["policy"] == "history_rule"
    ]
    assert len(runs) == 60 and runs == expected
    waits = [step for run in runs for step in run["steps"] if step["proposal"]["action"] == "wait"]
    assert waits
    for step in waits:
        receipt = step["receipt"]
        assert receipt["end_s"] - receipt["start_s"] == 2
        assert step["observation"]["time_s"] == receipt["end_s"]
    assert study["claim_boundary"]["llm_invoked"] is False


def test_callback_cannot_mutate_any_of_60_runs_or_replace_their_actions(recorded):
    output, _ = recorded
    study = json.loads((output / "study.json").read_text())
    before = digest(study)
    config = study["config"]
    worlds = {world["world_id"]: world for world in study["worlds"]}
    solver = FiniteModelSolver(config)
    seen = []
    for expected in study["policy_runs"]:
        if expected["split"] != "eval" or expected["policy"] != "history_rule":
            continue

        def hostile(history, budget, step_index):
            step = expected["steps"][step_index]
            assert history.observations[-1].time_ticks == step["public_budget"]["time_ticks"]
            assert budget.time_ticks == step["public_budget"]["time_ticks"]
            seen.append(step_index)
            # Deliberately bypass frozen dataclass assignment, including nested
            # observations. Only copies may be corrupted, never executor state.
            object.__setattr__(history.observations[0], "healthy", "private-marker")
            object.__setattr__(history, "retry_results", ())
            object.__setattr__(budget, "time_ticks", 12)
            object.__setattr__(budget, "released", 3)
            return ActionProposal("abort", "observer has no action authority")

        actual = run_policy_tape(
            config,
            worlds[expected["world_id"]],
            "history_rule",
            expected["parameters"],
            solver,
            approved=True,
            observation_callback=hostile,
        )
        assert actual == expected
    assert len(seen) == 542
    assert digest(study) == before


def test_observer_failure_is_incomplete_not_a_silent_success(recorded):
    output, _ = recorded
    study = json.loads((output / "study.json").read_text())

    def fail(*_):
        raise RuntimeError("observer unavailable")

    with pytest.raises(RuntimeError, match="observer unavailable"):
        run_policy_tape(
            study["config"],
            study["worlds"][0],
            "abort",
            approved=True,
            observation_callback=fail,
        )
    with pytest.raises(ValueError, match="callback must be callable"):
        run_policy_tape(
            study["config"],
            study["worlds"][0],
            "abort",
            approved=True,
            observation_callback=True,
        )


def test_cli_requires_explicit_synthetic_approval(tmp_path):
    output = tmp_path / "not-created"
    result = _invoke(output, approved=False)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "shadow_worker_synthetic_approval_required\n"
    assert not output.exists()


def test_cli_argument_errors_do_not_echo_caller_text(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--private-token=do-not-echo"],
        cwd=tmp_path,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "shadow_worker_invalid_arguments\n"


@pytest.mark.parametrize(
    "name",
    [
        "DEEPSEEK_API_KEY",
        "TYPESAFE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "AWS_CONFIG_FILE",
    ],
)
def test_worker_rejects_provider_and_cloud_credentials_without_echo(tmp_path, name):
    output = tmp_path / "not-created"
    env = _environment()
    env[name] = "private-marker-do-not-print"
    result = _invoke(output, env=env)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "shadow_worker_failed\n"
    assert not output.exists()


def test_worker_preserves_existing_output(recorded):
    output, _ = recorded
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    result = _invoke(output)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "shadow_worker_failed\n"
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_frame_bound_fails_without_emitting_partial_json(capsys):
    with pytest.raises(ValueError, match="frame_too_large"):
        worker._emit({"kind": "fault_observation", "data": "x" * worker.MAX_FRAME_BYTES})
    assert capsys.readouterr().out == ""


def test_worker_exception_is_sanitized_and_never_emits_complete(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "--output-dir", str(tmp_path / "new"), "--approve-synthetic"]
    )
    monkeypatch.setattr(worker, "provider_credentials_present", lambda: False)

    def fail(*args, **kwargs):
        raise RuntimeError("private-secret-provider-body")

    monkeypatch.setattr(worker, "run_dispenser_experiment", fail)
    assert worker.main() == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "shadow_worker_failed\n"
