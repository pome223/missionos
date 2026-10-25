"""Opt-in current-tick ascent and sea-recovery continuation judgments.

This worker module has no provider, credential or sleep API. The shared RPC
client owns at most one pending request. Its original simulation/wall epochs
include numerical projection and queue time; physical GNC continues while a
proposal is pending. A refused Booster trial is unresolved, never safe contact.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import math
from pathlib import Path
import time

from .starship_final_protocol import FinalObservationClient, digest, is_hash, request, wire

SOURCE = "src/runtime/starship_final_prefix_supervision.py"
SCHEMA = "missionos.starship_final_prefix_supervision.v1"
MODEL = "jev-1.13.0"
OBSERVATION_SCHEMA = "missionos.starship_final_supervision_observation.v1"
JUDGMENT_SCHEMA = "missionos.starship_final_supervision_judgment.v1"
ASCENT_PHASES = frozenset({"stack_ascent", "ship_ascent", "orbit_insertion"})
ACTIONS = {
    "ascent": ("continue_mission", "withhold_deployment"),
    "booster": ("splashdown", "inhibit_recovery"),
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class FinalPrefixSupervisionPolicy:
    baseline_ascent_time_s: float = 30.0
    ascent_window_s: float = 35.0
    booster_window_s: float = 8.0
    application_reserve_s: float = 2.0

    def __post_init__(self):
        require(
            self.baseline_ascent_time_s == 30.0
            and type(self.baseline_ascent_time_s) is float
            and self.ascent_window_s == 35.0
            and type(self.ascent_window_s) is float
            and self.booster_window_s == 8.0
            and type(self.booster_window_s) is float
            and self.application_reserve_s == 2.0
            and type(self.application_reserve_s) is float,
            "final_prefix_registered_engineering_windows_required",
        )

    def record(self):
        return {
            "schema": "missionos.starship_final_prefix_supervision_policy.v1",
            **asdict(self),
            "booster_epoch_is_engineering_continuation_review": True,
            "booster_review_is_not_boostback_ignition_permission": True,
            "boostback_waits_for_model": False,
            "booster_deepseek_eligible": False,
            "health_channel": "idealized_current_observed_main_availability_count_not_fault_diagnosis",
            "expired_ascent": "withhold_future_deployment_safety_path",
            "expired_booster": "continue_preapproved_numerical_sea_gnc_no_ai_claim",
            "valid_booster_inhibit": "stop_body_trial_at_actual_state_recovery_unresolved",
            "automatic_retry": False,
            "physical_clock_paused": False,
        }


def numerical_projection(state, vehicle, *, body, deployment_allowed=True, tower_ready=True):
    """Project present numerical observations; no plant object goes to a model.

    Source/arrival clocks are equal here: idealized engineering telemetry. Main
    availability is explicitly a health-channel observation, not diagnosis of
    an injected fault. Random return-fault diagnosis is a separate sensor path.
    """
    from . import starship_sixdof as dyn, starship_physics as env

    require(
        type(state) is dyn.State6DOF
        and type(vehicle) is dyn.Vehicle6DOF
        and body in ("ascent", "booster")
        and type(deployment_allowed) is bool
        and type(tower_ready) is bool,
        "final_prefix_actual_numerical_objects_required",
    )
    observed = dyn.observe(state, vehicle)
    main = [
        (engine, actual)
        for engine, actual in zip(vehicle.engines, state.engine_states)
        if not engine.name.startswith("rcs_")
    ]
    up, _, _ = env.local_frame(
        env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg)
    )
    ground_velocity = env.add(
        state.v_eci_mps,
        env.scale(env.cross((0.0, 0.0, env.EARTH_ROTATION_RAD_S), state.r_eci_m), -1.0),
    )
    vertical = env.dot(ground_velocity, up)
    horizontal = env.norm(env.add(ground_velocity, env.scale(up, -vertical)))
    axis = dyn.rotate(state.q_body_to_eci, (0.0, 0.0, 1.0))
    facts = {
        "time_s": float(state.time_s),
        "source_time_s": float(state.time_s),
        "available_time_s": float(state.time_s),
        "altitude_m": float(observed["altitude_m"]),
        "ground_speed_mps": float(env.norm(ground_velocity)),
        "vertical_speed_mps": float(vertical),
        "horizontal_speed_mps": float(horizontal),
        "propellant_kg": float(state.propellant_kg),
        "body_rate_rad_s": float(env.norm(state.omega_body_rad_s)),
        "tilt_deg": math.degrees(math.acos(max(-1.0, min(1.0, env.dot(axis, up))))),
        "dynamic_pressure_pa": float(observed["dynamic_pressure_pa"]),
        "expected_engine_count": len(main),
        "observed_available_engine_count": sum(actual.available for _, actual in main),
    }
    if body == "ascent":
        facts["deployment_allowed"] = deployment_allowed
    else:
        facts.update(recovery_control_available=True, tower_ready=tower_ready)
    # The public projection intentionally contains no current-state feasibility
    # success flag; existence of sea GNC or past catch failures cannot prove it.
    return facts


def validate_saved_judgment(row, result, *, mode):
    """Recheck saved bindings/finite choices; source authentication stays external."""
    require(
        mode in ("fixture", "live") and type(row) is dict, "final_prefix_saved_judgment_context"
    )
    value = row["request"]
    require(
        row["request_sha256"] == digest(value)
        and row["request_id"] == value["request_id"]
        and row["domain"] == value["domain"]
        and row["started_s"] == value["time_s"]
        and row["deadline_s"] == value["deadline_s"]
        and row["wall_deadline_s"] - row["started_wall_s"] == row["deadline_s"] - row["started_s"],
        "final_prefix_saved_request_epoch_required",
    )
    from . import starship_sixdof as dyn

    observed = row["observation"]
    require(
        observed["provider_input"] is False
        and observed["body"] == row["domain"]
        and observed["state_sha256"] == digest(observed["state"])
        and observed["vehicle_sha256"] == digest(observed["vehicle"]),
        "final_prefix_saved_observation_binding",
    )
    projected = numerical_projection(
        dyn.state_from_dict(observed["state"]),
        dyn.vehicle_from_dict(observed["vehicle"]),
        body=row["domain"],
        deployment_allowed=value["observation"]["facts"].get("deployment_allowed", True),
        tower_ready=value["observation"]["facts"].get("tower_ready", True),
    )
    require(
        digest(projected) == observed["facts_sha256"] == digest(value["observation"]["facts"]),
        "final_prefix_saved_observation_projection",
    )
    live = mode == "live"
    require(
        type(result) is dict
        and result.get("schema") == JUDGMENT_SCHEMA
        and result.get("request_id") == row["request_id"]
        and result.get("request_sha256") == row["request_sha256"]
        and result.get("domain") == row["domain"]
        and result.get("provider") == "typesafe"
        and result.get("mode") == mode
        and result.get("status") == ("valid" if live else "fixture_valid")
        and result.get("model_inference_invoked") is live
        and result.get("execution_permission_granted") is False
        and result.get("action") in ACTIONS[row["domain"]],
        "final_prefix_bound_judgment_required",
    )
    receipt = result.get("provider_receipt")
    require(
        type(receipt) is dict
        and receipt.get("schema_version") == "runtime_invocation_evidence.v1"
        and receipt.get("mode") == mode
        and receipt.get("request_model") == MODEL
        and receipt.get("requested_model_id") == receipt.get("model_id") == MODEL
        and receipt.get("status") == ("succeeded" if live else "fixture_succeeded")
        and receipt.get("model_inference_invoked") is live
        and all(receipt.get(key) is live for key in ("call_attempted", "call_succeeded"))
        and all(
            receipt.get(key) is False
            for key in (
                "approval_granted",
                "dispatch",
                "execution_authorized",
                "physical_execution",
            )
        )
        and receipt.get("retries") == receipt.get("redirects") == 0,
        "final_prefix_invocation_receipt_required",
    )
    require(
        receipt.get("original_request_sha256") == row["request_sha256"]
        and receipt.get("original_deadline_monotonic") == row["wall_deadline_s"]
        and number(receipt.get("decision_deadline_monotonic"))
        and row["started_wall_s"]
        < receipt["decision_deadline_monotonic"]
        <= row["wall_deadline_s"],
        "final_prefix_original_receipt_epoch_required",
    )
    if live:
        require(
            receipt.get("response_received") is True
            and receipt.get("complete_response_observed") is True
            and is_hash(receipt.get("request_sha256"))
            and is_hash(receipt.get("response_sha256"))
            and type(receipt.get("request_bytes")) is int
            and receipt["request_bytes"] > 0
            and type(receipt.get("response_bytes_read")) is int
            and receipt["response_bytes_read"] > 0,
            "final_prefix_native_response_evidence_required",
        )
    probabilities = receipt.get("native_choice_probabilities")
    confidence = receipt.get("native_choice_confidence")
    require(
        type(probabilities) is dict
        and set(probabilities) == set(ACTIONS[row["domain"]])
        and all(number(value) and value <= 1.0 for value in probabilities.values())
        and math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.001)
        and number(confidence)
        and confidence <= 1.0,
        "final_prefix_finite_choice_required",
    )

    return deepcopy(result)


class FinalPrefixSupervision:
    """Source-bound worker proposal gate sharing the supplemental RPC client.

    The outer worker may pace wall time from pending_epoch while advancing every
    existing physical interval. This module never sleeps, pauses, resets or
    replaces a plant state, and never consumes a foreign visual RPC request.
    """

    def __init__(
        self,
        profile,
        *,
        run_id,
        plan_sha256,
        source_sha256,
        scope_sha256,
        client,
        mode,
        policy=None,
        clock=time.monotonic,
    ):
        require(
            type(client) is FinalObservationClient
            and mode in ("fixture", "live")
            and is_hash(plan_sha256)
            and is_hash(scope_sha256)
            and callable(clock)
            and type(source_sha256) is dict
            and source_sha256
            and all(type(key) is str and is_hash(value) for key, value in source_sha256.items())
            and type(run_id) is str
            and 0 < len(run_id) <= 80
            and all(x.isascii() and (x.isalnum() or x in "-_") for x in run_id),
            "final_prefix_bound_configuration_required",
        )
        require(
            client.binding == {"plan_sha256": plan_sha256, "source_sha256": digest(source_sha256)},
            "final_prefix_rpc_binding_changed",
        )
        root = Path(__file__).resolve().parents[2]
        for name in (
            SOURCE,
            "src/runtime/starship_final_protocol.py",
            "src/intelligence/starship_final_supervision_agent.py",
        ):
            require(
                source_sha256.get(name) == sha256((root / name).read_bytes()).hexdigest(),
                "final_prefix_current_source_opt_in_required",
            )
        self.policy = FinalPrefixSupervisionPolicy() if policy is None else policy
        require(
            type(self.policy) is FinalPrefixSupervisionPolicy, "final_prefix_exact_policy_required"
        )
        self._profile_json = wire(profile)
        self.binding = {
            "run_id": run_id,
            "plan_sha256": plan_sha256,
            "source_sha256": digest(source_sha256),
            "scope_sha256": scope_sha256,
            "mode": mode,
            "policy_sha256": digest(self.policy.record()),
        }
        self._binding_json = wire(self.binding)
        self.client, self.clock = client, clock
        self._clock, self._pending, self._queued = None, None, []
        self._baseline, self._booster_seen = False, False
        self._availability, self._deployment_withheld, self._recovery_refused = {}, False, False
        self.requests, self.judgments, self.events = [], [], []

    def validate_scope(self, profile, source_sha256=None):
        require(
            wire(self.binding) == self._binding_json
            and wire(profile) == self._profile_json
            and (source_sha256 is None or digest(source_sha256) == self.binding["source_sha256"])
            and self.client.binding
            == {
                "plan_sha256": self.binding["plan_sha256"],
                "source_sha256": self.binding["source_sha256"],
            },
            "final_prefix_profile_source_or_rpc_changed",
        )

    @property
    def deployment_withheld(self):
        return self._deployment_withheld

    @property
    def recovery_refused(self):
        return self._recovery_refused

    @property
    def pending_epoch(self):
        if self._pending is None:
            return None
        return deepcopy(
            {
                key: self._pending[key]
                for key in (
                    "request_id",
                    "domain",
                    "started_s",
                    "deadline_s",
                    "started_wall_s",
                    "wall_deadline_s",
                )
            }
        )

    def observation_due(self, state, vehicle, phase, *, body):
        """Read-only trigger hint for the producer's separate actual witness.

        The observer still decides the exact trigger after servicing an older
        reply. A hint may therefore be discarded, but cannot create a request.
        """
        if body == "booster":
            return not self._booster_seen
        require(body == "ascent", "final_prefix_observation_body")
        if phase not in ASCENT_PHASES or self._deployment_withheld:
            return False
        available = sum(
            actual.available
            for engine, actual in zip(vehicle.engines, state.engine_states)
            if not engine.name.startswith("rcs_")
        )
        prior = self._availability.get(phase)
        return (not self._baseline and state.time_s >= self.policy.baseline_ascent_time_s) or (
            prior is not None and available < prior
        )

    def _observation_wall_start(self, supplied):
        now = self.clock()
        started = now if supplied is None else supplied
        require(number(started) and started <= now, "final_prefix_observation_original_wall_epoch")
        return started

    def _now(self, state):
        now = float(state.time_s)
        require(
            number(now)
            and (self._clock is None or now + 1e-9 >= self._clock)
            and wire(self.binding) == self._binding_json,
            "final_prefix_actual_clock_or_binding_changed",
        )
        self._clock = now
        self._pump(now)
        return now

    def _trigger(self, domain, facts, wall, reason, state, vehicle, phase):
        now = facts["time_s"]
        window = self.policy.ascent_window_s if domain == "ascent" else self.policy.booster_window_s
        identity = self.binding["run_id"] + "-" + domain + "-" + str(len(self.requests) + 1)
        value = request(
            identity,
            domain,
            now,
            now + window,
            {
                "schema": OBSERVATION_SCHEMA,
                "scope_sha256": self.binding["scope_sha256"],
                "facts": facts,
                "allowed_actions": list(ACTIONS[domain]),
            },
        )
        row = {
            "request_id": identity,
            "domain": domain,
            "request": value,
            "request_sha256": digest(value),
            "started_s": now,
            "deadline_s": now + window,
            "started_wall_s": wall,
            "wall_deadline_s": wall + window,
            "trigger_reason": reason,
            "observation": {
                "body": domain,
                "phase": phase,
                "state": asdict(state),
                "vehicle": asdict(vehicle),
                "state_sha256": digest(asdict(state)),
                "vehicle_sha256": digest(asdict(vehicle)),
                "facts_sha256": digest(facts),
                "provider_input": False,
                "channel": "idealized_current_engineering_telemetry",
            },
        }
        self.requests.append(deepcopy(row))
        self._queued.append(row)
        self.events.append(
            {
                "event": "supervision_trigger",
                "request_id": identity,
                "domain": domain,
                "time_s": now,
                "reason": reason,
                "physical_clock_paused": False,
            }
        )

    def _begin_queued(self, now):
        if self._pending is not None:
            return
        while self._queued:
            row = self._queued[0]
            if (
                now + self.policy.application_reserve_s >= row["deadline_s"]
                or self.clock() + self.policy.application_reserve_s >= row["wall_deadline_s"]
            ):
                self._queued.pop(0)
                self._unavailable(row, now, "original_epoch_expired_while_rpc_busy")
                continue
            if self.client.pending is not None:
                return
            self.client.begin(row["request"], started_wall_s=row["started_wall_s"])
            self._pending = self._queued.pop(0)
            return

    def _unavailable(self, row, now, reason, result=None):
        if row["domain"] == "ascent":
            self._deployment_withheld = True
        self.judgments.append(
            {
                "request_id": row["request_id"],
                "domain": row["domain"],
                "time_s": now,
                "accepted": False,
                "action": None,
                "reason": reason,
                "result": deepcopy(result),
                "native_model_judgment": False,
                "safety_path": "withhold_future_deployment"
                if row["domain"] == "ascent"
                else "preapproved_numerical_sea_gnc_continues",
                "physical_execution_authorized": False,
            }
        )

    def _pump(self, now):
        if self._pending is None:
            return
        row = self._pending
        require(
            self.client.pending is not None
            and self.client.pending["frame"]["request"]["request_id"] == row["request_id"]
            and self.client.pending["frame"]["request"]["domain"] == row["domain"],
            "final_prefix_own_pending_rpc_changed",
        )
        result = self.client.poll(now)
        if result is None:
            return
        self._pending = None
        if (
            now + self.policy.application_reserve_s >= row["deadline_s"]
            or self.clock() + self.policy.application_reserve_s >= row["wall_deadline_s"]
        ):
            self._unavailable(row, now, "original_application_reserve_consumed", result)
            return
        try:
            self._validate_result(row, result)
        except (ValueError, KeyError, TypeError):
            self._unavailable(row, now, "invalid_or_unavailable_judgment_no_retry", result)
            return
        action = result["action"]
        if action == "withhold_deployment":
            self._deployment_withheld = True
        elif action == "inhibit_recovery":
            self._recovery_refused = True
        self.judgments.append(
            {
                "request_id": row["request_id"],
                "domain": row["domain"],
                "time_s": now,
                "accepted": True,
                "action": action,
                "reason": "checked_current_mission_proposal",
                "result": deepcopy(result),
                "native_model_judgment": self.binding["mode"] == "live",
                "safety_path": None,
                "physical_execution_authorized": False,
            }
        )

    def _validate_result(self, row, result):
        return validate_saved_judgment(row, result, mode=self.binding["mode"])

    def observe_ascent(self, state, vehicle, phase, *, observation_started_wall_s=None):
        started = self._observation_wall_start(observation_started_wall_s)
        now = self._now(state)
        if phase in ASCENT_PHASES and not self._deployment_withheld:
            facts = numerical_projection(
                state, vehicle, body="ascent", deployment_allowed=not self._deployment_withheld
            )
            count = facts["observed_available_engine_count"]
            prior = self._availability.get(phase)
            self._availability[phase] = count
            baseline = not self._baseline and now >= self.policy.baseline_ascent_time_s
            if baseline:
                self._baseline = True
            decreased = prior is not None and count < prior
            if baseline or decreased:
                self._trigger(
                    "ascent",
                    facts,
                    started,
                    "baseline_current_state_check"
                    if baseline
                    else "observed_main_availability_count_decreased",
                    state,
                    vehicle,
                    phase,
                )
        self._begin_queued(now)
        return "withhold_deployment" if self._deployment_withheld else None

    def observe_booster(
        self, state, vehicle, phase, *, tower_ready=True, observation_started_wall_s=None
    ):
        started = self._observation_wall_start(observation_started_wall_s)
        now = self._now(state)
        require(
            type(phase) is str and phase.startswith("booster_"),
            "final_prefix_booster_phase_required",
        )
        if not self._booster_seen:
            self._booster_seen = True
            facts = numerical_projection(state, vehicle, body="booster", tower_ready=tower_ready)
            self._trigger(
                "booster",
                facts,
                started,
                "first_actual_separated_state_continuation_review",
                state,
                vehicle,
                phase,
            )
        self._begin_queued(now)
        return "inhibit_recovery" if self._recovery_refused else None

    def record(self):
        return deepcopy(
            {
                "schema": SCHEMA,
                **self.binding,
                "policy": self.policy.record(),
                "requests": self.requests,
                "judgments": self.judgments,
                "queued": self._queued,
                "pending_epoch": self.pending_epoch,
                "events": self.events,
                "deployment_withheld": self._deployment_withheld,
                "recovery_refused": self._recovery_refused,
                "booster_initial_burn_selected_by_ai": False,
                "numerical_gnc_waited_for_model": False,
                "provider_calls_by_worker": 0,
                "physical_clock_paused": False,
                "state_or_inventory_assigned": False,
                "execution_permission_granted": False,
            }
        )
