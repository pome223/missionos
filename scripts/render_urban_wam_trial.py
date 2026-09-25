#!/usr/bin/env python3
"""Render observed urban routes and camera frames from locally verified trials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_urban_wam_trial import verify  # noqa: E402


def render(roots, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from PIL import Image
    import numpy as np

    fig, axes = plt.subplots(
        len(roots),
        2,
        figsize=(12, 4.2 * len(roots)),
        squeeze=False,
        gridspec_kw={"width_ratios": [0.95, 1.25]},
    )
    fig.patch.set_facecolor("#f4f6f9")
    for row, root in enumerate(roots):
        evidence = verify(root)
        session = root / "session"
        scene = json.loads((session / "scene.json").read_text())
        events = [
            json.loads(line)
            for line in (session / "events.jsonl").read_text().splitlines()
        ]
        dispatch = next(e for e in events if e["event"] == "urban_route_dispatch")
        goal = next(e for e in events if e["event"] == "urban_goal_observed")
        start_ns, end_ns = (
            dispatch["observed_start"]["pose_simulation_time_ns"],
            goal["observed"]["pose_simulation_time_ns"],
        )
        records = [
            json.loads(line)
            for line in (session / "telemetry.jsonl").read_text().splitlines()
        ]
        points = np.array(
            [
                r["gazebo_pose_enu_m"]
                for r in records
                if r["pose_simulation_time_ns"] is not None
                and start_ns <= r["pose_simulation_time_ns"] <= end_ns
            ]
        )
        ax, camera = axes[row]
        for building in scene["buildings"]:
            lo, hi = building["lower_enu_m"], building["upper_enu_m"]
            ax.add_patch(
                Rectangle(
                    lo[:2],
                    hi[0] - lo[0],
                    hi[1] - lo[1],
                    facecolor="#8998a9",
                    alpha=0.6,
                    edgecolor="#536477",
                )
            )
            if lo[0] < 19:
                ax.text(
                    (lo[0] + hi[0]) / 2,
                    (lo[1] + hi[1]) / 2,
                    f"{hi[2]:.1f} m",
                    ha="center",
                    va="center",
                    fontsize=9,
                )
        ax.plot(points[:, 0], points[:, 1], color="#d4dbe3", lw=3)
        path = ax.scatter(
            points[:, 0],
            points[:, 1],
            c=points[:, 2],
            vmin=2.8,
            vmax=5.6,
            s=8,
            cmap="viridis",
            zorder=3,
        )
        ax.scatter(
            [0, scene["goal_enu_m"][0]],
            [0, scene["goal_enu_m"][1]],
            c=["#1d67ad", "#bd344d"],
            s=80,
            marker="x",
            lw=3,
            zorder=4,
        )
        ax.set(xlim=(-1, 18), ylim=(-12, 12), xlabel="East (m)", ylabel="North (m)")
        ax.set_aspect("equal")
        ax.grid(alpha=0.15)
        mode = (
            "ANWM ranking + geometry constraints"
            if evidence["model_invoked"]
            else "Declared geometric route"
        )
        if (
            evidence["model_invoked"]
            and not evidence["model_selection"]["multiple_admissible_routes"]
        ):
            mode = "Only route admitted by geometry"
        ax.set_title(
            f"{scene['family'].upper()} | {evidence['route_id']}\n{mode}",
            loc="left",
            fontsize=12,
            fontweight="bold",
        )
        plt.colorbar(
            path, ax=ax, fraction=0.045, pad=0.025, label="Observed altitude (m)"
        )
        frames = json.loads((session / "route-images/frames.json").read_text())[
            "frames"
        ]
        distances = np.linalg.norm(np.diff(points, axis=0), axis=1)
        cumulative = np.concatenate(([0.0], np.cumsum(distances)))
        midpoint = points[np.argmin(np.abs(cumulative - cumulative[-1] / 2))]
        frame = min(
            (f for f in frames if start_ns <= f["simulation_time_ns"] <= end_ns),
            key=lambda f: sum((a - b) ** 2 for a, b in zip(f["pose_enu_m"], midpoint)),
        )
        camera.imshow(Image.open(session / "route-images" / frame["file"]))
        camera.axis("off")
        camera.set_title(
            f"Actual Gazebo RGB | altitude {frame['pose_enu_m'][2]:.2f} m",
            loc="left",
            fontsize=12,
            fontweight="bold",
        )
        camera.text(
            0,
            -0.07,
            f"Arrived + landed + disarmed | path {evidence['observed_distance_m']:.2f} m\n"
            f"Min enclosing-sphere clearance {evidence['minimum_observed_envelope_clearance_m']:.2f} m | "
            f"route {evidence['route_simulation_seconds']:.2f} sim s",
            transform=camera.transAxes,
            fontsize=11,
            va="top",
        )
    all_wam = all(verify(root)["model_invoked"] for root in roots)
    fig.suptitle(
        "PX4 urban routes | "
        + (
            "ANWM ranking + geometry constraints"
            if all_wam
            else "observed geometric feasibility flights"
        ),
        fontsize=19,
        fontweight="bold",
        x=0.055,
        ha="left",
        y=0.98,
    )
    fig.text(
        0.055,
        0.018,
        "Textured simulation meshes, not real-world buildings. Geometry constrains flight; learned navigation benefit is unproven.\n"
        "OSRF gazebo_models / Apartment (CC BY 3.0; Nathan Koenig, Cole Biesemeyer). Camera images are observed, not generated.",
        fontsize=9,
        color="#465264",
    )
    fig.tight_layout(rect=(0.025, 0.06, 0.98, 0.95), h_pad=3)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.root, args.output)


if __name__ == "__main__":
    main()
