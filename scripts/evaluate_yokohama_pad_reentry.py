#!/usr/bin/env python3
"""Measured RGB replay through learned forecasts, Mission Assurance and pad Rules.

Own-aircraft hold is a fixture. No dispatch, landing or mission benefit is claimed.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.run_yokohama_pad_state import load_data, truth_states  # noqa: E402
from scripts.yokohama_pad_advisory_host import PadAdvisoryHost  # noqa: E402
from scripts.yokohama_pad_state import Model, sha, write  # noqa: E402
from src.runtime.yokohama_pad_advisory_contract import summarize  # noqa: E402
from src.runtime.yokohama_pad_queue import (  # noqa: E402
    clearance,
    digest,
    make_request,
    propose,
    require_response,
)

WAIT, ENTER = "wait_at_current_hold", "enter_delivery_approach"


def observation(config, sequence, i):
    """Measured actor, explicit stationary own-aircraft fixture, recorded clock."""
    t = int(sequence["stamps_ns"][i]) / 1e9
    pad = config["world"]["pad_queue"]["pad_xyz_m"]
    return dict(
        run_id=config["run_id"],
        world_sha256=config["world"]["world_sha256"],
        sim_s=t,
        wall_s=t,
        phase="02-D3",
        nav_state=4,
        arming_state=2,
        landed=False,
        position_valid=True,
        velocity_ned=[0, 0, 0],
        battery_fraction=0.8,
        reset_counters=[0, 0, 0],
        vehicle=dict(id=1, xyz=config["world"]["pad_queue"]["wait_xyz_m"], age_s=0, sensor_sim_s=t),
        queue_lead=dict(id=2, xyz=(sequence["xyz"][i] + pad).tolist(), age_s=0, sensor_sim_s=t),
        replay_scope="own-aircraft hold fixture; only lead and RGB were measured",
    )


def clear(config, row):
    value = clearance(config, row)
    return value["pad_clear"] and value["approach_clear"]


def velocity_forecast(prediction):
    value = copy.deepcopy(prediction)
    points = np.array([d["xyz_m"] for d in value["detections"]])
    velocity = points[-1] - points[-5]
    xyz = points[-1] + np.arange(17)[:, None] / 4 * velocity
    labels = truth_states(xyz) if value["supported"] else np.full(17, "unknown")
    for i, f in enumerate(value["forecasts"]):
        f.update(
            xyz_relative_to_pad_m=xyz[i].tolist() if value["supported"] else None,
            state=str(labels[i]),
        )
    value["model_kind"] = "constant velocity using identical learned detections and support gate"
    return value


class ReplayHost(PadAdvisoryHost):
    """Precomputed real CPU prediction; replaces file/camera acquisition, not judge."""

    def forecast(self, request, folder):
        return dict(
            schema="missionos.pad-state-advisory.v1",
            request_id=request["request_id"],
            model_sha256=self.policy["weights_sha256"],
            camera_entity=self.policy["camera_entity"],
            history_sha256=request["pad_camera_history"]["sha256"],
            status="computed",
            reason="measured_RGB_replay_no_live_aircraft",
            forecast=self.prediction,
            flight_authority_created=False,
        )


def evaluate(root, output, previous=None):
    previous = previous or REPO / "docs/examples/yokohama-pad-state"
    protocol = json.loads((root / "protocol.json").read_text())
    timeline = json.loads((root / "replay-protocol.json").read_text())
    frozen = json.loads((root / "model/training.json").read_text())
    if frozen["protocol_sha256"] != protocol.get(
        "training_protocol_sha256", sha(root / "protocol.json")
    ) or frozen["weights_sha256"] != sha(root / "model/model.npz"):
        raise ValueError("Post-training freeze changed")
    if sha(previous / "model/model.npz") != protocol["existing_weights_sha256"]:
        raise ValueError("Existing model changed")
    config_source = root / "capture/config.json"
    if not config_source.exists():
        config_source = root / "capture-config.json"
    base_config = json.loads(config_source.read_text())
    base_config["operator_approval"] = "explicit offline fixture replay only; no aircraft dispatch"
    base_config["run_id"] = "pad-reentry-offline-replay"
    sequences = [("new_timing", s) for s in load_data(root / "data", {"unseen_test"})]
    sequences += [
        ("previous_regression", s) for s in load_data(previous / "test-data", {"unseen_test"})
    ]
    if [s["id"] for scope, s in sequences if scope == "new_timing"] != [
        c["id"] for c in protocol["cases"] if c["split"] == "unseen_test"
    ]:
        raise ValueError("Evaluation case split changed")
    method_names = ["current_rules", "existing", "post_trained", "constant_velocity", "oracle"]
    if protocol.get("risk_followup"):
        method_names.insert(3, "post_trained_risk")
    models = {
        "existing": Model.load(previous / "model"),
        "post_trained": Model.load(root / "model"),
    }
    hashes = {
        "existing": sha(previous / "model/model.npz"),
        "post_trained": sha(root / "model/model.npz"),
    }
    output.mkdir(parents=True, exist_ok=False)
    records, episodes = [], []
    with TemporaryDirectory() as scratch:
        folder = Path(scratch)
        with gzip.open(output / "runtime-trace.jsonl.gz", "wt") as trace:
            for scope, s in sequences:
                rows = [observation(base_config, s, i) for i in range(len(s["rgb"]))]
                current_clear = [clear(base_config, row) for row in rows]
                by_index = {}
                for i in range(23, len(rows) - 16, 4):
                    evidence = (
                        rows[i - 20 : i + 1] if all(current_clear[i - 20 : i + 1]) else [rows[i]]
                    )
                    impending = not all(current_clear[i + 1 : i + 17])
                    config = copy.deepcopy(base_config)
                    config["world"]["pad_state_advisory"] = dict(
                        mode="assist",
                        camera_entity="pad_state_camera",
                        weights_sha256=hashes["existing"],
                    )
                    history = dict(
                        file="replay-array-window",
                        sha256=digest(
                            dict(
                                case=s["id"],
                                first=i - 15,
                                last=i,
                                stamps=s["stamps_ns"][i - 15 : i + 1].tolist(),
                            )
                        ),
                    )
                    actions, forecasts = {}, {}
                    for name, model in models.items():
                        # Never pass actor positions, schedule, future frames or case ID to inference.
                        prediction = model.predict(
                            s["rgb"][i - 15 : i + 1],
                            s["stamps_ns"][i - 15 : i + 1],
                            s["camera_poses"][i - 15 : i + 1],
                        )
                        forecasts[name] = prediction
                    forecasts["constant_velocity"] = velocity_forecast(forecasts["existing"])
                    if protocol.get("risk_followup"):
                        forecasts["post_trained_risk"] = copy.deepcopy(forecasts["post_trained"])
                    record = dict(
                        scope=scope,
                        case=s["id"],
                        frame_index=i,
                        elapsed_s=float(s["elapsed_s"][i]),
                        future_reoccupied=impending,
                        current_clear=current_clear[i],
                        decisions={},
                    )
                    for name, prediction in forecasts.items():
                        config["world"]["pad_state_advisory"]["weights_sha256"] = hashes[
                            "post_trained" if name.startswith("post_trained") else "existing"
                        ]
                        if name == "post_trained_risk":
                            config["world"]["pad_state_advisory"]["reentry_risk_advisory"] = True
                        else:
                            config["world"]["pad_state_advisory"].pop("reentry_risk_advisory", None)
                        request = make_request(config, i, evidence, history)
                        baseline = propose(config, request)
                        host = ReplayHost(folder, config)
                        host.prediction = prediction
                        response = host.respond(request, folder, baseline)
                        action = require_response(config, request, response, rows[i])
                        judgment = json.loads((folder / "mission-assurance.json").read_text())
                        if (
                            digest(judgment) != response["mission_assurance_sha256"]
                            or response["approval_granted"]
                            or response["dispatch_authority_created"]
                        ):
                            raise ValueError("Replay authority or judgment binding")
                        signal = summarize(config, request, response["advisory"])
                        actions[name] = action
                        record["decisions"][name] = dict(
                            action=action,
                            signal=signal,
                            supported=prediction["supported"],
                            forecast=prediction,
                        )
                        trace.write(
                            json.dumps(
                                dict(
                                    case=s["id"],
                                    frame_index=i,
                                    method=name,
                                    request=request,
                                    response=response,
                                    judgment=judgment,
                                ),
                                allow_nan=False,
                            )
                            + "\n"
                        )
                    actions["current_rules"] = baseline["proposed_action"]
                    actions["oracle"] = WAIT if impending else actions["current_rules"]
                    record["actions"] = actions
                    records.append(record)
                    by_index[i] = record
                if scope == "new_timing":
                    # Every predeclared arrival is retained. No dispatch: the first
                    # proposal is only scored against the recorded next four seconds.
                    for arrival in timeline["arrival_times_sim_s"]:
                        eligible = [
                            r
                            for r in by_index.values()
                            if r["elapsed_s"]
                            >= arrival + timeline["first_decision_after_arrival_s"]
                        ]
                        candidates = eligible[:: timeline["episode_decision_stride_s"]]
                        for method in method_names:
                            chosen = next(
                                (r for r in candidates if r["actions"][method] == ENTER), None
                            )
                            episodes.append(
                                dict(
                                    case=s["id"],
                                    arrival_s=arrival,
                                    method=method,
                                    first_entry_s=chosen["elapsed_s"] if chosen else None,
                                    reoccupation_next_4s=chosen["future_reoccupied"]
                                    if chosen
                                    else None,
                                    frame_index=chosen["frame_index"] if chosen else None,
                                    permitted_entry=chosen is not None,
                                    dispatch_invoked=False,
                                )
                            )
    metrics = {}
    for scope in ("new_timing", "previous_regression"):
        part = [r for r in records if r["scope"] == scope]
        current_entries = [r for r in part if r["actions"]["current_rules"] == ENTER]
        opportunities = [r for r in current_entries if r["future_reoccupied"]]
        metrics[scope] = dict(
            windows=len(part),
            current_entry_windows=len(current_entries),
            impending_reoccupation_windows=len(opportunities),
            methods={},
        )
        for method in method_names:
            metrics[scope]["methods"][method] = dict(
                anticipated_waits=sum(r["actions"][method] == WAIT for r in opportunities),
                missed_reentries=sum(r["actions"][method] == ENTER for r in opportunities),
                additional_wait_without_reoccupation_next_4s=sum(
                    r["actions"][method] == WAIT and not r["future_reoccupied"]
                    for r in current_entries
                ),
                changed_decisions=sum(
                    r["actions"][method] != r["actions"]["current_rules"] for r in part
                ),
            )
    episode_metrics = {}
    for method in method_names:
        part = [r for r in episodes if r["method"] == method]
        episode_metrics[method] = dict(
            episodes=len(part),
            entries=sum(r["permitted_entry"] for r in part),
            entries_followed_by_reoccupation=sum(r["reoccupation_next_4s"] is True for r in part),
            first_entry_elapsed_sum_s=sum(r["first_entry_s"] or 0 for r in part),
        )
    result = dict(
        schema="missionos.pad-reentry-evaluation.v1",
        status="replayed",
        metrics=metrics,
        episode_metrics=episode_metrics,
        inference_calls=len(records) * 2,
        shared_mission_judgments=len(records) * (4 if protocol.get("risk_followup") else 3),
        conditions=dict(
            own_aircraft="synthetic stable hold",
            lead="measured Gazebo poses",
            model="real CPU inference from recorded RGB",
            mission_judge="shared MissionAssuranceAgent with deterministic fixture",
            native_anwm=False,
            vla=False,
            gpu=False,
            aircraft_flown=False,
            mission_benefit_demonstrated=False,
        ),
        weights_sha256=hashes,
        protocol_sha256=sha(root / "protocol.json"),
        replay_protocol_sha256=sha(root / "replay-protocol.json"),
    )
    write(output / "evaluation.json", result)
    write(output / "predictions.json", records)
    write(output / "episodes.json", episodes)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.root, args.output)
