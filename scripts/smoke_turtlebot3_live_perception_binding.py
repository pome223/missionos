#!/usr/bin/env python3
"""Opt-in live VLM smoke for one TurtleBot3 camera/LiDAR capture receipt."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from src.intelligence.turtlebot3_perception_sidecar import (
    run_turtlebot3_perception_sidecar,
)
from src.runtime.perception_claim import (
    build_perception_claim_from_camera_observation,
)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_smoke(
    *,
    capture_receipt_path: Path,
    image_path: Path,
    decision_epoch_ref: str,
) -> tuple[dict[str, Any], bool]:
    receipt_bytes = capture_receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    image_sha256 = _sha256_file(image_path)
    receipt_image_sha256 = str(receipt.get("camera_frame_sha256") or "")
    image_hash_matches_receipt = bool(
        receipt_image_sha256 and receipt_image_sha256 == image_sha256
    )

    sidecar = run_turtlebot3_perception_sidecar(image_path=image_path)
    observation = dict(sidecar.get("camera_observation") or {})
    invocation = dict(sidecar.get("llm_invocation_evidence") or {})
    runtime_context = {
        "decision_epoch_ref": decision_epoch_ref,
        "capture": {
            "camera_frame_sha256": receipt_image_sha256,
            "camera_lidar_observation": dict(
                receipt.get("camera_lidar_observation") or {}
            ),
        },
        "llm_invocation_evidence": invocation,
    }
    claim = (
        build_perception_claim_from_camera_observation(
            observation,
            costmap_obstacle_observed=False,
            runtime_context=runtime_context,
        )
        if image_hash_matches_receipt
        else None
    )
    binding = claim.corroboration_binding if claim is not None else None
    passed = bool(
        sidecar.get("sidecar_status") == "classified"
        and image_hash_matches_receipt
        and binding is not None
        and binding.live_vlm_invocation_observed
        and binding.progressive_action_supported
    )
    result = {
        "schema_version": "missionos_turtlebot3_live_perception_binding_smoke.v1",
        "smoke_passed": passed,
        "decision_epoch_ref": decision_epoch_ref,
        "capture_receipt_sha256": sha256(receipt_bytes).hexdigest(),
        "capture_image_sha256": receipt_image_sha256,
        "observed_image_sha256": image_sha256,
        "image_hash_matches_receipt": image_hash_matches_receipt,
        "sidecar_status": str(sidecar.get("sidecar_status") or ""),
        "sidecar_blocking_reasons": list(sidecar.get("blocking_reasons") or ()),
        "vlm_invocation": {
            "schema_version": invocation.get("schema_version"),
            "invocation_kind": invocation.get("invocation_kind"),
            "invocation_target": invocation.get("invocation_target"),
            "provider": invocation.get("provider"),
            "model_id": invocation.get("model_id"),
            "input_image_sha256": invocation.get("input_image_sha256"),
            "invocation_ref": invocation.get("invocation_ref"),
            "invocation_started_at": invocation.get("invocation_started_at"),
            "invocation_completed_at": invocation.get("invocation_completed_at"),
            "invocation_exit_code": invocation.get("invocation_exit_code"),
        },
        "perception_claim": claim.model_dump(mode="json") if claim else {},
        "approval_created": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
    }
    return result, passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-receipt", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument(
        "--decision-epoch-ref",
        default="turtlebot3_live_perception_smoke:epoch:1",
    )
    args = parser.parse_args()
    try:
        result, passed = run_smoke(
            capture_receipt_path=args.capture_receipt,
            image_path=args.image,
            decision_epoch_ref=args.decision_epoch_ref,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": (
                "missionos_turtlebot3_live_perception_binding_smoke.v1"
            ),
            "smoke_passed": False,
            "blocking_reasons": [f"live_perception_smoke_failed:{type(exc).__name__}"],
            "approval_created": False,
            "dispatch_authority_created": False,
            "dispatch_request_sent": False,
            "physical_execution_invoked": False,
            "completion_claimed": False,
        }
        passed = False
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
