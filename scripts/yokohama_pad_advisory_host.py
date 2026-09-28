"""Live CPU RGB -> auxiliary forecast -> shared fixture Mission Assurance.

Weights are loaded only for a city pad-wait request and released at pad exit.
This is neither native ANWM nor VLA and cannot bypass independent entry Rules.
"""

from __future__ import annotations
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from src.runtime.yokohama_payload import atomic_json, digest
from src.runtime.yokohama_pad_advisory_contract import summarize, selected_action


def add_camera(root, world, model_bundle, mode):
    from src.runtime.yokohama_scene import camera, sha256

    reference = json.loads((model_bundle / "development-capture/train-depart.json").read_text())[
        "frames"
    ][0]["rig_pose"]
    metadata = json.loads((model_bundle / "model/model.json").read_text())
    source_config = json.loads((model_bundle / "development-capture/config.json").read_text())
    if world["pad_queue"]["pad_xyz_m"] != source_config["world"]["pad_queue"]["pad_xyz_m"]:
        raise ValueError("The auxiliary model is calibrated to a different pad")
    w, x, y, z = reference["quat_wxyz"]
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(2 * (w * y - z * x))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    path = root / "models/worlds/default.sdf"
    tree = ET.parse(path)
    model = ET.SubElement(tree.getroot().find("world"), "model", name="pad_state_camera")
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = " ".join(map(str, [*reference["xyz"], roll, pitch, yaw]))
    link = ET.SubElement(model, "link", name="base_link")
    camera(
        link,
        "pad_state_rgbd",
        [0.25, 0, 0.1],
        [0, 0, 0],
        "/yokohama/pad-state",
        rgbd=True,
        rate_hz=4,
    )
    plugin = ET.SubElement(
        model,
        "plugin",
        filename="gz-sim-pose-publisher-system",
        name="gz::sim::systems::PosePublisher",
    )
    for k, v in [
        ("publish_model_pose", "true"),
        ("publish_link_pose", "false"),
        ("use_pose_vector_msg", "true"),
        ("update_frequency", "250"),
        ("topic", "/world/default/pose/info"),
    ]:
        ET.SubElement(plugin, k).text = v
    tree.write(path, encoding="utf-8", xml_declaration=True)
    world["world_sha256"] = sha256(path)
    world["pad_state_advisory"] = dict(
        mode=mode,
        camera_entity="pad_state_camera",
        topic="/yokohama/pad-state/image",
        camera_source="fixed delivery-pad camera, not onboard",
        weights_sha256=metadata["weights_sha256"],
        model_kind="CPU learned lead state, not ANWM or VLA",
        request_interval_sim_s=2,
        activation="city pad-wait only; lazy load; release at exit",
        unknown_behavior="fresh current Rules; no fixed aggregate percentage gate",
    )
    return world


class PadAdvisoryHost:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.policy = config["world"]["pad_state_advisory"]
        self.model = None
        self.closed = False
        self.calls = 0
        self.last_stamp = -1

    def event(self, kind, **values):
        row = dict(
            event=kind,
            run_id=self.config["run_id"],
            wall_utc=datetime.now(timezone.utc).isoformat(),
            **values,
        )
        with (self.root / "pad-advisory-events.jsonl").open("a") as f:
            f.write(json.dumps(row, allow_nan=False) + "\n")

    def close(self):
        if not self.closed:
            self.model = None
            self.closed = True
            self.event("model_released", inference_calls=self.calls)
            atomic_json(
                self.root / "pad-advisory-release.json",
                dict(
                    released=True,
                    inference_calls=self.calls,
                    run_id=self.config["run_id"],
                    native_anwm_invoked=False,
                    vla_invoked=False,
                    gpu_used=False,
                ),
            )

    def forecast(self, request, folder):
        receipt = dict(
            schema="missionos.pad-state-advisory.v1",
            request_id=request["request_id"],
            model_sha256=self.policy["weights_sha256"],
            camera_entity=self.policy["camera_entity"],
            history_sha256=(request.get("pad_camera_history") or {}).get("sha256"),
            status="unavailable",
            reason="camera_history_not_ready",
            flight_authority_created=False,
        )
        if self.closed or (self.root / "pad-advisory-closed.json").exists():
            receipt["reason"] = "advisory_closed"
            return receipt
        row = request["observations"][-1]
        if row["phase"] != self.config["world"]["pad_queue"]["trigger_phase"]:
            raise ValueError("Auxiliary inference outside city pad wait")
        history = request.get("pad_camera_history")
        if history is None:
            return receipt
        started = time.perf_counter()
        try:
            import numpy as np
            from PIL import Image
            from scripts.ship_anwm import camera_pose
            from scripts.yokohama_pad_state import Model, sha

            if (
                history["file"] != "history/capture.json"
                or sha(folder / history["file"]) != history["sha256"]
            ):
                raise ValueError("Camera history identity changed")
            data = json.loads((folder / history["file"]).read_text())
            if (
                data["run_id"] != self.config["run_id"]
                or data["world_sha256"] != self.config["world"]["world_sha256"]
                or data["camera_entity"] != self.policy["camera_entity"]
                or len(data["frames"]) != 16
            ):
                raise ValueError("Foreign camera history")
            rgb, poses, stamps = [], [], []
            for frame in data["frames"]:
                if (
                    Path(frame["file"]).name != frame["file"]
                    or sha(folder / "history" / frame["file"]) != frame["sha256"]
                ):
                    raise ValueError("Camera image identity changed")
                image = Image.open(folder / "history" / frame["file"]).convert("RGB")
                if image.size != (640, 360):
                    raise ValueError("Camera shape changed")
                rgb.append(np.asarray(image.resize((320, 180), Image.Resampling.BILINEAR)))
                pose = frame["pose"]
                poses.append(
                    camera_pose(
                        dict(
                            vehicle_position_enu_m=pose["xyz"],
                            vehicle_quaternion_wxyz=pose["quat_wxyz"],
                        )
                    )
                )
                stamps.append(frame["stamp_ns"])
            if stamps[-1] <= self.last_stamp or not 0 <= row["sim_s"] - stamps[-1] / 1e9 <= 1:
                raise ValueError("Stale or reused image history")
            if self.model is None:
                if sha(self.root / "pad-state-model/model.npz") != self.policy["weights_sha256"]:
                    raise ValueError("Unregistered model weights")
                self.model = Model.load(self.root / "pad-state-model")
                self.event("model_loaded", phase=row["phase"], sim_s=row["sim_s"])
            prediction = self.model.predict(
                np.array(rgb), np.array(stamps, dtype=np.int64), np.array(poses)
            )
            self.calls += 1
            self.last_stamp = stamps[-1]
            receipt.update(status="computed", reason="measured_camera_history", forecast=prediction)
            self.event(
                "inference_completed",
                sequence=request["sequence"],
                phase=row["phase"],
                sim_s=row["sim_s"],
                input_last_stamp_ns=stamps[-1],
                supported=prediction["supported"],
            )
        except (ValueError, KeyError, TypeError, OSError) as error:
            receipt["reason"] = "input_or_model_unavailable:" + type(error).__name__
            self.event(
                "inference_unavailable", sequence=request["sequence"], reason=receipt["reason"]
            )
        receipt["host_seconds"] = time.perf_counter() - started
        return receipt

    def respond(self, request, folder, baseline):
        from src.intelligence.mission_assurance_agent import (
            MissionAssuranceAgent,
            MissionSituation,
            ModelJudgment,
        )

        receipt = self.forecast(request, folder)
        signal = summarize(self.config, request, receipt)
        action = selected_action(self.config, request, receipt, baseline["proposed_action"])
        situation = MissionSituation(
            situation_id=request["request_id"],
            observed_at=datetime.now(timezone.utc).isoformat(),
            mission_contract=dict(objective="wait for lead, deliver cargo and return"),
            progress=dict(region="city", phase="pad_wait"),
            observations=dict(
                current_rules_action=baseline["proposed_action"], auxiliary_signal=signal
            ),
            constraints=dict(
                mode=self.policy["mode"],
                allowed_actions=self.config["world"]["pad_queue"]["approved_actions"],
                unknown_fallback="current_rules",
                forecast_authorizes_entry=False,
            ),
            uncertainty=dict(
                source="learned CPU image forecast + deterministic mission judge", forecast=receipt
            ),
            source_refs=(request["request_id"],),
            source_schema_version=request["schema"],
            input_digest=digest([request, receipt]),
            execution_scope="simulation",
        )

        class FixtureJudge:
            def judge(self, prompt):
                s = prompt["mission_situation"]
                should_wait = s["observations"][
                    "current_rules_action"
                ] == "wait_at_current_hold" or (
                    s["constraints"]["mode"] == "assist"
                    and s["observations"]["auxiliary_signal"] == "future_occupancy_reobserve"
                )
                choice = "wait_at_current_hold" if should_wait else "enter_delivery_approach"
                return ModelJudgment(
                    output=dict(
                        proposed_response_kind="hold" if should_wait else "continue",
                        parameters=dict(action=choice),
                        rationale="Current Rules plus optional future occupancy; unknown falls back to current Rules.",
                        expected_outcome="Bounded wait or independently checked entry.",
                        uncertainty="Fixed pad camera; CPU fixture judge; no native VLA/ANWM.",
                        operator_question="Within the explicit simulator approval.",
                    ),
                    invocation_evidence=dict(invocation_kind="deterministic_fixture"),
                    model_inference_invoked=False,
                )

        proposal = MissionAssuranceAgent(FixtureJudge()).evaluate(situation).to_dict()
        if proposal["judgment_status"] != "proposal_guardrail_passed" or proposal["parameters"] != {
            "action": action
        }:
            raise ValueError("Mission judgment did not preserve the constrained action")
        judgment = dict(situation=situation.to_dict(), proposal=proposal)
        atomic_json(folder / "mission-assurance.json", judgment)
        atomic_json(folder / "advisory.json", receipt)
        return dict(
            baseline,
            proposed_action=action,
            backend="cpu_state_advisory_fixture",
            advisory=receipt,
            mission_assurance_sha256=digest(judgment),
        )
