"""Check reviewed integration results; does not rerun Gateway or physics."""

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.resolve().parents[2]))
from scripts.urban_headroom_contract import PROTOCOL, case_scene  # noqa: E402
from scripts.urban_navigation_contract import digest, route_check  # noqa: E402


def require(value, message):
    if not value:
        raise ValueError(message)


def verify_data(data):
    require(
        data["scope"] == "cli_gateway_static_depth_navigation_integration",
        "scope differs",
    )
    require(
        data["new_model_calls"] == 0 and data["new_rented_gpus"] == 0,
        "unsupported model claim",
    )
    require(data["not_part_of_r2_cohort"] is True, "cohorts pooled")
    require(
        data["unauthenticated_request_rejected"] is True
        and data["missing_approval_rejected"] is True
        and data["default_off_http_check_passed"] is True,
        "authority checks missing",
    )
    require(
        [r["scene"] for r in data["cases"]] == ["gap", "climb", "detour"],
        "case order differs",
    )
    require(
        data["initial_result_schema_label_correct"] is False
        and data["initial_flight_evidence_retained_unchanged"] is True,
        "initial defect hidden",
    )
    repeat = data["post_schema_fix_regression"]
    require(
        repeat["scene"] == "climb"
        and repeat["result"]["schema_version"] == "px4_depth_navigation_result.v1",
        "post-fix result schema",
    )
    for row in [*data["cases"], repeat]:
        result = row["result"]
        scene = case_scene(f"headroom_{row['scene']}_0")
        require(
            row["replay_rejected"] is True
            and row["same_task_approval_consumed"] is True,
            "authority binding missing",
        )
        require(
            result["family"] == scene["family"]
            and result["result_status"] == "verified",
            "result binding",
        )
        for field in (
            "destination_reached",
            "landing_and_disarm_observed",
            "simulator_removed",
        ):
            require(result[field] is True, "terminal evidence missing")
        for field in (
            "model_invoked",
            "jev_invoked",
            "model_forecast_used_for_dispatch",
            "physical_hardware_executed",
            "physical_execution_invoked",
            "delivery_completion_claimed",
            "learned_navigation_benefit_established",
        ):
            require(result[field] is False, "unsupported claim")
        require(
            result["selector"] == "history_depth"
            and result["building_contact_messages"] == 0,
            "selector or contact",
        )
        require(
            result["minimum_observed_envelope_clearance_m"]
            >= scene["required_clearance_m"],
            "clearance",
        )
        route = scene["routes"][result["route_id"]]
        require(
            result["route_sha256"] == digest(route)
            and route_check(scene, route)["admissible"],
            "route differs",
        )
        require(
            0 < result["route_simulation_seconds"] <= PROTOCOL["route_deadline_sim_s"]
            and 0 < result["route_wall_seconds"] <= PROTOCOL["route_deadline_wall_s"],
            "deadline",
        )
        require(
            0
            <= result["depth_selection"]["observation_age_at_dispatch_wall_s"]
            <= PROTOCOL["input_maximum_age_wall_s"],
            "stale observation",
        )
    return {
        "artifact_consistency": "passed",
        "verified_integration_flights": 4,
        "model_calls": 0,
        "sensor_or_flight_rerun": False,
    }


def verify():
    for name, expected in json.loads((ROOT / "manifest.json").read_text()).items():
        require(
            Path(name).name == name
            and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected,
            "artifact hash mismatch",
        )
    return verify_data(json.loads((ROOT / "summary.json").read_text()))


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
