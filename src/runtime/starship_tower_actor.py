"""Credential/provider-free actor seam; observations in, bounded directives out.

This adapter does not integrate a plant, apply a return target or certify a
trajectory. The caller supplies actual states and controller target evidence.
Only a per-run local integrity key is used; no environment credentials are read.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import os
import time
from uuid import uuid4

from .starship_operator_resolution import OperatorResolutionLedger
from .starship_return_sites import ReturnSites, return_site_frame
from .starship_tower_broker import FRAME_SCHEMA, _directory, _read, write_tower_frame
from .starship_tower_supervision import (
    TowerSupervision, TowerTick, _clock, _hash, _scope, _snapshot, _state, tower_observation,
)

IDENTITY_SCHEMA = "missionos.starship_tower_actor_identity.v1"
APPLICATION_SCHEMA = "missionos.starship_tower_actor_application.v1"
_PRIVATE_NAMES = {"actor-identity.json", "actor-ended.json", "actor-application-report.json"}


class TowerActorError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise TowerActorError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _signed(value, key):
    value = _snapshot(value)
    value["sha256"] = _digest(value)
    value["signature"] = hmac.new(key, _canonical(value), "sha256").hexdigest()
    return value


def _publish_private(root, name, value):
    _require(name in _PRIVATE_NAMES, "invalid_actor_file_name")
    root = _directory(root)
    target = root/name
    _require(not target.is_symlink(), "symlink_actor_file")
    raw = _canonical(value)
    _require(len(raw) <= 65536, "actor_record_too_large")
    temporary = root/("."+name+"."+uuid4().hex+".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def verify_actor_identity(identity, *, expected_scope, expected_sites_sha256, local_integrity_key):
    """Bind a supplied PID assertion; the host still verifies the OS process."""
    try:
        identity, scope = _snapshot(identity), _snapshot(expected_scope)
        _scope(scope)
        _require(type(local_integrity_key) is bytes and 32 <= len(local_integrity_key) <= 128,
                 "invalid_actor_integrity_key")
        fields = {"schema", "context", "approved_scope_sha256", "source_sha256", "return_sites_sha256", "actor_pid",
                  "actor_nonce", "started_wall_time_s", "provider_keys_read", "model_calls", "physical_execution", "sha256", "signature"}
        _require(type(identity) is dict and set(identity) == fields and identity["schema"] == IDENTITY_SCHEMA
            and identity["context"] == scope["context"] and identity["approved_scope_sha256"] == _digest(scope)
            and identity["source_sha256"] == scope["source_sha256"] and _hash(expected_sites_sha256)
            and identity["return_sites_sha256"] == expected_sites_sha256 and type(identity["actor_pid"]) is int
            and identity["actor_pid"] > 0 and type(identity["actor_nonce"]) is str and len(identity["actor_nonce"]) == 32
            and all(c in "0123456789abcdef" for c in identity["actor_nonce"]) and _clock(identity["started_wall_time_s"])
            and identity["provider_keys_read"] is False and type(identity["model_calls"]) is int and identity["model_calls"] == 0
            and identity["physical_execution"] is False, "unbound_actor_identity")
        unsigned = {key: value for key, value in identity.items() if key not in {"signature", "sha256"}}
        _require(identity["sha256"] == _digest(unsigned) and _hash(identity["signature"])
            and hmac.compare_digest(identity["signature"], hmac.new(local_integrity_key,
                _canonical({key: value for key, value in identity.items() if key != "signature"}), "sha256").hexdigest()),
            "actor_identity_signature_mismatch")
        return {"passed": True, "os_process_liveness_verified": False, "human_identity_verified": False}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return {"passed": False, "os_process_liveness_verified": False, "human_identity_verified": False}


class TowerActor:
    """One simulator actor; its caller alone owns integration and target changes."""

    def __init__(self, mailbox, approved_scope, return_sites, approved_sites_sha256, *, local_integrity_key,
                 ledger_path, clock=time.monotonic):
        scope = _snapshot(approved_scope)
        _scope(scope)
        _require(type(return_sites) is ReturnSites and _hash(approved_sites_sha256)
            and approved_sites_sha256 == return_sites.sha256 and type(local_integrity_key) is bytes
            and 32 <= len(local_integrity_key) <= 128 and callable(clock), "invalid_actor_scope_or_integrity")
        self._root = _directory(mailbox)
        self._scope, self._sites, self._key, self._clock = scope, return_sites, local_integrity_key, clock
        self._ledger = OperatorResolutionLedger(local_integrity_key, store_path=ledger_path)
        self._supervision = TowerSupervision(scope, resolution_ledger=self._ledger, resolution_signing_key=local_integrity_key)
        self._sequence, self._latest, self._last_frame = 0, None, None
        self._seen_responses, self._directive = set(), None
        self._closed, self._application, self._ended = False, None, None
        self._site_observations, self._rejections = [], []
        now = self._clock()
        _require(_clock(now), "invalid_actor_clock")
        self._identity = _signed({"schema": IDENTITY_SCHEMA, "context": scope["context"],
            "approved_scope_sha256": _digest(scope), "source_sha256": scope["source_sha256"],
            "return_sites_sha256": return_sites.sha256, "actor_pid": os.getpid(), "actor_nonce": uuid4().hex,
            "started_wall_time_s": now, "provider_keys_read": False, "model_calls": 0, "physical_execution": False}, self._key)
        _publish_private(self._root, "actor-identity.json", self._identity)

    def _response(self):
        record = self._supervision.receipt()
        for request in record["routing_requests"]:
            name = f"routing-response-{request['round']}.json"
            if name in self._seen_responses:
                continue
            try:
                proposal = _read(self._root, name)
                if proposal is None:
                    continue
                self._seen_responses.add(name)
                _require(proposal.get("request_sha256") == request["sha256"]
                    and proposal.get("context_sha256") == _digest(self._scope["context"])
                    and proposal.get("source_sha256") == self._scope["source_sha256"], "unbound_actor_proposal")
                return {"routing_proposal": proposal}
            except (ValueError, TypeError, KeyError, OSError):
                self._seen_responses.add(name)
                self._rejections.append({"file": name, "reason": "invalid_bound_actor_response"})
        name = "human-response.json"
        if record["human_resolution"] is not None and name not in self._seen_responses:
            try:
                decision = _read(self._root, name)
                if decision is not None:
                    self._seen_responses.add(name)
                    _require(decision == self._ledger.records(self._scope["context"])["decision"], "unbound_actor_human_decision")
                    return {"human_decision": decision}
            except (ValueError, TypeError, KeyError, OSError):
                self._seen_responses.add(name)
                self._rejections.append({"file": name, "reason": "invalid_bound_actor_response"})
        return {}

    def tick(self, state, *, tower_ready, return_mode, active_site_id, source_sha256, plan_sha256, run_active=True):
        """Sample the supplied current state; return a directive without applying it."""
        _require(not self._closed, "tower_actor_already_closed")
        state = _snapshot(state)
        _state(state)
        _require(type(active_site_id) is str and active_site_id in ("capture", "divert")
            and return_mode in ("capture", "divert", "undecided")
            and (return_mode == "undecided" or return_mode == active_site_id)
            and type(run_active) is bool, "actor_mode_site_mismatch")
        wall = self._clock()
        _require(_clock(wall), "invalid_actor_clock")
        self._sequence += 1
        identity = sha256(self._scope["context"]["request_id"].encode()).hexdigest()[:16]
        observation = tower_observation(context=self._scope["context"], observation_id=f"tower-{identity}-{self._sequence:06d}",
            state=state, wall_time_s=wall, tower_ready=tower_ready, return_mode=return_mode)
        frame = {"schema": FRAME_SCHEMA, "context": self._scope["context"], "source_sha256": source_sha256,
            "observation": observation, "simulation_time_s": state["time_s"], "wall_time_s": wall, "run_active": run_active}
        write_tower_frame(self._root, "latest-observation.json", frame, replace=True)
        self._latest, self._last_frame = observation, frame
        replies = self._response()
        result = self._supervision.tick(observation, simulation_time_s=state["time_s"], wall_time_s=self._clock(),
            run_active=run_active, source_sha256=source_sha256, plan_sha256=plan_sha256, **replies)
        self._site_observations.append({"evidence_sha256": observation["evidence_sha256"], "state_sha256": observation["state_sha256"],
            "simulation_time_s": state["time_s"], "wall_time_s": wall, "return_mode": return_mode,
            "active_site_id": active_site_id, "return_sites_sha256": self._sites.sha256})
        if result.routing_request is not None:
            write_tower_frame(self._root, f"routing-request-{result.routing_request['round']}.json", result.routing_request)
        if result.observation_request is not None:
            write_tower_frame(self._root, "observation-request.json", result.observation_request)
        if result.human_request is not None:
            write_tower_frame(self._root, "human-request.json", result.human_request)
        if result.directive is not None:
            self._directive = _snapshot(result.directive)
        return TowerTick(routing_request=result.routing_request, observation_request=result.observation_request,
                         human_request=result.human_request, directive=result.directive)

    def acknowledge_application(self, *, directive_sha256, before_state, after_state,
                                integration_record_sha256, guidance_target_evidence):
        """Bind a later caller integration/target report, never execute a command."""
        before, after, target = _snapshot(before_state), _snapshot(after_state), _snapshot(guidance_target_evidence)
        _state(before)
        _state(after)
        _require(not self._closed and self._directive is not None and self._application is None
            and directive_sha256 == self._directive["sha256"] and _hash(integration_record_sha256)
            and _digest(before) == self._directive["state_sha256"] and before["time_s"] == self._directive["simulation_time_s"]
            and self._latest is not None and after == self._latest["state"] and after["time_s"] > before["time_s"],
            "actor_application_requires_separate_later_state")
        fields = {"context", "return_sites_sha256", "active_site_id", "return_mode", "simulation_time_s",
                  "state_sha256", "target_origin_eci_m"}
        expected_site = "capture" if self._directive["action"] == "continue_capture" else "divert"
        _require(type(target) is dict and set(target) == fields and target["context"] == self._scope["context"]
            and target["return_sites_sha256"] == self._sites.sha256 and target["active_site_id"] == expected_site
            and target["return_mode"] == expected_site and target["simulation_time_s"] == after["time_s"]
            and target["state_sha256"] == _digest(after) and type(target["target_origin_eci_m"]) is list
            and len(target["target_origin_eci_m"]) == 3, "actor_application_target_binding_mismatch")
        expected = return_site_frame(self._sites.site(expected_site), after["time_s"])["origin_eci_m"]
        _require(all(type(x) in (int, float) and abs(x-y) <= 1e-7 for x, y in zip(target["target_origin_eci_m"], expected)),
                 "actor_application_target_geometry_mismatch")
        _require(self._site_observations[-1]["active_site_id"] == expected_site, "actor_application_site_not_observed")
        report = self._supervision.record_application(directive_sha256=directive_sha256,
            observed_evidence_sha256=self._latest["evidence_sha256"])
        record = {"schema": APPLICATION_SCHEMA, "context": self._scope["context"], "actor_identity_sha256": self._identity["sha256"],
            "directive_sha256": directive_sha256, "before_state_sha256": _digest(before), "after_state_sha256": _digest(after),
            "integration_start_time_s": before["time_s"], "integration_end_time_s": after["time_s"],
            "integration_record_sha256": integration_record_sha256, "guidance_target_evidence": target,
            "supervision_application_report": report, "integration_evidence_supplied": True,
            "integration_independently_verified": False, "physical_effect_verified": False, "flight_outcome_improvement": False,
            "model_value_established": False, "physical_execution": False}
        self._application = _signed(record, self._key)
        _publish_private(self._root, "actor-application-report.json", self._application)
        return _snapshot(self._application)

    def close(self):
        """Publish inactive lifecycle with the unchanged last sampled physical state."""
        if self._closed:
            return self.receipt()
        wall = self._clock()
        _require(_clock(wall), "invalid_actor_clock")
        if self._last_frame is not None:
            frame = _snapshot(self._last_frame)
            _require(wall >= frame["wall_time_s"], "actor_close_clock_regressed")
            frame.update(run_active=False, wall_time_s=wall)
            write_tower_frame(self._root, "latest-observation.json", frame, replace=True)
        ended = _signed({"schema": "missionos.starship_tower_actor_ended.v1", "context": self._scope["context"],
            "actor_identity_sha256": self._identity["sha256"], "actor_pid": os.getpid(), "ended_wall_time_s": wall,
            "last_state_sha256": self._latest["state_sha256"] if self._latest is not None else None,
            "last_simulation_time_s": self._latest["simulation_time_s"] if self._latest is not None else None,
            "run_active": False, "physical_outcome_verified": False}, self._key)
        _publish_private(self._root, "actor-ended.json", ended)
        self._ended = ended
        self._closed = True
        return self.receipt()

    def receipt(self):
        return {"schema": "missionos.starship_tower_actor.v1", "identity": _snapshot(self._identity),
            "return_sites_sha256": self._sites.sha256, "site_observations": _snapshot(self._site_observations),
            "supervision": self._supervision.receipt(), "application_evidence": _snapshot(self._application),
            "rejected_inputs": _snapshot(self._rejections), "ended": _snapshot(self._ended), "closed": self._closed,
            "model_calls": 0, "provider_keys_read": False, "state_assigned": False, "physical_effect_verified": False,
            "flight_outcome_improvement": False, "model_value_established": False}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
