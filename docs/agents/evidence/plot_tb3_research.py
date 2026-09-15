"""Rebuild research figures from reviewed public numbers; never invokes a simulator.

Requires matplotlib. Run: python docs/agents/evidence/plot_tb3_research.py
"""

from pathlib import Path
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "tb3-research-analysis-20260915.json").read_text())
ROWS = DATA["trials"]
OUT = ROOT / "tb3-research-figures"
OUT.mkdir(exist_ok=True)
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.hashsalt": "tb3-research-20260915",
        "svg.fonttype": "none",
    }
)
BLUE, GREEN, ORANGE, RED = "#2463a6", "#18765a", "#cc7925", "#aa3535"


def save(fig, name):
    fig.savefig(OUT / (name + ".png"), dpi=180, bbox_inches="tight")
    fig.savefig(OUT / (name + ".svg"), bbox_inches="tight", metadata={"Date": None})
    svg = OUT / (name + ".svg")
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
    plt.close(fig)


fig, ax = plt.subplots(figsize=(11.8, 5.0))
for i, row in enumerate(ROWS):
    goals = row["goals"]
    start = row["timing"]["decision_observation_sim_s"]
    end = row["seconds"]
    if goals:
        before = goals[0]["start_sim_s"] - start
        ax.barh(i, before, color="#9caabd", label="Before first goal dispatch" if i == 0 else None)
        for k, goal in enumerate(goals):
            left = goal["start_sim_s"] - start
            duration = goal["end_sim_s"] - goal["start_sim_s"]
            ax.barh(
                i,
                duration,
                left=left,
                color=BLUE if k == 0 else GREEN,
                label="Goal 1 execution"
                if i == 0 and k == 0
                else "Goal 2 execution"
                if i == 2 and k == 1
                else None,
            )
        ax.text(end + 0.35, i, f"{end:.3f}  arrived", va="center", color=GREEN)
    else:
        ax.barh(
            i,
            end,
            color="#eed6d6",
            hatch="//",
            edgecolor=RED,
            label="Stopped; zero goals" if i == 1 else None,
        )
        ax.scatter(end, i, marker="x", color=RED, zorder=4)
        ax.text(end + 0.5, i, f"{end:.3f}  stopped", va="center", color=RED)
ax.set_yticks(range(len(ROWS)), [r["name"] for r in ROWS])
ax.invert_yaxis()
ax.set_xlim(0, 45)
ax.set_xlabel("Simulator seconds from the first decision observation (startup excluded)")
ax.set_title(
    "Public frozen cohort: terminal outcomes and execution intervals",
    loc="left",
    weight="bold",
    pad=16,
)
ax.grid(axis="x", alpha=0.2)
ax.set_axisbelow(True)
ax.legend(loc="upper center", bbox_to_anchor=(0.48, -0.18), ncol=2, frameon=False)
fig.text(
    0.02,
    -0.02,
    "Shorter stops are not faster arrivals. Pre-dispatch includes sensing, waiting, Agents and planning; it is not pure LLM latency.",
    fontsize=9,
)
save(fig, "outcomes-and-time")

selected = [ROWS[0], ROWS[2], ROWS[4]]
fig, axes = plt.subplots(1, 3, figsize=(12.2, 4.8), sharex=True, sharey=True)
for ax, row in zip(axes, selected):
    trace = row["display_trajectory"]
    ax.plot(
        [v[1] for v in trace],
        [v[2] for v in trace],
        color=BLUE,
        lw=2.2,
        label="Observed robot path",
    )
    ax.scatter(
        trace[0][1], trace[0][2], marker="s", color="#343d4a", label="Observed start", zorder=5
    )
    ax.scatter(1, 0, marker="*", s=130, color=GREEN, label="Requested final goal", zorder=5)
    ax.add_patch(Circle((1, 0), 0.30, fc=GREEN, alpha=0.08))
    for goal in row["goals"]:
        ax.scatter(*goal["end_robot_xy_m"], color=BLUE, facecolors="none", s=65, zorder=6)
        if len(row["goals"]) == 2 and goal["goal_index"] == 0:
            ax.scatter(
                *goal["target_xy_m"], marker="D", color=ORANGE, s=45, label="Requested via point"
            )
            ax.add_patch(Circle(tuple(goal["target_xy_m"]), 0.30, fc=ORANGE, alpha=0.09))
    ax.plot(
        [0, 0],
        [row["actor_initial_y"], 1.6],
        color=RED,
        ls=":",
        alpha=0.55,
        label="Actor center corridor",
    )
    ax.set_title(row["name"], fontsize=11)
    ax.set_xlabel("Map x (m)")
    ax.set_xlim(-1.25, 1.45)
    ax.set_ylim(-1.15, 1.8)
    ax.set_aspect("equal")
    ax.grid(alpha=0.2)
axes[0].set_ylabel("Map y (m)")
handles, labels = [], []
for ax in axes:
    for handle, label in zip(*ax.get_legend_handles_labels()):
        if label not in labels:
            handles.append(handle)
            labels.append(label)
fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, -0.08), ncol=3, frameon=False)
fig.suptitle("Actual saved trajectories and requested goal regions", weight="bold")
fig.text(
    0.05,
    -0.13,
    "Display samples: every fifth saved pose plus final point; no smoothing. Circles: 0.30 m endpoint bounds. Not collision clearance.",
    fontsize=9,
)
save(fig, "observed-trajectories")

fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True)
x = list(range(len(ROWS)))
axes[0].bar(
    x,
    [r["prediction"]["predicted_exposure"] for r in ROWS],
    color=[ORANGE if i == 5 else BLUE for i in x],
)
axes[0].axhline(0.2, color=RED, ls="--", label="Forecast threshold 0.20")
axes[0].set_title("Recorded predicted-mask score (last prediction)")
for i, r in enumerate(ROWS):
    if r["post_wait_passed"] is not None:
        axes[1].bar(i, r["exposure"], color=GREEN if r["post_wait_passed"] else ORANGE)
    else:
        axes[1].text(i, 0.03, "N/A", rotation=90, ha="center", fontsize=9)
axes[1].axhline(0.06, color=RED, ls="--", label="Observed-clearance threshold 0.06")
axes[1].set_title("Post-wait observed exposure (no wait = N/A)")
for ax in axes:
    ax.set_xticks(x, [r["name"] for r in ROWS], rotation=60, ha="right", fontsize=8)
    ax.set_ylim(0, 0.8)
    ax.grid(axis="y", alpha=0.2)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8)
axes[0].set_ylabel("Exposure score (not a probability)")
fig.suptitle("Prediction and observation have different thresholds and timestamps", weight="bold")
fig.text(
    0.06,
    -0.29,
    "These are not paired forecast-error measurements. R2's 0.414 forecast exceeded 0.20, yet Recovery selected a wait before detouring.",
    fontsize=9,
)
save(fig, "prediction-and-observation")

historical = DATA["historical_three_arm"]["trials"]
fig, ax = plt.subplots(figsize=(8, 4.2))
for i, (suffix, label) in enumerate(
    [("history", "Image history"), ("nwm", "Rule + NWM"), ("agent", "Agent + NWM")]
):
    times = [r["seconds"] for r in historical if r["name"].endswith(suffix)]
    mean = sum(times) / len(times)
    ax.bar(i, mean, width=0.56, color=["#9caabd", GREEN, BLUE][i], alpha=0.8)
    ax.scatter([i - 0.08, i + 0.08], times, color="#222", s=30, zorder=4)
    ax.text(i, mean + 0.8, f"{mean:.3f}", ha="center")
ax.set_xticks(range(3), ["Image history", "Rule + NWM", "Agent + NWM"])
ax.set_ylim(0, 31)
ax.set_ylabel("Decision-to-arrival (sim s)")
ax.set_title(
    "Historical six-run comparison, separate from the public cohort",
    loc="left",
    weight="bold",
    fontsize=11,
)
ax.grid(axis="y", alpha=0.2)
ax.set_axisbelow(True)
fig.text(
    0.02,
    -0.02,
    "Bars: mean of two runs. Dots: individual results; no confidence intervals. Different source/protocol from the public seven-run cohort.",
    fontsize=8,
)
save(fig, "historical-three-arm")
print(f"Wrote four PNG/SVG figure pairs in {OUT.name}")
