"""Final same-flight visual seam: pixels/HTTP fixtures, no model or flight runs."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from src.runtime import starship_sixdof as dyn
from src.runtime.starship_final_visual import (
    FinalVisualHost, FinalVisualObserver, FinalVisualPolicy, PENDING, digest, validate_result,
)
from src.runtime.starship_sixdof_mission import payload_inertia, vehicle
from src.runtime.starship_sixdof_separation import release_payload
from src.intelligence.starship_visual_transport import fixture_provider

ROOT = Path(__file__).resolve().parents[2]
SOURCE = {"src/runtime/starship_visual_observation.py": sha256(
    (ROOT/"src/runtime/starship_visual_observation.py").read_bytes()).hexdigest()}
BINDING = {"run_id": "final-visual-test", "plan_sha256": "a"*64,
           "source_sha256": SOURCE, "mode": "fixture"}


class Clock:
    def __init__(self):
        self.value = 1000.
    def __call__(self):
        return self.value


class Plant:
    """Algebraic rigid release receipts; actual plant integration is never run."""
    def __init__(self):
        self.profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
        self.craft = vehicle(self.profile)
        self.state = dyn.State6DOF(100., (7e6, 0., 0.), (0., 7500., 0.), dyn.IDENTITY,
            (0., 0., 0.), 45000., tuple(dyn.EngineState() for _ in self.craft.engines),
            tuple(0. for _ in self.craft.aero_panels))
        self.records = []
    def release(self):
        count = len(self.records)+1
        after = vehicle(self.profile, payload_count=26-count)
        mass = self.profile["payload"]["mass_each_kg"]
        parent_mass = dyn.mass_properties(after, self.state.propellant_kg).mass_kg
        reduced = parent_mass*mass/(parent_mass+mass)
        self.state, child, craft, receipt = release_payload(self.state, self.craft, after, mass,
            (0., 0., self.profile["payload"]["center_z_m"]), payload_inertia(self.profile),
            (reduced*self.profile["payload"]["release_speed_mps"], 0., 0.))
        self.craft = after
        self.records.append({"id": f"satellite_{count:02d}", "release_state": asdict(child),
            "vehicle": asdict(craft), "receipt": receipt})
    def tick(self, observer, now, **kw):
        self.state = replace(self.state, time_s=now)
        before = digest({"state": asdict(self.state), "vehicle": asdict(self.craft), "records": self.records})
        result = observer.observe(self.state, self.craft, self.records, return_deadline_s=1000., **kw)
        assert digest({"state": asdict(self.state), "vehicle": asdict(self.craft), "records": self.records}) == before
        return result


def episode(tmp_path, *, decide=lambda _: PENDING, clock=None, numeric=True):
    plant = Plant()
    clock = Clock() if clock is None else clock
    observer = FinalVisualObserver(plant.profile, tmp_path, **BINDING, decide=decide, clock=clock)
    plant.tick(observer, 100.)
    for count in range(1, 14):
        when = 100.+count*2
        plant.state = replace(plant.state, time_s=when)
        plant.release()
        plant.tick(observer, when)
        plant.tick(observer, when+.5)
        plant.tick(observer, when+1.)
        plant.tick(observer, when+1.5)
    # 13th release is at126; timeout starts the original bounded hold at128.
    plant.tick(observer, 128., numeric_release_ready=numeric)
    assert len(observer.requests) == 1
    return observer, plant, clock


def visual_script(action="request_numeric_confirmation", choice="fresh_separation_counter"):
    return {"visibility": "adequate", "deployment_visual_state": "separation_visible",
        "telemetry_relation": "insufficient", "recommended_action": action,
        "observation_choice": choice, "frame_support": "supports"}


def final_script(action="continue"):
    return {"telemetry_relation": "consistent", "recommended_action": action,
            "observation_choice": "none"}


def host(root, endpoint, clock=None, **kw):
    return FinalVisualHost(root, **BINDING, fixture_endpoint=endpoint,
        clock=Clock() if clock is None else clock, **kw)


def test_trigger_only_at_thirteenth_rigid_release_and_pending_is_same_request(tmp_path):
    seen = []
    observer, plant, _ = episode(tmp_path, decide=lambda env: seen.append(env) or PENDING)
    assert observer.channel.current_attempt["expected_count"] == 13
    assert len(observer.channel.attempts) == 1
    assert not observer.release_allowed(128.) and not observer.withhold_deployment
    assert observer.record()["episode"]["started_s"] == 128.
    first = deepcopy(observer.requests[0])
    for when in (128.5, 129., 129.5, 130.):
        plant.tick(observer, when)
    assert len(observer.requests) == 1 and all(item == first for item in seen)
    request = first["request"]
    assert request["state_generation"] == request["telemetry"]["expected_count"] == 13
    assert request["telemetry"]["last_confirmed_count"] == 12
    assert "continue" not in request["allowed_actions"]
    assert len(request["manifest"]["frames"]) > 0
    wire = json.dumps(request)
    assert all(term not in wire for term in ("physical_count", "fault_label", "blocked", "true_count"))
    assert observer.record()["native_judgments"] == 0
    assert observer.record()["inventory_assigned"] is False


def test_real_loopback_pixels_then_fresh_counter_resume_has_no_model_claim(tmp_path):
    with fixture_provider([visual_script(), final_script()]) as provider:
        observer, plant, clock = episode(tmp_path)
        judge = host(tmp_path, provider["endpoint"], clock)
        observer.decide = judge.assess
        plant.tick(observer, 128.5)
        assert len(observer.observations) == 1
        assert observer.observations[0]["action"] == "request_numeric_confirmation"
        assert not observer.release_allowed(128.5)
        # The13th receipt arrives at130, but its source is126. A genuinely new
        # sample is required after the128.5 request; that arrives at130.5.
        plant.tick(observer, 129.)
        plant.tick(observer, 129.5)
        plant.tick(observer, 130.)
        assert len(observer.requests) == 1
        plant.tick(observer, 130.5)
        report = observer.record()
        assert report["closed_reason"] == "accepted_visual_proposal"
        assert report["withhold_deployment"] is False and observer.release_allowed(130.5)
        assert len(observer.requests) == 2
        second = observer.requests[1]["request"]
        assert second["evidence_kind"] == "numeric_followup" and second["manifest"]["frames"] == []
        assert set(second["allowed_actions"]) == {"continue", "stop_deployment"}
        assert second["telemetry"]["counter_source_time_s"] > 128.5
        assert [row["image_header"] for row in provider["requests"]] == [True, False]
        assert all(row["image_bytes"] > 0 for row in provider["requests"][:1])
        assert report["native_judgments"] == 0 and report["ai_visual_work_completed"] is False
        assert len(judge.record()["attempts"]) == 2
        assert all(row["accepted"] for row in report["results"])
        assert len(plant.records) == 13
        for row in report["results"]:
            assert row["reply"]["result"]["provider_receipt"]["model_inference_invoked"] is False
        # Duplicate request is rejected before a third local or external send.
        with pytest.raises(ValueError, match="replay_deadline_or_budget"):
            judge.assess(observer.requests[0])
        assert len(provider["requests"]) == 2


def test_valid_stop_is_preserved_at_supported_inventory_thirteen(tmp_path):
    with fixture_provider([visual_script("stop_deployment", "none")]) as provider:
        observer, plant, clock = episode(tmp_path)
        judge = host(tmp_path, provider["endpoint"], clock)
        observer.decide = judge.assess
        assert plant.tick(observer, 128.5) == "withhold_deployment"
        assert observer.withhold_deployment and not observer.release_allowed(132.)
        assert observer.record()["closed_reason"] == "accepted_visual_proposal"
        assert len(plant.records) == 13
        plant.tick(observer, 132.)
        assert len(provider["requests"]) == 1


def test_additional_camera_requires_new_delivered_frame_and_same_epoch(tmp_path):
    second = visual_script("continue", "none")
    second["telemetry_relation"] = "consistent"
    with fixture_provider([visual_script("request_visual_observation", "alternate_camera"), second]) as provider:
        observer, plant, clock = episode(tmp_path)
        judge = host(tmp_path, provider["endpoint"], clock)
        observer.decide = judge.assess
        plant.tick(observer, 128.5)
        for when in (129., 129.5, 130., 130.5):
            plant.tick(observer, when)
            if observer.record()["closed_reason"] is not None:
                break
        assert observer.record()["closed_reason"] == "accepted_visual_proposal"
        first, additional = observer.requests
        assert additional["started_wall_s"] == first["started_wall_s"]
        assert additional["wall_deadline_s"] == first["wall_deadline_s"]
        assert additional["request"]["deadline_s"] == first["request"]["deadline_s"]
        assert all(f["camera_id"] == "deployment_port_secondary" and f["capture_sim_time_s"] > 128.5
                   for f in additional["request"]["manifest"]["frames"])
        assert len(provider["requests"]) == 2


@pytest.mark.parametrize("change", ["hash", "path", "renderer", "camera", "symlink", "pixel"])
def test_host_rejects_substituted_or_unsafe_pixels_before_any_http(tmp_path, change):
    observer, _, clock = episode(tmp_path)
    envelope = deepcopy(observer.requests[0])
    frame = envelope["request"]["manifest"]["frames"][0]
    ref = envelope["frame_refs"][0]
    if change == "hash":
        ref["sha256"] = "0"*64
    elif change == "path":
        ref["path"] = "../frames/"+frame["image_sha256"]+".png"
    elif change == "renderer":
        frame["renderer_source_sha256"] = "0"*64
    elif change == "camera":
        frame["camera_config_sha256"] = "0"*64
    elif change == "pixel":
        (tmp_path/ref["path"]).write_bytes(b"notpng")
    else:
        original = (tmp_path/ref["path"]).read_bytes()
        outside = tmp_path/"outside.png"
        outside.write_bytes(original)
        (tmp_path/ref["path"]).unlink()
        (tmp_path/ref["path"]).symlink_to(outside)
    if change in {"renderer", "camera"}:
        manifest = envelope["request"]["manifest"]
        manifest["manifest_sha256"] = digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        envelope["request_sha256"] = digest(envelope["request"])
    envelope["envelope_sha256"] = digest({k: v for k, v in envelope.items() if k != "envelope_sha256"})
    with fixture_provider([visual_script()]) as provider:
        judge = host(tmp_path, provider["endpoint"], clock)
        with pytest.raises((ValueError, OSError)):
            judge.assess(envelope)
        assert provider["requests"] == []


@pytest.mark.parametrize("change", ["binding", "nan", "fixture_as_live", "late_wall", "late_sim"])
def test_invalid_or_late_result_stops_without_retry(tmp_path, change):
    observer, plant, clock = episode(tmp_path)
    with fixture_provider([visual_script("stop_deployment", "none")]) as provider:
        judge = host(tmp_path, provider["endpoint"], clock)
        result = judge.assess(observer.requests[0])
        if change == "binding":
            result["request_sha256"] = "0"*64
        elif change == "nan":
            result["result"]["untrusted"] = float("nan")
        elif change == "fixture_as_live":
            observer.binding["mode"] = "live"
            result["mode"] = "live"
        elif change == "late_wall":
            clock.value += observer.policy.hold_s
        observer.decide = lambda _: result
        when = 163. if change == "late_sim" else 128.5
        plant.tick(observer, when)
        assert observer.withhold_deployment and len(observer.requests) == 1
        plant.tick(observer, when+.5)
        assert len(provider["requests"]) == 1
        assert observer.record()["native_judgments"] == 0
        assert observer.record()["ai_visual_work_completed"] is False


@pytest.mark.parametrize("field,value", [("trigger_released_count", 1), ("trigger_released_count", True),
    ("maximum_judgments", 3), ("maximum_observations", 2), ("hold_s", 100.),
    ("first_counter_delay_s", 1.), ("width", 320), ("height", 180), ("apply_margin_s", 0.)])
def test_policy_rejects_unregistered_or_provider_invalid_dimensions(field, value):
    if field == "width":
        #256x280 is below the historical total area and the current side
        #minimum; the separate send-time check covers wide but short images.
        value = 256
    with pytest.raises(ValueError):
        FinalVisualPolicy(**{field: value})


def test_fixture_result_cannot_claim_native_or_grant_authority(tmp_path):
    observer, _, clock = episode(tmp_path)
    with fixture_provider([visual_script("stop_deployment", "none")]) as provider:
        result = host(tmp_path, provider["endpoint"], clock).assess(observer.requests[0])["result"]
        validate_result(result, observer.requests[0]["request"], "fixture")
        for field in ("approval_granted", "dispatch", "execution_authorized", "physical_execution"):
            changed = deepcopy(result)
            changed["provider_receipt"][field] = True
            with pytest.raises(ValueError, match="provider_receipt"):
                validate_result(changed, observer.requests[0]["request"], "fixture")
        with pytest.raises(ValueError):
            validate_result(result, observer.requests[0]["request"], "live")


def test_provider_rejection_keeps_only_finite_diagnostics_and_never_resends(tmp_path, monkeypatch):
    from src.intelligence import starship_visual_transport as transport
    observed = []
    body = json.dumps({"model": "private-provider-model", "answers": {}, "private": "never-store"}).encode()
    monkeypatch.setattr(transport, "_send_once", lambda *a, **kw:
        (observed.append(1) or ("response", body, 200)))
    observer, plant, clock = episode(tmp_path)
    judge = host(tmp_path, "http://127.0.0.1:12345/v1/systemone", clock)
    observer.decide = judge.assess
    plant.tick(observer, 128.5)
    saved = observer.record()
    assert saved["withhold_deployment"] is True and observed == [1]
    rejection = saved["results"][0]["reply"]
    assert rejection["provider_rejection_category"] == "visual_response_model_mismatch"
    assert rejection["provider_rejection_diagnostics"]["response_model_matches"] is False
    assert rejection["provider_rejection_diagnostics"]["response_model_utf8_bytes"] == len("private-provider-model")
    assert "private-provider-model" not in json.dumps(saved) and "never-store" not in json.dumps(saved)
    plant.tick(observer, 130.5)
    assert observed == [1] and len(observer.requests) == 1


def test_counter_arrival_never_bypasses_the_current_numerical_gate(tmp_path):
    with fixture_provider([visual_script(), final_script("stop_deployment")]) as provider:
        observer, plant, clock = episode(tmp_path)
        judge = host(tmp_path, provider["endpoint"], clock)
        observer.decide = judge.assess
        plant.tick(observer, 128.5)
        for when in (129., 129.5, 130., 130.5):
            plant.tick(observer, when, numeric_release_ready=False)
        assert observer.withhold_deployment
        assert observer.requests[1]["request"]["allowed_actions"] == ["stop_deployment"]
        assert len(plant.records) == 13


def test_host_original_epoch_and_binding_reject_before_send(tmp_path):
    observer, _, clock = episode(tmp_path)
    with fixture_provider([visual_script()]) as provider:
        judge = host(tmp_path, provider["endpoint"], clock)
        for field, value in (("run_id", "other-run"), ("plan_sha256", "b"*64),
                             ("wall_deadline_s", clock.value+36.), ("started_wall_s", clock.value+1.)):
            env = deepcopy(observer.requests[0])
            env[field] = value
            env["envelope_sha256"] = digest({k: v for k, v in env.items() if k != "envelope_sha256"})
            with pytest.raises(ValueError):
                judge.assess(env)
        assert not provider["requests"]


def test_optional_host_owned_ledger_prevents_resend_after_host_restart(tmp_path):
    frames, private = tmp_path/"worker-frames", tmp_path/"host-private"
    private.mkdir()
    observer, _, clock = episode(frames)
    ledger = private/"visual-attempts.json"
    with fixture_provider([visual_script("stop_deployment", "none")]) as provider:
        first = host(frames, provider["endpoint"], clock, ledger_path=ledger)
        valid = first.assess(observer.requests[0])
        assert valid["result"]["status"] == "fixture_valid"
        # Reconstructing the host cannot undo the single-send reservation.
        second = host(frames, provider["endpoint"], clock, ledger_path=ledger)
        rejected = second.assess(observer.requests[0])
        assert rejected["result"]["status"] == "unavailable"
        assert rejected["result"]["provider_receipt"]["status"] == "attempt_ledger_rejected"
        assert len(provider["requests"]) == 1


def prefix_episode(tmp_path, decide, *, ordinary=None):
    """The real prefix runtime sees synthetic present states and rigid receipts."""
    from tests.contract.test_starship_restored_prefix import setup as existing_setup
    from src.runtime.starship_restored_prefix import RestoredPrefixRuntime
    raw, profile, settings, controls, source, *_ = existing_setup.__wrapped__()
    source.update(SOURCE)
    source["src/runtime/starship_final_visual.py"] = sha256((ROOT/"src/runtime/starship_final_visual.py").read_bytes()).hexdigest()
    plant, clock = Plant(), Clock()
    radius = 6378137.+300000.
    plant.state = replace(plant.state, time_s=1805., r_eci_m=(radius, 0., 0.),
        v_eci_mps=(0., (3.986004418e14/radius)**.5, 0.), propellant_kg=80000.)
    observer = FinalVisualObserver(profile, tmp_path, **{**BINDING, "source_sha256": source},
        decide=decide, clock=clock)
    seen = []
    def ordinary_decision(summary):
        seen.append(dict(summary))
        return ordinary(summary) if ordinary else "continue_deployment"
    driver = RestoredPrefixRuntime(raw, source, settings, controls, 7500., ordinary_decision,
        final_visual_observer=observer)
    driver.begin(profile, settings, 7500.)
    settled = deepcopy(plant.state)
    schedule = {"final_gate_time_s": 1800., "first_release_time_s": 1845., "return_request_time_s": 3600.}
    driver.settled(settled, schedule)
    def tick(now, numeric=True):
        plant.state = replace(plant.state, time_s=now)
        before = digest({"state": asdict(plant.state), "vehicle": asdict(plant.craft), "records": plant.records})
        result = driver.observe_visual(plant.state, plant.craft, plant.records, release_ready=numeric)
        assert digest({"state": asdict(plant.state), "vehicle": asdict(plant.craft), "records": plant.records}) == before
        return result
    tick(1805.)
    plant.state = replace(plant.state, time_s=1845.)
    assert driver.decide(plant.state, 0, release_ready=True, sequencer_state="running") == "continue_deployment"
    for index in range(13):
        now = 1845.+index*15
        plant.state = replace(plant.state, time_s=now)
        plant.release()
        driver.released(plant.records[-1]["receipt"])
        tick(now)
        tick(now+.5)
        tick(now+1.)
        tick(now+1.5)
    return driver, observer, plant, clock, tick, seen, (settings, schedule, settled)


def test_prefix_visual_pending_blocks_ordinary_judgment_then_stop_preserves_origin13(tmp_path):
    from src.runtime.starship_restored_prefix import RestoredDeploymentPending
    with fixture_provider([visual_script("stop_deployment", "none")]) as provider:
        driver, observer, plant, clock, tick, seen, (settings, schedule, settled) = prefix_episode(tmp_path, lambda _: PENDING)
        assert type(driver.decide(plant.state, 13, release_ready=True, sequencer_state="running")) is RestoredDeploymentPending
        assert len(seen) == 1 and driver._pending is None and not driver.closed
        tick(2027.)
        judge = FinalVisualHost(tmp_path, **{**BINDING, "source_sha256": json.loads(driver._source_json)},
            fixture_endpoint=provider["endpoint"], clock=clock)
        observer.decide = judge.assess
        assert tick(2027.5) == "withhold_deployment"
        assert driver.closed and len(seen) == 1
        decision = driver._decisions[-1]
        assert decision["action"] == "withhold_deployment"
        assert decision["origin"] == "final_visual_observation_withhold_not_approval_proof"
        assert decision["summary"]["released_count"] == decision["summary"]["retained_count"] == 13
        assert decision["final_visual_observation_sha256"] == digest(observer.record())
        from src.runtime import starship_physics as env
        result = {"outcome": {"termination": "restored_deployment_handoff", "phase": "orbital_coast"},
            "final_state": asdict(plant.state), "final_vehicle": asdict(plant.craft),
            "experimental_mission_ascent": {"schema": "missionos.experimental_state_mission_ascent.v1",
                "settings": settings.record(), "cutoff_measurement": {"time_s": 1800.,
                    "state": asdict(replace(settled, time_s=1800.)), "gate_reached": True},
                "insertion_shutdown_until_s": 1805., "schedule": schedule,
                "settled_measurement": {"time_s": 1805., "state": asdict(settled),
                    "vehicle": asdict(vehicle(plant.profile)), "gate_reached": True,
                    "orbit": env.orbital_elements(env.State3D(settled.time_s, settled.r_eci_m,
                        settled.v_eci_mps, settled.propellant_kg))}}}
        evidence = driver.finish(result)
        assert evidence["handoff_record"]["inventory"]["released_count"] == 13
        assert evidence["handoff_record"]["inventory"]["retained_count"] == 13
        assert digest(evidence["handoff_record"]["final_state"]) == digest(asdict(plant.state))
        assert evidence["origin_validation_issue"] is None
        assert driver.origin().record()["retained_count"] == 13
        assert evidence["state_reset"] is False
        assert len(evidence["handoff_record"]["inventory"]["release_receipts"]) == 13


def test_prefix_visual_resume_still_requires_the_ordinary_deployment_decision(tmp_path):
    with fixture_provider([visual_script(), final_script()]) as provider:
        driver, observer, plant, clock, tick, seen, _ = prefix_episode(tmp_path, lambda _: PENDING,
            ordinary=lambda summary: "withhold_deployment" if summary["released_count"] == 13 else "continue_deployment")
        judge = FinalVisualHost(tmp_path, **{**BINDING, "source_sha256": json.loads(driver._source_json)},
            fixture_endpoint=provider["endpoint"], clock=clock)
        observer.decide = judge.assess
        for when in (2027., 2027.5, 2028., 2028.5, 2029., 2029.5):
            tick(when)
        assert observer.deployment_decision_ready and not observer.withhold_deployment
        assert driver.visual_release_allowed(2029.5) and not driver.closed
        assert len(seen) == 1
        assert driver.decide(plant.state, 13, release_ready=True, sequencer_state="running") == "withhold_deployment"
        assert len(seen) == 2 and driver.closed
        assert driver._decisions[-1]["origin"] == "external_callback_not_model_or_approval_proof"
        assert len(plant.records) == 13


def test_prefix_requires_exact_profile_and_source_bound_visual_observer(tmp_path):
    from tests.contract.test_starship_restored_prefix import setup as existing_setup
    from src.runtime.starship_restored_prefix import RestoredPrefixRuntime
    raw, profile, settings, controls, source, *_ = existing_setup.__wrapped__()
    args = (raw, source, settings, controls, 7500., lambda _: "continue_deployment")
    with pytest.raises(ValueError, match="exact_bound_final_visual_observer"):
        RestoredPrefixRuntime(*args, final_visual_observer=True)
    observer = FinalVisualObserver(profile, tmp_path, **BINDING, decide=lambda _: PENDING)
    with pytest.raises(ValueError, match="exact_bound_final_visual_observer"):
        RestoredPrefixRuntime(*args, final_visual_observer=observer)
    # Default constructor still has no visual policy, no new request or hold.
    ordinary = RestoredPrefixRuntime(*args)
    assert ordinary.final_visual_observer is None and ordinary.visual_release_allowed(0.) is True


@pytest.mark.parametrize("change", ["binding", "profile"])
def test_prefix_cannot_change_visual_binding_after_construction(tmp_path, change):
    driver, observer, plant, _, _, _, _ = prefix_episode(tmp_path, lambda _: PENDING)
    if change == "binding":
        observer.binding["mode"] = "live"
    else:
        observer.collector.world.profile["payload"]["mass_each_kg"] += 1
    with pytest.raises(ValueError, match="visual_binding_changed"):
        driver.visual_release_allowed(plant.state.time_s)
