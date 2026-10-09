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
import re
import subprocess
import threading
import time
import urllib.request
from uuid import uuid4

import numpy as np
from PIL import Image

from scripts import ship_anwm
from scripts.yokohama_endpoint_feedback import (
    feedback_policy,
    validate_feedback_candidate,
    validate_feedback_map,
    validate_feedback_request,
)
from scripts.yokohama_altitude_contract import (
    SCHEMA,
    compile_mission,
    recheck_mapping,
)
from scripts.yokohama_goal_distance_adapter import adapt_candidate, validate_adaptation
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
        feedback = feedback_policy(config)
        if feedback is not None and (detached or mock_http or backend != config["decisions"]["backend"]):
            raise ValueError("Endpoint feedback requires its explicit non-shadow backend")
        self.identity, self.pending = None, {}
        self.attempts = {}
        self.active, self.closed = False, False
        self.feedback_deadline = None
        self.feedback_failed = False
        self.feedback_activated = {}
        self.feedback_vla_capture_end = {}
        self.feedback_model_requests = {"vla": 0, "wam": 0}
        self.feedback_requested = set()
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
        if feedback_policy(getattr(self, "config", {})) is not None:
            from src.runtime.yokohama_shadow_http import exchange as bounded_exchange

            remaining = self.feedback_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Endpoint feedback total deadline exceeded")
            kwargs["timeout"] = min(kwargs.get("timeout", 75), remaining)
            return (getattr(self, "http_exchange", None) or bounded_exchange)(*args, **kwargs)
        return (getattr(self, "http_exchange", None) or exchange)(*args, **kwargs)

    def feedback_guard(self, message, *, model=None):
        """Independently bind endpoint continuation and reserve each model call."""
        policy = feedback_policy(self.config)
        if policy is None:
            return None
        if self.closed or not self.active or self.feedback_failed:
            raise ValueError("Endpoint feedback session is inactive")
        if self.feedback_deadline is None or time.monotonic() >= self.feedback_deadline:
            raise TimeoutError("Endpoint feedback total deadline exceeded")
        validate_feedback_map(self.config, self.bundle)
        validate_feedback_request(self.config, message)
        if (message.get("run_id") != self.config["run_id"]
                or message.get("config_sha256") != digest(self.config)):
            raise ValueError("Endpoint feedback run/config mismatch")
        cycle = message["cycle"]
        if cycle == 2 and message["previous_segment"]["permit"] != self.feedback_activated.get(1):
            raise ValueError("Endpoint feedback predecessor was not activated by this host")
        if model is not None:
            key = (cycle, model)
            if (key in self.feedback_requested
                    or self.feedback_model_requests[model] >= policy[f"max_{model}_requests"]):
                raise ValueError("Endpoint feedback model request budget exhausted")
            if model == "wam" and (cycle, "vla") not in self.feedback_requested:
                raise ValueError("Endpoint feedback WAM requires this cycle's VLA")
            self.feedback_requested.add(key)
            self.feedback_model_requests[model] += 1
        return policy

    def feedback_capture(self, message, record, model):
        if feedback_policy(self.config) is None:
            return
        first, last = record["frames"][0], record["frames"][-1]
        row = message["observation"]
        cutoff = (message["previous_segment"]["arrival"]["sim_s"]
                  if message["cycle"] == 2 else -math.inf)
        if model == "wam":
            cutoff = max(cutoff, self.feedback_vla_capture_end[message["cycle"]])
        if first["stamp_ns"] / 1e9 <= cutoff:
            raise ValueError("Endpoint feedback requires a fresh post-boundary capture")
        rotation = ship_anwm.rotation(row["vehicle"]["quat_wxyz"])
        captured_rotation = ship_anwm.rotation(last["pose"]["quat_wxyz"])
        if (math.dist(row["vehicle"]["xyz"], last["pose"]["xyz"]) > 0.5
                or np.arccos(np.clip((np.trace(rotation.T @ captured_rotation) - 1) / 2, -1, 1)) > 0.03):
            raise ValueError("Endpoint feedback capture/observation pose mismatch")
        if model == "vla":
            self.feedback_vla_capture_end[message["cycle"]] = last["stamp_ns"] / 1e9

    def feedback_rules(self, rules, start, candidate, *, origin=None, final=False):
        policy = feedback_policy(self.config)
        if policy is not None:
            target = candidate["target_world_xyz_m"]
            rules["endpoint_feedback"] = validate_feedback_candidate(
                self.config, start, target, origin=origin,
            )
            if final:
                rules["exit_connection"] = geometry_rules(
                    start, target, policy["exit_world_xyz_m"], self.config, self.bundle,
                    origin=origin,
                )
        return rules

    def lifecycle(self, operation):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host does not own service lifecycle")
        feedback = feedback_policy(self.config)
        timeout = self.config["decisions"].get("startup_timeout_s", 180) if operation == "start" else 60
        if feedback is not None and operation == "start":
            if self.feedback_deadline is None:
                raise ValueError("Endpoint feedback startup requires a session deadline")
            remaining = self.feedback_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Endpoint feedback total deadline exceeded before startup")
            timeout = min(timeout, remaining)
        argv = self.service_config[operation + "_argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
            raise ValueError("Lifecycle requires explicit argv")
        with (
            (self.folder / (operation + ".stdout")).open("w") as out,
            (self.folder / (operation + ".stderr")).open("w") as err,
        ):
            self.process = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                if feedback is None:
                    raise
                # Reap only this owned local lifecycle child. Remote cleanup
                # remains a separate stop receipt, even after local timeout.
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
                raise TimeoutError("Endpoint feedback lifecycle deadline: " + operation) from exc
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
        policy = feedback_policy(self.config)
        if policy is not None:
            if self.feedback_failed or self.feedback_deadline is not None:
                raise ValueError("Endpoint feedback model session is single use")
            validate_feedback_map(self.config, self.bundle)
            self.feedback_deadline = time.monotonic() + policy["total_timeout_s"]
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
            if policy is not None:
                from src.runtime.ship_aerovla_host import validate_service_identity

                if any(identity.get("fixture") is True for identity in identities.values()):
                    raise ValueError("Native endpoint feedback rejects fixture identities")
                validate_service_identity(identities["vla"])
                wam = identities["wam"]
                if (wam.get("schema_version") != "ship_anwm_static_service.v1"
                        or re.fullmatch(r"[0-9a-f]{32}", wam.get("session_id", "")) is None
                        or wam.get("model_revision") != ship_anwm.MODEL_REVISION
                        or wam.get("vae_revision") != ship_anwm.VAE_REVISION
                        or wam.get("diffusion_steps") != 250):
                    raise ValueError("Unreviewed endpoint feedback native WAM identity")
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
        feedback = self.feedback_guard(message, model="vla")
        path, capture, _ = self.capture(message)
        self.feedback_capture(message, capture, "vla")
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
        destination = "approved nearby goal" if feedback is not None else "delivery pad"
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
            prompt=f"<image>\nFly {direction} along the street toward the {destination}. Maintain height.\nAction: ",
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
                or (feedback is not None and response.get("fixture") is True)
                or response.get("input_images_sha256") != request["images_sha256"]
                or response.get("dispatch_invoked") is not False
                or response.get("physical_execution_invoked") is not False
                or response.get("cuda_allocated_after_request_bytes") != 0
                or response.get("pixel_values_shape") != [1, 6, 224, 224]
                or not response.get("generated_token_ids")
            ):
                raise ValueError("Unbound native VLA result")
        else:
            forward_bin = 55
            rejected_fixture = self.config.get("fixture_reject_second_candidate") and message["cycle"] == 2
            if rejected_fixture:
                forward_bin = 58
            elif feedback is not None:
                # Explicit CPU double: choose before decoding, never repair native output.
                forward_bin = min(58, math.floor(
                    math.dist(row["vehicle"]["xyz"], feedback["goal_world_xyz_m"]) * 98 / 5
                ))
                if forward_bin < 20:
                    raise ValueError("Fixture feedback goal needs no admissible translation")
            response = dict(generated_text=f"{forward_bin} 49 49", fixture=True,
                            vla_inference_invoked=False)
            if rejected_fixture:
                # Explicit CPU fault: repeat the full first-step distance even
                # though only about one metre remains. The valid level action
                # overshoots the goal and fails unchanged progress Rules.
                response["generated_text"] = "58 49 49"
        (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
        candidate = vla_candidate(response["generated_text"], row)
        try:
            candidate, adjustment = adapt_candidate(self.config, candidate, row, digest(response))
        except ValueError as exc:
            if self.config["decisions"].get("goal_distance_adapter") is not None:
                (output / "vehicle-distance-adjustment-rejected.json").write_text(json.dumps({
                    "original_candidate": candidate, "vla_response_sha256": digest(response),
                    "input_observation_sha256": digest(row), "config_sha256": digest(self.config),
                    "executed_candidate": None, "reason": str(exc),
                    "grants_dispatch_authority": False,
                }, indent=2, allow_nan=False) + "\n")
            raise
        if adjustment is not None:
            (output / "vehicle-distance-adjustment.json").write_text(json.dumps(adjustment, indent=2, allow_nan=False) + "\n")
        rules = geometry_rules(
            row["vehicle"]["xyz"],
            candidate["target_world_xyz_m"],
            message["next_target_world_xyz_m"],
            self.config,
            self.bundle,
        )
        self.feedback_rules(rules, row["vehicle"]["xyz"], candidate)
        result = dict(
            candidate=candidate,
            vla_response_sha256=digest(response),
            input_observation=row,
            preliminary_rules=rules,
            native_vla_invoked=self.backend == "native" and not getattr(self, "mock_http", False),
        )
        if adjustment is not None:
            result["vehicle_distance_adjustment"] = adjustment
        self.pending[message["cycle"]] = result
        return result

    def wam(self, message, output):
        if getattr(self, "detached", False) and (not self.active or self.closed):
            raise ValueError("Detached model session revoked or not attached")
        feedback = self.feedback_guard(message, model="wam")
        proposal = self.pending[message["cycle"]]
        validate_adaptation(self.config, proposal)
        if message["vla"] != proposal:
            raise ValueError("Cross-cycle VLA proposal")
        _, capture, arrays = self.capture(message)
        self.feedback_capture(message, capture, "wam")
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
            payload = {
                "request_id": uuid4().hex,
                "request_base64": base64.b64encode(
                    (output / "input/request.json").read_bytes()
                ).decode(),
                "history_base64": base64.b64encode(
                    (output / "input/history.npz").read_bytes()
                ).decode(),
            }
            response = self._exchange(
                self.service_config["wam_port"],
                "infer",
                payload,
            )
            (output / "native-response.json").write_text(json.dumps(response, indent=2) + "\n")
            if (
                response.get("identity") != self.identity["services"]["wam"]
                or response.get("request_sha256") != ship_anwm.digest(output / "input/request.json")
                or response.get("history_sha256") != request["history_sha256"]
                or response.get("wam_inference_invoked")
                is not (not getattr(self, "mock_http", False))
                or (getattr(self, "mock_http", False) and response.get("fixture") is not True)
                or (feedback is not None and (
                    response.get("fixture") is True
                    or response.get("physical_execution_invoked") is not False
                    or response.get("request_id") != payload["request_id"]
                ))
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
        feedback = self.feedback_guard(message)
        proposal = self.pending[message["cycle"]]
        validate_adaptation(self.config, proposal)
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
        self.feedback_rules(
            rules, row["vehicle"]["xyz"], candidate, origin=self.proposal_origin(proposal),
            final=feedback is not None and message["cycle"] == feedback["max_cycles"],
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
        # Intermediate feedback endpoints confer no authored AP continuation.
        connector_name, connector = None, None
        if feedback is None or message["cycle"] == feedback["max_cycles"]:
            expected_next = (feedback["exit_phase"] if feedback is not None else
                             {1: "01-D2", 2: "02-D3", 3: "03-DELIVERY"}[message["cycle"]])
            expected_target = (feedback["exit_world_xyz_m"] if feedback is not None else
                               message["next_target_world_xyz_m"])
            stage = next(s for s in self.config["flight_stages"] if s["name"] == expected_next)
            if stage["target_world_xyz_m"] != expected_target:
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
                # Freeze exit geometry; transport binds again at actual dispatch.
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
            connector_sha256=ship_anwm.digest(connector) if connector is not None else None,
            physical_execution_invoked=False,
        )
        if proposal.get("vehicle_distance_adjustment") is not None:
            permit["vehicle_distance_adjustment"] = copy.deepcopy(proposal["vehicle_distance_adjustment"])
        proposal["prepared_observation"] = row
        if message.get("attempt"):
            permit["attempt"] = message["attempt"]
        if offshore or mapped_contract:
            permit["executor_relative_altitude_m"] = command_altitude
            permit["altitude_mapping_observation_sha256"] = digest(row)
        if altitude_mapping is not None:
            permit["altitude_transport_sha256"] = digest(altitude_mapping)
            if connector_name is not None:
                permit["connector_world_items_sha256"] = ship_anwm.digest(
                    self.root / (connector_name + "-world-items.json")
                )
        proposal["prepared_permit"] = permit
        return permit

    def activate(self, message, output):
        if getattr(self, "detached", False):
            raise ValueError("Detached shadow host cannot activate dispatch")
        feedback = self.feedback_guard(message)
        proposal = self.pending[message["cycle"]]
        validate_adaptation(self.config, proposal)
        prepared = proposal["prepared_permit"]
        if prepared.get("vehicle_distance_adjustment") != proposal.get("vehicle_distance_adjustment"):
            raise ValueError("Prepared permit vehicle adjustment changed")
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
        self.feedback_rules(
            rules, current["vehicle"]["xyz"], prepared["candidate"],
            origin=self.proposal_origin(proposal),
            final=feedback is not None and message["cycle"] == feedback["max_cycles"],
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
        if feedback is not None:
            self.feedback_activated[message["cycle"]] = copy.deepcopy(permit)
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
                    if feedback_policy(self.config) is not None and message.get("operation") != "stop":
                        self.feedback_failed = True
                        self.active = False
                        self.pending.clear()
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
