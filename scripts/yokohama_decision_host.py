"""Host side of an opt-in, per-cycle city decision mailbox.

Only the host has model HTTP access. The simulator stays network-isolated.
Native lifecycle commands are supplied explicitly; this module allocates no VM.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
from uuid import uuid4

import numpy as np
from PIL import Image

from scripts import ship_anwm
from scripts.yokohama_altitude_contract import (
    SCHEMA,
    compile_mission,
    recheck_mapping,
)
from scripts.yokohama_wam_profile import MOTION_CONTRACT, validate_service_profile
from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script
from src.runtime.yokohama_native import (
    camera_heading,
    digest,
    forecast_consistency,
    executor_heading,
    executor_altitude,
    geometry_rules,
    load_capture,
    pad_structure_consistency,
    past_view,
    read_asset,
    vla_candidate,
    write_request,
)


def exchange(port, path, payload=None, timeout=75):
    data = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{path}", data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(24_000_001)
    if len(raw) > 24_000_000:
        raise ValueError("Oversized model response")
    return json.loads(raw)


def validate_native_services(config, identities, source_hashes):
    """Check pinned service contracts without reading files or contacting services."""
    if set(identities) != {"vla", "wam"} or not all(
        isinstance(identity, dict) for identity in identities.values()
    ):
        raise ValueError("Both native service identities are required")
    source_names = (
        "yokohama_appearance.py", "yokohama_wam_profile.py", "ship_anwm_server.py",
        "ship_anwm.py", "ship_aerovla_server.py", "ship_aerovla.py",
    )
    if any(
        not isinstance(source_hashes.get(name), str)
        or len(source_hashes[name]) != 64
        or any(c not in "0123456789abcdef" for c in source_hashes[name])
        for name in source_names
    ):
        raise ValueError("Frozen native source hashes are required")
    if any(v.get("cpu_between_requests") is not True for v in identities.values()):
        raise ValueError("Serial CPU-between-requests residency required")
    if identities["vla"].get("exit_after_request") is not False:
        raise ValueError("One-shot VLA cannot serve a repeated city session")
    wam_profile = config["decisions"].get("wam_profile", "legacy")
    compact = wam_profile == "motion-v4"
    if (
        identities["vla"].get("short_segment_flight") is not True
        or identities["vla"].get("yaw_bin_range") != ([45, 53] if compact else [38, 60])
        or (
            compact
            and (
                identities["vla"].get("compact_city_flight") is not True
                or identities["vla"].get("forward_bin_range") != [20, 58]
                or identities["vla"].get("hold_bin_allowed") is not False
                or identities["vla"].get("terminal_proposal_allowed") is not False
                or identities["vla"].get("decoding_policy")
                != "aerovla_compact_city_grammar.v2"
            )
        )
    ):
        raise ValueError("City translation phase requires bounded native decoding")
    contract = MOTION_CONTRACT if compact else "yokohama_anwm_request.v1"
    if contract not in identities["wam"].get("candidate_contracts", []):
        raise ValueError("Native WAM does not support this action contract")
    validate_service_profile(
        identities["wam"],
        wam_profile,
        appearance_sha256=source_hashes["yokohama_appearance.py"],
        profile_sha256=source_hashes["yokohama_wam_profile.py"],
    )
    if (
        identities["wam"].get("server_sha256") != source_hashes["ship_anwm_server.py"]
        or identities["wam"].get("helper_sha256") != source_hashes["ship_anwm.py"]
        or identities["wam"].get("checkpoint_sha256") != ship_anwm.MODEL_SHA256
        or identities["wam"].get("upstream_revision") != ship_anwm.UPSTREAM_REVISION
        or any(
            identities["vla"].get("runtime_sha256", {}).get(name) != source_hashes[name]
            for name in ("ship_aerovla_server.py", "ship_aerovla.py", "ship_anwm.py")
        )
    ):
        raise ValueError("Native services differ from the frozen source/checkpoint")


class DecisionHost:
    def __init__(
        self, root, config, bundle, backend, service_config=None, *,
        detached=False, capture_root=None, http_exchange=None, mock_http=False,
    ):
        if mock_http and not detached:
            raise ValueError("Mock HTTP requires a detached shadow host")
        self.root, self.config, self.bundle = Path(root), config, Path(bundle)
        self.capture_root = self.root if capture_root is None else Path(capture_root)
        self.detached, self.mock_http = detached, mock_http
        self.http_exchange = http_exchange
        self.attachment_attempted = False
        self.backend, self.service_config = backend, service_config
        self.identity, self.pending = None, {}
        self.attempts = {}
        self.active, self.closed = False, False
        self.stop_receipt = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.folder = self.root / "decisions"
        self.folder.mkdir()
        self.process = None
        self.thread = None
        if not detached:
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()

    def _exchange(self, *args, **kwargs):
        return (getattr(self, "http_exchange", None) or exchange)(*args, **kwargs)

    def lifecycle(self, operation):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host does not own service lifecycle")
        argv = self.service_config[operation + "_argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
            raise ValueError("Lifecycle requires explicit argv")
        with (
            (self.folder / (operation + ".stdout")).open("w") as out,
            (self.folder / (operation + ".stderr")).open("w") as err,
        ):
            self.process = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            self.process.wait(
                timeout=self.config["decisions"].get("startup_timeout_s", 180)
                if operation == "start"
                else 60
            )
        if self.process.returncode:
            raise RuntimeError("Model lifecycle failed: " + operation)
        receipt = json.loads((self.folder / (operation + ".stdout")).read_text().splitlines()[-1])
        if operation == "stop" and receipt.get("remote_model_processes_absent") is not True:
            raise ValueError("Remote model termination unverified")
        return receipt

    def start(self):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host must attach existing services")
        if self.active or self.closed:
            raise ValueError("Model session is single use")
        if self.backend == "native":
            receipt = self.lifecycle("start")
            identities = {
                name: self._exchange(self.service_config[name + "_port"], "health")
                for name in ("vla", "wam")
            }
            sources = self.root / "sources"
            validate_native_services(self.config, identities, {
                name: ship_anwm.digest(sources / name)
                for name in (
                    "yokohama_appearance.py", "yokohama_wam_profile.py", "ship_anwm_server.py",
                    "ship_anwm.py", "ship_aerovla_server.py", "ship_aerovla.py",
                )
            })
            identity = dict(backend="native", services=identities, lifecycle=receipt)
        else:
            identity = dict(backend="fixture", models_invoked=False)
        self.identity, self.active = identity, True
        return identity

    def attach_existing_services(self, *, expected_services, source_hashes):
        """Attach once to explicitly pinned services without owning their lifecycle."""
        if not getattr(self, "detached", False) or self.backend != "native":
            raise ValueError("Attaching requires a detached native-protocol host")
        if self.active or self.closed or self.attachment_attempted:
            raise ValueError("Model attachment is single use")
        self.attachment_attempted = True
        expected = copy.deepcopy(expected_services)
        validate_native_services(self.config, expected, source_hashes)
        identities = {
            name: self._exchange(self.service_config[name + "_port"], "health")
            for name in ("vla", "wam")
        }
        if identities != expected:
            raise ValueError("Existing service identity differs from the approved identity")
        if self.mock_http:
            if any(v.get("fixture") is not True for v in identities.values()):
                raise ValueError("Mock HTTP requires explicit fixture service identities")
        elif any(v.get("fixture") is True for v in identities.values()):
            raise ValueError("Native attachment rejects fixture service identities")
        validate_native_services(self.config, identities, source_hashes)
        self.identity = dict(
            backend="native", services=copy.deepcopy(identities),
            externally_owned_services=True, mock_http=self.mock_http,
        )
        self.active = True
        return copy.deepcopy(self.identity)

    def stop(self):
        if self.closed and self.stop_receipt is not None:
            return self.stop_receipt
        self.active, self.closed = False, True
        self.pending.clear()
        receipt = (
            dict(remote_cleanup_verified=False, externally_owned_services=True)
            if getattr(self, "detached", False)
            else self.lifecycle("stop") if self.backend == "native" else {"fixture_stopped": True}
        )
        self.stop_receipt = dict(receipt, session_revoked=True)
        (self.folder / "shutdown.json").write_text(json.dumps(self.stop_receipt, indent=2) + "\n")
        return self.stop_receipt

    def pad_cycle(self, cycle):
        from scripts.yokohama_decision_worker import decision_phases

        return decision_phases(self.config).get(cycle) == "02-D3"

    def require_pad_approach(self, row, target=None):
        """Host Rules for the D3 model step: current clear pad and approach corridor."""
        from src.runtime.yokohama_pad_queue import clearance, segment_distance

        c = clearance(self.config, row)
        if not (c["pad_clear"] and c["approach_clear"]):
            raise ValueError("Pad or approach not clear for a model approach step")
        if target is not None:
            p = self.config["world"]["pad_queue"]
            wait, approach = p["wait_xyz_m"], p["approach_xyz_m"]
            limit = self.config["decisions"]["pad_approach"]["corridor_lateral_max_m"]
            if segment_distance(target, wait, approach) > limit or math.dist(
                target, approach
            ) >= math.dist(wait, approach):
                raise ValueError("Model approach step leaves the wait-to-approach corridor")
        return c

    def proposal_origin(self, proposal):
        if self.config["decisions"].get("size_bound_origin") == "proposal_observation":
            return proposal["input_observation"]["vehicle"]["xyz"]
        return None

    def capture(self, message):
        entry = message["capture"]
        capture_root = getattr(self, "capture_root", self.root)
        path = capture_root / entry["file"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(capture_root.resolve())
            or ship_anwm.digest(path) != entry["sha256"]
        ):
            raise ValueError("Unbound city capture")
        record, arrays = load_capture(
            path, appearance=self.config["decisions"].get("wam_profile") == "motion-v4"
        )
        if not 0 <= message["observation"]["sim_s"] - arrays["stamps_ns"][-1] / 1e9 <= 2:
            raise ValueError("History does not end at a fresh observation")
        return path, record, arrays

    def vla(self, message, output):
        if getattr(self, "detached", False) and (not self.active or self.closed):
            raise ValueError("Detached model session revoked or not attached")
        path, capture, _ = self.capture(message)
        if self.pad_cycle(message["cycle"]):
            self.require_pad_approach(message["observation"])
        last = capture["frames"][-1]
        images = {
            key: read_asset(path.parent, last["assets"][sensor + "_png"])
            for key, sensor in [("rgb", "onboard_rgb"), ("down", "down_rgb")]
        }
        row = message["observation"]
        delta = np.array(message["next_target_world_xyz_m"]) - row["vehicle"]["xyz"]
        angle = math.remainder(math.atan2(delta[0], delta[1]) - camera_heading(row), 2 * math.pi)
        if abs(angle) > math.pi / 3:
            raise ValueError("Approved next segment lies outside the forward inspection envelope")
        direction = (
            "straight ahead"
            if abs(angle) <= 0.26
            else "forward-right"
            if angle > 0
            else "forward-left"
        )
        request = dict(
            schema_version="yokohama_aerovla_request.v1",
            run_id=self.config["run_id"],
            world_sha256=self.config["world"]["world_sha256"],
            plan_sha256=digest(self.config),
            request_id=uuid4().hex,
            input_row_sha256=digest(row),
            observed_at_s=row["wall_s"],
            sensor_stamp_s=last["stamp_ns"] / 1e9,
            images_sha256={
                k: __import__("hashlib").sha256(v).hexdigest() for k, v in images.items()
            },
            prompt=f"<image>\nFly {direction} along the street toward the delivery pad. Maintain height.\nAction: ",
            direction_source="approved_goal_and_simulator_camera_pose",
            future_ground_truth_used=False,
            dispatch_allowed=False,
        )
        (output / "native-request.json").write_text(json.dumps(request, indent=2) + "\n")
        if self.backend == "native":
            response = self._exchange(
                self.service_config["vla_port"],
                "infer",
                {
                    "request": request,
                    "images_base64": {k: base64.b64encode(v).decode() for k, v in images.items()},
                },
            )
            (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
            if (
                response.get("request_sha256") != digest(request)
                or response.get("service") != self.identity["services"]["vla"]
                or response.get("vla_inference_invoked") is not True
                or response.get("input_images_sha256") != request["images_sha256"]
                or response.get("dispatch_invoked") is not False
                or response.get("physical_execution_invoked") is not False
                or response.get("cuda_allocated_after_request_bytes") != 0
                or response.get("pixel_values_shape") != [1, 6, 224, 224]
                or not response.get("generated_token_ids")
            ):
                raise ValueError("Unbound native VLA result")
        else:
            response = dict(generated_text="55 49 49", fixture=True, vla_inference_invoked=False)
        (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
        candidate = vla_candidate(response["generated_text"], row)
        rules = geometry_rules(
            row["vehicle"]["xyz"],
            candidate["target_world_xyz_m"],
            message["next_target_world_xyz_m"],
            self.config,
            self.bundle,
        )
        result = dict(
            candidate=candidate,
            vla_response_sha256=digest(response),
            input_observation=row,
            preliminary_rules=rules,
            native_vla_invoked=self.backend == "native" and not getattr(self, "mock_http", False),
        )
        self.pending[message["cycle"]] = result
        return result

    def wam(self, message, output):
        if getattr(self, "detached", False) and (not self.active or self.closed):
            raise ValueError("Detached model session revoked or not attached")
        proposal = self.pending[message["cycle"]]
        if message["vla"] != proposal:
            raise ValueError("Cross-cycle VLA proposal")
        _, _, arrays = self.capture(message)
        row, original = message["observation"], proposal["input_observation"]
        if (
            math.dist(row["vehicle"]["xyz"], original["vehicle"]["xyz"]) > 0.5
            or abs(
                math.remainder(row["heading_ned_rad"] - original["heading_ned_rad"], 2 * math.pi)
            )
            > 0.03
        ):
            raise ValueError("Vehicle moved since VLA observation")
        candidate = dict(proposal["candidate"])
        # Express the immutable VLA endpoint from this new camera observation.
        pose = arrays["poses"][-1]
        forward = np.r_[pose[:2, 2], 0.0]
        forward /= np.linalg.norm(forward)
        right = np.array([-forward[1], forward[0], 0.0])
        current_yaw = math.atan2(forward[1], forward[0])
        yaw_delta = math.remainder(
            candidate["target_heading_world_ned_rad"] - current_yaw, 2 * math.pi
        )
        vehicle_target = np.array(candidate["target_world_xyz_m"])[[1, 0, 2]] * [1, 1, -1]
        c, s = math.cos(yaw_delta), math.sin(yaw_delta)
        new_rotation = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ pose[:3, :3]
        target_camera = vehicle_target + new_rotation @ [0, -0.1, 0.25]
        camera_delta = target_camera - pose[:3, 3]
        candidate["delta_body_frd"] = [
            float(camera_delta @ forward),
            float(camera_delta @ right),
            float(camera_delta[2]),
            yaw_delta,
        ]
        request = write_request(
            output / "input", arrays, self.config, candidate, proposal["vla_response_sha256"]
        )
        if self.backend == "native":
            response = self._exchange(
                self.service_config["wam_port"],
                "infer",
                {
                    "request_id": uuid4().hex,
                    "request_base64": base64.b64encode(
                        (output / "input/request.json").read_bytes()
                    ).decode(),
                    "history_base64": base64.b64encode(
                        (output / "input/history.npz").read_bytes()
                    ).decode(),
                },
            )
            (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
            if (
                response.get("identity") != self.identity["services"]["wam"]
                or response.get("request_sha256") != ship_anwm.digest(output / "input/request.json")
                or response.get("history_sha256") != request["history_sha256"]
                or response.get("wam_inference_invoked")
                is not (not getattr(self, "mock_http", False))
                or (getattr(self, "mock_http", False) and response.get("fixture") is not True)
                or response.get("dispatch_allowed") is not False
                or response.get("cuda_allocated_after_request_bytes") != 0
            ):
                raise ValueError("Unbound native WAM result")
            (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
            forecasts = response["forecasts"]
            if [f["candidate"] for f in forecasts] != request["candidates"]:
                raise ValueError("Forecast candidate mismatch")
        checks = []
        pad = self.pad_cycle(message["cycle"])
        if pad:
            _, hold_mask = past_view(arrays, ship_anwm.action_pose(pose, [0, 0, 0, 0]))
        for index, item in enumerate(request["candidates"]):
            target = ship_anwm.action_pose(pose, item["delta"])
            reference, mask = past_view(arrays, target)
            Image.fromarray(reference).save(output / (item["id"] + "-past-reference.png"))
            if self.backend == "native":
                entry = forecasts[index]["files"]["prediction"]
                data = base64.b64decode(entry["png_base64"], validate=True)
                if __import__("hashlib").sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("Forecast image hash mismatch")
                image = Image.open(io.BytesIO(data))
                if image.mode != "RGB" or image.size != (224, 224):
                    raise ValueError("Unexpected native forecast image")
                predicted = np.array(image)
                # Preserve the actual native PNG bytes, not a host re-encoding.
                (output / (item["id"] + "-prediction.png")).write_bytes(data)
            else:
                predicted = reference.copy()
                Image.fromarray(predicted).save(output / (item["id"] + "-prediction.png"))
            check = (
                pad_structure_consistency(predicted, reference, mask, hold_mask)
                if pad
                else forecast_consistency(predicted, reference, mask)
            )
            checks.append(dict(candidate=item, **check))
        result = dict(
            checks=checks,
            passed=all(c["passed"] for c in checks),
            vla_response_sha256=proposal["vla_response_sha256"],
            native_wam_invoked=self.backend == "native" and not getattr(self, "mock_http", False),
        )
        proposal["wam"] = result
        return result

    def authorize(self, message, output):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host cannot authorize dispatch")
        proposal = self.pending[message["cycle"]]
        if not proposal.get("wam", {}).get("passed"):
            raise ValueError("WAM prediction did not meet visible-structure bounds")
        row, old = message["observation"], proposal["input_observation"]
        if (
            math.dist(row["vehicle"]["xyz"], old["vehicle"]["xyz"]) > 0.5
            or abs(math.remainder(row["heading_ned_rad"] - old["heading_ned_rad"], 2 * math.pi))
            > 0.03
        ):
            raise ValueError("Stationary prediction contract expired")
        candidate = proposal["candidate"]
        rules = geometry_rules(
            row["vehicle"]["xyz"],
            candidate["target_world_xyz_m"],
            message["next_target_world_xyz_m"],
            self.config,
            self.bundle,
            origin=self.proposal_origin(proposal),
        )
        if self.pad_cycle(message["cycle"]):
            rules["pad"] = self.require_pad_approach(row, candidate["target_world_xyz_m"])
        from pyproj import Geod

        lon, lat = self.config["world"]["frame"]["home_lon_lat"]
        xyz = candidate["target_world_xyz_m"]
        glon, glat, _ = Geod(ellps="WGS84").fwd(
            lon, lat, math.degrees(math.atan2(xyz[0], xyz[1])), math.hypot(*xyz[:2])
        )
        world_frame = self.config["decisions"].get("wam_profile") == "motion-v4"
        command_heading = (
            executor_heading(candidate, row) if world_frame else candidate["target_heading_ned_rad"]
        )
        offshore = bool(self.config["world"].get("sea_extension"))
        mapped_contract = self.config.get("altitude_transport_contract") == SCHEMA
        command_altitude = executor_altitude(xyz[2], row) if offshore or mapped_contract else xyz[2]
        item = dict(
            seq=0,
            command=16,
            latitude_deg=glat,
            longitude_deg=glon,
            altitude_m=command_altitude,
            current=1,
            frame=6,
            param2=0.25,
            param4=math.degrees(command_heading),
        )
        from scripts.yokohama_decision_worker import attempt_name

        name = attempt_name(message["cycle"], message.get("attempt", 0))
        script = self.root / (name + "-upload.py")
        altitude_mapping = None
        if mapped_contract:
            world_item = dict(item, world_z_m=xyz[2])
            del world_item["altitude_m"]
            altitude_mapping = compile_mission(
                [world_item, dict(world_item, seq=1, command=17, current=0)],
                row,
                segment=name,
                run_id=self.config["run_id"],
                world_sha256=self.config["world"]["world_sha256"],
            )
            (self.root / (name + "-altitude-mapping.json")).write_text(
                json.dumps(altitude_mapping, allow_nan=False) + "\n"
            )
        if altitude_mapping is not None:
            (self.root / (name + "-altitude-items.json")).write_text(
                json.dumps(altitude_mapping["mission_items"], allow_nan=False) + "\n"
            )
        script.write_text(
            _inner_upload_script(
                [item, dict(item, seq=1, command=17, current=0)], reuse_mavlink_session=True,
                runtime_items_path="/mission/" + name + "-altitude-items.json" if altitude_mapping else None,
                prepared_binding_path="/mission/" + name + "-upload-binding.json"
                if self.config.get("mission_upload_preparation") else None,
            )
        )
        # Reconnect from the actual model endpoint, not the old authored stop.
        expected_next = {1: "01-D2", 2: "02-D3", 3: "03-DELIVERY"}[message["cycle"]]
        stage = next(s for s in self.config["flight_stages"] if s["name"] == expected_next)
        if stage["target_world_xyz_m"] != message["next_target_world_xyz_m"]:
            raise ValueError("City connector target is outside the approved stage")
        connector_name = name + "-connect"
        connector_items = []
        next_xyz = stage["target_world_xyz_m"]
        count = math.ceil(math.dist(xyz, next_xyz) / 20)
        geod = Geod(ellps="WGS84")
        for step in range(1, count + 1):
            point = [a + (b - a) * step / count for a, b in zip(xyz, next_xyz)]
            lng, lt, _ = geod.fwd(
                lon, lat, math.degrees(math.atan2(point[0], point[1])), math.hypot(*point[:2])
            )
            connector_items.append(
                dict(
                    item,
                    seq=step - 1,
                    current=int(step == 1),
                    longitude_deg=lng,
                    latitude_deg=lt,
                    altitude_m=executor_altitude(point[2], row) if offshore else point[2],
                    param2=0.5,
                    param4=math.degrees(math.atan2(next_xyz[0] - xyz[0], next_xyz[1] - xyz[1])),
                )
            )
        connector_items.append(
            dict(
                connector_items[-1],
                seq=len(connector_items),
                current=0,
                command=17,
                param4=stage["items"][-1]["param4"],
            )
        )
        connector = self.root / (connector_name + "-upload.py")
        if mapped_contract:
            # Freeze the approved connector geometry; bind transport to a fresh
            # observation at its actual dispatch, after the model segment.
            for index, connector_item in enumerate(connector_items):
                connector_item.pop("altitude_m")
                step = min(index + 1, count)
                connector_item["world_z_m"] = xyz[2] + (next_xyz[2] - xyz[2]) * step / count
            (self.root / (connector_name + "-world-items.json")).write_text(
                json.dumps(connector_items, allow_nan=False) + "\n"
            )
            connector.write_text(
                _inner_upload_script(
                    reuse_mavlink_session=True,
                    runtime_items_path="/mission/" + connector_name + "-altitude-items.json",
                    prepared_binding_path="/mission/" + connector_name + "-upload-binding.json"
                    if self.config.get("mission_upload_preparation") else None,
                )
            )
        else:
            connector.write_text(_inner_upload_script(connector_items, reuse_mavlink_session=True))
        permit = dict(
            run_id=self.config["run_id"],
            config_sha256=digest(self.config),
            cycle=message["cycle"],
            permit_id=uuid4().hex,
            candidate=candidate,
            executor_heading_ned_rad=command_heading,
            heading_mapping_observation_sha256=digest(row),
            rules=rules,
            observation_sha256=digest(row),
            vla_response_sha256=proposal["vla_response_sha256"],
            wam_assessment_sha256=digest(proposal["wam"]),
            expires_at_worker_wall_s=row["wall_s"] + 2,
            upload_name=name,
            upload_sha256=ship_anwm.digest(script),
            connector_name=connector_name,
            connector_sha256=ship_anwm.digest(connector),
            physical_execution_invoked=False,
        )
        proposal["prepared_observation"] = row
        if message.get("attempt"):
            permit["attempt"] = message["attempt"]
        if offshore or mapped_contract:
            permit["executor_relative_altitude_m"] = command_altitude
            permit["altitude_mapping_observation_sha256"] = digest(row)
        if altitude_mapping is not None:
            permit["altitude_transport_sha256"] = digest(altitude_mapping)
            permit["connector_world_items_sha256"] = ship_anwm.digest(
                self.root / (connector_name + "-world-items.json")
            )
        proposal["prepared_permit"] = permit
        return permit

    def activate(self, message, output):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host cannot activate dispatch")
        proposal = self.pending[message["cycle"]]
        prepared = proposal["prepared_permit"]
        if message["prepared_permit_sha256"] != digest(prepared):
            raise ValueError("Activation does not bind the uploaded mission")
        current = message["observation"]
        old = proposal["input_observation"]
        if self.config.get("altitude_transport_contract") == SCHEMA:
            mapping = json.loads(
                (self.root / (prepared["upload_name"] + "-altitude-mapping.json")).read_text()
            )
            if digest(mapping) != prepared["altitude_transport_sha256"]:
                raise ValueError("Changed city altitude transport")
            recheck_mapping(mapping, current)
        elif self.config["world"].get("sea_extension"):
            if (
                abs(
                    executor_altitude(0, current)
                    - executor_altitude(0, proposal["prepared_observation"])
                )
                > 0.05
            ):
                raise ValueError("World-to-AP altitude mapping expired during upload")
        if (
            math.dist(current["vehicle"]["xyz"], old["vehicle"]["xyz"]) > 0.5
            or abs(math.remainder(current["heading_ned_rad"] - old["heading_ned_rad"], 2 * math.pi))
            > 0.03
        ):
            raise ValueError("Hold lost during mission upload")
        if self.config["decisions"].get("wam_profile") == "motion-v4":
            mapping_pose = proposal["prepared_observation"]
            if (
                abs(
                    math.remainder(
                        camera_heading(current) - camera_heading(mapping_pose), 2 * math.pi
                    )
                )
                > 0.03
            ):
                raise ValueError("World-to-AP heading mapping expired during upload")
        rules = geometry_rules(
            current["vehicle"]["xyz"],
            prepared["candidate"]["target_world_xyz_m"],
            prepared["rules"]["next_target_world_xyz_m"],
            self.config,
            self.bundle,
            origin=self.proposal_origin(proposal),
        )
        if self.pad_cycle(message["cycle"]):
            rules["pad"] = self.require_pad_approach(
                current, prepared["candidate"]["target_world_xyz_m"]
            )
        permit = dict(
            prepared,
            prepared_permit_sha256=digest(prepared),
            rules=rules,
            observation_sha256=digest(current),
            expires_at_worker_wall_s=current["wall_s"] + 2,
        )
        del self.pending[message["cycle"]]
        return permit

    def revoked(self, message):
        from scripts.yokohama_decision_worker import attempt_name

        path = self.folder / (
            attempt_name(message["cycle"], message.get("attempt", 0)) + "-revoked.json"
        )
        if not path.exists():
            return False
        record = json.loads(path.read_text())
        if record != {
            key: message.get(key, 0) for key in ("run_id", "config_sha256", "cycle", "attempt")
        }:
            raise ValueError("Unbound attempt revocation")
        self.pending.pop(message["cycle"], None)
        return True

    def serve(self):
        seen = set()
        while not self.stop_event.wait(0.02):
            for path in sorted(self.folder.glob("*-request.json")):
                if path.name in seen:
                    continue
                seen.add(path.name)
                message = json.loads(path.read_text())
                output = self.folder / path.name.removesuffix("-request.json")
                output.mkdir()
                start = time.monotonic()
                result = dict(
                    request_sha256=digest(message),
                    operation=message["operation"],
                    run_id=self.config["run_id"],
                    native_backend=self.backend == "native",
                )
                try:
                    if message["run_id"] != self.config["run_id"] or message[
                        "config_sha256"
                    ] != digest(self.config):
                        raise ValueError("Mailbox run/config mismatch")
                    operation = message["operation"]
                    from scripts.yokohama_decision_worker import require_city_request

                    require_city_request(self.config, message)
                    if operation not in {"start", "stop"} and not self.active:
                        raise ValueError("Model session revoked or not started")
                    if operation != "stop":
                        attempt = message.get("attempt", 0)
                        policy = self.config["decisions"].get("hold_recovery")
                        if (policy and not 1 <= attempt <= policy["max_attempts"]) or (
                            not policy and attempt
                        ):
                            raise ValueError("Attempt outside configured recovery bound")
                        previous = self.attempts.get(message["cycle"], 0)
                        if attempt < previous or (
                            attempt > previous and operation not in {"start", "resume", "vla"}
                        ):
                            raise ValueError("Obsolete or unstarted decision attempt")
                        if attempt > previous:
                            self.pending.pop(message["cycle"], None)
                            self.attempts[message["cycle"]] = attempt
                        # A lifecycle already requested may finish, but its old
                        # response cannot grant authority to a revoked attempt.
                        if operation != "start" and self.revoked(message):
                            raise ValueError("Decision attempt revoked before processing")
                    delay = self.config["decisions"].get("fixture_delay_s", {}).get(operation, 0)
                    if self.backend == "fixture" and delay:
                        time.sleep(delay)
                    if operation in {"start", "stop"}:
                        value = getattr(self, operation)()
                    elif operation == "resume":
                        self.pending.pop(message["cycle"], None)
                        value = self.identity
                    elif operation in {"vla", "wam", "authorize", "activate"}:
                        value = getattr(self, operation)(message, output)
                    else:
                        raise ValueError("Unknown mailbox operation")
                    result["value"] = value
                    if operation != "stop" and self.revoked(message):
                        result["attempt_revoked"] = True
                        raise ValueError("Late result from revoked decision attempt")
                except Exception as exc:
                    result["error"] = type(exc).__name__ + ": " + str(exc)
                result["elapsed_s"] = time.monotonic() - start
                temp = path.with_name(path.name.replace("-request.json", "-response.tmp"))
                temp.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
                temp.replace(path.with_name(path.name.replace("-request.json", "-response.json")))

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.config["decisions"].get("startup_timeout_s", 180) + 10)
            if self.thread.is_alive():
                raise RuntimeError("Decision host still running; remote cleanup must reconcile")
        return self.stop()
