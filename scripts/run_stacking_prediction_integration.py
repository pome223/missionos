"""Opt-in simulator executor for the common MissionOS prediction service.

Requires an explicitly supplied staged stacking simulator module directory.
That environment, learned VLA, and model weights are not shipped in this repo.
"""

from __future__ import annotations

import argparse
import importlib
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
        timeout=60,
    ) as response:
        return json.load(response)


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
            x = game.gate_inputs(env, obs, place, accepted)
            np.savez(step / "input.npz", **x)
            request = {
                "request_id": f"game-{seed}-step-{i + 1}",
                "observation_id": f"sim:{seed}:{i + 1}",
                "observed_at": time.time(),
                "binding": health["binding"],
                "state": {k: v.tolist() for k, v in x.items()},
                "options": [
                    {
                        "option_id": k,
                        "horizon_seconds": h,
                        "parameters": {"macro": "stacking.fixed_vla_placement.v1"},
                    }
                    for k, h in [("continue", health["continue_horizon_seconds"]), ("bank", 14.2)]
                ],
            }
            decision = post(args.service_url, "/decide", request)
            (step / "decision.json").write_text(json.dumps(decision, indent=2))
            option = decision["selected_option"]
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
            # Measured placement+hold branch, explicitly distinct from the selected 14.2s action.
            end = game.snapshot(env)
            future, hold_actions = game.future_after_wait(env, obs, seed, place, result)
            (step / "counterfactual-hold.json").write_text(json.dumps(future, indent=2))
            np.save(step / "counterfactual-hold-actions.npy", hold_actions)
            event["hold_receipt"] = post(
                args.service_url,
                "/observe",
                outcome
                | {
                    "role": "counterfactual_hold",
                    "outcome_ref": f"sim:{seed}:{i + 1}:placement_then_hold",
                    "result": future,
                },
            )
            obs = game.restore(env, end)
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
            "llm_invoked": False,
            "simulator_execution": True,
            "physical_execution_invoked": False,
        }
        (folder / "result.json").write_text(json.dumps(record, indent=2))
        print(json.dumps({"seed": seed, "score": score, "decisions": len(events)}), flush=True)
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
