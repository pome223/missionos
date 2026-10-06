"""Opt-in direct-worker plumbing for a future approved tower-return driver.

The worker owns the actor PID and may receive local integrity material only.
This module does not integrate a vehicle or enable a public scenario. Its
fixed producer/checker entrypoints must exist before a flight can start.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import importlib
import json
import math
import os
from pathlib import Path
import stat
import time
from types import SimpleNamespace

from .starship_operator_resolution import SCOPE
from .starship_return_sites import ReturnSites
from .starship_tower_actor import TowerActor
from .starship_tower_supervision import SCOPE_SCHEMA, _scope, _state

POLICY_ID = "local_tower_return_v1"
CONTRACT_SCHEMA = "missionos.starship_tower_runtime_contract.v1"
ACTIVATION_SCHEMA = "missionos.starship_tower_runtime_activation.v1"
SITES_PATH = "examples/spaceflight/starship-return-sites-model-test.json"
DRIVER_MODULE = "src.runtime.starship_tower_return"
VERIFIER_MODULE = "src.runtime.starship_tower_return_verifier"
DRIVER_PATH = "src/runtime/starship_tower_return.py"
VERIFIER_PATH = "src/runtime/starship_tower_return_verifier.py"
RUNTIME_SOURCES = ("src/runtime/starship_tower_runtime.py", "scripts/run_starship_tower_mission_worker.py")
PROVIDER_NAMES = frozenset({"DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "JEV_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"})


class TowerRuntimeError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise TowerRuntimeError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _snapshot(value):
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TowerRuntimeError("invalid_tower_runtime_json") from None


def digest(value):
    return sha256(_canonical(value)).hexdigest()


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def runtime_contract(*, profile_sha256, catch_profile_sha256, return_sites_sha256):
    _require(all(_hash(value) for value in (profile_sha256, catch_profile_sha256, return_sites_sha256)),
             "invalid_tower_runtime_input_hash")
    return {"schema": CONTRACT_SCHEMA, "policy_id": POLICY_ID, "mode": "fixture",
        "initialization": "fresh_launch_separation_state", "profile_sha256": profile_sha256,
        "catch_profile_sha256": catch_profile_sha256, "return_sites_sha256": return_sites_sha256,
        "maximum_wall_time_s": 900., "pending_time_policy": "one_sim_second_per_wall_second",
        "activation": "fresh_post_planning_early_booster_return", "maximum_jev_calls": 0,
        "maximum_llm_calls": 0, "allowed_actions": ["continue_capture", "divert"],
        "physical_execution_authorized": False, "state_reset_allowed": False,
        "outcome_guaranteed": False, "model_value_demonstrated": False}


def validate_runtime_contract(value, plan):
    value, plan = _snapshot(value), _snapshot(plan)
    _require(type(value) is dict and type(plan.get("tower_supervision")) is dict,
             "explicit_tower_runtime_contract_required")
    expected = runtime_contract(profile_sha256=value.get("profile_sha256"),
        catch_profile_sha256=value.get("catch_profile_sha256"), return_sites_sha256=value.get("return_sites_sha256"))
    _require(value == expected and value["return_sites_sha256"] == plan["tower_supervision"].get("return_sites_sha256")
        and plan["tower_supervision"].get("mode") == "fixture" and type(plan.get("simulation")) is dict
        and plan["simulation"].get("scenario") == "launch"
        and plan["simulation"].get("booster_policy") == POLICY_ID
        and type(plan["simulation"].get("dt_scale")) in (int, float)
        and plan["simulation"]["dt_scale"] == 1.0 and plan["simulation"].get("duration_override_s") is None
        and value["profile_sha256"] == plan["simulation"].get("profile_sha256")
        and value["catch_profile_sha256"] == plan["simulation"].get("catch_profile_sha256"),
        "tower_runtime_contract_binding_mismatch")
    return value


def runtime_sources(repo):
    """Bind public Starship implementation files, never local trial artifacts."""
    repo = Path(repo)
    names = {*RUNTIME_SOURCES, *(str(path.relative_to(repo)) for path in
        (repo/"src/runtime").glob("starship_*.py"))}
    return {name: sha256((repo/name).read_bytes()).hexdigest() for name in sorted(names)}


def _private_read(path, *, maximum_bytes=65536):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file(), "private_runtime_file_required")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= maximum_bytes
            and metadata.st_mode & 0o077 == 0, "invalid_private_runtime_file")
        data = os.read(descriptor, maximum_bytes+1)
        _require(len(data) <= maximum_bytes, "private_runtime_file_too_large")
        return data
    finally:
        os.close(descriptor)


def _private_json(data):
    def object_pairs(pairs):
        result = {}
        for name, value in pairs:
            _require(name not in result, "duplicate_private_runtime_json_key")
            result[name] = value
        return result
    def constant(value):
        raise TowerRuntimeError("nonfinite_private_runtime_json")
    return json.loads(data, object_pairs_hook=object_pairs, parse_constant=constant)


def read_local_key(run_dir):
    key = _private_read(Path(run_dir)/"tower-integrity.key", maximum_bytes=32)
    _require(len(key) == 32, "invalid_tower_local_integrity_key")
    return key


def _signature(value, key):
    return hmac.new(key, digest(value).encode(), "sha256").hexdigest()


def read_activation(run_dir, *, plan, approval, run_id, process_pid, local_integrity_key):
    payload = _private_json(_private_read(Path(run_dir)/"tower-activation.json"))
    fields = {"schema", "scope", "actor_pid", "actor_identity_sha256", "return_sites_sha256",
        "activation_phase", "activated_at_epoch_s", "run_execution_deadline_epoch_s",
        "blocking_planning_finished_assertion", "physical_execution", "signature"}
    _require(type(payload) is dict and set(payload) == fields and payload["schema"] == ACTIVATION_SCHEMA,
             "invalid_tower_runtime_activation")
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    _require(hmac.compare_digest(str(payload["signature"]), _signature(unsigned, local_integrity_key)),
             "tower_runtime_activation_signature_invalid")
    scope = _snapshot(payload["scope"])
    _scope(scope)
    expected_context = {"session_id": plan["session_id"], "plan_id": plan["id"],
        "plan_sha256": plan["sha256"], "run_id": run_id, "request_id": "tower-"+run_id}
    contract = validate_runtime_contract(plan.get("tower_runtime"), plan)
    activated_epoch, run_deadline = payload["activated_at_epoch_s"], approval.get("execution_deadline_epoch_s")
    _require(all(type(value) in (int, float) and math.isfinite(value) for value in
        (activated_epoch, run_deadline, approval.get("consumed_at_epoch_s")))
        and approval["consumed_at_epoch_s"] <= activated_epoch < run_deadline
        and payload["run_execution_deadline_epoch_s"] == run_deadline, "tower_runtime_activation_run_deadline_mismatch")
    window = min(plan["tower_supervision"]["decision_expiry_s"], run_deadline-activated_epoch)
    _require(scope["context"] == expected_context and scope["source_sha256"] == digest(plan["source_sha256"])
        and scope["approval_record_sha256"] == digest(approval) and payload["actor_pid"] == process_pid
        and _hash(payload["actor_identity_sha256"]) and payload["return_sites_sha256"] == contract["return_sites_sha256"]
        and payload["activation_phase"] in ("recovery_boostback_burn", "recovery_powered_entry_prepare")
        and math.isclose(scope["original_simulation_deadline_s"]-scope["issued_simulation_time_s"], window, rel_tol=0., abs_tol=1e-7)
        and math.isclose(scope["original_wall_deadline_s"]-scope["issued_wall_time_s"], window, rel_tol=0., abs_tol=1e-7)
        and payload["blocking_planning_finished_assertion"] is True and payload["physical_execution"] is False,
        "tower_runtime_activation_binding_mismatch")
    return payload


class FixtureTowerJudge:
    """Deterministic handover fixture; no provider transport or model claim."""
    fixture_only = True

    def judge(self, prompt):
        return SimpleNamespace(output={"proposed_response_kind": "operator_escalation", "parameters": {}},
            invocation_evidence={"assessment_route": "human_review"}, model_inference_invoked=False)


class TowerWorkerContext:
    """Actual driver hook. Pacing does not integrate or overwrite a state."""

    def __init__(self, run_dir, plan, approval, run_id, profile, catch_config, return_sites, *,
                 local_integrity_key, source_is_current, clock=time.monotonic, sleep=time.sleep, epoch_clock=time.time):
        self._plan, self._approval = _snapshot(plan), _snapshot(approval)
        self._contract = validate_runtime_contract(self.plan.get("tower_runtime"), self.plan)
        _require(type(run_id) is str and len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)
            and type(return_sites) is ReturnSites and return_sites.sha256 == self.contract["return_sites_sha256"]
            and type(local_integrity_key) is bytes and len(local_integrity_key) == 32
            and callable(source_is_current) and callable(clock) and callable(sleep) and callable(epoch_clock), "invalid_tower_worker_context")
        consumed, deadline = self._approval.get("consumed_at_epoch_s"), self._approval.get("execution_deadline_epoch_s")
        _require(all(type(value) in (int, float) and math.isfinite(value) for value in (consumed, deadline))
            and deadline == consumed+self.contract["maximum_wall_time_s"] and self._approval.get("consumed_by_run") == run_id,
            "tower_worker_original_execution_deadline_required")
        self._profile, self._catch_config = _snapshot(profile), _snapshot(catch_config)
        self.return_sites, self.run_id = return_sites, run_id
        self._root, self._key, self._source_current = Path(run_dir), local_integrity_key, source_is_current
        self._clock, self._sleep, self.actor, self._scope, self._activation = clock, sleep, None, None, None
        self._epoch_clock = epoch_clock
        self._last_time, self._closed = None, False

    @property
    def plan(self):
        return _snapshot(self._plan)

    @property
    def contract(self):
        return _snapshot(self._contract)

    @property
    def approval(self):
        return _snapshot(self._approval)

    @property
    def profile(self):
        return _snapshot(self._profile)

    @property
    def catch_config(self):
        return _snapshot(self._catch_config)

    @property
    def scope(self):
        return _snapshot(self._scope)

    def activate(self, state, *, phase):
        from .starship_mission_control import _write
        body = _snapshot(state)
        _state(body)
        _require(not self._closed and self.actor is None and self._source_current() is True
            and phase in ("recovery_boostback_burn", "recovery_powered_entry_prepare"),
            "tower_runtime_activation_requires_fresh_early_return")
        now = self._clock()
        _require(type(now) in (int, float) and math.isfinite(now) and now >= 0., "invalid_tower_runtime_clock")
        epoch = self._epoch_clock()
        deadline = self.approval["execution_deadline_epoch_s"]
        _require(type(epoch) in (int, float) and math.isfinite(epoch)
            and self.approval["consumed_at_epoch_s"] <= epoch < deadline, "tower_original_run_deadline_expired")
        tower = self.plan["tower_supervision"]
        window = min(tower["decision_expiry_s"], deadline-epoch)
        scope = {"schema": SCOPE_SCHEMA, "scope": SCOPE,
            "context": {"session_id": self.plan["session_id"], "plan_id": self.plan["id"],
                "plan_sha256": self.plan["sha256"], "run_id": self.run_id, "request_id": "tower-"+self.run_id},
            "approval_record_sha256": digest(self.approval), "source_sha256": digest(self.plan["source_sha256"]),
            "issued_simulation_time_s": body["time_s"], "issued_wall_time_s": now,
            "original_simulation_deadline_s": body["time_s"]+window,
            "original_wall_deadline_s": now+window,
            "maximum_observation_age_s": tower["maximum_observation_age_s"],
            "observation_collection_allowed": tower["observation_collection_allowed"],
            "human_resolution_allowed": tower["human_resolution_allowed"]}
        self.actor = TowerActor(self._root/"tower", scope, self.return_sites, self.return_sites.sha256,
            local_integrity_key=self._key, ledger_path=self._root/"tower-resolution.sqlite3", clock=self._clock)
        self._scope, self._last_time = scope, body["time_s"]
        payload = {"schema": ACTIVATION_SCHEMA, "scope": scope, "actor_pid": os.getpid(),
            "actor_identity_sha256": digest(self.actor.receipt()["identity"]),
            "return_sites_sha256": self.return_sites.sha256, "activation_phase": phase,
            "activated_at_epoch_s": epoch, "run_execution_deadline_epoch_s": deadline,
            "blocking_planning_finished_assertion": True, "physical_execution": False}
        payload["signature"] = _signature(payload, self._key)
        _write(self._root/"tower-activation.json", payload)
        self._activation = payload
        return _snapshot(scope)

    def tick(self, state, *, tower_ready, return_mode, active_site_id, run_active=True):
        body = _snapshot(state)
        _state(body)
        _require(not self._closed and self.actor is not None and body["time_s"] >= self._last_time,
                 "tower_runtime_tick_requires_activated_monotonic_state")
        _require(type(run_active) is bool and (tower_ready is None or type(tower_ready) is bool)
            and return_mode in ("capture", "divert", "undecided") and active_site_id in ("capture", "divert"),
            "invalid_tower_runtime_tick_flags")
        active = run_active and self._epoch_clock() < self.approval["execution_deadline_epoch_s"]
        if self.actor.receipt()["supervision"]["directive"] is None and active:
            # The caller has already integrated to this physical time. Wait
            # only for pacing; never fabricate an observation or move a body.
            target = min(self.scope["original_wall_deadline_s"],
                self.scope["issued_wall_time_s"]+(body["time_s"]-self.scope["issued_simulation_time_s"]))
            while (self._clock() < target and self._source_current() is True
                    and self._epoch_clock() < self.approval["execution_deadline_epoch_s"]):
                remaining = target-self._clock()
                if remaining > 0:
                    self._sleep(min(.1, remaining))
        active = active and self._epoch_clock() < self.approval["execution_deadline_epoch_s"]
        result = self.actor.tick(body, tower_ready=tower_ready, return_mode=return_mode, active_site_id=active_site_id,
            source_sha256=digest(self.plan["source_sha256"]) if self._source_current() is True else "0"*64,
            plan_sha256=self.plan["sha256"], run_active=active)
        self._last_time = body["time_s"]
        return result

    def acknowledge_application(self, **evidence):
        _require(not self._closed and self.actor is not None, "tower_runtime_actor_not_active")
        return self.actor.acknowledge_application(**evidence)

    def close(self):
        self._closed = True
        return self.actor.close() if self.actor is not None else None


def _load_entrypoints(plan):
    _require({DRIVER_PATH, VERIFIER_PATH}.issubset(plan["source_sha256"]), "tower_runtime_driver_unavailable")
    try:
        producer = getattr(importlib.import_module(DRIVER_MODULE), "execute_approved_tower_return")
        checker = getattr(importlib.import_module(VERIFIER_MODULE), "verify_tower_return")
    except (ImportError, AttributeError):
        raise TowerRuntimeError("tower_runtime_driver_unavailable") from None
    _require(callable(producer) and callable(checker), "tower_runtime_driver_unavailable")
    return producer, checker


def execute_tower_worker(state_dir, run_id, *, disable_driver=False):
    """Real process entrypoint; unavailable physical integration fails closed."""
    from .starship_mission_control import (
        StarshipMissionService, REPO, ARTIFACT_NAMES, _read, _write, _plan_sources,
    )
    from .starship_sixdof_catalog import SIXDOF_PROFILE, CATCH_PROFILE
    _require(type(disable_driver) is bool, "invalid_reducing_only_driver_guard")
    _require(type(run_id) is str and len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id), "invalid_run_id")
    service = StarshipMissionService(state_dir)
    run_dir = service.root/("run-"+run_id)
    with (run_dir/"worker.lock").open("x"):
        request = _read(run_dir/"request.json")
        _require(type(request) is dict and set(request) == {"schema", "session_id", "plan_id", "plan_sha256", "run_id", "runtime_driver_disabled"}
            and request["schema"] == "missionos.starship_worker_request.v1" and request["run_id"] == run_id
            and type(request["runtime_driver_disabled"]) is bool and request["runtime_driver_disabled"] is disable_driver,
            "tower_worker_request_binding_mismatch")
        with service._lock():
            state = service._load(request["session_id"])
            plan = service._plan_matches(state, request["session_id"], request["plan_id"], request["plan_sha256"])
            service._valid_grant(state, consumed_by_run=run_id)
            contract = validate_runtime_contract(plan.get("tower_runtime"), plan)
            _require(state["status"] == "running" and state["execution"].get("run_id") == run_id
                and state["execution"].get("worker_pid") == os.getpid()
                and state["execution"].get("process_role") == "credential_free_tower_simulation_worker",
                "tower_worker_not_authorized")
            service._valid_tower_reservation(state, run_id, os.getpid())
            _require(state["execution"].get("runtime_driver_disabled") is disable_driver,
                     "tower_worker_reducing_guard_binding_mismatch")
            approval = _snapshot(state["approval"])
        result = {"schema": "missionos.starship_worker_receipt.v1", "run_id": run_id, "plan_sha256": plan["sha256"],
            "worker_pid": os.getpid(), "process_role": "credential_free_tower_simulation_worker",
            "verification_passed": False, "verification": {}, "artifact_sha256": {},
            "provider_credentials_present": any(name in os.environ for name in PROVIDER_NAMES),
            "simulator_provider_credentials_present": any(name in os.environ for name in PROVIDER_NAMES),
            "physical_execution": False, "mission_completed": False}
        result.update(runtime_driver_disabled=disable_driver, physical_driver_entrypoint_called=False)
        context = None
        try:
            _require(not result["provider_credentials_present"], "provider_credentials_in_worker")
            _require(plan["source_sha256"] == _plan_sources(plan), "approved_source_changed")
            _require(not disable_driver, "tower_runtime_driver_disabled")
            producer, checker = _load_entrypoints(plan)
            profile, catch = _read(REPO/SIXDOF_PROFILE), _read(REPO/CATCH_PROFILE)
            sites = ReturnSites.from_dict(_read(REPO/SITES_PATH), profile=profile, catch_config=catch)
            _require(sha256((REPO/SIXDOF_PROFILE).read_bytes()).hexdigest() == contract["profile_sha256"]
                and sha256((REPO/CATCH_PROFILE).read_bytes()).hexdigest() == contract["catch_profile_sha256"]
                and sites.sha256 == contract["return_sites_sha256"], "tower_runtime_public_inputs_changed")
            context = TowerWorkerContext(run_dir, plan, approval, run_id, profile, catch, sites,
                local_integrity_key=read_local_key(run_dir), source_is_current=lambda: plan["source_sha256"] == _plan_sources(plan))
            result["physical_driver_entrypoint_called"] = True
            study = _snapshot(producer(context))
            _require(type(study) is dict and context.actor is not None, "tower_runtime_producer_contract_missing")
            actor_record = context.close()
            _require(actor_record["site_observations"], "tower_runtime_requires_actual_observation")
            _require("tower_runtime" not in study, "tower_runtime_reserved_evidence_field")
            study["tower_runtime"] = {"contract": contract, "scope": context.scope, "actor": actor_record,
                "worker_pid": os.getpid(), "process_role": "credential_free_tower_simulation_worker",
                "producer_entrypoint_called": True, "physical_execution": False,
                "model_value_established": False}
            _require(plan["source_sha256"] == _plan_sources(plan), "approved_source_changed_during_execution")
            output = run_dir/"results"
            output.mkdir(mode=0o700)
            _write(output/"study.json", study)
            verdict = _snapshot(checker(study, plan=plan, profile=profile, catch_config=catch,
                return_sites=sites.to_dict(), tower_scope=context.scope, local_integrity_key=context._key))
            result["verification"] = verdict
            _write(output/"verification.json", verdict)
            _require(type(verdict) is dict and verdict.get("passed") is True
                and verdict.get("physical_execution") is False and verdict.get("model_value_established") is False,
                "tower_runtime_independent_verification_failed")
            from .starship_sixdof_report import build_report
            (output/"report.html").write_text(build_report(study), encoding="utf-8")
            _write(output/"manifest.json", {"schema": "missionos.starship_sixdof_manifest.v1",
                "files": {path.name: sha256(path.read_bytes()).hexdigest() for path in output.iterdir() if path.is_file()}})
            _require(plan["source_sha256"] == _plan_sources(plan), "approved_source_changed_during_verification")
            result["artifact_sha256"] = {name: sha256((output/name).read_bytes()).hexdigest() for name in ARTIFACT_NAMES
                if (output/name).is_file()}
            result["verification_passed"] = True
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            result["failure_reason"] = str(exc) if isinstance(exc, TowerRuntimeError) else "tower_worker_execution_or_verification_failed"
        finally:
            if context is not None:
                actor_record = context.close()
                if actor_record is not None:
                    _write(run_dir/"tower-actor-receipt.json", actor_record)
        result["ended_at_epoch_s"] = service.clock()
        result["signature"] = service._signature(result)
        _write(run_dir/"result.json", result)
        return 0 if result["verification_passed"] else 2
