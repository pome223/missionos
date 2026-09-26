"""Synthetic two-route CPU experiment. No flight, model, or dispatch boundary.

Selectors see scalar history only. The evaluator supplies future observations
to identical guarded procedures, including wait and one bounded route switch.
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

from src.runtime.ship_wam_headroom import digest


PROTOCOL = {
    "schema_version": "ship_dynamic_routes_protocol.v1",
    "endpoint": "synthetic_guarded_completion_within_deadline",
    "incremental_gpu_budget_usd": 0,
    "routes": {
        "direct": {"approach_s": 6, "after_crossing_s": 17},
        "detour": {"approach_s": 12, "after_crossing_s": 23},
    },
    "sample_s": 0.5,
    "crossing_s": 1,
    "obstacle_half_width_m": 10,
    "clearance_margin_m": 1,
    "speed_bound_mps": 4,
    "history_frames": 16,
    "history_sample_s": 0.25,
    "candidates": {
        "direct_wait": {"route": "direct", "switch_after_s": None},
        "detour_wait": {"route": "detour", "switch_after_s": None},
        "direct_switch4": {"route": "direct", "switch_after_s": 4},
        "detour_switch4": {"route": "detour", "switch_after_s": 4},
        "direct_switch8": {"route": "direct", "switch_after_s": 8},
        "detour_switch8": {"route": "detour", "switch_after_s": 8},
    },
    "policies": [
        "budget_detour_else_direct",
        "current_clearance",
        "reactive_switch4",
        "constant_velocity",
        "constant_acceleration",
    ],
    "baseline_decision_latency_s": 0.5,
    "latencies_s": [0, 0.5, 1, 5, 10, 21.5, 41.5],
    "native_latency_s": 41.5,
    "native_latency_basis": "historical 41.19s two-candidate ANWM processing rounded up to 0.5s clock; no fresh inference",
    "future_variants": ["continued_motion", "unannounced_turn"],
    "hidden_change_at_s": 4,
    "hidden_change_acceleration_mps2": 0.5,
    "noise_amplitudes_m": [0, 0.25],
    "noise_definition": "deterministic sin(index*1.7 + profile_index*0.37 + route_index*0.83); identical across future variants",
    "cohorts": {
        "development": {
            "profiles_p_v_a": [
                [-18, 2, 0],
                [18, -2, 0],
                [18, 0, -0.3],
                [8, 2, -0.3],
                [8, 0.3, 0.25],
                [24, -1, 0.2],
            ],
            "deadlines_s": [28, 40, 60, 90],
        },
        "evaluation": {
            "profiles_p_v_a": [
                [-20, 1.6, 0],
                [20, -1.6, 0],
                [20, 0, -0.25],
                [10, 1.6, -0.25],
                [10, 0.4, 0.2],
                [26, -1.2, 0.25],
            ],
            "deadlines_s": [30, 44, 64, 96],
        },
    },
    "selection_rule": "maximize success count then minimize successful completion time then lexical name; fixed and best-simple choices use development only",
    "admission_rule": "native-latency full-future oracle must preserve every best-simple success and add at least 5 percentage points on evaluation; actual model contract still required",
    "claims": [
        "CPU kinematics and exact scalar history plus declared synthetic noise, not PX4/Gazebo/RGB or physical execution.",
        "Both routes may be blocked; entry, staging, and retreat to the branch are always safe.",
        "Before crossing, two fresh clear samples satisfy half_width+margin+speed_bound*crossing_time.",
        "All procedures use identical causal guards; no future data enter guard authorization.",
        "Remaining route time includes abstract delivery and safe common return, not measured battery or deck recovery.",
        "Continued-motion futures obey bounded constant acceleration; CA is deliberately a strong simple comparator.",
        "Unannounced-turn twins have identical past observations and no encoded intent cue; full oracle gaps here are not learnable value.",
        "All failures are retained. Grid conditions are not independent trials or learned-model held-out performance.",
        "Decision latency blocks departure, with mission time advancing 1:1; asynchronous forecasts are out of scope.",
    ],
}


WINDOW_PROTOCOL = {
    **PROTOCOL,
    "schema_version": "ship_dynamic_routes_protocol.v2",
    "panel_reason": "Prospective window sweep after sparse panel showed fixed direct_wait saturated success; preserve sparse panel and use new motion parameters",
    "cohorts": {
        "development": {
            "profiles_p_v_a": [
                [-22, 1.8, 0],
                [22, -1.8, 0],
                [19, 0, -0.22],
                [9, 1.8, -0.24],
                [9, 0.35, 0.22],
                [25, -1.1, 0.22],
            ],
            "deadlines_s": list(range(26, 51, 2)) + [60, 96],
        },
        "evaluation": {
            "profiles_p_v_a": [
                [-24, 1.5, 0],
                [24, -1.5, 0],
                [21, 0, -0.27],
                [11, 1.5, -0.22],
                [11, 0.45, 0.18],
                [27, -1.3, 0.24],
            ],
            "deadlines_s": list(range(27, 52, 2)) + [61, 97],
        },
    },
}
PANELS = {"sparse": PROTOCOL, "windows": WINDOW_PROTOCOL}


def freeze_protocol(path, panel="sparse"):
    if panel not in PANELS:
        raise ValueError("Unknown panel")
    protocol = PANELS[panel]
    receipt = {
        "schema_version": "ship_dynamic_routes_freeze.v1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": digest(protocol),
        "protocol": protocol,
    }
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return receipt


def read_freeze(path):
    receipt = json.loads(Path(path).read_text())
    if (
        receipt.get("schema_version") != "ship_dynamic_routes_freeze.v1"
        or receipt.get("protocol") not in PANELS.values()
        or receipt.get("protocol_sha256") != digest(receipt.get("protocol"))
    ):
        raise ValueError("Changed or unsupported frozen protocol")
    stamp = datetime.fromisoformat(receipt["frozen_at_utc"])
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("Invalid freeze timestamp")
    return receipt


def propagate(position, velocity, acceleration, seconds):
    """Integrate constant acceleration with a hard, continuous velocity cap."""
    if seconds < 0:
        raise ValueError("Future integration requires nonnegative time")
    bound = PROTOCOL["speed_bound_mps"]
    velocity = max(-bound, min(bound, velocity))
    cap_time = math.inf
    if acceleration:
        cap = bound if acceleration > 0 else -bound
        cap_time = max(0, (cap - velocity) / acceleration)
    first = min(seconds, cap_time)
    end_v = max(-bound, min(bound, velocity + acceleration * first))
    return (
        position + velocity * first + acceleration * first * first / 2 + end_v * (seconds - first),
        end_v,
    )


def world_position(world, route, stamp):
    position, velocity, acceleration = world["profiles"][route]
    change = PROTOCOL["hidden_change_at_s"]
    if world["future_variant"] == "continued_motion" or stamp <= change:
        return propagate(position, velocity, acceleration, stamp)[0]
    position, velocity = propagate(position, velocity, acceleration, change)
    acceleration = PROTOCOL["hidden_change_acceleration_mps2"] * (-1 if velocity > 0 else 1)
    return propagate(position, velocity, acceleration, stamp - change)[0]


def guard_threshold():
    return (
        PROTOCOL["obstacle_half_width_m"]
        + PROTOCOL["clearance_margin_m"]
        + PROTOCOL["speed_bound_mps"] * PROTOCOL["crossing_s"]
    )


def run_procedure(observe, candidate, deadline_s, latency_s, *, trace=False):
    """One causal execution: only current/past gate samples authorize crossing.

    The callback is the evaluator's fresh range sensor or a planner's forecast.
    After crossing, the remaining delivery/return duration is an explicit fixture.
    """
    if candidate not in PROTOCOL["candidates"]:
        raise ValueError("Unknown procedure")
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or v < 0
        for v in (deadline_s, latency_s)
    ):
        raise ValueError("Invalid timing")
    spec = PROTOCOL["candidates"][candidate]
    route, switch_after = spec["route"], spec["switch_after_s"]
    now, switched, wait = latency_s, False, 0.0
    events = []

    def failed():
        result = {
            "success": False,
            "finish_s": None,
            "cross_at_s": None,
            "route": route,
            "switched": switched,
            "gate_wait_s": wait,
            "guard_lower_bound_m": None,
        }
        if trace:
            result["events"] = events + [
                {"event": "deadline_infeasible", "at_s": min(now, deadline_s), "route": route}
            ]
        return result

    while True:
        geometry = PROTOCOL["routes"][route]
        if (
            now + geometry["approach_s"] + PROTOCOL["crossing_s"] + geometry["after_crossing_s"]
            > deadline_s
        ):
            return failed()
        events.append({"event": "depart", "at_s": now, "route": route})
        now += geometry["approach_s"]
        arrived = now
        events.append({"event": "gate_arrival", "at_s": now, "route": route})
        while now + PROTOCOL["crossing_s"] + geometry["after_crossing_s"] <= deadline_s:
            samples = [observe(route, now - PROTOCOL["sample_s"]), observe(route, now)]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in samples):
                raise ValueError("Invalid gate observation")
            if all(abs(v) + 1e-9 >= guard_threshold() for v in samples):
                finish = now + PROTOCOL["crossing_s"] + geometry["after_crossing_s"]
                events.extend(
                    [
                        {"event": "cross", "at_s": now, "route": route},
                        {"event": "synthetic_completion", "at_s": finish, "route": route},
                    ]
                )
                result = {
                    "success": True,
                    "finish_s": finish,
                    "cross_at_s": now,
                    "route": route,
                    "switched": switched,
                    "gate_wait_s": wait,
                    "guard_lower_bound_m": abs(samples[-1])
                    - PROTOCOL["speed_bound_mps"] * PROTOCOL["crossing_s"]
                    - PROTOCOL["obstacle_half_width_m"],
                }
                if trace:
                    result["events"] = events
                return result
            if switch_after is not None and not switched and now - arrived >= switch_after:
                other = "detour" if route == "direct" else "direct"
                other_geometry = PROTOCOL["routes"][other]
                if (
                    now
                    + geometry["approach_s"]
                    + other_geometry["approach_s"]
                    + PROTOCOL["crossing_s"]
                    + other_geometry["after_crossing_s"]
                    > deadline_s
                ):
                    return failed()
                events.append({"event": "retreat_to_branch", "at_s": now, "route": route})
                now += geometry["approach_s"]  # charge the full return to the branch
                route = other
                switched = True
                events.append({"event": "branch_reached", "at_s": now, "route": route})
                break
            now += PROTOCOL["sample_s"]
            wait += PROTOCOL["sample_s"]
        else:
            return failed()


def validate_online_input(observed):
    if set(observed) != {"history", "deadline_s"}:
        raise ValueError("Only observed history and deadline enter the selector")
    deadline = observed["deadline_s"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("Invalid deadline")
    history = observed["history"]
    if len(history) != PROTOCOL["history_frames"]:
        raise ValueError("Expected sixteen observations")
    for index, row in enumerate(history):
        if set(row) != {"observed_at_s", "direct_x_m", "detour_x_m"} or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in row.values()
        ):
            raise ValueError("Only finite observed time/positions belong in history")
        if row["observed_at_s"] != (index - len(history) + 1) * PROTOCOL["history_sample_s"]:
            raise ValueError("History must end at zero with no future or missing frames")


def forecast_parameters(observed, policy):
    """Least-squares motion estimates, not learned model inference."""
    import numpy as np

    validate_online_input(observed)
    if policy not in ("constant_velocity", "constant_acceleration"):
        raise ValueError("Unknown forecast")
    history = observed["history"] if policy == "constant_acceleration" else observed["history"][-4:]
    degree = 2 if policy == "constant_acceleration" else 1
    params = {}
    for route in PROTOCOL["routes"]:
        coefficients = np.polynomial.polynomial.polyfit(
            [r["observed_at_s"] for r in history],
            [r[f"{route}_x_m"] for r in history],
            degree,
        )
        params[route] = [
            float(coefficients[0]),
            float(coefficients[1]),
            float(2 * coefficients[2]) if degree == 2 else 0.0,
        ]
    return params


def _best(table):
    return min(
        table,
        key=lambda name: (
            not table[name]["success"],
            table[name]["finish_s"] if table[name]["success"] else math.inf,
            name,
        ),
    )


def select_candidate(observed, policy):
    validate_online_input(observed)
    if policy not in PROTOCOL["policies"]:
        raise ValueError("Oracle is evaluator-only")
    latency = PROTOCOL["baseline_decision_latency_s"]
    if policy == "budget_detour_else_direct":
        geometry = PROTOCOL["routes"]["detour"]
        duration = (
            latency + geometry["approach_s"] + PROTOCOL["crossing_s"] + geometry["after_crossing_s"]
        )
        return "detour_wait" if duration <= observed["deadline_s"] else "direct_wait"
    if policy in ("current_clearance", "reactive_switch4"):
        last = observed["history"][-1]
        route = min(
            PROTOCOL["routes"],
            key=lambda r: (
                abs(last[f"{r}_x_m"]) < guard_threshold(),
                PROTOCOL["routes"][r]["approach_s"],
            ),
        )
        return f"{route}_switch4" if policy == "reactive_switch4" else f"{route}_wait"
    params = forecast_parameters(observed, policy)

    def observe(route, stamp):
        return propagate(*params[route], stamp)[0]

    predicted = {
        c: run_procedure(observe, c, observed["deadline_s"], latency)
        for c in PROTOCOL["candidates"]
    }
    return _best(predicted)


def generate_cases(protocol=PROTOCOL):
    for split, grid in protocol["cohorts"].items():
        profiles = grid["profiles_p_v_a"]
        states = itertools.product(
            range(len(profiles)),
            range(len(profiles)),
            grid["deadlines_s"],
            PROTOCOL["noise_amplitudes_m"],
        )
        for number, (direct, detour, deadline, noise) in enumerate(states):
            history = []
            for index in range(PROTOCOL["history_frames"]):
                t = (index - PROTOCOL["history_frames"] + 1) * PROTOCOL["history_sample_s"]
                row = {"observed_at_s": t}
                for ri, (route, pi) in enumerate((("direct", direct), ("detour", detour))):
                    position, velocity, acceleration = profiles[pi]
                    row[f"{route}_x_m"] = (
                        position
                        + velocity * t
                        + acceleration * t * t / 2
                        + noise * math.sin(index * 1.7 + pi * 0.37 + ri * 0.83)
                    )
                history.append(row)
            for variant in PROTOCOL["future_variants"]:
                yield {
                    "case_id": f"{split}-{number:03d}-{variant}",
                    "split": split,
                    "noise_amplitude_m": noise,
                    "online_input": {"history": history, "deadline_s": deadline},
                    "evaluator_only": {
                        "profiles": {"direct": profiles[direct], "detour": profiles[detour]},
                        "future_variant": variant,
                    },
                }


def _aggregate(outcomes):
    successes = [r for r in outcomes if r["success"]]
    return {
        "cases": len(outcomes),
        "successes": len(successes),
        "failures": len(outcomes) - len(successes),
        "mean_completion_on_success_s": mean(r["finish_s"] for r in successes)
        if successes
        else None,
    }


def _paired(a, b):
    gains = [x["finish_s"] - y["finish_s"] for x, y in zip(a, b) if x["success"] and y["success"]]
    return {
        "gained": sum(not x["success"] and y["success"] for x, y in zip(a, b)),
        "lost": sum(x["success"] and not y["success"] for x, y in zip(a, b)),
        "joint_successes": len(gains),
        "mean_time_gain_joint_s": mean(gains) if gains else None,
    }


def run_screen(path):
    frozen = read_freeze(path)
    protocol = frozen["protocol"]
    rows, choices_cache = [], {}
    base = str(PROTOCOL["baseline_decision_latency_s"])
    for case in generate_cases(protocol):
        observed = case["online_input"]
        key = digest(observed)
        if key not in choices_cache:
            choices_cache[key] = {p: select_candidate(observed, p) for p in PROTOCOL["policies"]}

        def observe(route, stamp):
            return world_position(case["evaluator_only"], route, stamp)

        tables = {
            str(latency): {
                c: run_procedure(observe, c, observed["deadline_s"], latency)
                for c in PROTOCOL["candidates"]
            }
            for latency in PROTOCOL["latencies_s"]
        }
        rows.append(
            {
                **case,
                "online_input_sha256": key,
                "policy_choices": choices_cache[key],
                "outcomes_by_latency_s": tables,
                "oracle_choices_by_latency_s": {k: _best(v) for k, v in tables.items()},
            }
        )

    def outcomes(cohort, name, latency=base):
        return [
            r["outcomes_by_latency_s"][latency][
                r["oracle_choices_by_latency_s"][latency]
                if name == "full_oracle"
                else r["input_oracle_candidate"]
                if name == "input_oracle"
                else r["policy_choices"].get(name, name)
            ]
            for r in cohort
        ]

    def rank(cohort, name):
        selected = outcomes(cohort, name)
        return (
            -sum(r["success"] for r in selected),
            sum(r["finish_s"] for r in selected if r["success"]),
            name,
        )

    groups = defaultdict(list)
    for row in rows:
        groups[row["online_input_sha256"]].append(row)
    for group in groups.values():
        selected = min(PROTOCOL["candidates"], key=lambda c: rank(group, c))
        for row in group:
            row["input_oracle_candidate"] = selected
    development = [r for r in rows if r["split"] == "development"]
    best_fixed = min(PROTOCOL["candidates"], key=lambda c: rank(development, c))
    best_simple = min(PROTOCOL["policies"], key=lambda p: rank(development, p))
    best_comparator = min((best_fixed, best_simple), key=lambda p: rank(development, p))
    summaries = {}
    for split in protocol["cohorts"]:
        full = [r for r in rows if r["split"] == split]
        strata = {"all": full}
        for variant in PROTOCOL["future_variants"]:
            for noise in PROTOCOL["noise_amplitudes_m"]:
                strata[f"{variant}_noise_{noise}"] = [
                    r
                    for r in full
                    if r["evaluator_only"]["future_variant"] == variant
                    and r["noise_amplitude_m"] == noise
                ]
        summaries[split] = {}
        for name, cohort in strata.items():
            series = {
                p: outcomes(cohort, p)
                for p in [
                    *PROTOCOL["candidates"],
                    *PROTOCOL["policies"],
                    "full_oracle",
                    "input_oracle",
                ]
            }
            summaries[split][name] = {
                "procedures_and_policies": {p: _aggregate(v) for p, v in series.items()},
                "vs_current_clearance": {
                    p: _paired(series["current_clearance"], v) for p, v in series.items()
                },
                "oracle_vs_best_simple": _paired(series[best_simple], series["full_oracle"]),
                "oracle_vs_best_comparator": _paired(
                    series[best_comparator], series["full_oracle"]
                ),
                "latency_oracle_vs_best_comparator": {
                    str(t): {
                        **_aggregate(outcomes(cohort, "full_oracle", str(t))),
                        **_paired(series[best_comparator], outcomes(cohort, "full_oracle", str(t))),
                    }
                    for t in PROTOCOL["latencies_s"]
                },
                "latency_oracle_vs_best_simple": {
                    str(t): {
                        **_aggregate(outcomes(cohort, "full_oracle", str(t))),
                        **_paired(series[best_simple], outcomes(cohort, "full_oracle", str(t))),
                    }
                    for t in PROTOCOL["latencies_s"]
                },
            }
    native = summaries["evaluation"]["all"]["latency_oracle_vs_best_comparator"][
        str(PROTOCOL["native_latency_s"])
    ]
    return {
        "schema_version": "ship_dynamic_routes_screen.v1",
        "status": "screened",
        "protocol_sha256": frozen["protocol_sha256"],
        "frozen_at_utc": frozen["frozen_at_utc"],
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
        "producer_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "best_fixed_from_development": best_fixed,
        "best_simple_from_development": best_simple,
        "best_comparator_from_development": best_comparator,
        "native_latency_headroom_gate_passed": native["lost"] == 0
        and native["gained"] / native["cases"] >= 0.05,
        "gpu_followup_admitted": False,
        "model_contract_qualified": False,
        "physical_execution_invoked": False,
        "px4_runtime_invoked": False,
        "gazebo_runtime_invoked": False,
        "wam_invoked": False,
        "vla_invoked": False,
        "additional_gpu_cost_usd": 0,
        "limitations": PROTOCOL["claims"],
        "summaries": summaries,
        "cases": rows,
    }
