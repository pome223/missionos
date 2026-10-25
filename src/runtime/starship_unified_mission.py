"""Same-flight visual/prefix/return composition and its common evidence index.

Fixture execution uses the production camera, HTTP decoder, shared-clock plant,
numerical tools and checker. It is never counted as hosted-model evidence.
"""
from copy import deepcopy
from pathlib import Path
import json
import time

from .starship_artifacts import write_verified_checkpoint, write_verified_input
from .starship_replanning import ROOT, digest, source_hashes as return_sources
from .starship_unified_contract import validate_plan, sources
from .starship_m3_sensor_executor import SymptomReplanningExecutor, CONFIG


class UnifiedSymptomExecutor(SymptomReplanningExecutor):
    def __init__(self, *args, composition, **kwargs):
        self.composition = deepcopy(composition)
        self.return_trouble = None
        super().__init__(*args, composition=composition, **kwargs)

    def runtime_sources(self):
        return sources()

    def validate_sensor_config(self, value):
        if "return_faults" in self.composition:
            from .starship_unified_trouble import sensor_configuration
            expected = sensor_configuration(self.composition)
            if digest(value) != digest(expected):
                raise ValueError("unified_sensor_resource_grant_mismatch")
            return deepcopy(expected)
        return super().validate_sensor_config(value)

    def make_plant(self, nominal):
        if "return_faults" in self.composition:
            from .starship_unified_trouble import inject
            return inject(self, nominal)
        return super().make_plant(nominal)

    def accelerometer_gain(self):
        return (self.return_trouble.accelerometer_gain(self.s.time_s) if self.return_trouble is not None
                else super().accelerometer_gain())

    def finish(self):
        value = super().finish()
        if self.return_trouble is not None:
            value["return_trouble"] = self.return_trouble.audit()
        return value


def sensor_config(plan):
    if "return_faults" in plan["composition"]:
        from .starship_unified_trouble import sensor_configuration
        return sensor_configuration(plan["composition"])
    from .starship_m3_sensor_mission import CASES
    return {**deepcopy(CONFIG), **CASES[plan["composition"]["sensor_case"]]}


def run(plan, output, *, decide, forecast_map, capture, guard_check, deadline):
    """Execute once; all continuations inherit the actual closed prefix state."""
    from .starship_m2_contract import PROFILE
    from .starship_m3_prefix import run_prefix
    from .starship_replanning_executor import ReplanningExecutor
    from .starship_deployment_world import DeploymentWorld
    from .starship_visual_director import VisualRuntime
    from .starship_visual_observation import VisualObservationCollector, default_cameras
    from .starship_visual_runner import fixture_choices
    from ..intelligence.starship_visual_agent import VisualAgent
    from ..intelligence.starship_visual_transport import VisualAttemptLedger, VisualTransport, fixture_provider
    from ..intelligence.starship_booster_recovery_evidence import mission_recovery_report
    plan, output = validate_plan(plan), Path(output)
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("unified_fresh_output_required")
    output.mkdir(parents=True, mode=0o700)
    comp = plan["composition"]
    profile = json.loads((ROOT/PROFILE).read_bytes())
    cfg = comp["visual_authority"]["visual"]
    collector = VisualObservationCollector(DeploymentWorld(profile), output/"camera", run_id=plan["run_id"],
        cameras=default_cameras(), config={"fps": cfg["camera_fps"], "max_age_s": cfg["frame_max_age_s"],
            "history_s": cfg["ring_duration_s"], "ring_bytes": cfg["maximum_ring_bytes"],
            "store_bytes": cfg["maximum_store_bytes"]})
    runtime = VisualRuntime(run_id=plan["run_id"], authority=comp["visual_authority"], collector=collector)
    runtime.composition = deepcopy(comp)
    study = {"schema": "missionos.starship_unified_study.v1", "plan": plan, "profile": profile,
             "run_id": plan["run_id"], "source_sha256": plan["source_sha256"],
             "prefix": None, "return_study": None, "runtime_failure": None,
             "recovery_evidence": mission_recovery_report(), "continuation": None,
             "hosted_model_calls": 0, "physical_execution": False, "retry_performed": False}
    started, actor = time.monotonic(), None
    write_verified_input(output/"plan.json", plan)
    ledger = VisualAttemptLedger(output/"visual-attempts.json", run_id=plan["run_id"],
                                authority_sha256=digest(runtime.authority), mode="loopback_fixture")
    choices = fixture_choices("retained_payload" if comp["case"] == "payload_jam" else cfg["case"])
    try:
        with fixture_provider(choices) as provider:
            transport = VisualTransport(mode="loopback_fixture", fixture_endpoint=provider["endpoint"],
                ledger=ledger, active=runtime.request_active, remaining=runtime.remaining_s)
            runtime.agent = VisualAgent(transport=transport, frame_store=collector.store)
            prefix = run_prefix(profile, "orbital_update", output/"prefix", run_id=plan["run_id"],
                                composition=comp, visual_runtime=runtime)
            study["prefix"] = prefix
            study["visual_http_requests"] = deepcopy(provider["requests"])
        from .starship_m3_prefix_verifier import verify as verify_prefix
        checked = verify_prefix(prefix)
        study["prefix_verification"] = checked
        if not checked["passed"]:
            raise ValueError("unified_prefix_verification_failed")
        if not checked["handoff_ready"]:
            study["continuation"] = {"status": "inhibited_unresolved", "reason": prefix["handoff_error"],
                                     "inventory_assumed": False, "return_started": False}
            return study
        if time.monotonic() >= deadline:
            raise TimeoutError("unified_deadline_before_return")
        inner, handoff = comp["authority"]["m2_authority"], prefix["handoff"]
        retained = handoff["retained_count"]
        # Never erase retained mass or use the zero-payload fault certificate.
        if retained and comp["sensor_case"] != "normal":
            study["continuation"] = {"status": "inhibited_unresolved", "reason": "retained_symptom_domain_unverified",
                                     "retained_count": retained, "return_started": False}
            return study
        returning = output/"return"
        returning.mkdir(mode=0o700)
        kwargs = {"deployment": {"authority": inner, "handoff": handoff,
                   "launch": prefix["prefix_run"], "visual_authority": comp["visual_authority"]},
                  "run_id": plan["run_id"]+"-return", "composition": comp}
        cls = ReplanningExecutor if retained else UnifiedSymptomExecutor
        if not retained:
            kwargs.update(sensor_config=sensor_config(plan), decide=decide, forecast_map=forecast_map,
                capture=capture, guard_check=guard_check, deadline=deadline,
                mode="live" if plan.get("mode") == "live_sensor" else "fixture")
        actor = cls(profile, comp["return_authority"], inner["return_case"], returning, **kwargs)
        study["continuation"] = {"status": "started", "retained_count": retained,
            "retained_payload_kg": retained*profile["payload"]["mass_each_kg"],
            "origin_sha256": digest(handoff["execution_state"]), "return_started": True,
            "route": "known_retained_nominal" if retained else "symptom_control"}
        study["return_study"] = actor.run()
        study["continuation"]["status"] = ("contact_recorded"
            if study["return_study"]["execution"]["outcome"]["contact_receipt"] is not None else "stopped_unresolved")
    except Exception as error:
        study["runtime_failure"] = {"type": type(error).__name__, "reason": str(error), "retry_performed": False}
        if actor is not None:
            study["return_study"] = actor.finish()
    finally:
        if "return_faults" in comp:
            from .starship_unified_trouble import host_accounting, call_count
            study["hosted_model_calls"] = call_count(host_accounting(output.parent))
            study["phase_scope"] = deepcopy(plan["phase_scope"])
        study.update(wall_time_s=time.monotonic()-started, sources_unchanged=sources() == plan["source_sha256"])
        study["visual"] = runtime.finish()
        study["camera"] = {"cameras": collector.cameras, "config": collector.config,
            "capture_count": collector._sequence, "renderer": "numpy_triangle_perspective_zbuffer_v1"}
        from .starship_artifacts import read_verified_input
        study["attempt_ledger"] = (read_verified_input(output/"visual-attempts.json")
            if (output/"visual-attempts.json").exists() else None)
        write_verified_checkpoint(output/"study.json", study)
    return study


def run_fixture(plan, output, **kwargs):
    if plan.get("mode", "fixture") != "fixture":
        raise ValueError("unified_fixture_cannot_execute_live_plan")
    return run(plan, output, **kwargs)


def verify(study, *, expected_plan, authoritative_capture, artifact_dir, host_decisions=None):
    """Recompute the sub-verdicts, state/mass binding and pinned pixel hashes."""
    from .starship_m3_prefix_verifier import verify as verify_prefix
    from .starship_visual_verifier import verify_observation_evidence
    from .starship_m3_sensor_mission import verify_sensor, verify_planning
    from .starship_replanning_verifier import verify as verify_return
    result = {"schema": "missionos.starship_unified_verification.v1", "passed": False,
              "issues": [], "same_flight_verified": False, "mission_completed": False,
              "ai_control_completed": False, "catch_supported": False, "physical_execution": False}
    checks = result["checks"] = {}
    try:
        validate_plan(expected_plan)
        checks["plan_and_sources"] = (study["plan"] == expected_plan and study["run_id"] == expected_plan["run_id"]
            and study["source_sha256"] == expected_plan["source_sha256"] and study["sources_unchanged"] is True)
        live_sensor = expected_plan.get("mode") == "live_sensor"
        checks["no_hidden_retry_or_live_claim"] = (study["runtime_failure"] is None and study["retry_performed"] is False
            and (live_sensor or study["hosted_model_calls"] == 0) and study["physical_execution"] is False)
        comp, prefix = expected_plan["composition"], study["prefix"]
        checks["composition_binding"] = prefix["unified_composition"] == comp
        prefix_check = verify_prefix(prefix)
        result["prefix"] = prefix_check
        checks["prefix"] = prefix_check["passed"]
        launch = prefix["prefix_run"]
        checks["visual_record_and_pixels"] = (study["visual"] == launch["visual_observation"]
            and verify_observation_evidence(comp["visual_authority"], study["visual"], launch,
                study["camera"], expected_plan["source_sha256"], study["visual_http_requests"],
                study["attempt_ledger"], artifact_dir))
        returning = study["return_study"]
        if returning is None:
            checks["return_inhibited_on_unclosed_inventory"] = (not prefix_check["handoff_ready"]
                and comp["case"] == "unknown_inventory" and prefix["handoff"] is None
                and study["continuation"]["return_started"] is False)
            result["status"] = "return_inhibited_unresolved"
        else:
            handoff = prefix["handoff"]
            binding = {k: handoff[k] for k in ("handoff_sha256", "authority_sha256", "profile_sha256",
                                               "generation", "released_count", "retained_count")}
            checks["exact_state_and_mass"] = (returning["execution_origin"] == handoff["execution_state"]
                and returning["deployment_binding"] == binding and returning["launch"] == launch
                and study["continuation"]["retained_count"] == handoff["retained_count"]
                and study["continuation"]["retained_payload_kg"] == handoff["retained_count"]*study["profile"]["payload"]["mass_each_kg"])
            inner = comp["authority"]["m2_authority"]
            planning = verify_planning(returning, comp["return_authority"], inner["return_case"],
                                       return_sources(), binding, study["run_id"]+"-return")
            result["planning"] = planning
            checks["return_planning"] = planning["passed"]
            if "sensor_control" in returning:
                outcome = verify_sensor(returning, authoritative_capture=authoritative_capture,
                    expected_config=sensor_config(expected_plan), expected_mode="live" if live_sensor else "fixture")
            else:
                outcome = verify_return(returning, expected_envelope=comp["return_authority"],
                    expected_case=inner["return_case"], expected_sources=return_sources(),
                    expected_run_id=study["run_id"]+"-return", expected_deployment_binding=binding)
            result["return"] = outcome
            checks["return_execution"] = outcome["passed"]
            result["same_flight_verified"] = checks["exact_state_and_mass"]
            result["status"] = "simulated_return_checked" if outcome["passed"] else "return_unresolved"
            if "return_faults" in comp:
                from .starship_unified_trouble import verify_injection, verify_host, call_count
                checks["return_trouble_injection"] = verify_injection(study, comp)
                checks["host_decisions_and_call_count"] = (verify_host(host_decisions, returning)
                    and study["hosted_model_calls"] == call_count(host_decisions))
                checks["declared_phase_scope"] = study["phase_scope"] == expected_plan["phase_scope"]
                result["ai_control_completed"] = outcome.get("ai_control_completed", False)
        physical_checks = dict(checks)
        if live_sensor:
            checks["requested_live_sensor_work"] = result["ai_control_completed"] is True
        result["passed"] = bool(checks) and all(checks.values())
        result["mission_completed"] = bool(physical_checks) and all(physical_checks.values()) and returning is not None
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        result["issues"].append(f"{type(error).__name__}: {error}")
    return result


def write_index(output, plan, study, verdict):
    """One small entry per run; raw camera/flight evidence stays in its stage."""
    from hashlib import sha256
    output = Path(output)
    records = {}
    for name in ("flight/study.json", "flight/prefix/study.json", "verification.json", "plan.json"):
        path = output/name
        if path.is_file():
            records[name] = {"sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    value = {"schema": "missionos.starship_unified_record_index.v1", "run_id": plan["run_id"],
             "plan_sha256": plan["plan_sha256"], "records": records, "verification": verdict,
             "execution": "local_6dof_live_sensor" if plan.get("mode") == "live_sensor" else "local_6dof_fixture",
             "model_calls": study.get("hosted_model_calls", 0), "physical_execution": False}
    write_verified_input(output/"index.json", value)
    return value
