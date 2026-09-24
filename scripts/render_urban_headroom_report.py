#!/usr/bin/env python3
"""Render observed CPU-baseline paths; no actuator or model access."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.urban_headroom_contract import case_scene  # noqa: E402


def render(summary, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import Rectangle
    import numpy as np

    data = json.loads(summary.read_text())
    rows = max(1, (len(data["runs"]) + 3) // 4)
    fig, axes = plt.subplots(
        rows, 4, figsize=(14, 4 * rows + 1), layout="constrained", squeeze=False
    )
    fig.patch.set_facecolor("#f3f5f8")
    fig.suptitle(
        f"Missing-map navigation | {len(data['runs'])} completed flights / 12 planned cases\nStopped before case 4 flight; gate incomplete; no WAM or rented GPU",
        fontsize=19,
        fontweight="bold",
    )
    for ax, r in zip(axes.flat, data["runs"]):
        scene = case_scene(r["family"])
        path = np.asarray(r["trajectory"])
        for b in scene["buildings"]:
            lo, hi = b["lower_enu_m"], b["upper_enu_m"]
            omitted = b["name"] in scene["omitted_map_entities"]
            ax.add_patch(
                Rectangle(
                    lo[:2],
                    hi[0] - lo[0],
                    hi[1] - lo[1],
                    facecolor="#b9c2cd",
                    edgecolor="#64748b",
                    alpha=0.65,
                    hatch="//" if omitted else None,
                )
            )
        xy = path[:, 1:3]
        lines = LineCollection(
            np.stack((xy[:-1], xy[1:]), axis=1),
            cmap="viridis",
            norm=plt.Normalize(3, 5.6),
            linewidths=3,
        )
        lines.set_array(path[:-1, 3])
        ax.add_collection(lines)
        ax.scatter(
            [0, scene["goal_enu_m"][0]],
            [0, 0],
            c=["#1768b0", "#bd3757"],
            marker="x",
            s=65,
            zorder=5,
        )
        ax.set(xlim=(-2, 18), ylim=(-12, 12), xlabel="East (m)", ylabel="North (m)")
        ax.set_aspect("equal")
        ax.grid(alpha=0.15)
        title = r["family"].removeprefix("headroom_").upper() + " | " + r["route_id"]
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.text(
            0.03,
            0.02,
            f"Arrived / landed / disarmed\nPath {r['observed_distance_m']:.2f} m | max z {r['maximum_altitude_m']:.2f} m\nSphere clearance {r['minimum_observed_envelope_clearance_m']:.2f} m",
            transform=ax.transAxes,
            fontsize=8,
            bbox=dict(facecolor="white", alpha=0.9, edgecolor="none"),
        )
    for ax in list(axes.flat)[len(data["runs"]) :]:
        ax.axis("off")
    if data["failures"] and len(data["runs"]) < axes.size:
        ax = list(axes.flat)[len(data["runs"])]
        ax.text(
            0.05,
            0.8,
            "GAP_3 | NOT FLOWN",
            transform=ax.transAxes,
            fontsize=14,
            fontweight="bold",
            color="#9b4520",
        )
        ax.text(
            0.05,
            0.7,
            "Contact probe exited 134\nbefore flight control started.\n\n3 completed flights\n1 preflight process failure\n8 cases not attempted\n\nHeadroom remains unknown.\nGPU gate did not pass.",
            transform=ax.transAxes,
            fontsize=12,
            va="top",
            linespacing=1.5,
        )
    fig.colorbar(lines, ax=axes, shrink=0.5, label="Observed altitude (m)")
    fig.supxlabel(
        "Hatched buildings are absent from the planner map. Truth geometry is shown for post-flight audit only.\nActual simulation trajectories; no learned-navigation or general success-rate claim.",
        fontsize=11,
    )
    fig.savefig(output, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    render(args.summary, args.output)
