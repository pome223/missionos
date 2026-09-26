#!/usr/bin/env python3
"""Audit observation support behind saved ANWM previews; never select or dispatch.

Coverage is evidence availability, not traversability or forecast accuracy. A
world model may predict unobserved pixels; this audit exposes that extrapolation
instead of confusing a low goal-image MSE with observed free space.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.aerial_anwm_runtime import digest_file, validate_request  # noqa: E402
from scripts.select_urban_wam_route import validate_forecasts  # noqa: E402


def project_observations(arrays, target):
    """Pinhole projection and nearest-depth visibility, using observations only."""
    depth = arrays["context_depth"]
    _, height, width = depth.shape
    intrinsics = arrays["camera_intrinsics"]
    fx, fy = intrinsics[0, 0], intrinsics[1, 1]
    cx, cy = intrinsics[0, 2], intrinsics[1, 2]
    u, v = np.meshgrid(np.arange(width), np.arange(height))
    rays = np.stack(((u - cx) / fx, (v - cy) / fy, np.ones_like(u)), axis=-1)
    points, colors = [], []
    for d, rgb, pose in zip(depth, arrays["context_rgb"], arrays["context_camera_poses"]):
        valid = d > 0
        camera = rays[valid] * d[valid, None]
        transform = np.linalg.inv(target) @ pose
        points.append(camera @ transform[:3, :3].T + transform[:3, 3])
        colors.append(rgb[valid])
    points, colors = np.concatenate(points), np.concatenate(colors)
    front = points[:, 2] > 1e-6
    points, colors = points[front], colors[front]
    u = points[:, 0] * fx / points[:, 2] + cx
    v = points[:, 1] * fy / points[:, 2] + cy
    inside = (u >= 0) & (u < width) & (v >= 0) & (v < height)
    u, v = u[inside].astype(int), v[inside].astype(int)
    z, colors = points[inside, 2], colors[inside]
    order = np.argsort(z)
    _, first = np.unique((v * width + u)[order], return_index=True)
    nearest = order[first]
    image = np.zeros((height, width, 3), dtype=np.uint8)
    mask = np.zeros((height, width), dtype=bool)
    image[v[nearest], u[nearest]] = colors[nearest]
    mask[v[nearest], u[nearest]] = True
    return image, mask


def model_crop(image):
    """Pinned upstream's 4:3 center crop and PIL bilinear 224-square resize."""
    image = Image.fromarray(image)
    width, height = image.size
    crop_h, crop_w = (
        (height, int(height * 4 / 3)) if width > height else (int(width * 3 / 4), width)
    )
    left, top = round((width - crop_w) / 2), round((height - crop_h) / 2)
    return np.asarray(
        image.crop((left, top, left + crop_w, top + crop_h)).resize(
            (224, 224), Image.Resampling.BILINEAR
        )
    )


def audit(input_dir, result_path):
    request = json.loads((input_dir / "request.json").read_text())
    arrays, manifest = validate_request(request, input_dir)
    checked, outputs = validate_forecasts(input_dir, result_path)
    assert checked == manifest
    by_id = {row["candidate_id"]: row for row in outputs}
    rows = []
    for candidate in manifest["candidates"]:
        output = by_id[candidate["candidate_id"]]
        projection, mask = project_observations(arrays, np.asarray(candidate["target_camera_pose"]))
        cropped = model_crop(projection)
        # Only fully supported interpolation footprints count. True black
        # observations stay valid; RGB nonzero is not a validity mask.
        weights = model_crop(mask.astype(np.float32))
        supported = weights >= 1 - 1e-6
        predicted = np.asarray(
            Image.open(result_path.parent / output["predicted_image"]).convert("RGB")
        )
        recorded = np.asarray(
            Image.open(result_path.parent / output["projection_image"]).convert("RGB")
        )
        if predicted.shape != cropped.shape or recorded.shape != cropped.shape:
            raise ValueError("forecast images must match the model grid")
        difference = np.abs(recorded.astype(float) - cropped.astype(float))
        if difference.max() > 1:
            raise ValueError("saved projection differs from source RGB-D reconstruction")
        residual = (predicted.astype(float) - cropped.astype(float)) / 255
        rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "goal_mse": output["goal_mse"],
                "native_observed_fraction": float(mask.mean()),
                "model_grid_fully_observed_fraction": float(supported.mean()),
                "model_grid_mean_observation_weight": float(weights.mean()),
                "supported_region_forecast_mse": float(np.mean(residual[supported] ** 2))
                if supported.any()
                else None,
                "projection_reconstruction_max_error_8bit": float(difference.max()),
                "empty_projection": not bool(mask.any()),
                "forecast_image_sha256": output["predicted_image_sha256"],
            }
        )
    return {
        "schema_version": "urban_wam_candidate_support_audit.v1",
        "input_request_sha256": digest_file(input_dir / "request.json"),
        "forecast_result_sha256": digest_file(result_path),
        "observation_only_geometry": True,
        "scene_truth_used_for_metrics": False,
        "model_invoked_by_audit": False,
        "decision_or_dispatch_authority": False,
        "support_is_traversability": False,
        "time_aligned_future_validated": False,
        "candidates": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.input_dir, args.result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
