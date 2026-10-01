#!/usr/bin/env python3
"""Fixed-weight VLA/WAM diagnostic on observed pad-camera histories.

No aircraft executor is imported. Native bins remain immutable. The two native
actions are zero translation or a 1--3 m level forward segment; their admission
does not imply that a delivery approach is safe or approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
PROMPT = (
    "<image>\nFly forward to the delivery pad on the ground. "
    "If another drone is above the pad, wait with zero forward motion. "
    "If the pad is empty, move forward. Stay level without turning.\nAction: "
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def modules():
    if __package__:
        from scripts import ship_aerovla, ship_aerovla_server, ship_anwm, ship_anwm_server
    else:
        import ship_aerovla
        import ship_aerovla_server
        import ship_anwm
        import ship_anwm_server
    return ship_aerovla, ship_aerovla_server, ship_anwm, ship_anwm_server


def pad_grammar(native, tokenizer):
    grammar = native.ActionGrammar(tokenizer, vertical_bins=(49, 49), yaw_bins=(49, 49))
    grammar.bin_sets[0] = {"0"} | {str(i) for i in range(20, 59)}
    grammar.translation_only = True
    grammar.policy = "aerovla_pad_wait_or_step.v1"
    return grammar


def proposal_kind(proposal):
    if proposal.get("kind") != "motion_proposal":
        return "reject"
    bins = proposal["bins"]
    if bins == [0, 49, 49]:
        return "wait"
    if 20 <= bins[0] <= 58 and bins[1:] == [49, 49]:
        return "short_forward_step"
    return "reject"


def prepare(source, output):
    sys.path.insert(0, str(REPO))
    from src.runtime.yokohama_native import load_capture

    if json.loads((source / "capture-result.json").read_text())["status"] != "passed":
        raise ValueError("Camera acquisition did not pass")
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((source / "config.json").read_text())
    cases = []
    for case in config["diagnostic_cases"]:
        key = case["id"]
        capture = source / key / "capture.json"
        record, arrays = load_capture(capture, appearance=True)
        dest = output / key
        dest.mkdir()
        np.savez_compressed(dest / "history.npz", **arrays)
        last = record["frames"][-1]
        for src, name in [("onboard_rgb_png", "rgb.png"), ("down_rgb_png", "down.png")]:
            shutil.copy2(capture.parent / last["assets"][src]["file"], dest / name)
        request = dict(
            schema="yokohama_pad_visual_probe.v1", case_id=key,
            source_run_id=config["run_id"], world_sha256=config["world"]["world_sha256"],
            capture_sha256=sha(capture), observed_at_sim_s=last["stamp_ns"] / 1e9,
            images_sha256={n: sha(dest / (n + ".png")) for n in ("rgb", "down")},
            history_sha256=sha(dest / "history.npz"), prompt=PROMPT,
            camera_kind="static diagnostic rig with aircraft camera transforms",
            future_ground_truth_used=False, dispatch_allowed=False,
        )
        write(dest / "request.json", request)
        cases.append({"id": key, "request_sha256": sha(dest / "request.json")})
    write(output / "manifest.json", dict(schema="yokohama_pad_probe_inputs.v1", cases=cases))


def verify_inputs(root):
    manifest = json.loads((root / "manifest.json").read_text())
    result = []
    for entry in manifest["cases"]:
        folder = root / entry["id"]
        if sha(folder / "request.json") != entry["request_sha256"]:
            raise ValueError("Request changed")
        request = json.loads((folder / "request.json").read_text())
        if request["prompt"] != PROMPT or request["dispatch_allowed"] is not False:
            raise ValueError("Diagnostic prompt/authority contract changed")
        for name in ("rgb", "down"):
            if sha(folder / (name + ".png")) != request["images_sha256"][name]:
                raise ValueError("Input image changed")
        if sha(folder / "history.npz") != request["history_sha256"]:
            raise ValueError("History changed")
        result.append((folder, request))
    return result


def vla(root, output, base, adapter):
    native, service, _, _ = modules()
    inputs = verify_inputs(root)
    output.mkdir(parents=True, exist_ok=False)
    model = service.NativeModel(base, adapter, cpu_between_requests=True)
    model.grammar = pad_grammar(native, model.tokenizer)
    write(output / "identity.json", dict(
        base_revision=native.BASE_REVISION, adapter_revision=native.ADAPTER_REVISION,
        adapter_sha256=native.ADAPTER_SHA256, grammar=model.grammar.policy,
        forward_bins=[0, *range(20, 59)], down_bins=[49], yaw_bins=[49],
        prompt=PROMPT, vla_source_sha256=sha(Path(service.__file__)),
        probe_source_sha256=sha(Path(__file__)), training=False,
    ))
    for folder, request in inputs:
        dest = output / folder.name
        dest.mkdir()
        images = [Image.open(folder / (n + ".png")).convert("RGB") for n in ("rgb", "down")]
        value, mosaic = model.predict(request["prompt"], images)
        (dest / "mosaic.png").write_bytes(mosaic)
        proposal = native.parse_proposal(value["generated_text"])
        value.update(
            request_sha256=sha(folder / "request.json"), proposal=proposal,
            proposal_kind=proposal_kind(proposal), vla_inference_invoked=True,
            dispatch_invoked=False, aircraft_flown=False, model_weights_updated=False,
        )
        write(dest / "result.json", value)
        print(json.dumps({"case": folder.name, "text": value["generated_text"], "kind": value["proposal_kind"]}), flush=True)


def wam(root, output, vla_results, upstream, checkpoint, adapter):
    _, _, native, service = modules()
    inputs = verify_inputs(root)
    output.mkdir(parents=True, exist_ok=False)
    model = service.NativeModel(upstream, checkpoint, cpu_between_requests=True, motion_adapter=adapter)
    write(output / "identity.json", dict(
        base_sha256=native.MODEL_SHA256, adapter_sha256=sha(adapter),
        upstream_revision=native.UPSTREAM_REVISION, source_sha256=sha(Path(service.__file__)),
        probe_source_sha256=sha(Path(__file__)), training=False,
        model_time_index=1, physical_future_time_calibration_verified=False,
    ))
    for folder, source in inputs:
        dest = output / folder.name
        dest.mkdir()
        vpath = vla_results / folder.name / "result.json"
        result = json.loads(vpath.read_text())
        if result["request_sha256"] != sha(folder / "request.json"):
            raise ValueError("VLA request binding mismatch")
        if result["proposal_kind"] == "reject":
            write(dest / "result.json", {"status": "skipped", "reason": "rejected_vla_proposal"})
            continue
        request = dict(
            schema_version=native.MOTION_CONTRACT,
            run_id=source["source_run_id"], world_sha256=source["world_sha256"],
            plan_sha256=sha(root / "manifest.json"), history_sha256=source["history_sha256"],
            source_kind="yokohama_rgbd_hold", ego_source="Gazebo_model_pose_simulator_ground_truth",
            delta_frame="body_frd_at_observation", num_timesteps=1, nominal_horizon_s=1,
            model_time_alignment_verified=False, future_ground_truth_used_for_forecast=False,
            dispatch_allowed=False, seed=42, diffusion_steps=250,
            adapter_sha256=native.MOTION_ADAPTER_SHA256, appearance_policy=native.APPEARANCE_POLICY,
            candidates=[{"id": "hold", "delta": [0, 0, 0, 0]},
                        {"id": "vla", "delta": result["proposal"]["requested_body_frd_delta_m_rad"]}],
            vla_response_sha256=sha(vpath),
        )
        shutil.copy2(folder / "history.npz", dest / "history.npz")
        write(dest / "request.json", request)
        request, arrays = native.validate(dest / "request.json")
        value = model.predict(request, arrays, dest)
        value.update(wam_inference_invoked=True, dispatch_invoked=False,
                     aircraft_flown=False, request_sha256=sha(dest / "request.json"))
        write(dest / "result.json", value)
        print(json.dumps({"case": folder.name, "wam": "completed"}), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["prepare", "vla", "wam"])
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--base", type=Path)
    p.add_argument("--adapter", type=Path)
    p.add_argument("--vla-results", type=Path)
    p.add_argument("--upstream", type=Path)
    p.add_argument("--checkpoint", type=Path)
    a = p.parse_args()
    if a.command == "prepare":
        prepare(a.inputs, a.output)
    elif a.command == "vla":
        vla(a.inputs, a.output, a.base, a.adapter)
    else:
        wam(a.inputs, a.output, a.vla_results, a.upstream, a.checkpoint, a.adapter)


if __name__ == "__main__":
    main()
