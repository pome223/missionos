import json

import pytest

from src.runtime.ship_onboard_model import interpret_proposal


def test_model_judgment_and_rule_forecast_selection_remain_distinct():
    proposal = {"action": "wait", "predicted_clearance_seconds": 90, "reason": "uncertain"}
    action = interpret_proposal(proposal, "onboard_vlm_action", elapsed_s=2)
    forecast = interpret_proposal(proposal, "onboard_vlm_forecast", elapsed_s=2)
    assert action["action"] == "wait"
    assert forecast["action"] == "detour"
    assert not forecast["vla_invoked"]
    assert not forecast["wam_invoked"]
    assert forecast["vision_language_model_invoked"]


@pytest.mark.parametrize(
    "fault", ["timeout", "nan", "negative", "bool", "out_of_range", "unknown", "extra", "missing"]
)
def test_invalid_model_output_cannot_select_an_approved_route(fault):
    proposal = {"action": "wait", "predicted_clearance_seconds": 5, "reason": "moving"}
    elapsed = 1
    if fault == "timeout":
        elapsed = 20.01
    elif fault == "nan":
        proposal["predicted_clearance_seconds"] = float("nan")
    elif fault == "negative":
        proposal["predicted_clearance_seconds"] = -1
    elif fault == "bool":
        proposal["predicted_clearance_seconds"] = True
    elif fault == "out_of_range":
        proposal["predicted_clearance_seconds"] = 121
    elif fault == "unknown":
        proposal["action"] = "fly_through"
    elif fault == "extra":
        proposal["approval"] = True
    else:
        del proposal["action"]
    with pytest.raises(ValueError):
        interpret_proposal(proposal, "onboard_vlm_forecast", elapsed_s=elapsed)


def test_model_input_binding_is_checked_without_another_inference(tmp_path, monkeypatch):
    from src.runtime import ship_onboard_model as model
    from test_ship_onboard import frame

    row = frame(tmp_path)
    history = [{"observed_at_s": 10, "obstacle_x_m": 5}]
    monkeypatch.setattr(model, "verify_local_model", lambda: None)
    response = {
        "model": model.MODEL,
        "done": True,
        "done_reason": "stop",
        "message": {
            "content": json.dumps(
                {"action": "wait", "predicted_clearance_seconds": 4, "reason": "moving"}
            )
        },
    }
    monkeypatch.setattr(model, "_json_call", lambda *a, **kw: response)
    decision = model.propose(tmp_path, [row], history, "onboard_vlm_action")
    assert model.verify_model_artifacts(tmp_path, [row], history, "onboard_vlm_action", decision)[
        "verified"
    ]
    request = tmp_path / "model-request.json"
    request.write_text(request.read_text().replace("parcel drone", "tampered drone"))
    with pytest.raises(ValueError, match="binding mismatch"):
        model.verify_model_artifacts(tmp_path, [row], history, "onboard_vlm_action", decision)
