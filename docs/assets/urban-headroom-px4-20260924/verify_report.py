"""Public headroom arithmetic/scope verifier; does not rerun sensors or flights."""

from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.urban_headroom_contract import CASES, PROTOCOL, case_scene  # noqa: E402
from scripts.urban_navigation_contract import (  # noqa: E402
    digest,
    route_check,
    segment_box_clearance,
)  # noqa: E402
from scripts.verify_urban_headroom import gate_result  # noqa: E402


def require(value, message):
    if not value:
        raise ValueError(message)


def close(a, b):
    require(
        math.isfinite(a) and math.isclose(a, b, rel_tol=0, abs_tol=1e-8),
        "trajectory arithmetic differs",
    )


def verify_data(data):
    require(data["schema_version"] == "urban_headroom_public_report.v1", "schema")
    require(
        data["scope"] == "fixed_static_map_omission_cpu_development_screen", "scope"
    )
    require(
        data["protocol"] == PROTOCOL and data["protocol_sha256"] == digest(PROTOCOL),
        "protocol",
    )
    cases = [r["family"] for r in data["runs"]]
    require(
        len(cases) == len(set(cases)) and set(cases) <= set(CASES),
        "duplicate or undeclared case",
    )
    failed = [r["case"] for r in data["failures"]]
    require(
        len(failed) == len(set(failed))
        and set(failed) <= set(CASES)
        and not set(cases) & set(failed),
        "invalid failure denominator",
    )
    require(
        set(data["unattempted_cases"]) == set(CASES) - set(cases) - set(failed),
        "unattempted cases hidden",
    )
    for failure in data["failures"]:
        require(
            failure["failure_kind"] == "preflight_contact_probe_native_abort"
            and failure["subprocess_exit_code"] == 134
            and failure["probe_report_written"] is True
            and failure["building_sensor_positive_control_contacts"] > 0
            and failure["probe_removed_observed"] is True
            and failure["controller_events_created"] is False
            and failure["flight_result_created"] is False
            and failure["created_container_removed"] is True,
            "preflight failure scope differs",
        )
    expected = gate_result(
        len(cases), len(cases) + len(failed), infrastructure_failures=len(failed)
    )
    require(data["gate"] == expected, "gate arithmetic/scope differs")
    for r in data["runs"]:
        scene = case_scene(r["family"])
        require(r["selector"] == "history_depth", "wrong comparator")
        for key in (
            "destination_reached",
            "landing_and_disarm_observed",
            "simulator_removed",
        ):
            require(r[key] is True, "incomplete terminal outcome")
        for key in (
            "model_invoked",
            "model_forecast_used_for_dispatch",
            "jev_invoked",
            "physical_hardware_executed",
            "learned_navigation_benefit_established",
        ):
            require(r[key] is False, "unsupported model or hardware claim")
        s = r["depth_selection"]
        require(
            s["uses_scene_truth_for_selection"] is False
            and s["safety_filter_rejected_frozen_choice"] is False,
            "truth/filter credited to selector",
        )
        require(
            s["shadow_choices_are_not_additional_flights"] is True,
            "shadow choices counted as flight",
        )
        require(
            s["building_sensor_positive_control_contacts"] > 0
            and s["probe_removed_before_flight"] is True
            and r["ground_contact_positive_control_messages"] > 0,
            "positive controls missing",
        )
        require(r["building_contact_messages"] == 0, "building contact")
        require(
            0
            <= s["initial_position_error_m"]
            <= PROTOCOL["initial_position_tolerance_m"]
            and 0 <= s["initial_speed_m_s"] <= PROTOCOL["initial_speed_limit_m_s"]
            and 0
            <= s["initial_heading_error_rad"]
            <= PROTOCOL["initial_yaw_tolerance_rad"],
            "start tolerance",
        )
        require(
            0
            <= s["observation_age_at_dispatch_wall_s"]
            <= PROTOCOL["input_maximum_age_wall_s"],
            "stale input",
        )
        require(set(s["choices"]) == set(PROTOCOL["comparators"]), "missing comparator")
        for name, choice in s["choices"].items():
            scores = choice["scores"]
            require(set(scores) == set(scene["routes"]), "candidate missing")
            eligible = [
                key
                for key, v in scores.items()
                if not v["rejected_by_observed_surface"]
                and not v["rejected_by_available_map"]
            ]
            expected_choice = (
                min(eligible, key=lambda k: (scores[k]["route_length_m"], k))
                if eligible
                else None
            )
            require(choice["route_id"] == expected_choice, "ranking differs")
            expected_filter = (
                route_check(
                    scene,
                    scene["routes"][expected_choice],
                    start=s["observed_start_enu_m"],
                )["admissible"]
                if expected_choice
                else False
            )
            require(
                s["truth_filter_on_shadow_choices"][name] == expected_filter,
                "shadow filter differs",
            )
        require(
            s["choices"]["history_depth"]["route_id"] == r["route_id"],
            "executed route differs",
        )
        points = r["trajectory"]
        require(
            len(points) == r["trajectory_samples"] and len(points) > 10,
            "trajectory count",
        )
        require(
            all(
                len(p) == 4
                and all(type(x) in (int, float) and math.isfinite(x) for x in p)
                for p in points
            ),
            "nonfinite trajectory",
        )
        close(points[0][0], 0)
        close(points[-1][0], r["route_simulation_seconds"])
        require(
            math.dist(points[-1][1:], scene["goal_enu_m"]) <= scene["goal_radius_m"],
            "goal not reached",
        )
        require(
            r["route_simulation_seconds"] <= PROTOCOL["route_deadline_sim_s"]
            and r["route_wall_seconds"] <= PROTOCOL["route_deadline_wall_s"],
            "deadline",
        )
        close(
            sum(math.dist(a[1:], b[1:]) for a, b in zip(points, points[1:])),
            r["observed_distance_m"],
        )
        close(max(p[3] for p in points), r["maximum_altitude_m"])
        require(
            all(0 <= b[0] - a[0] <= 0.25 for a, b in zip(points, points[1:])),
            "trajectory gap",
        )
        require(
            all(
                all(
                    lo <= v <= hi
                    for v, lo, hi in zip(
                        p[1:],
                        scene["geofence_lower_enu_m"],
                        scene["geofence_upper_enu_m"],
                    )
                )
                for p in points
            ),
            "geofence",
        )
        clearance = min(
            segment_box_clearance(
                a[1:],
                b[1:],
                o["lower_enu_m"],
                o["upper_enu_m"],
                scene["airframe_radius_m"],
            )
            for a, b in zip(points, points[1:])
            for o in scene["buildings"]
        )
        close(clearance, r["minimum_observed_envelope_clearance_m"])
        require(clearance >= scene["required_clearance_m"], "envelope clearance")
    return {
        "artifact_consistency": "passed",
        "verified_baseline_flights": len(cases),
        "gate": expected,
        "sensor_or_flight_rerun": False,
    }


def verify(root=None):
    root = Path(__file__).parent if root is None else root
    for name, expected in json.loads((root / "manifest.json").read_text()).items():
        require(
            Path(name).name == name
            and hashlib.sha256((root / name).read_bytes()).hexdigest() == expected,
            "artifact hash mismatch",
        )
    return verify_data(json.loads((root / "summary.json").read_text()))


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
