"""Pinned local NWM input convention and provenance; no downloads or training."""

import hashlib
import math

UPSTREAM_REVISION = "3f6cd8e70d6f2d1e2b9684acff510710135f0f41"


def encode_action(dx_m, dy_m, yaw_rad, *, spacing_m=0.255):
    if not all(math.isfinite(x) for x in [dx_m, dy_m, yaw_rad, spacing_m]) or spacing_m <= 0:
        raise ValueError("finite action and positive spacing required")
    # datasets._compute_actions divides xy by dataset metric waypoint spacing;
    # TrainingDataset then normalizes xy using data_config action_stats.
    x = 2 * (dx_m / spacing_m + 2.5) / 7.5 - 1
    y = 2 * (dy_m / spacing_m + 4) / 8 - 1
    if abs(x) > 1 or abs(y) > 1 or abs(yaw_rad) > math.pi:
        raise ValueError("action outside diagnostic normalization bounds")
    return [x, y, yaw_rad]


CHECKPOINT_SHA256 = "8ed9478d40e49809a1bbe6aec4df8af7f6f48c3bb427307dd8a423efb347ce6e"
XL_SHA256 = "02c145cbba5f3381278093d0a20fc97eef9d0e20c12b33ed896e9862606f9e90"
VAE_SHA256 = "32db726da04f06c1b6b14c0043ce115cc87a501482945c5add89a40d838fcb46"
VAE_CONFIG_SHA256 = "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
