"""Persistent local Agent process. Credentials stay out of simulator containers."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def verify_readiness(folder):
    """Warm actual inference before scene creation, without inventing mission facts."""
    from src.intelligence.navigation_agents import infer

    started = time.monotonic()
    output, invocation = infer(
        "assurance", "readiness", {"purpose": "runtime_readiness_without_mission"}
    )
    receipt = dict(
        phase="service_readiness",
        elapsed_wall_s=time.monotonic() - started,
        output=output,
        invocation=invocation,
        mission_judgment=False,
        simulator_invoked=False,
        dispatch_authority_created=False,
    )
    (folder / "service-readiness.json").write_text(json.dumps(receipt, indent=2))
    if output != {"ready": True}:
        raise RuntimeError("model_readiness_not_confirmed")
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--operator-authorized-simulation", action="store_true")
    parser.add_argument("--approve-bounded-detour", action="store_true")
    parser.add_argument("--approve-observed-continue", action="store_true")
    a = parser.parse_args()
    if a.env_file:
        from dotenv import load_dotenv

        load_dotenv(a.env_file, override=False)
    from src.runtime.tb3_predictive_recovery import PredictiveRecovery

    # Load the actual ADK runtime before the simulator clock starts.
    from google.adk import Workflow  # noqa: F401
    from src.agents.model_config import resolve_agent_model

    from src.agents.model_config import deepseek_llm_backend_enabled

    for role in ("mission_assurance_agent", "missionos_turtlebot3_recovery_planner_agent"):
        if (
            deepseek_llm_backend_enabled(role)
            and not os.environ.get("DEEPSEEK_API_KEY", "").strip()
        ):
            raise RuntimeError("configured_DeepSeek_credentials_missing")
        resolve_agent_model(agent_name=role)
    verify_readiness(a.folder)
    controller = PredictiveRecovery(
        a.folder,
        operator_authorized=a.operator_authorized_simulation,
        detour_authorized=a.approve_bounded_detour,
        continue_authorized=a.approve_observed_continue,
    )
    print("RPC:" + json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("phase") not in (
                "plan",
                "post_wait",
                "failed_wait",
                "observed_continue",
            ):
                raise ValueError("unknown_agent_phase")
            result = getattr(controller, request["phase"])(request)
        except Exception as error:
            controller.records.append(
                {"phase": "error", "type": type(error).__name__, "message": str(error)[:300]}
            )
            controller.persist()
            result = {"status": "blocked", "reason": type(error).__name__}
        print("RPC:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
