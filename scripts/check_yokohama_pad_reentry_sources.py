#!/usr/bin/env python3
"""Reopen a reentry bundle with its frozen sources, then replay it with current Rules.

The bundle pins its experiment sources, including check_yokohama_pad_reentry.py
itself, so a later edit to any pinned file stops the unchanged checker. Two
separate checks replace that single identity test:

1. Frozen reproduction: the unchanged checker runs from a temporary copy of
   scripts/ and src/ in which every pinned file matches its pin. A changed file
   is restored only from docs/examples/yokohama-frozen-sources, and only when
   that copy has the pinned hash.
2. Current-code equivalence: the current shared queue Rules revalidate every
   recorded executor check and reproduce every recorded judgment identity.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
FROZEN = REPO / "docs/examples/yokohama-frozen-sources"
sys.path.insert(0, str(REPO))
from scripts.yokohama_pad_state import sha  # noqa: E402
from src.runtime import yokohama_pad_queue as queue  # noqa: E402

# Runs the unchanged checker inside the frozen tree and refuses any scripts/src
# module loaded from outside it.
RUNNER = """
import json, sys
from pathlib import Path
root, bundle = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
sys.path.insert(0, str(root))
from scripts.check_yokohama_pad_reentry import check
result = check(bundle)
outside = sorted(
    name for name, module in list(sys.modules.items())
    if name.split(".")[0] in ("scripts", "src") and getattr(module, "__file__", None)
    and not Path(module.__file__).resolve().is_relative_to(root)
)
if outside:
    raise SystemExit("Frozen checker imported live sources: " + ", ".join(outside))
print(json.dumps(result))
"""


def frozen_tree(root, pins, repo=REPO, frozen=FROZEN):
    for part in ("scripts", "src"):
        shutil.copytree(repo / part, root / part, ignore=shutil.ignore_patterns("__pycache__"))
    # Registered weights and bundles are data read from docs/, never imported.
    (root / "docs").symlink_to(repo / "docs", target_is_directory=True)
    restored = []
    for name, value in pins.items():
        parts = PurePosixPath(name).parts
        if parts[0] not in ("scripts", "src") or ".." in parts:
            raise ValueError("Pinned source outside scripts/ or src/: " + name)
        target = root / name
        if target.is_file() and sha(target) == value:
            continue
        copy = frozen / name
        if not copy.is_file() or copy.is_symlink() or sha(copy) != value:
            raise ValueError("Frozen experiment source unavailable: " + name)
        shutil.copyfile(copy, target)
        restored.append(name)
    return sorted(restored)


def frozen_reproduction(bundle, repo=REPO, frozen=FROZEN):
    pins = json.loads((bundle / "source-sha256.json").read_text())
    with TemporaryDirectory() as temp:
        root = Path(temp)
        restored = frozen_tree(root, pins, repo, frozen)
        run = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER, str(root), str(bundle.resolve())],
            capture_output=True,
            text=True,
            check=False,
        )
    if run.returncode != 0:
        tail = (run.stderr.strip().splitlines() or ["no output"])[-1]
        raise ValueError("Frozen checker failed: " + tail)
    result = json.loads(run.stdout.strip().splitlines()[-1])
    if result.get("status") != "passed":
        raise ValueError("Frozen checker did not pass")
    return dict(result, restored_sources=restored)


def current_replay(bundle):
    """Replay the recorded executor checks with the current queue Rules."""
    config = json.loads((bundle / "capture-config.json").read_text())
    evaluation = json.loads((bundle / "evaluation/evaluation.json").read_text())
    config["run_id"] = "pad-reentry-offline-replay"
    config["operator_approval"] = "explicit offline fixture replay only; no aircraft dispatch"
    count = 0
    with gzip.open(bundle / "evaluation/runtime-trace.jsonl.gz", "rt") as stream:
        for line in stream:
            record = json.loads(line)
            request, response = record["request"], record["response"]
            config["world"]["pad_state_advisory"] = dict(
                mode="assist",
                camera_entity="pad_state_camera",
                weights_sha256=evaluation["weights_sha256"][
                    "post_trained" if record["method"].startswith("post_trained") else "existing"
                ],
            )
            if record["method"] == "post_trained_risk":
                config["world"]["pad_state_advisory"]["reentry_risk_advisory"] = True
            action = queue.require_response(config, request, response, request["observations"][-1])
            if (
                queue.digest(record["judgment"]) != response["mission_assurance_sha256"]
                or record["judgment"]["proposal"]["parameters"]["action"]
                != response["proposed_action"]
                or action != response["proposed_action"]
            ):
                raise ValueError("Current Rules changed a recorded judgment")
            count += 1
    if count != evaluation["shared_mission_judgments"]:
        raise ValueError("Missing judgment receipts")
    return dict(judgments=count, unchanged=True)


def check(bundle):
    return dict(
        status="passed",
        frozen_reproduction=frozen_reproduction(bundle),
        current_code_replay=current_replay(bundle),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(check(args.bundle)))
