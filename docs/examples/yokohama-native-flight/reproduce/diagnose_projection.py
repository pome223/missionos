"""Compare recorded service projections to past RGBD; never invoke a model."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--repo", type=Path, required=True)
p.add_argument("--wam-record", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
sys.path.insert(0, str(a.repo))
from scripts import ship_anwm  # noqa: E402
from src.runtime.yokohama_native import past_view, forecast_consistency  # noqa: E402

request, arrays = ship_anwm.validate(a.wam_record / "input/request.json")
response = json.loads((a.wam_record / "native-response.json").read_text())
assert response["request_sha256"] == ship_anwm.digest(a.wam_record / "input/request.json")
assert response["history_sha256"] == request["history_sha256"]
a.output.mkdir(parents=True, exist_ok=True)
(a.output / "images").mkdir(exist_ok=True)
checks = []
for forecast in response["forecasts"]:
    candidate = forecast["candidate"]
    reference, mask = past_view(
        arrays, ship_anwm.action_pose(arrays["poses"][-1], candidate["delta"])
    )
    entry = forecast["files"]["projection"]
    data = base64.b64decode(entry["png_base64"], validate=True)
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    image = a.output / "images" / ("cycle-1-" + candidate["id"] + "-projection.png")
    image.write_bytes(data)
    checks.append(
        dict(
            candidate=candidate,
            **forecast_consistency(np.asarray(Image.open(image)), reference, mask),
        )
    )
result = {
    "diagnostic_only": True,
    "in_runtime": False,
    "scope": "Recorded service projection against independent past RGBD reprojection; no new inference, no future observation",
    "checks": checks,
    "native_prediction_rejection_unchanged": True,
    "learned_model_root_cause_proven": False,
}
(a.output / "projection-diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
