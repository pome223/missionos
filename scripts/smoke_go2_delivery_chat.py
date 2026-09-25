#!/usr/bin/env python3
"""Opt-in real Gateway chat -> approval -> Go2 physics -> verified outcome smoke.

Running this command with the opt-in flag authorizes one simulated delivery.
It also checks that missing approval and foreign/tampered context cannot start it.
The Gateway must have a prepared Go2 cache and the same simulator opt-in enabled.
"""

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse
import uuid

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=(
            "baseline",
            "blocked_passage",
            "all_blocked",
            "temporary_blockage",
            "moving_obstacle",
        ),
        default="baseline",
    )
    parser.add_argument("--supervision-mode", choices=("rules", "agent"), default="rules")
    parser.add_argument("--cancel-after-motion", action="store_true")
    parser.add_argument("--cancel-during-yield", action="store_true")
    args = parser.parse_args()
    if args.cancel_during_yield and (
        args.scenario != "moving_obstacle" or args.cancel_after_motion
    ):
        parser.error("cancel-during-yield requires moving_obstacle and no other cancel option")
    if os.environ.get("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
        parser.error("requires RUN_MISSIONOS_GO2_DELIVERY_SIM=1")
    if urlparse(args.gateway_url).hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("the runtime smoke requires a loopback Gateway")
    if args.output.exists():
        parser.error("use a new output file to preserve evidence")
    session = "go2-chat-smoke-" + uuid.uuid4().hex
    headers = (
        {"X-API-Key": os.environ["GATEWAY_API_KEY"]} if os.environ.get("GATEWAY_API_KEY") else {}
    )
    client = httpx.Client(base_url=args.gateway_url, headers=headers, trust_env=False, timeout=20)
    context = {}
    exchanges = []

    def send(instruction, *, owner=session, binding=None):
        response = client.post(
            "/missionos/autonomy-conversation/run",
            json={
                "operator_instruction": instruction,
                "session_id": owner,
                "missionos_client_surface": "chat",
                "go2_scenario": args.scenario,
                "go2_supervision_mode": args.supervision_mode,
                "mission_designer_context": context if binding is None else binding,
            },
        )
        response.raise_for_status()
        payload = response.json()
        assert payload["routing_source"] == "go2_delivery_fixed_catalog"
        exchanges.append({"instruction": instruction, "response": payload})
        return payload

    proposal = send("会議室Aへ届けて")["mission_designer"]
    identity = proposal["summary"]["task_id"]
    context = {
        key: proposal[key]
        for key in (
            "mission_designer_context_ref",
            "mission_designer_context_sha256",
            "mission_designer_context_session_id",
        )
    }
    assert proposal["summary"]["status"] == "proposed"
    assert send("/run")["routed_action"] == "clarification"
    assert send("/approve", owner="foreign-session")["routed_action"] == "clarification"
    forged = dict(context, mission_designer_context_sha256="0" * 64)
    assert send("/approve", binding=forged)["routed_action"] == "clarification"
    assert send("/status")["mission_designer"]["summary"]["status"] == "proposed"
    assert send("/approve")["mission_designer"]["summary"]["status"] == "approved"
    for _ in range(2):
        summary = send("/run")["mission_designer"]["summary"]
        assert summary["task_id"] == identity
        assert summary["status"] in ("starting", "running", "completed")
    canceled = False
    deadline = time.monotonic() + 300
    phase = None
    while time.monotonic() < deadline:
        summary = send("/status")["mission_designer"]["summary"]
        assert summary["task_id"] == identity
        if summary["phase"] != phase:
            phase = summary["phase"]
            print(identity, phase, flush=True)
        if summary["status"] not in ("starting", "running", "cancel_requested"):
            break
        snapshot = summary.get("snapshot", {})
        xy = snapshot.get("xy") or [-2.5, 0]
        cancel_now = (args.cancel_after_motion and abs(xy[0] + 2.5) > 0.3) or (
            args.cancel_during_yield and snapshot.get("local_avoidance_state") == "yielding"
        )
        if cancel_now and not canceled:
            request = send("/cancel")["mission_designer"]["summary"]
            assert request["status"] == "cancel_requested"
            assert not request["completion_claimed"]
            canceled = True
        time.sleep(0.5)
    else:
        send("/cancel")
        raise AssertionError("Gateway did not produce a terminal result within 300 seconds")
    result_response = client.get(f"/missionos/go2/{identity}/view/result.json")
    result_response.raise_for_status()
    result = result_response.json()
    assert result["mission_id"] == identity
    assert result["approved_proposal_sha256"] == proposal["go2_proposal_sha256"]
    assert result["physical_execution_invoked"] is False
    if args.supervision_mode == "rules" or args.scenario in ("baseline", "moving_obstacle"):
        assert result["llm_judgment_invoked"] is False
    elif not (args.cancel_after_motion or args.cancel_during_yield):
        assert result["llm_judgment_invoked"] is True
        assert 1 <= len(result["supervision_decisions"]) <= 3
        for decision in result["supervision_decisions"]:
            assert decision["invocation"]["standalone_runner_invoked"] is True
            assert decision["invocation"]["model_id"] == "deepseek-flash"
            assert not decision["rule_blocking_reasons"]
    events = [event["event"] for event in result["events"]]
    if args.cancel_after_motion or args.cancel_during_yield:
        assert canceled and result["status"] == "canceled"
        assert "operator_cancel_stopped" in events
        assert not result["completion_claimed"]
        assert result["terminal_state"]["measured_speed_mps"] < 0.06
        if args.cancel_during_yield:
            dynamic = result["dynamic_avoidance"]
            assert dynamic["yield_count"] >= 1 and dynamic["contact_physics_steps"] == 0
            assert result["terminal_state"]["local_avoidance_state"] == "yielding"
    elif args.scenario == "all_blocked" and args.supervision_mode == "agent":
        assert result["status"] == "returned_undelivered"
        assert not result["completion_claimed"] and not result["receipt"]
        assert "undelivered_return_verified" in events
    elif (
        args.scenario in ("all_blocked", "temporary_blockage") and args.supervision_mode == "rules"
    ):
        assert result["status"] == "needs_attention"
        assert not result["completion_claimed"] and not result["receipt"]
    else:
        assert result["status"] == "completed" and result["completion_claimed"]
        assert events.index("terminal_hold_verified") < events.index("mission_completed")
        assert result["receipt"]["source"] == "simulation_recipient"
        if args.scenario == "moving_obstacle":
            dynamic = result["dynamic_avoidance"]
            assert dynamic["enabled"] and dynamic["obstacle_travel_m"] > 3.0
            assert dynamic["contact_physics_steps"] == 0
            assert dynamic["minimum_conservative_clearance_m"] > 0
            assert dynamic["yield_count"] >= 1
            devents = [e["event"] for e in dynamic["events"]]
            assert (
                devents.index("dynamic_yield_requested")
                < devents.index("dynamic_stop_observed")
                < devents.index("dynamic_path_clear_observed")
            )
        if args.scenario == "blocked_passage":
            assert events.count("bounded_retry_selected") == 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "task_id": identity,
                "scenario": args.scenario,
                "supervision_mode": args.supervision_mode,
                "cancel_after_motion": canceled and args.cancel_after_motion,
                "cancel_during_yield": canceled and args.cancel_during_yield,
                "checks_passed": True,
                "exchanges": exchanges,
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "task_id": identity,
                "status": result["status"],
                "checks_passed": True,
                "sim_time_s": result["terminal_state"]["sim_time_s"],
            }
        ),
        flush=True,
    )
    client.close()


if __name__ == "__main__":
    main()
