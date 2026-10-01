#!/usr/bin/env python3
"""CPU eligibility screen: can even a perfect view satisfy the frozen gate?"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.probe_yokohama_pad_models import verify_inputs, write  # noqa: E402
from scripts.ship_anwm import action_pose  # noqa: E402
from src.runtime.yokohama_native import forecast_consistency, past_view  # noqa: E402


def reference_eligibility(arrays, delta):
    target = action_pose(arrays["poses"][-1], delta)
    reference, mask = past_view(arrays, target)
    return forecast_consistency(reference, reference, mask)


def screen(root):
    rows = []
    for folder, request in verify_inputs(root):
        with np.load(folder / "history.npz", allow_pickle=False) as saved:
            arrays = {key: saved[key] for key in saved.files}
        candidates = []
        for forward in (0, 20 * 5 / 98, 58 * 5 / 98):
            delta = [forward, 0, 0, 0]
            candidates.append(dict(delta=delta, self_check=reference_eligibility(arrays, delta)))
        rows.append(dict(case=folder.name, candidates=candidates))
    return dict(
        schema="yokohama_pad_model_input_screen.v1",
        native_inference_invoked=False,
        gpu_requested=False,
        flight_qualification_admitted=all(
            c["self_check"]["passed"] for r in rows for c in r["candidates"]
        ),
        interpretation="Passing only admits further measurement; failing is input/gate incompatibility, not model error",
        candidates="Hold plus endpoint translations of the fixed forward range; not every intermediate pose",
        rows=rows,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = screen(a.inputs)
    write(a.output, result)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))
    raise SystemExit(0 if result["flight_qualification_admitted"] else 2)
