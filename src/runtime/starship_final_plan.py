"""Fresh, non-authorizing registrations for the maintained final mission.

Registering checks data and hashes only. It never advances a plant, sends a
provider request, consumes approval or reuses a historical execution receipt.
The output must be separately reviewed before the opt-in execution entrypoint.
"""
from copy import deepcopy
import re

from . import starship_final_shared as shared

SCHEMA = "missionos.starship_final_plan.v1"
CONFIG = "examples/spaceflight/starship-final/configuration-v1.json"
REFERENCE = "docs/assets/starship-system-repair-20261018/booster-h0-desired-reference-v1.json"
INPUTS = (shared.PROFILE, shared.SITES, shared.GOAL, REFERENCE, CONFIG)
RUNTIME_SOURCES = ("scripts/run_starship_final_mission.py", "scripts/forecast_starship_restored_candidate.py")
CASE_FAMILIES = {"N": "normal", "T": "return_propulsion_80", "A": "return_sensor_gain_80"}
CONFIG_KEYS = {"schema", "ascent_settings", "control_settings", "integration_policy", "operations",
    "visual_policy", "sensor_policy", "supervision_policy", "return_context_projection",
    "return_review_policy", "bounds", "authority", "design_scope"}
PLAN_KEYS = {"schema", "run_id", "case_id", "mode", "context", "source_commit", "source_sha256", "backend",
    "configuration", "input_sha256", "composition", "prerequisites", "execution_authority_granted", "plan_sha256"}


def configuration(value, *, provider_ready=True):
    from .starship_restored_integration import RestoredIntegrationPolicy
    from .starship_restored_operations import RegisteredRestoredOperations
    from .starship_final_visual import FinalVisualPolicy
    from .starship_final_sensor_execution import FinalSensorExecutionPolicy
    from .starship_final_prefix_supervision import FinalPrefixSupervisionPolicy
    from .starship_restored_return_review import validate_policy
    row = shared.parse(shared.wire(value))
    shared.require(type(row) is dict and set(row) == CONFIG_KEYS
        and row["schema"] == "missionos.starship_final_configuration.v1", "final_configuration_shape")
    shared.ascent_settings(row["ascent_settings"])
    shared.booster_control(row["control_settings"])
    integration = RestoredIntegrationPolicy.from_record(row["integration_policy"])
    shared.require(integration.subdivisions == 2, "final_integration_policy_changed")
    RegisteredRestoredOperations(row["operations"])
    visual_policy = FinalVisualPolicy(**row["visual_policy"])
    shared.require(type(provider_ready) is bool, "final_configuration_provider_ready_flag")
    if provider_ready:
        visual_policy.require_provider_dimensions()
    FinalSensorExecutionPolicy(mode="fixture", **row["sensor_policy"])
    FinalPrefixSupervisionPolicy(**row["supervision_policy"])
    validate_policy(row["return_review_policy"])
    shared.require(row["return_context_projection"] is True, "final_return_context_required")
    bounds = row["bounds"]
    shared.require(type(bounds) is dict and set(bounds) == {"per_mission_wall_s", "cohort_wall_s",
        "maximum_simulation_time_s", "maximum_output_bytes", "minimum_free_bytes", "parallel_missions",
        "parallel_forecasts", "automatic_retry"}
        and bounds == {"per_mission_wall_s": 1800., "cohort_wall_s": 6000., "maximum_simulation_time_s": 16000.,
            "maximum_output_bytes": 6*2**30, "minimum_free_bytes": 10*2**30, "parallel_missions": 1,
            "parallel_forecasts": 2, "automatic_retry": False}, "final_registered_bounds_changed")
    authority = row["authority"]
    shared.require(authority == {"kind": "preauthorized_scripted_http_simulation_principal",
        "real_human_review": False, "actual_request_required": True, "candidate_sha256_required": True,
        "request_sha256_required": True, "observed_state_sha256_required": True,
        "allowed_scope_delta": {"add_commands": ["select_next_orbit"], "resource_limits": {}},
        "may_choose_for_model": False, "may_bypass_rules": False, "tower_catch_allowed": False,
        "permanent_deepseek_call_count_cap": None}, "final_authority_configuration_changed")
    shared.require(type(row["design_scope"]) is dict
        and row["design_scope"].get("ascent_condition") == "O0"
        and row["design_scope"].get("normal_means") == "no_additional_return_fault_not_fault_free_ascent"
        and row["design_scope"].get("all_valid_choices_preserved") is True,
        "final_matched_condition_scope_required")
    return row


def stratified_selections():
    """Fixed bounded manifest-only stratification, never flight/AI search."""
    from .starship_final_faults import selection
    found = {}
    for seed in range(1000):
        row = selection(seed)
        found.setdefault(row["selected_family"], row)
        if set(found) == set(CASE_FAMILIES.values()):
            break
    shared.require(set(found) == set(CASE_FAMILIES.values()), "final_finite_seed_range_incomplete")
    return {case: deepcopy(found[family]) for case, family in CASE_FAMILIES.items()}


def prerequisites(value, mode):
    """A pin is not acceptance: the runner must reverify the pinned proof."""
    shared.require(type(value) is dict and set(value) == {"kind", "artifacts", "same_configuration_sha256"}
        and value["kind"] == ("fixed_source_boundary_preflight" if mode == "fixture" else "accepted_fixture_cohort")
        and type(value["artifacts"]) is list and value["artifacts"]
        and type(value["same_configuration_sha256"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", value["same_configuration_sha256"]),
        "final_external_prerequisite_pins_required")
    for item in value["artifacts"]:
        shared.require(type(item) is dict and set(item) == {"path", "sha256"}
            and type(item["sha256"]) is str and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]),
            "final_prerequisite_artifact_pin")
        shared.relative(item["path"])
    return deepcopy(value)


def register(*, run_id, case_id, mode, prerequisite_pins, extra_sources=()):
    """Freeze committed bytes/backend; does not create execution permission."""
    from hashlib import sha256
    config = configuration(shared.read(shared.relative(CONFIG)))
    shared.require(type(run_id) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id)
        and case_id in CASE_FAMILIES and mode in ("fixture", "live"), "final_registration_identity")
    proof = prerequisites(prerequisite_pins, mode)
    shared.require(proof["same_configuration_sha256"] == shared.digest(config), "final_prerequisite_configuration_changed")
    row = {"schema": SCHEMA, "run_id": run_id, "case_id": case_id, "mode": mode,
        "context": {"run_id": run_id, "mission_id": "starship-final-"+case_id,
            "vehicle_id": "starship-final-ship"}, "source_commit": shared.clean_commit(),
        "source_sha256": shared.source_manifest([*INPUTS, *RUNTIME_SOURCES, *extra_sources]), "backend": shared.environment(),
        "configuration": config, "input_sha256": {name: sha256(shared.relative(name).read_bytes()).hexdigest() for name in INPUTS},
        "composition": {"schema": "missionos.starship_final_composition.v1",
            "fault_selection": stratified_selections()[case_id], "showcase": case_id == "T",
            "normal_scope": "no_additional_return_fault_with_same_O0_ascent_outages"},
        "prerequisites": proof, "execution_authority_granted": False}
    row["plan_sha256"] = shared.digest(row)
    validate_plan(row)
    return row


def validate_plan(value, *, current=False):
    from .starship_inflight_runtime import _context
    from .starship_final_faults import validate_selection
    row = shared.parse(shared.wire(value))
    shared.require(type(row) is dict and set(row) == PLAN_KEYS and row["schema"] == SCHEMA
        and row["execution_authority_granted"] is False
        and row["plan_sha256"] == shared.digest({k: v for k, v in row.items() if k != "plan_sha256"})
        and type(row["run_id"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", row["run_id"])
        and row["case_id"] in CASE_FAMILIES and row["mode"] in ("fixture", "live")
        and type(row["source_commit"]) is str and re.fullmatch(r"[a-f0-9]{40}", row["source_commit"]),
        "final_plan_identity_or_hash")
    _context(row["context"])
    shared.require(row["context"] == {"run_id": row["run_id"], "mission_id": "starship-final-"+row["case_id"],
        "vehicle_id": "starship-final-ship"}, "final_plan_context")
    config = configuration(row["configuration"], provider_ready=current)
    prereq = prerequisites(row["prerequisites"], row["mode"])
    shared.require(prereq["same_configuration_sha256"] == shared.digest(config), "final_plan_prerequisite_configuration")
    comp = row["composition"]
    shared.require(type(comp) is dict and set(comp) == {"schema", "fault_selection", "showcase", "normal_scope"}
        and comp["schema"] == "missionos.starship_final_composition.v1"
        and comp["showcase"] is (row["case_id"] == "T")
        and comp["normal_scope"] == "no_additional_return_fault_with_same_O0_ascent_outages"
        and validate_selection(comp["fault_selection"]) == stratified_selections()[row["case_id"]],
        "final_plan_manifest_stratification_changed")
    shared.require(set(row["input_sha256"]) == set(INPUTS)
        and all(row["source_sha256"].get(k) == v for k, v in row["input_sha256"].items())
        and all(name in row["source_sha256"] for name in RUNTIME_SOURCES),
        "final_plan_inputs_not_in_source_binding")
    if current:
        shared.check_manifest(row["source_sha256"])
        shared.require(row["backend"] == shared.environment() and row["source_commit"] == shared.clean_commit(),
            "final_plan_current_source_or_backend_changed")
    return row


def verification_inputs(plan):
    """Externally validated projection; never take bindings from produced logs."""
    from .starship_final_verifier import validate_verification_inputs
    from .starship_final_visual import FinalVisualPolicy
    from .starship_final_prefix_supervision import FinalPrefixSupervisionPolicy
    row = validate_plan(plan)
    config = row["configuration"]
    result = {"schema": "missionos.starship_final_verification_inputs.v1",
        **{key: deepcopy(row[key]) for key in ("context", "plan_sha256", "case_id", "mode", "source_commit", "source_sha256", "backend", "input_sha256")},
        **{key: shared.read(shared.relative(name)) for key, name in
            (("profile", shared.PROFILE), ("site_catalog", shared.SITES), ("goal", shared.GOAL))},
        "ascent_settings": shared.ascent_settings(config["ascent_settings"]).record(),
        "control_settings": shared.booster_control(config["control_settings"]).record(),
        **{key: deepcopy(config[key]) for key in ("integration_policy", "operations", "bounds", "authority",
            "return_context_projection", "return_review_policy")},
        "visual_policy": FinalVisualPolicy(**config["visual_policy"]).record(),
        "sensor_policy": {"mode": row["mode"], **deepcopy(config["sensor_policy"])},
        "supervision_policy": FinalPrefixSupervisionPolicy(**config["supervision_policy"]).record(),
        "fault_selection": deepcopy(row["composition"]["fault_selection"])}
    return validate_verification_inputs(result)
