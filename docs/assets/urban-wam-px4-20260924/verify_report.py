"""Check public urban-report arithmetic and scope, without simulation or model calls."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.urban_navigation_contract import (  # noqa: E402
    scene_spec,
    segment_box_clearance,
    route_check,
    digest,
)


def require(value, message):
    if not value:
        raise ValueError(message)


def close(a, b):
    require(
        math.isfinite(a) and math.isclose(a, b, rel_tol=0, abs_tol=1e-8),
        "inconsistent route measurement",
    )


def verify_data(data):
    require(
        data["schema_version"] == "missionos_urban_public_report.v1", "report schema"
    )
    require(
        data["scope"] == "bounded_engineering_trials_not_randomized_benchmark",
        "benchmark scope",
    )
    for key in (
        "physical_hardware_executed",
        "learned_navigation_benefit_established",
        "collision_prediction_validated",
        "receding_horizon_replanning_implemented",
    ):
        require(data[key] is False, "unsupported scope: " + key)
    resources = data["resources"]
    require(
        resources["created_vm_absent"] is True
        and resources["created_boot_disk_absent"] is True,
        "GPU cleanup not verified",
    )
    require(
        resources["invoice_reconciled"] is False, "invoice reconciliation not observed"
    )
    require(
        0
        < resources["current_run_estimate_upper_bound_usd"]
        <= resources["total_task_conservative_estimate_upper_bound_usd"]
        <= resources["user_total_cap_usd"]
        == 10,
        "cost bound differs",
    )
    for run in data["runs"]:
        v, points = run["verification"], run["trajectory"]
        scene = scene_spec(v["family"])
        require(v["route_id"] in scene["routes"], "undeclared route")
        require(
            v["route_sha256"] == digest(scene["routes"][v["route_id"]]), "route binding"
        )
        require(
            v["destination_reached"] is True
            and v["landing_and_disarm_observed"] is True
            and v["simulator_removed"] is True,
            "incomplete observed outcome",
        )
        require(
            v["physical_hardware_executed"] is False
            and v["jev_invoked"] is False
            and v["learned_navigation_benefit_established"] is False,
            "unsupported execution claim",
        )
        require(
            len(points) == v["trajectory_samples"] and len(points) > 10,
            "trajectory missing",
        )
        require(
            all(
                len(p) == 4
                and all(type(x) in (int, float) and math.isfinite(x) for x in p)
                for p in points
            ),
            "invalid trajectory",
        )
        close(points[0][0], 0)
        close(points[-1][0], v["route_simulation_seconds"])
        close(
            sum(math.dist(a[1:], b[1:]) for a, b in zip(points, points[1:])),
            v["observed_distance_m"],
        )
        close(max(p[3] for p in points), v["maximum_altitude_m"])
        require(
            math.dist(points[-1][1:], scene["goal_enu_m"]) <= scene["goal_radius_m"],
            "destination not reached",
        )
        gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
        require(all(0 <= g <= 0.25 for g in gaps), "missing trajectory interval")
        close(max(gaps), v["maximum_trajectory_sim_gap_seconds"])
        minimum = min(
            segment_box_clearance(
                a[1:],
                b[1:],
                building["lower_enu_m"],
                building["upper_enu_m"],
                scene["airframe_radius_m"],
            )
            for a, b in zip(points, points[1:])
            for building in scene["buildings"]
        )
        close(minimum, v["minimum_observed_envelope_clearance_m"])
        require(
            minimum >= scene["required_clearance_m"],
            "envelope intersects building margin",
        )
        require(
            v["building_contact_messages"] == 0
            and v["contact_sensor_streams_observed"] is False
            and v["ground_contact_positive_control_messages"] > 0,
            "contact observation scope differs",
        )
        require(
            all(re.fullmatch("[a-f0-9]{64}", s) for s in v["source_sha256"].values()),
            "missing source bindings",
        )
        if v["model_invoked"]:
            require(
                v["model_forecast_used_for_dispatch"] is True,
                "model did not control route",
            )
            model, choice = run["model"], v["model_selection"]
            require(
                model["actual_model_calls"] == len(scene["routes"])
                and model["sampling_steps"] == 250
                and model["context_size"] == 16,
                "model call contract",
            )
            require(
                model["model_time_alignment_verified"] is False
                and model["nominal_horizon_seconds"] == 8
                and model["prefix_path_length_m"] == 5
                and model["goal_image_used_for_scoring_only"] is True,
                "preview scope",
            )
            require(
                0
                <= model["observation_age_at_selection_seconds"]
                <= model["observation_age_at_dispatch_seconds"]
                <= 180,
                "expired observation",
            )
            stamps = model["input_frame_simulation_time_ns"]
            require(
                len(stamps) == 16
                and all(
                    abs((b - a) / 1e9 - 0.25) <= 0.004000001
                    for a, b in zip(stamps, stamps[1:])
                ),
                "invalid model history cadence",
            )
            require(
                set(choice["scores"]) == set(scene["routes"]),
                "candidate scores missing",
            )
            eligible = [
                name
                for name, route in scene["routes"].items()
                if route_check(scene, route, start=points[0][1:])["admissible"]
            ]
            require(
                v["route_id"]
                == min(eligible, key=lambda k: (choice["scores"][k]["goal_mse"], k)),
                "selected route differs from admissible model ranking",
            )
            require(
                choice["projection_choice"]
                == min(
                    eligible,
                    key=lambda k: (choice["scores"][k]["projection_goal_mse"], k),
                ),
                "projection choice differs",
            )
            require(
                choice["multiple_admissible_routes"] == (len(eligible) > 1),
                "choice headroom differs",
            )
            require(
                choice["unconstrained_model_choice"]
                == min(
                    choice["scores"], key=lambda k: (choice["scores"][k]["goal_mse"], k)
                ),
                "unconstrained choice changed",
            )
        else:
            require(
                v["model_forecast_used_for_dispatch"] is False and "model" not in run,
                "geometric flight credited to model",
            )
    return {
        "artifact_consistency": "passed",
        "verified_report_runs": len(data["runs"]),
        "model_or_flight_rerun": False,
        "learned_navigation_benefit_established": False,
    }


def verify(root=None):
    root = Path(__file__).parent if root is None else root
    hashes = json.loads((root / "manifest.json").read_text())
    for name, sha in hashes.items():
        require(
            Path(name).name == name
            and hashlib.sha256((root / name).read_bytes()).hexdigest() == sha,
            "artifact hash mismatch",
        )
    return verify_data(json.loads((root / "summary.json").read_text()))


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
