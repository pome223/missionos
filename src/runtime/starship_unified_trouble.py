"""Registered phase-scoped live judgments and private, seeded return injection.

The prefix/image decisions stay fixtures. Only symptom judgments use providers;
no fault schedule, seed or plant factor crosses that decision boundary.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Lock

from .starship_replanning import ROOT, digest
from .starship_artifacts import read_verified_input

REGISTRATION = "examples/spaceflight/starship-unified-trouble-registration-v2.json"
PLAN_SCHEMA = "missionos.starship_unified_trouble_plan.v2"
COMPOSITION_SCHEMA = "missionos.starship_unified_trouble_composition.v2"
MODES = ("fixture", "live_sensor")
POOL = ("propulsion_80", "sensor_gain_80")


@contextmanager
def provider_environment(project, *, enabled):
    if not enabled:
        yield
        return
    from scripts.start_starship_gateway import _read_secret
    keys = ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY")
    previous = {k: os.environ.get(k) for k in keys}
    try:
        for name, secret in zip(keys, ("jev-api-key", "deepseek-api-key")):
            os.environ[name] = _read_secret(project, secret)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def composition(seed):
    from .starship_unified_contract import composition as base
    from .starship_trouble_engine import make_manifest
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("unified_invalid_seed")
    index = int.from_bytes(sha256(f"unified-return-trouble-v1:{seed}".encode()).digest()[:8], "big") % 2
    case = POOL[index]
    value = base(seed, case=case)
    family = "return_propulsion_80" if case == "propulsion_80" else "return_sensor_gain_80"
    value.update(schema=COMPOSITION_SCHEMA, selection_basis="seeded_fault_only_test_pool",
        return_faults=make_manifest(seed, allowed_families=[family], max_events=1, nominal_probability=0.),
        sensor_resource_profile="unified_sensor_online_300_v1")
    return value


def sensor_configuration(comp):
    from .starship_m3_sensor_executor import CONFIG
    from .starship_m3_sensor_mission import CASES
    if digest(comp) != digest(composition(comp["seed"])):
        raise ValueError("unified_sensor_resource_composition_mismatch")
    return {**deepcopy(CONFIG), **CASES[comp["case"]], "forecast_batch_timeout_s": 300.}


def prepare(seed, *, run_id, mode):
    from .starship_unified_contract import prepare as base
    registration = json.loads((ROOT/REGISTRATION).read_bytes())
    if (mode not in MODES or seed != registration["seed"] or type(seed) is not int
            or registration["fault_pool"] != list(POOL) or registration["maximum_attempts_per_stage"] != 1
            or registration["maximum_hosted_deepseek_calls"] is not None
            or registration["online_forecast_resource_profile"] != "unified_sensor_online_300_v1"
            or registration["online_forecast_batch_timeout_s"] != 300.):
        raise ValueError("unified_trouble_unregistered_trial")
    comp = composition(seed)
    value = base(seed, run_id=run_id, case=comp["case"])
    value.pop("plan_sha256")
    value.update(schema=PLAN_SCHEMA, mode=mode, composition=comp,
        trouble_registration_sha256=digest(registration), phase_scope=deepcopy(registration["phase_scope"]))
    config = sensor_configuration(comp)
    value["sensor_decision_authority"] = {
        "schema": "missionos.starship_unified_sensor_authority.v1", "mode": mode,
        "run_id": run_id+"-return", "configuration_sha256": digest(config),
        "source_sha256": value["source_sha256"], "maximum_deepseek_calls": None,
        "provider_policy": registration["provider_policy"],
        "scope": "separate return-capability judgment grant; fixture prefix/orbital grant unchanged",
        "model_supplies_capability_factor": False, "physical_execution_authorized": False}
    if registration["limits"] != value["limits"]:
        raise ValueError("unified_trouble_limit_changed")
    value["plan_sha256"] = digest(value)
    return value


def preflight_admission(plan, directory):
    """Bind native execution to this source/backend/seed and completed fixture."""
    from .starship_unified_contract import validate_plan
    directory = Path(directory)
    old = validate_plan(read_verified_input(directory/"plan.json"))
    verdict = read_verified_input(directory/"verification.json")
    if (plan.get("schema") != PLAN_SCHEMA or plan.get("mode") != "live_sensor"
            or old.get("schema") != PLAN_SCHEMA or old.get("mode") != "fixture"
            or any(old[k] != plan[k] for k in ("composition", "source_sha256", "backend", "limits", "trouble_registration_sha256"))
            or verdict.get("passed") is not True or verdict.get("mission_completed") is not True
            or verdict.get("checks", {}).get("return_trouble_injection") is not True):
        raise ValueError("unified_trouble_matching_preflight_required")
    return {"schema": "missionos.starship_unified_preflight_admission.v1",
        "preflight_plan_sha256": old["plan_sha256"], "live_plan_sha256": plan["plan_sha256"],
        "verification_file_sha256": sha256((directory/"verification.json").read_bytes()).hexdigest(),
        "same_source_backend_seed_and_fault": True, "passed": True}


def inject(actor, nominal):
    """Return the actual injected plant, preserving state and controller history."""
    from .starship_trouble_engine import TroubleEngine
    from .starship_ship_capability_observation import model_vehicle
    original = asdict(actor.s)
    engine = TroubleEngine(actor.composition["return_faults"])
    state, plant = engine.before_step(actor.s, nominal, "ship", "accepted_return_dispatch")
    if asdict(state) != original or plant != model_vehicle(nominal, actor.sensor_config["physical_factor"]):
        raise ValueError("unified_actual_return_injection_mismatch")
    if engine.accelerometer_gain(state.time_s) != actor.sensor_config["accelerometer_gain"]:
        raise ValueError("unified_actual_sensor_injection_mismatch")
    actor.return_trouble = engine
    return plant


def verify_injection(study, comp):
    """Replay only the discrete injector, never a flight or prediction."""
    from .starship_sixdof_mission import vehicle
    from .starship_sixdof import state_from_dict
    from .starship_trouble_engine import TroubleEngine
    r = study["return_study"]
    record = r["sensor_control"]["record"]
    engine = TroubleEngine(comp["return_faults"])
    state = state_from_dict(record["initial_state"])
    after, plant = engine.before_step(state, vehicle(study["profile"], payload_count=0), "ship", "accepted_return_dispatch")
    audit = engine.audit()
    limits = record["plant_engine_limits_n"]
    return (after == state and audit == r["return_trouble"] and len(audit["receipts"]) == 1
        and not audit["unapplied_event_ids"] and state.time_s == r["dispatch"]["time_s"]
        and [e.max_thrust_n for e in plant.engines] == limits
        and engine.accelerometer_gain(state.time_s) == r["sensor_control"]["fault_harness"]["accelerometer_gain"])


class RecordingDispatch:
    """Host-owned request/receipt hashes; API credentials and bodies stay out."""
    def __init__(self, live, active, directory):
        from scripts.run_starship_m3_sensor import Dispatch
        self.inner, self.directory = Dispatch(live, active), Path(directory)
        self.lock, self.rows = Lock(), []
        self._save()

    def _save(self):
        # Atomic replace supports finite in-progress snapshots without exposing keys.
        path = self.directory/"host-sensor-decisions.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema": "missionos.starship_unified_host_decisions.v1", "rows": self.rows},
                                  allow_nan=False, indent=2)+"\n")
        temp.chmod(0o600)
        temp.replace(path)

    def assess(self, envelope):
        with self.lock:
            row = {"envelope_sha256": digest(envelope), "provider": envelope["provider"],
                   "completed": False, "response_sha256": None, "receipt": None}
            self.rows.append(row)
            self._save()
        try:
            response = self.inner.assess(envelope)
        except Exception as error:
            with self.lock:
                row.update(error_type=type(error).__name__)
                self._save()
            raise
        with self.lock:
            row.update(completed=True, response_sha256=digest(response), receipt=response.get("provider_receipt"))
            self._save()
        return response


def host_accounting(directory):
    path = Path(directory)/"host-sensor-decisions.json"
    return read_verified_input(path) if path.exists() else None


def call_count(rows):
    if rows is None:
        return 0
    if any(not row["completed"] for row in rows["rows"]):
        return None
    return sum(row["receipt"].get("call_attempted") is True for row in rows["rows"])


def verify_host(rows, returning):
    """Bind accepted worker replies to the host's independent HTTP assessment log."""
    if type(rows) is not dict or rows.get("schema") != "missionos.starship_unified_host_decisions.v1":
        return False
    control = returning["sensor_control"]
    if len(rows["rows"]) != len(control.get("decisions", [])):
        return False
    envelopes = {(e["request_sha256"], e["provider"]): e for e in control.get("envelopes", [])}
    for row, decision in zip(rows["rows"], control.get("decisions", [])):
        env = envelopes[(decision["request_sha256"], decision["provider"])]
        if (not row["completed"] or row["envelope_sha256"] != digest(env)
                or row["provider"] != decision["provider"]
                or row["response_sha256"] != digest(decision["reply"])
                or row["receipt"] != decision["reply"].get("provider_receipt")):
            return False
    return True
