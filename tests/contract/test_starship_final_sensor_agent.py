"""Host sensor projection/strict decoder, with zero provider or flight calls.

The live-shaped receipts below are invented for rejection tests. They are not
native-model evidence, success rates or a completed integration mission.
"""

from copy import deepcopy
from hashlib import sha256
import json
import socket
import subprocess

import pytest

from src.intelligence import starship_final_sensor_agent as sensor
from src.intelligence import starship_final_supervision_agent as supervision
from src.runtime import starship_final_sensor_execution as execution
from src.runtime.starship_final_fixture import FinalFixtureTransport
from src.runtime import starship_sixdof as dyn, starship_physics as physics


@pytest.fixture(autouse=True)
def no_physics_or_provider(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Sensor contracts cannot call providers, integrate or spawn")

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (physics, "step"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def request(stage="final", *, run="sensor-contract"):
    actions = (
        [execution.CONTINUE, execution.TRACK]
        if stage == "initial"
        else [execution.CONTINUE, execution.ADOPT, execution.DELEGATE]
    )
    facts = {
        "time_s": 500.0,
        "altitude_m": 250000.0,
        "fuel_gauge_kg": 90000.0,
        "accel_factor": 0.8,
        "accel_band_low": 0.788,
        "accel_band_high": 0.812,
        "accel_samples": 80,
        "accel_anomaly": True,
        "application_time_s": 1100.0,
        "tracking_requested": stage == "final",
        "tracking_delay_s": 30.0,
    }
    if stage == "final":
        facts.update(
            tracking_factor=0.8,
            tracking_band_low=0.788,
            tracking_band_high=0.812,
            tracking_intervals=40,
        )
        for action in (execution.CONTINUE, execution.ADOPT):
            for label, factor in (
                ("band_low", 0.788),
                ("point_estimate", 0.8),
                ("band_high", 0.812),
            ):
                for key, value in {
                    "sampled_factor": factor,
                    "speed_mps": 3.5 if action == execution.ADOPT else 9.0,
                    "tilt_deg": 0.4,
                    "body_rate_rad_s": 0.001,
                    "reserve_kg": 58000.0,
                    "limits_met": action == execution.ADOPT,
                }.items():
                    facts[f"{action}.{label}.{key}"] = value
    value = {
        "schema": execution.PUBLIC_SCHEMA,
        "run_id": run,
        "request_id": run + "-" + stage,
        "stage": stage,
        "issued_at_s": 500.0,
        "deadline_s": 535.0 if stage == "initial" else 650.0,
        "scope_sha256": "a" * 64,
        "facts": facts,
        "allowed_actions": actions,
        "effects": {action: execution.EFFECTS[action] for action in actions},
    }
    return execution.validate_public_request(value)


def jev(value, choice):
    return {
        "model": supervision.JEV_MODEL,
        "answers": {
            "decision": {
                "type": "choice",
                "choice": choice,
                "probabilities": {
                    action: float(action == choice) for action in value["allowed_actions"]
                },
                "confidence": 0.9,
            }
        },
    }


def deep(choice=execution.ADOPT):
    return {
        "model": supervision.DEEPSEEK_MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "action": choice,
                            "summary": "Use the independent numerical evidence within the available authority.",
                            "reference_ids": ["accel_factor", "tracking_factor"],
                        }
                    ),
                },
            }
        ],
    }


class ScriptedTransport:
    def __init__(self, answers, *, native_shape=False, after=None):
        self.answers, self.calls, self.native_shape, self.after = (
            list(answers),
            [],
            native_shape,
            after,
        )

    def _invoke(self, provider, payload, receipt, decode):
        self.calls.append(deepcopy(payload))
        raw = self.answers.pop(0)
        answer, model = decode(raw)
        receipt.update(status="succeeded", model_id=model)
        if self.native_shape:
            receipt.update(
                response_received=True,
                complete_response_observed=True,
                call_attempted=True,
                call_succeeded=True,
                model_inference_invoked=True,
                model_inference_status="confirmed_by_valid_response",
                retries=0,
                redirects=0,
                request_sha256=sha256(supervision.canonical_bytes(payload)).hexdigest(),
                response_sha256=sha256(supervision.canonical_bytes(raw)).hexdigest(),
                request_bytes=len(supervision.canonical_bytes(payload)),
                response_bytes_read=len(supervision.canonical_bytes(raw)),
            )
        if self.after:
            self.after(receipt)
        return answer


def assess(value=None, answers=None, *, mode="fixture", monkeypatch=None):
    value = request() if value is None else value
    clock = Clock()
    transport = ScriptedTransport(
        [jev(value, execution.ADOPT)] if answers is None else answers, native_shape=mode == "live"
    )
    if mode == "live":
        monkeypatch.setenv(supervision.LIVE_OPT_IN_ENV, "1")
    agent = sensor.FinalSensorAgent(
        mode=mode, transport=transport, clock=clock, live_authorized=lambda _r, _p: True
    )
    result = agent.assess(
        value,
        remaining_s=value["deadline_s"] - value["issued_at_s"],
        outer_deadline_monotonic=clock() + value["deadline_s"] - value["issued_at_s"],
    )
    return agent, result, transport


def reseal(session):
    response = session["result"]
    response["provenance"]["host_assessments_sha256"] = sensor.digest(session["assessments"])
    response["decision_id"] = sensor.digest(
        {k: v for k, v in response.items() if k != "decision_id"}
    )


@pytest.mark.parametrize(
    "stage,action",
    [
        ("initial", execution.TRACK),
        ("initial", execution.CONTINUE),
        ("final", execution.ADOPT),
        ("final", execution.CONTINUE),
    ],
)
def test_real_strict_decoder_preserves_each_eligible_fixture_choice(stage, action):
    value = request(stage)
    agent, result, transport = assess(value, [jev(value, action)])
    assert result["action"] == action and result["mode"] == "fixture"
    checked = sensor.verify_saved_session(agent.record()["sessions"][0], value, result, "fixture")
    assert checked["fixture_judgments"] == 1 and checked["native_judgments"] == 0
    assert not result["provenance"]["model_inference_invoked"] and len(transport.calls) == 1
    assert transport.calls[0]["state"]["evidence"] == value["facts"]
    assert transport.calls[0]["state"]["phase"] == "sensor"


def test_production_fixed_preflight_transport_is_unmistakably_non_native():
    value = request()
    agent = sensor.FinalSensorAgent(
        mode="fixture", transport=FinalFixtureTransport(), clock=Clock()
    )
    result = agent.assess(value, remaining_s=150.0, outer_deadline_monotonic=250.0)
    assert (
        result["action"] == execution.ADOPT
        and result["provenance"]["model_inference_invoked"] is False
    )
    session = agent.record()["sessions"][0]
    assert session["assessments"][0]["provider_receipt"]["mode"] == "fixture"
    assert not agent.record()["execution_permission_granted"]


def test_explicit_delegation_retains_one_bound_original_epoch_and_numeric_reference_ids():
    value = request()
    agent, result, transport = assess(value, [jev(value, execution.DELEGATE), deep()])
    session = agent.record()["sessions"][0]
    assert len(transport.calls) == 2 and result["action"] == execution.ADOPT
    assert result["provenance"]["explicit_delegation"] is True
    assert all(
        call["provider_receipt"]["original_deadline_monotonic"] == 250.0
        for call in session["assessments"]
    )
    assert transport.calls[1]["model"] == "deepseek-flash"
    public = json.loads(transport.calls[1]["messages"][-1]["content"])
    assert execution.DELEGATE not in public["allowed_actions"]
    assert session["assessments"][1]["references"] == [
        {"reference_id": "accel_factor", "value": 0.8},
        {"reference_id": "tracking_factor", "value": 0.8},
    ]
    checked = sensor.verify_saved_session(session, value, result, "fixture")
    assert checked["fixture_judgments"] == 2 and not checked["free_prose_verified"]


@pytest.mark.parametrize(
    "place,key,value",
    [
        ("facts", "fault_label", "engine_loss"),
        ("facts", "physical_factor", 0.8),
        ("facts", "seed", 20261021),
        ("facts", "actual_vehicle", {}),
        ("top", "truth", {}),
        ("facts", "accel_factor", float("nan")),
        ("facts", "accel_factor", float("inf")),
        ("facts", "accel_factor", True),
        ("top", "allowed_actions", ["engine_shutdown"]),
        ("top", "effects", {"continue_nominal_assumption": "unreviewed"}),
    ],
)
def test_private_truth_and_unreviewed_actions_fail_before_provider(place, key, value):
    public = request()
    (public["facts"] if place == "facts" else public)[key] = value
    transport = ScriptedTransport([])
    agent = sensor.FinalSensorAgent(mode="fixture", transport=transport, clock=Clock())
    with pytest.raises((ValueError, TypeError)):
        agent.assess(public, remaining_s=150.0, outer_deadline_monotonic=250.0)
    assert transport.calls == []


@pytest.mark.parametrize(
    "change", ["model", "sum", "choice", "bool", "missing", "confidence", "truncated"]
)
def test_invalid_receipt_or_answer_is_preserved_without_retry(change):
    value = request()
    response = jev(value, execution.ADOPT)
    answer = response["answers"]["decision"]
    if change == "model":
        response["model"] = "old-alias"
    elif change == "sum":
        answer["probabilities"] = {k: 0.9 for k in value["allowed_actions"]}
    elif change == "choice":
        answer["choice"] = "engine_shutdown"
    elif change == "bool":
        answer["probabilities"][execution.ADOPT] = True
    elif change == "missing":
        del answer["confidence"]
    elif change == "confidence":
        answer["confidence"] = 1.1
    else:
        response = {"model": supervision.JEV_MODEL}
    agent, result, transport = assess(value, [response])
    assert result == {"status": "unavailable", "action": None} and len(transport.calls) == 1
    session = agent.record()["sessions"][0]
    assert session["assessments"][0]["status"] == "no_valid_judgment"
    assert (
        session["assessments"][0]["provider_receipt"]["validation_error"] in supervision.REJECTIONS
    )


@pytest.mark.parametrize(
    "change",
    ["request", "rpc", "response", "action", "references", "empty_references", "epoch", "delegate"],
)
def test_saved_session_tampering_is_rejected_even_after_finite_hashes_are_resealed(change):
    value = request()
    agent, _, _ = assess(value, [jev(value, execution.DELEGATE), deep()])
    session = agent.record()["sessions"][0]
    if change == "request":
        session["request"]["facts"]["accel_factor"] = 0.9
    elif change == "rpc":
        session["rpc_request"]["observation"]["facts"]["accel_factor"] = 0.9
    elif change == "response":
        session["result"]["action"] = execution.CONTINUE
    elif change == "action":
        session["assessments"][1]["action"] = "engine_shutdown"
    elif change == "references":
        session["assessments"][1]["references"][0]["value"] = 0.9
    elif change == "empty_references":
        session["assessments"][1]["references"] = []
    elif change == "epoch":
        session["assessments"][1]["provider_receipt"]["original_deadline_monotonic"] += 1.0
    else:
        session["assessments"][0]["action"] = execution.CONTINUE
    reseal(session)
    with pytest.raises(ValueError):
        sensor.verify_saved_session(session, value, session["result"], "fixture")


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "complete",
        "mode",
        "retries",
        "invoked",
        "response_hash",
        "response_bytes",
        "confidence",
        "authority",
        "decision_epoch",
    ],
)
def test_saved_native_shaped_receipts_require_concrete_bounded_native_facts(monkeypatch, change):
    value = request()
    agent, _, _ = assess(value, mode="live", monkeypatch=monkeypatch)
    session = agent.record()["sessions"][0]
    receipt = session["assessments"][0]["provider_receipt"]
    if change == "model":
        receipt["model_id"] = "unexpected"
    elif change == "complete":
        receipt["complete_response_observed"] = False
    elif change == "mode":
        receipt["mode"] = "fixture"
    elif change == "retries":
        receipt["retries"] = 1
    elif change == "invoked":
        receipt["model_inference_invoked"] = False
    elif change == "response_hash":
        receipt["response_sha256"] = "not-a-hash"
    elif change == "response_bytes":
        receipt["response_bytes_read"] = 0
    elif change == "confidence":
        receipt["native_choice_confidence"] = -0.1
    elif change == "authority":
        receipt["approval_granted"] = True
    else:
        receipt["decision_deadline_monotonic"] = receipt["original_deadline_monotonic"] + 1.0
    reseal(session)
    with pytest.raises(ValueError):
        sensor.verify_saved_session(session, value, session["result"], "live")


def test_same_request_cannot_send_again_after_an_unavailable_response():
    value = request()
    transport = ScriptedTransport([{"model": "wrong"}])
    agent = sensor.FinalSensorAgent(mode="fixture", transport=transport, clock=Clock())
    assert (
        agent.assess(value, remaining_s=150.0, outer_deadline_monotonic=250.0)["status"]
        == "unavailable"
    )
    with pytest.raises(ValueError, match="already_consumed"):
        agent.assess(value, remaining_s=150.0, outer_deadline_monotonic=250.0)
    assert len(transport.calls) == 1
