"""Approved offshore water-entry objective; no buoyancy or water dynamics.

The location reference is the existing declared synthetic 30 km east site.
Its surface-contact objective is not relabeled as success: this distinct goal
is approved separately and has explicit speed, area, pose and reserve limits.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path

GOAL_FILE = "examples/spaceflight/starship-splashdown-goal.json"


@dataclass(frozen=True)
class SplashdownGoal:
    reference_site_sha256: str
    area_radius_m: float
    maximum_contact_speed_mps: float
    maximum_downward_speed_mps: float
    maximum_horizontal_speed_mps: float
    maximum_tilt_deg: float
    maximum_body_rate_rad_s: float
    minimum_propellant_kg: float

    def __post_init__(self):
        if type(self.reference_site_sha256) is not str or len(self.reference_site_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.reference_site_sha256):
            raise ValueError("invalid_splashdown_reference")
        for name, low, high in (("area_radius_m", 1, 10000), ("maximum_contact_speed_mps", .1, 5),
            ("maximum_downward_speed_mps", .1, 2), ("maximum_horizontal_speed_mps", .1, 3),
            ("maximum_tilt_deg", .1, 5), ("maximum_body_rate_rad_s", .001, .02),
            ("minimum_propellant_kg", 10000, 100000)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("invalid_splashdown_bounds")

    def to_dict(self):
        return {"schema":"missionos.starship_splashdown_goal.v1", "goal_id":"controlled_offshore_water_entry_model_v1",
            **vars(self), "surface":"WGS84_zero_elevation_sea_level_proxy", "water_response_modeled":False,
            "clearance_verified":False, "physical_execution":False}

    @classmethod
    def from_dict(cls, row):
        if type(row) is not dict:
            raise ValueError("invalid_splashdown_goal")
        try:
            obj = cls(**{k:row[k] for k in cls.__dataclass_fields__})
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid_splashdown_goal") from exc
        if obj.to_dict() != row:
            raise ValueError("invalid_splashdown_goal")
        return obj

    def validate_site(self, site):
        if site.site_id != "divert" or site.elevation_m != 0 or site.sha256 != self.reference_site_sha256:
            raise ValueError("splashdown_site_binding")


def load_goal():
    root = Path(__file__).resolve().parents[2]
    return SplashdownGoal.from_dict(json.loads((root/GOAL_FILE).read_text()))


def entry_result(booster, goal):
    """Producer assessment; the separate verifier rechecks saved state geometry."""
    receipt = booster.get("contact")
    outcome = booster["outcome"]
    if not receipt:
        return {"goal":goal.to_dict(), "controlled_water_entry_envelope_met":False,
                "contact_observed":False, "water_response_verified":False, "physical_execution":False}
    normal = receipt["surface_normal_speed_mps"]
    tangent = receipt["surface_tangential_speed_mps"]
    checks = {"area":outcome["return_site_distance_m"] <= goal.area_radius_m,
        "speed":receipt["surface_relative_speed_mps"] <= goal.maximum_contact_speed_mps,
        "vertical":-goal.maximum_downward_speed_mps <= normal <= 0,
        "horizontal":tangent <= goal.maximum_horizontal_speed_mps,
        "tilt":outcome["final_tilt_deg"] <= goal.maximum_tilt_deg,
        "rate":outcome["final_body_rate_rad_s"] <= goal.maximum_body_rate_rad_s,
        "reserve":booster["final_state"]["propellant_kg"] >= goal.minimum_propellant_kg,
        "no_overlap":receipt["initial_overlap"] is False}
    # Integrated plant state can carry NumPy scalar comparisons. Persist
    # ordinary JSON booleans, not scalar objects with a bool type name.
    checks = {name:bool(value) for name,value in checks.items()}
    return {"goal":goal.to_dict(), "checks":checks, "contact_observed":True,
            "controlled_water_entry_envelope_met":all(checks.values()),
            "water_response_verified":False, "physical_execution":False}
