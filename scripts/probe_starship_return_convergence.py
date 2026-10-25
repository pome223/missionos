#!/usr/bin/env python3
"""Opt-in, same-origin return sensitivity; no qualification or runtime changes.

One fresh output per declared case. Coupled cases change control/event cadence
as well as numerical steps. RK4 cases subdivide the existing solver's stages
while retaining the configured control/event cadence, held command and absolute
clock. This is diagnostic numerical refinement, not independent flight physics.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime import starship_physics as env, starship_sixdof as dyn  # noqa: E402
from src.runtime.starship_artifacts import write_verified_input  # noqa: E402
from src.runtime.starship_replanning_verifier import metrics, site_distance  # noqa: E402
from src.runtime.starship_return_prediction import forecast, validate_origin  # noqa: E402
from src.runtime.starship_return_feasibility import backend  # noqa: E402
from src.runtime.starship_ship_return import ReturnController, control_interval  # noqa: E402

CASES = {
    "baseline": (1., 1), "coupled_half": (.5, 1), "coupled_quarter": (.25, 1),
    "rk4_half": (1., 2), "rk4_quarter": (1., 4),
}
LIMITS = {"wall_time_s": 600., "maximum_output_bytes": 200_000_000,
          "minimum_free_bytes": 5 * 1024**3, "maximum_control_ticks": 200_000,
          "maximum_deorbit_trace_rows": 20_000}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_hashes():
    names = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/"src/runtime").glob("starship*.py"))
    names.append("scripts/probe_starship_return_convergence.py")
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def fixed_profile(profile, case):
    result = deepcopy(profile)
    scale, _ = CASES[case]
    for key in ("powered_dt_s", "coast_dt_s"):
        result["integration"][key] *= scale
    return result


def alignment_deg(state):
    up = env.unit(state.r_eci_m)
    tangent = env.add(state.v_eci_mps, env.scale(up, -env.dot(state.v_eci_mps, up)))
    cosine = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), env.scale(env.unit(tangent), -1.))
    return math.degrees(math.acos(max(-1., min(1., cosine))))


@contextmanager
def diagnostic_hooks(subdivisions, trace):
    """Serial process only; restore all hooks even after timeout/failure.

    The solver retains its stability-based outer subdivision, fuel event search
    and contact bisection. No control command is recomputed within RK4 substeps.
    A fuel-zero crossing inside a substep is an unsupported diagnostic outcome,
    explicitly failed instead of changing the solver's fuel event semantics.
    """
    if type(subdivisions) is not int or subdivisions not in (1, 2, 4):
        raise ValueError("unsupported_rk4_subdivision")
    original_rk4, original_update, original_command = dyn._rk4, ReturnController.update, ReturnController.command
    trace.update(rk4_calls=0, rk4_substeps=0, command_ticks=0, phase_extrema={},
                 deorbit_trace=[], transitions=[], command_clock_sha256=None)
    clock_hash = sha256()

    def refined(y, vehicle, command, available, dt, gravity, j2, atmosphere,
                force, torque, gradient, time_s=0.):
        trace["rk4_calls"] += 1
        out = y
        for index in range(subdivisions):
            out = original_rk4(out, vehicle, command, available, dt/subdivisions,
                gravity, j2, atmosphere, force, torque, gradient, time_s+index*dt/subdivisions)
            trace["rk4_substeps"] += 1
            if subdivisions > 1 and index < subdivisions-1 and out[13] <= 0:
                raise ValueError("diagnostic_subdivision_crossed_fuel_zero")
        return out

    def observed_update(controller, state, vehicle, profile, samples, event):
        before = controller.phase
        result = original_update(controller, state, vehicle, profile, samples, event)
        if before != controller.phase:
            trace["transitions"].append({"time_s": state.time_s, "from": before,
                "to": controller.phase, "propellant_kg": state.propellant_kg,
                "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
                "q_body_to_eci": list(state.q_body_to_eci),
                "omega_body_rad_s": list(state.omega_body_rad_s),
                "retrograde_alignment_deg": alignment_deg(state)})
        return result

    def observed_command(controller, step, vehicle, profile, altitude):
        command, diagnostics = original_command(controller, step, vehicle, profile, altitude)
        trace["command_ticks"] += 1
        if trace["command_ticks"] > LIMITS["maximum_control_ticks"]:
            raise ValueError("diagnostic_control_tick_limit")
        interval = control_interval(profile, step.throttle, altitude)
        clock_hash.update(json.dumps([step.state.time_s, controller.phase, interval], separators=(",", ":")).encode())
        rate = env.norm(step.state.omega_body_rad_s)
        extrema = trace["phase_extrema"].setdefault(controller.phase,
            {"ticks": 0, "maximum_body_rate_rad_s": 0., "minimum_propellant_kg": step.state.propellant_kg})
        extrema["ticks"] += 1
        extrema["maximum_body_rate_rad_s"] = max(extrema["maximum_body_rate_rad_s"], rate)
        extrema["minimum_propellant_kg"] = min(extrema["minimum_propellant_kg"], step.state.propellant_kg)
        if controller.phase in ("deorbit_slew", "deorbit_burn"):
            if len(trace["deorbit_trace"]) >= LIMITS["maximum_deorbit_trace_rows"]:
                raise ValueError("diagnostic_deorbit_trace_limit")
            trace["deorbit_trace"].append({"time_s": step.state.time_s, "phase": controller.phase,
                "control_interval_s": interval, "commanded_main_throttle": step.throttle,
                "retrograde_alignment_deg": alignment_deg(step.state),
                "actual_main_throttle": [s.throttle for s in step.state.engine_states[:3]],
                "propellant_kg": step.state.propellant_kg})
        return command, diagnostics

    dyn._rk4, ReturnController.update, ReturnController.command = refined, observed_update, observed_command
    try:
        yield
    finally:
        dyn._rk4, ReturnController.update, ReturnController.command = original_rk4, original_update, original_command
        trace["command_clock_sha256"] = clock_hash.hexdigest()


def diagnostics(prediction, trace):
    m = metrics(prediction)
    receipt = prediction["outcome"]["contact_receipt"]
    contact = None if receipt is None else {
        "point_eci_m": receipt["point_eci_m"],
        "point_ecef_m": list(env.eci_to_ecef(receipt["point_eci_m"], receipt["event_time_s"])),
        "event_time_s": receipt["event_time_s"], "metrics": m,
    }
    burn = [s for s in trace["deorbit_trace"] if s["phase"] == "deorbit_burn"]
    return {"contact": contact, "events": prediction["events"],
        "deorbit_alignment_gate_off_commanded_s": sum(s["control_interval_s"] for s in burn if s["commanded_main_throttle"] == 0),
        "deorbit_maximum_alignment_deg": max((s["retrograde_alignment_deg"] for s in burn), default=None),
        "gate_duration_semantics": "sum of held outer command intervals while phase is deorbit_burn; not exact continuous crossing time",
        "trace": trace}


def compare(reference, prediction):
    keys = ("origin", "return_time_s", "opportunity_window", "events", "samples", "final_state", "outcome", "return_controller")
    exact = {key: reference.get(key) == prediction.get(key) for key in keys}
    a, b = metrics(reference), metrics(prediction)
    delta = None
    if a is not None and b is not None:
        ca, cb = reference["outcome"]["contact_receipt"], prediction["outcome"]["contact_receipt"]
        ae = env.eci_to_ecef(ca["point_eci_m"], ca["event_time_s"])
        be = env.eci_to_ecef(cb["point_eci_m"], cb["event_time_s"])
        delta = {"surface_distance_m": site_distance(a, b),
            "eci_chord_m": math.dist(ca["point_eci_m"], cb["point_eci_m"]),
            "ecef_chord_m": math.dist(ae, be),
            "same_point_rotation_only_chord_m": math.dist(ae, env.eci_to_ecef(ca["point_eci_m"], cb["event_time_s"])),
            **{key: b[key]-a[key] for key in ("time_s", "speed_mps", "tilt_deg", "reserve_kg")}}
    return {"physical_exact_fields": exact, "physical_exact": all(exact.values()),
            "contact_difference": delta, "reference_metrics": a, "case_metrics": b}


def write_bounded(path, value, output):
    encoded = (json.dumps(value, indent=2, allow_nan=False)+"\n").encode("utf-8")
    size = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    if size+len(encoded) > LIMITS["maximum_output_bytes"]:
        raise ValueError("diagnostic_output_budget_exceeded")
    return write_verified_input(path, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-simulation", action="store_true")
    parser.add_argument("--reference-prediction", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT/"examples/spaceflight/starship-sixdof-profile.json")
    parser.add_argument("--expected-reference-sha256", required=True)
    parser.add_argument("--expected-profile-sha256", required=True)
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.approve_simulation or args.output_dir.exists():
        parser.error("explicit opt-in and a fresh output directory are required")
    ancestor = args.output_dir.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < LIMITS["minimum_free_bytes"]:
        parser.error("diagnostic_minimum_free_space_unavailable")
    reference_bytes, profile_bytes = args.reference_prediction.read_bytes(), args.profile.read_bytes()
    if (sha256(reference_bytes).hexdigest() != args.expected_reference_sha256
            or sha256(profile_bytes).hexdigest() != args.expected_profile_sha256):
        parser.error("diagnostic_frozen_input_hash_mismatch")
    reference, profile = json.loads(reference_bytes), json.loads(profile_bytes)
    p = fixed_profile(profile, args.case)
    origin, when, window, duration = (reference["origin"], reference["return_time_s"],
        reference.get("opportunity_window"), reference["horizon_s"])
    validate_origin(origin, p, when, duration, window=window)
    before = source_hashes()
    request = {"schema": "missionos.return_convergence_request.v1", "case": args.case,
        "origin": origin, "origin_sha256": digest(origin), "profile": p, "base_profile": profile,
        "reference_prediction_sha256": sha256(reference_bytes).hexdigest(),
        "profile_file_sha256": sha256(profile_bytes).hexdigest(), "source_sha256": before,
        "backend": backend(), "return_time_s": when, "window": window, "duration_s": duration,
        "outer_interval_scale": CASES[args.case][0], "rk4_subdivisions": CASES[args.case][1],
        "limits": LIMITS, "model_calls": 0, "automatic_retry": False,
        "runtime_admission": False, "physics_limits_changed": False}
    args.output_dir.mkdir(parents=True)
    request = write_bounded(args.output_dir/"inputs.json", request, args.output_dir)
    for name in before:
        target = args.output_dir/"source"/name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (ROOT/name).read_bytes()
        occupied = sum(p.stat().st_size for p in args.output_dir.rglob("*") if p.is_file())
        if occupied+len(raw) > LIMITS["maximum_output_bytes"]:
            write_verified_input(args.output_dir/"failure.json", {"type": "ValueError", "detail": "diagnostic_source_snapshot_budget_exceeded"})
            raise ValueError("diagnostic_source_snapshot_budget_exceeded")
        target.write_bytes(raw)
    started, trace = time.monotonic(), {}
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0] or old_timer[1]:
        raise ValueError("diagnostic_requires_unowned_alarm_timer")

    def expired(_signum, _frame):
        raise TimeoutError("diagnostic_wall_time_budget_exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, LIMITS["wall_time_s"])
    try:
        with diagnostic_hooks(request["rk4_subdivisions"], trace):
            prediction = forecast(origin, p, return_time_s=when, duration_s=duration, window=window)
        prediction = write_bounded(args.output_dir/"prediction.json", prediction, args.output_dir)
        comparison = compare(reference, prediction)
        evidence = diagnostics(prediction, trace)
        write_bounded(args.output_dir/"diagnostics.json", evidence, args.output_dir)
        unchanged = before == source_hashes()
        valid = unchanged and prediction["outcome"]["termination"] != "numerical_failure"
        if args.case == "baseline":
            valid = valid and comparison["physical_exact"]
        result = {"schema": "missionos.return_convergence_case.v1", "case": args.case,
            "record_valid": valid, "source_unchanged": unchanged, "origin_sha256": digest(origin),
            "source_sha256": before, "backend": backend(), "comparison": comparison,
            "termination": prediction["outcome"]["termination"],
            "prediction_sha256": sha256((args.output_dir/"prediction.json").read_bytes()).hexdigest(),
            "diagnostics_sha256": sha256((args.output_dir/"diagnostics.json").read_bytes()).hexdigest(),
            "wall_time_s": time.monotonic()-started, "model_calls": 0,
            "numerical_convergence_established": False, "runtime_admission": False,
            "independent_physics_validation": False,
            "interpretation": "coupled cases vary control/event intervals too; RK4 cases keep their configured intervals, not threshold crossing times"}
        write_bounded(args.output_dir/"result.json", result, args.output_dir)
        print(json.dumps({key: result[key] for key in ("case", "record_valid", "termination", "comparison", "wall_time_s")}))
        return 0 if valid else 2
    except Exception as error:
        # Evidence from failed attempts is never replaced or retried here.
        write_verified_input(args.output_dir/"failure.json", {"type": type(error).__name__,
            "detail": str(error), "wall_time_s": time.monotonic()-started,
            "source_unchanged": before == source_hashes(),
            "partial_trace_counts": {key: trace.get(key) for key in ("command_ticks", "rk4_calls", "rk4_substeps")}})
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.)
        signal.signal(signal.SIGALRM, old_handler)


if __name__ == "__main__":
    raise SystemExit(main())
