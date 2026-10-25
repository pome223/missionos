"""Opt-in actual launch/deployment prefix; no return or operational authority.

The callback receives an immutable, sanitized summary at actual release slots
0 and 13 and returns only ``continue_deployment`` or ``withhold_deployment``.
It has no plant reference or state-writing interface. A callback failure closes
the sequencer once; it is not retried. Actual rigid-release receipts alone
produce inventory generations. The host-supplied source manifest is provenance
assertion, not an authenticated worker receipt, approval or execution grant.

Pending callbacks must poll ONE host-owned request. The trusted host is
responsible for wall-clock pacing during native calls; this numeric runtime
does not sleep, create providers, or prove a model deadline from simulated time.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from types import MappingProxyType

import numpy as np

from . import starship_restored_contract as contract


@dataclass(frozen=True)
class RestoredDeploymentPending:
    """A host poll is pending; never an AI action, approval or new request."""


PENDING_DEPLOYMENT_DECISION = RestoredDeploymentPending()
DECISION_WINDOW_S = 35.


def _native(value):
    def numeric(item):
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(type(item).__name__)
    return json.loads(json.dumps(value, default=numeric, allow_nan=False))


class RestoredPrefixRuntime:
    """Single-use, exact-typed experimental deployment driver.

    The configuration is copied into immutable canonical bytes. ``origin()``
    is available only after an actual closed prefix has passed the separate
    ActualRestoredOrigin constructor; it confers no operational permission.
    """

    __slots__ = ("_profile_raw", "_profile_json", "_source_json", "_settings_json",
        "_control_json", "_settings", "_control", "_horizon", "_decision",
        "_started", "_active", "_closed", "_withheld", "_history", "_receipts",
        "_decisions", "_checked", "_schedule", "_origin", "_finished", "_pending", "_final_visual", "_final_visual_binding_json", "_final_supervision", "_final_supervision_binding_json")

    def __init__(self, profile_raw_bytes, expected_source_sha256, ascent_settings,
                 booster_control_settings, maximum_simulation_time_s, deployment_decision, *,
                 final_visual_observer=None, final_prefix_supervision=None):
        from .starship_state_mission import ExperimentalStateMissionAscentSettings
        from .starship_booster_terminal_control import BoosterTerminalRecoverySettings
        if (type(profile_raw_bytes) is not bytes or sha256(profile_raw_bytes).hexdigest() != contract.PROFILE_SHA256
                or type(ascent_settings) is not ExperimentalStateMissionAscentSettings
                or type(booster_control_settings) is not BoosterTerminalRecoverySettings
                or type(maximum_simulation_time_s) not in (int, float)
                or not math.isfinite(maximum_simulation_time_s)
                or not 3336. < maximum_simulation_time_s <= 7500. or not callable(deployment_decision)):
            raise ValueError("restored_prefix_requires_explicit_frozen_configuration")
        contract._manifest(expected_source_sha256)
        if "src/runtime/starship_restored_prefix.py" not in expected_source_sha256:
            raise ValueError("restored_prefix_source_missing")
        contract._settings({"schema":"missionos.experimental_state_mission_ascent.v1",
            "settings":ascent_settings.record()}, booster_control_settings.record())
        if final_visual_observer is not None:
            from .starship_final_visual import FinalVisualObserver, digest
            if (type(final_visual_observer) is not FinalVisualObserver
                    or final_visual_observer.binding["source_sha256"] != digest(expected_source_sha256)
                    or contract._encode(final_visual_observer.collector.world.profile)
                        != contract._encode(contract._read(profile_raw_bytes))):
                raise ValueError("restored_prefix_requires_exact_bound_final_visual_observer")
        if final_prefix_supervision is not None:
            from .starship_final_prefix_supervision import FinalPrefixSupervision
            if type(final_prefix_supervision) is not FinalPrefixSupervision:
                raise ValueError("restored_prefix_exact_final_prefix_supervision_required")
            final_prefix_supervision.validate_scope(contract._read(profile_raw_bytes), expected_source_sha256)
            if final_visual_observer is not None and any(final_prefix_supervision.binding[key]
                    != final_visual_observer.binding[key] for key in ("run_id", "plan_sha256", "source_sha256", "mode")):
                raise ValueError("restored_prefix_supervision_visual_binding_changed")
        self._final_supervision = final_prefix_supervision
        self._final_supervision_binding_json = (None if final_prefix_supervision is None else
            contract._encode(final_prefix_supervision.binding))
        self._final_visual = final_visual_observer
        self._final_visual_binding_json = (None if final_visual_observer is None else
            contract._encode(final_visual_observer.binding))
        self._profile_raw = profile_raw_bytes
        self._profile_json = contract._encode(contract._read(profile_raw_bytes))
        self._source_json = contract._encode(expected_source_sha256)
        self._settings_json = contract._encode(ascent_settings.record())
        self._control_json = contract._encode(booster_control_settings.record())
        self._settings, self._control = ascent_settings, booster_control_settings
        self._horizon, self._decision = float(maximum_simulation_time_s), deployment_decision
        self._started = self._active = self._closed = self._withheld = self._finished = False
        self._history, self._receipts, self._decisions, self._checked = [], [], [], set()
        self._schedule, self._origin = None, None
        self._pending = None

    @property
    def ascent_settings(self):
        return self._settings

    @property
    def booster_control_settings(self):
        return self._control

    @property
    def maximum_simulation_time_s(self):
        return self._horizon

    @property
    def final_prefix_supervision(self):
        return self._final_supervision

    def _validate_final_supervision(self):
        if self._final_supervision is not None:
            if contract._encode(self._final_supervision.binding) != self._final_supervision_binding_json:
                raise ValueError("restored_prefix_supervision_binding_changed")
            self._final_supervision.validate_scope(contract._read(self._profile_json), contract._read(self._source_json))

    def observe_supervision(self, state, vehicle, phase, *, observation_started_wall_s=None):
        self._validate_final_supervision()
        if self._final_supervision is None:
            return None
        action = self._final_supervision.observe_ascent(state, vehicle, phase,
            observation_started_wall_s=observation_started_wall_s)
        if action == "withhold_deployment" and self._active and not self._closed:
            self._close_from_supervision(state)
        return action

    def _close_from_supervision(self, state):
        self._validate_final_supervision()
        count = len(self._receipts)
        if (self._final_supervision is None or not self._final_supervision.deployment_withheld
                or not self._active or self._closed or count not in (0, 13, 26) or self._pending is not None
                or not self._schedule["final_gate_time_s"]+5. <= state.time_s < self._schedule["return_request_time_s"]):
            raise ValueError("restored_prefix_supervision_withhold_requires_actual_supported_inventory")
        evidence = self._final_supervision.record()
        self._decisions.append({"summary": {"time_s": float(state.time_s), "released_count": count,
                "retained_count": 26-count, "release_ready": False, "sequencer_state": "skipped",
                "notice": "Ascent supervision withheld future releases; actual rigid inventory retained.",
                "decision_started_s": None, "decision_deadline_s": None},
            "action": "withhold_deployment", "callback_error_type": None, "pending": None,
            "origin": "final_ascent_supervision_withhold_not_approval_proof",
            "final_prefix_supervision_judgments_sha256": sha256(contract._encode(evidence["judgments"])).hexdigest(),
            "final_prefix_supervision_judgment_count": len(evidence["judgments"])})
        self._checked.add(count)
        self._withheld = self._closed = True

    @property
    def final_visual_observer(self):
        return self._final_visual

    def _validate_final_visual(self):
        if self._final_visual is not None and (
                contract._encode(self._final_visual.binding) != self._final_visual_binding_json
                or contract._encode(self._final_visual.collector.world.profile) != self._profile_json):
            raise ValueError("restored_prefix_final_visual_binding_changed")

    def visual_release_allowed(self, now_s):
        self._validate_final_visual()
        return self._final_visual is None or self._final_visual.release_allowed(now_s)

    def observe_visual(self, state, vehicle, satellites, *, release_ready):
        """Observe only present plant records; a stop closes supported inventory13.

        This hook has no provider, state replacement or inventory setter. The
        ordinary count13 deployment judgment stays separate and follows a valid
        visual resume; a visual or Rules stop is preserved as its own cause.
        """
        self._validate_final_visual()
        if self._final_visual is None:
            return None
        if not self._active or self._closed or len(satellites) != len(self._receipts):
            raise ValueError("restored_prefix_visual_observation_order_or_inventory")
        action = self._final_visual.observe(state, vehicle, satellites,
            phase="orbital_coast", numeric_release_ready=release_ready,
            return_deadline_s=self._schedule["return_request_time_s"])
        if action == "withhold_deployment":
            if len(self._receipts) != 13:
                raise ValueError("restored_prefix_visual_stop_outside_supported_inventory")
            return self.decide(state, 13, release_ready=release_ready, sequencer_state="held")
        return None

    @property
    def closed(self):
        return self._closed

    def validate_scope(self, profile, settings, controls, duration_s):
        if (contract._encode(profile) != self._profile_json
                or type(settings) is not type(self._settings)
                or contract._encode(settings.record()) != self._settings_json
                or type(controls) is not type(self._control)
                or contract._encode(controls.record()) != self._control_json
                or type(duration_s) not in (int, float) or duration_s != self._horizon):
            raise ValueError("restored_prefix_profile_settings_or_horizon_mismatch")

    def begin(self, profile, settings, duration_s):
        self.validate_scope(profile, settings, self._control, duration_s)
        if self._started:
            raise ValueError("restored_prefix_is_single_use")
        self._started = True

    def settled(self, state, schedule):
        self._validate_final_supervision()
        if not self._started or self._active or self._closed:
            raise ValueError("restored_prefix_settled_handoff_order")
        if (set(schedule) != {"final_gate_time_s","first_release_time_s","return_request_time_s"}
                or abs(state.time_s-schedule["final_gate_time_s"]-5.) > 1e-8):
            raise ValueError("restored_prefix_requires_actual_settled_clock")
        self._schedule = _native(schedule)
        self._history.append({"state":_native(asdict(state)),"retained_count":26})
        self._active = True
        if self._final_supervision is not None and self._final_supervision.deployment_withheld:
            self._close_from_supervision(state)

    def decide(self, state, released_count, *, release_ready, sequencer_state):
        self._validate_final_visual()
        if (not self._active or self._closed or released_count != len(self._receipts)
                or released_count not in (0,13) or released_count in self._checked
                or type(release_ready) is not bool):
            raise ValueError("restored_prefix_decision_order_or_inventory")
        if not self._schedule["first_release_time_s"] <= state.time_s < self._schedule["return_request_time_s"]:
            raise ValueError("restored_prefix_decision_outside_actual_schedule")
        summary = {"time_s":float(state.time_s), "released_count":released_count,
            "retained_count":26-released_count,"release_ready":release_ready,
            "sequencer_state":sequencer_state or "running", "notice":"",
            "decision_started_s":self._pending["started_s"] if self._pending else None,
            "decision_deadline_s":self._pending["deadline_s"] if self._pending else None}
        failure, origin, visual_digest = None, "external_callback_not_model_or_approval_proof", None
        if self._final_visual is not None and released_count == 13:
            if not self._final_visual.deployment_decision_ready:
                # A poll of the visual host is not an ordinary deployment model
                # request and must not consume or restart that request's epoch.
                return PENDING_DEPLOYMENT_DECISION
            if self._final_visual.withhold_deployment:
                evidence = self._final_visual.record()
                visual_digest = sha256(contract._encode(evidence)).hexdigest()
                origin = "final_visual_observation_withhold_not_approval_proof"
                summary["notice"] = "Independent observation episode ended with further deployment withheld."
        if visual_digest is not None:
            action = "withhold_deployment"
        elif self._pending is not None and state.time_s >= self._pending["deadline_s"]-1e-9:
            action, failure = "withhold_deployment", "SimulationDecisionDeadline"
        else:
            try:
                action = self._decision(MappingProxyType(summary))
                if type(action) is not RestoredDeploymentPending and (type(action) is not str
                        or action not in ("continue_deployment","withhold_deployment")):
                    raise ValueError("restored_prefix_callback_action_outside_scope")
            except Exception as error:
                action, failure = "withhold_deployment", type(error).__name__
        if type(action) is RestoredDeploymentPending:
            if self._pending is None:
                self._pending = {"released_count":released_count,"started_s":float(state.time_s),
                    "deadline_s":min(float(state.time_s)+DECISION_WINDOW_S,self._schedule["return_request_time_s"]),
                    "poll_count":0}
            self._pending["poll_count"] += 1
            return PENDING_DEPLOYMENT_DECISION
        self._checked.add(released_count)
        decision = {"summary":summary,"action":action,"callback_error_type":failure,
            "pending":deepcopy(self._pending), "origin":origin}
        if visual_digest is not None:
            decision["final_visual_observation_sha256"] = visual_digest
        self._decisions.append(decision)
        self._pending = None
        if action == "withhold_deployment":
            self._withheld = self._closed = True
        return action

    def needs_decision(self, released_count):
        return self._active and not self._closed and released_count in (0,13) and released_count not in self._checked

    def released(self, receipt):
        if not self._active or self._closed or len(self._receipts) >= 26:
            raise ValueError("restored_prefix_release_after_closed_or_without_handoff")
        row = _native(receipt)
        before, after = row["parent_before_state"], row["parent_after_state"]
        count = 26-len(self._receipts)
        if before["time_s"] != after["time_s"] or before["time_s"] < self._history[-1]["state"]["time_s"]:
            raise ValueError("restored_prefix_release_clock_mismatch")
        self._history.extend(({"state":before,"retained_count":count},
                              {"state":after,"retained_count":count-1}))
        self._receipts.append(row)
        if len(self._receipts) == 26:
            self._closed = True

    def finish(self, result):
        self._validate_final_visual()
        self._validate_final_supervision()
        if self._finished:
            raise ValueError("restored_prefix_already_finished")
        self._finished = True
        evidence = {"schema":"missionos.starship_restored_prefix.v1","closed":self._closed,
            "decisions":deepcopy(self._decisions),"release_generation":len(self._receipts),
            "pending_decision":deepcopy(self._pending),"decision_window_s":DECISION_WINDOW_S,
            "state_reset":False,"old_certificate_admitted":False,"mission_authority_granted":False,
            "model_calls_inferred":False,"handoff_record":None,"handoff_record_sha256":None,"origin":None,
            "origin_validation_issue":None}
        if self._closed:
            if result["outcome"]["termination"] != "restored_deployment_handoff" or result["outcome"]["phase"] != "orbital_coast":
                raise ValueError("restored_prefix_closed_without_actual_handoff")
            final = _native(result["final_state"])
            history = deepcopy(self._history)
            current = {"state":final,"retained_count":26-len(self._receipts)}
            if not history or history[-1] != current:
                history.append(current)
            record = {"schema":contract.RECORD_SCHEMA,"source_sha256":contract._read(self._source_json),
                "profile_sha256":contract.PROFILE_SHA256,
                "experimental_mission_ascent":_native(result["experimental_mission_ascent"]),
                "booster_control_settings":contract._read(self._control_json),"phase":"orbital_coast",
                "final_state":final,"final_vehicle":_native(result["final_vehicle"]),"state_history":history,
                "inventory":{"schema":contract.INVENTORY_SCHEMA,"total_count":26,
                    "released_count":len(self._receipts),"retained_count":26-len(self._receipts),
                    "generation":len(self._receipts),"sequencer_state":"skipped" if self._withheld else "running",
                    "pending_action":None,"observed_time_s":final["time_s"],"release_receipts":deepcopy(self._receipts)}}
            raw = contract._encode(record)
            digest = sha256(raw).hexdigest()
            evidence.update(handoff_record=record,handoff_record_sha256=digest)
            try:
                self._origin = contract.ActualRestoredOrigin(digest,raw,self._profile_raw,contract._read(self._source_json))
            except ValueError as error:
                # Preserve the actual prefix before the host asks origin() and
                # stops its cohort. This is invalid evidence, never a fallback.
                evidence["origin_validation_issue"] = str(error)[:160]
            else:
                evidence["origin"] = self._origin.record()
        return evidence

    def origin(self):
        if self._origin is None:
            raise ValueError("restored_prefix_has_no_validated_closed_actual_origin")
        return self._origin


def restored_prefix_mission(profile, runtime, *, return_sites, splashdown_goal, on_tick=None):
    """Invoke the common plant only when iterated by the concurrent API.

    This synchronous entry integrates a newly registered physical trial. Pure
    tests must replace the plant boundary; constructing the runtime does not fly.
    """
    if type(runtime) is not RestoredPrefixRuntime:
        raise ValueError("restored_prefix_requires_exact_runtime")
    if on_tick is not None and not callable(on_tick):
        raise ValueError("restored_prefix_requires_callable_tick_observer")
    from . import starship_physics as env
    from .starship_experimental_continuation import development_settings
    from .starship_retained_return import TRIMMED_POLICY_ID
    from .starship_sixdof_mission import simulate_concurrent
    peak, peak_altitude = None, -math.inf
    def observed(tick):
        nonlocal peak, peak_altitude
        if tick.body_id == "booster":
            altitude = env.ecef_to_geodetic(tick.state.r_eci_m)[2]
            if altitude > peak_altitude:
                peak, peak_altitude = _native(asdict(tick.state)), altitude
        if on_tick is not None:
            on_tick(tick)
    result = simulate_concurrent(profile, scenario="launch", duration_s=runtime.maximum_simulation_time_s,
        return_policy=TRIMMED_POLICY_ID, booster_policy="fixed_v1",
        _development_return_qualification=development_settings(),return_sites=return_sites,
        splashdown_goal=splashdown_goal,experimental_ascent=runtime.ascent_settings,
        booster_terminal_recovery=runtime.booster_control_settings,restored_runtime=runtime,on_tick=observed)
    booster = result.get("booster_run")
    if booster is not None:
        final = _native(booster["final_state"])
        if env.ecef_to_geodetic(final["r_eci_m"])[2] > peak_altitude:
            peak = final
        booster["runner_booster_peak_witness"] = {"state":peak,
            "origin":"all_yielded_physics_ticks_and_final_state; no extra plant step",
            "not_independent_physics_reexecution":True}
    return result
