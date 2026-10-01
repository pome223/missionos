"""Opt-in stationary-ship mission on real local PX4/Gazebo processes."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from missionos_core import canonical_sha256

from src.runtime.ship_delivery import ShipDeliveryScenario, _preflight_reasons
from src.runtime.ship_delivery_px4 import build_stationary_ship_px4_plan
from src.runtime.ship_delivery_world import prepare_ship_world
from src.runtime.runtime_claim_evidence import validate_runtime_invocation_evidence


IMAGE = "px4io/px4-sitl-gazebo:latest"


def _return_permit(config: dict, samples: list[dict]) -> dict:
    return {
        **{k: config[k] for k in ("run_id", "world_sha256", "plan_sha256")},
        "delivery_verified": True,
        "evidence_sample_count": len(samples),
        "evidence_sha256": canonical_sha256({"samples": samples}),
    }


def _run(args, *, timeout=60, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=timeout)


def _read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    content = path.read_text()
    lines = content.splitlines()
    for index, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not content.endswith("\n"):
                break  # A writer may be in the middle of the final line.
            raise ValueError("Corrupt complete telemetry record") from None
    return rows


def build_ship_sitl_missions(s: ShipDeliveryScenario) -> dict:
    """Two AUTO missions with an unbounded delivery hold between them.

    The hold cannot expire into a return. The runtime uploads the return leg
    only after the independent payload verifier produces a bound permit.
    """
    plan = build_stationary_ship_px4_plan(
        offshore_distance_m=s.offshore_distance_m,
        urban_distance_m=s.urban_distance_m,
        cruise_altitude_m=s.cruise_altitude_m,
    )
    split = plan["dropoff_dwell_mission_seq"]
    outbound = plan["mission_items"][:split]
    low_hold = dict(outbound[-1], command=17, altitude_m=3.0, param1=0.0, param2=0.0)
    outbound.append(low_hold)
    returning = [
        dict(outbound[-2], current=1),
        *plan["mission_items"][plan["return_start_mission_seq"] :],
    ]
    for items in (outbound, returning):
        for i, item in enumerate(items):
            item.update(seq=i, current=int(i == 0))
    return {
        "outbound": outbound,
        "return": returning,
        "frame": "global_relative_altitude",
        "goal_north_m": s.offshore_distance_m + s.urban_distance_m,
        "release_altitude_m": 3.0,
        "return_requires_verified_delivery": True,
    }


def verify_ship_sitl_delivery(samples: list[dict], config: dict) -> dict:
    reasons = []
    if not samples:
        return {"verified": False, "reasons": ["no_observations"]}
    expected = None
    previous_time = -1.0
    for row in samples:
        stamp = row.get("elapsed_s")
        if (
            not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or stamp <= previous_time
        ):
            reasons.append("observation_time_invalid")
        else:
            previous_time = stamp
        if (
            row.get("run_id") != config["run_id"]
            or row.get("world_sha256") != config["world_sha256"]
        ):
            reasons.append("observation_binding_mismatch")
        if not row.get("poses_fresh") or not all(
            row.get(k) for k in ("vehicle", "payload", "ship")
        ):
            continue
        identities = tuple(row[k]["id"] for k in ("vehicle", "payload", "ship"))
        if expected is None:
            expected = identities
        if identities != expected:
            reasons.append("gazebo_entity_identity_changed")
        for entity in ("vehicle", "payload", "ship"):
            point = row[entity].get("xyz")
            if (
                not isinstance(point, list)
                or len(point) != 3
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in point)
            ):
                return {"verified": False, "reasons": ["invalid_entity_position"]}
        if math.dist(row["ship"]["xyz"], [0, 0, -1]) > 0.001:
            reasons.append("ship_not_stationary")
    airborne = [
        r
        for r in samples
        if r.get("vehicle")
        and r.get("payload")
        and r.get("poses_fresh")
        and r["vehicle"]["xyz"][2] > 10
        and r["phase"] == "outbound"
    ]
    carried = any(math.dist(r["vehicle"]["xyz"], r["payload"]["xyz"]) < 1 for r in airborne)
    if not carried:
        reasons.append("airborne_payload_attachment_not_observed")
    stable = []
    goal = config["goal_north_m"]
    for row in samples:
        if not (
            row["phase"] in ("delivery_verify", "return")
            and row.get("payload")
            and row.get("vehicle")
            and row.get("poses_fresh")
        ):
            stable = []
            continue
        if stable and row["elapsed_s"] - stable[-1]["elapsed_s"] > 1.5:
            stable = []
        x, y, z = row["payload"]["xyz"]
        valid = (
            math.hypot(x, y - goal) < 6
            and 0.04 <= z <= 0.2
            and row["payload_contact"]
            and math.dist(row["payload"]["xyz"], row["vehicle"]["xyz"]) > 1.5
        )
        if valid:
            stable.append(row)
        else:
            stable = []
    if len(stable) < 2 or stable[-1]["elapsed_s"] - stable[0]["elapsed_s"] < 3:
        reasons.append("payload_touchdown_and_stability_not_observed")
    elif max(math.dist(r["payload"]["xyz"], stable[-1]["payload"]["xyz"]) for r in stable) > 0.3:
        reasons.append("payload_not_stationary")
    return {
        "verified": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "entity_ids": expected,
        "stability_samples": len(stable),
    }


def verify_ship_sitl_run(
    samples: list[dict], events: list[dict], config: dict, *, evidence_root: Path | None = None
) -> dict:
    delivery = verify_ship_sitl_delivery(samples, config)
    reasons = list(delivery["reasons"])
    event_names = [e.get("event") for e in events]
    for name in (
        "outbound_upload",
        "outbound_ready",
        "outbound_requested",
        "wind_observed",
        "detach_published",
        "return_authorized",
        "return_hold_observed",
        "return_upload",
        "return_ready",
        "return_requested",
        "return_climb_observed",
        "recovery_candidate_observed",
    ):
        if name not in event_names:
            reasons.append(f"missing_event:{name}")
    uploads = [e for e in events if e.get("event", "").endswith("_upload")]
    expected_uploads = {"outbound_upload", "return_upload"}
    if config.get("urban"):
        expected_uploads.add("urban_upload")
    if (
        len(uploads) != len(expected_uploads)
        or {e["event"] for e in uploads} != expected_uploads
        or any(e["receipt"].get("mission_ack_type") != 0 for e in uploads)
    ):
        reasons.append("mission_upload_not_verified")
    if "worker_failed" in event_names:
        reasons.append("worker_failed")
    required_order = [
        "outbound_upload",
        "outbound_ready",
        "outbound_requested",
        "detach_published",
        "return_authorized",
        "return_hold_observed",
        "return_upload",
        "return_ready",
        "return_requested",
        "return_climb_observed",
        "recovery_candidate_observed",
    ]
    if all(name in event_names for name in required_order):
        indices = [event_names.index(name) for name in required_order]
        if indices != sorted(indices):
            reasons.append("mission_event_order_invalid")
    for event in events:
        if event.get("run_id") != config["run_id"]:
            reasons.append("event_run_binding_mismatch")
        if event.get("event") == "return_authorized":
            binding = event.get("binding", {})
            if binding.get("delivery_verified") is not True or any(
                binding.get(k) != config[k] for k in ("run_id", "world_sha256", "plan_sha256")
            ):
                reasons.append("return_authority_binding_mismatch")
            count = binding.get("evidence_sample_count")
            if (
                type(count) is not int
                or not 0 < count <= len(samples)
                or binding.get("evidence_sha256") != canonical_sha256({"samples": samples[:count]})
                or not verify_ship_sitl_delivery(samples[:count], config)["verified"]
            ):
                reasons.append("return_delivery_evidence_mismatch")
        if event.get("event") == "wind_observed":
            from scripts.ship_delivery_sitl_worker import field

            readback = event.get("readback", "")
            if (
                "enable_wind: true" not in readback
                or abs((field(readback, "y") or 0) - config["wind_mps"]) > 0.01
            ):
                reasons.append("wind_runtime_readback_mismatch")
    tail = []
    for row in samples:
        v = row.get("vehicle")
        velocity = row.get("velocity_ned", [])
        valid = (
            row["phase"] == "return"
            and row.get("poses_fresh")
            and v
            and math.hypot(*v["xyz"][:2]) < 3
            # Model origin differs from foot contact height; allow 2 cm below deck.
            and -0.02 <= v["xyz"][2] < 1
            and row["landed"] is True
            and row["arming_state"] == 1
            and row["deck_contact"]
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in velocity)
            and len(velocity) == 3
            and math.sqrt(sum(x * x for x in velocity)) < 0.3
        )
        if tail and row["elapsed_s"] - tail[-1]["elapsed_s"] > 1.5:
            tail = []
        tail = [*tail, row] if valid else []
    recovered = len(tail) >= 2 and tail[-1]["elapsed_s"] - tail[0]["elapsed_s"] >= 3
    if not recovered:
        reasons.append("stationary_deck_recovery_not_verified")
    battery = [r.get("battery_remaining") for r in samples]
    battery_valid = bool(battery) and all(
        isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
        for value in battery
    )
    minimum_battery = min(battery) if battery_valid else None
    # PX4's simulated battery recharges after disarm. The final sample alone
    # therefore cannot establish that the flight retained its required reserve.
    if minimum_battery is None or minimum_battery < config["reserve_fraction"]:
        reasons.append("return_battery_reserve_not_verified")
    urban_verification = None
    if config.get("urban"):
        from src.runtime.ship_urban_world import verify_urban_run

        urban_verification = verify_urban_run(samples, events, config["urban"])
        reasons.extend(urban_verification["reasons"])
    onboard_verification = None
    if (config.get("urban") or {}).get("onboard_camera"):
        if evidence_root is None:
            reasons.append("onboard_image_artifacts_not_checked")
        else:
            from src.runtime.ship_onboard import verify_onboard_artifacts

            try:
                onboard_verification = verify_onboard_artifacts(
                    evidence_root, config, samples, events
                )
                reasons.extend(onboard_verification["reasons"])
            except (ValueError, KeyError, TypeError, OSError) as exc:
                reasons.append(f"onboard_artifact_verification_failed:{exc}")
    vla_guard_verification = None
    if (config.get("urban") or {}).get("vla_guard_limits"):
        from src.runtime.ship_vla_adapter import verify_guard_artifacts

        try:
            if evidence_root is None:
                raise ValueError("vla_guard_artifacts_not_checked")
            vla_guard_verification = verify_guard_artifacts(evidence_root, config, samples, events)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reasons.append(f"vla_guard_verification_failed:{exc}")
    vla_execution_verification = None
    if (config.get("urban") or {}).get("vla_executor_contract"):
        from src.runtime.ship_vla_execution_verifier import verify_execution

        try:
            if evidence_root is None:
                raise ValueError("vla_execution_artifacts_not_checked")
            vla_execution_verification = verify_execution(evidence_root, config, samples, events)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reasons.append(f"vla_execution_verification_failed:{exc}")
    native_integration_verification = None
    if (config.get("urban") or {}).get("native_integration"):
        from src.runtime.ship_native_integration import verify_integration

        try:
            if evidence_root is None:
                raise ValueError("native_integration_artifacts_not_checked")
            native_integration_verification = verify_integration(
                evidence_root,
                config,
                samples,
                events,
                vla_execution_verification,
                onboard_verification,
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reasons.append(f"native_integration_verification_failed:{exc}")
    return {
        "verified": not reasons,
        "onboard_verification": onboard_verification,
        "native_integration_verification": native_integration_verification,
        "vla_guard_verification": vla_guard_verification,
        "vla_execution_verification": vla_execution_verification,
        "delivery_verified": delivery["verified"],
        "recovery_verified": recovered,
        "reasons": list(dict.fromkeys(reasons)),
        "entity_ids": delivery.get("entity_ids"),
        "recovery_stability_samples": len(tail),
        "minimum_simulated_battery_remaining": minimum_battery,
        "urban_verification": urban_verification,
    }


def run_ship_delivery_sitl(
    scenario: ShipDeliveryScenario,
    *,
    output_dir: Path,
    operator_approved: bool = False,
    timeout_s: float = 900,
    urban_case: str | None = None,
    urban_policy: str = "constant_velocity",
    capture_anwm: bool = False,
    vla_guard_smoke: bool = False,
    vla_executor_smoke: bool = False,
    aerovla_url: str | None = None,
    anwm_url: str | None = None,
) -> dict:
    """Run a fresh isolated simulation and always clean up only its container."""
    if operator_approved is not True:
        raise PermissionError("Explicit --approve-sitl is required before any simulator is started")
    if not 120 <= timeout_s <= 1800 or not math.isfinite(timeout_s):
        raise ValueError("timeout_s must be finite and between 120 and 1800")
    reasons = _preflight_reasons(scenario)
    if scenario.urban_blockage_s:
        reasons.append("SITL step 1 supports the static urban corridor only")
    if scenario.cruise_altitude_m < 25:
        reasons.append("SITL step 1 requires cruise_altitude_m >= 25 for airborne wind activation")
    if reasons:
        raise ValueError("; ".join(reasons))
    urban = None
    if urban_case is not None:
        from src.runtime.ship_urban_world import configure_urban

        urban = configure_urban(scenario, urban_case, urban_policy)
        from src.runtime.ship_onboard_model import MODEL_POLICIES, verify_local_model

        if urban_policy in MODEL_POLICIES:
            if scenario.airspeed_mps != 12:
                raise ValueError("The frozen local model comparison requires 12 m/s cruise")
            verify_local_model()
    if anwm_url:
        from src.runtime.ship_anwm_static import check_service, POLICY

        if not urban or urban_policy != POLICY or "stationary_obstacle_east_m" not in urban:
            raise ValueError("Live ANWM requires its static policy and stationary urban case")
        urban["anwm_static"] = check_service(anwm_url)
        capture_anwm = True
    elif urban_policy == "onboard_anwm_static":
        raise ValueError("Native ANWM policy requires explicit --anwm-url")
    if capture_anwm:
        if not urban or not urban.get("onboard_camera"):
            raise ValueError("ANWM capture requires an onboard urban policy")
        urban["capture_anwm"] = True
    if aerovla_url and (vla_executor_smoke or (capture_anwm and not anwm_url)):
        raise ValueError(
            "Live AeroVLA, fixture execution and historical capture are separate modes"
        )
    if aerovla_url:
        from src.runtime.ship_aerovla_host import check_service

        if not urban or not urban.get("onboard_camera"):
            raise ValueError("Live AeroVLA requires an onboard urban policy")
        urban["aerovla_live"] = check_service(aerovla_url)
        if anwm_url:
            from src.runtime.ship_native_integration import CONTRACT

            urban["native_integration"] = dict(CONTRACT)
    vla_execution = bool(vla_executor_smoke or aerovla_url)
    if vla_execution:
        from src.runtime.ship_vla_execution import executor_contract

        if not urban or not urban.get("onboard_camera"):
            raise ValueError("VLA executor smoke requires an onboard urban policy")
        urban["vla_executor_contract"] = executor_contract(native=bool(aerovla_url))
        vla_guard_smoke = True
    if vla_guard_smoke:
        from src.runtime.ship_vla_adapter import entry_limits

        if not urban or not urban.get("onboard_camera"):
            raise ValueError("VLA guard smoke requires an onboard urban policy")
        urban["vla_guard_limits"] = entry_limits(urban)
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    container = "missionos-ship-" + run_id[:12]
    model_root = root / "models"
    model_root.mkdir()
    missions = build_ship_sitl_missions(scenario)
    if urban:
        from src.runtime.ship_urban_world import urbanize_missions

        urbanize_missions(missions, urban)
    result = {
        "schema_version": "ship_delivery_sitl_run.v1",
        "execution_backend": "px4_gazebo_sitl",
        "status": "blocked",
        "run_id": run_id,
        "px4_runtime_invoked": False,
        "gazebo_runtime_invoked": False,
        "physical_execution_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "simulation_mission_completed": False,
        "delivery_verified": False,
        "recovery_verified": False,
        "blocking_reasons": [],
        "container": container,
    }
    worker = None
    worker_started_at = None
    stdout = stderr = None
    config = None
    try:
        image_id = _run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]).stdout.strip()
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-v",
                f"{model_root}:/out",
                image_id,
                "-c",
                "mkdir -p /out/worlds\n"
                "for model in x500 x500_base; do\n"
                "mkdir -p /out/$model\n"
                "cp /opt/px4-gazebo/share/gz/models/$model/model.sdf /out/$model/\n"
                "cp /opt/px4-gazebo/share/gz/models/$model/model.config /out/$model/\n"
                "if [ -d /opt/px4-gazebo/share/gz/models/$model/meshes ]; then\n"
                "ln -s /opt/px4-gazebo/share/gz/models/$model/meshes /out/$model/meshes\n"
                "fi\ndone\n"
                "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /out/worlds/",
            ],
            timeout=90,
        )
        world = prepare_ship_world(model_root, scenario)
        if urban:
            from src.runtime.ship_urban_world import urbanize_world

            urbanize_world(model_root, urban)
            world["urban_experiment"] = urban
            if urban.get("onboard_camera"):
                from src.runtime.ship_onboard import add_onboard_camera

                add_onboard_camera(model_root, urban)
                sources = [
                    Path(__file__),
                    Path(__file__).with_name("ship_onboard.py"),
                    Path(__file__).with_name("ship_onboard_model.py"),
                    Path(__file__).with_name("ship_onboard_uncertainty.py"),
                    Path(__file__).with_name("ship_urban_world.py"),
                    Path(__file__).with_name("ship_urban_decision.py"),
                    *[
                        Path(__file__).resolve().parents[2] / "scripts" / name
                        for name in (
                            "ship_delivery_sitl_worker.py",
                            "ship_urban_sitl_stage.py",
                            "ship_onboard_camera.py",
                            "ship_anwm_capture.py",
                            "ship_onboard_entrypoint.sh",
                            "ship_urban_camera_worker.py",
                        )
                    ],
                ]
                source_root = root / "sources"
                if vla_guard_smoke:
                    sources.append(Path(__file__).with_name("ship_vla_adapter.py"))
                if anwm_url:
                    sources.append(Path(__file__).with_name("ship_anwm_static.py"))
                    sources.extend(
                        Path(__file__).resolve().parents[2] / "scripts" / name
                        for name in ("ship_anwm_server.py", "ship_anwm.py")
                    )
                if aerovla_url:
                    sources.extend(
                        Path(__file__).with_name(name)
                        for name in ("ship_aerovla_host.py", "ship_aerovla_live.py")
                    )
                    sources.extend(
                        Path(__file__).resolve().parents[2] / "scripts" / name
                        for name in ("ship_aerovla_server.py", "ship_aerovla.py", "ship_anwm.py")
                    )
                if vla_execution:
                    sources.extend(
                        Path(__file__).with_name(name)
                        for name in ("ship_vla_execution.py", "ship_vla_execution_verifier.py")
                    )
                    sources.extend(
                        Path(__file__).resolve().parents[2] / "scripts" / name
                        for name in (
                            "ship_vla_executor.py",
                            "ship_vla_mavlink.py",
                            "smoke_px4_gazebo_sitl_mission_upload.py",
                        )
                    )
                if urban.get("native_integration"):
                    sources.append(Path(__file__).with_name("ship_native_integration.py"))
                source_root.mkdir()
                urban["onboard_source_sha256"] = {}
                for source in sources:
                    shutil.copyfile(source, source_root / source.name)
                    urban["onboard_source_sha256"][source.name] = hashlib.sha256(
                        source.read_bytes()
                    ).hexdigest()
        hashes = {
            str(p.relative_to(model_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(model_root.rglob("*.sdf"))
        }
        config = {
            "run_id": run_id,
            "world_sha256": canonical_sha256(hashes),
            "plan_sha256": canonical_sha256(
                {
                    "scenario": scenario.model_dump(),
                    "missions": missions,
                    "world": hashes,
                    "urban": urban,
                }
            ),
            "goal_north_m": missions["goal_north_m"],
            "airspeed_mps": scenario.airspeed_mps,
            "wind_mps": scenario.wind_mps,
            "timeout_s": timeout_s,
            "reserve_fraction": scenario.reserve_wh / scenario.battery_wh,
            "operator_approval_ref": "operator:explicit-sitl-opt-in",
            "execution_scope": "sim",
            "urban": urban,
        }
        result.update(
            world=world,
            world_files_sha256=hashes,
            config=config,
            image_id=image_id,
            scenario_parameters=scenario.model_dump(exclude={"backend"}),
            missions=missions,
        )
        (root / "config.json").write_text(json.dumps(config, indent=2))
        from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script

        mission_names = (
            ("outbound", "return", "urban-wait", "urban-detour")
            if urban
            else ("outbound", "return")
        )
        for name in mission_names:
            (root / f"{name}-upload.py").write_text(
                _inner_upload_script(
                    missions[name], reuse_mavlink_session=vla_execution and name != "outbound"
                )
            )
        shutil.copyfile(
            Path(__file__).resolve().parents[2] / "scripts/ship_delivery_sitl_worker.py",
            root / "worker.py",
        )
        if urban:
            shutil.copyfile(
                Path(__file__).with_name("ship_urban_decision.py"), root / "urban_policy.py"
            )
            shutil.copyfile(
                Path(__file__).resolve().parents[2] / "scripts/ship_urban_sitl_stage.py",
                root / "ship_urban_sitl_stage.py",
            )
        camera_args = []
        if aerovla_url:
            shutil.copyfile(
                Path(__file__).with_name("ship_aerovla_live.py"), root / "ship_aerovla_live.py"
            )
        if vla_execution:
            shutil.copyfile(
                Path(__file__).with_name("ship_vla_execution.py"), root / "ship_vla_execution.py"
            )
            for name in ("ship_vla_executor.py", "ship_vla_mavlink.py"):
                shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts" / name, root / name)
        if vla_guard_smoke:
            shutil.copyfile(
                Path(__file__).with_name("ship_vla_adapter.py"), root / "ship_vla_adapter.py"
            )
        if urban and urban.get("onboard_camera"):
            for name in (
                "ship_onboard_camera.py",
                "ship_anwm_capture.py",
                "ship_urban_camera_worker.py",
                "ship_onboard_entrypoint.sh",
            ):
                shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts" / name, root / name)
            camera_args = [
                "--network",
                "none",
                "--entrypoint",
                "/bin/sh",
                "-e",
                "LIBGL_ALWAYS_SOFTWARE=1",
            ]
        _run(
            [
                "docker",
                "run",
                "-d",
                *camera_args,
                "--name",
                container,
                "-v",
                f"{root}:/mission",
                "-e",
                "PX4_GZ_MODELS=/mission/models",
                "-e",
                "PX4_GZ_WORLDS=/mission/models/worlds",
                "-e",
                "PX4_GZ_WORLD=default",
                "-e",
                "GZ_SIM_RESOURCE_PATH=/mission/models:/opt/px4-gazebo/share/gz/models",
                "-e",
                "PX4_SIM_MODEL=gz_x500",
                "-e",
                "HEADLESS=1",
                "-e",
                "PX4_GZ_NO_FOLLOW=1",
                "-e",
                "PX4_HOME_LAT=35.3195",
                "-e",
                "PX4_HOME_LON=138.7435",
                "-e",
                "PX4_HOME_ALT=0",
                image_id,
                *(["/mission/ship_onboard_entrypoint.sh"] if camera_args else []),
                "-d",
            ],
            timeout=90,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            logs = _run(["docker", "logs", container]).stdout
            if (
                "Startup script returned successfully" in logs
                and "gz_bridge] world: default, model: x500_0" in logs
            ):
                result.update(px4_runtime_invoked=True, gazebo_runtime_invoked=True)
                break
            time.sleep(1)
        else:
            raise TimeoutError("PX4/Gazebo startup timed out")
        stdout, stderr = (root / "worker.stdout").open("w"), (root / "worker.stderr").open("w")
        worker_started_at = datetime.now(timezone.utc).isoformat()
        worker = subprocess.Popen(
            ["docker", "exec", container, "python3", "-u", "/mission/worker.py"],
            stdout=stdout,
            stderr=stderr,
        )
        deadline = time.monotonic() + timeout_s + 60
        permitted = False
        next_housekeeping = 0.0
        while worker.poll() is None:
            if time.monotonic() > deadline:
                raise TimeoutError("host watchdog expired")
            housekeeping = time.monotonic() >= next_housekeeping
            if housekeeping:
                next_housekeeping = time.monotonic() + 0.5
            if housekeeping and urban and urban.get("onboard_camera"):
                from src.runtime.ship_onboard import process_vision_request

                process_vision_request(root, config, anwm_url=anwm_url)
            if aerovla_url:
                from src.runtime.ship_aerovla_host import process_native_request

                process_native_request(root, config, aerovla_url)
            if not permitted and housekeeping:
                samples = _read_jsonl(root / "telemetry.jsonl")
                verification = verify_ship_sitl_delivery(samples, config)
                if verification["verified"]:
                    permit = _return_permit(config, samples)
                    temporary = root / "return-authorized.tmp"
                    temporary.write_text(json.dumps(permit))
                    temporary.replace(root / "return-authorized.json")
                    permitted = True
            time.sleep(0.02 if aerovla_url else 0.5)
        if worker.returncode:
            result["blocking_reasons"].append(
                "SITL worker failed; see worker.stderr and events.jsonl"
            )
        verification = verify_ship_sitl_run(
            _read_jsonl(root / "telemetry.jsonl"),
            _read_jsonl(root / "events.jsonl"),
            config,
            evidence_root=root,
        )
        result.update(
            verification=verification,
            delivery_verified=verification["delivery_verified"],
            recovery_verified=verification["recovery_verified"],
        )
        result["blocking_reasons"].extend(verification["reasons"])
        completed = not result["blocking_reasons"] and permitted
        result.update(
            status="completed" if completed else "blocked", simulation_mission_completed=completed
        )
    except Exception as exc:
        result["blocking_reasons"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        if stdout:
            stdout.close()
            stderr.close()
        if worker is not None:
            evidence = {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "docker_exec",
                "invocation_target": f"{container}:python3 /mission/worker.py",
                "invocation_started_at": worker_started_at,
                "invocation_completed_at": datetime.now(timezone.utc).isoformat(),
                "invocation_exit_code": worker.returncode,
                "run_id": run_id,
                "execution_scope": "sim",
                "worker_sha256": hashlib.sha256((root / "worker.py").read_bytes()).hexdigest(),
            }
            for stream in ("stdout", "stderr"):
                artifact = root / f"worker.{stream}"
                evidence[f"{stream}_artifact_path"] = str(artifact)
                evidence[f"invocation_{stream}_sha256"] = hashlib.sha256(
                    artifact.read_bytes()
                ).hexdigest()
            result["runtime_invocation_evidence"] = validate_runtime_invocation_evidence(evidence)
        try:
            logs = _run(["docker", "logs", container], check=False, timeout=15)
            (root / "simulator.log").write_text(logs.stdout + logs.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["log_capture_error"] = str(exc)
        try:
            cleanup = _run(["docker", "rm", "-f", container], check=False, timeout=20)
            result["container_removed"] = cleanup.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["container_removed"] = False
            result["cleanup_error"] = str(exc)
        invocation = root / "model-invocation.json"
        if invocation.exists():
            result["vision_model_invocation"] = json.loads(invocation.read_text())
            result["vision_language_model_invoked"] = True
        if (root / "anwm-http-response.json").exists():
            result["wam_invoked"] = (
                json.loads((root / "anwm-http-response.json").read_text()).get(
                    "wam_inference_invoked"
                )
                is True
            )
        native_response = root / "aerovla-response.json"
        if native_response.exists():
            native_receipt = json.loads(native_response.read_text())
            result["aerovla_invocation_attempted"] = (
                native_receipt.get("invocation_attempted") is True
            )
            result["vla_invoked"] = (
                native_receipt.get("response", {}).get("vla_inference_invoked") is True
            )
        result["limitations"] = [
            "Single stationary synthetic ship, 50g cargo; no marine hydrodynamics.",
            "PX4 simulated battery is time-based, not an airframe endurance validation.",
            "Wind is applied after takeoff at 20m with uncalibrated Gazebo force scaling 0.05.",
            "VLA/WAM, moving-deck landing, multi-drone coordination and physical hardware remain outside Step 1.",
        ]
        if urban:
            result["limitations"].extend(
                [
                    "Known-colour image segmentation and mapped obstacle depth with PX4 ego pose; privileged Gazebo poses used by the independent verifier."
                    if urban.get("onboard_camera")
                    else "Scripted collision box and privileged Gazebo position history; not camera perception.",
                    "Constant-velocity cases establish action divergence, not learned-model value.",
                ]
            )
        (root / "result.json").write_text(json.dumps(result, indent=2))
    return result
