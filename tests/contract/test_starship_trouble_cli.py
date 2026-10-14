"""CLI admission and failure-evidence boundary; real flights are runtime smokes."""
import importlib.util
import json

import pytest


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("trouble_cli", "scripts/run_starship_trouble.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_requires_explicit_opt_in_before_output(cli, tmp_path):
    output = tmp_path / "run"
    with pytest.raises(SystemExit):
        cli.main(["--seed", "14", "--output-dir", str(output)])
    assert not output.exists()


def test_cli_keeps_prior_evidence(cli, tmp_path):
    sentinel = tmp_path / "prior.json"
    sentinel.write_text("previous failed trial")
    with pytest.raises(SystemExit):
        cli.main(["--approve-simulation", "--seed", "14", "--output-dir", str(tmp_path)])
    assert sentinel.read_text() == "previous failed trial"


def test_execution_error_keeps_frozen_manifest_and_partial_clock(cli, monkeypatch, tmp_path):
    output = tmp_path / "run"
    trace = {"tick_order": [{"body_id":"ship", "time_s":0.}], "completed": False}
    def failed(*args, **kwargs):
        assert (output / "manifest.json").is_file()
        error = ValueError("injected_test_error")
        error.concurrent_execution = trace
        raise error
    monkeypatch.setattr(cli, "simulate_concurrent", failed)
    with pytest.raises(ValueError, match="injected_test_error"):
        cli.main(["--approve-simulation", "--seed", "14", "--output-dir", str(output)])
    failure = json.loads((output / "failure.json").read_text())
    assert failure["concurrent_execution"] == trace
    assert failure["automatic_retry"] is False
    assert (output / "inputs.json").is_file()
    assert not (output / "summary.json").exists()


def test_cli_rejects_manifest_and_new_distribution(cli, tmp_path):
    output = tmp_path / "run"
    with pytest.raises(SystemExit):
        cli.main(["--approve-simulation", "--manifest", "does-not-exist.json", "--family", "payload_jam", "--output-dir", str(output)])
    assert not output.exists()
