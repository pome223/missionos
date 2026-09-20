#!/usr/bin/env python3
"""Opt-in hosted-model check of all seven active agents; synthetic proposals only."""

import argparse
import json
import os
from pathlib import Path
import time

from src.intelligence import missionos_agent_runtime as runtime


CASES = (
    ("status", "Report the current mission status and missing evidence only."),
    (
        "plan",
        "Propose a bounded hold response for a paused mission while awaiting a fresh observation.",
    ),
    (
        "repair",
        "Plan post-run repair after a fixture grasp failed. Propose evidence collection and a bounded next-run repair.",
    ),
    (
        "runtime_recovery",
        "Judge an in-flight recovery proposal: fresh telemetry reports battery warning and route deviation. No verified maneuver parameters are available; propose operator review or a bounded safe response.",
    ),
    (
        "mission_designer_plan",
        "Design a synthetic PX4 simulation scenario brief with a 0.2 kg payload and wind 2 m/s. Coordinates are unknown; keep them unknown.",
    ),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--intents", nargs="+", choices=[item[0] for item in CASES])
    args = parser.parse_args()
    if os.getenv("RUN_MISSIONOS_SEVEN_AGENT_GRAPH_SMOKE") != "1":
        parser.error("Set RUN_MISSIONOS_SEVEN_AGENT_GRAPH_SMOKE=1 for hosted inference")
    os.environ["MISSIONOS_AGENT_RUNTIME_ADK_ENABLED"] = "1"
    os.environ["MISSIONOS_ADK_V2_GRAPH_PRIMARY"] = "1"
    os.environ["MISSIONOS_ADK_V2_GRAPH_ROLLBACK"] = "0"
    args.output.mkdir(parents=True, exist_ok=True)
    runtime.ARTIFACT_ROOT = args.output / "receipts"
    seen = set()
    summary = []
    cases = [item for item in CASES if not args.intents or item[0] in args.intents]
    for intent, text in cases:
        started = time.perf_counter()
        result = runtime.run_missionos_agent_runtime(
            utterance=f"{text} This is a proposal-only synthetic evaluation. Use intent {intent}. Do not approve or execute anything.",
            missionos_state={
                "task_id": "synthetic_graph_smoke",
                "fixture_only": True,
                "approved": False,
                "execution_allowed": False,
            },
            route_hint=intent,
            timeout_seconds=60,
        )
        (args.output / f"{intent}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        graph = result.get("adk_v2_graph_result") or {}
        invocations = result.get("agent_invocations") or []
        expected = [
            "missionos_chief_agent",
            runtime._CHIEF_TO_SPECIALIST[intent],
            "missionos_safety_critic_agent",
        ]
        actual = [item["agent_name"] for item in invocations]
        passed = (
            result.get("runtime_status") == "proposal_guardrail_passed"
            and actual == expected
            and all(item.get("agent_node_execution") == "ctx.run_node" for item in invocations)
            and all(item.get("graph_run_id") == graph.get("graph_run_id") for item in invocations)
            and all(
                result.get(field) is False
                for field in (
                    "approval_created",
                    "dispatch_authority_created",
                    "executor_invoked",
                    "physical_execution_invoked",
                    "outcome_observed",
                    "progress_counted",
                )
            )
        )
        row = {
            "intent": intent,
            "passed": passed,
            "agent_sequence": actual,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "blocking_reasons": result.get("blocking_reasons", []),
        }
        summary.append(row)
        print(json.dumps(row), flush=True)
        if passed:
            seen.update(actual)
        else:
            break
    report = {
        "passed": len(summary) == len(cases) and all(x["passed"] for x in summary),
        "verified_agent_count": len(seen),
        "cases": summary,
        "synthetic_input": True,
        "hosted_llm_invoked": True,
        "executor_invoked": False,
    }
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
