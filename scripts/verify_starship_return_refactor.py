"""Read-only exact regression audit of two source-bound 39-case censuses.

Only development_source_sha256 differs by design. All flight samples (including
commands), events, separated payloads, booster results and final states must be
identical. This is refactor regression evidence, never independent physics.
"""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_return_feasibility import sources  # noqa: E402


def compare_runs(previous, current):
    keys = set(previous) - {"development_source_sha256"}
    return {"same_fields": set(previous) == set(current),
            **{k: current.get(k) == previous[k] for k in sorted(keys)}}


def audit(previous_dir, current_dir, certificate):
    current_sources = sources()
    expected = {(n, 0) for n in range(27)} | {(n, delta) for n in (0, 14, 15, 16, 25, 26) for delta in (-1000, 1000)}
    cases = certificate["cases"]
    if len(cases) != 39 or {(r["retained_count"], r["initial_fuel_offset_kg"]) for r in cases} != expected:
        raise ValueError("complete_39_case_baseline_required")
    rows = []
    for row in cases:
        n, offset = row["retained_count"], int(row["initial_fuel_offset_kg"])
        name = f"retained{n}"+(f"-fuel{offset:+d}" if offset else "")
        old_raw, new_raw = (previous_dir/name/"study.json").read_bytes(), (current_dir/name/"study.json").read_bytes()
        result = json.loads((current_dir/name/"result.json").read_text())
        old_study, new_study = json.loads(old_raw), json.loads(new_raw)
        previous, current = old_study["runs"][0], new_study["runs"][0]
        checks = {"previous_study_bound": sha256(old_raw).hexdigest() == row["study_sha256"],
            "current_study_bound": sha256(new_raw).hexdigest() == result["study_sha256"],
            "current_case_bound": result["retained_count"] == n and result["initial_fuel_offset_kg"] == offset,
            "previous_source_bound": all(previous["development_source_sha256"].get(k) == v for k, v in certificate["source_sha256"].items()),
            "current_source_bound": all(current["development_source_sha256"].get(k) == v for k, v in current_sources.items()),
            "contact_gate_maintained": result["provisional_speed_and_reserve_gate_met"] is True,
            "profile_exact": old_study["profile"] == new_study["profile"],
            **compare_runs(previous, current)}
        rows.append({"case": name, "passed": all(checks.values()), "checks": checks,
            "sample_count": len(current["samples"]), "previous_study_sha256": row["study_sha256"],
            "current_study_sha256": result["study_sha256"]})
    return {"schema": "missionos.ship_return_refactor_regression.v1", "passed": all(r["passed"] for r in rows),
            "case_count": len(rows), "cases": rows, "current_source_sha256": current_sources,
            "old_record_relabeling": False, "independent_physics_validation": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-dir", type=Path, required=True)
    parser.add_argument("--current-dir", type=Path, required=True)
    parser.add_argument("--previous-certificate", type=Path, required=True)
    parser.add_argument("--previous-certificate-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("use a fresh audit output")
    raw = args.previous_certificate.read_bytes()
    if sha256(raw).hexdigest() != args.previous_certificate_sha256:
        parser.error("baseline certificate identity mismatch")
    result = audit(args.previous_dir, args.current_dir, json.loads(raw))
    result["previous_certificate_sha256"] = args.previous_certificate_sha256
    write_verified_input(args.output, result)
    print(json.dumps({k: result[k] for k in ("passed", "case_count")}))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
