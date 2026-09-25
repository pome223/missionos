#!/usr/bin/env python3
"""CPU-only usefulness screen of the paired calibration captures.

This is a retrospective, scene-specific audit. Scene truth and paired outcomes
are auditor inputs, never prediction inputs or permission to dispatch a flight.
"""

from __future__ import annotations

import argparse
from itertools import product
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aerial_anwm_runtime import digest_json  # noqa: E402
from scripts.compare_px4_anwm_outcomes import compare, require  # noqa: E402
from scripts.px4_aerial_flight_scene import BOXES  # noqa: E402


def green_pixel_count(rgb: np.ndarray) -> int:
    """Calibration marker diagnostic, not a general object detector/confidence."""
    values = np.asarray(rgb, dtype=np.float64) / 255
    red, green, blue = np.moveaxis(values, -1, 0)
    return int(np.count_nonzero((green > 2 * red) & (green > 2 * blue) & (green > 0.1)))


def excluded_frustum_planes(
    box: dict,
    optical_to_ned: np.ndarray,
    intrinsics: np.ndarray,
    width: int,
    height: int,
) -> list[str]:
    """Sufficient exclusion of a whole ENU box; empty does NOT prove visibility.

    Corners all outside one pinhole half-space exclude the convex box. Homogeneous
    coordinates avoid dividing by zero or reversing signs for behind-camera points.
    Occlusion, lighting and indirect rendering effects are outside this test.
    """
    center = np.asarray(box["center_xyz_m"], dtype=float)
    size = np.asarray(box["size_xyz_m"], dtype=float)
    enu = center + np.asarray(list(product((-0.5, 0.5), repeat=3))) * size
    ned = enu[:, [1, 0, 2]] * [1, 1, -1]
    camera = (np.linalg.inv(optical_to_ned) @ np.c_[ned, np.ones(8)].T).T[:, :3]
    pixels_h = camera @ intrinsics.T
    u, v, z = pixels_h.T
    planes = {
        "behind": z,
        "left": u,
        "right": width * z - u,
        "top": v,
        "bottom": height * z - v,
    }
    return [name for name, values in planes.items() if np.all(values < -1e-8)]


def audit(left_root: Path, right_root: Path) -> dict:
    comparison = compare(left_root, right_root)
    costs = comparison["actual_goal_rgb_mse_by_arm"]
    best = min(costs.values())
    target = next(box for box in BOXES if box["name"] == "aerial_left_marker")
    records = {}
    for candidate_id, root in (("left_5m", left_root), ("right_5m", right_root)):
        scene = json.loads((root / "session/scene.json").read_text())
        require(
            scene["boxes"] == BOXES,
            "audit only supports the published calibration scene",
        )
        # compare() has already checked the asset digest, goal digest and both
        # forecast image digests against this arm's real inference receipt.
        with np.load(root / "input-27/assets.npz", allow_pickle=False) as source:
            history = source["context_rgb"]
            poses = source["context_camera_poses"]
            intrinsics = source["camera_intrinsics"]
        require(
            history.shape == (16, 360, 640, 3) and poses.shape == (16, 4, 4),
            "unexpected calibration history shape",
        )
        require(
            np.isfinite(poses).all() and np.isfinite(intrinsics).all(),
            "non-finite camera geometry",
        )
        goal = np.asarray(Image.open(root / "session/goal/goal.png").convert("RGB"))
        require(
            green_pixel_count(goal) > 0,
            "declared goal lacks the calibration marker color",
        )
        result = json.loads((root / "model-output-27/result.json").read_text())
        counts = {}
        for candidate in result["candidates"]:
            counts[candidate["candidate_id"]] = {
                kind: green_pixel_count(
                    np.asarray(
                        Image.open(root / "model-output-27" / candidate[field]).convert(
                            "RGB"
                        )
                    )
                )
                for kind, field in (
                    ("anwm", "predicted_image"),
                    ("projection", "projection_image"),
                )
            }
        arm = comparison["arms"][candidate_id]
        choices = {
            "anwm_mse": arm["model_selected"],
            "projection_mse": arm["projection_selected"],
            "available_goal_pose": arm["geometry_selected"],
        }
        records[candidate_id] = {
            "history_green_pixels_per_frame": [
                green_pixel_count(frame) for frame in history
            ],
            "goal_green_pixels": green_pixel_count(goal),
            "target_excluded_planes_per_frame": [
                excluded_frustum_planes(target, pose, intrinsics, 640, 360)
                for pose in poses
            ],
            "forecast_green_pixels": counts,
            "selected_candidates": choices,
            "paired_proxy_regret": {
                name: costs[choice] - best for name, choice in choices.items()
            },
            "two_candidate_forecast_wall_seconds": result[
                "runtime_invocation_evidence"
            ]["forecast_seconds"],
        }
    return {
        "schema_version": "missionos_px4_anwm_value_audit.v1",
        "scope": "retrospective_single_static_calibration_scene",
        "comparison_sha256": digest_json(comparison),
        "actual_goal_rgb_mse_by_arm": costs,
        "paired_proxy_best_mse": best,
        "arms": records,
        "limitations": [
            "near_matched_starts_not_a_same_state_counterfactual_outcome_matrix",
            "goal_mse_is_not_mission_success_or_calibrated_risk",
            "marker_color_rule_is_scene_specific_not_a_semantic_detector",
            "frustum_exclusion_only_covers_direct_pinhole_visibility",
            "scene_truth_used_only_for_audit_not_for_candidate_selection",
            "wall_inference_seconds_and_simulation_motion_seconds_are_distinct",
        ],
        "new_gpu_inference": False,
        "flight_dispatched": False,
        "production_selection_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-root", type=Path, required=True)
    parser.add_argument("--right-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.left_root, args.right_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "audit_complete": True,
                "new_gpu_inference": False,
                "production_selection_changed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
