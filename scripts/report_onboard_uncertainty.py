"""Reopen a frozen camera/SITL batch, retain failures, and emit a portable report.

This script only reads existing runs. It cannot start a flight or model service.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil

from src.runtime.ship_onboard_comparison import reverify_onboard_run


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_batch(root):
    root = Path(root)
    protocol = json.loads((root / "protocol-freeze.json").read_text())
    attempts = json.loads((root / "attempts.json").read_text())
    repo = Path(__file__).resolve().parents[1]
    if (
        protocol.get("schema_version") != "ship_onboard_uncertainty_batch.v1"
        or protocol.get("gpu_cost_usd") != 0
        or not protocol.get("source_sha256")
        or {tuple(v) for v in protocol["matrix"]}
        != {
            (c, p)
            for c in ("short_clear", "long_block", "brake_stop")
            for p in ("onboard_stopping", "onboard_uncertainty")
        }
    ):
        raise ValueError("Unsupported batch protocol")
    for name, digest in protocol["source_sha256"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid source path")
        if (
            digest_file(repo / relative) != digest
            or digest_file(root / "source" / relative) != digest
        ):
            raise ValueError("Frozen runtime source changed: " + name)
    expected = {tuple(v) for v in protocol["matrix"]}
    if expected != {(a["case"], a["policy"]) for a in attempts}:
        raise ValueError("Incomplete or unexpected frozen matrix")
    if len(attempts) not in (6, 7) or len({a["directory"] for a in attempts}) != len(attempts):
        raise ValueError("Invalid attempt denominator")
    records = []
    run_ids = set()
    for index, attempt in enumerate(attempts):
        name = attempt["directory"]
        if Path(name).name != name:
            raise ValueError("Invalid attempt path")
        directory = root / name
        result = json.loads((directory / "result.json").read_text())
        config = result["config"]
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        if attempt["urban_decision_recorded"] != any(
            e["event"] == "urban_decision" for e in events
        ):
            raise ValueError("Attempt hides an urban decision")
        if config["run_id"] in run_ids:
            raise ValueError("Reused flight identity")
        run_ids.add(config["run_id"])
        if (config["urban"]["case"], config["urban"]["policy"]) != (
            attempt["case"],
            attempt["policy"],
        ):
            raise ValueError("Attempt label does not match flight")
        if datetime.fromisoformat(attempt["started_at_utc"]) <= datetime.fromisoformat(
            protocol["frozen_at_utc"]
        ):
            raise ValueError("Flight predates freeze")
        if not result.get("container_removed"):
            raise ValueError("Simulator cleanup not confirmed")
        if any(
            result.get(k)
            for k in (
                "vla_invoked",
                "wam_invoked",
                "vision_language_model_invoked",
                "physical_execution_invoked",
            )
        ):
            raise ValueError("Unexpected model or hardware invocation")
        verified = None
        error = None
        try:
            verified = reverify_onboard_run(directory)
        except (ValueError, KeyError, OSError) as exc:
            error = str(exc)
        if result.get("status") == "completed" and error:
            raise ValueError("Claimed completion failed reverification: " + error)
        if attempt["status"] != result["status"]:
            raise ValueError("Attempt status mismatch")
        previous = [
            a
            for a in attempts[:index]
            if (a["case"], a["policy"]) == (attempt["case"], attempt["policy"])
        ]
        if previous and (
            len(previous) != 1
            or previous[0]["urban_decision_recorded"]
            or previous[0]["status"] == "completed"
        ):
            raise ValueError("Retry violates the frozen infrastructure-only rule")
        records.append(
            {
                **attempt,
                "run_id": config["run_id"],
                "verified": verified,
                "verification_error": error,
                "result_sha256": digest_file(directory / "result.json"),
            }
        )
    completed = [r["verified"] for r in records if r["verified"]]
    if completed:
        for key in ("world_sha256", "sources_sha256", "image_id", "scenario_parameters"):
            if any(v[key] != completed[0][key] for v in completed):
                raise ValueError("Unmatched world/code/scenario: " + key)
    pairs = []
    for case in ("short_clear", "long_block", "brake_stop"):
        pair = {
            p: next((r for r in completed if r["case"] == case and r["policy"] == p), None)
            for p in ("onboard_stopping", "onboard_uncertainty")
        }
        base, candidate = pair.values()
        pairs.append(
            {
                "case": case,
                "baseline_verified": base is not None,
                "candidate_verified": candidate is not None,
                "baseline_action": base["action"] if base else None,
                "candidate_action": candidate["action"] if candidate else None,
                "baseline_urban_elapsed_s": base["urban_elapsed_s"] if base else None,
                "candidate_urban_elapsed_s": candidate["urban_elapsed_s"] if candidate else None,
                "candidate_minus_baseline_s": candidate["urban_elapsed_s"] - base["urban_elapsed_s"]
                if base and candidate
                else None,
                "candidate_differs_on_same_images": candidate["action"]
                != candidate["matched_image_rule_replay"]["onboard_stopping"]["action"]
                if candidate
                else None,
                "candidate_gate": candidate["matched_image_rule_replay"]["onboard_uncertainty"][
                    "uncertainty_gate"
                ]
                if candidate
                else None,
            }
        )
    return {
        "schema_version": "ship_onboard_uncertainty_comparison.v1",
        "protocol_sha256": digest_file(root / "protocol-freeze.json"),
        "attempt_count": len(records),
        "verified_flight_count": len(completed),
        "failures_retained": [r["directory"] for r in records if r["verified"] is None],
        "initial_gate_runtime_integration_passed": all(p["candidate_verified"] for p in pairs),
        "paired_matrix_complete": all(
            p["baseline_verified"] and p["candidate_verified"] for p in pairs
        ),
        "two_dynamic_route_cpu_efficacy_transferred": False,
        "step2_native_vla_wam_completed": False,
        "gpu_cost_usd": 0,
        "pairs": pairs,
        "attempts": records,
        "limits": [
            "Runtime adaptation in one visible obstacle and an approved static detour, not a direct transfer of the two-dynamic-route CPU panel.",
            "Five irregular observed frames and measured PX4 motion; geometric route-time estimate is not a calibrated flight-duration bound.",
            "Separate flights have different observations and resource contention; one flight per condition does not establish a timing advantage.",
            "Same-image rule replay is retrospective and does not execute the alternative trajectory.",
            "Known-colour mapped-plane perception, one aircraft, stationary simulated ship; no WAM/VLA inference or physical flight in this batch.",
        ],
    }


def replay_data(root, report, destination):
    data = []
    images = destination / "images"
    images.mkdir()
    for record in report["attempts"]:
        if record["verified"] is None:
            continue
        directory = root / record["directory"]
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        start = next(e["elapsed_s"] for e in events if e["event"] == "urban_entry_observed")
        rows = [
            json.loads(line) for line in (directory / "telemetry.jsonl").read_text().splitlines()
        ]
        points = []
        last_image = -10
        for row in rows:
            if row["elapsed_s"] < start or not row.get("vehicle"):
                continue
            t = row["elapsed_s"] - start
            point = {
                "t": round(t, 3),
                "vehicle": row["vehicle"]["xyz"],
                "payload": row["payload"]["xyz"] if row.get("payload") else None,
                "obstacle": row["urban_obstacle"]["xyz"] if row.get("urban_obstacle") else None,
            }
            frame = row.get("onboard_frame")
            if frame and t - last_image >= 3:
                source = directory / frame["file"]
                if digest_file(source) != frame["sha256"]:
                    raise ValueError("Replay image binding mismatch")
                image = images / (record["directory"] + "-" + Path(frame["file"]).name)
                shutil.copy2(source, image)
                point["image"] = "images/" + image.name
                last_image = t
            points.append(point)
        config = json.loads((directory / "config.json").read_text())
        data.append(
            {
                "case": record["case"],
                "policy": record["policy"],
                "action": record["action"],
                "points": points,
                "buildings": config["urban"]["buildings"],
                "events": [
                    {"t": round(e["elapsed_s"] - start, 3), "event": e["event"]}
                    for e in events
                    if e["elapsed_s"] >= start
                ],
            }
        )
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit_batch(args.batch_root)
    args.output_dir.mkdir(exist_ok=False)
    (args.output_dir / "comparison.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    replay = replay_data(args.batch_root, report, args.output_dir)
    (args.output_dir / "replay-data.json").write_text(
        json.dumps(replay, separators=(",", ":"), allow_nan=False)
    )
    template = Path(__file__).with_name("templates") / "onboard-uncertainty-replay.html"
    payload = json.dumps(
        {"runs": replay, "comparison": {k: v for k, v in report.items() if k != "attempts"}},
        ensure_ascii=False,
        allow_nan=False,
    ).replace("</", "<\\/")
    (args.output_dir / "report.html").write_text(
        template.read_text().replace("__REPLAY_DATA__", payload)
    )
    print(json.dumps({k: v for k, v in report.items() if k not in ("attempts", "pairs")}, indent=2))


if __name__ == "__main__":
    main()
