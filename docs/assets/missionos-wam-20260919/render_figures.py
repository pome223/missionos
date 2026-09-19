"""Rebuild report figures from reviewed public data; no simulator or model access.

Run: uv run --no-project --with matplotlib python <this-file>
"""

import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parent
D = json.loads((ROOT / "figure-data.json").read_text())
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.hashsalt": "missionos-wam-report-v1",
    }
)
COLORS = {
    "vla": "#8392a5",
    "current": "#e5a541",
    "old": "#9176b5",
    "new": "#188e87",
    "width": "#253c60",
}
LABELS = {
    "vla": "VLA only",
    "current": "Current rule",
    "old": "Old WAM",
    "new": "WAM / new",
    "width": "Width rule",
}


def save(fig, name):
    fig.savefig(ROOT / (name + ".png"), dpi=180, bbox_inches="tight", facecolor="white")
    fig.savefig(ROOT / (name + ".svg"), bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


fig, axes = plt.subplots(1, 3, figsize=(13, 4.6), sharey=True)
for ax, (v, rows) in zip(axes, D["cohorts"].items()):
    keys = [k for k in COLORS if k in rows[0]]
    means = [sum(r[k] for r in rows) / 40 for k in keys]
    bars = ax.bar(range(len(keys)), means, color=[COLORS[k] for k in keys], width=0.7)
    ax.bar_label(bars, labels=[f"{x:.3f}" for x in means], padding=3, fontsize=9)
    ax.set_xticks(range(len(keys)), [LABELS[k] for k in keys], rotation=28, ha="right")
    ax.set_ylim(0, 10)
    ax.set_title(v.upper() + (" | original scoring" if v != "v7" else " | uniform terminal hold"))
    ax.grid(axis="y", alpha=0.2)
    ax.set_axisbelow(True)
axes[0].set_ylabel("Mean points per game (40 games per cohort)")
fig.suptitle("Separate cohorts and endpoints: do not interpret as a learning curve", fontsize=13)
fig.tight_layout()
save(fig, "01-cohort-scores")

rows = sorted(D["cohorts"]["v7"], key=lambda r: (r["width_group"] != "wide", r["seed"]))
keys = ["vla", "current", "old", "new", "width"]
fig, ax = plt.subplots(figsize=(14, 4.1))
values = np.array([[r[k] for r in rows] for k in keys])
im = ax.imshow(values, vmin=0, vmax=10, cmap="YlGnBu", aspect="auto")
for i in range(5):
    for j in range(40):
        ax.text(
            j,
            i,
            str(values[i, j]),
            ha="center",
            va="center",
            fontsize=8,
            color="white" if values[i, j] >= 7 else "#172432",
        )
ax.set_yticks(range(5), [LABELS[k] for k in keys])
ax.set_xticks(range(40), [str(r["seed"]) for r in rows], rotation=90, fontsize=8)
ax.axvline(19.5, color="white", linewidth=3)
ax.set_title("V7: every measured terminal score | wide games (left), narrow games (right)")
fig.colorbar(im, ax=ax, label="Points", shrink=0.7)
fig.tight_layout()
save(fig, "02-v7-game-scores")

fig, ax = plt.subplots(figsize=(10, 5))
stats = D["v7_paired_statistics"]
for y, (name, mean, lo, hi, p) in enumerate(stats):
    ax.errorbar(
        mean,
        y,
        xerr=[[mean - lo], [hi - mean]],
        fmt="o",
        color="#188e87" if p < 0.05 else "#53647b",
        capsize=4,
    )
    ax.text(8.1, y, f"{p:.5f}", va="center", fontsize=10)
ax.axvline(0, color="#9a4853", ls="--")
ax.set_yticks(range(7), [r[0] for r in stats])
ax.invert_yaxis()
ax.set_xlim(-3, 9.4)
ax.set_xlabel("Paired mean point difference; unadjusted 95% bootstrap interval")
ax.set_title("V7 paired comparisons | p-values adjusted across all seven comparisons")
ax.text(8.1, -0.6, "Holm p", fontsize=10)
ax.grid(axis="x", alpha=0.2)
fig.tight_layout()
save(fig, "03-v7-paired-intervals")

fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
deltas = [r["new"] - r["width"] for r in D["cohorts"]["v7"]]
assert deltas.count(1) == 15 and deltas.count(-8) == 2 and sum(deltas) == -1
axes[0].bar(
    ["15 gains", "2 collapses", "Net"], [15, -16, -1], color=["#188e87", "#c35c55", "#253c60"]
)
for i, n in enumerate([15, -16, -1]):
    axes[0].text(
        i, n + (1 if n >= 0 else -1), f"{n:+d}", ha="center", va="bottom" if n >= 0 else "top"
    )
axes[0].axhline(0, color="#778")
axes[0].set_ylim(-20, 19)
axes[0].set_ylabel("Total points relative to width stopping")
axes[0].set_title("Many small gains, two large losses")
axes[1].bar(
    ["Narrow: 6", "Wide: 8", "Wide: 9"], [20, 3, 17], color=["#253c60", "#188e87", "#188e87"]
)
axes[1].set_ylim(0, 23)
axes[1].set_ylabel("Games by attempted bank count")
axes[1].set_title("Stopping still strongly tracks width")
for i, n in enumerate([20, 3, 17]):
    axes[1].text(i, n + 0.4, str(n), ha="center")
fig.tight_layout()
save(fig, "04-v7-tradeoff")

fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))
x = np.arange(2)
series = [
    ("deterministic", "WAM + deterministic rule", "#253c60"),
    ("deepseek_r3", "DeepSeek R3", "#a8adb9"),
    ("deepseek_r4", "DeepSeek R4", "#188e87"),
]
for i, (key, label, col) in enumerate(series):
    bars = axes[0].bar(x + (i - 1) * 0.24, D["e2e"][key], width=0.24, label=label, color=col)
    axes[0].bar_label(bars, padding=2)
axes[0].set_xticks(x, [str(s) for s in D["e2e"]["seeds"]])
axes[0].set_ylim(0, 11)
axes[0].set_ylabel("Measured terminal points")
axes[0].set_title("Two inspected cases: not held-out evaluation")
axes[0].legend(fontsize=8, loc="lower left")
for i, c in enumerate(D["proxy_cases"]):
    n = c["next_count"]
    bank = (n - 1) * (1 - c["bank_risk"])
    cont = n * (1 - c["continue_risk"])
    axes[1].bar(i - 0.16, bank, 0.32, color="#253c60", label="Bank proxy" if i == 0 else None)
    axes[1].bar(i + 0.16, cont, 0.32, color="#188e87", label="Continue proxy" if i == 0 else None)
    for xx, v in [(i - 0.16, bank), (i + 0.16, cont)]:
        axes[1].text(xx, v + 0.12, f"{v:.3f}", ha="center", fontsize=9)
axes[1].set_xticks(x, ["67002: next 6", "67016: next 9"])
axes[1].set_ylim(0, 10)
axes[1].set_title("Illustrative arithmetic, NOT calibrated utility")
axes[1].set_ylabel("Proxy points")
axes[1].legend(fontsize=8, loc="lower left")
fig.tight_layout()
save(fig, "05-deepseek-correction")

fig, ax = plt.subplots(figsize=(12, 7))
ax.set_xlim(0, 12)
ax.set_ylim(0, 7)
ax.axis("off")
boxes = [
    (0.3, 5.45, "Observation + VLA macro", "Current state / proposed option"),
    (4.3, 5.45, "Prediction Core + WAM", "Bound model-inferred forecast"),
    (8.3, 5.45, "LLM Assurance", "Evidence intake + actual judgment"),
    (8.3, 3, "Human approval", "Bounded preapproved policy"),
    (4.3, 3, "Rules", "Revalidate + reserve + ticket"),
    (0.3, 3, "Executor + SmolVLA", "Ticketed simulator motor steps"),
    (0.3, 0.55, "Verifier", "Measured collapse / score"),
    (4.3, 0.55, "Next state / terminal receipt", "Revision changes; old evidence invalid"),
]
for i, (x, y, title, sub) in enumerate(boxes):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            3.4,
            1.15,
            boxstyle="round,pad=.08",
            facecolor="#e9f4f2" if i < 3 else "#edf0f6",
            edgecolor="#446078",
        )
    )
    ax.text(x + 1.7, y + 0.75, title, ha="center", va="center", fontsize=11, fontweight="bold")
    ax.text(x + 1.7, y + 0.32, sub, ha="center", va="center", fontsize=8)
for a, b in [
    ((3.8, 6), (4.15, 6)),
    ((7.8, 6), (8.15, 6)),
    ((10, 5.3), (10, 4.35)),
    ((8.15, 3.6), (7.8, 3.6)),
    ((4.15, 3.6), (3.8, 3.6)),
    ((2, 2.85), (2, 1.85)),
    ((3.8, 1.1), (4.15, 1.1)),
]:
    ax.annotate("", xy=b, xytext=a, arrowprops=dict(arrowstyle="->", lw=2, color="#446078"))
ax.text(
    8.6,
    1.15,
    "Forecast ≠ approval\nJudgment ≠ dispatch\nExecution ≠ verified outcome",
    fontsize=11,
    va="center",
    color="#843f45",
)
ax.set_title(
    "MissionOS: evidence and authority remain separate at each transition", fontsize=14, pad=10
)
fig.tight_layout()
save(fig, "06-governed-architecture")
print("Rendered six PNG/SVG figures from reviewed public data.")
