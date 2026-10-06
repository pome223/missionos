"""Exercise the actual opt-in CLI, serialization, and saved-result verifier."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_starship_dispenser_experiment.py"


def invoke(*args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *(str(x) for x in args)],
                          cwd=cwd, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    root = tmp_path_factory.mktemp("dispenser-cli")
    output = root / "recorded"
    result = invoke("--output-dir", output, "--approve-synthetic", cwd=root)
    assert result.returncode == 0, result.stderr + result.stdout
    return root, output, result


def test_cli_requires_opt_in_before_creating_output(tmp_path):
    output = tmp_path / "not-created"
    result = invoke("--output-dir", output, cwd=tmp_path)
    assert result.returncode == 2
    assert "--approve-synthetic" in result.stderr
    assert not output.exists()


def test_actual_cli_serializes_full_comparison_and_hashes(experiment):
    _, output, result = experiment
    summary = json.loads(result.stdout)
    assert summary["status"] == "verified"
    assert summary["world_count"] == 90
    assert summary["policy_run_count"] == 450
    manifest = json.loads((output / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == expected
    study = json.loads((output / "study.json").read_text())
    assert study["provenance"]["physical_execution"] is False
    assert study["provenance"]["llm_invoked"] is False
    assert study["provenance"]["experiment_wall_time_s"] > 0
    report = (output / "report.html").read_text()
    assert 'id="world"' in report and 'id="clock"' in report
    assert "Flight 14の実機再現" in report
    assert "__DATA__" not in report


def test_saved_cli_verification_is_read_only_and_refuses_tampered_receipt(experiment):
    root, output, _ = experiment
    source = output / "study.json"
    original = source.read_bytes()
    result = invoke("--verify", source, cwd=root)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["verified"] is True
    assert source.read_bytes() == original
    tampered = json.loads(original)
    run = next(r for r in tampered["policy_runs"] if r["policy"] == "finite_model_optimal")
    run["terminal"]["release_count"] += 1
    bad = root / "tampered.json"
    bad.write_text(json.dumps(tampered))
    rejected = invoke("--verify", bad, cwd=root)
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["verified"] is False


def test_cli_preserves_existing_artifacts(experiment):
    root, output, _ = experiment
    original = (output / "manifest.json").read_bytes()
    result = invoke("--output-dir", output, "--approve-synthetic", cwd=root)
    assert result.returncode == 2
    assert (output / "manifest.json").read_bytes() == original


def test_report_does_not_turn_record_strings_into_script():
    from src.runtime.starship_dispenser_report import build_report

    report = build_report({"note": "</script><script>alert(1)</script>"}, {"verified": False})
    assert "</script><script>alert(1)" not in report
    encoded = report.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(encoded)["study"]["note"] == "</script><script>alert(1)</script>"
