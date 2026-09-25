"""Render the report figure solely from the curated public JSON files."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parent
summary = json.loads((ROOT / "summary.json").read_text())
points = json.loads((ROOT / "trajectory.json").read_text())["points"]
fig = plt.figure(figsize=(13, 8), facecolor="#f5f7fb")
grid = fig.add_gridspec(
    2, 3, width_ratios=[1.2, 1, 1], height_ratios=[1, 1], hspace=0.48, wspace=0.38
)
ax = fig.add_subplot(grid[:, 0])
ax.set_facecolor("white")
ax.plot(
    [p["position_ned_m"][1] for p in points],
    [p["position_ned_m"][0] for p in points],
    color="#176aa3",
    lw=2,
    label="Observed trajectory",
)
goal = summary["goal_camera_position_ned_m"]
target = summary["flight"]["commanded_target_ned_m"]
ax.scatter(goal[1], goal[0], marker="*", s=170, c="#257b48", label="Goal reference (left)")
ax.scatter(target[1], target[0], marker="x", s=95, c="#c34235", label="Selected target (right)")
ax.scatter(
    points[0]["position_ned_m"][1], points[0]["position_ned_m"][0], c="#222", s=30, label="Start"
)
for b in summary["geometry"]["declared_static_collision_boxes_enu"]:
    x, y, _ = b["center_xyz_m"]
    sx, sy, _ = b["size_xyz_m"]
    ax.add_patch(Rectangle((x - sx / 2, y - sy / 2), sx, sy, color="#777", alpha=0.25))
ax.set(
    xlim=(-1.3, 4.5),
    ylim=(-6, 6),
    xlabel="East / world X (m)",
    ylabel="North / world Y (m)",
    title="Observed simulator trajectory",
)
ax.set_aspect("equal", adjustable="box")
ax.grid(alpha=0.2)
ax.legend(loc="upper right", fontsize=7)
ax = fig.add_subplot(grid[0, 1:])
ax.set_facecolor("white")
ax.plot(
    [p["elapsed_simulation_s"] for p in points],
    [-p["position_ned_m"][2] for p in points],
    color="#176aa3",
    lw=2,
)
for name in ("dispatch", "arrival", "landing", "disarm"):
    c = summary["flight"]["checkpoints"][name]
    ax.axvline(c["elapsed_simulation_s"], color="#999", lw=0.7, ls="--")
    ax.text(c["elapsed_simulation_s"], 0.25, name, rotation=90, va="bottom", fontsize=8)
ax.set(
    xlabel="Elapsed simulation time (s)",
    ylabel="Height (m)",
    title="Takeoff, hold, selected motion, landing and disarm",
    ylim=(-0.15, 3.5),
)
ax.grid(alpha=0.2)
ax = fig.add_subplot(grid[1, 1])
candidates = summary["candidates"]
labels = ["Left", "Right (selected)"]
costs = [c["goal_image_mse"] for c in candidates]
ax.bar(labels, costs, color=["#257b48", "#c34235"], width=0.6)
for i, cost in enumerate(costs):
    ax.text(i, cost + 0.0006, f"{cost:.6f}", ha="center", fontsize=10)
ax.set(
    ylabel="Goal-image MSE (lower is better)",
    ylim=(0, 0.044),
    title="ANWM score chose the wrong direction",
)
ax.text(
    0.5,
    -0.24,
    "Planned camera distance to goal:\nleft 0.042 m; right 10.000 m",
    ha="center",
    transform=ax.transAxes,
    fontsize=9,
)
ax = fig.add_subplot(grid[1, 2])
ax.axis("off")
f = summary["flight"]
o = summary["observation"]
text = f"Actual ANWM calls: 2\nForecasting: {summary['model']['forecast_seconds']:.2f} s\nJev API: {summary['jev']['latency_ms'] / 1000:.3f} s\nImage age at dispatch: {o['original_image_age_at_dispatch_s']:.2f} s\n\nMeasured displacement: {f['measured_displacement_m']:.3f} m\nEndpoint error: {f['target_error_m']:.3f} m\nFlight duration: {f['candidate_duration_simulation_s']:.3f} sim s\nModel horizon: nominal 1 s\n\nLanding and disarm: observed\nHardware execution: none"
ax.text(0, 1, text, va="top", fontsize=11, linespacing=1.6)
fig.suptitle(
    "ANWM + Jev + PX4 SITL: integration succeeded, goal selection failed",
    fontsize=17,
    fontweight="bold",
    y=0.985,
)
fig.text(
    0.055,
    0.02,
    "One static-scene trial. Curated telemetry plot, not video. Image similarity is not collision risk or navigation accuracy.",
    fontsize=10,
    color="#a2362b",
)
fig.subplots_adjust(left=0.065, right=0.975, top=0.855, bottom=0.115)
fig.savefig(ROOT / "flight-verification.png", dpi=160, facecolor=fig.get_facecolor())
print(ROOT / "flight-verification.png")
