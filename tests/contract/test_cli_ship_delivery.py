"""Exercise installed CLI boundaries and fixture-only claim separation."""

import json

import pytest
from click.testing import CliRunner

from missionos_cli.cli import missionos
from src.runtime.ship_delivery import STAGES


def test_plan_serializes_validated_scenario_and_exact_contract(tmp_path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text('{"offshore_distance_m": 900}', encoding="utf-8")
    output = tmp_path / "reports" / "plan.json"
    result = CliRunner().invoke(
        missionos,
        ["ship-delivery", "plan", "--scenario", str(scenario), "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["scenario"]["offshore_distance_m"] == 900
    assert len(report["parent_contract_sha256"]) == 64
    assert [stage["stage_ref"] for stage in report["parent_contract"]["stages"]] == list(STAGES)
    assert report["execution_mode"] == "fixture_only"
    assert report["operator_approved"] is False


def test_run_requires_fixture_approval_before_execution(tmp_path) -> None:
    output = tmp_path / "blocked.json"
    result = CliRunner().invoke(missionos, ["ship-delivery", "run", "--output", str(output)])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["stage_receipts"] == []
    assert report["trajectory"] == []
    assert report["fixture_mission_completed"] is False


def test_approved_run_reports_independent_fixture_outcomes() -> None:
    result = CliRunner().invoke(missionos, ["ship-delivery", "run", "--approve-fixture"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "completed"
    assert report["fixture_mission_completed"] is True
    assert report["delivery_verified"] is True
    assert report["recovery_verified"] is True
    assert report["identity_continuity_verified"] is True
    for field in (
        "physical_execution_invoked",
        "px4_runtime_invoked",
        "vla_invoked",
        "wam_invoked",
    ):
        assert report[field] is False
    assert report["coordinator"]["mission_completion_claimed"] is False


@pytest.mark.parametrize("material", ["{broken", "[]", '{"wind_mps": NaN}', '{"unknown": 1}'])
def test_invalid_scenario_is_a_cli_error(tmp_path, material: str) -> None:
    scenario = tmp_path / "invalid.json"
    scenario.write_text(material, encoding="utf-8")
    result = CliRunner().invoke(missionos, ["ship-delivery", "plan", "--scenario", str(scenario)])
    assert result.exit_code == 1
    assert "Invalid ship delivery scenario:" in result.output


def test_px4_export_succeeds_without_promoting_execution_readiness(tmp_path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        '{"offshore_distance_m": 900, "urban_distance_m": 300, "cruise_altitude_m": 40}',
        encoding="utf-8",
    )
    output = tmp_path / "px4-plan.json"
    result = CliRunner().invoke(
        missionos,
        ["ship-delivery", "px4-plan", "--scenario", str(scenario), "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["offshore_distance_m"] == 900
    assert report["urban_distance_m"] == 300
    assert report["cruise_altitude_m"] == 40
    assert report["status"] == "blocked_pending_runtime_adapter"
    assert report["mission_items"]
    assert report["live_execution_supported"] is False
    assert report["runtime_invoked"] is False
    assert report["payload_release_observed"] is False
    assert report["delivery_completion_claimed"] is False
    assert "payload_release_observation_not_bound_to_return_departure" in report["blocked_reasons"]


def test_px4_compiler_rejection_is_a_cli_error(tmp_path) -> None:
    scenario = tmp_path / "low-altitude.json"
    scenario.write_text('{"cruise_altitude_m": 5}', encoding="utf-8")
    result = CliRunner().invoke(
        missionos, ["ship-delivery", "px4-plan", "--scenario", str(scenario)]
    )
    assert result.exit_code == 1
    assert "Cannot export ship delivery PX4 plan:" in result.output


def test_sitl_cli_requires_explicit_approval_before_starting_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.runtime.ship_delivery_sitl._run", lambda *a, **k: pytest.fail("Docker invoked")
    )
    result = CliRunner().invoke(
        missionos, ["ship-delivery", "run-sitl", "--output-dir", str(tmp_path / "run")]
    )
    assert result.exit_code == 1
    assert "--approve-sitl is required" in result.output
    assert not (tmp_path / "run").exists()
