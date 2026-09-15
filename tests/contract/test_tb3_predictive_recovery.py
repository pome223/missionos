"""Fixture judgments exercise the real shared graph and persistent policy gate.

These are authority/freshness regression tests, not model or navigation proof.
"""

from datetime import datetime, timezone
import json

import pytest

from src.intelligence.navigation_prediction import NavigationPrediction
from src.runtime import tb3_predictive_recovery as runtime
from src.intelligence import navigation_agents


def packet():
    return dict(
        request={"timestamps": [1.0, 1.5, 2.0, 2.5], "context_sha256": ["c" * 64] * 4},
        prediction={
            "learned_wam_invoked": True,
            "current_exposure": 0.4,
            "predicted_exposure": 0.02,
            "candidate": "detour",
            "provenance": {"task_manifest_sha256": "a" * 64},
        },
        observation={"sim_s": 2.6},
    )


def live(folder, sim_s=2.6, stationary=True):
    (folder / "live-observation.json").write_text(
        json.dumps(
            dict(
                sim_s=sim_s,
                camera_stamp_sim_s=sim_s,
                observed_at=datetime.now(timezone.utc).isoformat(),
                robot_stationary=stationary,
                image_sha256="b" * 64,
            )
        )
    )


def test_prediction_factual_immutable_and_clock_bound():
    p = packet()
    forecast = NavigationPrediction(
        request=p["request"], prediction=p["prediction"], clock_domain="one"
    )
    assert "candidate" not in forecast.read()
    assert forecast.current(6.49, "one")
    assert not forecast.current(6.5, "one")
    assert not forecast.current(3, "two")
    assert not forecast.current(float("nan"), "one")
    exposed = forecast.read()
    exposed["provenance"]["task_manifest_sha256"] = "changed"
    assert forecast.read()["provenance"]["task_manifest_sha256"] == "a" * 64


@pytest.fixture
def controller(tmp_path, monkeypatch):
    live(tmp_path)

    def fake(role, phase, evidence):
        current = json.loads((tmp_path / "live-observation.json").read_text())
        live(tmp_path, current["sim_s"], current["robot_stationary"])
        invocation = dict(
            agent_name="fixture_" + role,
            agent_role=role,
            provider="fixture",
            model_id="fixture-no-model",
            invocation_exit_code=0,
            exit_code=0,
        )
        if role == "recovery":
            return dict(
                action=runtime.ACTION,
                prediction_ref=evidence["prediction"]["prediction_ref"],
                rationale="Fixture current .4 versus predicted .02 supports waiting.",
                uncertainty="Fixture prediction requires actual clearance.",
            ), invocation
        return dict(
            proposed_response_kind="continue" if phase == "post_wait" else "hold",
            parameters={},
            rationale="Fixture observation requires bounded response.",
            expected_outcome="Observed clearance before continuing.",
            uncertainty="Fixture only.",
            operator_question="Use existing approved simulation bounds.",
        ), invocation

    monkeypatch.setattr(navigation_agents, "infer", fake)
    monkeypatch.setattr(runtime, "infer", fake)
    return runtime.PredictiveRecovery(tmp_path, operator_authorized=True)


def test_real_graph_policy_reservation_and_post_observation(controller):
    response = controller.plan(packet())
    assert response["status"] == "authorized_wait", response
    assert response["authority"]["budget_reserved"] is True
    denied = controller.post_wait(dict(verification={"passed": False}, observation={}))
    assert denied["status"] == "blocked"
    passed = controller.post_wait(
        dict(
            verification={"passed": True, "image": {"stamp_s": 6.6}},
            observation={"sim_s": 6.6, "camera_stamp_sim_s": 6.6, "robot_stationary": True},
        )
    )
    assert passed["status"] == "continue"
    replay = controller.plan(packet())
    assert replay["status"] == "blocked"


def test_expired_prediction_requires_new_observation(controller):
    live(controller.folder, sim_s=7)
    assert controller.plan(packet())["status"] == "reobserve"
    assert controller.grant is None


def test_moving_robot_cannot_reserve_wait(controller):
    live(controller.folder, stationary=False)
    response = controller.plan(packet())
    assert response["status"] == "blocked", response
    assert controller.grant is None


def test_unapproved_invocation_rejected(tmp_path):
    with pytest.raises(ValueError, match="authorization_required"):
        runtime.PredictiveRecovery(tmp_path, operator_authorized=False)


@pytest.mark.parametrize("read_tool", [True, False])
def test_actual_adk_runner_receives_bound_prefetch_and_optional_tool(monkeypatch, read_tool):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from src.agents import model_config

    class FixtureLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            assert any(
                "prefetched_navigation_evidence" in (p.text or "")
                for c in llm_request.contents
                for p in c.parts
            )
            responses = [
                p.function_response
                for c in llm_request.contents
                for p in c.parts
                if p.function_response
            ]
            if read_tool and not responses:
                content = types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="read_navigation_prediction", args={}
                            )
                        )
                    ],
                )
            else:
                if read_tool:
                    assert responses[-1].response == {"prediction": {"prediction_ref": "fixture"}}
                content = types.Content(
                    role="model", parts=[types.Part(text='{"action":"operator_review"}')]
                )
            yield LlmResponse(content=content)

    monkeypatch.setattr(
        model_config, "resolve_agent_model", lambda **_: FixtureLlm(model="fixture")
    )
    if read_tool:
        output, invocation = navigation_agents.infer(
            "recovery", "test", {"prediction": {"prediction_ref": "fixture"}}
        )
        assert output["action"] == "operator_review"
        assert len(invocation["tool_evidence_sha256"]) == 2
    else:
        output, invocation = navigation_agents.infer(
            "recovery", "test", {"prediction": {"prediction_ref": "fixture"}}
        )
        assert len(invocation["tool_evidence_sha256"]) == 1
        assert invocation["initial_tool_read_mode"] == "agent_runtime_prefetch_before_inference"


def test_stale_during_initial_agent_reobserves_without_reservation(controller, monkeypatch):
    real = runtime.assurance

    def delayed(*args, **kwargs):
        result = real(*args, **kwargs)
        live(controller.folder, 7)
        return result

    monkeypatch.setattr(runtime, "assurance", delayed)
    assert controller.plan(packet())["status"] == "reobserve"
    assert controller.grant is None
    assert not any(e["phase"] == "rules" for e in controller.records)


def test_recovery_detour_is_not_replaced_by_deterministic_wait(controller, monkeypatch):
    real = runtime.infer

    def detour(*args):
        result, evidence = real(*args)
        result["action"] = "detour"
        return result, evidence

    monkeypatch.setattr(runtime, "infer", detour)
    assert controller.plan(packet())["status"] == "blocked"
    assert controller.grant is None
    assert any(
        e["phase"] == "recovery" and e["result"]["output"]["action"] == "detour"
        for e in controller.records
    )


def test_revoked_policy_cannot_reserve_wait(controller):
    controller.store.revoke(controller.policy.sha256)
    with pytest.raises(ValueError, match="approved_active_policy_required"):
        controller.plan(packet())
    assert controller.grant is None


@pytest.mark.parametrize("ready", [True, False])
def test_readiness_uses_real_adapter_before_any_mission(tmp_path, monkeypatch, ready):
    from scripts.tb3_prediction.agent_service import verify_readiness

    calls = []

    def fake(role, phase, evidence):
        calls.append((role, phase, evidence))
        return {"ready": ready}, {"provider": "fixture", "invocation_kind": "fixture"}

    monkeypatch.setattr(navigation_agents, "infer", fake)
    if ready:
        result = verify_readiness(tmp_path)
        assert result["mission_judgment"] is False
        assert result["simulator_invoked"] is False
        assert result["dispatch_authority_created"] is False
    else:
        with pytest.raises(RuntimeError, match="readiness_not_confirmed"):
            verify_readiness(tmp_path)
    assert calls == [("assurance", "readiness", {"purpose": "runtime_readiness_without_mission"})]
    assert (tmp_path / "service-readiness.json").exists()
    assert not (tmp_path / "operator-policy.sqlite").exists()


def test_expiry_before_any_judgment_retains_auditable_chain(controller, monkeypatch):
    live(controller.folder, 7.0)
    monkeypatch.setattr(
        runtime, "assurance", lambda *a, **kw: pytest.fail("stale forecast reached model")
    )
    assert controller.plan(packet()) == {
        "status": "reobserve",
        "reason": "prediction_expired_before_assurance",
    }
    chain = json.loads((controller.folder / "agent-chain.json").read_text())
    event = chain["events"][0]
    assert event["phase"] == "stale_prediction"
    assert event["target_at_sim_s"] == 6.5
    assert event["observed_now_sim_s"] == 7.0
    assert event["dispatch_authority_created"] is False
    assert controller.grant is None


@pytest.fixture
def detour_controller(controller, monkeypatch):
    original = navigation_agents.infer

    def judged(role, phase, evidence):
        if phase == "failed_wait":
            bound = evidence["navigation"]["approved_detour_policy"]
            assert bound["authority_source"] == "human_approved_policy"
            assert "reroute" in bound["policy"]["actions"]
            assert bound["policy_sha256"]
        if phase == "failed_wait_detour":
            return {
                "action": "detour",
                "observation_ref": evidence["observation_ref"],
                "rationale": "Actual clearance failed; try the existing bounded detour.",
                "uncertainty": "Nav2 must verify arrival.",
            }, {
                "provider": "fixture",
                "model_id": "fixture",
                "agent_name": "fixture_recovery",
                "agent_role": "recovery",
                "invocation_exit_code": 0,
                "exit_code": 0,
            }
        output, invocation = original(role, phase, evidence)
        if phase == "detour_alignment":
            output["proposed_response_kind"] = "replan"
        return output, invocation

    monkeypatch.setattr(navigation_agents, "infer", judged)
    monkeypatch.setattr(runtime, "infer", judged)
    c = runtime.PredictiveRecovery(
        controller.folder, operator_authorized=True, detour_authorized=True
    )
    assert c.plan(packet())["status"] == "authorized_wait"
    live(c.folder, 7.0)
    return c


def failed_observation():
    return {
        "verification": {
            "passed": False,
            "observed_exposure": 0.4,
            "image": {"stamp_s": 7.0, "sha256": "d" * 64},
        },
        "route_plan": {
            "status": "succeeded",
            "frame_id": "map",
            "computed_at_sim_s": 7.0,
            "waypoints": [[0.35, -0.7], [1.0, 0.0]],
            "path_xy_m": [[-1.0, 0.0], [0.35, -0.7], [1.0, 0.0]],
        },
        "observation": {"sim_s": 7.0, "camera_stamp_sim_s": 7.0, "robot_stationary": True},
    }


def test_failed_wait_detour_uses_real_graph_separate_policy_and_one_use(detour_controller):
    c = detour_controller
    result = c.failed_wait(failed_observation())
    assert result["status"] == "authorized_detour", result
    assert result["waypoints"] == [[0.35, -0.7], [1.0, 0.0]]
    assert result["authority"]["budget_reserved"] is True
    assert c.failed_wait(failed_observation())["status"] == "blocked"
    assert c.policy.sha256 != c.detour_policy.sha256


def test_wait_approval_never_implies_detour(controller):
    controller.plan(packet())
    assert (
        controller.failed_wait(failed_observation())["reason"]
        == "separate_detour_approval_required"
    )


@pytest.mark.parametrize("invalid", ["passed", "clear", "stale", "moving", "stamp", "hash"])
def test_detour_requires_actual_failed_wait_binding(detour_controller, invalid):
    c = detour_controller
    p = failed_observation()
    if invalid == "passed":
        p["verification"]["passed"] = True
    if invalid == "clear":
        p["verification"]["observed_exposure"] = 0
    if invalid == "stale":
        live(c.folder, 8.0)
    if invalid == "moving":
        live(c.folder, 7.0, False)
    if invalid == "stamp":
        p["verification"]["image"]["stamp_s"] = 6.0
    if invalid == "hash":
        p["verification"]["image"]["sha256"] = ""
    assert c.failed_wait(p)["status"] == "blocked"
    assert not c.detour_consumed


def test_revoked_detour_policy_cannot_reserve(detour_controller):
    c = detour_controller
    c.store.revoke(c.detour_policy.sha256)
    with pytest.raises(ValueError, match="approved_active_policy_required"):
        c.failed_wait(failed_observation())
    assert not c.detour_consumed


def test_recovery_cannot_substitute_an_unbound_detour(detour_controller, monkeypatch):
    c = detour_controller

    def altered(*args):
        return {
            "action": "detour",
            "observation_ref": "other-event",
            "rationale": "wrong",
            "uncertainty": "wrong",
        }, {}

    monkeypatch.setattr(runtime, "infer", altered)
    # The service catches graph failure; no policy reservation or dispatch is created.
    with pytest.raises(Exception, match="detour_observation_binding_required"):
        c.failed_wait(failed_observation())
    assert not c.detour_consumed


@pytest.mark.parametrize("change", ["failed", "stale", "wrong_goal", "outside", "miss_goal"])
def test_detour_requires_actual_bounded_nav2_plan(detour_controller, change):
    p = failed_observation()
    plan = p["route_plan"]
    if change == "failed":
        plan["status"] = "failed"
    if change == "stale":
        plan["computed_at_sim_s"] = 4.0
    if change == "wrong_goal":
        plan["waypoints"][0] = [0.4, -0.7]
    if change == "outside":
        plan["path_xy_m"][0] = [-2.0, 0.0]
    if change == "miss_goal":
        plan["path_xy_m"] = [[-1.0, 0.0], [-0.9, 0.0]]
    with pytest.raises(ValueError, match="Nav2_plan"):
        detour_controller.failed_wait(p)
    assert not detour_controller.detour_consumed


@pytest.fixture
def continue_controller(tmp_path, monkeypatch):
    live(tmp_path)

    def fake(role, phase, evidence):
        state = json.loads((tmp_path / "live-observation.json").read_text())
        state["observed_exposure"] = 0.0
        state["observed_at"] = datetime.now(timezone.utc).isoformat()
        (tmp_path / "live-observation.json").write_text(json.dumps(state))
        invocation = dict(
            agent_name="fixture_" + role,
            agent_role=role,
            provider="fixture",
            model_id="fixture",
            invocation_exit_code=0,
            exit_code=0,
        )
        if role == "recovery":
            return dict(
                action="resume_original_route",
                observation_ref=evidence["observation_ref"],
                rationale="Observed exposure zero supports the original goal.",
                uncertainty="Observations may change.",
            ), invocation
        return dict(
            proposed_response_kind="continue",
            parameters={},
            rationale="Actual route is clear.",
            expected_outcome="Continue the original mission.",
            uncertainty="Observe again before dispatch.",
            operator_question="Use the separately approved original goal.",
        ), invocation

    monkeypatch.setattr(navigation_agents, "infer", fake)
    monkeypatch.setattr(runtime, "infer", fake)
    c = runtime.PredictiveRecovery(tmp_path, operator_authorized=True, continue_authorized=True)
    assert c.plan(packet())["status"] == "observe_original_route"
    return c


def clear_packet():
    return dict(
        verification=dict(
            passed=True, observed_exposure=0.0, image=dict(stamp_s=2.6, sha256="b" * 64)
        ),
        observation=dict(sim_s=2.6, camera_stamp_sim_s=2.6, robot_stationary=True),
        route_plan=dict(
            status="succeeded",
            frame_id="map",
            waypoints=[[1.0, 0.0]],
            computed_at_sim_s=2.6,
            path_xy_m=[[-1.0, 0.0], [0.0, 0.0], [1.0, 0.0]],
        ),
    )


def test_separate_continue_policy_real_graph_and_one_use(continue_controller):
    c = continue_controller
    result = c.observed_continue(clear_packet())
    assert result["status"] == "authorized_continue", result
    assert result["authority"]["policy_authorized"] and result["authority"]["budget_reserved"]
    assert result["authority"]["policy_sha256"] != c.policy.sha256
    assert c.grant is None  # no invented executed wait or wait reservation
    assert c.observed_continue(clear_packet())["status"] == "blocked"
    assert c.plan(packet())["status"] == "blocked"


@pytest.mark.parametrize("change", ["blocked", "stale", "moving", "image", "route", "revoked"])
def test_continue_fails_closed_on_invalid_evidence_or_authority(continue_controller, change):
    c = continue_controller
    p = clear_packet()
    if change == "blocked":
        p["verification"]["observed_exposure"] = 0.5
    if change == "stale":
        p["observation"]["sim_s"] = 1.0
    if change == "moving":
        p["observation"]["robot_stationary"] = False
    if change == "image":
        p["verification"]["image"]["stamp_s"] = 2.0
    if change == "route":
        p["route_plan"]["waypoints"] = [[0.35, -0.7], [1.0, 0.0]]
    if change == "revoked":
        c.store.revoke(c.continue_policy.sha256)
    if change in ("route", "revoked"):
        with pytest.raises(ValueError, match="matching_Nav2|approved_active_policy"):
            c.observed_continue(p)
    else:
        assert c.observed_continue(p)["status"] == "blocked"
    assert not c.resume_consumed


def test_wait_approval_does_not_authorize_original_route(controller):
    assert controller.observed_continue(clear_packet())["status"] == "blocked"


@pytest.mark.parametrize(
    "text",
    ["collision-free-claimed route", "safe route", "no collision guarantee", "衝突なし", "安全"],
)
def test_route_safety_wording_is_not_agent_owned(text):
    with pytest.raises(ValueError, match="runtime_owned"):
        runtime.validate_route_explanation(
            dict(rationale=text, uncertainty="Observed exposure may change.")
        )


def test_continue_rechecks_actual_clearance_after_agent(continue_controller, monkeypatch):
    c = continue_controller
    original = runtime.run_missionos_mission_incident_graph

    def changed(**kwargs):
        graph = original(**kwargs)
        p = c.folder / "live-observation.json"
        current = json.loads(p.read_text())
        current["observed_exposure"] = 0.4
        p.write_text(json.dumps(current))
        return graph

    monkeypatch.setattr(runtime, "run_missionos_mission_incident_graph", changed)
    result = c.observed_continue(clear_packet())
    assert result["status"] == "blocked"
    assert "policy_preserve_condition_not_verified" in result["authority"]["blocking_reasons"]
    assert not c.resume_consumed


def test_future_camera_waits_for_clock_without_accepting_future_frame(controller, monkeypatch):
    p = controller.folder / "live-observation.json"
    current = json.loads(p.read_text())
    current["camera_stamp_sim_s"] = 2.604
    p.write_text(json.dumps(current))

    def catch_up(_):
        current["sim_s"] = 2.606
        p.write_text(json.dumps(current))

    monkeypatch.setattr(runtime.time, "sleep", catch_up)
    observed = controller.live()
    assert observed["sim_s"] >= observed["camera_stamp_sim_s"]
    assert observed["camera_stamp_sim_s"] == 2.604


def test_rejected_route_guarantee_cannot_reserve_continue(continue_controller, monkeypatch):
    c = continue_controller
    original = runtime.infer

    def unsupported(role, phase, evidence):
        output, invocation = original(role, phase, evidence)
        output["rationale"] = "The collision-free-claimed route permits continuation."
        return output, invocation

    monkeypatch.setattr(runtime, "infer", unsupported)
    with pytest.raises(Exception, match="route_safety_explanation_is_runtime_owned"):
        c.observed_continue(clear_packet())
    assert not c.resume_consumed
    assert not any(e["phase"] == "continue_rules" for e in c.records)
    assert any(
        "collision-free-claimed" in e["result"]["output"]["rationale"]
        for e in c.records
        if e["phase"] == "continue_recovery"
    )
