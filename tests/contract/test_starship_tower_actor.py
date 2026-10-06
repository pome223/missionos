"""Actor IPC/evidence fixtures; no dynamics, model inference or target control."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import starship_tower_actor as actors, starship_tower_broker as ipc
from src.runtime import starship_return_sites as sites
from src.runtime.starship_operator_resolution import OperatorResolutionLedger, SCOPE
from src.runtime.starship_tower_supervision_verifier import verify_tower_supervision

KEY = b"explicit-actor-fixture-integrity-key-32bytes"
CONTEXT = {"session_id": "session-1", "plan_id": "plan-1", "plan_sha256": "a"*64,
           "run_id": "run-1", "request_id": "request-1"}
SOURCE = "b"*64


class Clock:
    now = 1000.
    def __call__(self):
        return self.now


class Judge:
    fixture_only = True
    def __init__(self, route="bounded", response="replan"):
        self.route, self.response, self.calls = route, response, 0
    def judge(self, prompt):
        self.calls += 1
        return SimpleNamespace(output={"proposed_response_kind": self.response, "parameters": {}},
            invocation_evidence={"assessment_route": self.route}, model_inference_invoked=False)


def catalog():
    config = json.loads(Path("examples/spaceflight/starship-return-sites-model-test.json").read_text())
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    return sites.ReturnSites.from_dict(config, profile=profile, catch_config=catch)


def scope():
    return {"schema": "missionos.starship_tower_supervision_scope.v1", "scope": SCOPE,
        "context": deepcopy(CONTEXT), "approval_record_sha256": "c"*64, "source_sha256": SOURCE,
        "issued_simulation_time_s": 0., "issued_wall_time_s": 1000., "original_simulation_deadline_s": 85.,
        "original_wall_deadline_s": 1075., "maximum_observation_age_s": 2., "observation_collection_allowed": True,
        "human_resolution_allowed": True}


def state(simulation=10.):
    # Declared input fixture; changing time here is not an integrated trajectory.
    return {"time_s": simulation, "r_eci_m": [6378137., 0., 0.], "v_eci_mps": [0., 0., 0.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 78000.,
        "engine_states": [{"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": True} for _ in range(45)],
        "flap_angles_rad": [0.]*6}


def system(tmp_path, *, route="bounded", response="replan"):
    mailbox, store, declared, clock = tmp_path/"mailbox", tmp_path/"ledger.sqlite3", catalog(), Clock()
    actor = actors.TowerActor(mailbox, scope(), declared, declared.sha256,
        local_integrity_key=KEY, ledger_path=store, clock=clock)
    judge = Judge(route, response)
    ledger = OperatorResolutionLedger(KEY, store_path=store)
    process = SimpleNamespace(poll=lambda: None)
    broker = ipc.TowerBroker(mailbox, CONTEXT["run_id"], scope(), process, ledger, judge, lambda: True, clock=clock)
    return SimpleNamespace(actor=actor, broker=broker, clock=clock, sites=declared, mailbox=mailbox, judge=judge, ledger=ledger)


def tick(item, simulation=10., mode="capture", ready=True):
    item.clock.now = 1000.+simulation-10.
    return item.actor.tick(state(simulation), tower_ready=ready, return_mode=mode, active_site_id=mode,
                          source_sha256=SOURCE, plan_sha256=CONTEXT["plan_sha256"])


def directive(item):
    tick(item)
    item.broker.step()
    assert tick(item, 11.).directive is None
    result = tick(item, 12.)
    assert result.directive is not None
    return result.directive


def target(item, after, site="divert"):
    return {"context": deepcopy(CONTEXT), "return_sites_sha256": item.sites.sha256, "active_site_id": site,
        "return_mode": site, "simulation_time_s": after["time_s"], "state_sha256": actors._digest(after),
        "target_origin_eci_m": sites.return_site_frame(item.sites.site(site), after["time_s"])["origin_eci_m"]}


def test_actor_binds_real_pid_and_local_signature_without_reading_provider_credentials(tmp_path):
    item = system(tmp_path)
    identity = json.loads((item.mailbox/"actor-identity.json").read_text())
    assert identity["actor_pid"] == os.getpid()
    assert actors.verify_actor_identity(identity, expected_scope=scope(), expected_sites_sha256=item.sites.sha256,
        local_integrity_key=KEY)["passed"]
    assert identity["provider_keys_read"] is False and identity["model_calls"] == 0
    assert KEY not in (item.mailbox/"actor-identity.json").read_bytes()
    assert (item.mailbox/"actor-identity.json").stat().st_mode & 0o077 == 0


def test_late_signed_human_ipc_delivery_keeps_actor_alive_and_consumes_no_second_request(tmp_path):
    item = system(tmp_path, route="human_review", response="operator_escalation")
    tick(item)
    item.broker.step()
    handover = tick(item, 11.)
    assert handover.human_request is not None
    latest = item.broker.pending()["latest_observation"]
    signed = item.broker.resolve(context=CONTEXT, request_sha256=handover.human_request["sha256"],
        observed_evidence_sha256=latest["evidence_sha256"], action="continue_capture")
    assert tick(item, 14.).directive is None
    receipt = item.actor.receipt()
    assert receipt["supervision"]["events"][0]["event"] == "stale_human_decision_rejected"
    assert receipt["supervision"]["accepted_decision"] is None
    assert receipt["supervision"]["human_resolution"]["decision"] == signed
    assert verify_tower_supervision(receipt["supervision"], expected_scope=scope(), resolution_signing_key=KEY)["passed"]
    assert tick(item, 15.).directive is None
    assert item.ledger.records(CONTEXT)["decision"] == signed
    tick(item, 85.)
    assert tick(item, 85.1).directive["action"] == "divert"
    final = item.actor.close()
    assert verify_tower_supervision(final["supervision"], expected_scope=scope(), resolution_signing_key=KEY)["passed"]
    assert final["closed"] and final["ended"]["run_active"] is False


@pytest.mark.parametrize("field,value", [("actor_pid", 0), ("source_sha256", "f"*64), ("return_sites_sha256", "f"*64),
    ("context", {**CONTEXT, "run_id": "other"}), ("signature", "f"*64), ("provider_keys_read", True)])
def test_actor_identity_cannot_be_transplanted_or_promoted_to_provider_access(tmp_path, field, value):
    item = system(tmp_path)
    identity = item.actor.receipt()["identity"]
    identity[field] = value
    assert not actors.verify_actor_identity(identity, expected_scope=scope(), expected_sites_sha256=item.sites.sha256,
        local_integrity_key=KEY)["passed"]


def test_directive_is_returned_without_mutating_state_site_or_performing_control(tmp_path):
    item = system(tmp_path)
    before_sites = item.sites.to_dict()
    original = state()
    before = deepcopy(original)
    result = item.actor.tick(original, tower_ready=True, return_mode="capture", active_site_id="capture",
                            source_sha256=SOURCE, plan_sha256=CONTEXT["plan_sha256"])
    assert result.directive is None
    item.broker.step()
    tick(item, 11.)
    result = tick(item, 12.)
    assert result.directive["action"] == "divert"
    assert original == before and item.sites.to_dict() == before_sites
    assert item.actor.receipt()["application_evidence"] is None
    assert item.actor.receipt()["state_assigned"] is False
    assert item.actor.receipt()["site_observations"][-1]["active_site_id"] == "capture"
    verdict = verify_tower_supervision(item.actor.receipt()["supervision"], expected_scope=scope(), resolution_signing_key=KEY)
    assert verdict["passed"], verdict


def test_signed_human_request_protocol_survives_shared_ledger_and_later_tick(tmp_path):
    item = system(tmp_path, route="human_review")
    tick(item)
    item.broker.step()
    handover = tick(item, 11.).human_request
    assert handover == item.ledger.records(CONTEXT)["request"]
    assert json.loads((item.mailbox/"human-request.json").read_text()) == handover
    pending = item.broker.pending()
    decision = item.broker.resolve(context=CONTEXT, request_sha256=handover["sha256"],
        observed_evidence_sha256=pending["latest_observation"]["evidence_sha256"], action="divert")
    assert tick(item, 12.).directive is None
    applied = tick(item, 13.)
    assert applied.directive["action"] == "divert"
    assert item.actor.receipt()["supervision"]["human_resolution"]["decision"] == decision
    assert decision["human_identity_authenticated"] is False


def test_application_ack_requires_later_state_and_declared_actual_target_geometry(tmp_path):
    item = system(tmp_path)
    issued = directive(item)
    before = state(12.)
    with pytest.raises(actors.TowerActorError, match="separate_later_state"):
        item.actor.acknowledge_application(directive_sha256=issued["sha256"], before_state=before, after_state=before,
            integration_record_sha256="d"*64, guidance_target_evidence=target(item, before))
    tick(item, 13., mode="divert")
    after = state(13.)
    report = item.actor.acknowledge_application(directive_sha256=issued["sha256"], before_state=before, after_state=after,
        integration_record_sha256="d"*64, guidance_target_evidence=target(item, after))
    assert report["supervision_application_report"]["observed_guidance_mode_change"] is True
    assert report["integration_evidence_supplied"] is True
    assert report["integration_independently_verified"] is False
    assert report["physical_effect_verified"] is False and report["flight_outcome_improvement"] is False
    assert json.loads((item.mailbox/"actor-application-report.json").read_text()) == report
    with pytest.raises(actors.TowerActorError):
        item.actor.acknowledge_application(directive_sha256=issued["sha256"], before_state=before, after_state=after,
            integration_record_sha256="d"*64, guidance_target_evidence=target(item, after))


@pytest.mark.parametrize("change", ["before", "after", "site", "catalog", "position", "context", "digest"])
def test_application_ack_rejects_changed_state_target_or_integration_binding(tmp_path, change):
    item = system(tmp_path)
    issued = directive(item)
    tick(item, 13., mode="divert")
    before, after = state(12.), state(13.)
    evidence, checksum = target(item, after), "d"*64
    if change == "before":
        before["propellant_kg"] -= 1.
    elif change == "after":
        after["v_eci_mps"][0] += 1.
    elif change == "site":
        evidence = target(item, after, "capture")
    elif change == "catalog":
        evidence["return_sites_sha256"] = "f"*64
    elif change == "position":
        evidence["target_origin_eci_m"][0] += 1.
    elif change == "context":
        evidence["context"]["session_id"] = "other"
    else:
        checksum = "invalid"
    with pytest.raises(actors.TowerActorError):
        item.actor.acknowledge_application(directive_sha256=issued["sha256"], before_state=before, after_state=after,
            integration_record_sha256=checksum, guidance_target_evidence=evidence)
    assert item.actor.receipt()["application_evidence"] is None


def test_close_and_exception_finally_publish_inactive_without_retiming_physical_state(tmp_path):
    item = system(tmp_path)
    with pytest.raises(ArithmeticError):
        with item.actor:
            tick(item)
            before = json.loads((item.mailbox/"latest-observation.json").read_text())
            item.clock.now = 1001.
            raise ArithmeticError("fixture exception")
    final = json.loads((item.mailbox/"latest-observation.json").read_text())
    assert final["run_active"] is False
    assert final["observation"] == before["observation"]
    assert final["simulation_time_s"] == before["simulation_time_s"]
    assert final["wall_time_s"] == 1001.
    assert item.actor.receipt()["ended"]["last_state_sha256"] == before["observation"]["state_sha256"]
    assert item.broker.step()["status"] == "stopped_child_frame"
    assert item.actor.close()["closed"] is True
    with pytest.raises(actors.TowerActorError, match="already_closed"):
        tick(item, 12.)


def test_no_initial_observation_end_record_does_not_invent_a_vehicle_state(tmp_path):
    item = system(tmp_path)
    result = item.actor.close()
    assert result["ended"]["last_state_sha256"] is None
    assert result["ended"]["last_simulation_time_s"] is None
    assert not (item.mailbox/"latest-observation.json").exists()


def test_closed_sites_hash_and_local_integrity_key_are_explicit_not_provider_credentials(tmp_path):
    declared = catalog()
    for changed in ({"approved_sites_sha256": "f"*64}, {"local_integrity_key": b"short"}):
        values = {"approved_sites_sha256": declared.sha256, "local_integrity_key": KEY}
        values.update(changed)
        with pytest.raises(actors.TowerActorError):
            actors.TowerActor(tmp_path/"mailbox", scope(), declared, values["approved_sites_sha256"],
                local_integrity_key=values["local_integrity_key"], ledger_path=tmp_path/"store.sqlite3", clock=Clock())


def test_actor_private_identity_file_is_exclusive_and_symlinks_are_not_followed(tmp_path):
    item = system(tmp_path)
    with pytest.raises(FileExistsError):
        actors.TowerActor(item.mailbox, scope(), item.sites, item.sites.sha256,
            local_integrity_key=KEY, ledger_path=tmp_path/"ledger.sqlite3", clock=item.clock)
    external = tmp_path/"outside.json"
    external.write_text("{}")
    (item.mailbox/"actor-application-report.json").symlink_to(external)
    with pytest.raises(actors.TowerActorError, match="symlink_actor_file"):
        actors._publish_private(item.mailbox, "actor-application-report.json", {})
