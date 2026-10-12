"""Development-only restart at a recorded first coast sample; no state reset.

The immutable receipt contains the original run bytes, not a caller-provided
replacement state. It is rechecked when consumed. A coast probe stops before
executing the first terminal command and cannot qualify splashdown or catch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math

from . import starship_sixdof as dyn


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("coast_restart_invalid_recorded_time")
    return value


@dataclass(frozen=True)
class CoastRestart:
    record_bytes: bytes
    record_sha256: str
    profile_sha256: str

    @classmethod
    def from_record(cls, raw, *, expected_sha256, profile, return_sites):
        receipt = cls(raw, expected_sha256, digest(profile))
        receipt.resolve(profile, return_sites=return_sites)
        return receipt

    def resolve(self, profile, *, return_sites, separation_state=None):
        from .starship_sixdof_booster import _configuration
        from .starship_sixdof_mission import vehicle
        if type(self.record_bytes) is not bytes or hashlib.sha256(self.record_bytes).hexdigest() != self.record_sha256:
            raise ValueError("coast_restart_record_hash_mismatch")
        if digest(profile) != self.profile_sha256:
            raise ValueError("coast_restart_profile_hash_mismatch")
        run = json.loads(self.record_bytes)
        if (run.get("scenario") != "booster_return" or run.get("body_id") != "booster"
                or run.get("guidance_policy") != "fixed_v1"
                or digest(run.get("guidance_configuration")) != digest(_configuration(profile))
                or digest(run.get("final_vehicle")) != digest(asdict(vehicle(profile, "booster")))):
            raise ValueError("coast_restart_record_model_mismatch")
        if (return_sites is None or run.get("return_site_sha256") != return_sites.divert.sha256
                or digest(run.get("active_return_site")) != digest(return_sites.divert.to_dict())):
            raise ValueError("coast_restart_return_site_mismatch")
        original = run.get("booster_separation_state")
        dyn.state_from_dict(original)
        if separation_state is not None and digest(separation_state) != digest(original):
            raise ValueError("coast_restart_separation_mismatch")
        samples = run.get("samples", [])
        indices = [i for i, sample in enumerate(samples) if sample.get("phase") == "booster_entry_coast"]
        if not indices:
            raise ValueError("coast_restart_no_recorded_coast")
        index = indices[0]
        if not index or samples[index-1].get("phase") not in ("booster_boostback", "booster_boostback_slew"):
            raise ValueError("coast_restart_not_first_coast_boundary")
        sample = samples[index]
        state_dict = {field.name: sample[field.name] for field in fields(dyn.State6DOF)}
        state = dyn.state_from_dict(state_dict)
        dyn.observe(state, vehicle(profile, "booster"))
        cutoffs = [e for e in run.get("events", []) if e.get("event") == "booster_boostback_cutoff_command"]
        ignitions = [e for e in run.get("events", []) if e.get("event") == "booster_boostback_ignition_command"]
        if len(cutoffs) != 1 or len(ignitions) > 1:
            raise ValueError("coast_restart_ambiguous_boostback_history")
        cutoff = _time(cutoffs[0].get("time_s"))
        if not _time(original.get("time_s")) <= cutoff <= state.time_s:
            raise ValueError("coast_restart_invalid_cutoff_history")
        burn_start = _time(ignitions[0].get("time_s")) if ignitions else None
        if burn_start is not None and not original["time_s"] <= burn_start <= cutoff:
            raise ValueError("coast_restart_invalid_ignition_history")
        return {"state": state, "separation_state": original, "coast_started_s": cutoff,
                "burn_start_s": burn_start, "sample_index": index,
                "state_sha256": digest(asdict(state)), "source_run_sha256": self.record_sha256,
                "profile_sha256": self.profile_sha256, "return_site_sha256": return_sites.divert.sha256}


def diagnostic_summary(run):
    """Report sampled controller behavior, never infer qualification from it."""
    samples = [s for s in run["samples"] if s["phase"] == "booster_entry_coast"]
    jets = [i for i, engine in enumerate(run["final_vehicle"]["engines"]) if engine["name"].startswith("rcs_")]
    saturated = sum(any(c.get("enabled") and c.get("throttle", 0) >= 1-1e-9
                        for i, c in enumerate(s.get("command", {}).get("engines", [])) if i in jets) for s in samples)
    final = run["final_state"]
    boundary = run.get("development_coast", {}).get("terminal_boundary")
    return {"termination": run["outcome"]["termination"], "start_time_s": run["initial_state"]["time_s"],
            "end_time_s": final["time_s"], "elapsed_s": run["outcome"]["duration_s"],
            "fuel_consumed_kg": run["initial_state"]["propellant_kg"]-final["propellant_kg"],
            "remaining_fuel_kg": final["propellant_kg"], "sampled_coast_points": len(samples),
            "sampled_rcs_saturated_points": saturated,
            "maximum_sampled_attitude_error_deg": max((s.get("controller", {}).get("attitude_error_deg", 0.) for s in samples), default=None),
            "terminal_boundary": boundary, "controlled_water_entry_qualified": False,
            "physical_execution": False}
