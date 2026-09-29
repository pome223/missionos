#!/usr/bin/env python3
"""Reopen live fixed-pad RGB forecasts and their MissionOS/executor connection."""

from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_yokohama_pad_state import without_timing  # noqa: E402
from scripts.check_yokohama_pad_temporal import same  # noqa: E402
from scripts.verify_yokohama_pad_queue import verify as verify_queue  # noqa: E402
from scripts.yokohama_pad_advisory_host import PadAdvisoryHost  # noqa: E402
from src.runtime.yokohama_pad_advisory_contract import summarize  # noqa: E402
from src.runtime.yokohama_pad_queue import digest, propose  # noqa: E402
from src.runtime.yokohama_payload import read_jsonl  # noqa: E402


def verify(root):
    def read(path):
        return json.loads(path.read_text())

    config = read(root / "config.json")
    result = read(root / "result.json")
    queue = verify_queue(root)
    checks = dict(full_queue_mission=queue["status"] == "passed")
    closed = read(root / "pad-advisory-closed.json")
    released = read(root / "pad-advisory-release.json")
    events = read_jsonl(root / "pad-advisory-events.jsonl")
    flight = read_jsonl(root / "flight-events.jsonl")
    opened = next(
        e["observation"]["sim_s"] for e in flight if e["event"] == "pad_occupied_reported"
    )
    entry = next(
        e["permit"]["rules_checked_at"]["sim_s"]
        for e in flight
        if e["event"] == "pad_entry_authorized"
    )
    checks["city_session_closed_before_delivery"] = (
        closed["run_id"] == config["run_id"] and entry <= closed["closed_sim_s"] <= entry + 1
    )
    calls = [e for e in events if e["event"] == "inference_completed"]
    loads = [e for e in events if e["event"] == "model_loaded"]
    checks["load_and_infer_only_during_city_wait"] = (
        len(loads) == 1
        and bool(calls)
        and all(
            e["phase"] == config["world"]["pad_queue"]["trigger_phase"]
            and opened <= e["sim_s"] <= closed["closed_sim_s"]
            for e in [*loads, *calls]
        )
    )
    release_indices = [i for i, e in enumerate(events) if e["event"] == "model_released"]
    checks["released_without_later_inference"] = (
        release_indices == [len(events) - 1]
        and released["released"] is True
        and released["run_id"] == config["run_id"]
        and result["cpu_pad_state_released"] is True
        and len(calls) == released["inference_calls"] == result["cpu_pad_state_inference_calls"]
    )
    requests = []
    with TemporaryDirectory() as tmp:
        replay_root = Path(tmp)
        shutil.copytree(root / "pad-state-model", replay_root / "pad-state-model")
        host = PadAdvisoryHost(replay_root, config)
        for folder in sorted((root / "pad-decisions").iterdir()):
            req, resp = read(folder / "request.json"), read(folder / "response.json")
            receipt = read(folder / "advisory.json")
            judgment = read(folder / "mission-assurance.json")
            if receipt != resp["advisory"] or digest(judgment) != resp["mission_assurance_sha256"]:
                raise ValueError("Response/forecast/judgment binding changed")
            signal = summarize(config, req, receipt)
            s, p = judgment["situation"], judgment["proposal"]
            # A pad mission judge acts after the advisory; the fixture judgment
            # is bound to the advisory-stage action it saw.
            stage = resp.get("mission_judge", {}).get("prior_action", resp["proposed_action"])
            baseline = propose(config, req)["proposed_action"]
            if (
                s["input_digest"] != digest([req, receipt])
                or s["uncertainty"]["forecast"] != receipt
                or s["observations"] != dict(current_rules_action=baseline, auxiliary_signal=signal)
                or p["parameters"] != dict(action=stage)
                or p["situation_input_digest"] != s["input_digest"]
                or p["proposed_response_kind"]
                != ("hold" if stage == "wait_at_current_hold" else "continue")
                or p["judgment_status"] != "proposal_guardrail_passed"
                or any(
                    p.get(k) is not False
                    for k in [
                        "model_inference_invoked",
                        "operator_approved",
                        "dispatch_authority_created",
                    ]
                )
            ):
                raise ValueError("Shared fixture judgment not bound to this forecast/action")
            if req.get("pad_camera_history"):
                frames = read(folder / "history/capture.json")["frames"]
                if any(
                    not opened <= f["stamp_ns"] / 1e9 <= req["observations"][-1]["sim_s"]
                    or abs(f["pose"]["sensor_sim_s"] - f["stamp_ns"] / 1e9) > 0.04
                    for f in frames
                ):
                    raise ValueError("Image or measured camera pose outside live history")
            replay = host.forecast(req, folder)
            if replay["status"] != receipt["status"] or (
                receipt["status"] == "computed"
                and not same(
                    without_timing(replay["forecast"]), without_timing(receipt["forecast"])
                )
            ):
                raise ValueError("Live image forecast did not reproduce")
            if receipt["status"] == "computed":
                matched = [e for e in calls if e["sequence"] == req["sequence"]]
                if (
                    len(matched) != 1
                    or matched[0]["input_last_stamp_ns"]
                    != receipt["forecast"]["input_last_stamp_ns"]
                ):
                    raise ValueError("Missing actual inference event")
            requests.append(
                dict(
                    sequence=req["sequence"],
                    sim_s=req["observations"][-1]["sim_s"],
                    status=receipt["status"],
                    supported=receipt.get("forecast", {}).get("supported"),
                    signal=signal,
                    action=stage,
                    baseline_action=baseline,
                    action_changed=stage != baseline,
                    host_seconds=receipt.get("host_seconds"),
                )
            )
        host.close()
        checks["live_rgb_predictions_reproduced"] = host.calls == len(calls) > 0
    checks["at_least_one_supported_forecast"] = any(r["supported"] is True for r in requests)
    return dict(
        status="passed" if all(checks.values()) else "failed",
        run_id=config["run_id"],
        checks=checks,
        inference_calls=len(calls),
        requests=requests,
        signals=dict(Counter(r["signal"] for r in requests)),
        supported=sum(r["supported"] is True for r in requests),
        action_changes_against_same_observations=sum(r["action_changed"] for r in requests),
        queue_summary=queue,
        mission_advantage_demonstrated=False,
        scope="Live CPU learned forecasts through shared deterministic Mission Assurance, fixed delivery-pad camera; not native ANWM/VLA, not onboard perception or real flight.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = verify(args.run)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))
    sys.exit(0 if output["status"] == "passed" else 1)
