"""Development-only observed deployment -> forecast boundary; no dispatch grant.

The counter is the simulator's synthetic confirmed-separation channel, not an
acknowledgement. Recorded release times are observation times, not exact events.
The existing M1 adapter and physical qualification sources are unchanged.
"""

from copy import deepcopy
from dataclasses import fields
from hashlib import sha256
import json
import math

from .starship_sixdof import State6DOF, state_from_dict
from .starship_replanning import estimate, uncertainty_origins

SCHEMA = "missionos.deployment_return_handoff.v1"
BINDING_SCHEMA = "missionos.deployment_forecast_binding.v1"
MAX_OBSERVATION_AGE_S = 2.001
ACTIONS = {"hold", "continue", "stop_deployment", "collect_status"}
SEQUENCERS = {"running", "held", "skipped", "inhibited"}


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _identity(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid_handoff_identity")
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_handoff_clock")
    return value


class DeploymentTracker:
    def __init__(self, total_count, authority_sha256):
        if type(total_count) is not int or not 1 <= total_count <= 26:
            raise ValueError("invalid_deployment_inventory")
        self.total_count = total_count
        self.authority_sha256 = _identity(authority_sha256)
        self._generation, self._clock = 0, None
        self._latest, self._pending = None, None
        self._history, self._releases, self._issued = [], [], set()

    @property
    def generation(self):
        return self._generation

    @property
    def history(self):
        return deepcopy(self._history)

    def _advance(self, now):
        now = _time(now)
        if self._clock is not None and now < self._clock:
            raise ValueError("nonmonotonic_deployment_clock")
        return now

    def observe(self, row):
        if type(row) is not dict:
            raise ValueError("invalid_deployment_observation")
        try:
            observed = _copy({k: row[k] for k in ("time_s", "released_count", "sequencer_state")})
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid_deployment_observation") from error
        count, sequence = observed["released_count"], observed["sequencer_state"]
        if (type(count) is not int or not 0 <= count <= self.total_count
                or type(sequence) is not str or sequence not in SEQUENCERS):
            raise ValueError("invalid_deployment_observation")
        _time(observed["time_s"])
        if observed == self._latest:
            return  # Director.confirm may repeat exactly the same observation.
        now = self._advance(observed["time_s"])
        prior = 0 if self._latest is None else self._latest["released_count"]
        if (self._latest is None and count != 0) or not prior <= count <= prior + 1:
            raise ValueError("separation_observation_gap_or_decrease")
        if self._latest is not None and now <= self._latest["time_s"]:
            raise ValueError("conflicting_observation_at_same_time")
        if self._latest is not None and (count != prior or sequence != self._latest["sequencer_state"]):
            self._generation += 1
        if count != prior:
            self._releases.append({"released_count": count, "observed_time_s": now,
                                   "previous_observation_time_s": self._latest["time_s"]})
        if self._pending is not None and now > self._pending["time_s"]:
            expected = {"hold": {"held"}, "continue": {"running"},
                        "stop_deployment": {"skipped"}, "collect_status": {"held", "skipped", "inhibited"}}
            if sequence in expected[self._pending["action"]]:
                self._pending = None
        self._latest, self._clock = observed, now
        self._history.append({"kind": "observation", **observed})

    def command(self, action, time_s):
        if type(action) is not str or action not in ACTIONS:
            raise ValueError("unsupported_deployment_command")
        now = self._advance(time_s)
        if self._latest is None or self._pending is not None:
            raise ValueError("deployment_command_unconfirmed")
        self._generation += 1  # Resume invalidates before count/state can change.
        self._pending = {"action": action, "time_s": now}
        self._clock = now
        self._history.append({"kind": "command", **self._pending})

    def _closure(self):
        if self._latest is None or self._pending is not None:
            raise ValueError("deployment_observation_or_confirmation_missing")
        sequence = self._latest["sequencer_state"]
        if sequence == "held":
            raise ValueError("resumable_hold_is_not_closed_deployment")
        if self._latest["released_count"] != self.total_count and sequence not in ("skipped", "inhibited"):
            raise ValueError("deployment_not_closed")

    def freeze(self, state_dict, profile_sha256):
        self._closure()
        profile_sha256 = _identity(profile_sha256)
        if type(state_dict) is not dict or set(state_dict) != {f.name for f in fields(State6DOF)}:
            raise ValueError("complete_execution_state_required")
        exact = _copy(state_dict)
        state = state_from_dict(exact)
        age = _time(state.time_s) - self._latest["time_s"]
        if not 0 <= age <= MAX_OBSERVATION_AGE_S:
            raise ValueError("stale_deployment_observation")
        retained = self.total_count - self._latest["released_count"]
        origin = estimate(state)
        origin["retained_count"] = retained
        result = {"schema": SCHEMA, "development_only": True, "dispatch_authorized": False,
                  "inventory_basis": "synthetic_confirmed_separation_counter_v1",
                  "authority_sha256": self.authority_sha256, "profile_sha256": profile_sha256,
                  "generation": self._generation, "total_count": self.total_count,
                  "released_count": self._latest["released_count"], "retained_count": retained,
                  "execution_state": exact, "origin": origin, "history": self.history,
                  "release_observations": deepcopy(self._releases)}
        result["handoff_sha256"] = _digest(result)
        self._issued.add(result["handoff_sha256"])
        return result

    def require_current(self, handoff):
        validate_handoff(handoff)
        self._closure()
        if (handoff["handoff_sha256"] not in self._issued
                or handoff["authority_sha256"] != self.authority_sha256
                or handoff["generation"] != self._generation
                or handoff["total_count"] != self.total_count
                or handoff["released_count"] != self._latest["released_count"]
                or handoff["history"][-1]["sequencer_state"] != self._latest["sequencer_state"]):
            raise ValueError("stale_or_unissued_deployment_handoff")


def validate_handoff(handoff):
    """Replay evidence and check its digest; identity alone is not authority."""
    if type(handoff) is not dict or handoff.get("schema") != SCHEMA:
        raise ValueError("invalid_deployment_handoff")
    try:
        digest = _identity(handoff["handoff_sha256"])
        if digest != _digest({k: v for k, v in handoff.items() if k != "handoff_sha256"}):
            raise ValueError("handoff_digest_mismatch")
        tracker = DeploymentTracker(handoff["total_count"], handoff["authority_sha256"])
        for row in handoff["history"]:
            if row["kind"] == "observation" and set(row) == {"kind", "time_s", "released_count", "sequencer_state"}:
                tracker.observe(row)
            elif row["kind"] == "command" and set(row) == {"kind", "time_s", "action"}:
                tracker.command(row["action"], row["time_s"])
            else:
                raise ValueError("invalid_deployment_history")
        expected = tracker.freeze(handoff["execution_state"], handoff["profile_sha256"])
        if handoff != expected:
            raise ValueError("deployment_handoff_evidence_mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("invalid_deployment_handoff") from error


def _expected_origin(handoff, expected_origin):
    validate_handoff(handoff)
    origin = handoff["origin"] if expected_origin is None else _copy(expected_origin)
    if _digest(origin) not in {_digest(o) for o in [handoff["origin"], *uncertainty_origins(handoff["origin"]) ]}:
        raise ValueError("unregistered_handoff_origin")
    return origin


def _check_origins(result, origin):
    found = 0
    def walk(value):
        nonlocal found
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "origin":
                    found += 1
                    if _digest(child) != _digest(origin):
                        raise ValueError("forecast_handoff_origin_mismatch")
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(result)
    if not found or type(result) is not dict or _digest(result.get("origin")) != _digest(origin):
        raise ValueError("forecast_origin_missing")
    if result.get("runtime_admission") or result.get("prediction_is_execution"):
        raise ValueError("forecast_is_not_dispatch_authority")


def _binding(handoff, origin):
    return {"schema": BINDING_SCHEMA, "handoff_sha256": handoff["handoff_sha256"],
            "authority_sha256": handoff["authority_sha256"], "profile_sha256": handoff["profile_sha256"],
            "generation": handoff["generation"], "retained_count": handoff["retained_count"],
            "origin_sha256": _digest(origin), "dispatch_authorized": False}


def bind_handoff(result, handoff, *, expected_origin=None):
    """Copy a forecast and attach its observed deployment provenance."""
    origin = _expected_origin(handoff, expected_origin)
    bound = _copy(result)
    _check_origins(bound, origin)
    if "deployment_handoff" in bound:
        raise ValueError("forecast_already_bound")
    bound["deployment_handoff"] = _binding(handoff, origin)
    return bound


def verify_binding(result, handoff, *, expected_origin=None):
    origin = _expected_origin(handoff, expected_origin)
    _check_origins(result, origin)
    if _digest(result.get("deployment_handoff")) != _digest(_binding(handoff, origin)):
        raise ValueError("forecast_handoff_binding_mismatch")
