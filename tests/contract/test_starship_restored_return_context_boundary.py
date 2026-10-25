"""Real inherited FD+decoder and scripted actual ticks; no flight or API."""

from copy import deepcopy
import json
import threading

import pytest

from src.intelligence import starship_restored_agent as agent
from src.runtime import starship_restored_coordinator as control
from src.runtime import starship_restored_decision_pipe as pipe
from src.runtime.starship_inflight_runtime import approval_pipe, MAX_FRAME_BYTES
from tests.contract import test_starship_restored_return_context as projection_contract
from tests.contract import test_starship_restored_coordinator as coordinator_contract
from tests.contract.test_starship_restored_agent import SyntheticTransport

request, AREA = projection_contract.request, projection_contract.AREA
no_physics_or_provider = projection_contract.no_physics_or_provider
scenario, ack = coordinator_contract.scenario, coordinator_contract.ack


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def realistic_request():
    value = request()
    facts = value["facts"]
    facts.update(
        {
            "notice.generation": 0,
            "notice.issued_time_s": 0.0,
            "notice.expiry_time_s": 16000.0,
            "authority.approval_deadline_s": 3320.6999999999975,
            "resources.power_kwh": 21.745416666666667,
            "resources.temperature_k": 295.15275,
            "resources.propellant_loss_kg": 3.055,
        }
    )
    for candidate, when in (("nominal", 3600.0), ("next_orbit", 9100.0)):
        facts.update(
            {
                f"candidate.{candidate}.additional_wait_s": when - 3600.0,
                f"candidate.{candidate}.arrival_time_s": when + 2017.817658174,
                f"candidate.{candidate}.contact_speed_mps": 4.379594542034478,
                f"candidate.{candidate}.latitude_deg": 22.763451662468466,
                f"candidate.{candidate}.longitude_deg": -171.8059149115098,
                f"candidate.{candidate}.power_kwh": 10.324032261236725
                if candidate == "next_orbit"
                else 19.40125,
                f"candidate.{candidate}.propellant_loss_kg": 140.11161286515932,
                f"candidate.{candidate}.remaining_propellant_kg": 55504.81341397534,
                f"candidate.{candidate}.temperature_k": 302.005580643258,
                f"candidate.{candidate}.tilt_deg": 2.36680312301214,
            }
        )
    return value


@pytest.mark.parametrize("route", ["keep_reserved_return", "select_next_orbit", "explicit_deep"])
def test_real_fd_realistic_context_fits_original_frame_and_preserves_each_valid_route(route):
    value = realistic_request()
    before = deepcopy(value)
    pairs = [approval_pipe(), approval_pipe()]
    clock, calls = Clock(), []

    class LocalAgent:
        def __init__(self, *, active, clock):
            self.active, self.clock = active, clock

        def assess(self, req, **kwargs):
            provider = kwargs["provider"]
            if provider == "typesafe":
                chosen = "request_deep_reasoning" if route == "explicit_deep" else route
                others = len(req["allowed_actions"]) - 1
                answer = {
                    "model": agent.JEV_MODEL,
                    "answers": {
                        "decision": {
                            "type": "choice",
                            "choice": chosen,
                            "probabilities": {
                                action: 0.5 if action == chosen else 0.5 / others
                                for action in req["allowed_actions"]
                            },
                            "confidence": 0.24,
                        }
                    },
                }
            else:
                answer = {
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "action": "select_next_orbit",
                                        "summary": "Compare the independently reported operating availability and scope.",
                                        "reference_ids": [
                                            "notice.next_orbit_available",
                                            "candidate.nominal.reason." + AREA,
                                        ],
                                    }
                                ),
                            },
                        }
                    ],
                }
            transport = SyntheticTransport(answer)
            result = agent.RestoredAgent(
                transport=transport, active=self.active, clock=self.clock
            ).assess(req, **kwargs)
            calls.append(
                {
                    "provider": provider,
                    "request": deepcopy(req),
                    "payload": deepcopy(transport.sent),
                }
            )
            return result

    binding = {
        "context": {"run_id": "context", "mission_id": "synthetic", "vehicle_id": "ship"},
        "plan_sha256": "a" * 64,
        "source_sha256": "b" * 64,
    }
    client = pipe.RestoredDecisionClient(
        **binding, request_writer=pairs[0][1], response_reader=pairs[1][0], clock=clock
    )
    host = pipe.RestoredDecisionHost(
        **binding, reader=pairs[0][0], writer=pairs[1][1], agent_factory=LocalAgent, clock=clock
    )
    try:
        frame = client.begin(value)
        assert (
            len(pipe._wire(value)) <= 24000 and len(pipe._wire(frame)) <= MAX_FRAME_BYTES == 16384
        )
        result = None
        for _ in range(1000):
            host.tick()
            result = client.poll(value["time_s"] + 0.25)
            if result is not None:
                break
            threading.Event().wait(0.001)
        assert result is not None and result["status"] == "valid"
        assert result["action"] == ("select_next_orbit" if route == "explicit_deep" else route)
        assert len(calls) == (2 if route == "explicit_deep" else 1)
        assert calls[0]["payload"]["state"]["allowed_actions"] == value["allowed_actions"]
        assert (
            calls[0]["payload"]["state"]["conditional_return_effects"]["keep_reserved_return"][
                "candidate_id"
            ]
            == "nominal"
        )
        assert (
            calls[0]["payload"]["state"]["conditional_return_effects"]["keep_reserved_return"][
                "independent_tools_accepted"
            ]
            is False
        )
        if route == "explicit_deep":
            assert (
                calls[1]["provider"] == "deepseek"
                and calls[1]["request"]["facts"] == value["facts"]
            )
            assert calls[1]["request"]["time_s"] == value["time_s"]
            assert "request_deep_reasoning" not in calls[1]["request"]["allowed_actions"]
        assert value == before and result["execution_permission_granted"] is False
        assert result["model_inference_invoked"] is False
    finally:
        for pair in pairs:
            for endpoint in pair:
                endpoint.close()


def test_coordinator_opt_in_projects_same_actual_reservation_and_current_tool_epoch(
    scenario, monkeypatch
):
    s = scenario
    # The old isolated scenario constructs the default-false coordinator. Its
    # real constructor opt-in/source guard is tested separately below; this
    # scripted-state seam tests only the request-building branch.
    s.coordinator.return_context_projection = True
    s.start()
    ack(s)
    s.coordinator.tick()
    s.forecasts[0].ready = True
    original = control.admit_restored_candidate

    def independent_tool(candidate, observation, **kwargs):
        checked = original(candidate, observation, **kwargs)
        nominal = candidate.record()["proposal"]["candidate_id"] == "nominal"
        checked.update(
            accepted=not nominal,
            reasons=[AREA] if nominal else [],
            numerical_contact_capability_met=True,
            matched_synthetic_site_ids=[] if nominal else ["next-area"],
        )
        return checked

    monkeypatch.setattr(control, "admit_restored_candidate", independent_tool)
    s.coordinator.tick()
    assert len(s.requests) == 1
    value = s.requests[0]
    actual = s.coordinator._decision["request_snapshot"]
    assert value["facts"]["reservation.nominal"] is True
    assert value["facts"]["reservation.return_time_s"] == actual["binding"]["return_time_s"]
    assert value["facts"]["time_s"] == actual["state"]["time_s"]
    assert value["facts"]["notice.nominal_available"] is False
    assert value["facts"]["notice.next_orbit_available"] is True
    assert value["allowed_actions"] == list(agent.RETURN)
    s.replies.append({"status": "valid", "action": "keep_reserved_return"})
    s.coordinator.tick()
    assert s.coordinator.decisions[0]["original_action"] == "keep_reserved_return"
    assert s.coordinator.decisions[0]["disposition"] == "valid_choice_rejected_not_overwritten"
    assert not s.coordinator.changes and s.coordinator.actor.status == "idle"


@pytest.mark.parametrize("opt_in", [True, 1])
def test_constructor_rejects_missing_new_source_and_non_bool_opt_in(scenario, opt_in):
    c = scenario.coordinator
    with pytest.raises(ValueError, match="return_context_opt_in_or_source"):
        control.RestoredReturnCoordinator(
            c.origin,
            profile_raw_bytes=c.profile,
            expected_source_sha256=c.source,
            integration_policy=c.policy,
            context=c.context,
            plan_sha256=c.plan,
            output_dir=c.output,
            decision_client=c.client,
            ready_writer=c.ready_writer,
            ready_reader=c.ready_reader,
            approval_request_writer=c.approval_writer,
            approval_grant_reader=c.approval_reader,
            telemetry_writer=c.telemetry,
            sites=c.sites,
            notice_resources=c.tools,
            notice_generations=c.generations,
            deployment_record=c.deployment,
            return_context_projection=opt_in,
        )
