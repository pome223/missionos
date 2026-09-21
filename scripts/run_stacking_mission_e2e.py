"""Opt-in governed stacking executor: no motion before a bound dispatch receipt.

Requires an explicitly supplied staged stacking simulator module directory.
That environment, learned VLA, and model weights are not shipped in this repo.
"""

from __future__ import annotations

import argparse
import importlib
import hashlib
from urllib.error import HTTPError
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen


def post(url, route, value):
    with urlopen(
        Request(
            url + route,
            data=json.dumps(value, allow_nan=False).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=300,
    ) as response:
        return json.load(response)


def acwm_current_image(env, np, mujoco):
    """Registered #110 camera, restored before any VLA observation or motor call."""
    model = env.sim.model._model
    cid = env.sim.model.camera_name2id("agentview")
    old = (
        model.cam_pos[cid].copy(),
        model.cam_quat[cid].copy(),
        float(model.cam_fovy[cid]),
    )
    pos, target = np.array([1.15, 0, 1.5]), np.array([0, 0, 1.0])
    back = (pos - target) / np.linalg.norm(pos - target)
    right = np.cross([0, 0, 1.0], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    quat = np.empty(4)
    mujoco.mju_mat2Quat(quat, np.column_stack([right, up, back]).ravel())
    try:
        model.cam_pos[cid], model.cam_quat[cid], model.cam_fovy[cid] = pos, quat, 50
        mujoco.mj_forward(model, env.sim.data._data)
        return np.ascontiguousarray(env.sim.render(240, 240, camera_name="agentview")[::-1])
    finally:
        model.cam_pos[cid], model.cam_quat[cid], model.cam_fovy[cid] = old
        mujoco.mj_forward(model, env.sim.data._data)


def predictor_inputs(game, env, obs, place, accepted, health):
    if health["binding"]["input_schema"] == "stacking.current_image_macro.v1":
        # Rendering refreshes MuJoCo's derived kinematics. Capture numerical
        # state afterward, matching the online benchmark's observation order.
        frame = acwm_current_image(env, game.np, game.mujoco)
        state = game.gate_inputs(env, obs, place, accepted)
        state = {k: state[k] for k in ("objects", "velocity", "physics", "robot", "count", "plan")}
        state["image"] = frame
        return state
    return game.gate_inputs(env, obs, place, accepted)


def predictor_options(health):
    if health["binding"]["input_schema"] == "stacking.current_image_macro.v1":
        return [
            {
                "option_id": "continue",
                "horizon_seconds": 14.2,
                "parameters": {
                    "macro": "smolvla-physical-tower-placement-284-v1",
                    "readout_sha256": health["readout_sha256"],
                },
            }
        ]
    return [
        {
            "option_id": k,
            "horizon_seconds": h,
            "parameters": {"macro": "stacking.fixed_vla_placement.v1"},
        }
        for k, h in [("continue", health["continue_horizon_seconds"]), ("bank", 14.2)]
    ]


def run(args):
    if not args.allow_simulator:
        raise ValueError("simulator execution requires --allow-simulator")
    sys.path.insert(0, str(args.simulator_module_dir.resolve()))
    game = importlib.import_module("game")
    sim = importlib.import_module("sim")
    np = game.np
    sim.ROOT = args.output_root
    sim.RPC = args.output_root / "rpc"
    game.ROOT = args.output_root
    with urlopen(args.service_url + "/health", timeout=10) as response:
        health = json.load(response)
    if health.get("governed_mission") is not True:
        raise ValueError("governed mission service required")
    if health["binding"]["policy_sha256"] != args.policy_sha256:
        raise ValueError("service/VLA binding mismatch")
    seed = args.seed
    np.random.seed(seed)
    rng = np.random.default_rng(seed + 700000)
    cfg = game.configuration(seed)
    env = game.PhysicalTower(cfg)
    obs = env.reset()
    env.park()
    folder = args.output_root / "episodes" / str(seed)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "physics.json").write_text(json.dumps(cfg, indent=2))
    xy = rng.uniform(-0.004, 0.004, 2)
    heights = np.array([c["half_size"][2] * 2 for c in cfg])
    zs = 0.8 + np.cumsum(heights) - heights / 2 + 0.008
    accepted = []
    events = []
    score = None
    try:
        for i in range(10):
            step = folder / f"step-{i + 1:02}"
            step.mkdir()
            place = np.r_[xy, zs[i]]
            x = predictor_inputs(game, env, obs, place, accepted, health)
            np.savez(step / "input.npz", **x)
            request = {
                "request_id": f"game-{seed}-step-{i + 1}",
                "seed": seed,
                "state_revision": i + 1,
                "observation_id": f"sim:{seed}:{i + 1}",
                "observed_at": time.time(),
                "binding": health["binding"],
                "state": {k: v.tolist() for k, v in x.items()},
                "options": predictor_options(health),
            }
            decision = post(args.service_url, "/decide", request)
            (step / "decision.json").write_text(json.dumps(decision, indent=2))
            option = decision["selected_option"]
            current = predictor_inputs(game, env, obs, place, accepted, health)
            np.savez(step / "dispatch-input.npz", **current)

            def digest(value):
                return hashlib.sha256(
                    json.dumps(
                        value, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode()
                ).hexdigest()

            dispatch_request = {
                "request_id": request["request_id"],
                "decision_sha256": digest(decision),
                "approval_policy_sha256": decision["approval_policy_sha256"],
                "observation_id": request["observation_id"],
                "state_revision": i + 1,
                "state_sha256": digest({k: v.tolist() for k, v in current.items()}),
                "observed_at": time.time(),
            }
            if i == 0:
                probes = []
                for key, value in [
                    ("approval_policy_sha256", "missing"),
                    ("state_revision", -1),
                    ("observed_at", time.time() - 400),
                ]:
                    before = float(env.sim.data.time)
                    try:
                        post(
                            args.service_url,
                            "/dispatch",
                            dispatch_request | {key: value},
                        )
                    except HTTPError as error:
                        assert error.code == 400
                        probes.append({"mutation": key, "status": 400, "motor_calls": 0})
                    else:
                        raise AssertionError("invalid dispatch accepted")
                    assert float(env.sim.data.time) == before
                (step / "rejection-probes.json").write_text(json.dumps(probes, indent=2))
            dispatch = post(args.service_url, "/dispatch", dispatch_request)
            assert dispatch["dispatch_authority_created"] and dispatch["option"] == option
            (step / "dispatch.json").write_text(json.dumps(dispatch, indent=2))
            started = time.time()
            obs, result, actions = game.transition(env, obs, seed, place, accepted, option)
            np.save(step / "selected-actions.npy", actions)
            (step / "selected-outcome.json").write_text(json.dumps(result, indent=2))
            outcome = {
                "request_id": request["request_id"],
                "request_sha256": decision["request_sha256"],
                "observation_id": request["observation_id"],
                "option_id": option,
                "role": "selected_execution",
                "outcome_ref": f"sim:{seed}:{i + 1}:selected",
                "result": result,
                "ticket": dispatch["ticket"],
                "runtime_invocation": {
                    "invocation_kind": "simulator_motor_loop",
                    "motor_steps": len(actions),
                    "started_at": started,
                    "completed_at": time.time(),
                    "actions_sha256": hashlib.sha256(actions.tobytes()).hexdigest(),
                },
            }
            receipt = post(args.service_url, "/observe", outcome)
            event = {
                "step": i + 1,
                "decision": decision,
                "selected_outcome": result,
                "receipt": receipt,
            }
            if option == "bank":
                score = result["score"]
                events.append(event)
                break
            future = None
            if i == 9 and not result["collapsed"]:
                started = time.time()
                future, hold_actions = game.future_after_wait(env, obs, seed, place, result)
                (step / "terminal-hold.json").write_text(json.dumps(future, indent=2))
                np.save(step / "terminal-hold-actions.npy", hold_actions)
                event["hold_receipt"] = post(
                    args.service_url,
                    "/observe",
                    outcome
                    | {
                        "role": "terminal_hold",
                        "outcome_ref": f"sim:{seed}:{i + 1}:terminal_hold",
                        "result": future,
                        "runtime_invocation": {
                            "invocation_kind": "simulator_motor_loop",
                            "motor_steps": len(hold_actions),
                            "started_at": started,
                            "completed_at": time.time(),
                            "actions_sha256": hashlib.sha256(hold_actions.tobytes()).hexdigest(),
                        },
                    },
                )
            events.append(event)
            if result["collapsed"] or result["technical_failure"]:
                score = result["score"]
                break
            accepted = result["references_after"]
            if i == 9:
                score = future["score"]
        record = {
            "seed": seed,
            "score": score,
            "events": events,
            "terminal_hold_steps": 284,
            "integration_not_independent_capability_evaluation": True,
            "model_binding": health["binding"],
            "llm_invoked": True,
            "bounded_preapproval": True,
            "executor_path": "MissionOS governed stacking simulator adapter",
            "simulator_execution": True,
            "physical_execution_invoked": False,
        }
        (folder / "result.json").write_text(json.dumps(record, indent=2))
        print(
            json.dumps({"seed": seed, "score": score, "decisions": len(events)}),
            flush=True,
        )
    finally:
        env.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--simulator-module-dir", required=True, type=Path)
    p.add_argument("--output-root", required=True, type=Path)
    p.add_argument("--service-url", required=True)
    p.add_argument("--policy-sha256", required=True)
    p.add_argument("--seed", required=True, type=int)
    p.add_argument("--allow-simulator", action="store_true")
    run(p.parse_args())
