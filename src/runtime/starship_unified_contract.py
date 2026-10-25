"""One frozen experimental composition; no inference or physics at import time.

The private draw belongs to the harness. Only timestamped observations, never
this plan or its fault schedule, cross the model boundary. This contract grants
no nominal qualification and does not weaken the historical entry guards.
"""
from copy import deepcopy
from hashlib import sha256
import json

from .starship_replanning import ROOT, digest

SCHEMA = "missionos.starship_unified_plan.v2"
COMPOSITION_SCHEMA = "missionos.starship_unified_composition.v2"
VISUAL_SCHEMA = "missionos.starship_m3_visual_authority.v1"
REGISTRATION = "examples/spaceflight/starship-unified-registration.json"
# These are synthetic trial frequencies, not historical incident frequencies.
POOL = ("normal", "propulsion_80", "sensor_gain_80", "payload_jam", "unknown_inventory")
LIMITS = {"maximum_wall_time_s": 2400, "maximum_output_bytes": 1610612736,
          "minimum_free_bytes": 5368709120}


def draw(seed):
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("unified_invalid_seed")
    return POOL[int.from_bytes(sha256(f"unified-v1:{seed}".encode()).digest()[:8], "big") % len(POOL)]


def composition(seed, *, case=None):
    """A random draw and an explicit fixture are never labelled interchangeably."""
    sampled = draw(seed)
    if case is not None and case not in POOL:
        raise ValueError("unified_unregistered_case")
    selected = sampled if case is None else case
    from .starship_m3_live import contract
    from .starship_visual_contract import visual_contract
    from .starship_trouble_engine import fixture_manifest
    authority = contract("fixture", "orbital_update")
    returning = deepcopy(authority["m2_authority"]["return_authority"])
    returning.update(resource_profile="unified_forecast_300_v1", forecast_timeout_s=300.)
    # Use the existing camera, deadline and independent inventory semantics.
    # The parent M3 grant, not a legacy visual flight, owns the continuation.
    visual_case = "unknown_inventory" if selected == "unknown_inventory" else "delayed_confirmation"
    visual = visual_contract(visual_case)
    visual.update(schema=VISUAL_SCHEMA, m2_authority=deepcopy(authority["m2_authority"]),
                  continuation_policy="unified_m3_exact_state_v1")
    faults = fixture_manifest([{"family": "payload_jam", "delay_s": 0., "target": "dispenser"}]
                              if selected == "payload_jam" else [])
    return {"schema": COMPOSITION_SCHEMA, "seed": seed, "case": selected,
            "selection_basis": "seeded_uniform_test_pool" if case is None else "explicit_fixture",
            "authority": authority, "return_authority": returning,
            "visual_authority": visual, "prefix_faults": faults,
            "sensor_case": selected if selected in ("propulsion_80", "sensor_gain_80") else "normal",
            "retained_fault_policy": "unresolved_until_retained_symptom_domain_is_verified",
            "catch_policy": "report_informed_judgment_requires_separate_execution_admission",
            "physical_execution_authorized": False}


def validate_composition(value):
    if type(value) is not dict:
        raise ValueError("unified_composition_shape")
    from . import starship_unified_trouble as trouble
    if value.get("schema") == trouble.COMPOSITION_SCHEMA:
        expected = trouble.composition(value.get("seed"))
        if digest(value) != digest(expected):
            raise ValueError("unified_composition_changed")
        return deepcopy(value)
    basis = value.get("selection_basis")
    if basis not in ("seeded_uniform_test_pool", "explicit_fixture"):
        raise ValueError("unified_composition_basis")
    expected = composition(value.get("seed"), case=value.get("case") if basis == "explicit_fixture" else None)
    if digest(value) != digest(expected):
        raise ValueError("unified_composition_changed")
    return deepcopy(value)


def validate_visual(value):
    """Only the exact finite M3 visual composition is accepted, no arbitrary grant."""
    from .starship_m3_live import contract
    from .starship_visual_contract import visual_contract
    for case in ("delayed_confirmation", "unknown_inventory"):
        expected = visual_contract(case)
        expected.update(schema=VISUAL_SCHEMA, m2_authority=contract("fixture", "orbital_update")["m2_authority"],
                        continuation_policy="unified_m3_exact_state_v1")
        if digest(value) == digest(expected):
            return deepcopy(value)
    raise ValueError("unified_visual_authority_changed")


def sources():
    from .starship_m3_sensor_mission import source_hashes
    from .starship_visual_contract import sources as visual_sources
    names = set(source_hashes()) | set(visual_sources()) | {
        REGISTRATION, "src/runtime/starship_unified_contract.py",
        "src/runtime/starship_unified_mission.py", "scripts/run_starship_unified_mission.py",
        "src/runtime/starship_unified_trouble.py", "examples/spaceflight/starship-unified-trouble-registration.json",
        "examples/spaceflight/starship-unified-trouble-registration-v2.json"}
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(names)}


def prepare(seed, *, run_id, case=None):
    from .starship_visual_contract import identity
    from .starship_return_feasibility import backend
    from .starship_m2_contract import PROFILE
    if not identity(run_id) or len(run_id) > 80:
        raise ValueError("unified_invalid_run_id")
    registration = json.loads((ROOT/REGISTRATION).read_bytes())
    if (registration["pool"] != list(POOL) or registration["limits"] != LIMITS
            or registration["schema"] != "missionos.starship_unified_registration.v2"
            or registration["forecast_resource_profile"] != "unified_forecast_300_v1"
            or registration["forecast_timeout_s"] != 300. or registration["batch_timeout_s"] != 600.):
        raise ValueError("unified_registration_changed")
    result = {"schema": SCHEMA, "run_id": run_id, "composition": composition(seed, case=case),
              "registration_sha256": digest(registration), "source_sha256": sources(),
              "profile_file": PROFILE, "backend": backend(), "limits": dict(LIMITS),
              "execution_authorized": False, "hosted_models_authorized": False,
              "qualification": "experimental_unqualified_composition"}
    result["plan_sha256"] = digest(result)
    return result


def validate_plan(value):
    comp = validate_composition(value["composition"])
    from . import starship_unified_trouble as trouble
    if value.get("schema") == trouble.PLAN_SCHEMA:
        expected = trouble.prepare(comp["seed"], run_id=value["run_id"], mode=value.get("mode"))
    else:
        expected = prepare(comp["seed"], run_id=value["run_id"],
                           case=comp["case"] if comp["selection_basis"] == "explicit_fixture" else None)
    if digest(value) != digest(expected):
        raise ValueError("unified_plan_stale_or_changed")
    return deepcopy(value)


def validate_runtime(runtime, director, permission, trouble, profile=None):
    """Bind the new simulator seam to exact actors and their intersected grants."""
    from .starship_visual_director import VisualRuntime, VisualDeploymentDirector
    from .starship_m3_directors import M3Directors
    from .starship_ascent_supervisor import AscentSupervisor
    from .starship_trouble_engine import manifest_digest
    if type(runtime) is not VisualRuntime or type(director) is not M3Directors:
        raise ValueError("unified_bound_runtime_required")
    comp = validate_composition(runtime.composition)
    if (runtime.authority != comp["visual_authority"] or director.visual_runtime is not runtime
            or type(director.ship) is not VisualDeploymentDirector or runtime.director is not director.ship
            or director.ship.visual_runtime is not runtime
            or director.ship.run_id != runtime.run_id+"-deployment"
            or director.booster.run_id != runtime.run_id+"-booster"
            or director.ship.envelope != comp["authority"]["m2_authority"]["deployment_authority"]
            or director.booster.envelope != comp["authority"]["prefix_authority"]["booster_authority"]
            or type(getattr(permission, "__self__", None)) is not AscentSupervisor
            or permission != permission.__self__.deployment_permission
            or permission.__self__.authority != comp["authority"]["prefix_authority"]["ascent_authority"]
            or trouble is None or trouble.manifest_sha256 != manifest_digest(comp["prefix_faults"])):
        raise ValueError("unified_runtime_authority_binding")
    if profile is not None and digest(profile) != comp["authority"]["m2_authority"]["profile_sha256"]:
        raise ValueError("unified_runtime_profile_binding")
    return comp
