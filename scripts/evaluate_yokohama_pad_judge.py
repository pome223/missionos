#!/usr/bin/env python3
"""Offline A/B of pad-judge arms on recorded Rules-entry moments; no simulator or GPU.

The reentry bundles hold real pad requests (5 s clear windows with camera advisory
receipts) and the captured lead trajectory. At every request where the Rules and the
existing advisory allowed entry, the ground truth is whether the lead re-entered the
pad exclusion radius within the horizon. Arms:

- ``rules``: always enter.
- ``kinematic``: wait only while the lead approaches (the same computed facts).
- ``deepseek_v1``: the first judge instruction and raw facts, as flown on 2026-09-30.
- ``deepseek_v2``: the current judge instruction and derived facts.

The DeepSeek arms run only with ``--live`` and the Gateway agent environment
(``RUN_MISSIONOS_YOKOHAMA_AGENTS=1`` and ``DEEPSEEK_API_KEY``). Proposals here grant
nothing; the ground truth uses hindsight that no arm sees.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import gzip
import json
import math
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_pad_advisory_contract import summarize  # noqa: E402
from src.runtime.yokohama_pad_queue import clearance, judge_situation  # noqa: E402
from src.runtime.yokohama_payload import digest  # noqa: E402

BUNDLES = ("docs/examples/yokohama-pad-reentry", "docs/examples/yokohama-pad-reentry-learning")
V1_AGENT = "missionos_yokohama_pad_judge_agent_v1"
V1_INSTRUCTION = """
A delivery drone is holding near a harbour delivery pad that another aircraft
recently used. Deterministic Rules have already observed the pad and approach
clear for the required window and would allow entry. You may only add a
bounded wait; you cannot authorize entry, change the destination or steer.
Use only the supplied facts: the other aircraft's distance from the pad and how
it changed over the window, its altitude, the camera advisory signal (a learned
estimate, not proof), battery, and the remaining wait budget. Choose wait when
the facts suggest the pad may be reoccupied soon; otherwise choose enter.
Return exactly these four JSON fields, without extra fields:
observation_id: copy the supplied id;
action: enter or wait;
wait_seconds: 0 for enter, 1 to remaining_wait_seconds for wait;
rationale: concise plain Japanese for the operator, without internal field names.
""".strip()


def v1_situation(config, request, response):
    """The facts the first judge received (commit 2d02b39a), kept for the A/B."""
    p = config["world"]["pad_queue"]
    rows = request["observations"]
    first, last = clearance(config, rows[0]), clearance(config, rows[-1])
    situation = dict(
        rules_action=response["proposed_action"],
        clear_window_s=round(rows[-1]["sim_s"] - rows[0]["sim_s"], 1),
        required_clear_window_s=p["stable_clear_sim_s"],
        lead_horizontal_distance_to_pad_m=round(last["horizontal_distance_m"], 1),
        lead_distance_change_over_window_m=round(
            last["horizontal_distance_m"] - first["horizontal_distance_m"], 1
        ),
        lead_altitude_m=round(rows[-1]["queue_lead"]["xyz"][2], 1),
        pad_exclusion_radius_m=p["pad_exclusion_radius_m"],
        battery_fraction=rows[-1].get("battery_fraction"),
    )
    advisory = response.get("advisory")
    if advisory:
        situation["camera_advisory"] = dict(
            signal=summarize(config, request, advisory),
            status=advisory.get("status"),
            supported=advisory.get("forecast", {}).get("supported"),
        )
    return situation


def points(bundle, horizon_s):
    """Recorded Rules-entry requests with hindsight ground truth."""
    config = json.loads((bundle / "capture-config.json").read_text())
    evaluation = json.loads((bundle / "evaluation/evaluation.json").read_text())
    config["run_id"] = "pad-reentry-offline-replay"
    config["operator_approval"] = "explicit offline fixture replay only; no aircraft dispatch"
    config["world"]["pad_state_advisory"] = dict(
        mode="assist",
        camera_entity="pad_state_camera",
        weights_sha256=evaluation["weights_sha256"]["existing"],
    )
    p = config["world"]["pad_queue"]
    captures = {}
    out = []
    with gzip.open(bundle / "evaluation/runtime-trace.jsonl.gz", "rt") as stream:
        for line in stream:
            record = json.loads(line)
            response = record["response"]
            if (
                record["method"] != "existing"
                or not record["case"].startswith("test")
                or response["proposed_action"] != "enter_delivery_approach"
            ):
                continue
            case = record["case"]
            if case not in captures:
                frames = json.loads((bundle / "capture" / f"{case}.json").read_text())["frames"]
                captures[case] = [
                    (f["stamp_ns"] / 1e9, math.dist(f["lead_pose"]["xyz"][:2], p["pad_xyz_m"][:2]))
                    for f in frames
                ]
            request = record["request"]
            now = request["observations"][-1]["sim_s"]
            future = [d for t, d in captures[case] if now < t <= now + horizon_s]
            v2 = judge_situation(config, request, response)
            out.append(
                dict(
                    bundle=bundle.name,
                    case=case,
                    sim_s=now,
                    reentered=any(d <= p["pad_exclusion_radius_m"] for d in future),
                    horizon_truncated=captures[case][-1][0] < now + horizon_s,
                    motion=v2["lead_motion"],
                    facts_v1=v1_situation(config, request, response),
                    facts_v2=v2,
                )
            )
    return out


def judge_request(facts):
    request = dict(
        observation_id="pad_judge_1",
        situation=facts,
        remaining_wait_seconds=28,
        decisions_remaining=1,
        allowed_actions=["enter", "wait"],
    )
    request["judge_request_id"] = digest(request)
    return request


def ask(agents, agent_name, request):
    try:
        evidence = agents._run(agent_name, "Delivery pad entry judge", request, 20)
    except Exception as exc:  # noqa: BLE001 - provider failures are recorded per call.
        return dict(status="unavailable", error_type=type(exc).__name__)
    guard = evidence["guardrail_result"]
    output = guard.get("validated_output", {}) if guard["guardrail_passed"] else {}
    if not agents.valid_decision(request, output):
        return dict(status="invalid", response_sha256=evidence.get("response_sha256"))
    return dict(
        status="valid",
        action=output["action"],
        wait_seconds=output["wait_seconds"],
        rationale=output["rationale"],
        model_id=evidence.get("model_id"),
        response_sha256=evidence.get("response_sha256"),
    )


def register_v1():
    from src.agents import missionos_agents as registry

    def build(*, model_id=None):
        agent = registry._agent(
            name=V1_AGENT,
            role="Delivery pad entry judge",
            model_id=model_id,
            instruction=V1_INSTRUCTION,
        )
        agent.generate_content_config.max_output_tokens = 512
        return agent

    registry.MISSIONOS_AGENT_BUILDERS[V1_AGENT] = build


def summarize_arms(rows, arms):
    groups = dict(
        reentry_visible=lambda r: r["reentered"] and r["motion"] == "approaching",
        reentry_not_visible=lambda r: r["reentered"] and r["motion"] != "approaching",
        no_reentry=lambda r: not r["reentered"],
    )
    summary = {}
    for arm in arms:
        summary[arm] = {}
        for name, member in groups.items():
            decided = [r[arm] for r in rows if member(r)]
            valid = [d for d in decided if d["status"] == "valid"]
            waits = [d for d in valid if d["action"] == "wait"]
            summary[arm][name] = dict(
                n=len(decided),
                valid=len(valid),
                wait=len(waits),
                mean_wait_seconds=round(sum(d["wait_seconds"] for d in waits) / len(waits), 1)
                if waits
                else 0,
            )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizon-s", type=float, default=60)
    parser.add_argument("--live", action="store_true", help="Call DeepSeek for the judge arms")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    rows = [r for b in BUNDLES for r in points(REPO / b, args.horizon_s)]
    for r in rows:
        r["rules"] = dict(status="valid", action="enter", wait_seconds=0)
        approaching = r["facts_v2"]["lead_motion"] == "approaching"
        r["kinematic"] = dict(
            status="valid",
            action="wait" if approaching else "enter",
            wait_seconds=10 if approaching else 0,
        )
    arms = ["rules", "kinematic"]
    if args.live:
        from src.intelligence import yokohama_delivery_agents as agents

        agents.configuration()
        register_v1()
        jobs = [
            (r, arm, name, judge_request(r[facts]))
            for r in rows
            for arm, name, facts in [
                ("deepseek_v1", V1_AGENT, "facts_v1"),
                ("deepseek_v2", agents.JUDGE, "facts_v2"),
            ]
        ]
        with ThreadPoolExecutor(args.workers) as pool:
            answers = list(pool.map(lambda job: ask(agents, job[2], job[3]), jobs))
        for (row, arm, _, _), answer in zip(jobs, answers):
            row[arm] = answer
        arms += ["deepseek_v1", "deepseek_v2"]
    args.output.mkdir(parents=True, exist_ok=True)
    result = dict(
        schema="yokohama_pad_judge_ab.v1",
        horizon_s=args.horizon_s,
        points=len(rows),
        summary=summarize_arms(rows, arms),
        truth="lead re-entered the pad exclusion radius within the horizon (hindsight only)",
        rows=[copy.deepcopy(r) for r in rows],
    )
    (args.output / "judge-ab.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
