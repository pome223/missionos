#!/usr/bin/env python3
"""Post-hoc, GPU-free diagnosis of the native regional forecast result.

Reads the published bundle and the published pad geometry only. Recorded actor
poses explain ground truth here; they were never native model inputs. This is
not a new experiment, a native rerun or an adoption change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.prepare_yokohama_pad_focus import state
from scripts.yokohama_pad_focus_data import sha, write

REPO = Path(__file__).resolve().parents[1]
GEOMETRY = "docs/examples/yokohama-pad-queue/protocol.json"
CHANGE = 20  # same per-channel moving-pixel threshold as the training ROI
LATENT_STRIDE = 8
WINDOW = 512


def r(value):
    return round(float(value), 3)


def components(mask):
    """Eight-connected foreground components, largest first: [pixels, w, h, cx, cy]."""
    seen = np.zeros_like(mask, bool)
    out = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        stack, points = [(y, x)], []
        while stack:
            a, b = stack.pop()
            points.append((a, b))
            for c in range(max(a - 1, 0), min(a + 2, mask.shape[0])):
                for d in range(max(b - 1, 0), min(b + 2, mask.shape[1])):
                    if mask[c, d] and not seen[c, d]:
                        seen[c, d] = True
                        stack.append((c, d))
        ys, xs = np.array(points).T
        out.append([len(points), int(np.ptp(xs)) + 1, int(np.ptp(ys)) + 1, xs.mean(), ys.mean()])
    return sorted(out, key=lambda c: -c[0])


def changed(a, b):
    return np.max(np.abs(a.astype(float) - b.astype(float)), axis=2) > CHANGE


def diagnose(bundle):
    evaluation = json.loads((bundle / "evaluation.json").read_text())
    truth = {t["id"]: t for t in json.loads((bundle / "prepared/truth.json").read_text())}
    bindings = json.loads((bundle / "prepared/capture-bindings.json").read_text())
    data = json.loads((bundle / "prepared/dataset/dataset.json").read_text())
    training = json.loads((bundle / "results/training.json").read_text())
    development = json.loads((bundle / "results/development.json").read_text())
    geometry = json.loads((REPO / GEOMETRY).read_text())["pad_queue"]
    pad = np.asarray(geometry["pad_xyz_m"])
    captures = {}
    for path in sorted((bundle / "capture").glob("focus-*.json")):
        captures[sha(path)] = json.loads(path.read_text())
    background = np.asarray(Image.open(bundle / "prepared/dataset/background.png").convert("RGB"))

    def distance(frame):
        return float(np.linalg.norm(np.asarray(frame["lead_pose"]["xyz"][:2]) - pad[:2]))

    # Bind the published geometry to every evaluation label before using it.
    tests = {b["id"]: b for b in bindings if b["id"] in truth}
    if set(tests) != set(truth):
        raise ValueError("Evaluation binding set")
    for name, b in tests.items():
        frames = captures[b["capture_sha256"]]["frames"]
        if (
            state(frames[b["target_index"]], pad) != truth[name]["state"]
            or state(frames[b["source_index"]], pad) != truth[name]["current_state"]
        ):
            raise ValueError("Pad geometry does not reproduce evaluation truth")

    rows = evaluation["rows"]
    known = [row for row in rows if row["truth"] != "unknown"]
    changes = [row for row in known if row["truth"] != row["current_pose_state"]]
    stages = list(rows[0]["methods"])
    headroom = dict(
        known_targets=len(known),
        persistence_matches=evaluation["counts"]["persistence"]["matches"],
        state_change_conditions=[
            dict(
                id=row["id"],
                current=row["current_pose_state"],
                future=row["truth"],
                **{stage: row["methods"][stage]["state"] for stage in stages},
            )
            for row in changes
        ],
        max_gain_over_persistence=len(known) - evaluation["counts"]["persistence"]["matches"],
        clear_to_occupied_opportunities=sum(
            row["current_pose_state"] == "clear" and row["truth"] == "occupied" for row in known
        ),
    )
    failure_mode = {
        stage: dict(
            matches=evaluation["counts"][stage]["matches"],
            wrong_state=evaluation["counts"][stage]["false_clear"]
            + evaluation["counts"][stage]["false_occupied"],
            unknown=evaluation["counts"][stage]["unknown"],
        )
        for stage in stages
    }

    groups = []
    for overlap in (True, False):
        for horizon in sorted({row["horizon_s"] for row in rows}):
            members = [
                row
                for row in rows
                if row["exact_training_history_overlap"] is overlap and row["horizon_s"] == horizon
            ]
            group = dict(
                exact_training_history_overlap=overlap, horizon_s=horizon, conditions=len(members)
            )
            for stage in stages:
                group[stage] = dict(
                    known_matches=sum(
                        row["truth"] != "unknown" and row["methods"][stage]["state"] == row["truth"]
                        for row in members
                    ),
                    known_targets=sum(row["truth"] != "unknown" for row in members),
                    median_rgb_mae=r(
                        statistics.median(row["methods"][stage]["rgb_mae"] for row in members)
                    ),
                )
            groups.append(group)

    pairs = [s for s in data["samples"] if s["split"] == "train"]
    histories = {s["history"] for s in pairs}

    def window(rows):
        return dict(
            median_loss=r(statistics.median(x["loss"] for x in rows)),
            median_roi=r(statistics.median(x["roi"] for x in rows)),
        )

    learning = dict(
        pairs=len(pairs),
        unique_histories=len(histories),
        updates=len(training),
        updates_per_pair=r(len(training) / len(pairs)),
        first_512=window(training[:WINDOW]),
        last_512=window(training[-WINDOW:]),
        gradient_clipped_fraction=r(sum(x["gradient"] > 1 for x in training) / len(training)),
    )

    scale, motion = [], []
    for site in sorted({s["history"] for s in data["samples"] if s["split"] == "test"}):
        with np.load(bundle / "prepared/dataset" / site, allow_pickle=False) as a:
            rgb = a["rgb"]
        now = components(changed(rgb[-1], background))
        before = components(changed(rgb[0], background))
        name = Path(site).stem
        scale.append(
            dict(
                history=name,
                changed_pixel_fraction=r(changed(rgb[-1], background).mean()),
                largest_components_wh=[c[1:3] for c in now[:2]],
            )
        )
        binding = next(b for b in tests.values() if b["id"].startswith(name + "-"))
        frames = captures[binding["capture_sha256"]]["frames"]
        i = binding["source_index"]
        motion.append(
            dict(
                history=name,
                largest_component_shift_px=r(
                    np.hypot(now[0][3] - before[0][3], now[0][4] - before[0][4])
                ),
                lead_distance_m={
                    "-3.75s": r(distance(frames[i - 15])),
                    "0s": r(distance(frames[i])),
                    "+1s": r(distance(frames[i + 4])),
                    "+4s": r(distance(frames[i + 16])),
                },
            )
        )
    widths = [s["largest_components_wh"][0][0] for s in scale]
    heights = [s["largest_components_wh"][0][1] for s in scale]

    windows = {}
    for record in captures.values():
        frames = record["frames"]
        labels = [state(f, pad) for f in frames]
        windows[record["case"]["id"]] = {
            f"+{offset / 4:g}s": r(
                sum(
                    labels[i] == "clear" and labels[i + offset] == "occupied"
                    for i in range(len(frames) - offset)
                )
                / 4
            )
            for offset in (4, 16)
        }

    return dict(
        schema="native_pad_focus_diagnosis.v1",
        post_hoc=True,
        native_rerun=False,
        actor_pose_model_input=False,
        adoption_changed=False,
        attribution="Correlational; the combined crop/horizon/conditioning/scope/loss change stays unattributed",
        geometry_source=dict(path=GEOMETRY, sha256=sha(REPO / GEOMETRY)),
        headroom=headroom,
        failure_mode=failure_mode,
        by_history_overlap=groups,
        training=learning,
        development=dict(
            native_passed=sum(x["passed"] for x in development["rows"]),
            vae_round_trip_matches=sum(
                x["vae_reader"]["state"] == x["target"] for x in development["rows"]
            ),
            probes=len(development["rows"]),
            vae_round_trip_min_margin=r(
                min(x["vae_reader"]["margin"] for x in development["rows"])
            ),
        ),
        object_scale=dict(
            crop_px=list(background.shape[:2]),
            latent_stride=LATENT_STRIDE,
            median_largest_component_wh_px=[
                r(statistics.median(widths)),
                r(statistics.median(heights)),
            ],
            median_largest_component_wh_latent=[
                r(statistics.median(widths) / LATENT_STRIDE),
                r(statistics.median(heights) / LATENT_STRIDE),
            ],
            max_changed_pixel_fraction=max(s["changed_pixel_fraction"] for s in scale),
            histories=scale,
        ),
        motion_in_history=motion,
        clear_to_occupied_window_s=windows,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--write", action="store_true", help="Write diagnosis.json into the bundle")
    args = p.parse_args()
    result = diagnose(args.bundle)
    if args.write:
        write(args.bundle / "diagnosis.json", result)
    print(json.dumps(result, indent=2))
