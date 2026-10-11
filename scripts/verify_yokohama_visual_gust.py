"""Offline measured-trajectory/transport verifier for the new CPU-only scope."""

import hashlib
import json
import math
from pathlib import Path

from scripts.yokohama_visual_gust_contract import validate_config
from scripts.verify_yokohama_candidate_recovery import stable_observed, raw_field
from scripts.yokohama_altitude_contract import verify_transport_evidence


def verify(root):
    root = Path(root)
    try:
        config = json.loads((root / "config.json").read_text())
        p = validate_config(config)
        result = json.loads((root / "result.json").read_text())
        events = [json.loads(x) for x in (root / "flight-events.jsonl").read_text().splitlines()]
        rows = [json.loads(x) for x in (root / "flight-trajectory.jsonl").read_text().splitlines()]

        def require(value, reason):
            if not value:
                raise ValueError(reason)

        checks = {}
        require(
            result["config_sha256"]
            == hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
            "config_changed",
        )
        for name, expected in result["source_sha256"].items():
            require(
                hashlib.sha256((root / "sources" / name).read_bytes()).hexdigest() == expected,
                "runtime_source_changed_" + name,
            )
        sdf = root / "models/worlds/default.sdf"
        require(
            hashlib.sha256(sdf.read_bytes()).hexdigest() == config["world"]["world_sha256"],
            "world_changed",
        )
        require(
            result.get("decision_backend") == "fixture"
            and not any(
                result.get(k)
                for k in (
                    "vla_invoked",
                    "wam_invoked",
                    "gpu_requested",
                    "physical_execution_invoked",
                )
            ),
            "CPU_claim_boundary",
        )
        inspect = json.loads((root / "container-inspect.json").read_text())[0]
        require(
            inspect["HostConfig"]["NetworkMode"] == "none"
            and not inspect["HostConfig"].get("DeviceRequests"),
            "container_network_GPU_boundary",
        )
        checks["source_world_config_CPU_bound"] = True
        alignment = json.loads((root / "initial-alignment.json").read_text())
        from src.runtime.yokohama_native import camera_heading

        desired = math.radians(config["simulator_initial_heading_deg"])
        stable_observed(
            alignment,
            lambda r: (
                r["landed"] is True
                and r["arming_state"] == 1
                and 0 <= r["vehicle"]["age_s"] <= 2
                and math.hypot(*r["velocity_ned"]) <= 0.3
                and abs(math.remainder(camera_heading(r) - desired, 2 * math.pi)) <= 0.03
                and abs(math.remainder(r["heading_ned_rad"] - camera_heading(r), 2 * math.pi))
                <= 0.03
            ),
        )
        checks["distinct_ground_alignment_before_arm"] = True
        verify_transport_evidence(root, config, events)
        checks["altitude_upload_binding"] = True
        requests = [
            json.loads(f.read_text()) for f in sorted((root / "decisions").glob("*-request.json"))
        ]
        counts = {op: sum(r["operation"] == op for r in requests) for op in ("vla", "wam")}
        scenario = config["visual_gust_trial"]["scenario"]
        expected_counts = (
            {"vla": 4, "wam": 4} if scenario in ("calm", "reach") else {"vla": 3, "wam": 2}
        )
        require(counts == expected_counts, "finite_actual_request_budget")
        dispatch = [e for e in events if e["event"] == "city_segment_dispatched"]
        arrivals = [e for e in events if e["event"] == "city_segment_arrived"]
        require(
            len(dispatch) == len(arrivals) == (4 if scenario in ("calm", "reach") else 2),
            "observed_segment_count",
        )
        for index, e in enumerate(arrivals):
            row = e["observation"]
            activate = next(
                r for r in requests if r["operation"] == "activate" and r["cycle"] == index + 1
            )
            target = (
                activate["permit"]["candidate"]["target_world_xyz_m"]
                if "permit" in activate
                else None
            )
            # Bound permit is in the actual response to the activation request.
            response = json.loads(
                (root / "decisions" / f"{activate['sequence']:03d}-response.json").read_text()
            )["value"]
            target = response["candidate"]["target_world_xyz_m"]
            require(
                math.dist(row["vehicle"]["xyz"], target) <= 0.25
                and abs(row["vehicle"]["xyz"][2] - target[2]) <= 0.15
                and math.hypot(*row["velocity_ned"]) <= 0.3,
                "arrival_tolerance",
            )
            require(
                response["rules"]["allowed"]
                and min(response["rules"]["minimum_centerline_clearances_m"]) > 2,
                "geometry_gate",
            )
        checks["all_admitted_segments_observed"] = True
        a, b = p["entry_world_xyz_m"], p["goal_world_xyz_m"]
        model_started_wall_s = min(r["observation"]["wall_s"] for r in requests)
        d = [b[i] - a[i] for i in (0, 1)]
        for row in rows:
            if row["wall_s"] < model_started_wall_s or row["phase"] not in (
                "00-D1",
                "01-FEEDBACK-EXIT",
                "recovery_return",
                "recovery_land",
                "return_land",
            ):
                continue
            xyz = row["vehicle"]["xyz"]
            t = max(0, min(1, sum((xyz[i] - a[i]) * d[i] for i in (0, 1)) / sum(x * x for x in d)))
            lateral = math.hypot(*(xyz[i] - a[i] - t * d[i] for i in (0, 1)))
            require(
                lateral <= 1.5
                and 0 <= row["vehicle"]["age_s"] <= 2
                and row["position_valid"]
                and row["battery_fraction"] >= 0.2,
                "measured_corridor_or_freshness",
            )
            if row["phase"] not in ("recovery_land", "return_land"):
                require(abs(xyz[2] - a[2]) <= 0.5, "measured_altitude_envelope")
        checks["measured_corridor_altitude_freshness"] = True
        if scenario == "calm":
            require(
                not (root / "weak-gust.json").exists() and not config["world"].get("wind"),
                "calm_has_wind",
            )
            checks["calm_without_wind"] = True
        else:
            gust = json.loads((root / "weak-gust.json").read_text())
            receipts = gust["receipts"]
            require(
                gust["pulse_count"] == 1
                and len(receipts) == 2
                and [r["requested_enu_mps"] for r in receipts] == [[0.5, 0, 0], [0, 0, 0]]
                and all(r["confirmed"] for r in receipts),
                "one_confirmed_weak_pulse",
            )
            require(
                2 <= receipts[1]["start_sim_s"] - receipts[0]["end_sim_s"] <= 3, "pulse_duration"
            )
            samples = gust["rows"]
            witnesses = [
                (r["wind_probe"]["wind_witness"], r["wind_probe"]["wind_control"]) for r in samples
            ]
            require(
                len(witnesses) >= 3
                and all(0 <= v["age_s"] <= 2 for pair in witnesses for v in pair),
                "wind_witness_freshness",
            )
            require(
                witnesses[-1][0]["xyz"][0] - witnesses[0][0]["xyz"][0] > 0.05
                and all(math.dist(pair[1]["xyz"], [0, 3010, 100]) < 0.001 for pair in witnesses),
                "physical_force_against_control",
            )
            settled = next(
                e for e in events if e["event"] == "weak_gust_settled_before_reobservation"
            )
            second = next(r for r in requests if r["operation"] == "vla" and r["cycle"] == 2)
            capture = json.loads((root / second["capture"]["file"]).read_text())
            require(
                capture["frames"][0]["stamp_ns"] / 1e9 > settled["observation"]["sim_s"],
                "fresh_images_after_gust",
            )
            require(
                math.hypot(*gust["after"]["velocity_ned"]) <= 0.3
                and gust["after"]["nav_state"] == 4,
                "PX4_settled_before_model",
            )
            checks["one_physical_pulse_and_fresh_reobservation"] = True
        if scenario in ("calm", "reach"):
            require(
                result["status"] == "passed"
                and result["observed"]["model_goal_reached"] is True
                and math.dist(arrivals[-1]["observation"]["vehicle"]["xyz"], b) <= 0.5,
                "eight_metre_goal_not_observed",
            )
            require(
                any(e["event"] == "city_session_revoked" for e in events), "authority_not_revoked"
            )
        else:
            rejected = next(e for e in events if e["event"] == "candidate_rejected")
            revoked = next(e for e in events if e["event"] == "model_authority_revoked")
            returned = next(e for e in events if e["event"] == "recovery_return_observed")
            final = next(e for e in events if e["event"] == "recovery_landing_disarm_observed")
            require(
                rejected["wall_s"] <= revoked["wall_s"] < returned["wall_s"] < final["wall_s"],
                "recovery_order",
            )
            require(
                rejected["recovery_deadline_wall_s"] == rejected["recovery_begin_wall_s"] + 220
                and final["wall_s"] <= rejected["recovery_deadline_wall_s"],
                "fixed_return_deadline",
            )
            require(
                result["mission_outcome"] == "failed"
                and result["observed"]["status"] == "failed_recovered",
                "failed_mission_not_preserved",
            )
        require(
            any(e["event"] == "city_late_response_rejected" for e in events),
            "late_response_guard_not_observed",
        )
        stable_observed(
            [r for r in rows if r["phase"] in ("01-FEEDBACK-EXIT", "recovery_return")],
            lambda r: (
                math.dist(r["vehicle"]["xyz"][:2], a[:2]) <= 0.25
                and abs(r["vehicle"]["xyz"][2] - a[2]) <= 0.15
                and math.hypot(*r["velocity_ned"]) <= 0.3
                and r["arming_state"] == 2
            ),
        )
        final = rows[-1]
        require(
            final["landed"] is True
            and final["arming_state"] == 1
            and math.dist(final["vehicle"]["xyz"][:2], a[:2]) < 1.5
            and -0.2 <= final["vehicle"]["xyz"][2] <= 0.5,
            "return_landing_disarm",
        )
        terminal = stable_observed(
            [r for r in rows if r["phase"] in ("return_land", "recovery_land")],
            lambda r: (
                r["landed"] is True
                and r["arming_state"] == 1
                and raw_field(r["raw_px4"]["vehicle_status"], "arming_state") == 1
                and raw_field(r["raw_px4"]["vehicle_land_detected"], "landed") is True
                and math.dist(r["vehicle"]["xyz"][:2], a[:2]) < 1.5
                and -0.2 <= r["vehicle"]["xyz"][2] <= 0.5
                and math.hypot(*r["velocity_ned"]) <= 0.3
            ),
        )
        require(final["sim_s"] >= terminal["sim_s"], "terminal_state_not_distinct_stable")
        contacts = [json.loads(x) for x in (root / "sensor-events.jsonl").read_text().splitlines()]
        require(
            any(
                c["topic"] == "launch_pad"
                and "x500" in c["collision1"] + c["collision2"]
                and final["sim_s"] - 2 <= c["sensor_sim_s"] <= final["sim_s"]
                for c in contacts
            ),
            "fresh_launch_contact",
        )
        checks["revoked_late_guard_return_land_disarm"] = True
        return dict(
            status="passed",
            scenario=scenario,
            checks=checks,
            requests=counts,
            mission_outcome="passed" if scenario in ("calm", "reach") else "failed",
            interpretation="CPU fixture wiring, measured PX4/Gazebo wind/return only; no learned visual performance",
        )
    except Exception as exc:
        return dict(status="failed", reason=type(exc).__name__ + ": " + str(exc))
