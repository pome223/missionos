"""The diagnostic exposes a real wait choice and grants no flight authority."""

import json

import pytest
import numpy as np

from scripts import ship_aerovla
from scripts.probe_yokohama_pad_models import pad_grammar, proposal_kind, verify_inputs
from tests.contract.test_ship_aerovla_grammar import TokenizerDouble, accepts
from scripts.screen_yokohama_pad_models import reference_eligibility
from scripts.ship_anwm import camera_pose


def test_wait_and_every_bounded_forward_action_are_available_without_occupancy_input():
    grammar = pad_grammar(ship_aerovla, TokenizerDouble())
    for forward in [0, *range(20, 59)]:
        text = f"{forward} 49 49"
        accepts(grammar, text)
        value = ship_aerovla.parse_proposal(text + "</s>")
        assert proposal_kind(value) == ("wait" if forward == 0 else "short_forward_step")
        assert value["dispatch_allowed"] is False
    for text in ("LAND", "0 49 49 LAND", "1 49 49", "59 49 49", "20 48 49", "20 49 50"):
        assert not grammar.valid_prefix(text, complete=True)


@pytest.mark.parametrize("text", ["LAND", "garbage", "99 49 49", "19 49 49", "20 48 49"])
def test_out_of_contract_native_output_is_rejected_without_rewriting(text):
    assert proposal_kind(ship_aerovla.parse_proposal(text)) == "reject"


def test_changed_request_is_rejected_before_loading_models(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"cases": [{"id": "case", "request_sha256": "0" * 64}]})
    )
    (tmp_path / "case").mkdir()
    (tmp_path / "case/request.json").write_text("{}")
    with pytest.raises(ValueError, match="Request changed"):
        verify_inputs(tmp_path)


def test_perfect_reference_with_too_much_unknown_area_is_rejected_before_gpu():
    y, x = np.indices((360, 640))
    rgb = np.repeat((((x // 64 + y // 64) % 2) * 255)[..., None], 3, axis=2).astype(np.uint8)
    depth = np.full((360, 640), 20.0)
    fx = 640 / (2 * np.tan(np.pi / 6))
    pose = camera_pose(dict(vehicle_position_enu_m=[0, 0, 5], vehicle_quaternion_wxyz=[1, 0, 0, 0]))
    arrays = dict(
        rgb=rgb[None],
        depth=depth[None],
        poses=pose[None],
        intrinsics=np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]),
    )
    assert reference_eligibility(arrays, [0, 0, 0, 0])["passed"]
    depth[:220] = 0
    result = reference_eligibility(arrays, [0, 0, 0, 0])
    assert result["luminance_mae"] == 0
    assert result["matched_edge_fraction"] == 1
    assert not result["passed"]
