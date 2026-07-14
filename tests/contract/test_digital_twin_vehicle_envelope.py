from tests.fixtures.digital_twin_environment import (
    build_fixture_backed_vehicle_environment,
)


def test_fixture_backed_vehicle_envelope_and_energy_budget_pass() -> None:
    result = build_fixture_backed_vehicle_environment()
    summary = result["summary"]

    assert summary["vehicle_flight_envelope_ref"]
    assert summary["vehicle_profile_ref"]
    assert summary["vehicle_envelope_status"] == "passed"
    assert summary["vehicle_envelope_blocked_reasons"] == []
    assert summary["mission_energy_budget_ref"]
    assert summary["mission_energy_budget_status"] == "passed"
    assert summary["mission_energy_required_wh"] > 0
    assert summary["mission_energy_remaining_wh"] >= 0
    assert summary["mission_energy_blocked_reasons"] == []
    assert summary["source_backed_target"] is True
    assert summary["source_backed_terrain"] is True
    assert summary["source_backed_weather"] is True

    for artifacts in (result, summary):
        assert artifacts.get("hardware_target_allowed", False) is False
        assert artifacts.get("physical_execution_invoked", False) is False
