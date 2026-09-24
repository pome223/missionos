"""Layout actual, source-bound RGB observations; does not synthesize scenery."""

import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent


def render():
    observations = json.loads((ROOT / "observations.json").read_text())
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.3), layout="constrained")
    for ax, family in zip(axes, ("gap", "climb", "detour")):
        matches = [r for r in observations if r["family"] == family]
        if matches:
            r = matches[0]
            path = ROOT / r["file"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == r["image_sha256"]
            assert r["generated_image"] is False
            ax.imshow(mpimg.imread(path))
            x, y, z = r["pose_enu_m"]
            ax.set_title(
                f"{family.title()} | observed RGB\nENU ({x:.2f}, {y:.2f}, {z:.2f}) m",
                fontsize=12,
            )
        else:
            ax.text(
                0.5, 0.5, "No verified example", transform=ax.transAxes, ha="center"
            )
            ax.set_title(family.title())
        ax.axis("off")
    fig.suptitle(
        "Actual PX4/Gazebo camera observations — CPU depth baseline",
        fontsize=16,
        fontweight="bold",
    )
    fig.supxlabel(
        "Illustrations from variant 0; not predictions or additional flights.\nApartment asset: OSRF gazebo_models, CC BY 3.0; Nathan Koenig / Cole Biesemeyer.",
        fontsize=10,
    )
    fig.savefig(ROOT / "observed-rgb.png", dpi=150, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    render()
