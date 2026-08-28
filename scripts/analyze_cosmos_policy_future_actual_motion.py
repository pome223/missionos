#!/usr/bin/env python3
"""Compare predicted and observed visual motion without granting success authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402


def _load_rgb(path: Path, *, flip_top_bottom: bool) -> Any:
    import numpy as np
    from PIL import Image

    image = Image.open(path).convert("RGB")
    if flip_top_bottom:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    image = image.resize((224, 224))
    return np.asarray(image, dtype=np.float32)


def _mean_absolute_pixel_difference(left: Any, right: Any) -> float:
    import numpy as np

    return float(np.mean(np.abs(left - right)))


def analyze(*, trial_root: Path, output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise ValueError("cosmos_future_actual_analysis_output_exists")
    report_path = trial_root / "repair" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual_by_step = {
        int(item["step_number"]): item for item in report["actual_observation_manifest"]
    }
    chunks = []
    for future in report["future_prediction_manifest"]:
        start = int(future["applied_action_start_index"])
        end = start + 16
        if start not in actual_by_step or end not in actual_by_step:
            raise RuntimeError("cosmos_future_actual_analysis_observation_pair_missing")
        camera_results = []
        for observation_key, prediction_key in (
            ("agentview_image", "future_image"),
            ("robot0_eye_in_hand_image", "future_wrist_image"),
        ):
            start_camera = next(
                item
                for item in actual_by_step[start]["cameras"]
                if item["observation_key"] == observation_key
            )
            end_camera = next(
                item
                for item in actual_by_step[end]["cameras"]
                if item["observation_key"] == observation_key
            )
            prediction = next(
                item for item in future["images"] if item["prediction_key"] == prediction_key
            )
            start_pixels = _load_rgb(
                trial_root / start_camera["artifact_relative_path"], flip_top_bottom=True
            )
            end_pixels = _load_rgb(
                trial_root / end_camera["artifact_relative_path"], flip_top_bottom=True
            )
            predicted_pixels = _load_rgb(
                trial_root / prediction["artifact_relative_path"], flip_top_bottom=False
            )
            camera_results.append(
                {
                    "observation_key": observation_key,
                    "actual_motion_mean_absolute_pixel_difference": (
                        _mean_absolute_pixel_difference(end_pixels, start_pixels)
                    ),
                    "predicted_motion_mean_absolute_pixel_difference": (
                        _mean_absolute_pixel_difference(predicted_pixels, start_pixels)
                    ),
                    "prediction_error_mean_absolute_pixel_difference": (
                        _mean_absolute_pixel_difference(predicted_pixels, end_pixels)
                    ),
                }
            )
        chunks.append(
            {
                "query_index": int(future["query_index"]),
                "applied_action_start_index": start,
                "applied_action_end_index_exclusive": end,
                "cameras": camera_results,
            }
        )

    agent_results = [
        camera
        for chunk in chunks
        for camera in chunk["cameras"]
        if camera["observation_key"] == "agentview_image"
    ]
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_future_actual_motion.v1",
        "status": "future_actual_visual_motion_compared",
        "source_repair_report_sha256": report["result_sha256"],
        "chunk_count": len(chunks),
        "agentview_mean_actual_motion_pixel_difference": sum(
            item["actual_motion_mean_absolute_pixel_difference"] for item in agent_results
        )
        / len(agent_results),
        "agentview_mean_predicted_motion_pixel_difference": sum(
            item["predicted_motion_mean_absolute_pixel_difference"] for item in agent_results
        )
        / len(agent_results),
        "agentview_mean_prediction_error_pixel_difference": sum(
            item["prediction_error_mean_absolute_pixel_difference"] for item in agent_results
        )
        / len(agent_results),
        "chunks": chunks,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "metric": "mean_absolute_rgb_pixel_difference_after_orientation_normalization",
            "future_predictions_may_establish_success": False,
            "object_motion_established_by_pixel_difference": False,
            "physical_execution_invoked": False,
        },
    }
    result = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(trial_root=args.trial_root.resolve(), output_path=args.output.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
