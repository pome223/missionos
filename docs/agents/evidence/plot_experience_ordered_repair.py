"""Rebuild publication figures from reviewed aggregate data only.

Requires matplotlib. This script does not open a Backend, invoke a model, or
load private episodes. It regenerates the two SVG figures used by the report.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
DATA = HERE / "experience-ordered-repair-analysis-20260915.json"
ASSETS = HERE.parent / "assets"
COLORS = {
    "parent": "#657381",
    "fixed": "#377eb8",
    "experience": "#d56c1c",
    "shared": "#657381",
    "neither": "#c9d4de",
}


def load_cohorts() -> list[dict]:
    analysis = json.loads(DATA.read_text())
    assert analysis["schema"] == "missionos.public-experience-ordered-repair-analysis.v1"
    cohorts = analysis["direct_trial_cohorts"]
    assert [cohort["starts"] for cohort in cohorts] == [16, 20]
    for cohort in cohorts:
        paired = cohort["paired_fixed_vs_experience"]
        assert sum(paired.values()) == cohort["starts"]
        assert cohort["fixed_order_completed"] == (
            paired["both_complete"] + paired["fixed_only_complete"]
        )
        assert cohort["experience_order_completed"] == (
            paired["both_complete"] + paired["experience_only_complete"]
        )
        assert cohort["additional_vs_fixed"] == paired["experience_only_complete"]
        assert (
            cohort["lost_parent_successes"] == cohort["unknown"] == cohort["protection_losses"] == 0
        )
        if "targeted_role_breakdown" in cohort:
            roles = cohort["targeted_role_breakdown"].values()
            for field, total in (
                ("starts", cohort["starts"]),
                ("parent_completed", cohort["parent_completed"]),
                ("fixed_completed", cohort["fixed_order_completed"]),
                ("experience_completed", cohort["experience_order_completed"]),
                ("experience_only_completed", cohort["additional_vs_fixed"]),
            ):
                assert sum(role[field] for role in roles) == total
    return cohorts


def write_svg(fig, path: Path) -> None:
    stream = io.StringIO()
    fig.savefig(stream, format="svg", facecolor="white")
    path.write_text("\n".join(line.rstrip() for line in stream.getvalue().splitlines()) + "\n")
    plt.close(fig)


def plot_completion_counts(cohorts: list[dict]):
    names = ["Initial 16-start cohort", "Targeted 20-start cohort"]
    labels = ["Parent only", "Fixed alternative order", "Experience-ordered alternative"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "svg.fonttype": "none"})
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.5), sharey=True)
    fig.patch.set_facecolor("white")
    for ax, title, cohort in zip(axes, names, cohorts, strict=True):
        denominator = cohort["starts"]
        counts = [
            cohort["parent_completed"],
            cohort["fixed_order_completed"],
            cohort["experience_order_completed"],
        ]
        for i, (count, color) in enumerate(
            zip(counts, (COLORS["parent"], COLORS["fixed"], COLORS["experience"]), strict=True)
        ):
            ax.barh(i, count, height=0.56, color=color, zorder=3)
            ax.text(
                count + 0.25, i, f"{count}/{denominator}", va="center", ha="left", fontweight="bold"
            )
        ax.set_title(title, loc="left", pad=17, fontweight="bold")
        ax.set_xlim(0, 20)
        ax.set_xticks([0, 5, 10, 15, 20])
        ax.set_yticks(range(3), labels)
        ax.invert_yaxis()
        ax.grid(axis="x", color="#dce2e7", linewidth=0.8, zorder=0)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.text(
            0.0,
            -0.17,
            f"Gain vs fixed order: +{cohort['additional_vs_fixed']} starts",
            transform=ax.transAxes,
            ha="left",
            va="top",
            color="#2e3a46",
            fontweight="bold",
        )
    fig.suptitle(
        "Book-placement repair: same-start simulator completion",
        x=0.02,
        ha="left",
        fontsize=14,
        fontweight="bold",
    )
    fig.supxlabel("Completed starts (exact counts; cohort denominators differ)", y=0.04)
    fig.subplots_adjust(left=0.24, right=0.98, top=0.77, bottom=0.24, wspace=0.18)
    return fig


def plot_paired_outcomes(cohorts: list[dict]):
    fig, ax = plt.subplots(figsize=(10.0, 3.65))
    fig.patch.set_facecolor("white")
    categories = [
        ("both_complete", "Both orders complete", COLORS["shared"]),
        ("experience_only_complete", "Experience order only", COLORS["experience"]),
        ("fixed_only_complete", "Fixed order only", COLORS["fixed"]),
        ("neither_complete", "Neither order completes", COLORS["neither"]),
    ]
    for index, cohort in enumerate(cohorts):
        left = 0
        for key, label, color in categories:
            count = cohort["paired_fixed_vs_experience"][key]
            if count:
                ax.barh(
                    index,
                    count,
                    left=left,
                    height=0.54,
                    color=color,
                    label=label if index == 0 else None,
                )
                ax.text(
                    left + count / 2,
                    index,
                    str(count),
                    color="white" if color != COLORS["neither"] else "#263642",
                    va="center",
                    ha="center",
                    fontweight="bold",
                )
                left += count
        assert left == cohort["starts"]
        ax.text(left + 0.35, index, f"n={left}", va="center", ha="left", color="#263642")
    ax.set_yticks((0, 1), ("Initial 16 starts", "Targeted 20 starts"))
    ax.invert_yaxis()
    ax.set_xlim(0, 22)
    ax.set_xticks((0, 5, 10, 15, 20))
    ax.set_xlabel("Paired start outcomes (exact counts)")
    ax.grid(axis="x", color="#dce2e7", linewidth=0.8, zorder=0)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.suptitle(
        "Fixed vs experience order: where the terminal outcomes differ",
        x=0.02,
        ha="left",
        fontsize=13,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.045,
        "Fixed-only completions: 0 in both cohorts",
        ha="center",
        fontsize=10,
        fontweight="bold",
        color="#2e3a46",
    )
    # The zero-count fixed-only category does not appear as a segment.
    fig.legend(
        [Patch(color=COLORS[key]) for key in ("shared", "experience", "neither")],
        ["Both orders complete", "Experience order only", "Neither order completes"],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.15),
        ncol=3,
        frameon=False,
    )
    fig.subplots_adjust(left=0.19, right=0.98, top=0.74, bottom=0.39)
    return fig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preview-dir", type=Path, help="Also render PNG copies for visual inspection"
    )
    args = parser.parse_args()
    cohorts = load_cohorts()
    ASSETS.mkdir(parents=True, exist_ok=True)
    figures = (
        ("experience-ordered-repair-outcomes-20260915", plot_completion_counts),
        ("experience-ordered-repair-paired-outcomes-20260915", plot_paired_outcomes),
    )
    for stem, plot in figures:
        fig = plot(cohorts)
        if args.preview_dir:
            args.preview_dir.mkdir(parents=True, exist_ok=True)
            fig.savefig(args.preview_dir / f"{stem}.png", dpi=180, facecolor="white")
        write_svg(fig, ASSETS / f"{stem}.svg")


if __name__ == "__main__":
    main()
