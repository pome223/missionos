from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_route.runtime_lifecycle import (
    PX4RouteInitialRealismRuntime,
    PX4RouteRuntimeLifecycle,
    collect_initial_realism,
    px4_route_runtime_session,
)


def _runtime(
    tmp_path: Path,
    events: list[str],
    *,
    fail_at: str | None = None,
    stop_fails: bool = False,
) -> PX4RouteRuntimeLifecycle:
    run_dir = tmp_path / "run"

    def create() -> Path:
        events.append("create")
        run_dir.mkdir()
        return run_dir

    def reset(path: Path) -> Path:
        events.append("reset")
        if fail_at == "reset":
            raise RuntimeError("reset failed")
        trace = path / "pose_samples.jsonl"
        trace.write_text("")
        return trace

    def start(path: Path) -> Path:
        events.append("start")
        assert path == run_dir
        if fail_at == "start":
            raise RuntimeError("start failed")
        model_root = path / "models"
        model_root.mkdir()
        return model_root

    def wait() -> None:
        events.append("wait")
        if fail_at == "wait":
            raise RuntimeError("wait failed")

    def initialize(model_root: Path | None) -> dict[str, Any]:
        events.append("initialize")
        assert model_root == run_dir / "models"
        if fail_at == "initialize":
            raise RuntimeError("initialize failed")
        return {"wind": {"materialized": True}}

    def stop() -> None:
        events.append("stop")
        if stop_fails:
            raise RuntimeError("stop failed")

    def mark(path: Path) -> bool:
        events.append("mark")
        assert path == run_dir
        return True

    return PX4RouteRuntimeLifecycle(
        create_run_directory=create,
        reset_runtime_state=reset,
        start_container=start,
        wait_for_home=wait,
        initialize_realism=initialize,
        stop_container=stop,
        mark_cleanup_observed=mark,
    )


def test_session_initializes_readback_and_tears_down_after_body(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    runtime = _runtime(tmp_path, events)

    with px4_route_runtime_session(runtime) as session:
        events.append("body")
        assert session.run_dir == tmp_path / "run"
        assert session.pose_trace_path == tmp_path / "run" / "pose_samples.jsonl"
        assert session.payload_model_root == tmp_path / "run" / "models"
        assert session.initial_realism == {"wind": {"materialized": True}}

    assert events == [
        "create",
        "reset",
        "start",
        "wait",
        "initialize",
        "body",
        "stop",
        "mark",
    ]


def test_initial_realism_collects_readbacks_without_authority_fields(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    model_root = tmp_path / "models"

    def source(name: str) -> dict[str, Any]:
        events.append(name)
        return {"source": name, "observed": True}

    initial = collect_initial_realism(
        PX4RouteInitialRealismRuntime(
            terrain_world_readback=lambda root: (
                source("terrain") if root == model_root else {}
            ),
            apply_wind_realism=lambda root: source("wind") if root == model_root else {},
            thermal_weather_realism=lambda: source("thermal"),
            vehicle_realism=lambda root: source("vehicle") if root == model_root else {},
            battery_realism=lambda: source("battery"),
            sensor_realism=lambda: source("sensor"),
            world_realism=lambda root: source("world") if root == model_root else {},
            visibility_realism=lambda root: (
                source("visibility") if root == model_root else {}
            ),
            operational_realism=lambda root: (
                source("operational") if root == model_root else {}
            ),
            mavlink_link_realism=lambda: source("mavlink"),
        ),
        payload_model_root=model_root,
    )

    assert events == [
        "terrain",
        "wind",
        "thermal",
        "vehicle",
        "battery",
        "sensor",
        "world",
        "visibility",
        "operational",
        "mavlink",
    ]
    projected = initial.as_dict()
    assert set(projected) == {
        "terrain_world",
        "wind",
        "thermal_weather",
        "vehicle",
        "battery",
        "sensor",
        "world",
        "visibility",
        "operational",
        "mavlink_link",
    }
    assert not {
        "operator_approval_performed",
        "dispatch_authority_created",
        "completion_claimed",
    }.intersection(key for summary in projected.values() for key in summary)


@pytest.mark.parametrize("fail_at", ["reset", "start", "wait", "initialize"])
def test_initialization_failure_still_attempts_stop_and_cleanup_mark(
    tmp_path: Path,
    fail_at: str,
) -> None:
    events: list[str] = []
    runtime = _runtime(tmp_path, events, fail_at=fail_at)

    with pytest.raises(RuntimeError, match=f"{fail_at} failed"):
        with px4_route_runtime_session(runtime):
            raise AssertionError("body must not start")

    assert events[-2:] == ["stop", "mark"]
    assert "body" not in events


def test_body_failure_tears_down_without_claiming_success(tmp_path: Path) -> None:
    events: list[str] = []
    runtime = _runtime(tmp_path, events)

    with pytest.raises(RuntimeError, match="route failed"):
        with px4_route_runtime_session(runtime):
            events.append("body")
            raise RuntimeError("route failed")

    assert events[-3:] == ["body", "stop", "mark"]


def test_cleanup_mark_runs_even_if_container_stop_reports_failure(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    runtime = _runtime(tmp_path, events, stop_fails=True)

    with pytest.raises(RuntimeError, match="stop failed"):
        with px4_route_runtime_session(runtime):
            events.append("body")

    assert events[-3:] == ["body", "stop", "mark"]
