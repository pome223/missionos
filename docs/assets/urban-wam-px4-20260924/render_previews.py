"""Render curated forecast previews from local trial roots, then an output PNG.

Usage: python render_previews.py <trial-root> [<trial-root> ...] <output.png>
Uses only explicitly supplied local inputs; no model or vehicle commands.
"""

import json
import sys
from pathlib import Path
import numpy as np
from PIL import Image
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

roots = [Path(p) for p in sys.argv[1:-1]]
out = Path(sys.argv[-1])
fig, axes = plt.subplots(
    len(roots), 5, figsize=(16, 4.6 * len(roots) + 0.6), squeeze=False
)
fig.patch.set_facecolor("#f4f6f9")


def crop(arr):
    im = Image.fromarray(arr)
    w, h = im.size
    cw = int(h * 4 / 3)
    left = round((w - cw) / 2)
    return im.crop((left, 0, left + cw, h)).resize(
        (224, 224), Image.Resampling.BILINEAR
    )


for row, r in enumerate(roots):
    result = json.loads((r / "forecast/result.json").read_text())
    decision = json.loads((r / "session/urban-selection.json").read_text())
    scene = json.loads((r / "session/scene.json").read_text())
    with np.load(r / "input/assets.npz", allow_pickle=False) as arrays:
        panels = [
            (
                "Observed history",
                crop(arrays["context_rgb"][-1]),
                "16 real frames, 4 Hz sim",
            ),
            ("Declared goal", crop(arrays["goal_rgb"]), "Scoring reference only"),
        ]
    for c in result["candidates"]:
        accepted = decision["route_checks"][c["candidate_id"]]["admissible"]
        selected = c["candidate_id"] == decision["route_id"]
        note = (
            "SELECTED"
            if selected
            else "admissible" if accepted else "rejected by geometry"
        )
        if selected and not decision["multiple_admissible_routes"]:
            note = "sole geometry-admitted route"
        panels.append(
            (
                c["candidate_id"],
                Image.open(r / "forecast" / c["predicted_image"]),
                f"Goal MSE {c['goal_mse']:.4f} | {note}",
            )
        )
    for col, ax in enumerate(axes[row]):
        if col >= len(panels):
            ax.axis("off")
            continue
        title, im, note = panels[col]
        ax.imshow(im)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(
            (scene["family"].upper() + " | " if col == 0 else "") + title,
            loc="left",
            fontsize=11,
            fontweight="bold",
        )
        ax.text(0, -0.045, note, transform=ax.transAxes, fontsize=9, va="top")
        if title == decision["route_id"]:
            for spine in ax.spines.values():
                spine.set_color("#198268")
                spine.set_linewidth(3)
fig.suptitle(
    "ANWM urban route previews | generated images used for initial route ranking",
    x=0.035,
    ha="left",
    fontsize=18,
    fontweight="bold",
)
fig.text(
    0.035,
    0.014,
    "Five-metre path prefixes; nominal 8 s conditioning, not verified future flight timing. RGB MSE is not collision risk.\n"
    "Goal reference is not used for image generation. Geometry constrains execution; navigation improvement is unproven.",
    fontsize=10,
    color="#465264",
)
fig.tight_layout(
    rect=(
        0.02,
        0.15 if len(roots) == 1 else 0.075,
        0.99,
        0.90 if len(roots) == 1 else 0.94,
    ),
    h_pad=3,
)
fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
