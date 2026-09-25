"""Render recorded trajectories; never generates or executes a flight."""

import json
from pathlib import Path
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from scripts.urban_headroom_contract import case_scene  # noqa: E402

ROOT = Path(__file__).parent
COLORS = ["#2368a2", "#168579", "#d48721", "#925bb0"]


def render():
    data = json.loads((ROOT / "summary.json").read_text())
    runs = {r["family"]: r for r in data["runs"]}
    fig, axes = plt.subplots(3, 2, figsize=(13.5, 10), layout="constrained")
    for row, family in enumerate(("gap", "climb", "detour")):
        top, altitude = axes[row]
        for index in range(4):
            name = f"headroom_{family}_{index}"
            scene = case_scene(name)
            for b in scene["buildings"]:
                lo, hi = b["lower_enu_m"], b["upper_enu_m"]
                top.add_patch(
                    Rectangle(
                        (lo[0], lo[1]),
                        hi[0] - lo[0],
                        hi[1] - lo[1],
                        facecolor="#8e99a6",
                        edgecolor="#67717e",
                        alpha=0.10,
                    )
                )
            if name not in runs:
                continue
            trace = runs[name]["trajectory"]
            label = f"variant {index}: {runs[name]['observed_distance_m']:.2f} m"
            top.plot(
                [p[1] for p in trace],
                [p[2] for p in trace],
                color=COLORS[index],
                linewidth=1.9,
                label=label,
            )
            altitude.plot(
                [p[0] for p in trace],
                [p[3] for p in trace],
                color=COLORS[index],
                linewidth=1.9,
                label=f"variant {index}",
            )
        scene = case_scene(f"headroom_{family}_0")
        goal = scene["goal_enu_m"]
        top.scatter([0], [0], marker="o", color="#172438", s=28, zorder=6)
        top.scatter([goal[0]], [goal[1]], marker="*", color="#172438", s=130, zorder=6)
        top.set(
            xlim=(-1.5, 18),
            ylim=(-9, 9),
            xlabel="East (m)",
            ylabel="North (m)",
            title=f"{family.title()} | observed horizontal path",
        )
        top.set_aspect("equal", adjustable="box")
        altitude.set(
            xlim=(0, 90),
            ylim=(0, 7),
            xlabel="Route time (simulation s)",
            ylabel="Altitude (m)",
            title=f"{family.title()} | observed altitude",
        )
        for ax in (top, altitude):
            ax.grid(alpha=0.18)
            if ax.lines:
                ax.legend(loc="best", fontsize=8)
            else:
                ax.text(
                    0.5,
                    0.5,
                    "No verified route",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                )
    gate = data["gate"]
    fig.suptitle(
        f"CPU depth baseline, R2 — {gate['verified_baseline_arrivals']}/12 verified arrivals\n{gate['decision']} | no WAM/model/GPU calls",
        fontsize=15,
        fontweight="bold",
    )
    fig.supxlabel(
        "Grey: declared building bounds for verification (all four offsets). Lines: recorded motion, not predictions. Circles/stars: nominal start/goal.\nFresh simulator per case; static development scenes; no exact physics-state cloning.",
        fontsize=10,
    )
    fig.savefig(ROOT / "observed-paths.png", dpi=150, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    render()
