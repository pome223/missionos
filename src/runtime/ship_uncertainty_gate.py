"""CPU-only uncertainty gate and causal re-observation experiment.

The selector receives observed scalar history. Evaluator-only worlds supply
new sensor observations; they never enter the proposal or admission functions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import gzip
import hashlib
import itertools
import json
import math
from pathlib import Path
from statistics import mean

from src.runtime.ship_dynamic_routes import (
    PROTOCOL as ROUTES,
    WINDOW_PROTOCOL,
    propagate,
    run_procedure,
    select_candidate,
    validate_online_input,
)
from src.runtime.ship_wam_headroom import digest


PROTOCOL = {
    "schema_version": "ship_uncertainty_protocol.v1",
    "prior_window_protocol_sha256": digest(WINDOW_PROTOCOL),
    "endpoint": "synthetic_guarded_completion_by_deadline",
    "incremental_gpu_cost_usd": 0,
    "decision_cost_s": 0.5,
    "reobserve_decisions_s": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
    "hold_departure_s": 2.5,
    "fit_rms_limit_m": 0.10,
    "alternate_time_reserve_s": 1.0,
    "brake_after_s": [0, 2, 4, 6, 8],
    "brake_decelerations_mps2": [0.3, 0.6],
    "model_set": "CA plus delayed brake-to-rest; include full-history CV only if its RMS also passes",
    "candidate_rule": "keep baseline unless nominal baseline misses deadline and every admitted alternate forecast succeeds with one-second reserve",
    "policies": ["baseline", "raw_ca", "patient_ca", "initial_gate", "shadow_gate", "hold_gate"],
    "regression": "all prior window development and evaluation states; unchanged initial data and world",
    "fresh_profiles_p_v_a": [
        [-25, 1.6, 0],
        [25, -1.6, 0],
        [22, 0, -0.26],
        [12, 1.7, -0.24],
        [12, 0.4, 0.2],
        [28, -1.25, 0.22],
    ],
    "fresh_deadlines_s": [31, 35, 37, 39, 41, 43, 45, 49, 61, 97],
    "fresh_noise_amplitudes_m": [0, 0.10, 0.25],
    "fresh_turn_times_s": [2.5, 5.5, 8],
    "fresh_turn_accelerations_mps2": [0.3, 0.6],
    "acceptance": "zero lost baseline successes in regression and fresh panels, with at least one fresh gain; separately compare shadow to initial gate",
    "claims": [
        "Synthetic CPU kinematics, not PX4/Gazebo, learned WAM/VLA, delivery, or physical flight.",
        "Thresholds fixed before scoring. Prior panels are regressions, fresh motion/timing states are unused; no learning or post-result tuning.",
        "Initial and subsequent planning history carry the same deterministic bounded sinusoidal noise; crossing guards use exact current range for all policies.",
        "Re-observation overlaps the existing direct approach. A detour pays for retreat of the already-travelled direct prefix.",
        "The hold control pays two extra seconds before departing, including when it falls back to direct.",
        "Fit residuals and enumerated braking hypotheses are heuristic uncertainty checks, not calibrated confidence or all-futures guarantees.",
        "Fresh future reversals can occur later than observation and differ from stop-and-hold forecasts. No future-change time or profile ID enters the selector.",
        "Timing, safe approach/retreat, speed limits, and abstract return cost inherit the prior CPU route model.",
    ],
}


def freeze_protocol(path):
    receipt = {
        "schema_version": "ship_uncertainty_freeze.v1",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": digest(PROTOCOL),
        "protocol": PROTOCOL,
    }
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return receipt


def read_freeze(path):
    r = json.loads(Path(path).read_text())
    if (
        r.get("schema_version") != "ship_uncertainty_freeze.v1"
        or r.get("protocol") != PROTOCOL
        or r.get("protocol_sha256") != digest(PROTOCOL)
    ):
        raise ValueError("Changed or unsupported frozen protocol")
    stamp = datetime.fromisoformat(r["frozen_at_utc"])
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("Invalid freeze timestamp")
    return r


def validate_observation(observed, decision_at_s):
    if set(observed) != {"history", "deadline_s", "observed_through_s"}:
        raise ValueError("Only observed history, deadline and observation time are allowed")
    validate_online_input({k: observed[k] for k in ("history", "deadline_s")})
    end = observed["observed_through_s"]
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (end, decision_at_s)
    ):
        raise ValueError("Invalid observation clock")
    if not math.isclose(end + PROTOCOL["decision_cost_s"], decision_at_s, abs_tol=1e-9):
        raise ValueError("Future or stale observation; processing time must be charged")


@lru_cache(maxsize=4096)
def _fit_rows(rows):
    import numpy as np

    t = np.array([r[0] for r in rows])
    fits = {}
    for index, route in enumerate(ROUTES["routes"], 1):
        y = np.array([r[index] for r in rows])
        fits[route] = {}
        for degree, name in ((1, "cv"), (2, "ca")):
            c = np.polynomial.polynomial.polyfit(t, y, degree)
            fitted = np.polynomial.polynomial.polyval(t, c)
            fits[route][name] = {
                "parameters": [float(c[0]), float(c[1]), float(2 * c[2]) if degree == 2 else 0.0],
                "rms_m": float(np.sqrt(np.mean((fitted - y) ** 2))),
            }
    return fits


def fit_history(observed):
    rows = tuple(
        (r["observed_at_s"], r["direct_x_m"], r["detour_x_m"]) for r in observed["history"]
    )
    return _fit_rows(rows)


def brake_position(parameters, future_s, brake_after_s, deceleration):
    """Continue estimated motion, then decelerate to rest and remain stopped."""
    if future_s <= brake_after_s:
        return propagate(*parameters, future_s)[0]
    position, velocity = propagate(*parameters, brake_after_s)
    braking_time = min(future_s - brake_after_s, abs(velocity) / deceleration)
    sign = 1 if velocity > 0 else -1 if velocity < 0 else 0
    return position + velocity * braking_time - sign * deceleration * braking_time**2 / 2


def assess_gate(observed, decision_at_s, direct_departure_s, retreat_s, *, patient_only=False):
    """Propose a route change from past data; never authorizes physical motion."""
    validate_observation(observed, decision_at_s)
    if (
        direct_departure_s < 0
        or direct_departure_s > decision_at_s
        or retreat_s < 0
        or retreat_s > ROUTES["routes"]["direct"]["approach_s"]
        or not math.isclose(retreat_s, decision_at_s - direct_departure_s, abs_tol=1e-9)
    ):
        raise ValueError("Invalid common-prefix progress or retreat cost")
    fits = fit_history(observed)
    origin = observed["observed_through_s"]
    deadline = observed["deadline_s"]
    event = {
        "at_s": decision_at_s,
        "observed_through_s": origin,
        "online_input_sha256": digest(observed),
        "retreat_s": retreat_s,
        "recommend_detour": False,
        "ca_rms_m": {r: fits[r]["ca"]["rms_m"] for r in fits},
    }
    if not patient_only and any(
        v > PROTOCOL["fit_rms_limit_m"] for v in event["ca_rms_m"].values()
    ):
        return {**event, "reason": "poor_motion_fit"}

    def nominal(route, stamp):
        if stamp < origin:
            raise ValueError("Forecast queried before its observation origin")
        return propagate(*fits[route]["ca"]["parameters"], stamp - origin)[0]

    base = run_procedure(nominal, "direct_wait", deadline, direct_departure_s)
    if base["success"]:
        return {**event, "reason": "baseline_predicted_feasible"}
    departure = decision_at_s + retreat_s
    reserve = 0 if patient_only else PROTOCOL["alternate_time_reserve_s"]
    alternate_deadline = max(0, deadline - reserve)
    alternate = run_procedure(nominal, "detour_wait", alternate_deadline, departure)
    if not alternate["success"]:
        return {**event, "reason": "nominal_alternate_infeasible"}
    predictions = [alternate["finish_s"]]
    if not patient_only:
        if fits["detour"]["cv"]["rms_m"] <= PROTOCOL["fit_rms_limit_m"]:

            def cv(route, stamp):
                return propagate(*fits[route]["cv"]["parameters"], stamp - origin)[0]

            outcome = run_procedure(cv, "detour_wait", alternate_deadline, departure)
            if not outcome["success"]:
                return {**event, "reason": "credible_velocity_model_disagrees"}
            predictions.append(outcome["finish_s"])
        for after, deceleration in itertools.product(
            PROTOCOL["brake_after_s"], PROTOCOL["brake_decelerations_mps2"]
        ):

            def braking(route, stamp):
                return brake_position(
                    fits[route]["ca"]["parameters"], stamp - origin, after, deceleration
                )

            outcome = run_procedure(braking, "detour_wait", alternate_deadline, departure)
            if not outcome["success"]:
                return {**event, "reason": "braking_hypothesis_disagrees"}
            predictions.append(outcome["finish_s"])
    return {
        **event,
        "recommend_detour": True,
        "reason": "alternate_passes_forecast_gate",
        "tested_hypotheses": len(predictions),
        "worst_predicted_finish_s": max(predictions),
    }


def run_controller(history_at, current_range, deadline_s, policy):
    """Execute a CPU fixture with causal sensing and charged retreat/hold time."""
    if policy not in PROTOCOL["policies"]:
        raise ValueError("Unknown controller; oracle is evaluator-only")
    start = PROTOCOL["decision_cost_s"]
    events = []
    if policy == "raw_ca":
        obs = history_at(0)
        validate_observation(obs, start)
        if obs["deadline_s"] != deadline_s:
            raise ValueError("Sensor history cannot change the mission deadline")
        candidate = select_candidate(
            {k: obs[k] for k in ("history", "deadline_s")}, "constant_acceleration"
        )
        result = run_procedure(current_range, candidate, deadline_s, start)
        return {**result, "candidate": candidate, "departure_s": start, "decisions": []}
    if policy == "baseline":
        result = run_procedure(current_range, "direct_wait", deadline_s, start)
        return {**result, "candidate": "direct_wait", "departure_s": start, "decisions": []}
    if policy == "hold_gate":
        start = PROTOCOL["hold_departure_s"]
        decisions = [start]
    elif policy == "shadow_gate":
        decisions = PROTOCOL["reobserve_decisions_s"]
    else:
        decisions = [start]
    for now in decisions:
        if now > deadline_s:
            break
        obs = history_at(now - PROTOCOL["decision_cost_s"])
        if obs["deadline_s"] != deadline_s:
            raise ValueError("Sensor history cannot change the mission deadline")
        event = assess_gate(obs, now, start, now - start, patient_only=policy == "patient_ca")
        events.append(event)
        if event["recommend_detour"]:
            departure = now + event["retreat_s"]
            result = run_procedure(current_range, "detour_wait", deadline_s, departure)
            return {
                **result,
                "candidate": "detour_wait",
                "departure_s": departure,
                "decisions": events,
            }
    result = run_procedure(current_range, "direct_wait", deadline_s, start)
    return {**result, "candidate": "direct_wait", "departure_s": start, "decisions": events}


def world_position(world, route, stamp):
    parameters = world["profiles"][route]
    if stamp < 0:
        x, v, a = parameters
        return x + v * stamp + a * stamp * stamp / 2
    change = world["turn_at_s"]
    if change is None or stamp <= change:
        return propagate(*parameters, stamp)[0]
    x, v = propagate(*parameters, change)
    a = world["turn_acceleration_mps2"] * (-1 if v > 0 else 1)
    return propagate(x, v, a, stamp - change)[0]


def observed_history(case, through_s):
    """Evaluator-owned sensor: returns only samples at/before through_s."""
    rows = []
    for index in range(ROUTES["history_frames"]):
        relative = (index - ROUTES["history_frames"] + 1) * ROUTES["history_sample_s"]
        stamp = through_s + relative
        row = {"observed_at_s": relative}
        for ri, route in enumerate(ROUTES["routes"]):
            phase = (15 + stamp / 0.25) * 1.7 + case["profile_indices"][route] * 0.37 + ri * 0.83
            row[f"{route}_x_m"] = world_position(case["evaluator_only"], route, stamp) + case[
                "noise_amplitude_m"
            ] * math.sin(phase)
        rows.append(row)
    return {"history": rows, "deadline_s": case["deadline_s"], "observed_through_s": through_s}


def generate_cases():
    for split, grid in WINDOW_PROTOCOL["cohorts"].items():
        profiles = grid["profiles_p_v_a"]
        variants = [(None, None), (4, 0.5)]
        yield from _grid_cases(
            f"regression_{split}", profiles, grid["deadlines_s"], [0, 0.25], variants
        )
    variants = [
        (None, None),
        *itertools.product(
            PROTOCOL["fresh_turn_times_s"], PROTOCOL["fresh_turn_accelerations_mps2"]
        ),
    ]
    yield from _grid_cases(
        "fresh_evaluation",
        PROTOCOL["fresh_profiles_p_v_a"],
        PROTOCOL["fresh_deadlines_s"],
        PROTOCOL["fresh_noise_amplitudes_m"],
        variants,
    )


def _grid_cases(cohort, profiles, deadlines, noise_levels, variants):
    states = itertools.product(range(len(profiles)), range(len(profiles)), deadlines, noise_levels)
    for number, (direct, detour, deadline, noise) in enumerate(states):
        for vi, (change, acceleration) in enumerate(variants):
            yield {
                "case_id": f"{cohort}-{number:04d}-{vi}",
                "cohort": cohort,
                "deadline_s": deadline,
                "noise_amplitude_m": noise,
                "profile_indices": {"direct": direct, "detour": detour},
                "evaluator_only": {
                    "profiles": {"direct": profiles[direct], "detour": profiles[detour]},
                    "turn_at_s": change,
                    "turn_acceleration_mps2": acceleration,
                },
            }


def paired(baseline, outcomes):
    gains = [
        a["finish_s"] - b["finish_s"]
        for a, b in zip(baseline, outcomes)
        if a["success"] and b["success"]
    ]
    return {
        "gained": sum(not a["success"] and b["success"] for a, b in zip(baseline, outcomes)),
        "lost": sum(a["success"] and not b["success"] for a, b in zip(baseline, outcomes)),
        "joint_successes": len(gains),
        "mean_time_gain_joint_s": mean(gains) if gains else None,
    }


def summarize(rows):
    summaries = {}
    for cohort in sorted({r["cohort"] for r in rows}):
        cases = [r for r in rows if r["cohort"] == cohort]
        strata = {"all": cases}
        for noise in sorted({r["noise_amplitude_m"] for r in cases}):
            for variant in ("continued", "changed"):
                strata[f"{variant}_noise_{noise}"] = [
                    r
                    for r in cases
                    if r["noise_amplitude_m"] == noise
                    and (r["evaluator_only"]["turn_at_s"] is None) == (variant == "continued")
                ]
        summaries[cohort] = {}
        for name, group in strata.items():
            base = [r["outcomes"]["baseline"] for r in group]
            values = {}
            for policy in [*PROTOCOL["policies"], "fixed_detour", "oracle"]:
                outputs = [r["outcomes"][policy] for r in group]
                values[policy] = {
                    "cases": len(outputs),
                    "successes": sum(v["success"] for v in outputs),
                    **paired(base, outputs),
                }
            summaries[cohort][name] = {
                "policies_vs_baseline": values,
                "shadow_vs_initial": paired(
                    [r["outcomes"]["initial_gate"] for r in group],
                    [r["outcomes"]["shadow_gate"] for r in group],
                ),
                "patient_vs_shadow": paired(
                    [r["outcomes"]["patient_ca"] for r in group],
                    [r["outcomes"]["shadow_gate"] for r in group],
                ),
            }
    return summaries


def run_screen(protocol_path, output_path):
    """Write compressed full evidence; return a compact CLI receipt."""
    frozen = read_freeze(protocol_path)
    path = Path(output_path)
    if path.resolve() == Path(protocol_path).resolve() or path.suffix != ".gz":
        raise ValueError("Evidence output must be a separate .json.gz path")
    if path.exists():
        raise FileExistsError("Evidence output already exists")
    rows = []
    for case in generate_cases():

        def history_at(stamp):
            return observed_history(case, stamp)

        def current_range(route, stamp):
            return world_position(case["evaluator_only"], route, stamp)

        outcomes = {
            p: run_controller(history_at, current_range, case["deadline_s"], p)
            for p in PROTOCOL["policies"]
        }
        alternative = run_procedure(
            current_range, "detour_wait", case["deadline_s"], PROTOCOL["decision_cost_s"]
        )
        outcomes["fixed_detour"] = alternative
        outcomes["oracle"] = min(
            (outcomes["baseline"], alternative),
            key=lambda v: (not v["success"], v["finish_s"] if v["success"] else math.inf),
        )
        assert all(not v["success"] or outcomes["oracle"]["success"] for v in outcomes.values())
        rows.append(
            {**case, "initial_online_input_sha256": digest(history_at(0)), "outcomes": outcomes}
        )
    summaries = summarize(rows)
    shadow = [s["all"]["policies_vs_baseline"]["shadow_gate"] for s in summaries.values()]
    result = {
        "schema_version": "ship_uncertainty_screen.v1",
        "status": "screened",
        "protocol_sha256": frozen["protocol_sha256"],
        "frozen_at_utc": frozen["frozen_at_utc"],
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {
            p: hashlib.sha256(Path(__file__).with_name(p).read_bytes()).hexdigest()
            for p in ("ship_uncertainty_gate.py", "ship_dynamic_routes.py", "ship_wam_headroom.py")
        },
        "cpu_acceptance_passed": all(s["lost"] == 0 for s in shadow)
        and summaries["fresh_evaluation"]["all"]["policies_vs_baseline"]["shadow_gate"]["gained"]
        > 0,
        "physical_execution_invoked": False,
        "px4_runtime_invoked": False,
        "gazebo_runtime_invoked": False,
        "wam_invoked": False,
        "vla_invoked": False,
        "model_adopted": False,
        "additional_gpu_cost_usd": 0,
        "summaries": summaries,
        "cases": rows,
    }
    with path.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    return {k: v for k, v in result.items() if k != "cases"} | {
        "case_count": len(rows),
        "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
