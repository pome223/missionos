#!/usr/bin/env python3
"""Opt-in indoor delivery episode with actual MuJoCo Go2 locomotion."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approval-ref")
    parser.add_argument("--approved-manifest", type=Path)
    parser.add_argument("--progress-every-s", type=float, default=10.0)
    parser.add_argument("--mission-id", default="go2-office-delivery")
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
    parser.add_argument("--simulate-recipient", action="store_true")
    parser.add_argument("--video", action="store_true")
    args = parser.parse_args()
    if os.getenv("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
        parser.error("requires RUN_MISSIONOS_GO2_DELIVERY_SIM=1")
    if not args.approval_ref and not args.approved_manifest:
        parser.error("requires the operator's simulation approval reference")
    if not 0.1 <= args.progress_every_s <= 60:
        parser.error("progress interval must be between 0.1 and 60 seconds")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty to preserve evidence")
    args.output.mkdir(parents=True, exist_ok=True)
    from src.runtime.go2_delivery_mission import Go2DeliveryMission, Go2DeliveryPlan, now
    from simulators.go2_delivery.mujoco_backend import Go2Physics, MuJoCoDeliveryClient

    plan = Go2DeliveryPlan(mission_id=args.mission_id)
    proposal_sha256 = None
    if args.approved_manifest:
        from src.runtime.go2_supervision import supervision_envelope

        envelope = json.loads(args.approved_manifest.read_text())
        proposal, approval = envelope["proposal"], envelope["approval"]
        proposal_sha256 = sha256(
            json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if (
            approval["approved_proposal_sha256"] != proposal_sha256
            or proposal["execution_target"] != "go2_mujoco_delivery"
            or proposal["terminal_hold_sim_s"] != 5.0
            or proposal["scenario"]
            not in (
                "baseline",
                "blocked_passage",
                "all_blocked",
                "temporary_blockage",
                "moving_obstacle",
            )
            or proposal["simulate_recipient"] is not True
            or (
                proposal["plan"].get("supervision_mode") == "agent"
                and proposal.get("supervision_envelope") != supervision_envelope()
            )
            or proposal["input_sha256"]
            != {
                "model": sha256(args.model.read_bytes()).hexdigest(),
                "policy": sha256(args.policy.read_bytes()).hexdigest(),
            }
        ):
            parser.error("approved manifest or simulator inputs do not match")
        plan = Go2DeliveryPlan(**proposal["plan"])
        args.approval_ref = approval["operator_approval_ref"]
        args.scenario = proposal["scenario"]
        args.simulate_recipient = proposal["simulate_recipient"]
    plan.validate()
    cancel_path = args.output.parent / f"{plan.mission_id}.cancel"

    video = None
    mission = None
    physics = None
    events = (args.output / "events.jsonl").open("w")
    telemetry = (args.output / "telemetry.jsonl").open("w")

    def emit(event):
        if event["event"] == "terminal_hold_started":
            client.phase = "Verifying return"
        item = dict(
            event, observed_at=now(), sim_time_s=float(physics.data.time) if physics else 0.0
        )
        events.write(json.dumps(item) + "\n")
        events.flush()
        print(
            json.dumps(
                item
                if event["event"].startswith(("go2_supervision_", "dynamic_"))
                else {
                    k: v
                    for k, v in item.items()
                    if k
                    in ("event", "reason", "status", "sim_time_s", "leg", "attempt", "passages")
                }
            ),
            flush=True,
        )

    last_print = -10.0

    def observe(state, client):
        nonlocal last_print
        telemetry.write(json.dumps(dict(state, phase=client.phase)) + "\n")
        if video:
            video.frame(state, client)
        if state["sim_time_s"] - last_print >= args.progress_every_s:
            last_print = state["sim_time_s"]
            print(
                json.dumps(
                    dict(
                        sim_time_s=round(last_print, 1),
                        phase=client.phase,
                        xy=state["ground_truth_xy"],
                        height_m=state["height_m"],
                        measured_speed_mps=state["measured_speed_mps"],
                        moving_obstacle=state["moving_obstacle"],
                        local_avoidance_state=state["local_avoidance_state"],
                    )
                ),
                flush=True,
            )

    try:
        physics = Go2Physics(args.model, args.policy)
        client = MuJoCoDeliveryClient(physics, emit=emit, observe=observe, scenario=args.scenario)
        client.cancel_requested = cancel_path.exists if args.approved_manifest else lambda: False
        client.metadata = dict(
            client.metadata,
            model_sha256=sha256(args.model.read_bytes()).hexdigest(),
            policy_sha256=sha256(args.policy.read_bytes()).hexdigest(),
            mujoco_version=physics.mj.__version__,
            torch_version=physics.torch.__version__,
            supervision_mode=plan.supervision_mode,
        )
        if args.video:
            from simulators.go2_delivery.recording import DeliveryVideo

            video = DeliveryVideo(physics, args.output)
        client.wait(3.0)
        supervisor = None
        if plan.supervision_mode == "agent":
            if not args.approved_manifest:
                raise ValueError("Agent supervision requires the Gateway-approved manifest")
            from src.runtime.go2_supervision import FileSupervisor

            supervisor = FileSupervisor(args.output, emit)
        mission = Go2DeliveryMission(
            plan,
            client,
            operator_approval_ref=args.approval_ref,
            approved_plan_sha256=plan.digest,
            emit=emit,
            supervisor=supervisor,
        )
        if mission.start():
            client.phase = "Awaiting receipt"
            client.wait(3.0)
            if client.cancel_requested():
                mission.status = "canceled"
                mission.record("operator_cancel_observed")
            elif args.simulate_recipient:
                mission.confirm_receipt(
                    dict(
                        mission_id=plan.mission_id,
                        parcel_id=plan.parcel_id,
                        destination=plan.destination,
                        received=True,
                        source="simulation_recipient",
                        issued_at=now(),
                    )
                )
        client.phase = (
            "Completed"
            if mission.status == "completed"
            else mission.status.replace("_", " ").title()
        )
        if mission.status not in ("completed", "awaiting_receipt", "returned_undelivered"):
            client.wait(5.0)
        state = client.read_state()
        if client.cancel_requested():
            stopped = state["base_stable"] and state["measured_speed_mps"] < 0.06
            mission.status = "canceled" if stopped else "needs_attention"
            mission.record(
                "operator_cancel_stopped" if stopped else "operator_cancel_stop_unverified",
                state=state,
            )
            client.phase = "Canceled" if stopped else "Needs attention"
        if video:
            video.frame(state, client)
        result = dict(
            mission.summary(),
            scenario=args.scenario,
            terminal_state=state,
            dynamic_avoidance=client.dynamic_summary(),
            terminal_hold_sim_s=5.0,
            raw_telemetry="telemetry.jsonl",
            raw_events="events.jsonl",
            approved_proposal_sha256=proposal_sha256,
        )
        root = Path(__file__).resolve().parent.parent
        sources = (
            "scripts/run_go2_delivery.py",
            "src/runtime/go2_delivery_mission.py",
            "src/runtime/go2_delivery_navigation.py",
            "simulators/go2_delivery/mujoco_backend.py",
            "simulators/go2_delivery/recording.py",
            "simulators/go2_delivery/dynamic_obstacles.py",
            "src/runtime/go2_supervision.py",
        )
        result["runtime_source_sha256"] = {
            p: sha256((root / p).read_bytes()).hexdigest() for p in sources
        }
    except Exception as exc:
        if mission:
            mission.status = "needs_attention"
            result = dict(mission.summary(), error=str(exc))
        else:
            result = dict(
                status="simulator_failed",
                completion_claimed=False,
                physical_execution_invoked=False,
                error=str(exc),
            )
        import traceback

        traceback.print_exc()
    finally:
        if video:
            video.close()
        events.close()
        telemetry.close()
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                status=result["status"],
                completion_claimed=result["completion_claimed"],
                result=str(args.output / "result.json"),
            )
        ),
        flush=True,
    )
    return 0 if result["status"] in ("completed", "awaiting_receipt") else 2


if __name__ == "__main__":
    sys.exit(main())
