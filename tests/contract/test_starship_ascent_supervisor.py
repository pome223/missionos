from dataclasses import replace
from types import SimpleNamespace
from copy import deepcopy
import json
from src.runtime.starship_ascent_supervisor import CONFIG, AscentSupervisor
from src.runtime.starship_replanning import digest
from test_starship_replanning import example


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += max(dt, 1e-6)


def setup(mode="fixture", mailbox=None):
    _, state = example()
    clock = Clock()
    actor = AscentSupervisor(
        deepcopy(CONFIG), mode=mode, run_id="test", mailbox=mailbox, clock=clock, sleep=clock.sleep
    )

    def tick(t, *, phase="stack_ascent", failed=False, withheld=False, body="ship"):
        engines = state.engine_states
        if failed:
            engines = (replace(engines[0], available=False), *engines[1:])
        return SimpleNamespace(
            body_id=body,
            time_s=t,
            phase=phase,
            state=replace(state, time_s=t, engine_states=engines),
            deployment_withheld=withheld,
            sequencer_state="skipped" if withheld else "running",
            released_count=0,
        )

    return actor, tick, clock


def test_normal_monitoring_changes_no_engine_command_or_deployment():
    actor, tick, clock = setup()
    actor.observe(tick(0))
    actor.observe(tick(0.1))
    actor.observe(tick(10, phase="ship_ascent"))
    actor.observe(tick(10.1, phase="ship_ascent"))
    result = actor.finish()
    assert len(result["decisions"]) == 2 and actor.deployment_permission()
    assert all(
        r["action"] == "continue_mission" and not r["fallback"] and not r["physical_engine_command"]
        for r in result["decisions"]
    )
    assert all(r["response"]["model_inference_invoked"] is False for r in result["decisions"])
    assert clock.now >= 0.2 - 1e-8


def test_stale_continue_cannot_override_fresh_engine_loss(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    r = actor.pending["request"]
    (tmp_path / "response.json").write_text(
        json.dumps(
            {
                "request_id": r["request_id"],
                "request_sha256": digest(r),
                "action": "continue_mission",
                "model_inference_invoked": True, "mode": "live", "jev_invocation": receipt(),
            }
        )
    )
    actor.observe(tick(1, failed=True))
    assert not actor.deployment_permission()
    assert actor.records[0]["fallback"] and actor.records[0]["stale_health"]
    actor.observe(tick(2, failed=True, withheld=True))
    assert actor.confirmations[0]["later_observation"]["time_s"] > actor.last_command_time


def test_missing_reply_expires_without_waiting_on_gnc(tmp_path):
    actor, tick, clock = setup("live", tmp_path)
    actor.observe(tick(0))
    actor.observe(tick(12))
    assert actor.pending is None and actor.records[0]["fallback"]
    assert actor.records[0]["action"] == "continue_mission"
    assert actor.deployment_permission() and clock.now >= 12


def test_withhold_is_irreversible_in_preflight_scope():
    actor, tick, _ = setup()
    actor.observe(tick(0, failed=True))
    actor.observe(tick(0.1, failed=True))
    actor.observe(tick(20, phase="ship_ascent"))
    actor.observe(tick(20.1, phase="ship_ascent"))
    assert not actor.deployment_permission()


def test_other_body_cannot_change_ship_supervisor():
    actor, tick, _ = setup()
    actor.observe(tick(0, failed=True, body="booster"))
    assert actor.request_count == 0 and actor.deployment_permission()


def test_engine_loss_after_request_budget_still_withholds():
    actor, tick, _ = setup()
    actor.request_count = CONFIG['maximum_requests']
    actor.observe(tick(0))
    actor.observe(tick(1, failed=True))
    assert not actor.deployment_permission()
    assert actor.pending is None
    assert actor.events[-1]['trigger'] == 'observed_engine_loss_interlock'
    actor.observe(tick(2, failed=True, withheld=True))
    assert actor.confirmations


def test_conservative_reply_survives_separation_topology_change(tmp_path):
    actor, tick, _ = setup('live', tmp_path)
    actor.observe(tick(0))
    request = actor.pending['request']
    (tmp_path/'response.json').write_text(json.dumps({
        'request_id': request['request_id'], 'request_sha256': digest(request),
        'action': 'withhold_deployment', 'model_inference_invoked': True, 'mode': 'live', 'jev_invocation': receipt()}))
    changed = tick(.1, phase='ship_ascent')
    changed.state = replace(changed.state, engine_states=changed.state.engine_states[:2])
    actor.observe(changed)
    assert actor.records[0]['accepted_action'] == 'withhold_deployment'
    assert not actor.deployment_permission()


def receipt():
    return {"call_attempted": True, "model_inference_invoked": True,
            "status": "succeeded", "provider": "typesafe"}


def reply(actor, action="continue_mission"):
    request = actor.pending["request"]
    return {"request_id": request["request_id"], "request_sha256": digest(request),
            "action": action, "mode": "live", "model_inference_invoked": True,
            "jev_invocation": receipt()}


def test_live_exact_deadline_falls_back_and_cancels_provider(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    response = reply(actor, "withhold_deployment")
    (tmp_path/"response.json").write_text(json.dumps(response))
    actor.observe(tick(12))
    assert actor.records[0]["fallback"] and actor.deployment_permission()
    cancelled = json.loads((tmp_path/"cancelled.json").read_text())
    assert cancelled["request_sha256"] == response["request_sha256"]


def test_non_object_live_response_does_not_crash_or_authorize(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    (tmp_path/"response.json").write_text("[]")
    actor.observe(tick(1))
    assert actor.pending and not actor.records
    actor.observe(tick(12))
    assert actor.records[0]["fallback"]


def test_fixture_response_cannot_be_adopted_in_live_phase(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    response = reply(actor, "withhold_deployment")
    response.update(mode="fixture", model_inference_invoked=False, jev_invocation=None)
    (tmp_path/"response.json").write_text(json.dumps(response))
    actor.observe(tick(1))
    assert actor.records[0]["fallback"] and actor.deployment_permission()


def test_deepseek_cannot_expand_short_phase_authority(tmp_path):
    actor, tick, _ = setup("live", tmp_path)
    actor.observe(tick(0))
    response = reply(actor, "withhold_deployment")
    response["llm_invocation"] = {"call_attempted": True}
    (tmp_path/"response.json").write_text(json.dumps(response))
    actor.observe(tick(1))
    assert actor.records[0]["fallback"] and actor.deployment_permission()
