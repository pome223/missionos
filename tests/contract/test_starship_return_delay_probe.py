"""Admission guards and outcome accounting for the opt-in timing probe."""
from copy import deepcopy

import pytest

from scripts.probe_starship_return_delay import contact_passed, main


def test_probe_requires_opt_in_and_fresh_output(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--delay-s", "0", "--output-dir", str(tmp_path/"new")])
    assert exc.value.code == 2
    assert not (tmp_path/"new").exists()
    with pytest.raises(SystemExit) as exc:
        main(["--approve-simulation", "--delay-s", "60", "--output-dir", str(tmp_path)])
    assert exc.value.code == 2


@pytest.mark.parametrize("field,value", [("speed_mps", 5.01), ("tilt_deg", 5.01),
    ("body_rate_rad_s", .0201), ("reserve_kg", 27999.), ("speed_mps", float("nan"))])
def test_contact_limits_cannot_be_replaced_by_record_validity(field, value):
    metrics = {"speed_mps": 4., "tilt_deg": 2., "body_rate_rad_s": .01, "reserve_kg": 30000.}
    assert contact_passed(metrics)
    changed = deepcopy(metrics)
    changed[field] = value
    assert not contact_passed(changed)
    assert not contact_passed(None)
