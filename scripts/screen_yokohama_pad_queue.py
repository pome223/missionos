"""Analytical design screen for an occupied delivery pad; no flight or models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def screen():
    # Authored assumptions, not measurements. The selector must never receive
    # these future clearance times. They are only the evaluation oracle.
    decision_s, wait_limit_s, alternate_travel_s = 70, 120, 90
    clear_observation_s, unload_s, delivery_deadline_s = 2, 30, 240
    splits = {"design": [100, 140, 300], "reserved_evaluation": [90, 170, 310]}
    groups = {}
    for split, clearance_times in splits.items():
        rows = []
        for index, clear_s in enumerate(clearance_times):
            wait_end = max(decision_s, clear_s + clear_observation_s)
            wait = wait_end + unload_s if wait_end <= decision_s + wait_limit_s else None
            alternate = decision_s + alternate_travel_s + unload_s
            values = {"wait_A": wait, "approved_alternate_B": alternate}
            passing = {
                k: v for k, v in values.items() if v is not None and v <= delivery_deadline_s
            }
            best = min(passing, key=passing.get)
            rows.append(
                dict(
                    case=f"{split}-{index + 1}",
                    initial_observation="A occupied",
                    clearance_truth_s=clear_s,
                    delivery_completion_s=values,
                    oracle_choice=best,
                    oracle_completion_s=passing[best],
                )
            )
        oracle_mean = sum(r["oracle_completion_s"] for r in rows) / len(rows)
        # Current-occupancy-only 'if busy, use B' and fixed B coincide here.
        groups[split] = dict(
            cases=rows,
            fixed_wait_deliveries=sum(
                r["delivery_completion_s"]["wait_A"] is not None for r in rows
            ),
            fixed_B_deliveries=len(rows),
            fixed_B_mean_s=alternate,
            current_occupancy_only_mean_s=alternate,
            oracle_mean_s=oracle_mean,
            oracle_headroom_s=alternate - oracle_mean,
        )
    return dict(
        schema_version="yokohama_pad_queue_design_screen.v1",
        evidence_kind="analytical_authored_scenarios",
        assumptions=dict(
            decision_latency_s=decision_s,
            wait_limit_s=wait_limit_s,
            alternate_travel_s=alternate_travel_s,
            fresh_clear_window_s=clear_observation_s,
            unload_s=unload_s,
            delivery_deadline_s=delivery_deadline_s,
        ),
        splits=groups,
        model_or_training_admitted=False,
        blockers=[
            "No observed clips showing that remaining occupancy is inferable",
            "Current VLA translation contract has no mission-level WAIT/ALTERNATE choice",
            "Current WAM target-view prediction has no validated long-horizon pad-clearance forecast",
            "Fleet queue ETA and tracked-departure comparators have not been evaluated",
        ],
        simulation_flights=0,
        model_calls=0,
        gpu_spend_usd=0,
        delivery_return_or_model_benefit_verified=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(screen(), stream, indent=2, allow_nan=False)
        stream.write("\n")
    print("Design screen written; no simulator, model or training was run.")


if __name__ == "__main__":
    main()
