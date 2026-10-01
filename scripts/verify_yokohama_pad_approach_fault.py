#!/usr/bin/env python3
"""Reopen a lead-return fault run: the D3 model step must never be dispatched.

Passing means the injected reoccupation was refused before any D3 model or
delivery dispatch, and the owned simulation and model session were closed.
The flight itself is expected to fail; this is not a mission-success check.
"""

import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_pad_queue import clearance  # noqa: E402
from src.runtime.yokohama_payload import read_jsonl  # noqa: E402

REJECTIONS = (
    "Occupied approach; continuation revoked",
    "Pad reoccupied during reconfirmation",
    "Pad or approach not clear for a model approach step",
)


def verify(root):
    config = json.loads((root / "config.json").read_text())
    result = json.loads((root / "result.json").read_text())
    events = read_jsonl(root / "flight-events.jsonl")
    rows = read_jsonl(root / "flight-trajectory.jsonl")
    queue = config["world"]["pad_queue"]
    reason = result.get("reason") or result.get("observed", {}).get("reason") or ""

    def first(name, **fields):
        found = [
            e
            for e in events
            if e["event"] == name and all(e.get(k) == v for k, v in fields.items())
        ]
        return found[0] if found else None

    entry = first("pad_entry_authorized")
    wam_request = first("city_request", operation="wam", cycle=3)
    wam_response = first("city_response", operation="wam", cycle=3)
    trigger = first("fault_lead_return_triggered")
    reoccupied = next(
        (
            r
            for r in rows
            if trigger
            and r["wall_s"] >= trigger["wall_s"]
            and r.get("queue_lead")
            and not all(clearance(config, r)[k] for k in ("pad_clear", "approach_clear"))
        ),
        None,
    )
    inference_end = wam_response["wall_s"] if wam_response else float("inf")
    shutdown = root / "decisions" / "shutdown.json"
    d3_dispatch = [
        e
        for e in events
        if (e["event"] == "upload_receipt" and str(e.get("segment", "")).startswith("city-03"))
        or (
            e["event"] in ("city_permit_consumed", "city_upload_permit")
            and e["permit"]["cycle"] == 3
        )
        or e["event"] in ("pad_hold_moved", "pad_entry_reconfirmed")
        or (
            e["event"] == "city_response"
            and e.get("cycle") == 3
            and e["operation"] in ("authorize", "activate")
        )
        or (e["event"] == "city_segment_dispatched" and e["phase"] == "02-D3")
    ]
    checks = dict(
        fault_injected_at_d3_wam_request=queue.get("fault_lead_return") == "on_d3_wam_request"
        and config["decisions"]["backend"] == "fixture"
        and bool(entry and wam_request and trigger)
        # The trigger runs inside the same exchange, just before the request event.
        and entry["wall_s"] < trigger["wall_s"] <= wam_request["wall_s"]
        and wam_request["wall_s"] - trigger["wall_s"] < 1,
        reoccupation_observed_during_d3_inference=bool(reoccupied)
        and wam_request is not None
        and wam_request["wall_s"] < reoccupied["wall_s"] < inference_end,
        refused_for_reoccupation=result["status"] == "failed"
        and any(text in reason for text in REJECTIONS),
        no_d3_move_command=not d3_dispatch,
        no_delivery_dispatch=not any(
            e["event"] == "upload_receipt" and e["phase"] in ("03-DELIVERY", "PAYLOAD-LOW")
            for e in events
        ),
        session_closed=shutdown.exists()
        and json.loads(shutdown.read_text()).get("session_revoked") is True,
        simulator_and_supervisor_released=result.get("cleanup") is True
        and result.get("pad_supervisor_stopped") is True,
    )
    return dict(
        status="passed" if all(checks.values()) else "failed",
        checks=checks,
        run_id=config["run_id"],
        reason=reason,
        d3_wam_request_wall_s=wam_request and wam_request["wall_s"],
        fault_trigger_wall_s=trigger and trigger["wall_s"],
        reoccupied_observation_wall_s=reoccupied and reoccupied["wall_s"],
        d3_wam_response_wall_s=wam_response and wam_response["wall_s"],
        scope="CPU fixture fault injection; scripted lead return, pose-based occupancy",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    value = verify(a.run)
    a.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2))
    return 0 if value["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
