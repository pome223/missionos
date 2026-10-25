"""Explicit fixed responses for preflight only; never native model evidence.

Choices depend on current public observations/action IDs, never hidden fault
labels. This module cannot be selected by a live host. It does not prove the
model will make these choices or that the actual plant can complete a mission.
"""
from copy import deepcopy
import json


class FinalFixtureTransport:
    def _invoke(self, provider, payload, receipt, decode):
        public = payload["state"] if provider == "typesafe" else json.loads(payload["messages"][-1]["content"])
        phase = public["phase"]
        facts = public.get("evidence", public.get("facts", {}))
        allowed = public["allowed_actions"]
        if phase == "ascent":
            action = "continue_mission"
        elif phase == "booster":
            action = "splashdown"
        elif phase == "deployment":
            action = "withhold_deployment" if facts["released_count"] >= 13 else "continue_deployment"
        elif phase == "sensor":
            if "request_independent_tracking" in allowed:
                action = "request_independent_tracking"
            elif "adopt_accelerometer_estimate" in allowed:
                adopt = all(facts[f"adopt_accelerometer_estimate.{label}.limits_met"]
                    for label in ("band_low", "point_estimate", "band_high"))
                action = "adopt_accelerometer_estimate" if adopt else "continue_nominal_assumption"
            else:
                action = "continue_nominal_assumption"
        elif phase == "return":
            action = "request_deep_reasoning" if provider == "typesafe" else "request_scope_amendment"
        else:
            raise ValueError("final_fixed_unknown_phase")
        if action not in allowed:
            raise ValueError("final_fixed_choice_not_available_no_substitution")
        if provider == "typesafe":
            response = {"model": payload["model"], "answers": {"decision": {"type": "choice", "choice": action,
                "probabilities": {key: float(key == action) for key in allowed}, "confidence": 1.}}}
        else:
            evidence = public.get("evidence", {})
            ref = "candidate.next_orbit.return_time_s" if phase == "return" else next(iter(evidence))
            response = {"model": payload["model"], "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"action": action,
                    "summary": "Registered synthetic fixed proposal, not native inference or authority.",
                    "reference_ids": [ref]})}}]}
        receipt.update(mode="synthetic_fixture", call_attempted=False, call_succeeded=False, response_received=False,
            complete_response_observed=False, model_inference_invoked=False,
            model_inference_status="not_invoked_synthetic_fixture")
        result, model = decode(deepcopy(response))
        receipt.update(status="succeeded", model_id=model, synthetic_decoder_succeeded=True)
        return result


def restored_agent_factory(*, active, clock):
    from src.intelligence.starship_restored_agent import RestoredAgent
    return RestoredAgent(transport=FinalFixtureTransport(), active=active, clock=clock)


def visual_scripts():
    return [{"visibility": "adequate", "deployment_visual_state": "separation_visible",
        "telemetry_relation": "insufficient", "recommended_action": "request_numeric_confirmation",
        "observation_choice": "fresh_separation_counter", "frame_support": "supports"},
        {"telemetry_relation": "consistent", "recommended_action": "continue", "observation_choice": "none"}]
