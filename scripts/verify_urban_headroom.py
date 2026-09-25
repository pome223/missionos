#!/usr/bin/env python3
"""Recompute CPU route choices and observed flight bounds; no dispatch/model calls."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.urban_headroom_contract import (  # noqa: E402
    CASES,
    PROTOCOL,
    planner_geometry,
)  # noqa: E402
from scripts.urban_navigation_contract import digest, route_check  # noqa: E402


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_choice(root, scene, config, outcome, dispatch, events):
    import numpy as np
    from scripts.select_urban_depth_route import depth_clouds, rank_routes
    from scripts.px4_urban_headroom_trial import validate_selection, check_initial
    from scripts.px4_urban_wam_trial import validate_trigger

    session = root / "session"
    chosen = json.loads((session / "depth-selection.json").read_text())
    envelope = json.loads((session / "urban-go-accepted.json").read_text())
    frozen = [e for e in events if e["event"] == "headroom_choice_frozen"]
    require(
        len(frozen) == 1 and frozen[0]["selection"] == chosen,
        "frozen choice missing or changed",
    )
    require(
        chosen["route_id"] == outcome["route_id"] == dispatch["route_id"],
        "choice did not control flight",
    )
    require(
        sha(session / "depth-selection.json")
        == envelope["command"]["selection_sha256"]
        == outcome["selection_sha256"],
        "choice hash differs",
    )
    instant = datetime.fromisoformat(dispatch["at"]).timestamp()
    validate_trigger(
        envelope, config, (session / ".dispatch-key").read_bytes(), instant
    )
    age = validate_selection(
        chosen, envelope["command"], config, dispatch["observed_start"], instant
    )
    latest, history, receipt = depth_clouds(session / "history")
    for key, value in receipt.items():
        require(value == chosen[key], "depth source changed: " + key)
    planner = json.loads((root / "planner-input/input.json").read_text())
    expected = {
        **planner_geometry(scene),
        "observation_enu_m": receipt["observation_enu_m"],
    }
    require(
        planner == expected and digest(planner) == chosen["planner_input_sha256"],
        "planner whitelist/input changed",
    )
    surfaces = root / "planner-input/observed-surfaces.npz"
    require(sha(surfaces) == chosen["surface_file_sha256"], "surface hash differs")
    with np.load(surfaces, allow_pickle=False) as data:
        require(
            set(data.files) == {"latest", "history"}
            and np.array_equal(data["latest"], latest)
            and np.array_equal(data["history"], history),
            "surfaces do not reproduce raw depth",
        )
    choices = {
        name: rank_routes(planner, cloud)
        for name, cloud in (
            ("stale_map", []),
            ("latest_depth", latest),
            ("history_depth", history),
        )
    }
    require(
        choices == chosen["choices"]
        and choices["history_depth"]["route_id"] == chosen["route_id"],
        "choice does not reproduce",
    )
    require(
        outcome["safety_filter_rejected_frozen_choice"] is False,
        "chosen route needed safety rejection",
    )
    control = json.loads((session / "contact-positive-control.json").read_text())
    require(
        control["building_sensor_positive_control"] is True
        and control["probe_contacts"] > 0
        and control["probe_removed_observed"] is True
        and control["aircraft_commands_sent"] is False,
        "building contact sensor control missing",
    )
    initial = json.loads((root / "initial-state.json").read_text())
    check_initial(initial)
    check_initial(dispatch["observed_start"])
    return {
        "choices": choices,
        "truth_filter_on_shadow_choices": {
            name: (
                route_check(
                    scene,
                    scene["routes"][v["route_id"]],
                    start=chosen["observation_enu_m"],
                )["admissible"]
                if v["route_id"]
                else False
            )
            for name, v in choices.items()
        },
        "shadow_choices_are_not_additional_flights": True,
        "uses_scene_truth_for_selection": False,
        "safety_filter_rejected_frozen_choice": False,
        "model_invoked": False,
        "initial_position_error_m": math.dist(initial["gazebo_pose_enu_m"], [0, 0, 3]),
        "initial_speed_m_s": math.sqrt(
            sum(v * v for v in initial["local_ned_velocity_mps"])
        ),
        "initial_heading_error_rad": abs(initial["yaw_ned_rad"] - math.pi / 2),
        "observed_start_enu_m": dispatch["observed_start"]["gazebo_pose_enu_m"],
        "observation_age_at_dispatch_wall_s": age,
        "building_sensor_positive_control_contacts": control["probe_contacts"],
        "probe_removed_before_flight": True,
        "source_sha256": {
            name: sha(session / name)
            for name in (
                "depth-selection.json",
                "contact-positive-control.json",
                "urban-go-accepted.json",
                "history/capture.json",
            )
        },
        "planner_input_sha256": chosen["planner_input_sha256"],
        "surface_file_sha256": chosen["surface_file_sha256"],
    }


def gate_result(successes, attempted, *, infrastructure_failures=0):
    require(
        type(successes) is int
        and type(attempted) is int
        and 0 <= successes <= attempted <= 12,
        "invalid denominator",
    )
    remaining_upper = 12 - successes
    return {
        "fixed_cohort_cases": 12,
        "attempted_cases": attempted,
        "verified_baseline_arrivals": successes,
        "oracle_arrivals_upper_bound": 12,
        "oracle_additional_arrivals_upper_bound": remaining_upper,
        "minimum_additional_arrivals_required": 3,
        "numerical_headroom_gate_impossible": remaining_upper < 3,
        "cohort_completed": attempted == 12 and infrastructure_failures == 0,
        "gpu_gate_passed": False,
        "decision": (
            "stop_no_headroom_in_fixed_cohort"
            if remaining_upper < 3
            else (
                "incomplete_stop"
                if infrastructure_failures or attempted < 12
                else "requires_candidate_matrix_and_scorer_no_gpu_yet"
            )
        ),
        "complete_candidate_outcome_matrix_collected": False,
        "exact_physics_state_cloning_claimed": False,
        "learned_navigation_benefit_established": False,
        "new_model_calls": 0,
        "new_rented_gpu_instances": 0,
    }


def verify_cohort(root):
    from scripts.verify_urban_wam_trial import verify
    from scripts.px4_urban_headroom_trial import source_hashes

    frozen = json.loads((root / "protocol.json").read_text())
    require(
        frozen["protocol"] == PROTOCOL
        and frozen["protocol_sha256"] == digest(PROTOCOL),
        "protocol changed",
    )
    require(
        frozen["source_sha256"] == source_hashes(),
        "runtime source changed since freeze",
    )
    runs, failures = [], []
    for case in CASES:
        path = root / case
        if not path.exists():
            continue
        require(
            (path / "session/scene.json").stat().st_mtime >= frozen["frozen_at_unix_s"],
            "scene predates freeze",
        )
        try:
            observed = verify(path)
            require(
                observed["route_simulation_seconds"] <= PROTOCOL["route_deadline_sim_s"]
                and observed["route_wall_seconds"] <= PROTOCOL["route_deadline_wall_s"],
                "flight deadline exceeded",
            )
            require(
                observed["ground_contact_positive_control_messages"] > 0,
                "ground control missing",
            )
            events = [
                json.loads(s)
                for s in (path / "session/events.jsonl").read_text().splitlines()
            ]
            first = next(e for e in events if e["event"] == "urban_route_dispatch")[
                "observed_start"
            ]
            last = next(e for e in events if e["event"] == "urban_goal_observed")[
                "observed"
            ]
            t0, t1 = first["pose_simulation_time_ns"], last["pose_simulation_time_ns"]
            trace = [
                json.loads(s)
                for s in (path / "session/telemetry.jsonl").read_text().splitlines()
            ]
            samples = (
                [first]
                + [
                    s
                    for s in trace
                    if s["pose_simulation_time_ns"] is not None
                    and t0 < s["pose_simulation_time_ns"] < t1
                ]
                + [last]
            )
            observed["trajectory_columns"] = [
                "simulation_seconds",
                "east_m",
                "north_m",
                "altitude_m",
            ]
            observed["trajectory"] = [
                [(s["pose_simulation_time_ns"] - t0) / 1e9, *s["gazebo_pose_enu_m"]]
                for s in samples
            ]
            runs.append(observed)
        except Exception:
            # Public export is an explicit whitelist; exception strings can
            # contain private paths or instruction-bearing command arguments.
            error_path = path / "host-error.json"
            error = json.loads(error_path.read_text()) if error_path.exists() else {}
            cleanup_path = path / "cleanup.json"
            cleanup = (
                json.loads(cleanup_path.read_text()) if cleanup_path.exists() else {}
            )
            control_path = path / "session/contact-positive-control.json"
            control = (
                json.loads(control_path.read_text()) if control_path.exists() else {}
            )
            status_match = re.search(r"exit status (\d+)", error.get("message", ""))
            abort = (
                error.get("type") == "CalledProcessError"
                and "urban_headroom_contact_probe.py" in error.get("message", "")
                and status_match is not None
                and int(status_match[1]) == 134
                and "terminate called without an active exception"
                in error.get("stderr", "")
            )
            failures.append(
                {
                    "case": case,
                    "failure_kind": (
                        "preflight_contact_probe_native_abort"
                        if abort
                        else "measurement_verification_failed"
                    ),
                    "subprocess_exit_code": (
                        int(status_match[1]) if status_match else None
                    ),
                    "probe_report_written": control_path.exists(),
                    "building_sensor_positive_control_contacts": control.get(
                        "probe_contacts", 0
                    ),
                    "probe_removed_observed": control.get("probe_removed_observed")
                    is True,
                    "controller_events_created": (
                        path / "session/events.jsonl"
                    ).exists(),
                    "flight_result_created": (
                        path / "session/flight-result.json"
                    ).exists(),
                    "created_container_removed": cleanup.get(
                        "created_container_removed"
                    )
                    is True,
                    "source_sha256": {
                        p.name: sha(p)
                        for p in (error_path, cleanup_path, control_path)
                        if p.exists()
                    },
                }
            )
    return {
        "schema_version": "urban_headroom_public_report.v1",
        "protocol": PROTOCOL,
        "protocol_sha256": digest(PROTOCOL),
        "frozen_record_sha256": sha(root / "protocol.json"),
        "runtime_source_sha256": frozen["source_sha256"],
        "scope": "fixed_static_map_omission_cpu_development_screen",
        "gate": gate_result(
            len(runs), len(runs) + len(failures), infrastructure_failures=len(failures)
        ),
        "runs": runs,
        "failures": failures,
        "unattempted_cases": [case for case in CASES if not (root / case).exists()],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = verify_cohort(args.root)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result["gate"], indent=2))
    if result["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
