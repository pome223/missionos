"""Narrow stationary capture scope; leave the original origin-only probe intact."""

from scripts.probe_px4_aerial_camera import CAPTURE

ORIGIN_GUARD = "abs(xyz[0]) < 1 and abs(xyz[1]) < 1 and 2.5 < xyz[2] < 3.5"
CHECKPOINT_GUARD = "abs(xyz[0] - 3) < 0.2 and abs(xyz[1]) < 0.2 and abs(xyz[2] - 3) < 0.2"


def capture_program(stage):
    if stage == "approach":
        return CAPTURE
    if stage != "resume" or CAPTURE.count(ORIGIN_GUARD) != 1:
        raise ValueError("unknown stage or upstream capture guard changed")
    # Only this fixed checkpoint is authorized. No caller-selected coordinate or
    # generic bypass is accepted; timestamps/calibration/raw sensor checks remain.
    return CAPTURE.replace(ORIGIN_GUARD, CHECKPOINT_GUARD).replace(
        "authorized initial hover boundary violated",
        "authorized stationary checkpoint boundary violated",
    )
