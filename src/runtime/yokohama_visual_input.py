"""Image-only raw-action comparison before Rules, with no dispatch authority.

The only enabled adapter is a declared CPU color double. Real VLA input files
are prepared for a later reviewed experiment; no live model client is supplied.
"""

from dataclasses import dataclass, field
import hashlib
import io
import math
import re
import time

import numpy as np
from PIL import Image

PROMPT = "<image>\nFly straight ahead toward the approved nearby goal. Maintain height. If the visible passage is blocked, propose hold.\nAction: "
CONTRACT = {
    "schema": "yokohama.visual-input-hold.v1",
    "maximum_observations": 3,
    "maximum_reobservations": 2,
    "maximum_wait_s": 20,
    "maximum_image_age_s": 2,
    "hold_action": "0 49 49",
    "native_enabled": False,
    "dispatch_authority": False,
}


def checked_images(payload):
    # No case ID, ground-truth obstacle, schedule, future image or route scalar
    # is allowed across the actual inference adapter's interface.
    if (
        set(payload) != {"prompt", "images"}
        or payload["prompt"] != PROMPT
        or set(payload["images"]) != {"rgb", "down"}
    ):
        raise ValueError("Unexpected model input; possible truth/label leakage")
    arrays = {}
    for key, raw in payload["images"].items():
        if not isinstance(raw, bytes) or len(raw) > 2 * 1024**2:
            raise ValueError("Unexpected image payload")
        with Image.open(io.BytesIO(raw)) as im:
            if im.mode != "RGB" or im.size != (640, 360):
                raise ValueError("Unexpected image shape/mode")
            arrays[key] = np.asarray(im).copy()
    return arrays


def cpu_color_double(payload):
    arrays = checked_images(payload)
    red, green, blue = arrays["rgb"].astype(float).transpose(2, 0, 1)
    mask = (red > 80) & (red > green * 1.6) & (red > blue * 1.6)
    # Fixed synthetic red barrier; this is not learned/general perception.
    blocked = int(mask[90:270, 160:480].sum()) > 1500
    return dict(
        generated_text="0 49 49" if blocked else "43 49 49",
        fixture=True,
        vla_inference_invoked=False,
        dispatch_invoked=False,
        backend="CPU_color_double_not_AeroVLA",
    )


def raw_action(response):
    if (
        set(response)
        != {"generated_text", "fixture", "vla_inference_invoked", "dispatch_invoked", "backend"}
        or response["fixture"] is not True
        or response["vla_inference_invoked"] is not False
        or response["dispatch_invoked"] is not False
        or response["backend"] != "CPU_color_double_not_AeroVLA"
    ):
        raise ValueError("Only the declared non-dispatch CPU adapter is admitted")
    text = response["generated_text"]
    match = re.fullmatch(r"(0|[1-9][0-9]?) (47|48|49|50|51) (45|46|47|48|49|50|51|52|53)", text)
    if not match:
        raise ValueError("Unsupported raw visual action; LAND/rotation are excluded")
    forward, down, yaw = map(int, match.groups())
    if forward == 0:
        if (down, yaw) != (49, 49):
            raise ValueError("Wait must retain pose and heading")
        return {"kind": "hold", "raw_text": text, "bins": [forward, down, yaw]}
    if not 20 <= forward <= 58:
        raise ValueError("Unbounded visual translation")
    return {"kind": "translation", "raw_text": text, "bins": [forward, down, yaw]}


@dataclass
class FiniteObservation:
    started_s: float
    observations: int = 0
    last_stamp_s: float | None = None
    closed: bool = False
    clock: object = time.monotonic
    started_wall_s: float = field(init=False)

    def __post_init__(self):
        self.started_wall_s = self.clock()

    def observe(self, stamp_s, now_s, payload, adapter=cpu_color_double):
        if (
            self.closed
            or not all(
                type(v) in (int, float) and math.isfinite(v)
                for v in (self.started_s, stamp_s, now_s)
            )
            or not 0 <= now_s - self.started_s <= CONTRACT["maximum_wait_s"]
            or not 0 <= now_s - stamp_s <= CONTRACT["maximum_image_age_s"]
            or (self.last_stamp_s is not None and stamp_s <= self.last_stamp_s)
            or self.observations >= CONTRACT["maximum_observations"]
        ):
            self.closed = True
            raise ValueError("Closed, stale, regressed or expired visual observation")
        checked_images(payload)
        self.last_stamp_s = stamp_s
        self.observations += 1
        before = self.clock()
        try:
            response = adapter(payload)
            after = self.clock()
            if (
                after < before
                or after - before > CONTRACT["maximum_image_age_s"]
                or after - self.started_wall_s > CONTRACT["maximum_wait_s"]
            ):
                raise ValueError("Visual response arrived after its finite lease")
            action = raw_action(response)
        except Exception:
            self.closed = True
            raise
        terminal = action["kind"] != "hold" or self.observations == CONTRACT["maximum_observations"]
        self.closed = terminal
        return dict(
            raw_response=response,
            raw_candidate_before_Rules=action,
            input_images_sha256={
                k: hashlib.sha256(v).hexdigest() for k, v in payload["images"].items()
            },
            observation=self.observations,
            terminal=terminal,
            outcome="stop_without_authority"
            if terminal and action["kind"] == "hold"
            else "proposal_only",
            reobservation_allowed=not terminal,
            rules_evaluated=False,
            dispatch_authorized=False,
            physical_execution_invoked=False,
        )
