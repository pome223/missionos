"""CPU admission screen, never a flight planner, model adapter, or dispatch path.

Synthetic monotone clearance and an always-clear detour define a deliberately
bounded candidate-selection problem. Future schedules are evaluator-only.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
from statistics import mean


PROTOCOL = {
    "schema_version": "ship_wam_headroom_protocol.v1",
    "task": "candidate_selection_only",
    "primary_endpoint": "analytic_budget_feasibility_not_observed_delivery",
    "secondary_endpoint": "extra_seconds_on_jointly_feasible_cases",
    "incremental_gpu_budget_usd": 0,
    "clearance_x_m": 14.0,
    "sample_period_s": 0.5,
    "required_clear_samples": 2,
    "history_frames": 16,
    "history_period_s": 0.25,
    "airspeed_mps": 12.0,
    "detour_lateral_m": 85.0,
    "detour_turn_allowance_s": 8.0,
    "wait_timeout_s": 120.0,
    "candidates": {
        "detour": 0,
        "wait_2_then_detour": 2,
        "wait_8_then_detour": 8,
        "wait_20_then_detour": 20,
        "wait_60_then_detour": 60,
        "wait_120_then_detour": 120,
        "wait": None,
    },
    "policies": ["onboard_velocity", "onboard_stopping", "budget_detour_else_wait"],
    "cohorts": {
        "development": {
            "last_x_m": [2, 8],
            "last_velocity_mps": [0.15, 2.0],
            "past_acceleration_mps2": [0, -0.5],
            "slack_s": [4, 12, 22, 23, 45, 120],
            "release_speed_mps": 2.5,
            "pause_s": 12,
        },
        "evaluation": {
            "last_x_m": [4, 10],
            "last_velocity_mps": [0.35, 1.4],
            "past_acceleration_mps2": [0, -0.6],
            "slack_s": [6, 16, 22, 24, 60, 120],
            "release_speed_mps": 1.75,
            "pause_s": 18,
        },
    },
    "future_families": ["continue", "release_fast", "pause_resume", "persistent"],
    "latencies_s": [0, 1, 5, 10, 20, 21.265, 41.19],
    "native_two_candidate_latency_s": 41.19,
    "native_latency_basis": "rounded historical ANWM two-candidate processing; excludes load/network",
    "material_mean_time_gain_s": 5,
    "admission_rule": "native-latency oracle preserves every simple-rule success and improves success count or mean joint-feasible time by at least 5 seconds",
    "split_rule": "disjoint observed-state parameters, no fitting or post-result retuning",
    "best_fixed_selection_rule": "development cohort: maximize feasible count, then minimize time on feasible cases, then candidate name; apply unchanged to evaluation",
    "assumptions": [
        "Synthetic exact scalar observation history; no RGB perception or measured flight outcomes.",
        "One monotonically clearing obstacle; detour remains safe and available indefinitely.",
        "Slack is an abstract budget after common delivery/return cost, not calibrated battery endurance.",
        "Hold/direct/detour share the same start; lateral detour adds 2*85/12+8 seconds.",
        "Future motion may change immediately after the observed prefix; no intent cues are encoded.",
        "Latency blocks departure, with concurrent clearance monitoring and 1:1 wall/mission time.",
        "Late oracle gets perfect future knowledge; this is more optimistic than a 1-second image forecast.",
        "All budget-infeasible and persistent-blockage cases stay in the denominator.",
    ],
}


def digest(material):
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def freeze_protocol(path):
    """Create an immutable-by-convention protocol before any scoring; no overwrite."""
    receipt = {
        "schema_version": "ship_wam_headroom_freeze.v1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": digest(PROTOCOL),
        "protocol": PROTOCOL,
    }
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return receipt


def read_freeze(path):
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        set(receipt) != {"schema_version", "frozen_at_utc", "protocol_sha256", "protocol"}
        or receipt["schema_version"] != "ship_wam_headroom_freeze.v1"
        or receipt["protocol_sha256"] != digest(receipt["protocol"])
        or receipt["protocol"] != PROTOCOL
    ):
        raise ValueError("Frozen protocol changed or is unsupported")
    frozen_at = datetime.fromisoformat(receipt["frozen_at_utc"])
    if frozen_at.tzinfo is None or frozen_at > datetime.now(timezone.utc):
        raise ValueError("Invalid protocol freeze timestamp")
    return receipt


def detour_seconds():
    return (
        2 * PROTOCOL["detour_lateral_m"] / PROTOCOL["airspeed_mps"]
        + PROTOCOL["detour_turn_allowance_s"]
    )


def validate_online_input(observed):
    from src.runtime.ship_urban_decision import choose_urban_action

    if set(observed) != {"history", "slack_s"}:
        raise ValueError("Only observed history and mission slack enter the selector")
    if (
        type(observed["slack_s"]) not in (int, float)
        or not math.isfinite(observed["slack_s"])
        or observed["slack_s"] < 0
    ):
        raise ValueError("Invalid mission slack")
    choose_urban_action(observed["history"], "constant_velocity")
    if observed["history"][-1]["observed_at_s"] != 0:
        raise ValueError("History must end at the decision origin, with no future rows")


def select_candidate(observed, policy):
    """Online-shaped selector: no episode identifiers, future tape, or oracle."""
    validate_online_input(observed)
    if policy == "budget_detour_else_wait":
        return "detour" if detour_seconds() <= observed["slack_s"] else "wait"
    if policy not in PROTOCOL["policies"]:
        raise ValueError("Oracle is evaluator-only")
    from src.runtime.ship_onboard import choose_onboard_action

    return choose_onboard_action(observed["history"], policy)["action"]


def _cases():
    for split, grid in PROTOCOL["cohorts"].items():
        states = itertools.product(
            grid["last_x_m"],
            grid["last_velocity_mps"],
            grid["past_acceleration_mps2"],
            grid["slack_s"],
        )
        for state_index, (x, velocity, acceleration, slack) in enumerate(states):
            history = []
            for index in range(PROTOCOL["history_frames"]):
                t = (index - PROTOCOL["history_frames"] + 1) * PROTOCOL["history_period_s"]
                history.append(
                    {
                        "observed_at_s": t,
                        "obstacle_x_m": x + velocity * t + 0.5 * acceleration * t * t,
                    }
                )
            for family in PROTOCOL["future_families"]:
                # Private evaluator state, never passed to select_candidate.
                clear_at = None
                if family != "persistent":
                    speed = velocity if family == "continue" else grid["release_speed_mps"]
                    clear_at = (PROTOCOL["clearance_x_m"] - x) / speed
                    if family == "pause_resume":
                        clear_at += grid["pause_s"]
                yield {
                    "case_id": f"{split}-{state_index:03d}-{family}",
                    "split": split,
                    "online_input": {"history": history, "slack_s": slack},
                    "evaluator_only": {"future_family": family, "clear_at_s": clear_at},
                }


def candidate_outcome(candidate, clear_at_s, slack_s, latency_s=0):
    """Evaluate one bounded procedure; times are analytic, never telemetry."""
    if candidate not in PROTOCOL["candidates"]:
        raise ValueError("Unknown candidate")
    values = [slack_s, latency_s] + ([] if clear_at_s is None else [clear_at_s])
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Invalid evaluation time")
    confirmation = None
    if clear_at_s is not None:
        period = PROTOCOL["sample_period_s"]
        confirmation = math.ceil(clear_at_s / period) * period + period * (
            PROTOCOL["required_clear_samples"] - 1
        )
    cap = PROTOCOL["candidates"][candidate]
    extra = latency_s + detour_seconds()
    if candidate != "detour":
        limit = PROTOCOL["wait_timeout_s"] if cap is None else cap
        if confirmation is not None and confirmation <= latency_s + limit:
            extra = max(latency_s, confirmation)
        else:
            extra = None if cap is None else latency_s + cap + detour_seconds()
    feasible = extra is not None and extra <= slack_s
    return {
        "budget_feasible": feasible,
        "extra_s": extra,
        "reason": "within_budget"
        if feasible
        else ("clearance_timeout" if extra is None else "budget_exceeded"),
    }


def _best(table):
    return min(
        table,
        key=lambda name: (
            not table[name]["budget_feasible"],
            math.inf if table[name]["extra_s"] is None else table[name]["extra_s"],
            name,
        ),
    )


def _summary(outcomes):
    feasible = [row for row in outcomes if row["budget_feasible"]]
    return {
        "cases": len(outcomes),
        "feasible": len(feasible),
        "infeasible": len(outcomes) - len(feasible),
    }


def _paired(baseline, candidate):
    gains = [
        a["extra_s"] - b["extra_s"]
        for a, b in zip(baseline, candidate)
        if a["budget_feasible"] and b["budget_feasible"]
    ]
    return {
        "feasibility_gains": sum(
            not a["budget_feasible"] and b["budget_feasible"] for a, b in zip(baseline, candidate)
        ),
        "feasibility_losses": sum(
            a["budget_feasible"] and not b["budget_feasible"] for a, b in zip(baseline, candidate)
        ),
        "jointly_feasible": len(gains),
        "mean_time_gain_on_joint_success_s": mean(gains) if gains else None,
        "material_time_gains": sum(g >= PROTOCOL["material_mean_time_gain_s"] for g in gains),
    }


def run_screen(freeze):
    # Read and validate the receipt again before any candidate scoring.
    receipt = read_freeze(freeze)
    rows = []
    for case in _cases():
        observed = case["online_input"]
        choices = {p: select_candidate(observed, p) for p in PROTOCOL["policies"]}
        clear = case["evaluator_only"]["clear_at_s"]
        tables = {
            str(latency): {
                c: candidate_outcome(c, clear, observed["slack_s"], latency)
                for c in PROTOCOL["candidates"]
            }
            for latency in PROTOCOL["latencies_s"]
        }
        rows.append(
            {
                **case,
                "online_input_sha256": digest(observed),
                "policy_choices": choices,
                "candidate_tables_by_latency_s": tables,
                "oracle_choices_by_latency_s": {k: _best(t) for k, t in tables.items()},
            }
        )

    # Optimistic distribution-aware oracle, forced to pick the same procedure
    # for identical encoded inputs. Not a learned/online policy or RGB bound.
    groups = defaultdict(list)
    for row in rows:
        groups[row["online_input_sha256"]].append(row)
    for group in groups.values():
        grouped = min(
            PROTOCOL["candidates"],
            key=lambda c: (
                -sum(r["candidate_tables_by_latency_s"]["0"][c]["budget_feasible"] for r in group),
                sum(
                    r["candidate_tables_by_latency_s"]["0"][c]["extra_s"]
                    for r in group
                    if r["candidate_tables_by_latency_s"]["0"][c]["budget_feasible"]
                ),
                c,
            ),
        )
        for row in group:
            row["encoded_input_oracle_candidate"] = grouped

    development = [r for r in rows if r["split"] == "development"]
    best_fixed = min(
        PROTOCOL["candidates"],
        key=lambda c: (
            -sum(
                r["candidate_tables_by_latency_s"]["0"][c]["budget_feasible"] for r in development
            ),
            sum(
                r["candidate_tables_by_latency_s"]["0"][c]["extra_s"]
                for r in development
                if r["candidate_tables_by_latency_s"]["0"][c]["budget_feasible"]
            ),
            c,
        ),
    )
    summaries = {}
    for split in PROTOCOL["cohorts"]:
        cohort = [r for r in rows if r["split"] == split]
        series = {}
        for name in [*PROTOCOL["candidates"], *PROTOCOL["policies"], "encoded_input_oracle"]:
            series[name] = [
                r["candidate_tables_by_latency_s"]["0"][
                    r["encoded_input_oracle_candidate"]
                    if name == "encoded_input_oracle"
                    else r["policy_choices"].get(name, name)
                ]
                for r in cohort
            ]
        oracle_series = {
            str(latency): [
                r["candidate_tables_by_latency_s"][str(latency)][
                    r["oracle_choices_by_latency_s"][str(latency)]
                ]
                for r in cohort
            ]
            for latency in PROTOCOL["latencies_s"]
        }
        summaries[split] = {
            "cases": len(cohort),
            "encoded_input_groups": len({r["online_input_sha256"] for r in cohort}),
            "best_fixed_from_development": best_fixed,
            "procedures_and_policies": {k: _summary(v) for k, v in series.items()},
            "zero_latency_oracle_vs_each": {
                k: _paired(v, oracle_series["0"]) for k, v in series.items()
            },
            "latency_oracle": {
                k: {**_summary(v), **_paired(series["budget_detour_else_wait"], v)}
                for k, v in oracle_series.items()
            },
        }
    evaluation = summaries["evaluation"]
    native = evaluation["latency_oracle"][str(PROTOCOL["native_two_candidate_latency_s"])]
    admitted = native["feasibility_losses"] == 0 and (
        native["feasibility_gains"] > 0
        or (
            native["mean_time_gain_on_joint_success_s"] is not None
            and native["mean_time_gain_on_joint_success_s"] >= PROTOCOL["material_mean_time_gain_s"]
        )
    )
    return {
        "schema_version": "ship_wam_headroom_screen.v1",
        "status": "screened",
        "protocol_sha256": receipt["protocol_sha256"],
        "frozen_at_utc": receipt["frozen_at_utc"],
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
        "producer_source_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("ship_wam_headroom.py", "ship_onboard.py", "ship_urban_decision.py")
        },
        "detour_extra_s": detour_seconds(),
        "summaries": summaries,
        "screen_admits_native_wam_followup": admitted,
        "physical_execution_invoked": False,
        "px4_runtime_invoked": False,
        "gazebo_runtime_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "additional_gpu_cost_usd": 0,
        "model_adopted": False,
        "limitations": PROTOCOL["assumptions"],
        "cases": rows,
    }
