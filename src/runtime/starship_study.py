"""Frozen, opt-in 3D engineering study and offline evidence verification.

Public broadcast readings are diagnostic references, not precision telemetry.
This module never promotes a consistent numerical run to real-vehicle validation.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from uuid import uuid4

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
)

from .runtime_claim_evidence import validate_runtime_invocation_evidence
from .starship_flight import FlightProfile, SCENARIOS, simulate_flight
from .starship_physics import Control, State3D, Vehicle, observe, step

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = ROOT / "examples/spaceflight/starship-flight14-observations.json"
GOAL = {
    "scope": "sampled_point_mass_orbit_and_contact_diagnostic_only",
    "payload_count": 26,
    "minimum_payload_perigee_m": 180_000.0,
    "maximum_payload_apogee_m": 450_000.0,
    "minimum_payload_tracking_s": 120.0,
    "maximum_contact_ground_speed_mps": 5.0,
    "surface_tolerance_m": 0.1,
    "limits_are_engineering_assumptions_not_spacex_requirements": True,
}
CLAIMS = {
    "model": "3d_translation_with_prescribed_attitude",
    "six_dof": False,
    "public_broadcast_comparison": "diagnostic_only",
    "calibrated_aerodynamic_database": False,
    "tps_survival_validated": False,
    "starship_vehicle_validated": False,
    "physical_execution_invoked": False,
    "llm_invoked": False,
    "gateway_integration_verified": False,
    "starlink_service_verified": False,
}

SOURCES = [
    {
        "title": "SpaceX Starship Flight 14",
        "url": "https://www.spacex.com/launches/starship-flight-14",
        "classification": "official mission narrative; not raw telemetry",
    },
    {
        "title": "SpaceX Starship V3",
        "url": "https://www.spacex.com/updates#starship-v3",
        "classification": "published design specifications, 2026-05-12",
    },
    {
        "title": "Official Flight 14 broadcast",
        "url": "https://x.com/i/broadcasts/1qxvvenMPMQxB",
        "classification": "manual screen readings; unknown sensor frame and uncertainty",
    },
    {
        "title": "Starlink V3 satellites",
        "url": "https://starlink.com/ls/updates/starlink-version-3-satellites",
        "classification": "published satellite design; service not modeled",
    },
    {
        "title": "NGA WGS84",
        "url": "https://earth-info.nga.mil/?action=wgs84&dir=wgs84",
        "classification": "reference Earth geometry",
    },
    {
        "title": "U.S. Standard Atmosphere 1976",
        "url": "https://ntrs.nasa.gov/citations/19770009539",
        "classification": "layered atmosphere to 86 km; upper atmosphere is an assumed surrogate",
    },
]


def parameter_provenance(profile: dict) -> list[dict]:
    """Public design numbers do not establish actual loaded mass or flight controls."""
    public_design = {"sea_level_thrust_n", "vacuum_thrust_n"}
    loaded = {"booster_propellant_kg", "ship_propellant_kg"}
    rows = []
    for field, value in profile.items():
        classification = "engineering assumption; not actual SpaceX telemetry or control software"
        source = None
        if field in public_design:
            classification = (
                "published V3 rated thrust; modeled as constant, actual thrust history unknown"
            )
            source = SOURCES[1]["url"]
        elif field in loaded:
            classification = (
                "published tank capacity used as assumed loaded propellant; actual load unknown"
            )
            source = SOURCES[1]["url"]
        elif field == "satellite_count":
            classification = (
                "official Flight 14 reported release count; modeled identifiers are synthetic"
            )
            source = SOURCES[0]["url"]
        elif field in ("suborbital_target_altitude_m", "orbit_insertion_perigee_m"):
            classification = "engineering target informed by public ~275 km display; not an independently validated orbit"
            source = SOURCES[2]["url"]
        if profile.get("profile_id") == "starship-flight14-v11-calibration-v1" and field in (
            "ascent_altitude_response_s",
            "orbit_gate_delay_s",
        ):
            classification = "selected using V11 altitude and visible burning at T+1551 s; calibration input, not validation"
            source = SOURCES[2]["url"]
        rows.append(
            {"field": field, "value": value, "classification": classification, "source_url": source}
        )
    return rows


def digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_hashes() -> dict[str, str]:
    paths = [
        "src/runtime/starship_physics.py",
        "src/runtime/starship_flight.py",
        "src/runtime/starship_study.py",
        "scripts/run_starship_3d.py",
        "examples/spaceflight/starship-flight14-observations.json",
    ]
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}


def make_contract(request: dict) -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id="starship-3d-research",
        contract_version="1",
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput("request", "frozen_simulation_request", digest(request)),
            ReferenceInput("goal", "engineering_acceptance_limits", digest(GOAL)),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id="integrated_states",
                evidence_kind="three_dimensional_point_mass_trace",
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=60.0,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.ENUMERATED_MEMBERS,
            member_ids=("ship", "booster", *(f"sim-starlink-v3-{i:02d}" for i in range(1, 27))),
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id="bounded_orbit_and_contact",
            statement="Modeled payload orbital states and modeled Ship surface speed meet fixed diagnostic limits.",
            claim_scope=GOAL["scope"],
        ),
        predicate_package=PredicatePackageBinding(
            package_id="starship_study",
            package_version="1",
            content_sha256=digest({"goal": GOAL, "source": request["source_sha256"]}),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
            maximum_duration_seconds=86_400.0,
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def make_request(
    scenarios: list[str], profile: FlightProfile, dt_s: float, sample_interval_s: float
) -> dict:
    if (
        not scenarios
        or len(set(scenarios)) != len(scenarios)
        or any(s not in SCENARIOS for s in scenarios)
    ):
        raise ValueError("Choose unique supported scenarios.")
    profile.validate()
    if profile.satellite_count != GOAL["payload_count"]:
        raise ValueError("This frozen study requires 26 modeled payloads.")
    if max(profile.max_mission_duration_s, profile.max_ascent_duration_s) > 86_400:
        raise ValueError("Requested duration exceeds the one-day local study contract.")
    if isinstance(dt_s, bool) or not math.isfinite(dt_s) or not 0.05 <= dt_s <= 2.0:
        raise ValueError("dt_s must be finite and between 0.05 and 2 seconds.")
    if (
        isinstance(sample_interval_s, bool)
        or not math.isfinite(sample_interval_s)
        or not dt_s <= sample_interval_s <= 60.0
    ):
        raise ValueError("sample interval must lie between dt_s and 60 seconds.")
    return {
        "schema": "missionos.starship_3d_request.v1",
        "scope": "local_opt_in_three_dof_engineering_simulation",
        "scenarios": list(scenarios),
        "profile": asdict(profile),
        "dt_s": dt_s,
        "sample_interval_s": sample_interval_s,
        "source_sha256": source_hashes(),
        "goal": GOAL,
        "claim_boundary": CLAIMS,
        "public_reference_sha256": digest(json.loads(OBSERVATIONS.read_text())),
    }


def _state(material: dict) -> State3D:
    return State3D(
        time_s=material["time_s"],
        r=tuple(material["r"]),
        v=tuple(material["v"]),
        propellant_kg=material["propellant_kg"],
    )


def _material_from_sample(s: dict) -> dict:
    return {
        "time_s": s["time_s"],
        "r": [s[f"{a}_m"] for a in "xyz"],
        "v": [s[f"v{a}_mps"] for a in "xyz"],
        "propellant_kg": s["propellant_mass_kg"],
    }


def _distance(a: dict, b: dict) -> tuple[float, float, float, float]:
    return (
        abs(a["time_s"] - b["time_s"]),
        math.dist(a["r"], b["r"]),
        math.dist(a["v"], b["v"]),
        abs(a["propellant_kg"] - b["propellant_kg"]),
    )


def _same(a: dict, b: dict) -> bool:
    return all(x <= tol for x, tol in zip(_distance(a, b), (1e-7, 1e-4, 1e-6, 1e-6)))


def _finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(v) for v in value)
    return True


def _orbit(s: dict) -> dict | None:
    """Independent osculating two-body elements, equatorial-radius altitude reference."""
    r = [s[f"{a}_m"] for a in "xyz"]
    v = [s[f"v{a}_mps"] for a in "xyz"]
    radius = math.sqrt(sum(x * x for x in r))
    mu, a_earth = 3.986004418e14, 6_378_137.0
    energy = sum(x * x for x in v) / 2 - mu / radius
    if energy >= 0:
        return None
    h = [r[1] * v[2] - r[2] * v[1], r[2] * v[0] - r[0] * v[2], r[0] * v[1] - r[1] * v[0]]
    e = math.sqrt(max(0.0, 1 + 2 * energy * sum(x * x for x in h) / mu**2))
    sma = -mu / (2 * energy)
    return {"perigee_m": sma * (1 - e) - a_earth, "apogee_m": sma * (1 + e) - a_earth}


def compare_observations(run: dict, reference: dict) -> dict:
    trace = run["traces"]["ship"]
    times = [s["time_s"] for s in trace]
    rows = []
    for observation in reference["observations"]:
        row = dict(observation)
        row["source_url"] = reference["source_url"]
        row["speed_basis"] = reference["speed_basis"]
        t = row["time_s"]
        i = bisect_left(times, t)
        row["covered"] = bool(times and times[0] <= t <= times[-1])
        if row["covered"]:
            left, right = max(0, i - 1), min(i, len(times) - 1)
            w = (
                0.0
                if times[right] == times[left]
                else (t - times[left]) / (times[right] - times[left])
            )

            def value(key):
                return trace[left][key] * (1 - w) + trace[right][key] * w

            row["model_bracket_s"] = [times[left], times[right]]
            row["model_altitude_m"] = value("altitude_m")
            row["model_ground_speed_mps"] = value("speed_mps")
            row["model_inertial_speed_mps"] = value("inertial_speed_mps")
            if "altitude_m" in row:
                row["altitude_difference_m"] = row["model_altitude_m"] - row["altitude_m"]
            if "speed_kmh" in row:
                row["observed_display_speed_mps"] = row["speed_kmh"] / 3.6
                row["ground_speed_difference_mps"] = (
                    row["model_ground_speed_mps"] - row["observed_display_speed_mps"]
                )
                row["inertial_speed_difference_mps"] = (
                    row["model_inertial_speed_mps"] - row["observed_display_speed_mps"]
                )
        rows.append(row)
    altitude = [r for r in rows if "altitude_m" in r and r["time_s"] >= 0]
    covered = [r for r in altitude if r["covered"]]
    entry = [r for r in altitude if 10_457 <= r["time_s"] <= 11_309]
    covered_entry = [r for r in entry if r["covered"]]
    return {
        "scenario": run["scenario"],
        "rows": rows,
        "reference_altitude_points": len(altitude),
        "covered_altitude_points": len(covered),
        "missing_altitude_point_ids": [r["id"] for r in altitude if not r["covered"]],
        "altitude_mean_absolute_difference_m": (
            sum(abs(r["altitude_difference_m"]) for r in covered) / len(covered)
            if covered
            else None
        ),
        "accuracy_verified": False,
        "calibration_scope": (
            "V11 altitude and visible burning used to select this profile from eight candidates. Entry points V17-V25 were not used in that selection. Orbital altitude target was already informed by public observations."
            if run["profile"]["profile_id"] == "starship-flight14-v11-calibration-v1"
            else "Public ~275 km display informed the design goal. This is not a blind real-flight validation."
        ),
        "entry_reference_points": len(entry),
        "entry_covered_points": len(covered_entry),
        "entry_mean_absolute_difference_m": (
            sum(abs(r["altitude_difference_m"]) for r in covered_entry) / len(covered_entry)
            if covered_entry
            else None
        ),
        "scope": "Same overlay time, interpolated simulated samples; unknown broadcast datum/frame/latency. No extrapolation or actual-vehicle validation.",
    }


def verify_flight(run: dict, request: dict, *, recompute_steps: bool = True) -> dict:
    """Derive numerical consistency and endpoints from raw states, never saved outcome flags."""
    checks: dict[str, bool] = {}
    metrics: dict = {}
    try:
        checks["finite_records"] = _finite(run)
        checks["profile_binding"] = run["profile"] == request["profile"]
        checks["scenario_binding"] = run["scenario"] in request["scenarios"]
        traces = run["traces"]
        checks["ship_trace_present"] = len(traces["ship"]) >= 2
        step_records = run["step_records"]
        maxima = [0.0, 0.0, 0.0, 0.0]
        count = 0
        transitions = run.get("state_transitions", [])
        receipts = run["release_receipts"]
        # The recorded controls are not themselves authority to change the
        # physics or policy. Replay the frozen runner as well as each numerical
        # step, binding initial conditions, transitions, engine/vehicle choices,
        # release receipts, gate decisions, telemetry and termination to source.
        expected_run = simulate_flight(
            FlightProfile(**request["profile"]),
            scenario=run["scenario"],
            dt_s=request["dt_s"],
            sample_interval_s=request["sample_interval_s"],
        )
        checks["frozen_model_reproduction"] = digest(expected_run) == digest(
            {k: v for k, v in run.items() if k != "verification"}
        )
        del expected_run
        for body in ("ship", "booster"):
            trace, records = traces[body], step_records[body]
            absent_booster = body == "booster" and not any(
                t["reason"] == "stage_separation" for t in transitions
            )
            checks[f"ordered:{body}"] = (bool(trace) or absent_booster) and all(
                b["time_s"] > a["time_s"] for a, b in zip(trace, trace[1:])
            )
            checks[f"steps_present:{body}"] = bool(records) or (absent_booster and not trace)
            continuity, correct, samples_match, nonnegative = True, True, True, True
            states_at_time = {}
            previous = None
            for record in records:
                before, after = record["before"], record["after"]
                states_at_time.setdefault(before["time_s"], []).append(before)
                states_at_time.setdefault(after["time_s"], []).append(after)
                nonnegative &= before["propellant_kg"] >= 0 and after["propellant_kg"] >= 0
                if previous is not None and not _same(previous, before):
                    matched = [
                        tr
                        for tr in transitions
                        if tr["body_id"] == body
                        and _same(tr["before"], previous)
                        and _same(tr["after"], before)
                    ]
                    continuity &= len(matched) == 1
                continuity &= 0 < after["time_s"] - before["time_s"] <= record["dt_s"] + 1e-6
                continuity &= record["j2"] is True and record["atmosphere"] is True
                if recompute_steps:
                    predicted = step(
                        _state(before),
                        Vehicle(**record["vehicle"]),
                        Control(**record["control"]),
                        record["dt_s"],
                        j2=record.get("j2", True),
                        atmosphere=record.get("atmosphere", True),
                        gravity=record.get("gravity", True),
                        stop_at_ground=record.get("stop_at_ground", True),
                    )
                    errors = _distance(asdict(predicted), after)
                    maxima = [max(x, y) for x, y in zip(maxima, errors)]
                    correct &= all(x <= tol for x, tol in zip(errors, (1e-7, 1e-3, 1e-5, 1e-6)))
                previous = after
                count += 1
            for sample in trace:
                possible = states_at_time.get(sample["time_s"], [])
                possible += [
                    tr["after"]
                    for tr in transitions
                    if tr["body_id"] == body and tr["after"]["time_s"] == sample["time_s"]
                ]
                samples_match &= any(
                    _same(_material_from_sample(sample), material) for material in possible
                )
                vehicle = Vehicle(
                    dry_mass_kg=sample["mass_kg"] - sample["propellant_mass_kg"],
                    area_m2=0,
                    cd=0,
                    cl=0,
                    isp_s=1,
                    max_thrust_n=0,
                )
                derived = observe(_state(_material_from_sample(sample)), vehicle)
                # These frame-dependent quantities must not be invented in the renderer.
                for key, derived_key in (
                    ("altitude_m", "altitude_m"),
                    ("speed_mps", "ground_speed_mps"),
                    ("inertial_speed_mps", "inertial_speed_mps"),
                ):
                    samples_match &= math.isclose(
                        sample[key], derived[derived_key], rel_tol=1e-9, abs_tol=1e-4
                    )
            if records and trace:
                samples_match &= _same(_material_from_sample(trace[-1]), records[-1]["after"])
            checks[f"step_continuity:{body}"] = continuity
            checks[f"sample_state_binding:{body}"] = samples_match
            checks[f"nonnegative_fuel:{body}"] = nonnegative
            checks[f"recomputed_steps:{body}"] = correct and recompute_steps
        metrics["integration_steps_rechecked"] = count
        metrics["maximum_reintegration_error"] = dict(
            zip(("time_s", "position_m", "velocity_mps", "propellant_kg"), maxima)
        )
        ship = traces["ship"]
        last = ship[-1]
        metrics.update(
            {
                "ship_end_time_s": last["time_s"],
                "ship_final_altitude_m": last["altitude_m"],
                "ship_contact_speed_mps": last["speed_mps"]
                if abs(last["altitude_m"]) <= GOAL["surface_tolerance_m"]
                else None,
                "ship_peak_altitude_m": max(s["altitude_m"] for s in ship),
                "ship_remaining_propellant_kg": last["propellant_mass_kg"],
                "satellites_released": len(traces["satellites"]),
            }
        )
        satellite_metrics = {}
        checks["release_identity_binding"] = list(traces["satellites"]) == [
            r["satellite_id"] for r in receipts
        ]
        for identity, track in traces["satellites"].items():
            receipt = next(r for r in receipts if r["satellite_id"] == identity)
            bound = bool(track) and _same(
                _material_from_sample(track[0]), receipt["satellite_state"]
            )
            sat_vehicle = Vehicle(request["profile"]["satellite_mass_kg"], 20.0, 2.2, 0.0, 1.0, 0.0)
            for a, b in zip(track, track[1:]):
                h = b["time_s"] - a["time_s"]
                bound &= 0 < h <= min(10 * request["dt_s"], 10.0) + 1e-6
                if h <= 0:
                    break
                predicted = step(_state(_material_from_sample(a)), sat_vehicle, Control(), h)
                bound &= _same(asdict(predicted), _material_from_sample(b))
            checks[f"satellite_dynamics:{identity}"] = bound
            ordered = len(track) >= 2 and all(
                b["time_s"] > a["time_s"] for a, b in zip(track, track[1:])
            )
            orbit = _orbit(track[-1]) if ordered else None
            valid = bool(
                ordered
                and orbit
                and track[-1]["time_s"] - track[0]["time_s"] >= GOAL["minimum_payload_tracking_s"]
                and orbit["perigee_m"] >= GOAL["minimum_payload_perigee_m"]
                and orbit["apogee_m"] <= GOAL["maximum_payload_apogee_m"]
            )
            satellite_metrics[identity] = {
                "ordered": ordered,
                "orbit": orbit,
                "sampled_orbit_verified": valid,
            }
        metrics["satellites"] = satellite_metrics
        metrics["satellites_in_verified_orbit"] = sum(
            s["sampled_orbit_verified"] for s in satellite_metrics.values()
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        ZeroDivisionError,
        StopIteration,
    ):
        checks["well_formed_records"] = False
    valid = bool(checks) and all(checks.values())
    payload = (
        valid
        and metrics.get("satellites_released") == GOAL["payload_count"]
        and metrics.get("satellites_in_verified_orbit") == GOAL["payload_count"]
    )
    contact_speed = metrics.get("ship_contact_speed_mps")
    contact = (
        valid
        and contact_speed is not None
        and contact_speed <= GOAL["maximum_contact_ground_speed_mps"]
    )
    return {
        "evidence_verified": valid,
        "checks": checks,
        "metrics": metrics,
        "reasons": [k for k, v in checks.items() if not v],
        "payload_orbit_verified": payload,
        "ship_soft_contact_verified": contact,
        "bounded_orbit_and_contact_verified": payload and contact,
        "simulated_mission_completed": False,
        "starship_vehicle_validated": False,
        "starlink_service_verified": False,
        "claim_scope": "Point-mass numerical consistency and sampled states only; no satellite communications, attitude dynamics, TPS survival, water impact or recovery validation.",
    }


def study_content_hash(study: dict) -> str:
    return digest({k: v for k, v in study.items() if k not in ("verification", "content_sha256")})


def verify_study(study: dict) -> dict:
    checks, verdicts = {}, []
    try:
        request = study["request"]
        checks["content_binding"] = study["content_sha256"] == study_content_hash(study)
        checks["source_binding"] = request["source_sha256"] == source_hashes()
        expected = make_request(
            request["scenarios"],
            FlightProfile(**request["profile"]),
            request["dt_s"],
            request["sample_interval_s"],
        )
        checks["request_binding"] = request == expected
        checks["contract_binding"] = study["contract"] == make_contract(request).to_material()
        checks["opt_in"] = study["authority"] == {
            "simulation_opt_in": True,
            "request_sha256": digest(request),
            "scope": request["scope"],
            "authenticated_operator_identity": False,
        }
        reference = json.loads(OBSERVATIONS.read_text())
        checks["public_reference_binding"] = study["public_reference"] == reference
        checks["display_reference_binding"] = study["observations"] == [
            {**p, "source_url": reference["source_url"], "speed_basis": reference["speed_basis"]}
            for p in reference["observations"]
        ]
        checks["claim_boundary"] = study["claim_boundary"] == CLAIMS
        checks["parameter_provenance_binding"] = study[
            "parameter_provenance"
        ] == parameter_provenance(request["profile"])
        checks["sources_binding"] = study["sources"] == SOURCES
        checks["scenario_coverage"] = [r["scenario"] for r in study["runs"]] == request["scenarios"]
        invocation = validate_runtime_invocation_evidence(study["runtime_invocation_evidence"])
        checks["runtime_invocation"] = invocation["invocation_exit_code"] == 0
        receipt = json.loads(invocation["invocation_stdout_preimage"])
        checks["worker_request_binding"] = receipt["request_sha256"] == digest(request)
        raw_runs = [{k: v for k, v in r.items() if k != "verification"} for r in study["runs"]]
        checks["worker_output_binding"] = receipt["run_sha256"] == [digest(r) for r in raw_runs]
        for run in study["runs"]:
            verdict = verify_flight(run, request)
            verdicts.append({"scenario": run["scenario"], **verdict})
            checks[f"trajectory:{run['scenario']}"] = verdict["evidence_verified"]
            checks[f"saved_verdict:{run['scenario']}"] = run["verification"] == verdict
        checks["comparison_binding"] = study["comparison"] == [
            compare_observations(r, reference) for r in study["runs"]
        ]
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, OSError):
        checks["well_formed_study"] = False
    return {
        "study_verified": bool(checks) and all(checks.values()),
        "checks": checks,
        "reasons": [k for k, v in checks.items() if not v],
        "run_verdicts": verdicts,
        "scenario_count": len(verdicts),
        "simulated_mission_completed": False,
        "starship_vehicle_validated": False,
        "physical_execution_invoked": False,
    }


def worker(request_path: Path, output_path: Path) -> dict:
    request = json.loads(request_path.read_text())
    expected = make_request(
        request["scenarios"],
        FlightProfile(**request["profile"]),
        request["dt_s"],
        request["sample_interval_s"],
    )
    if request != expected:
        raise ValueError("Frozen request does not match this source/configuration.")
    runs = [
        simulate_flight(
            FlightProfile(**request["profile"]),
            scenario=scenario,
            dt_s=request["dt_s"],
            sample_interval_s=request["sample_interval_s"],
        )
        for scenario in request["scenarios"]
    ]
    if request["source_sha256"] != source_hashes():
        raise ValueError("Source changed while the worker was running.")
    output_path.write_text(json.dumps(runs, ensure_ascii=False, allow_nan=False))
    return {"request_sha256": digest(request), "run_sha256": [digest(r) for r in runs]}


def run_study(
    output_dir: Path,
    *,
    approved: bool = False,
    scenarios: list[str] | None = None,
    profile: FlightProfile | None = None,
    dt_s: float = 1.0,
    sample_interval_s: float = 5.0,
) -> dict:
    if approved is not True:
        raise PermissionError("Use --approve-simulation for this local engineering study.")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Use an empty output directory; earlier results must be preserved.")
    request = make_request(
        scenarios or ["flight14_inspired"], profile or FlightProfile(), dt_s, sample_interval_s
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    request_path = output_dir / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False))
    raw_path = output_dir / "worker-runs.json"
    started = utc_now()
    command = [
        sys.executable,
        str(ROOT / "scripts/run_starship_3d.py"),
        "--approve-simulation",
        "--worker-request",
        str(request_path.resolve()),
        "--worker-output",
        str(raw_path.resolve()),
    ]
    process = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    finished = utc_now()
    (output_dir / "worker.stdout.txt").write_text(process.stdout)
    (output_dir / "worker.stderr.txt").write_text(process.stderr)
    if process.returncode:
        raise RuntimeError(
            f"Simulation worker failed ({process.returncode}); request and logs were preserved."
        )
    runs = json.loads(raw_path.read_text())
    for run in runs:
        run["verification"] = verify_flight(run, request)
    reference = json.loads(OBSERVATIONS.read_text())
    study = {
        "schema": "missionos.starship_3d_study.v1",
        "run_id": f"starship-3d-{uuid4().hex}",
        "request": request,
        "contract": make_contract(request).to_material(),
        "authority": {
            "simulation_opt_in": True,
            "request_sha256": digest(request),
            "scope": request["scope"],
            "authenticated_operator_identity": False,
        },
        "runs": runs,
        "public_reference": reference,
        "observations": [
            {**p, "source_url": reference["source_url"], "speed_basis": reference["speed_basis"]}
            for p in reference["observations"]
        ],
        "comparison": [compare_observations(r, reference) for r in runs],
        "claim_boundary": CLAIMS,
        "sources": SOURCES,
        "parameter_provenance": parameter_provenance(request["profile"]),
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "native_basilisk_invoked_in_this_study": False,
        },
        "runtime_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "subprocess",
            "invocation_target": "scripts/run_starship_3d.py --worker-request request.json",
            "invocation_started_at": started,
            "invocation_completed_at": finished,
            "invocation_exit_code": process.returncode,
            "invocation_stdout_sha256": sha256(process.stdout.encode()).hexdigest(),
            "invocation_stderr_sha256": sha256(process.stderr.encode()).hexdigest(),
            "invocation_stdout_preimage": process.stdout,
            "invocation_stderr_preimage": process.stderr,
        },
    }
    study["content_sha256"] = study_content_hash(study)
    study["verification"] = verify_study(study)
    return study
