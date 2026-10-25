#!/usr/bin/env python3
"""One inert shared-outer FD/Gateway smoke; no plant or hosted inference."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/missionos-core/src"))
from scripts import smoke_starship_restored_native_host as base  # noqa: E402
from src.runtime import starship_restored_outer as outer, starship_restored_contract as contract  # noqa: E402
from src.runtime.starship_restored_return import RestoredReturnRequest  # noqa: E402
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy  # noqa: E402


def run_smoke(output):
    output = Path(output).resolve()
    outer.require(not output.exists(), "outer_smoke_fresh_output_required")
    output.mkdir(parents=True)
    directory = output / "success"
    (directory / "governance").mkdir(parents=True)
    sources = base._source_manifest()
    sources["scripts/smoke_starship_restored_outer.py"] = sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    original, _ = base.synthetic_origin()
    source = json.loads(original.source_record_bytes())
    source["source_sha256"] = sources
    encoded = base.native._wire(source)
    profile = (ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, profile, sources)
    initial = RestoredReturnRequest(
        origin, contract.RestoredCandidateSchedule(origin), "nominal", profile, sources
    )
    (directory / "profile.json").write_bytes(profile)
    (directory / "governance/closed-origin.json").write_bytes(encoded)
    context = {"run_id": "outer-smoke", "mission_id": "synthetic", "vehicle_id": "ship"}
    plan = base.native._hash({"source": sources, "synthetic_only": True})
    job = {
        "context": context,
        "plan_sha256": plan,
        "source_sha256": sources,
        "initial_request": initial.record(),
        "candidate_sha256": base.native._hash(
            {
                "fixed_synthetic_proposal": True,
                "next_time_s": contract.RestoredCandidateSchedule(origin).record()["next_orbit_s"],
            }
        ),
    }
    outer.save(directory / "job.json", job)
    approved = [False]

    def operator_step(host, token, transcript):
        if host.approval_host is None or host.approval_host.bridge.request is None or approved[0]:
            return
        status, pending = base.operator_http(address, context, token)
        request = pending["request"]
        outer.require(
            status == 200 and pending["resolvable"] is True, "outer_smoke_actual_request_required"
        )
        transcript.append({"method": "GET", "status": status, "response": pending})
        body = {
            "request_sha256": request["sha256"],
            "observed_state_sha256": request["observed_state_sha256"],
            "candidate_sha256": request["candidate_sha256"],
        }
        status, response = base.operator_http(
            address, context, token, request_id=request["request_id"], choice="approve", body=body
        )
        transcript.append(
            {
                "method": "POST",
                "status": status,
                "response": response,
                "not_real_human_review": True,
            }
        )
        outer.require(status == 200, "outer_smoke_http_approval_failed")
        approved[0] = True

    until = time.monotonic() + 25.0

    def budget():
        outer.require(time.monotonic() < until, "outer_smoke_wall_bound")

    def forbidden(*_a, **_k):
        raise AssertionError("outer_smoke_forbids_plant_and_models")

    from src.runtime import (
        starship_sixdof as dyn,
        starship_physics as physics,
        starship_sixdof_mission as mission,
    )
    from src.intelligence.starship_restored_agent import RestoredAgent
    from src.intelligence.starship_m2_broker import DeadlineTransport

    try:
        with ExitStack() as stack:
            for owner, name in (
                (dyn, "step"),
                (dyn, "_rhs"),
                (dyn, "_rk4"),
                (physics, "step"),
                (mission, "advance_plant"),
                (RestoredAgent, "assess"),
                (DeadlineTransport, "_invoke"),
            ):
                stack.enter_context(patch.object(owner, name, forbidden))
            service, address = stack.enter_context(base.loopback_gateway(output / "gateway"))
            receipt = outer.run_native_child(
                folder=directory,
                cwd=ROOT,
                context=context,
                plan_sha256=plan,
                source_sha256=sources,
                profile_raw_bytes=profile,
                integration_policy=RestoredIntegrationPolicy(2),
                mode="fixture",
                environment={"synthetic": True},
                service=service,
                worker_token_name="MISSIONOS_RESTORED_OUTER_SMOKE",
                operator_id="scripted-smoke-not-human",
                worker_command=lambda fds: [
                    sys.executable,
                    str(ROOT / "scripts/smoke_starship_restored_native_host.py"),
                    "--_worker",
                    str(directory),
                    "--fds",
                    json.dumps(fds),
                    "--scenario",
                    "success",
                ],
                check_wall=budget,
                check_resources=budget,
                operator_step=operator_step,
                agent_factory=base.FixedSmokeAgent,
            )
        worker = json.loads((directory / "worker-result.json").read_bytes())
        host = json.loads((directory / "host-record.json").read_bytes())
        checks = {
            "seven_real_inherited_channels": host["handshake"]["ack"] is not None,
            "scripted_real_loopback_http_approval": approved[0],
            "one_consumption_and_later_confirmation": worker["approval"]["status"]
            == "reservation_confirmed"
            and len(worker["updates"]) == 1,
            "synthetic_decision_not_inference": worker["decision_client"]["results"][0][
                "model_inference_invoked"
            ]
            is False,
            "source_unchanged": all(
                sha256((ROOT / name).read_bytes()).hexdigest() == value
                for name, value in sources.items()
            ),
            "worker_wall_preserved": all(
                0
                <= row["received_wall_s"]
                - row["frame"]["runtime_state"]["observation"]["wall_time_s"]
                <= 5.0
                for row in host["telemetry"]
            ),
        }
        result = {
            "schema": "missionos.starship_restored_outer_smoke.v1",
            "passed": all(checks.values()),
            "checks": checks,
            "source_sha256": sources,
            "plan_sha256": plan,
            "host_record_sha256": receipt["host_record_sha256"],
            "plant_steps": 0,
            "hosted_model_calls": 0,
            "operator_is_scripted_not_human_review": True,
            "no_flight_or_live_admission_proof": True,
        }
        outer.save(output / "summary.json", result)
        outer.require(result["passed"], "outer_smoke_check_failed")
        return result
    except BaseException as error:
        outer.save(
            output / "failed.json",
            {
                "exception_type": type(error).__name__,
                "reason": str(error)[:160],
                "retry": False,
                "plant_steps": 0,
                "hosted_model_calls": 0,
            },
        )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-synthetic-boundary", action="store_true")
    args = parser.parse_args()
    if not args.run_synthetic_boundary:
        parser.error("explicit inert synthetic boundary opt-in required")
    result = run_smoke(args.output_dir)
    print(json.dumps({"passed": result["passed"], "plant_steps": 0, "hosted_model_calls": 0}))


if __name__ == "__main__":
    main()
