"""Fixed, inspectable MissionOS choices for the coupled six-DOF simulator."""

SIXDOF_SCENARIOS = {
    "sixdof_launch": "launch",
    "sixdof_engine_out": "engine_out",
    "sixdof_entry_perturbation": "entry_perturbation",
    "sixdof_gimbal_step": "gimbal_step",
    "sixdof_flap_asymmetry": "flap_asymmetry",
    "sixdof_deployment_supervised": "deployment_no_effect",
    "sixdof_observation_supervised": "deployment_no_effect",
    "sixdof_retained_return_supervised": "deployment_no_effect",
    "sixdof_continuous_return_supervised": "deployment_no_effect",
    "sixdof_booster_catch": "booster_catch",
    "sixdof_booster_catch_tower_unavailable": "booster_catch_tower_unavailable",
    "sixdof_launch_catch": "launch",
}
SIXDOF_PROFILE = "examples/spaceflight/starship-sixdof-profile.json"
SIXDOF_DESCRIPTIONS = {
    "sixdof_launch": "Coupled six-DOF launch, finite payload separation and return attempt with the fixed development profile; not a validated SpaceX flight",
    "sixdof_engine_out": "The same six-DOF mission with one synthetic booster engine failure; preserve impacts and recovery failures",
    "sixdof_entry_perturbation": "Initialized six-DOF atmospheric attitude perturbation response for 30 seconds; not a full mission",
    "sixdof_gimbal_step": "Initialized six-DOF gimbal step response for 30 seconds; not a full mission",
    "sixdof_flap_asymmetry": "Initialized six-DOF asymmetric flap response for 30 seconds; not a full mission",
    "sixdof_deployment_supervised": "Full six-DOF mission with an explicitly synthetic accepted release command without separation; Jev routes bounded hold/skip supervision, conditional DeepSeek proposal, fresh telemetry verification; no retry or validated SpaceX procedure",
    "sixdof_observation_supervised": "The same synthetic missing-separation flight with at most one approved fresh deployment-status report, one reassessment and one skip; no actuator retry, guidance change or demonstrated model benefit",
    "sixdof_retained_return_supervised": "The same synthetic missing-separation mission with explicitly approved deterministic mass/state terminal guidance for retained payload; return time and deorbit unchanged, no additional model authority or guaranteed recovery",
    "sixdof_continuous_return_supervised": "Retained-payload mission with a conditioned attitude-reference bridge near singular entry references and landing transitions; no prescribed vehicle attitude or guaranteed return",
    "sixdof_booster_catch": "Initialized near-tower six-DOF booster contact experiment: finite arms, compliant support forces and settling; not a launch-derived catch or validated SpaceX hardware",
    "sixdof_booster_catch_tower_unavailable": "The same initialized local catch experiment with tower unavailable: inhibit capture and execute a predefined local divert; not a full boostback-to-ocean contingency",
    "sixdof_launch_catch": "Launch-derived booster recovery with predictive deterministic guidance, exact separation-state continuation and conditional finite-force catch; failed approaches are retained without initializing near the tower",
}
SIXDOF_SOURCES = (
    "src/runtime/starship_sixdof_catalog.py", SIXDOF_PROFILE,
    "scripts/run_starship_sixdof.py", "src/runtime/starship_sixdof.py",
    "src/runtime/starship_sixdof_mission.py", "src/runtime/starship_sixdof_separation.py",
    "src/runtime/starship_sixdof_contact.py", "src/runtime/starship_sixdof_booster.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_sixdof_report.py",
    "src/runtime/starship_sixdof_verifier.py", "src/runtime/assets/starship_sixdof_replay.js",
    "src/runtime/starship_flight_supervision.py",
    "src/runtime/starship_retained_return.py", "src/runtime/starship_retained_return_verifier.py",
    "src/runtime/starship_attitude_reference.py",
    "src/runtime/starship_booster_catch.py", "src/runtime/starship_booster_catch_verifier.py",
    "src/runtime/starship_booster_control.py", "src/runtime/starship_booster_recovery.py",
    "src/runtime/starship_fin_allocation.py",
    "src/runtime/starship_entry_trim.py", "src/runtime/starship_return_feasibility.py",
    "src/runtime/starship_return_feasibility_verifier.py",
    "docs/assets/starship-state-return-qualification/qualification.json",
    "src/runtime/starship_landing_context.py",
    "src/runtime/starship_booster_recovery_verifier.py",
    "src/runtime/starship_wind.py", "src/runtime/starship_wind_verifier.py",
    "examples/spaceflight/starship-catch-profile.json",
    "src/runtime/assets/starship_cg_models.js", "src/runtime/assets/starship_cg_renderer.js",
)

SUPERVISED_SCENARIO = "sixdof_deployment_supervised"
OBSERVATION_SCENARIO = "sixdof_observation_supervised"
RETAINED_RETURN_SCENARIO = "sixdof_retained_return_supervised"
CONTINUOUS_RETURN_SCENARIO = "sixdof_continuous_return_supervised"
RETAINED_RETURN_SCENARIOS = frozenset({RETAINED_RETURN_SCENARIO, CONTINUOUS_RETURN_SCENARIO})
SUPERVISED_SCENARIOS = frozenset({SUPERVISED_SCENARIO, OBSERVATION_SCENARIO, *RETAINED_RETURN_SCENARIOS})
FIXED_RETURN_POLICY = "fixed_v1"
RETAINED_RETURN_POLICY = "mass_state_terminal_v1"
CONTINUOUS_RETURN_POLICY = "mass_state_terminal_v3"
CATCH_PROFILE = "examples/spaceflight/starship-catch-profile.json"
LAUNCH_CATCH_SCENARIO = "sixdof_launch_catch"
BOOSTER_RECOVERY_POLICY = "predictive_return_v1"
CATCH_SCENARIOS = ("booster_catch", "booster_catch_tower_unavailable", "booster_catch_lateral_offset",
                   "booster_catch_fast_descent", "booster_catch_one_support")
CATCH_CATALOG = frozenset(k for k, v in SIXDOF_SCENARIOS.items() if v in CATCH_SCENARIOS)
SUPERVISION_SOURCES = (
    "src/runtime/starship_flight_broker.py",
    "src/runtime/starship_flight_supervision_verifier.py",
    "src/intelligence/starship_flight_supervisor.py",
    "src/intelligence/space_ops_investigator.py",
    "src/agents/model_config.py",
)


def supervision_contract(mode, *, observation_collection=False):
    if mode not in {"fixture", "live"}:
        raise ValueError("flight_supervisor_not_configured")
    result = {
        "procedure_id": "local_deployment_missing_effect_v1", "mode": mode,
        "allowed_actions": ["hold", "skip_remaining_deployment"],
        "maximum_commands": 1, "maximum_jev_calls": 1 if mode == "live" else 0,
        "maximum_deepseek_calls": 1 if mode == "live" else 0,
        "deepseek_trigger": "jev_deep_reasoning_only", "decision_expiry_s": 75,
        "pending_time_policy": "continuous_6dof_paced_one_sim_second_per_wall_second",
        "physical_execution_authorized": False, "model_value_demonstrated": False,
    }
    if observation_collection:
        result.update(procedure_id="local_deployment_status_collection_v1", maximum_observation_requests=1,
                      observation_kind="deployment_status", maximum_jev_calls=2 if mode == "live" else 0,
                      maximum_deepseek_calls=1 if mode == "live" else 0,
                      collection_does_not_extend_deadline=True)
    return result


def retained_return_contract(policy=RETAINED_RETURN_POLICY):
    """Inspectable preflight authority, separate from the model's skip proposal."""
    return {
        "policy_id": policy,
        "activation": "configured_return_time_if_payload_retained",
        "authority": "deterministic_approved_guidance",
        "inputs": ["current_mass", "current_inertia", "current_com", "current_available_thrust",
                   "descent_speed", "attitude", "current_body_rate", "actuator_lag"],
        "launch_and_orbit_guidance_unchanged": True,
        "return_time_and_deorbit_unchanged": True,
        "provider_decision_required": False,
        "outcome_guaranteed": False,
        "physical_execution_authorized": False,
    }


def catch_contract():
    return {"policy_id": "local_compliant_catch_v1", "initialization": "near_tower_terminal",
            "human_go_scope": "this_local_simulation_only", "tower_health_required": True,
            "failed_health_action": "local_predefined_divert", "model_dispatch_authority": False,
            "physical_execution_authorized": False, "real_hardware_validated": False}


def recovery_contract():
    return {"policy_id": BOOSTER_RECOVERY_POLICY, "initialization": "launch_separation_state",
            "catch_control_policy": "net_thrust_trim_v1", "catch_maximum_duration_s": 30.,
            "human_go_scope": "this_local_simulation_only", "state_reset_allowed": False,
            "authority": "deterministic_approved_guidance", "catch_handoff_requires_observed_arrival": True,
            "capture_planning_contract": "missionos.starship_capture_planning.v1",
            "unadmitted_geographic_trial_scope": "local_simulation_only",
            "tower_health_required": True, "model_dispatch_authority": False,
            "outcome_guaranteed": False, "physical_execution_authorized": False,
            "real_hardware_validated": False}
