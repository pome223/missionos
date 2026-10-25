"""Scripted actual observations + production inherited RPC; no flight/provider."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import socket
import subprocess
from threading import Event

import pytest

from src.runtime import starship_final_prefix_supervision as supervision
from src.runtime import starship_final_protocol as rpc
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime import starship_sixdof_mission as mission
from src.runtime import starship_sixdof_booster as booster
from src.runtime.starship_inflight_runtime import approval_pipe
from src.intelligence.starship_final_supervision_agent import FinalSupervisionAgent

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_flight_or_provider(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("Prefix supervision tests cannot integrate or call providers")

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (env, "step"),
        (mission, "advance_plant"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FixtureTransport:
    def __init__(self, choices):
        self.choices, self.calls = list(choices), []

    def _invoke(self, provider, payload, receipt, decode):
        self.calls.append(deepcopy(payload))
        choice = self.choices.pop(0)
        criteria = payload["questions"]["decision"]["criteria"]
        answer, model = decode(
            {
                "model": payload["model"],
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": choice,
                        "probabilities": {key: float(key == choice) for key in criteria},
                        "confidence": 0.8,
                    }
                },
            }
        )
        receipt.update(status="succeeded", model_id=model)
        return answer


def source_manifest():
    from src.runtime import starship_restored_contract as contract

    source = {
        key: "1" * 64
        for key in contract.CRITICAL_SOURCES | {"src/runtime/starship_restored_prefix.py"}
    }
    source["examples/spaceflight/starship-sixdof-profile.json"] = contract.PROFILE_SHA256
    for key in (
        supervision.SOURCE,
        "src/runtime/starship_final_protocol.py",
        "src/intelligence/starship_final_supervision_agent.py",
    ):
        source[key] = sha256((ROOT / key).read_bytes()).hexdigest()
    return source


def actual(profile, *, kind="ship", now=30.0, altitude=71000.0, fuel=80000.0):
    craft = mission.vehicle(profile, kind)
    state = dyn.State6DOF(
        now,
        (env.EARTH_EQUATORIAL_RADIUS_M + altitude, 0.0, 0.0),
        (0.0, 1200.0, 100.0),
        dyn.IDENTITY,
        (0.0, 0.001, 0.0),
        fuel,
        tuple(dyn.EngineState() for _ in craft.engines),
        tuple(0.0 for _ in craft.aero_panels),
    )
    return state, craft


@contextmanager
def boundary(choices=("continue_mission",), *, mode="fixture"):
    hr, cw = approval_pipe()
    cr, hw = approval_pipe()
    clock, source = Clock(), source_manifest()
    profile = json.loads((ROOT / "examples/spaceflight/starship-sixdof-profile.json").read_text())
    client = rpc.FinalObservationClient(
        cw, cr, plan_sha256="a" * 64, source_sha256=rpc.digest(source), clock=clock
    )
    transport = FixtureTransport(choices)
    agent = FinalSupervisionAgent(transport=transport, clock=clock)
    host = rpc.FinalObservationHost(
        hr,
        hw,
        plan_sha256="a" * 64,
        source_sha256=rpc.digest(source),
        handler=agent.assess,
        clock=clock,
    )
    observer = supervision.FinalPrefixSupervision(
        profile,
        run_id="prefix-test",
        plan_sha256="a" * 64,
        source_sha256=source,
        scope_sha256="c" * 64,
        client=client,
        mode=mode,
        clock=clock,
    )
    try:
        yield observer, host, transport, clock, profile, source, hw
    finally:
        for endpoint in (hr, cw, cr, hw):
            endpoint.close()


def pump(observer, host, state, craft, *, body="ascent", phase=None):
    phase = phase or ("stack_ascent" if body == "ascent" else "booster_boostback_slew")
    for _ in range(1000):
        host.tick()
        action = (
            observer.observe_ascent(state, craft, phase)
            if body == "ascent"
            else observer.observe_booster(state, craft, phase)
        )
        if observer.pending_epoch is None and not observer.record()["queued"]:
            return action
        Event().wait(0.001)
    pytest.fail("The local RPC fixture did not complete")


def test_baseline_same_tick_projection_real_pipe_and_finite_fixture_provenance():
    with boundary() as (observer, host, transport, _, profile, _, _):
        state, craft = actual(profile, kind="booster", now=29.75)
        observer.observe_ascent(state, craft, "stack_ascent")
        assert observer.pending_epoch is None
        state = replace(state, time_s=30.0)
        original = rpc.digest({"state": asdict(state), "vehicle": asdict(craft)})
        observer.observe_ascent(state, craft, "stack_ascent")
        assert observer.pending_epoch == {
            "request_id": "prefix-test-ascent-1",
            "domain": "ascent",
            "started_s": 30.0,
            "deadline_s": 65.0,
            "started_wall_s": 100.0,
            "wall_deadline_s": 135.0,
        }
        pump(observer, host, replace(state, time_s=30.25), craft)
        report = observer.record()
        assert len(report["requests"]) == len(report["judgments"]) == len(transport.calls) == 1
        row, result = report["requests"][0], report["judgments"][0]["result"]
        assert supervision.validate_saved_judgment(row, result, mode="fixture") == result
        assert report["judgments"][0]["accepted"] is True and not report["deployment_withheld"]
        assert report["judgments"][0]["native_model_judgment"] is False
        assert report["provider_calls_by_worker"] == 0 and report["physical_clock_paused"] is False
        assert original == rpc.digest({"state": asdict(state), "vehicle": asdict(craft)})
        wire = json.dumps(transport.calls[0])
        assert all(
            key not in wire
            for key in ("engine_states", "r_eci_m", "source_sha256", "seed", "fault_label")
        )
        assert report["policy"]["health_channel"].startswith("idealized_current_observed")


def test_observed_availability_decrease_requests_judgment_and_withhold_is_sticky():
    with boundary(("continue_mission", "withhold_deployment")) as (
        observer,
        host,
        transport,
        _,
        profile,
        _,
        _,
    ):
        state, craft = actual(profile, kind="booster", now=30.0)
        observer.observe_ascent(state, craft, "stack_ascent")
        pump(observer, host, replace(state, time_s=30.25), craft)
        engines = tuple(
            replace(row, available=False) if i == 0 else row
            for i, row in enumerate(state.engine_states)
        )
        changed = replace(state, time_s=76.0, engine_states=engines)
        observer.observe_ascent(changed, craft, "stack_ascent")
        assert (
            observer.record()["requests"][-1]["trigger_reason"]
            == "observed_main_availability_count_decreased"
        )
        assert pump(observer, host, replace(changed, time_s=76.25), craft) == "withhold_deployment"
        assert observer.deployment_withheld
        observer.observe_ascent(replace(changed, time_s=90.0), craft, "stack_ascent")
        assert observer.deployment_withheld and len(transport.calls) == 2


def test_stage_change_is_not_a_fictitious_engine_loss_event():
    with boundary() as (observer, host, transport, _, profile, _, _):
        state, craft = actual(profile, kind="booster", now=30.0)
        observer.observe_ascent(state, craft, "stack_ascent")
        pump(observer, host, replace(state, time_s=30.25), craft)
        second, ship = actual(profile, now=142.0)
        observer.observe_ascent(second, ship, "ship_ascent")
        assert len(observer.requests) == len(transport.calls) == 1


def test_booster_review_uses8s_original_epoch_no_deepseek_and_keeps_preapproved_gnc():
    with boundary(("splashdown",)) as (observer, host, transport, clock, profile, _, _):
        state, craft = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        observer.observe_booster(state, craft, "booster_boostback_slew")
        assert observer.pending_epoch["deadline_s"] == 150.0
        clock.now += 0.1  # Pipe/setup consumes the original8s; it does not renew it.
        pump(observer, host, replace(state, time_s=142.25), craft, body="booster")
        assert not observer.recovery_refused
        public = transport.calls[0]["state"]
        assert public["evidence"]["history.supported_catches"] == 0
        assert "request_deep_reasoning" not in public["allowed_actions"]
        assert observer.record()["booster_initial_burn_selected_by_ai"] is False
        assert observer.record()["numerical_gnc_waited_for_model"] is False
        assert (
            observer.judgments[0]["result"]["provider_receipt"]["original_deadline_monotonic"]
            == 108.0
        )


def test_valid_booster_inhibit_is_preserved_not_changed_to_splashdown():
    with boundary(("inhibit_recovery",)) as (observer, host, _, _, profile, _, _):
        state, craft = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        observer.observe_booster(state, craft, "booster_boostback_slew")
        assert (
            pump(observer, host, replace(state, time_s=142.25), craft, body="booster")
            == "inhibit_recovery"
        )
        assert observer.recovery_refused and observer.judgments[0]["action"] == "inhibit_recovery"


def test_busy_channel_does_not_restart_the_booster_epoch_or_poll_it_as_another_domain():
    with boundary() as (observer, _, _, clock, profile, _, _):
        state, craft = actual(profile, now=30.0)
        observer.observe_ascent(state, craft, "ship_ascent")
        separated, booster_craft = actual(profile, kind="booster", now=31.0, fuel=260000.0)
        observer.observe_booster(separated, booster_craft, "booster_boostback_slew")
        queued = observer.record()["queued"][0]
        assert queued["started_s"] == 31.0 and queued["deadline_s"] == 39.0
        clock.now = 135.0
        observer.observe_booster(
            replace(separated, time_s=65.0), booster_craft, "booster_boostback"
        )
        assert observer.pending_epoch is None and observer.client.sequence == 1
        assert observer.deployment_withheld and not observer.recovery_refused
        assert observer.judgments[-1]["reason"] == "original_epoch_expired_while_rpc_busy"
        assert observer.judgments[-1]["domain"] == "booster" and not observer.record()["queued"]


def test_response_inside_transport_deadline_but_without_apply_reserve_is_not_applied():
    with boundary(("inhibit_recovery",)) as (observer, host, _, clock, profile, _, _):
        state, craft = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        observer.observe_booster(state, craft, "booster_boostback_slew")
        # Complete host work without letting the client receive it yet.
        for _ in range(1000):
            host.tick()
            if host.sessions and host.pending is None:
                host.writer.flush()
                break
            Event().wait(0.001)
        clock.now = 106.0
        observer.observe_booster(replace(state, time_s=148.0), craft, "booster_boostback")
        assert not observer.recovery_refused
        assert observer.judgments[-1]["accepted"] is False
        assert observer.judgments[-1]["reason"] == "original_application_reserve_consumed"


@pytest.mark.parametrize("change", ["source", "profile", "client", "binding"])
def test_runtime_binding_cannot_change(change):
    with boundary() as (observer, _, _, _, profile, source, _):
        if change == "source":
            source["other"] = "a" * 64
        elif change == "profile":
            profile["ship"]["dry_mass_kg"] += 1
        elif change == "client":
            observer.client.binding["plan_sha256"] = "f" * 64
        else:
            observer.binding["mode"] = "live"
        with pytest.raises(ValueError):
            observer.validate_scope(profile, source)


def test_fixture_reply_cannot_become_native_evidence():
    with boundary(mode="live") as (observer, host, transport, _, profile, _, _):
        state, craft = actual(profile, now=30.0)
        observer.observe_ascent(state, craft, "ship_ascent")
        pump(observer, host, replace(state, time_s=30.25), craft, phase="ship_ascent")
        assert observer.deployment_withheld and observer.judgments[0]["accepted"] is False
        assert len(transport.calls) == 1
        assert observer.judgments[0]["native_model_judgment"] is False


def test_supervision_does_not_consume_foreign_visual_pending():
    with boundary() as (observer, _, _, clock, profile, _, _):
        now = 30.0
        manifest = {
            "schema": "missionos.starship_visual_evidence.v1",
            "run_id": "prefix-test",
            "vehicle_id": "ship",
            "snapshot_time_s": now,
            "frames": [],
        }
        manifest["manifest_sha256"] = rpc.digest(manifest)
        visual = {
            "schema": "missionos.starship_visual_request.v1",
            "run_id": "prefix-test",
            "request_id": "visual-1",
            "episode_id": "episode-1",
            "vehicle_id": "ship",
            "release_attempt_id": "attempt-1",
            "event_id": "event-1",
            "authority_sha256": "a" * 64,
            "state_generation": 13,
            "evidence_kind": "numeric_followup",
            "snapshot_time_s": now,
            "deadline_s": now + 35.0,
            "manifest": manifest,
            "telemetry": {},
            "allowed_actions": ["continue", "stop_deployment"],
            "observation_choices": ["none"],
            "prior_diagnosis": {"diagnosis_id": "d" * 64},
        }
        envelope = {
            "schema": "missionos.starship_final_visual_request.v1",
            "run_id": "prefix-test",
            "plan_sha256": "a" * 64,
            "source_sha256": observer.binding["source_sha256"],
            "mode": "fixture",
            "policy_sha256": "b" * 64,
            "request": visual,
            "request_sha256": rpc.digest(visual),
            "frame_refs": [],
            "started_wall_s": clock(),
            "wall_deadline_s": clock() + 35.0,
        }
        envelope["envelope_sha256"] = rpc.digest(envelope)
        foreign = rpc.request("foreign-visual", "visual", now, now + 35.0, envelope)
        observer.client.begin(foreign, started_wall_s=clock())
        state, craft = actual(profile, now=now)
        observer.observe_ascent(state, craft, "ship_ascent")
        assert observer.pending_epoch is None and len(observer.record()["queued"]) == 1
        assert observer.client.pending["frame"]["request"] == foreign
        assert observer.client.completed == []


def test_ascent_withhold_closes_actual_inventory26_after_real_settled_handoff():
    from src.runtime.starship_restored_prefix import RestoredPrefixRuntime
    from tests.contract.test_starship_restored_prefix import setup as existing_setup

    with boundary(("withhold_deployment",)) as (observer, host, _, _, profile, source, _):
        state, craft = actual(profile, now=30.0, altitude=300000.0)
        observer.observe_ascent(state, craft, "ship_ascent")
        pump(observer, host, replace(state, time_s=30.25), craft, phase="ship_ascent")
        assert observer.deployment_withheld
        raw, _, settings, controls, *_ = existing_setup.__wrapped__()
        ordinary_calls = []
        driver = RestoredPrefixRuntime(
            raw,
            source,
            settings,
            controls,
            7500.0,
            lambda summary: ordinary_calls.append(summary) or "continue_deployment",
            final_prefix_supervision=observer,
        )
        driver.begin(profile, settings, 7500.0)
        radius = env.EARTH_EQUATORIAL_RADIUS_M + 300000.0
        settled = replace(
            state,
            time_s=1805.0,
            r_eci_m=(radius, 0.0, 0.0),
            v_eci_mps=(0.0, math.sqrt(env.EARTH_MU_M3_S2 / radius), 0.0),
        )
        cutoff = replace(settled, time_s=1800.0)
        schedule = {
            "final_gate_time_s": 1800.0,
            "first_release_time_s": 1845.0,
            "return_request_time_s": 3600.0,
        }
        before = rpc.digest(asdict(settled))
        driver.settled(settled, schedule)
        assert driver.closed and len(driver._receipts) == 0 and ordinary_calls == []
        assert (
            driver._decisions[0]["origin"] == "final_ascent_supervision_withhold_not_approval_proof"
        )
        result = {
            "outcome": {"termination": "restored_deployment_handoff", "phase": "orbital_coast"},
            "final_state": asdict(settled),
            "final_vehicle": asdict(craft),
            "experimental_mission_ascent": {
                "schema": "missionos.experimental_state_mission_ascent.v1",
                "settings": settings.record(),
                "cutoff_measurement": {
                    "time_s": 1800.0,
                    "state": asdict(cutoff),
                    "gate_reached": True,
                },
                "insertion_shutdown_until_s": 1805.0,
                "schedule": schedule,
                "settled_measurement": {
                    "time_s": 1805.0,
                    "state": asdict(settled),
                    "vehicle": asdict(craft),
                    "gate_reached": True,
                    "orbit": env.orbital_elements(mission.point_state(settled)),
                },
            },
        }
        proof = driver.finish(result)
        assert proof["origin_validation_issue"] is None
        assert driver.origin().record()["retained_count"] == 26
        assert proof["handoff_record"]["inventory"]["release_receipts"] == []
        assert rpc.digest(proof["handoff_record"]["final_state"]) == before


def test_production_booster_hook_refuses_body_at_actual_state_without_a_plant_step():
    from tests.contract.test_starship_restored_prefix import setup as existing_setup

    with boundary(("inhibit_recovery",)) as (observer, host, _, _, profile, _, _):
        state, craft = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        observer.observe_booster(state, craft, "booster_boostback_slew")
        current = replace(state, time_s=142.25)
        pump(observer, host, current, craft, body="booster")
        raw, _, _, controls, _, sites, goal = existing_setup.__wrapped__()
        generator = booster._simulate_booster_steps(
            profile,
            asdict(current),
            duration_s=1.0,
            guidance_policy="availability_terminal_recovery_v1",
            terminal_recovery_settings=controls,
            return_sites=sites,
            splashdown_goal=goal,
            final_prefix_supervision=observer,
        )
        assert next(generator).state == current
        with pytest.raises(StopIteration) as complete:
            next(generator)
        result = complete.value.value
        assert result["outcome"]["termination"] == "booster_recovery_refused_by_supervision"
        assert result["outcome"]["integration_steps"] == 0
        assert result["outcome"]["recovery_unresolved"] is True
        assert (
            result["outcome"]["simulated_contact_envelope_met"] is False
            and result["contact"] is None
        )
        assert rpc.digest(result["final_state"]) == rpc.digest(asdict(current))
        assert result["outcome"]["end_time_s"] == current.time_s


def test_booster_hook_is_explicit_typed_and_cannot_borrow_default_controller():

    with boundary() as (observer, _, _, _, profile, _, _):
        state, _ = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        for invalid in (True, observer):
            with pytest.raises(ValueError, match="exact_terminal_scope"):
                next(
                    booster._simulate_booster_steps(
                        profile, asdict(state), duration_s=1.0, final_prefix_supervision=invalid
                    )
                )
        original = booster._simulate_booster_steps(profile, asdict(state), duration_s=1.0)
        explicit_none = booster._simulate_booster_steps(
            profile, asdict(state), duration_s=1.0, final_prefix_supervision=None
        )
        assert next(original) == next(explicit_none)
        original.close()
        explicit_none.close()


def test_original_epoch_includes_actual_witness_preparation_and_unavailable_queue_is_retained():
    with boundary() as (observer, _, _, clock, profile, _, _):
        state, craft = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        assert observer.observation_due(state, craft, "booster_boostback_slew", body="booster")
        started = clock()
        before = rpc.digest({"state": asdict(state), "vehicle": asdict(craft)})
        clock.now += 0.25
        observer.observe_booster(
            state, craft, "booster_boostback_slew", observation_started_wall_s=started
        )
        assert observer.pending_epoch["started_wall_s"] == started
        assert observer.pending_epoch["wall_deadline_s"] == started + 8.0
        assert not observer.observation_due(state, craft, "booster_boostback_slew", body="booster")
        assert before == rpc.digest({"state": asdict(state), "vehicle": asdict(craft)})
    with boundary() as (observer, _, _, clock, profile, _, _):
        state, craft = actual(profile, now=30.0)
        observer.observe_ascent(state, craft, "ship_ascent")
        separated, craft = actual(profile, kind="booster", now=31.0, fuel=260000.0)
        observer.observe_booster(separated, craft, "booster_boostback_slew")
        clock.now = 135.0
        observer.observe_booster(replace(separated, time_s=65.0), craft, "booster_boostback")
        assert len(observer.record()["requests"]) == 2
        assert {row["request_id"] for row in observer.record()["judgments"]} == {
            row["request_id"] for row in observer.record()["requests"]
        }


def test_production_booster_trigger_captures_separate_actual_witness_before_observer(monkeypatch):
    from tests.contract.test_starship_restored_prefix import setup as existing_setup

    with boundary(("inhibit_recovery",)) as (observer, host, _, _, profile, _, _):
        state, _ = actual(profile, kind="booster", now=142.0, fuel=260000.0)
        _, _, _, controls, _, sites, goal = existing_setup.__wrapped__()
        real_observe = observer.observe_booster

        def complete_current_tick(current, craft, phase, **kwargs):
            real_observe(current, craft, phase, **kwargs)
            for _ in range(1000):
                host.tick()
                result = real_observe(current, craft, phase)
                if observer.pending_epoch is None:
                    return result
                Event().wait(0.001)
            pytest.fail("The inherited RPC did not finish")

        monkeypatch.setattr(observer, "observe_booster", complete_current_tick)
        iterator = booster._simulate_booster_steps(
            profile,
            asdict(state),
            duration_s=1.0,
            guidance_policy="availability_terminal_recovery_v1",
            terminal_recovery_settings=controls,
            return_sites=sites,
            splashdown_goal=goal,
            final_prefix_supervision=observer,
        )
        assert next(iterator).state == state
        with pytest.raises(StopIteration) as completed:
            next(iterator)
        report = completed.value.value
        witness = report["final_supervision_actual_witnesses"][0]
        row = observer.record()["requests"][0]
        assert witness["request_ids"] == [row["request_id"]]
        assert (
            rpc.digest(witness["state"])
            == witness["state_sha256"]
            == row["observation"]["state_sha256"]
        )
        assert (
            rpc.digest(witness["vehicle"])
            == witness["vehicle_sha256"]
            == row["observation"]["vehicle_sha256"]
        )
        assert witness["state"] == asdict(state) and witness["time_s"] == state.time_s
        assert witness["origin"] == "actual_plant_tick_before_supervision"
        assert witness["state_or_inventory_assigned"] is False
        assert report["outcome"]["integration_steps"] == 0 and report["contact"] is None
