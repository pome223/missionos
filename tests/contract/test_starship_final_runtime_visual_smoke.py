"""Real supplemental pipes/visual fixture HTTP, without integration or models."""
import subprocess
import time

import pytest

from scripts import smoke_starship_final_runtime as smoke
from src.runtime import starship_final_native as native
from src.runtime import starship_final_protocol as rpc
from src.runtime import starship_final_shared as shared
from src.runtime import starship_final_outer as outer
from src.runtime import starship_physics as physics
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_final_fixture import visual_scripts
from src.runtime.starship_final_visual import FinalVisualHost
from src.intelligence.starship_visual_transport import fixture_provider


@pytest.fixture(autouse=True)
def forbid_plant_or_models(monkeypatch):
    from src.intelligence.starship_m2_broker import DeadlineTransport

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Visual smoke contracts cannot integrate, forecast, invoke models or spawn children")

    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"),
                        (physics, "step"), (DeadlineTransport, "_invoke"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def exercise(tmp_path, *, scripts=None):
    from contextlib import ExitStack
    stack = ExitStack()
    try:
        frame_root = tmp_path / "camera"
        frame_root.mkdir()
        source = shared.source_manifest((smoke.SCRIPT,))
        manifest = {"plan_sha256": "a"*64, "source_sha256": source}
        provider = stack.enter_context(fixture_provider(visual_scripts() if scripts is None else scripts))
        visual = FinalVisualHost(frame_root, run_id="final-runtime-smoke", plan_sha256="a"*64,
            source_sha256=source, mode="fixture", fixture_endpoint=provider["endpoint"])
        pairs = [outer.approval_pipe() for _ in range(2)]
        for pair in pairs:
            for endpoint in pair:
                stack.callback(endpoint.close)
        client = rpc.FinalObservationClient(pairs[0][1], pairs[1][0],
            plan_sha256="a"*64, source_sha256=shared.digest(source))
        host = rpc.FinalObservationHost(pairs[0][0], pairs[1][1],
            plan_sha256="a"*64, source_sha256=shared.digest(source),
            handler=lambda value, **_kw: visual.assess(value["observation"]))
        result = smoke.visual_observer_exchange(client, manifest, frame_root,
            deadline=time.monotonic()+15., service=host.tick)
        return result, client.record(), host.record(), visual.record(), provider["requests"]
    finally:
        stack.close()


def test_real_observer_followup_crosses_rpc_with_original_epoch(tmp_path):
    result, client, host, visual, http = exercise(tmp_path)
    assert smoke.COVERAGE_REVISION == 3
    assert result["passed"] and result["two_requests"] and result["later_followup_snapshot"]
    assert result["same_original_epoch"] and result["observer_inputs_unchanged"]
    assert result["retained_count"] == 13
    assert result["fixture_observation_origin"] == "algebraic_rigid_release_not_integrated_flight"
    assert result["physics_steps"] == result["provider_calls"] == 0
    assert result["execution_permission_granted"] is False
    assert len(client["completed"]) == len(host["sessions"]) == len(visual["attempts"]) == len(http) == 2
    requests = [row["frame"] for row in client["completed"]]
    first, second = (row["request"] for row in requests)
    assert first["time_s"] == second["time_s"] == 128.
    assert first["deadline_s"] == second["deadline_s"] == 163.
    assert first["observation"]["request"]["snapshot_time_s"] == 128.
    assert second["observation"]["request"]["snapshot_time_s"] == 130.5
    assert requests[0]["started_wall_s"] == requests[1]["started_wall_s"]
    assert requests[0]["wall_deadline_s"] == requests[1]["wall_deadline_s"]
    assert [row["frame"] for row in host["sessions"]] == requests
    assert [row["image_header"] for row in http] == [True, False]
    assert http[0]["image_bytes"] > 0
    observer = result["observer"]
    assert observer["native_judgments"] == 0
    assert observer["ai_visual_work_completed"] is False
    assert observer["inventory_assigned"] is False
    assert observer["plant_state_assigned"] is False
    assert [row["reply"]["result"]["action"] for row in observer["results"]] == [
        "request_numeric_confirmation", "continue"]
    assert all(row["reply"]["result"]["provider_receipt"]["local_http_attempted"] is True
        and row["reply"]["result"]["model_inference_invoked"] is False for row in observer["results"])


def test_old_snapshot_as_epoch_bug_is_rejected_on_second_observer_request(tmp_path, monkeypatch):
    def old_epoch(_observer, envelope):
        value = envelope["request"]
        return rpc.request(value["request_id"], "visual", value["snapshot_time_s"],
            value["deadline_s"], envelope)

    monkeypatch.setattr(native, "visual_rpc_request", old_epoch)
    with pytest.raises(ValueError, match="final_rpc_visual_original_epoch"):
        exercise(tmp_path)


def test_no_plant_context_blocks_a_direct_step_and_native_transport():
    from src.intelligence.starship_m2_broker import DeadlineTransport
    with smoke.no_plant_or_paid_provider():
        for call in (dyn.step, dyn._rk4, dyn._rhs, physics.step, DeadlineTransport._invoke):
            with pytest.raises(AssertionError, match="smoke_cannot_integrate"):
                call()
