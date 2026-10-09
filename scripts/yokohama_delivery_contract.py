"""Explicit full-delivery scope; the prior inland-only contract stays unchanged."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

CONTRACT = {
    "schema": "yokohama.delivery-feedback-trial.v1",
    "timeout_s": 2100,
    "recovery_timeout_s": 500,
    "recovery_trigger_margin_s": 5,
    "maximum_flight_attempts": 1,
    "physical_execution_allowed": False,
    "model_control_scope": "two inland feedback legs; AP carries the authored delivery route",
}
SCENARIOS = ("delivery", "wait", "candidate_rejected", "timeout")
STAGES = (
    "SEA-TAKEOFF", "SEA-INBOUND-COAST", "00-D1", "01-FEEDBACK-EXIT",
    "01-D2", "02-D3", "03-DELIVERY", "PAYLOAD-LOW", "PAYLOAD-CLIMB",
    "04-D3", "05-D2", "06-D1", "SEA-OUTBOUND-COAST", "SEA-RETURN",
)


def authored_digest(config):
    """Separate immutable plan binding catches shared waypoint-list mutation."""
    world = config["world"]
    value = dict(points=world["points"], frame=world.get("frame"),
                 scene_sources=world["source_sha256"], sea=world["sea_extension"],
                 payload=world["payload_delivery"], stages=config["flight_stages"])
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def inland_proxy(config):
    """Validate the identical four-metre model envelope inside the larger world."""
    proxy = deepcopy(config)
    proxy.pop("delivery_trial", None)
    proxy.pop("delivery_scenario", None)
    proxy.pop("delivery_recovery", None)
    for key in ("sea_extension", "payload_delivery", "pad_queue"):
        proxy["world"].pop(key, None)
    proxy["decisions"].update(sea_leg_present=False, payload_release_present=False)
    proxy["flight_stages"] = [s for s in proxy["flight_stages"]
                              if s["name"] in {"00-D1", "01-FEEDBACK-EXIT"}]
    return proxy


def build_config(base, scenario):
    from scripts.yokohama_sitl import build_endpoint_feedback_config
    from scripts.yokohama_goal_distance_adapter import POLICY

    if scenario not in SCENARIOS:
        raise ValueError("Unknown delivery scenario")
    config = deepcopy(base)
    proxy = inland_proxy(config)
    proxy["flight_stages"] = [s for s in proxy["flight_stages"] if s["name"] == "00-D1"]
    proxy["decisions"]["points"] = ["D1"]
    feedback = build_endpoint_feedback_config(proxy)
    config["flight_stages"].insert(3, feedback["flight_stages"][1])
    config["decisions"].update(
        points=["D1"], size_bound_origin="proposal_observation",
        endpoint_feedback=feedback["decisions"]["endpoint_feedback"],
        goal_distance_adapter=deepcopy(POLICY),
    )
    config["delivery_trial"] = deepcopy(CONTRACT)
    config["delivery_scenario"] = scenario
    config["hold_horizontal_tolerance_m"] = 0.25
    config["delivery_authored_plan_sha256"] = authored_digest(config)
    validate_config(config)
    return config


def validate_config(config):
    from_scripts = config.get("decisions", {})
    scenario = config.get("delivery_scenario")
    world = config.get("world", {})
    sea = world.get("sea_extension", {})
    if (
        config.get("delivery_trial") != CONTRACT or scenario not in SCENARIOS
        or config.get("timeout_s") != CONTRACT["timeout_s"]
        or not config.get("operator_approval_manifest_sha256")
        or config.get("endpoint_adapter_trial") is not None
        or config.get("candidate_recovery") is not None
        or config.get("delivery_authored_plan_sha256") != authored_digest(config)
        or config.get("fixture_reject_second_candidate", False) is not False
        or from_scripts.get("backend") not in {"fixture", "native"}
        or (scenario != "delivery" and from_scripts.get("backend") != "fixture")
        or from_scripts.get("sea_leg_present") is not True
        or from_scripts.get("payload_release_present") is not True
        or from_scripts.get("fixture_delay_s") != {}
        or not world.get("payload_delivery")
        or world["payload_delivery"].get("attachment_offset_z_m") != -0.14
        or world["payload_delivery"].get("removable_support_entity") != "delivery_cargo_support"
        or bool(world.get("pad_queue")) != (scenario == "wait")
        or sea.get("stationary_ship") is not True or sea.get("wind_mps") != 0
        or sea.get("offshore_distance_m") != 1000 or sea.get("coast_to_city_entry_m") != 400
        or sea.get("sea_airspeed_mps") != 8 or sea.get("city_airspeed_mps") != 3
        or [s.get("name") for s in config.get("flight_stages", [])] != list(STAGES)
        or [config.get(k) for k in ("hold_duration_sim_s", "hold_horizontal_tolerance_m",
                                   "hold_vertical_tolerance_m", "hold_max_speed_mps")]
        != [30, 0.25, 0.6, 0.5]
    ):
        raise ValueError("Unapproved full-delivery scope, route, fault or hold policy")
    if __package__:
        from .yokohama_endpoint_feedback import feedback_policy
        from .yokohama_goal_distance_adapter import POLICY
    else:
        from yokohama_endpoint_feedback import feedback_policy
        from yokohama_goal_distance_adapter import POLICY
    if from_scripts.get("goal_distance_adapter") != POLICY:
        raise ValueError("Exact vehicle distance adapter required")
    policy = feedback_policy(inland_proxy(config))
    points = {p["id"]: p["world_xyz_m"] for p in world["points"]}
    targets = [sea["ship_hold_world_xyz_m"], sea["coast_world_xyz_m"], points["D1"],
               points["D1"], points["D2"], points["D3"], points["DELIVERY"],
               world["payload_delivery"]["hover_world_xyz_m"], points["DELIVERY"],
               points["D3"], points["D2"], points["D1"], sea["coast_world_xyz_m"],
               sea["ship_hold_world_xyz_m"]]
    if any(s["target_world_xyz_m"] != target for s, target in zip(config["flight_stages"], targets)):
        raise ValueError("Authored delivery waypoint changed")
    entry, coast, ship = points["D1"], targets[1], targets[0]
    if (
        not math.isclose(math.dist(entry, coast), 400, abs_tol=1e-8)
        or not math.isclose(math.dist(coast, ship), 1000, abs_tol=1e-8)
        or any(abs((coast[i] - entry[i]) * 3.5 - (ship[i] - entry[i])) > 1e-8
               for i in range(3))
        or sea["ship_deck_world_xyz_m"] != [*ship[:2], 0]
        or any(s.get("airspeed_mps") != (8 if s["name"].startswith("SEA-") else 3)
               for s in config["flight_stages"])
    ):
        raise ValueError("Authored stationary ship connector changed")
    return policy


def arguments(models, image, scenario="delivery", service=None):
    from scripts.yokohama_local_preflight import image_id
    if scenario not in SCENARIOS or models not in {"fixture", "native"}:
        raise ValueError("Unsupported delivery backend/scenario")
    if (service is not None) != (models == "native") or (models == "native" and scenario != "delivery"):
        raise ValueError("Fault/wait scenarios are CPU-only; native needs an explicit service")
    values = ["--phase", "flight", "--delivery-trial", scenario, "--sea-round-trip",
              "--deliver-payload", "--goal-distance-adapter", "--decision-backend", models,
              "--wam-profile", "motion-v4", "--timeout-seconds", str(CONTRACT["timeout_s"]),
              "--local-image-id", image_id(image)]
    if scenario == "wait":
        values.append("--occupied-pad")
    if service is not None:
        values += ["--native-service-config", str(Path(service).resolve())]
    return values


def qualify_return_map(config, asset):
    """Qualify both the narrow model corridor and the original offshore line."""
    import numpy as np
    from shapely.geometry import LineString, shape
    from src.runtime.yokohama_scene import to_source
    policy = validate_config(config)
    raw = Path(asset).read_bytes()
    if hashlib.sha256(raw).hexdigest() != policy["map_sha256"]:
        raise ValueError("Recovery map changed")
    sea = config["world"]["sea_extension"]
    source = to_source(np.array([policy["goal_world_xyz_m"], policy["entry_world_xyz_m"],
                                 sea["coast_world_xyz_m"], sea["ship_hold_world_xyz_m"]]),
                       config["world"]["frame"])
    scale = float(np.linalg.norm(np.linalg.inv(np.array(
        config["world"]["frame"]["source_to_world_matrix"])), ord=2))
    lines = [LineString([a[:2], b[:2]]) for a, b in zip(source, source[1:])]
    shapes = [shape(f["geometry"]) for f in json.loads(raw)["features"]
              if f["properties"]["zmax"] >= min(source[:, 2]) - 2
              and f["properties"]["zmin"] <= max(source[:, 2]) + 2]
    minimum = min(line.distance(poly) for line in lines for poly in shapes)
    required = 3.5 * scale  # identical 0.5 corridor + 1 tracking + 2 clearance
    if minimum <= required:
        raise ValueError("Authored ship-return corridor lacks mapped clearance")
    return dict(map_sha256=policy["map_sha256"], minimum_clearance_m=minimum,
                required_centerline_clearance_m=required,
                scope="Mapped buildings only; offshore geometry is authored, not surveyed")


def admit(raw, root, supplied):
    from scripts.yokohama_endpoint_feedback import FIXED
    from scripts.yokohama_goal_distance_adapter import POLICY
    from src.runtime.yokohama_execution_service import proposal_digest, recovery_input_hashes
    manifest = json.loads(raw)
    proposal, approval = manifest["proposal"], manifest["approval"]
    service = proposal.get("native_service_config")
    if (
        proposal.get("schema") != "yokohama.delivery-feedback-proposal.v1"
        or proposal.get("contract") != CONTRACT or proposal.get("feedback_limits") != FIXED
        or proposal.get("adapter_policy") != POLICY
        or proposal.get("physical_execution_invoked") is not False
        or proposal.get("simulator_arguments") != arguments(
            proposal.get("city_models"), proposal.get("image_id"), proposal.get("scenario"), service)
        or supplied != proposal["simulator_arguments"]
        or proposal.get("input_sha256") != recovery_input_hashes(Path(root))
        or approval.get("approved_proposal_sha256") != proposal_digest(proposal)
        or approval.get("maximum_actual_flight_trials") != 1
        or not all(approval.get(k) for k in ("operator_approval_ref", "actor_session_id", "approved_at"))
    ):
        raise ValueError("Full-delivery approval/source/catalog mismatch")
    service_bytes = Path(service).read_bytes() if service is not None else None
    expected = hashlib.sha256(service_bytes).hexdigest() if service_bytes is not None else None
    if proposal.get("native_service_config_sha256") != expected:
        raise ValueError("Native delivery service changed since approval")
    return manifest, service_bytes
