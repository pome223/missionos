"""Regressions for misleading success, stale input and damaged public evidence."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("aerial_public_report", ROOT / "verify_report.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def data():
    return tuple(json.loads((ROOT / n).read_text()) for n in ("summary.json", "trajectory.json"))


def test_curated_bundle():
    assert module.verify()["artifact_consistency"] == "passed"


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["scope"].update(goal_direction_selection_correct=True),
        lambda s: s["scope"].update(physical_hardware_execution=True),
        lambda s: s["scope"].update(live_turtlebot3_motion_demonstrated=True),
        lambda s: s["model"].update(risk_score=0.1),
        lambda s: s["observation"].update(original_image_age_at_dispatch_s=181),
        lambda s: s["flight"].update(measured_displacement_m=5.0),
        lambda s: s["flight"]["checkpoints"]["disarm"].update(armed=True),
        lambda s: s["candidates"][0].update(distance_to_goal_camera_m=10),
        lambda s: s["cleanup"].update(gpu_vm_removed=False),
        lambda s: s["budget"].update(billing_statement_verified=True),
        lambda s: s.update(session_id="private-source-identity"),
    ],
)
def test_misleading_or_inconsistent_change_rejected(change):
    summary, trajectory = deepcopy(data())
    change(summary)
    with pytest.raises(ValueError):
        module.verify_data(summary, trajectory)


def test_changed_file_hash_rejected(tmp_path):
    for name in module.ARTIFACTS | {"manifest.json"}:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "summary.json").write_text("{}\n")
    with pytest.raises(ValueError, match="artifact digest"):
        module.verify(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        lambda t: [
            p["position_ned_m"].__setitem__(0, -p["position_ned_m"][0]) for p in t["points"]
        ],
        lambda t: [p.update(position_ned_m=[100, 100, 100]) for p in t["points"]],
        lambda t: t["points"][-1].update(elapsed_simulation_s=47),
        lambda t: t["points"][-1].pop("checkpoint"),
        lambda t: t["points"][-1].update(phase="flying_candidate"),
    ],
)
def test_inconsistent_observed_trajectory_rejected(change):
    summary, trajectory = deepcopy(data())
    change(trajectory)
    with pytest.raises(ValueError):
        module.verify_data(summary, trajectory)


@pytest.mark.parametrize("manifest", [{}, {"summary.json": "0" * 64}])
def test_incomplete_manifest_rejected(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest artifact set"):
        module.verify(tmp_path)
