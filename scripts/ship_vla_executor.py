"""Qualify a bounded fixture proposal through actual PX4 position/yaw execution."""

from __future__ import annotations

import hashlib
import json
import math
import time

if __package__:
    from src.runtime.ship_vla_adapter import (
        bind_proposal,
        content_hash,
        make_envelope,
        snapshot_from_px4,
    )
    from src.runtime.ship_vla_execution import (
        authorize_candidate,
        at_target,
        check_execution_sample,
        require_contract,
    )
    from .ship_vla_mavlink import PositionStream
else:
    from ship_vla_adapter import bind_proposal, content_hash, make_envelope, snapshot_from_px4
    from ship_vla_execution import (
        authorize_candidate,
        at_target,
        check_execution_sample,
        require_contract,
    )
    from ship_vla_mavlink import PositionStream


def execute_fixture(root, config, sample, event, run, binary, clock, *, provider=None):
    contract = require_contract(config)
    native = contract["proposal_source"] == "fresh_native_aerovla"
    if native != (provider is not None):
        raise ValueError("native_provider_contract_mismatch")
    receipt = {
        "schema_version": "ship_vla_execution.v1",
        "status": "failed",
        "samples": [],
        "proposal_source": contract["proposal_source"],
        "vla_inference_invoked": False,
        "physical_execution_invoked": False,
        "dispatch_invoked": False,
    }
    transport = None
    try:

        def permit_pair(*, input_row=None, inference=None):
            input_row = input_row if input_row is not None else sample()
            current_row = sample()
            proposal = bind_proposal(
                inference["response"]["generated_text"]
                if inference
                else contract.get("fixture_text", contract.get("prestream_fixture_text")),
                snapshot_from_px4(input_row, config),
                make_envelope(config),
            )
            permit = authorize_candidate(
                config,
                proposal,
                input_row,
                current_row,
                now_s=clock(),
                inference=inference,
                prestream=native and inference is None,
            )
            return {
                "input_elapsed_s": input_row["elapsed_s"],
                "current_elapsed_s": current_row["elapsed_s"],
                "proposal": proposal,
                "permit": permit,
            }, snapshot_from_px4(input_row, config)

        warm, initial = permit_pair()
        receipt["prestream"] = warm
        event(
            "vla_execution_started",
            proposal_source=contract["proposal_source"],
            controller_sha256=warm["permit"]["controller_sha256"],
        )
        run(
            [
                binary + "mavlink",
                "start",
                "-u",
                "14606",
                "-r",
                "400000",
                "-t",
                "127.0.0.1",
                "-o",
                "14656",
                "-m",
                "onboard",
            ]
        )
        transport = PositionStream(
            root,
            initial["position_ned_m"],
            initial["heading_ned_rad"],
            clock,
            warm["permit"]["expires_at_s"],
            content_hash(warm["permit"]),
        )
        transport.start()
        receipt["dispatch_invoked"] = True

        def observe(stage, position, yaw, permit, initial, modes):
            transport.update(position, yaw)
            row = sample()
            state = check_execution_sample(row, initial, permit, config, modes=modes)
            receipt["samples"].append(
                {"stage": stage, "elapsed_s": row["elapsed_s"], "row_sha256": content_hash(row)}
            )
            if transport.error:
                raise RuntimeError(transport.error)
            return state, row

        end = clock() + contract["prestream_s"]
        while clock() < end:
            observe(
                "prestream",
                initial["position_ned_m"],
                initial["heading_ned_rad"],
                warm["permit"],
                initial,
                {4},
            )
            time.sleep(0.1)
        # New observation and permit after prestream, immediately before mode send.
        if native:
            input_row = sample()

            def hold_tick():
                return observe(
                    "prestream",
                    initial["position_ned_m"],
                    initial["heading_ned_rad"],
                    warm["permit"],
                    initial,
                    {4},
                )

            inference = provider.infer(input_row, hold_tick, clock)
            receipt["inference"] = inference
            receipt["vla_inference_invoked"] = (
                inference["response"].get("vla_inference_invoked") is True
            )
            selected, initial = permit_pair(input_row=input_row, inference=inference)
        else:
            selected, initial = permit_pair()
        receipt["execution"] = selected
        permit = selected["permit"]
        candidate = permit["candidate"]
        start, target, yaw = (
            candidate["start_ned_m"],
            candidate["target_ned_m"],
            candidate["target_heading_ned_rad"],
        )
        transport.update(start, initial["heading_ned_rad"], content_hash(permit))
        receipt["offboard_requested_at_s"] = clock()
        transport.command("offboard")
        end = clock() + 3
        while True:
            state, row = observe(
                "offboard", start, initial["heading_ned_rad"], permit, initial, {4, 14}
            )
            if state["nav_state"] == 14 and transport.accepted():
                receipt["offboard_observed_at_s"] = row["elapsed_s"]
                break
            if clock() > end:
                raise RuntimeError("offboard_ack_or_mode_not_observed")
            time.sleep(0.1)
        end = clock() + 5
        stable = None
        while True:
            state, row = observe("rotate", start, yaw, permit, initial, {14})
            okay = (
                abs(math.remainder(state["heading_ned_rad"] - yaw, 2 * math.pi))
                < contract["yaw_tolerance_rad"]
                and math.hypot(*state["velocity_ned_mps"]) < contract["settle_speed_mps"]
            )
            stable = (stable if stable is not None else row["elapsed_s"]) if okay else None
            if stable is not None and row["elapsed_s"] - stable >= 0.5:
                break
            if clock() > end:
                raise RuntimeError("yaw_alignment_not_observed")
            time.sleep(0.1)
        receipt["translation_started_at_s"] = clock()
        duration = math.dist(start, target) / contract["speed_mps"]
        while True:
            fraction = (
                min(1.0, (clock() - receipt["translation_started_at_s"]) / duration)
                if duration
                else 1.0
            )
            point = [a + fraction * (b - a) for a, b in zip(start, target)]
            observe("translate", point, yaw, permit, initial, {14})
            if fraction == 1.0:
                break
            time.sleep(0.1)
        stable = None
        while True:
            state, row = observe("settle", target, yaw, permit, initial, {14})
            okay = at_target(state, candidate, contract)
            stable = (stable if stable is not None else row["elapsed_s"]) if okay else None
            if stable is not None and row["elapsed_s"] - stable >= contract["settle_s"]:
                receipt["target_stable_at_s"] = row["elapsed_s"]
                break
            time.sleep(0.1)
        receipt["loiter_requested_at_s"] = clock()
        transport.command("loiter")
        stable = None
        end = clock() + 4
        while True:
            state, row = observe("loiter", target, yaw, permit, initial, {4, 14})
            okay = (
                state["nav_state"] == 4
                and transport.accepted()
                and at_target(state, candidate, contract)
            )
            stable = (stable if stable is not None else row["elapsed_s"]) if okay else None
            if stable is not None and row["elapsed_s"] - stable >= 1.0:
                receipt["loiter_stable_at_s"] = row["elapsed_s"]
                break
            if clock() > end:
                raise RuntimeError("loiter_handoff_not_observed")
            time.sleep(0.1)
        receipt["status"] = "observed_complete"
    except Exception as exc:
        receipt["failure"] = str(exc)
        raise
    finally:
        if transport is not None and receipt["status"] != "observed_complete":
            try:
                run([binary + "commander", "mode", "auto:loiter"])
                receipt["abort_loiter_requested"] = True
            except Exception as exc:
                receipt["abort_loiter_error"] = str(exc)
        if transport:
            transport.close()
        path = root / "vla-execution.json"
        path.write_text(json.dumps(receipt, indent=2, allow_nan=False))
    event("vla_execution_observed", receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
