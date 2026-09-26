import pytest

from scripts.ship_aerovla import parse_proposal


def test_decode_native_bins_and_terminal_request_without_dispatch():
    value = parse_proposal("98 49 49 LAND</s>")
    assert value["forward_m"] == 5
    assert value["down_m"] == 0
    assert value["yaw_delta_rad"] == 0
    assert value["stop_proposed"] is True
    assert value["dispatch_allowed"] is False


@pytest.mark.parametrize(
    "text", ["", "move forward", "99 49 49", "-1 49 49", "98 49 49 20", "1e2 49 49"]
)
def test_malformed_action_never_becomes_zero_or_land(text):
    value = parse_proposal(text)
    assert value["kind"] == "rejected"
    assert value["dispatch_allowed"] is False


def test_large_yaw_keeps_upstream_controller_semantics_visible():
    value = parse_proposal("98 49 98")
    assert value["yaw_delta_rad"] == 1.1
    assert value["upstream_controller_translates_horizontal"] is False
    assert value["nominal_action_duration_s"] is None
