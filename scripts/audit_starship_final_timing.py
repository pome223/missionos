#!/usr/bin/env python3
"""Audit immutable saved timestamps; never advance a plant or call a provider."""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path

MAXIMUM = 8 * 2**20
NAMES = ("plan.json", "started.json", "worker.claim.json", "worker-result.json", "verification.json")


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(path, expected=None):
    path = Path(path).absolute()
    require(path.resolve() == path and not path.is_symlink() and path.is_file()
        and 0 < path.stat().st_size <= MAXIMUM, "ordinary_bounded_saved_metadata_required")
    raw = path.read_bytes()
    require(expected is None or sha256(raw).hexdigest() == expected, "saved_metadata_hash_changed")
    def unique(items):
        result = dict(items)
        require(len(result) == len(items), "duplicate_saved_metadata_key")
        return result
    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: require(False, "nonfinite_saved_metadata"))
    return value, sha256(raw).hexdigest()


def audit(folder, manifest, manifest_sha256, plan_sha256, *, sleep_report=None, sleep_report_sha256=None):
    folder = Path(folder).absolute()
    require(folder.resolve() == folder and folder.is_dir(), "ordinary_saved_folder_required")
    index, _ = read(manifest, manifest_sha256)
    require(index.get("schema") == "missionos.private_live_T_tree_manifest.v2"
        and index.get("plan_sha256") == plan_sha256, "saved_manifest_binding")
    rows = {row["path"]: row for row in index["files"]}
    require(len(rows) == len(index["files"]), "duplicate_manifest_path")
    records, refs = {}, {}
    for name in NAMES:
        row = rows[name]
        require(row["kind"] == "file" and type(row["mtime_ns"]) is int, "timestamped_file_required")
        path = folder/name
        records[name], identity = read(path, row["sha256"])
        stamp = path.stat()
        require(stamp.st_mtime_ns == row["mtime_ns"] and stamp.st_size == row["bytes"], "saved_metadata_timestamp_changed")
        refs[name] = {"sha256": identity, "mtime_ns": row["mtime_ns"]}
    plan = records["plan.json"]
    canonical = json.dumps({k: v for k, v in plan.items() if k != "plan_sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    require(plan["plan_sha256"] == plan_sha256 == sha256(canonical).hexdigest()
        and all(records[n]["plan_sha256"] == plan_sha256 for n in NAMES if n != "plan.json"), "saved_plan_changed")
    start, end = (rows[n]["mtime_ns"] for n in ("worker.claim.json", "worker-result.json"))
    require(start <= end <= rows["verification.json"]["mtime_ns"], "saved_time_order")
    seconds = (end-start)/1e9
    maximum = plan["configuration"]["bounds"]["per_mission_wall_s"]
    require(type(maximum) in (int, float) and maximum > 0, "saved_time_bound_required")
    sleep = None
    if sleep_report is not None or sleep_report_sha256 is not None:
        require(sleep_report is not None and sleep_report_sha256 is not None, "paired_sleep_report_pin_required")
        supplied, identity = read(sleep_report, sleep_report_sha256)
        require(supplied["plan_sha256"] == plan_sha256, "sleep_report_plan_binding")
        review = supplied["time_budget_review"]
        sleep = {"report_sha256": identity, "os_recorded_sleep_duration_s": review["os_recorded_sleep_duration_s"],
            "events": review["os_sleep_events_in_mission_window"],
            "sleep_is_not_subtracted_for_registered_acceptance": True}
    return {"schema": "missionos.starship_final_saved_timing_audit.v1", "plan_sha256": plan_sha256,
        "run_id": plan["run_id"], "source_commit": plan["source_commit"],
        "preservation_manifest_sha256": manifest_sha256, "metadata": refs,
        "clock": "filesystem_UTC_mtime_ns_crosschecked_against_preserved_manifest",
        "interval": "worker_claim_to_serialized_worker_result",
        "start_utc": datetime.fromtimestamp(start/1e9, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end/1e9, timezone.utc).isoformat(),
        "elapsed_ns": end-start, "elapsed_s": seconds, "registered_maximum_s": maximum,
        "sleep_inclusive_registered_limit_met": seconds < maximum,
        "complete_monotonic_clock_pair_available": False,
        "independent_time_authentication_established": False, "sleep_evidence": sleep,
        "saved_verifier_passed_unchanged": records["verification.json"]["passed"],
        "this_audit_reruns_physical_verifier": False, "new_flights": 0, "model_calls": 0,
        "changes_original_evidence": False, "grants_execution_authority": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--sleep-report", type=Path)
    parser.add_argument("--sleep-report-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value = audit(args.folder, args.manifest, args.manifest_sha256, args.plan_sha256,
        sleep_report=args.sleep_report, sleep_report_sha256=args.sleep_report_sha256)
    require(args.output.absolute().resolve() == args.output.absolute(), "ordinary_new_output_required")
    raw = json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()+b"\n"
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
    print(json.dumps({"output_sha256": sha256(raw).hexdigest(), "elapsed_s": value["elapsed_s"],
        "registered_limit_met": value["sleep_inclusive_registered_limit_met"], "new_flights": 0, "model_calls": 0}))


if __name__ == "__main__":
    main()
