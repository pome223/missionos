"""Final-only saved Booster refusal; legacy physical admission stays unchanged.

The ordinary path delegates to the original checker. A valid final-supervision
refusal is a separate unresolved terminal record, not a renamed impact, fake
normal flight or safe recovery. These checks replay algebraic observations and
commands only, never a plant, model, forecast or old qualification.
"""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from . import starship_restored_verifier as legacy
from . import starship_restored_contract as contract
from . import starship_sixdof_verifier as saved
from . import starship_state_mission_metrics as state_checks
from . import starship_booster_terminal_metrics as booster_checks
from . import starship_sixdof as dyn, starship_physics as env
from .starship_experimental_continuation import development_settings
from .starship_experimental_mission_metrics import (
    FORBIDDEN,
    STATE_FIELDS,
    _vehicle_for_sample,
    _clock,
)
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_final_protocol import digest, is_hash
from .starship_final_prefix_supervision import FinalPrefixSupervisionPolicy, validate_saved_judgment

SOURCE = "src/runtime/starship_final_prefix_verifier.py"
REFUSAL = "booster_recovery_refused_by_supervision"
# Exact unchanged common block in the old checker, from its def through just
# before Booster admission. Drift requires an explicit review of this copy;
# current full source pins additionally bind both implementations.
COPIED_COMMON_SHA256 = "a558fc2000f4826e7b8fdd7e8c5fd1ff939044af3305205136628a058fdfb25d"
ROOT = Path(__file__).resolve().parents[2]
CRITICAL = (
    SOURCE,
    "src/runtime/starship_restored_verifier.py",
    "src/runtime/starship_experimental_mission_metrics.py",
    "src/runtime/starship_sixdof_verifier.py",
    "src/runtime/starship_final_prefix_supervision.py",
    "src/intelligence/starship_final_supervision_agent.py",
    "src/runtime/starship_restored_prefix.py",
    "src/runtime/starship_sixdof_mission.py",
    "src/runtime/starship_sixdof_booster.py",
    "src/runtime/starship_concurrent.py",
)
_prefix_basics, _observations = legacy._prefix_basics, legacy._observations


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _same(left, right):
    return (
        type(left) is dict
        and type(right) is dict
        and all(left.get(k) == right.get(k) for k in STATE_FIELDS)
    )


def _source_scope(source, plan, context, mode):
    contract._manifest(source)
    _require(
        is_hash(plan)
        and type(context) is dict
        and set(context) == {"run_id", "mission_id", "vehicle_id"}
        and all(type(x) is str and x for x in context.values())
        and mode in ("fixture", "live"),
        "final_refusal_external_plan_context_mode_required",
    )
    for name in CRITICAL:
        _require(
            source.get(name) == sha256((ROOT / name).read_bytes()).hexdigest(),
            "final_refusal_current_source_binding",
        )
    raw = (ROOT / "src/runtime/starship_restored_verifier.py").read_text()
    start = raw.index("def _prefix_physical(")
    end = raw.index('    booster = launch.get("booster_run")', start)
    _require(
        sha256(raw[start:end].encode()).hexdigest() == COPIED_COMMON_SHA256,
        "final_refusal_copied_common_checks_need_review",
    )


def _common_prefix(
    launch,
    profile,
    *,
    settings,
    controls,
    expected_settings,
    expected_site,
    expected_goal,
    expected_return_sites,
    expected_prefix_horizon_s,
):
    """Unchanged ground/ascent/split/deployment/Booster checks for explicit wrappers."""
    saved._json_tree({key: value for key, value in launch.items() if key != "ascent_control_trace"})
    _require(
        launch.get("scenario") == "experimental_physical_mission"
        and not any(key in launch for key in (*FORBIDDEN, "experimental_mission_execution"))
        and launch.get("development_return_qualification") == development_settings(),
        "Prefix borrowed old execution, certificate or actor scope",
    )
    metadata = launch["experimental_mission_ascent"]
    contract._settings(metadata, controls)
    _require(
        metadata.get("settings") == expected_settings
        and metadata.get("requested_absolute_duration_s") == expected_prefix_horizon_s
        and metadata.get("shutdown_schedule") == settings.ascent.record()["shutdowns"]
        and metadata.get("final_mission_perigee_gate_m") == 220000.0
        and metadata.get("booster_policy") == "availability_terminal_recovery_v1"
        and metadata.get("booster_terminal_recovery") == controls
        and metadata.get("booster_splashdown_goal") == expected_goal
        and metadata.get("booster_return_sites") == expected_return_sites
        and type(expected_return_sites) is dict
        and expected_site in expected_return_sites["sites"],
        "Prefix settings, source site or finite horizon differs",
    )
    for key, value in (
        ("engineering_objectives_not_real_flight_identification", True),
        ("shared_production_deployment_and_return_loop", True),
        ("concurrent_actual_separation", True),
        ("qualified_return", False),
        ("mission_authority_granted", False),
        ("physical_execution", False),
        ("state_reset", False),
        ("all_features_completed", False),
    ):
        _require(metadata.get(key) is value, "Prefix claim boundary changed")
    _require(
        type(metadata.get("native_model_calls")) is int and metadata["native_model_calls"] == 0,
        "Numerical ascent cannot claim native model work",
    )
    evidence = launch["restored_prefix"]
    _require(
        evidence.get("schema") == "missionos.starship_restored_prefix.v1"
        and type(evidence.get("closed")) is bool
        and type(evidence.get("release_generation")) is int
        and evidence["release_generation"] == launch["outcome"]["payload_released_count"]
        and evidence.get("state_reset") is False
        and evidence.get("old_certificate_admitted") is False
        and evidence.get("mission_authority_granted") is False
        and evidence.get("model_calls_inferred") is False
        and evidence.get("origin_validation_issue") is None
        and evidence.get("decision_window_s") == 35.0,
        "Restored prefix evidence claim boundary changed",
    )
    _, last, outcome, events = _prefix_basics(launch, profile)
    _require(
        outcome["termination"] != "numerical_failure"
        and last["time_s"] <= expected_prefix_horizon_s,
        "Invalid numerical prefix or horizon",
    )
    retained_policy = launch.get("retained_return", {})
    _require(
        not events.get("return_requested")
        and retained_policy.get("policy_id") == TRIMMED_POLICY_ID
        and retained_policy.get("status") == "not_activated"
        and retained_policy.get("activation") is None
        and retained_policy.get("landing_verified") is False
        and retained_policy.get("starship_vehicle_validated") is False
        and all(
            sample["phase"] in state_checks.prefix.PHASES | {"orbital_coast"}
            for sample in launch["samples"]
        ),
        "Every restored prefix must stop before any return request, activation or return phase",
    )
    reached, commands = state_checks._ascent(launch, profile, settings, metadata, events)
    saved._payloads(launch, profile, "$.prefix", [len(launch["samples"])], events, reached)
    releases = events.get("payload_released", [])
    _observations(
        launch["samples"],
        lambda sample: _vehicle_for_sample(launch, profile, sample, releases),
        settings,
    )
    _clock(launch, profile, expected_prefix_horizon_s)
    state_checks._trace_clock_binding(launch)
    return reached, commands, outcome, events


def _booster_refusal(
    launch, profile, *, settings, controls, site, goal, horizon, source, plan, context, mode
):
    """Separate unresolved terminal branch; later RPC check authenticates delivery."""
    from .starship_sixdof_mission import vehicle
    from .starship_sixdof_booster import _configuration
    from .starship_return_sites import ReturnSite, validate_return_site
    from .starship_splashdown import SplashdownGoal
    from .starship_splashdown_verifier import verify_splashdown
    from .starship_sixdof_contact import hull_clearance

    booster, separation = launch["booster_run"], launch["booster_separation_state"]
    state0, last = saved._samples(
        booster.get("samples"), "$.final_refusal.samples", [0], "booster", wind_profile=profile
    )
    outcome = booster["outcome"]
    _require(
        booster.get("scenario") == "booster_return"
        and booster.get("body_id") == "booster"
        and booster.get("guidance_policy") == "availability_terminal_recovery_v1"
        and booster.get("booster_separation_state") == separation
        and _same(state0, separation)
        and _same(booster.get("initial_state"), separation)
        and _same(last, booster.get("final_state"))
        and outcome.get("termination") == REFUSAL
        and outcome.get("phase") == last["phase"] in booster_checks.PHASES
        and last.get("contact") is False
        and booster.get("contact") is None
        and outcome.get("recovery_refused_by_supervision") is True
        and outcome.get("recovery_unresolved") is True
        and all(
            outcome.get(key) is False
            for key in (
                "simulated_contact_envelope_met",
                "catch_verified",
                "landing_hardware_validated",
                "starship_vehicle_validated",
                "attitude_prescribed",
                "physical_execution",
            )
        )
        and outcome.get("six_dof_integrated") is True
        and outcome.get("booster_return_6dof_implemented") is True,
        "final_refusal_requires_exact_actual_noncontact_unresolved_termination",
    )
    _require(
        state0["time_s"] <= last["time_s"] <= horizon
        and type(outcome.get("integration_steps")) is int
        and outcome["integration_steps"] >= 0
        and saved._near(outcome.get("duration_s"), last["time_s"] - state0["time_s"])
        and outcome.get("start_time_s") == state0["time_s"]
        and outcome.get("end_time_s") == last["time_s"]
        and saved._near(outcome.get("final_altitude_m"), last["altitude_m"])
        and saved._near(outcome.get("final_ground_speed_mps"), last["ground_speed_mps"])
        and outcome["max_altitude_m"]
        >= max(row["altitude_m"] for row in booster["samples"]) - 1e-5,
        "final_refusal_actual_clock_step_or_observation_changed",
    )
    event_rows = saved._events(
        booster.get("events"), "$.final_refusal.events", state0["time_s"], last["time_s"]
    )
    terminal = event_rows.get(REFUSAL, [])
    _require(
        len(terminal) == 1
        and terminal[0]["time_s"] == last["time_s"]
        and terminal[0].get("state_reset") is False
        and terminal[0].get("numerical_gnc_waited_for_model") is False
        and not any(
            event_rows.get(name) for name in ("surface_contact", "catch_handoff", "time_limit")
        ),
        "final_refusal_missing_exact_terminal_event",
    )
    current_site = ReturnSite.from_dict(site)
    validate_return_site(current_site, profile, terminal_goal="model_surface_contact")
    SplashdownGoal.from_dict(goal).validate_site(current_site)
    _require(
        booster.get("active_return_site") == site
        and booster.get("return_site_sha256") == current_site.sha256
        and booster.get("capture_inhibited") is True
        and booster.get("divert_destination_reached") is False
        and booster.get("guidance_configuration") == _configuration(profile),
        "final_refusal_target_or_guidance_changed",
    )
    for name in (
        *FORBIDDEN,
        "booster_outage_control",
        "booster_outage_execution",
        "booster_energy_control",
        "booster_energy_execution",
        "booster_terminal_recovery_execution",
        "water_return",
        "booster_fidelity",
        "recovery_record",
        "catch_record",
        "development_coast",
        "development_coast_from_separation",
    ):
        _require(name not in booster, "final_refusal_borrowed_unregistered_actor")
    craft = vehicle(profile, "booster")
    _require(
        contract._same(booster.get("final_vehicle"), asdict(craft)),
        "final_refusal_vehicle_capability_changed",
    )
    fuel = separation["propellant_kg"]
    for sample in booster["samples"]:
        state = dyn.state_from_dict({key: sample[key] for key in STATE_FIELDS})
        command = (
            dyn.command_from_dict(sample["command"])
            if sample.get("command") is not None
            else dyn.Command6DOF(
                tuple(dyn.EngineCommand() for _ in craft.engines), state.flap_angles_rad
            )
        )
        dyn._validate_pair(state, craft, command)
        _require(
            sample["phase"] in booster_checks.PHASES
            and booster_checks._availability(sample, separation)
            and state_checks._check_availability(sample, "booster", settings)
            and state.propellant_kg <= fuel + 1e-6,
            "final_refusal_availability_fuel_or_phase_changed",
        )
        fuel = state.propellant_kg
        observation = dyn.observe(state, craft)
        _require(
            saved._near(sample["mass_kg"], observation["mass_kg"], 1e-5)
            and saved._near(sample["altitude_m"], observation["altitude_m"], 1e-5),
            "final_refusal_saved_observation_changed",
        )
    _require(
        booster.get("shared_clock_checkpoint")
        == {"absolute_deadline_s": horizon, "unfinished": False, "state_reset": False},
        "final_refusal_shared_clock_checkpoint_changed",
    )
    ticks = [
        row for row in launch["concurrent_execution"]["tick_order"] if row["body_id"] == "booster"
    ]
    _require(
        ticks
        and ticks[0]["time_s"] == separation["time_s"]
        and ticks[-1]["next_time_s"] == last["time_s"]
        and outcome["integration_steps"]
        == sum(row["next_time_s"] > row["time_s"] for row in ticks),
        "final_refusal_physical_step_count_not_actual_clock",
    )
    saved._booster_envelope(booster, profile, last, "$.final_refusal")
    actual = dyn.state_from_dict(booster["final_state"])
    clearance = hull_clearance(
        actual, craft, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"]
    )
    _require(
        saved._near(outcome.get("final_hull_clearance_m"), clearance["signed_clearance_m"], 1e-5)
        and clearance["signed_clearance_m"] > 0,
        "final_refusal_noncontact_snapshot_intersects_surface",
    )
    peak = booster["runner_booster_peak_witness"]
    _require(
        peak.get("origin") == "all_yielded_physics_ticks_and_final_state; no extra plant step"
        and peak.get("not_independent_physics_reexecution") is True
        and state0["time_s"] <= peak["state"]["time_s"] <= last["time_s"]
        and saved._near(
            outcome["max_altitude_m"], env.ecef_to_geodetic(peak["state"]["r_eci_m"])[2], 1e-5
        ),
        "final_refusal_actual_peak_witness_changed",
    )
    dyn.state_from_dict(peak["state"])
    history = booster["booster_terminal_recovery"]
    commands = [row for row in booster["samples"] if row.get("command") is not None]
    if commands:
        booster_checks._control_metadata(booster, profile, separation, controls, horizon=horizon)
    else:
        _require(
            outcome["integration_steps"] == 0
            and _same(last, separation)
            and history.get("schema") == "missionos.booster_terminal_recovery.v1"
            and history.get("policy_id") == "availability_terminal_recovery_v1"
            and history.get("settings") == controls
            and history.get("control_witnesses") == []
            and history.get("availability_preserved") is True
            and history.get("separation_state_preserved") is True
            and history.get("legacy_guidance_preserved")
            is (not controls.get("low_pressure_reference_capture", False))
            and all(
                history.get(key) is False
                for key in (
                    "physics_limits_changed",
                    "actual_state_assigned",
                    "mission_authority_granted",
                    "qualification_refreshed",
                )
            )
            and history.get("nested_sixdof_forecasts") == history.get("hosted_model_calls") == 0,
            "final_refusal_zero_step_cannot_borrow_control_history",
        )
        if controls.get("low_pressure_reference_capture", False):
            _require(
                history.get("reference_capture_handoff") is None,
                "final_refusal_zero_step_capture_handoff",
            )
        if controls.get("powered_reference_capture", False):
            _require(
                history.get("powered_reference_capture")
                == {
                    "schema": "missionos.booster_powered_reference_capture_history.v1",
                    "last_request": None,
                    "last_request_state": None,
                    "status_witnesses": [],
                },
                "final_refusal_zero_step_powered_history",
            )
    supervision = launch["final_prefix_supervision"]
    partial = booster["final_prefix_supervision"]
    binding = {
        "schema": "missionos.starship_final_prefix_supervision.v1",
        "run_id": context["run_id"],
        "plan_sha256": plan,
        "source_sha256": digest(source),
        "mode": mode,
        "policy_sha256": digest(FinalPrefixSupervisionPolicy().record()),
        "scope_sha256": digest({"context": context, "phase": "prefix", "plan": plan}),
    }
    _require(
        all(
            value.get(key) == wanted
            for value in (supervision, partial)
            for key, wanted in binding.items()
        )
        and supervision.get("policy")
        == partial.get("policy")
        == FinalPrefixSupervisionPolicy().record()
        and supervision.get("recovery_refused") is partial.get("recovery_refused") is True,
        "final_refusal_bound_supervision_record_required",
    )
    judgments = [row for row in supervision["judgments"] if row["domain"] == "booster"]
    requests = [row for row in supervision["requests"] if row["domain"] == "booster"]
    _require(
        len(judgments) == len(requests) == 1,
        "final_refusal_single_original_booster_request_required",
    )
    judgment, request = judgments[0], requests[0]
    _require(
        judgment.get("accepted") is True
        and judgment.get("action") == "inhibit_recovery"
        and judgment.get("request_id") == request["request_id"]
        and judgment.get("native_model_judgment") is (mode == "live")
        and judgment.get("physical_execution_authorized") is False
        and request["started_s"] == separation["time_s"]
        and request["deadline_s"] == separation["time_s"] + 8.0
        and separation["time_s"] <= judgment["time_s"] <= last["time_s"]
        and last["time_s"] - judgment["time_s"]
        <= max(profile["integration"]["powered_dt_s"], profile["integration"]["coast_dt_s"]) + 1e-8
        and last["time_s"] + 2.0 < request["deadline_s"],
        "final_refusal_original_inhibit_not_actual_disposition",
    )
    checked = validate_saved_judgment(request, judgment["result"], mode=mode)
    _require(
        checked["action"] == "inhibit_recovery"
        and _same(request["observation"]["state"], separation)
        and contract._same(request["observation"]["vehicle"], asdict(craft))
        and any(contract._same(row, judgment) for row in partial["judgments"])
        and any(contract._same(row, request) for row in partial["requests"]),
        "final_refusal_proposal_or_actual_initial_state_reassigned",
    )
    witnesses = booster.get("final_supervision_actual_witnesses", [])
    actual_rows = [row for row in witnesses if request["request_id"] in row.get("request_ids", [])]
    _require(
        len(actual_rows) == 1
        and actual_rows[0].get("origin") == "actual_plant_tick_before_supervision"
        and actual_rows[0].get("state_or_inventory_assigned") is False
        and actual_rows[0].get("body") == "booster"
        and _same(actual_rows[0].get("state"), separation)
        and actual_rows[0].get("state_sha256") == request["observation"]["state_sha256"]
        and actual_rows[0].get("vehicle_sha256") == request["observation"]["vehicle_sha256"]
        and contract._same(actual_rows[0].get("vehicle"), request["observation"]["vehicle"]),
        "final_refusal_actual_observation_seam_missing",
    )
    water = verify_splashdown(booster, goal)
    _require(
        water["passed"] and water["controlled_water_entry_envelope_met"] is False,
        "final_refusal_cannot_claim_water_entry",
    )
    return water | {
        "recovery_refused": True,
        "recovery_unresolved": True,
        "saved_refusal_snapshot_checked": True,
        "host_delivery_requires_separate_rpc_check": True,
        "source_authentication_established": False,
    }


def verify_prefix_physical(
    launch,
    profile,
    *,
    settings,
    controls,
    expected_settings,
    expected_site,
    expected_goal,
    expected_return_sites,
    expected_prefix_horizon_s,
    expected_source_sha256=None,
    expected_plan_sha256=None,
    expected_context=None,
    expected_mode=None,
):
    """Same five-element result as legacy; refuse new outcomes unless exact opt-in."""
    kwargs = {
        "settings": settings,
        "controls": controls,
        "expected_settings": expected_settings,
        "expected_site": expected_site,
        "expected_goal": expected_goal,
        "expected_return_sites": expected_return_sites,
        "expected_prefix_horizon_s": expected_prefix_horizon_s,
    }
    booster = launch.get("booster_run")
    if type(booster) is not dict or booster.get("outcome", {}).get("termination") != REFUSAL:
        return legacy._prefix_physical(launch, profile, **kwargs)
    _source_scope(expected_source_sha256, expected_plan_sha256, expected_context, expected_mode)
    reached, commands, outcome, events = _common_prefix(launch, profile, **kwargs)
    _require(
        outcome.get("booster_return_invoked") is True and bool(events.get("stage_separation")),
        "final_refusal_booster_requires_actual_shared_separation",
    )
    water = _booster_refusal(
        launch,
        profile,
        settings=settings,
        controls=controls,
        site=expected_site,
        goal=expected_goal,
        horizon=expected_prefix_horizon_s,
        source=expected_source_sha256,
        plan=expected_plan_sha256,
        context=expected_context,
        mode=expected_mode,
    )
    return reached, commands, outcome, False, water
