"""Synthetic response and archived-record checks; no flight or hosted model."""
import json

import pytest

from src.runtime import starship_m3_prefix as prefix
from src.runtime import starship_m3_prefix_verifier as verifier
from src.runtime.starship_ascent_supervisor import CONFIG
from src.runtime.starship_deployment_handoff import DeploymentTracker
from src.runtime.starship_m3_directors import M3Directors
from src.runtime.starship_mission_director import contract
from src.runtime.starship_replanning import digest
from src.runtime.starship_m2_attempts import empty
from test_starship_m3_prefix import make_study as archived_factory, observed, state
from dataclasses import asdict
from test_starship_m3_splashdown import observation


@pytest.fixture
def make_study():
    return archived_factory.__wrapped__()


def receipt():
    return {"schema_version": "runtime_invocation_evidence.v1", "provider": "typesafe", "mode": "live",
        "status": "succeeded", "call_attempted": True, "model_inference_invoked": True,
        "request_sha256": "a"*64, "timeout_seconds": 10., "application_margin_s": 2., "retries": 0}


def response(request, action):
    return {"request_id": request["request_id"], "request_sha256": digest(request), "mode": "live",
        "action": action, "model_inference_invoked": True, "jev_invocation": receipt(), "llm_invocation": None}


def ledger(authority, phase, records=()):
    result = {**empty(authority, "live-test", phase), "schema": "missionos.starship_m3_host_attempts.v1"}
    for n, record in enumerate(records, 1):
        request = record["request"]
        result["attempts"].append({"sequence": n, "request_id": request["request_id"],
            "request_sha256": digest(request), "provider": "typesafe", "provider_request_sha256": "a"*64,
            "timeout_s": 10., "remaining_s": 12., "request_file_age_s": 0.,
            "status": "succeeded", "completed": True, "valid_response": True, "response_published": True})
    return result


def live_study(make_study, withheld=False):
    study = make_study(withheld)
    authority = prefix.m3_prefix_contract("normal", mode="live")
    study.update(authority=authority, run_id="live-test", model_calls=1)
    study["ascent"]["mode"] = "live"
    d = study["ascent"]["decisions"][0]
    d["request"]["request_id"] = "live-test-ascent-0"
    study["ascent"]["trigger_events"][0]["request_id"] = d["request"]["request_id"]
    d["response"] = response(d["request"], d["action"])
    later_tick = next(r for r in study["prefix_run"]["concurrent_execution"]["tick_order"]
                      if r["body_id"] == "ship" and r["time_s"] > d["time_s"])
    d["later_observation"] = observed(later_tick)
    if not withheld:
        study["ascent_witnesses"].append(json.loads(json.dumps({"sequence": later_tick["sequence"],
            "state": asdict(state(later_tick["time_s"])), "observation": observed(later_tick)})))
    study["prefix_run"]["mission_director"]["envelope"] = authority["m2_authority"]["deployment_authority"]
    study["prefix_run"]["booster_mission_director"] = {"records": []}
    study["provider_attempt_ledger"] = {"ascent": ledger(authority, "ascent", [d]), "booster": ledger(authority, "booster")}
    study["deployment_attempt_ledger"] = empty(authority["m2_authority"], "live-test", "deployment")
    return study


def test_live_scope_has_separate_bounded_jev_grants_and_unmodified_ascent_config():
    authority = prefix.m3_prefix_contract("normal", mode="live")
    assert authority["schema"].endswith(".v3") and authority["maximum_model_calls"] == 11
    assert authority["ascent_authority"] == CONFIG
    assert authority["maximum_ascent_jev_calls"] == 4 and authority["maximum_booster_jev_calls"] == 1
    assert authority["booster_authority"] == contract("live", splashdown=True)
    assert "splashdown_goal" not in authority["m2_authority"]["deployment_authority"]
    with pytest.raises(ValueError):
        prefix.m3_prefix_contract("normal", mode="live", splashdown=False)


def test_real_phase_actor_mailboxes_are_separate_and_no_fixture_callback(tmp_path):
    for phase in ("deployment", "booster"):
        (tmp_path/phase).mkdir()
    authority = prefix.m3_prefix_contract("normal", mode="live")
    router = M3Directors(authority["m2_authority"]["deployment_authority"], authority["booster_authority"],
        DeploymentTracker(26, authority["m2_authority_sha256"]), mode="live", mailbox=tmp_path, run_id="live-test")
    assert router.update("booster_selection", observation("booster_return")) is None
    request = json.loads((tmp_path/"booster"/"request.json").read_text())
    assert request["request_id"] == "live-test-booster:1"
    (tmp_path/"deployment"/"response.json").write_text(json.dumps(response(request, "splashdown")))
    assert router.update("booster_selection", observation("booster_return", 10.1)) is None
    (tmp_path/"booster"/"response.json").write_text(json.dumps(response(request, "splashdown")))
    assert router.update("booster_selection", observation("booster_return", 10.2)) == "splashdown"
    router.confirm("booster", observation("booster_return", 10.3))
    assert router.finish_booster()["records"][0]["later_observation"]["time_s"] == 10.3
    assert not router.finish()["records"]


@pytest.mark.parametrize("withheld", [False, True])
def test_live_ascent_evidence_requires_actual_later_withhold(make_study, withheld):
    study = live_study(make_study, withheld)
    verifier._ascent(study, study["prefix_run"]["concurrent_execution"]["tick_order"])
    result = verifier._providers(study)
    assert result["attempted"] == result["actor_valid_responses"] == result["rules_accepted"] == 1
    if withheld:
        study["ascent"]["confirmations"] = []
        with pytest.raises(ValueError, match="confirmation_missing"):
            verifier._ascent(study, study["prefix_run"]["concurrent_execution"]["tick_order"])


@pytest.mark.parametrize("mutate", [
    lambda s: s["provider_attempt_ledger"]["ascent"].update(authority_sha256="0"*64),
    lambda s: s["provider_attempt_ledger"]["ascent"].update(run_id="another-run"),
    lambda s: s["provider_attempt_ledger"]["ascent"]["attempts"][0].update(provider="deepseek"),
    lambda s: s["provider_attempt_ledger"]["ascent"]["attempts"][0].update(timeout_s=11.),
    lambda s: s["provider_attempt_ledger"]["ascent"]["attempts"][0].update(response_published=False),
    lambda s: s["provider_attempt_ledger"]["ascent"]["attempts"][0].update(provider_request_sha256="b"*64),
    lambda s: s["provider_attempt_ledger"]["ascent"].update(attempts=[]),
    lambda s: s["ascent"]["decisions"][0]["response"].update(jev_invocation=None),
    lambda s: s["ascent"]["decisions"][0]["response"].update(llm_invocation={"call_attempted": True}),
    lambda s: s.update(model_calls=0),
])
def test_live_attempt_corruption_rejected_independently(make_study, mutate):
    study = live_study(make_study)
    mutate(study)
    with pytest.raises(ValueError):
        verifier._providers(study)


def test_failed_host_attempt_without_actor_response_is_counted(make_study):
    study = live_study(make_study)
    row = study["provider_attempt_ledger"]["ascent"]["attempts"][0]
    row.update(status="transport_timeout", valid_response=False, response_published=False)
    d = study["ascent"]["decisions"][0]
    d.update(response=None, accepted_action=None, fallback=True)
    result = verifier._providers(study)
    assert result["attempted"] == 1 and result["actor_valid_responses"] == result["rules_accepted"] == 0


def test_host_startup_wait_is_bounded_before_physics(tmp_path):
    authority = prefix.m3_prefix_contract("normal", mode="live")
    for phase in ("ascent", "booster", "deployment"):
        (tmp_path/phase).mkdir()
    now = [0.]
    def sleep(dt):
        now[0] += dt
        for phase in ("ascent", "booster", "deployment"):
            value = (ledger(authority, phase) if phase != "deployment"
                     else empty(authority["m2_authority"], "live-test", phase))
            (tmp_path/phase/"host-attempts.json").write_text(json.dumps(value))
    prefix._wait_live_mailboxes(tmp_path, authority, "live-test", clock=lambda: now[0], sleep=sleep)
    assert 0 < now[0] < 5.
    with pytest.raises(ValueError, match="authority_mismatch"):
        prefix._wait_live_mailboxes(tmp_path, authority, "other-run", clock=lambda: now[0], sleep=sleep)
    for path in tmp_path.glob("*/host-attempts.json"):
        path.unlink()
    now[0] = 0.
    with pytest.raises(ValueError, match="startup_timeout"):
        prefix._wait_live_mailboxes(tmp_path, authority, "live-test", clock=lambda: now[0],
                                   sleep=lambda dt: now.__setitem__(0, now[0]+dt))
    assert now[0] == 5.


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_orbital_update_keeps_deployment_normal_and_registered_return(mode):
    approved = prefix.m3_prefix_contract("orbital_update", mode=mode)
    assert approved["m2_authority"]["case"] == "orbital_update"
    assert approved["m2_authority"]["return_case"] == "comparison_orbital_update"


def test_live_continuation_also_needs_a_later_observation(make_study):
    study = live_study(make_study)
    study["ascent"]["decisions"][0]["later_observation"] = None
    with pytest.raises(ValueError, match="ascent_live_later_observation_missing"):
        verifier._ascent(study, study["prefix_run"]["concurrent_execution"]["tick_order"])
