"""One planned PX4 checkpoint/reselection inside a normal depth task.

The supplied gap scenario includes a scripted static barrier appearing after
departure. This is an opt-in simulator integration, not general obstacle recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts import px4_urban_reobserve_trial as trial
from scripts.urban_navigation_contract import digest, scene_spec
from scripts.urban_reobserve_contract import PROTOCOL, CHECKPOINT, IMAGE, routes

ROOT = Path(__file__).resolve().parents[2]


def build_request():
    scene = scene_spec("gap")
    return {
        "schema_version": "px4_depth_navigation_request.v1",
        "scene": "gap",
        "reobserve": True,
        "scene_sha256": scene["scene_sha256"],
        "routes": routes("resume"),
        "approach_routes": routes("approach"),
        "goal_enu_m": scene["goal_enu_m"],
        "checkpoint_enu_m": CHECKPOINT,
        "maximum_resume_decisions": 1,
        "runtime_source_sha256": {
            **trial.source_hashes(),
            **{
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in (
                    "src/runtime/px4_depth_navigation.py",
                    "src/runtime/px4_reobserve_navigation.py",
                    "scripts/verify_urban_reobserve.py",
                )
            },
        },
        "reobserve_protocol_sha256": digest(PROTOCOL),
        "image_id": IMAGE,
        "selector": "latest_native_depth_at_planned_stop",
        "scope": "fixed_gap_with_scripted_barrier_and_one_reobservation",
        "operator_approval_required_before_dispatch": True,
        "unknown_space_certified_free": False,
        "model_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def binding(request, approval):
    return {
        "task_id": approval["task_id"],
        "request_sha256": digest(request),
        "execution_approval_id": approval["approval_id"],
        "approval_scope": approval["scope"],
    }


def run_live(root, request, approval, progress):
    from src.runtime.px4_depth_navigation import _require

    _require(request == build_request(), "reobserve request or runtime changed")
    assets = os.getenv("MISSIONOS_PX4_DEPTH_ASSETS", "")
    _require(bool(assets), "server-side pinned urban assets must be configured")
    trial.run_session(
        root,
        Path(assets).resolve(),
        "gateway-depth-approval:" + approval["approval_id"],
        "reobserve",
        progress,
        binding(request, approval),
    )


def verify_run(root, request, approval):
    from scripts.verify_urban_reobserve import verify
    from src.runtime.px4_depth_navigation import _require, approval_scope

    _require(request == build_request(), "reobserve request or runtime changed")
    _require(approval["scope"] == approval_scope(request), "reobserve scope differs")
    _require(
        json.loads((root / "gateway-binding.json").read_text()) == binding(request, approval),
        "Gateway reobserve binding differs",
    )
    config = json.loads((root / "session/config.json").read_text())
    _require(
        config["mode"] == "reobserve"
        and config["approved_instruction_ref"] == "gateway-depth-approval:" + approval["approval_id"]
        and config["runtime_source_sha256"] == trial.source_hashes(),
        "flight did not use this task reobserve approval",
    )
    observed = verify(root)
    _require(
        observed["landing_and_disarm_observed"] is True
        and observed["simulator_removed"] is True
        and observed["model_invoked"] is False
        and observed["replan_count"] in (0, 1)
        and (observed["destination_reached"] is True or observed["safe_abort"] is True),
        "reobserve terminal outcome incomplete",
    )
    return {
        **observed,
        "source_verification_schema_version": observed["schema_version"],
        "schema_version": "px4_depth_navigation_result.v1",
        "result_status": "verified" if observed["destination_reached"] else "safe_aborted",
        "route_id": observed["choices"]["resume"]["selected"],
        "reobserve": True,
        "request_sha256": digest(request),
        "execution_approval_id": approval["approval_id"],
        "flight_outcome_verified": True,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
    }
