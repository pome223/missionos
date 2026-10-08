"""Build reviewed model-domain admission data from a frozen opt-in census.

Does not run flights, fit parameters or discard failures. Every inventory and
the declared perturbation probes must pass the unchanged four-part gate.
"""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_return_feasibility import CORE_SOURCES, MATCH_LIMITS, POLICY, sources, backend, digest, normalized_profile, deorbit_fuel  # noqa: E402
from src.runtime.starship_retained_return_verifier import verify_retained_return  # noqa: E402
from src.runtime.starship_sixdof_verifier import verify_study  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = sources()
    base = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    references, corridors, rows, total, terminal, deorbit = [], [], [], [], [], []
    required = [(n, 0) for n in range(27)]+[(n, offset) for n in (0, 14, 15, 16, 25, 26) for offset in (-1000, 1000)]
    for n, offset in required:
        name = f"retained{n}" if offset == 0 else f"retained{n}-fuel{offset:+d}"
        path = args.input_dir/name
        result = json.loads((path/"result.json").read_text())
        if result["retained_count"] != n or result["initial_fuel_offset_kg"] != offset or result["provisional_speed_and_reserve_gate_met"] is not True:
            raise ValueError("failed_or_mismatched_case:"+name)
        raw = (path/"study.json").read_bytes()
        if sha256(raw).hexdigest() != result["study_sha256"]:
            raise ValueError("changed_saved_study:"+name)
        study = json.loads(raw)
        run = study["runs"][0]
        scope = run["development_return_qualification"]
        if any(run["development_source_sha256"].get(key) != current[key] for key in CORE_SOURCES):
            raise ValueError("qualification_source_not_current:"+name)
        if (digest(normalized_profile(study["profile"])) != digest(normalized_profile(base))
                or not verify_study(study, expected_development_return_qualification=scope)["passed"]
                or not verify_retained_return(run, study["profile"], expected_policy=POLICY,
                    expected_development_return_qualification=scope)["passed"]):
            raise ValueError("independent_checks_failed:"+name)
        start = run["retained_return"]["activation"]["state"]
        trigger = run["retained_return"]["trigger"]["state"]
        contact = run["outcome"]["contact_receipt"]
        total.append(start["propellant_kg"]-contact["propellant_kg"])
        terminal.append(trigger["propellant_kg"]-contact["propellant_kg"])
        inputs = {"r_eci_m": start["r_eci_m"], "v_eci_mps": start["v_eci_mps"],
            "fuel_observed_kg": start["propellant_kg"], "deorbit_perigee_m": base["guidance"]["deorbit_perigee_m"],
            "engine_isp_s": base["ship"]["engine_isp_s"],
            "dry_and_payload_mass_kg": base["ship"]["dry_mass_kg"]+n*base["payload"]["mass_each_kg"]}
        deorbit.append(deorbit_fuel(inputs))
        rows.append(result)
        references.append({"case_id": name, "retained_count": n, **{k: start[k] for k in
            ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg")}})
        samples = {s["time_s"]: s for s in run["samples"] if s["phase"] == "orbital_coast"
                   and start["time_s"]-92. <= s["time_s"] <= start["time_s"]}
        samples[start["time_s"]] = start
        packed = [[s["time_s"], *s["r_eci_m"], *s["v_eci_mps"], *s["q_body_to_eci"],
                   *s["omega_body_rad_s"], s["propellant_kg"]] for _, s in sorted(samples.items())]
        if len(packed) < 40 or any(not 0 < b[0]-a[0] <= MATCH_LIMITS["sample_gap_s"] for a, b in zip(packed, packed[1:])):
            raise ValueError("incomplete_coast_corridor:"+name)
        corridors.append({"case_id": name, "retained_count": n, "initial_fuel_offset_kg": offset,
                          "study_sha256": result["study_sha256"], "samples": packed})
    certificate = {"schema": "missionos.starship_state_return_qualification.v2", "policy_id": POLICY,
        "qualification_complete": True, "retained_counts": list(range(27)),
        "source_sha256": current, "backend": backend(),
        "normalized_profile_sha256": digest(normalized_profile(base)),
        "maximum_return_consumption_kg": max(total), "maximum_terminal_consumption_kg": max(terminal),
        "maximum_reference_deorbit_fuel_kg": max(deorbit), "reference_states": references,
        "coast_corridors": corridors, "matching_tolerances": MATCH_LIMITS,
        "coast_sample_columns": ["time_s", "r_x_m", "r_y_m", "r_z_m", "v_x_mps", "v_y_mps", "v_z_mps",
                                 "q_w", "q_x", "q_y", "q_z", "omega_x_rad_s", "omega_y_rad_s", "omega_z_rad_s", "fuel_kg"],
        "interpolation_is_physical_robustness_proof": False,
        "case_count": len(rows), "cases": rows,
        "thresholds": {"speed_mps": 5., "tilt_deg": 5., "body_rate_rad_s": .02, "reserve_kg": 28000.},
        "qualified_initial_fuel_offset_kg": [-1000., 1000.], "physical_recovery_certified": False,
        "scope": "frozen simulation-profile inventory census and selected initial-fuel perturbations; not every state combination or real vehicle"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # One packed sample per line keeps the complete evidence small and reviewable.
    encoded = json.dumps(certificate, indent=2, allow_nan=False)
    import re
    encoded = re.sub(r'\[\n\s+([-+0-9.eE]+(?:,\n\s+[-+0-9.eE]+){14})\n\s+\]',
                     lambda match: '['+re.sub(r'\s+', ' ', match[1])+']', encoded)
    args.output.write_text(encoded+"\n")
    print(json.dumps({"cases": len(rows), "qualification_complete": True, "maximum_return_consumption_kg": max(total),
        "maximum_terminal_consumption_kg": max(terminal), "physical_recovery_certified": False}))


if __name__ == "__main__":
    main()
