"""Possible return only adds a bounded reobservation reason, never entry authority."""

import copy
import json

import pytest

from scripts.yokohama_pad_advisory_host import PadAdvisoryHost
from src.runtime.yokohama_pad_advisory_contract import selected_action, summarize
from src.runtime.yokohama_pad_queue import make_request, propose, require_response
from test_yokohama_pad_advisory import forecast, setup
from test_yokohama_pad_queue import observation


def case():
    config = setup()
    config["world"]["pad_state_advisory"]["reentry_risk_advisory"] = True
    request = make_request(config, 0, [observation(t) for t in range(6)])
    receipt = forecast(config, request, "clear")
    f = receipt["forecast"]
    # Supported positions can coexist with unknown occupancy labels.
    f["detections"] = [dict(xyz_m=[11 - i / 5, 0, 7], supported=True) for i in range(16)]
    for i, entry in enumerate(f["forecasts"]):
        entry.update(state="unknown", xyz_relative_to_pad_m=[8 - i / 4, 0, 7])
    return config, request, receipt


def test_possible_return_reaches_shared_judge_and_executor_check(tmp_path, monkeypatch):
    config, request, receipt = case()
    host = PadAdvisoryHost(tmp_path, config)
    monkeypatch.setattr(host, "forecast", lambda *args: receipt)
    response = host.respond(request, tmp_path, propose(config, request))
    assert summarize(config, request, receipt) == "possible_reentry_reobserve"
    assert require_response(config, request, response, observation(5)) == "wait_at_current_hold"
    assert (
        json.loads((tmp_path / "mission-assurance.json").read_text())["proposal"]["parameters"][
            "action"
        ]
        == "wait_at_current_hold"
    )
    assert not response["approval_granted"] and not response["dispatch_authority_created"]
    assert {f["state"] for f in receipt["forecast"]["forecasts"]} == {"unknown"}


@pytest.mark.parametrize(
    "variant", ["disabled", "shadow", "outgoing", "static", "no_crossing", "unsupported", "expired"]
)
def test_ordinary_unknown_or_expired_forecast_does_not_become_wait_veto(variant):
    config, request, receipt = case()
    p = config["world"]["pad_state_advisory"]
    f = receipt["forecast"]
    if variant == "disabled":
        p.pop("reentry_risk_advisory")
    elif variant == "shadow":
        p["mode"] = "shadow"
    elif variant in {"outgoing", "static"}:
        for i, d in enumerate(f["detections"]):
            d["xyz_m"][0] = 8 + (i / 5 if variant == "outgoing" else 0)
    elif variant == "no_crossing":
        for entry in f["forecasts"]:
            entry["xyz_relative_to_pad_m"] = [9, 0, 7]
    elif variant == "unsupported":
        f["supported"] = False
    else:
        f["input_last_stamp_ns"] -= 2_000_000_000
    assert (
        selected_action(config, request, receipt, "enter_delivery_approach")
        == "enter_delivery_approach"
    )


@pytest.mark.parametrize("fault", ["nan", "missing", "unsupported_detection"])
def test_invalid_localizations_fail_without_dispatch(fault):
    config, request, receipt = case()
    if fault == "nan":
        receipt["forecast"]["forecasts"][1]["xyz_relative_to_pad_m"][0] = float("nan")
    elif fault == "missing":
        receipt["forecast"]["detections"].pop()
    else:
        receipt["forecast"]["detections"][0]["supported"] = False
    with pytest.raises(ValueError):
        selected_action(config, request, receipt, "enter_delivery_approach")


def test_no_forecast_can_upgrade_current_wait_to_entry():
    config, request, receipt = case()
    clear = copy.deepcopy(receipt)
    for d in clear["forecast"]["detections"]:
        d["xyz_m"] = [12, 0, 7]
    for f in clear["forecast"]["forecasts"]:
        f.update(state="clear", xyz_relative_to_pad_m=[12, 0, 7])
    assert selected_action(config, request, clear, "wait_at_current_hold") == "wait_at_current_hold"
