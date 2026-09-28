"""Reopen measured AP recovery and fresh decision receipts; no model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.yokohama_decision_worker import digest, physical_heading, require_city_request  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def verify(root):
    config = read(root / "config.json")
    assert config["decisions"]["backend"] == "fixture"
    policy = config["decisions"]["hold_recovery"]
    radius = policy.get("maximum_anchor_distance_m", 1)
    if "mapped_envelopes" in policy:
        from src.runtime.yokohama_native import recovery_envelopes

        assert policy["mapped_envelopes"] == recovery_envelopes(
            config,
            Path(__file__).resolve().parents[1] / "docs/examples/yokohama-urban-scene",
            radius,
        )
    else:
        assert radius == 1  # Original development trial had the fixed 1 m cap.
    events = [json.loads(line) for line in (root / "flight-events.jsonl").read_text().splitlines()]
    rows = [
        json.loads(line) for line in (root / "flight-trajectory.jsonl").read_text().splitlines()
    ]
    from scripts.verify_yokohama_decisions import verify as verify_decisions

    decision_verification = verify_decisions(root)
    verified_cycles = {c["cycle"]: c for c in decision_verification["cycles"]}
    requests = []
    for path in sorted((root / "decisions").glob("*-request.json")):
        request = read(path)
        response = read(path.with_name(path.name.replace("-request", "-response")))
        assert response["request_sha256"] == digest(request)
        assert request["run_id"] == config["run_id"] and request["config_sha256"] == digest(config)
        require_city_request(config, request)
        requests.append((request, response))
    revoked = [e for e in events if e["event"] == "city_attempt_revoked"]
    consumed = [e["permit"] for e in events if e["event"] == "city_permit_consumed"]
    assert all(
        not any(p["cycle"] == e["cycle"] and p.get("attempt") == e["attempt"] for e in revoked)
        for p in consumed
    )
    details = []
    for settled in [e for e in events if e["event"] == "city_recovery_held"]:
        cycle, attempt = settled["cycle"], settled["attempt"]
        start = next(
            e
            for e in events
            if e["event"] == "city_recovery_started"
            and e["cycle"] == cycle
            and e["attempt"] == attempt
        )
        assert any(
            e["cycle"] == cycle and e["attempt"] == attempt and e["wall_s"] <= start["wall_s"]
            for e in revoked
        )
        anchor, final = start["anchor"], settled["observation"]
        require_city_request(config, dict(operation="resume", cycle=cycle, observation=anchor))
        window = [r for r in rows if start["wall_s"] < r["wall_s"] <= final["wall_s"]]
        assert len(window) >= 3
        assert all(0 <= b["sim_s"] - a["sim_s"] <= 2 for a, b in zip(window, window[1:]))
        assert all(
            r["nav_state"] == 4
            and r["arming_state"] == 2
            and not r["landed"]
            and r["position_valid"]
            and r["battery_fraction"] >= 0.2
            and r["reset_counters"] == anchor["reset_counters"]
            and math.dist(r["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) <= radius
            for r in window
        )
        stable = [r for r in window if r["sim_s"] >= settled["stable_since_sim_s"]]
        assert stable[-1]["sim_s"] - stable[0]["sim_s"] >= 5
        assert all(
            math.hypot(*r["velocity_ned"]) <= 0.3
            and math.dist(r["vehicle"]["xyz"], anchor["vehicle"]["xyz"]) <= 0.5
            and abs(math.remainder(r["heading_ned_rad"] - anchor["heading_ned_rad"], 2 * math.pi))
            <= 0.03
            and abs(math.remainder(physical_heading(r) - physical_heading(anchor), 2 * math.pi))
            <= 0.03
            for r in stable
        )
        following = [
            (r, s) for r, s in requests if r["cycle"] == cycle and r.get("attempt") == attempt + 1
        ]
        fresh = {}
        for operation in ("vla", "wam"):
            matches = [
                (r, s) for r, s in following if r["operation"] == operation and "error" not in s
            ]
            if matches:
                r, response = matches[0]
                capture = root / r["capture"]["file"]
                assert hashlib.sha256(capture.read_bytes()).hexdigest() == r["capture"]["sha256"]
                first = read(capture)["frames"][0]["stamp_ns"] / 1e9
                assert first > final["sim_s"]
                fresh[operation] = dict(first_frame_sim_s=first, response_sha256=digest(response))
        permits = [p for p in consumed if p["cycle"] == cycle and p.get("attempt") == attempt + 1]
        arrivals = [
            e
            for e in events
            if e["event"] == "city_segment_arrived"
            and any(p["permit_id"] == e["permit_id"] for p in permits)
        ]
        details.append(
            dict(
                cycle=cycle,
                revoked_attempt=attempt,
                stable_duration_sim_s=stable[-1]["sim_s"] - stable[0]["sim_s"],
                recovery_end_sim_s=final["sim_s"],
                fresh_calls=fresh,
                observed_arrival=bool(arrivals),
                independently_reopened_decision=verified_cycles.get(cycle),
                complete=len(fresh) == 2 and bool(arrivals) and cycle in verified_cycles,
            )
        )
    stop = any(e["event"] == "city_response" and e["operation"] == "stop" for e in events)
    result = read(root / "result.json")
    checks = dict(
        recovery_observed=bool(details),
        fresh_decision_and_arrival=any(d["complete"] for d in details),
        no_revoked_permit_consumed=True,
        city_request_gates=True,
        shutdown_acknowledged=stop,
        host_cleanup=result.get("cleanup") is True
        and result.get("model_shutdown", {}).get("session_revoked") is True,
    )
    return dict(
        schema_version="yokohama_hold_recovery_verification.v1",
        run_id=config["run_id"],
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        recoveries=details,
        whole_delivery_verified=False,
        native_models_invoked=False,
        source_sha256={
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "config.json",
                "flight-events.jsonl",
                "flight-trajectory.jsonl",
                "result.json",
            )
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.run_dir)
    except (AssertionError, KeyError, ValueError, OSError, StopIteration) as exc:
        result = dict(status="failed", error=type(exc).__name__ + ": " + str(exc))
    (args.run_dir / "recovery-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
