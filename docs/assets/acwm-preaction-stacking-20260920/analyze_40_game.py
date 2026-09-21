#!/usr/bin/env python3
"""Recompute the recorded paired 40-game comparisons (requires NumPy)."""

from pathlib import Path
import json
import numpy as np


ROOT = Path(__file__).resolve().parent
recorded = json.loads((ROOT / "v7-40-game-acwm-comparison.json").read_text())
rows = recorded["rows"]


def paired(other):
    difference = np.asarray([row["scores"]["acwm"] - row["scores"][other] for row in rows], dtype=float)
    rng = np.random.default_rng(67500)
    bootstrap = np.zeros(20_000)
    for group in ["wide", "narrow"]:
        values = difference[[row["width_group"] == group for row in rows]]
        bootstrap += values[rng.integers(0, len(values), (20_000, len(values)))].sum(1)
    bootstrap /= len(difference)
    null = (difference * rng.choice([-1, 1], (100_000, len(difference)))).mean(1)
    return {
        "n": len(rows),
        "mean_difference": float(difference.mean()),
        "total_difference": float(difference.sum()),
        "bootstrap95": np.quantile(bootstrap, [.025, .975]).tolist(),
        "p": float((1 + sum(np.abs(null) >= abs(difference.mean()) - 1e-12)) / 100_001),
        "wins": int(sum(difference > 0)),
        "ties": int(sum(difference == 0)),
        "losses": int(sum(difference < 0)),
    }


computed = {name: paired(name) for name in ["wam", "width_rule", "old_wam", "rule", "vla"]}
last = 0.0
for rank, name in enumerate(sorted(computed, key=lambda key: computed[key]["p"])):
    last = max(last, min(1.0, computed[name]["p"] * (5 - rank)))
    computed[name]["holm_p"] = last
assert computed == recorded["comparisons"]
print(json.dumps(computed, indent=2))
