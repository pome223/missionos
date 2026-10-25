"""Exercise the real generator/scheduler/supervisor return-value seam.

The two finite plant advances are deliberately mocked. This is a short control
flow smoke, not a flight, recovery result, injected-fault trial or model call.
"""
from contextlib import ExitStack
from dataclasses import asdict, replace
import socket
import subprocess
from threading import Event
from unittest.mock import patch

import pytest

from src.runtime import starship_sixdof as dyn, starship_physics as physics
from src.runtime import starship_sixdof_mission as mission
from src.runtime.starship_restored_prefix import RestoredPrefixRuntime
from src.runtime.starship_experimental_continuation import development_settings
from src.runtime.starship_retained_return import TRIMMED_POLICY_ID
from src.runtime.starship_final_protocol import digest
from src.runtime.starship_final_prefix_supervision import FinalPrefixSupervision
from src.runtime.starship_final_verifier import verify_prefix_supervision
from tests.contract.test_starship_final_prefix_supervision import boundary
from tests.contract.test_starship_restored_prefix import setup as existing_setup


def exercise_concurrent_witness_boundary():
    """Run exactly two mocked advances through actual production control flow.

    The first synthetic advance marks one actuator unavailable. The real
    supervisor notices that current availability change on the second tick;
    its real numerical projection/RPC/fixed decoder create the actual request.
    A synthetic terminal receipt bounds the test to two steps. No simulator
    dynamics or provider transport may execute.
    """
    from src.intelligence.starship_m2_broker import DeadlineTransport

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Witness seam smoke cannot integrate or call providers")

    with ExitStack() as patches:
        for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"),
            (physics, "step"), (socket, "socket"), (socket, "create_connection"),
            (subprocess, "Popen"), (DeadlineTransport, "_invoke")):
            patches.enter_context(patch.object(owner, name, forbidden))
        with boundary(("continue_mission",)) as (original_observer, host, transport, clock, profile, source, _):
            context = {"run_id": "prefix-test", "mission_id": "witness-seam", "vehicle_id": "fixture-stack"}
            inputs = {"context": context, "plan_sha256": "a"*64, "source_sha256": source, "mode": "fixture"}
            observer = FinalPrefixSupervision(profile, run_id=context["run_id"], plan_sha256=inputs["plan_sha256"],
                source_sha256=source, scope_sha256=digest({"context": context, "phase": "prefix", "plan": inputs["plan_sha256"]}),
                client=original_observer.client, mode="fixture", clock=clock)
            inputs["supervision_policy"] = observer.policy.record()
            raw, _, settings, controls, _, sites, goal = existing_setup.__wrapped__()
            driver = RestoredPrefixRuntime(raw, source, settings, controls, 7500.,
                lambda _summary: "continue_deployment", final_prefix_supervision=observer)
            actual_observe = observer.observe_ascent
            plant_inputs, tick_inputs = [], []

            def complete_fixed_rpc(state, vehicle, phase, **kwargs):
                result = actual_observe(state, vehicle, phase, **kwargs)
                if observer.pending_epoch is None:
                    return result
                for _ in range(1000):
                    host.tick()
                    result = actual_observe(state, vehicle, phase)
                    if observer.pending_epoch is None:
                        return result
                    Event().wait(.001)
                raise AssertionError("The real inherited-pipe fixed RPC did not finish")

            def tiny_plant(state, vehicle, command, dt, *_args):
                plant_inputs.append({"state": asdict(state), "vehicle": asdict(vehicle), "dt": dt})
                assert len(plant_inputs) <= 2, "The tiny mock cannot become a full flight"
                engines = tuple(replace(row, available=False, throttle=0.) if index == 0 else row
                    for index, row in enumerate(state.engine_states))
                later = replace(state, time_s=state.time_s+dt, engine_states=engines,
                    propellant_kg=state.propellant_kg-1.)
                contact = {"contact": True, "surface_relative_speed_mps": 3.5} if len(plant_inputs) == 2 else None
                return later, contact

            patches.enter_context(patch.object(observer, "observe_ascent", complete_fixed_rpc))
            patches.enter_context(patch.object(mission, "advance_plant", tiny_plant))
            result = mission.simulate_concurrent(profile, duration_s=7500., return_policy=TRIMMED_POLICY_ID,
                _development_return_qualification=development_settings(), return_sites=sites, splashdown_goal=goal,
                experimental_ascent=settings, booster_terminal_recovery=controls, restored_runtime=driver,
                on_tick=lambda tick: tick_inputs.append({"state": asdict(tick.state), "phase": tick.phase}))
            host_sessions = host.record()["sessions"]
            client_sessions = observer.client.record()["completed"]
            exchanges = {row["frame"]["request"]["request_id"]: {"host": row,
                "client": next(item for item in client_sessions if item["frame"]["request_sha256"]
                    == row["frame"]["request_sha256"])} for row in host_sessions}
            return {"result": result, "supervision": observer.record(), "plant_inputs": plant_inputs,
                "tick_inputs": tick_inputs, "fixed_decode_calls": len(transport.calls),
                "consumer_inputs": inputs, "rpc": exchanges}


def assert_owned_nonempty_witnesses(evidence):
    result, observer = evidence["result"], evidence["supervision"]
    witnesses = result["final_supervision_actual_witnesses"]
    assert len(witnesses) == len(observer["requests"]) == evidence["fixed_decode_calls"] == 1
    witness, request = witnesses[0], observer["requests"][0]
    assert witness["request_ids"] == [request["request_id"]]
    assert witness["origin"] == "actual_plant_tick_before_supervision"
    assert witness["state_or_inventory_assigned"] is False
    assert witness["body"] == "ascent" and witness["phase"] == "stack_ascent"
    assert witness["time_s"] == evidence["tick_inputs"][1]["state"]["time_s"]
    assert witness["state"] == evidence["tick_inputs"][1]["state"] == evidence["plant_inputs"][1]["state"]
    assert witness["vehicle"] == evidence["plant_inputs"][1]["vehicle"]
    assert witness["state_sha256"] == digest(witness["state"]) == request["observation"]["state_sha256"]
    assert witness["vehicle_sha256"] == digest(witness["vehicle"]) == request["observation"]["vehicle_sha256"]
    assert result["final_prefix_supervision"] == observer
    assert len(evidence["plant_inputs"]) == result["outcome"]["integration_steps"] == 2
    assert result["concurrent_execution"]["posthoc_ordering"] is False
    assert len(result["concurrent_execution"]["tick_order"]) == 2
    assert result["booster_run"] is None and result["restored_prefix"]["closed"] is False


def test_actual_ship_generator_publishes_owned_witnesses_through_real_coordinate_and_outer():
    evidence = exercise_concurrent_witness_boundary()
    assert_owned_nonempty_witnesses(evidence)
    checked = check_saved_supervision_consumer(evidence)
    assert checked["actual_tick_projection_consistent"] is True
    assert checked["request_count"] == 1 and checked["fixture_by_domain"] == {"ascent": 1}
    assert checked["native_by_domain"] == {} and checked["safety_path_count"] == 0


def check_saved_supervision_consumer(evidence):
    # Exercise this exact production consumer of the generated witness field.
    # The tiny prefix has no separated Booster; this is not a full-prefix or
    # aggregate final-mission verification and deliberately claims neither.
    return verify_prefix_supervision(evidence["supervision"], inputs=evidence["consumer_inputs"],
        prefix={"final_supervision_actual_witnesses": evidence["result"]["final_supervision_actual_witnesses"],
            "booster_run": {}}, rpc=evidence["rpc"])


def test_consumer_fails_if_nonempty_actual_witness_is_replaced_by_empty_list():
    evidence = exercise_concurrent_witness_boundary()
    evidence["result"]["final_supervision_actual_witnesses"] = []
    with pytest.raises(ValueError, match="without_actual_tick"):
        check_saved_supervision_consumer(evidence)


def test_consumer_rejects_witness_request_rebinding_even_with_valid_own_hashes():
    evidence = exercise_concurrent_witness_boundary()
    evidence["result"]["final_supervision_actual_witnesses"][0]["request_ids"] = ["unrelated-request"]
    with pytest.raises(ValueError, match="without_actual_tick"):
        check_saved_supervision_consumer(evidence)
