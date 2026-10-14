"""Complete synthetic candidate material fits the bounded provider transport.

No saved private flight artifact, API credential or network is used. The fixture
contains four contact samples per candidate and the production comparison schema.
"""

from copy import deepcopy

from src.intelligence import starship_flight_supervisor as transport
from src.intelligence import starship_replanning as host
from src.runtime.starship_replanning import MODE_ENV, contract, digest, notices, resources
from src.runtime.starship_replanning_comparison import candidate_material
from src.runtime.starship_replanning_verifier import metrics
from tests.contract.test_starship_comparison_models import request
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate


def full_request(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1352.0, case="comparison_update")
    authority = contract("live", "comparison_update")
    value = request(authority)
    value["request_id"] = "payload-boundary-" + "x" * 96
    value["time_s"] = 1599.1234567890123
    value["deadline_s"] = value["time_s"] + authority["decision_timeout_s"]
    value["allowed_actions"] = list(authority["actions"])
    value["notice"] = notices("comparison_update", value["time_s"])
    value["resources"] = resources(authority, value["time_s"], actor.scheduled)
    value["comparison_context"] = {
        "preparation_id": "payload-boundary-comparison-" + "x" * 96,
        "generation_sha256": "a" * 64,
        "ready": True,
        "reason": "predictions_available",
        "missing_candidate_ids": [],
        "prepared_candidate_ids": ["nominal", "next_orbit"],
    }
    value["trigger_event_ids"] = ["event-12", "event-13"]
    for name, area in (("nominal", "east"), ("next_orbit", "west")):
        candidate = make_candidate(actor, name)
        points = [metrics(trial) for trial in candidate["trials"]]
        # Keep real field shape with nontrivial numeric widths rather than a
        # one-field stand-in that would miss growth in the actual API payload.
        for index, point in enumerate(points):
            for key in point:
                point[key] += (index + 1) * 0.000012345678901
        value["candidates"][name] = {
            "candidate_sha256": digest(candidate),
            "return_time_s": candidate["return_time_s"],
            "predicted_contacts": points,
            "constraint_rejections": [],
            "areas": [area],
            "comparison_material": candidate_material(
                candidate, authority, value["notice"], actor.scheduled
            ),
        }
    return authority, value


def test_full_two_candidate_material_fits_deepseek_and_jev_with_headroom(monkeypatch, tmp_path):
    authority, value = full_request(monkeypatch, tmp_path)
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    captured = []

    def capture(provider, payload, receipt, decode):
        captured.append((provider, deepcopy(payload)))
        return "keep_plan"  # Encoding only; no provider reply or inference claim.

    monkeypatch.setattr(agent.transport, "_invoke", capture)
    first, second = agent.assess(value), agent.assess(value)
    assert [provider for provider, _ in captured] == ["deepseek", "typesafe"]
    assert not first["model_inference_invoked"] and not second["model_inference_invoked"]
    for _, payload in captured:
        length = len(host.canonical_bytes(payload))
        assert length <= transport.MAX_REQUEST_BYTES - 4096, length
    assert all(len(row["predicted_contacts"]) == 4 for row in value["candidates"].values())


def test_oversized_material_is_rejected_before_any_network_attempt(monkeypatch, tmp_path):
    authority, value = full_request(monkeypatch, tmp_path)
    monkeypatch.setenv(MODE_ENV, "live")
    agent = host.ReplanningAgent(authority, lambda: True)
    captured = []

    def capture(provider, payload, receipt, decode):
        captured.append(deepcopy(payload))
        return "keep_plan"

    monkeypatch.setattr(agent.transport, "_invoke", capture)
    agent.assess(value)
    oversized = captured[0]
    oversized["messages"][1]["content"] += "x" * transport.MAX_REQUEST_BYTES

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("Oversized input must never reach network transport")

    monkeypatch.setattr(transport, "_send_once", forbidden_network)
    receipt = transport._receipt("live", "deepseek", None)
    bounded = transport.FlightSupervisor("live", active=lambda: True)
    assert bounded._invoke("deepseek", oversized, receipt, lambda _value: None) is None
    assert receipt["status"] == "request_too_large"
    assert receipt["request_bytes"] > transport.MAX_REQUEST_BYTES
    assert receipt["call_attempted"] is False
