from missionos_cli import cli as missionos_cli
from missionos_gateway.server import _fixture_task


def test_fixture_job_status_does_not_claim_actual_sitl_flight() -> None:
    lines = missionos_cli._job_operator_summary(_fixture_task("task_fixture"))

    rendered = "\n".join(lines)

    assert "Fixture Complete: no live SITL flight was invoked" in rendered
    assert "Route: [----------------------------] 0.0%" in rendered
    assert "Distance: 0 m" in rendered
    assert "actual_sitl_flight=False" in rendered
    assert "physical_execution=False" in rendered
