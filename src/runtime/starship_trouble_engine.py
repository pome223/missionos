"""Opt-in, seeded plant faults with a separate current-health observation channel.

The distribution and delay bounds below are synthetic engineering assumptions,
not incident rates, measured hardware limits, or an accident reenactment. A
manifest is frozen before flight. It belongs to the harness, never a model's
input. No future fault is injected into a prediction of an observed state.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import math
from types import MappingProxyType

SCHEMA = "missionos.starship_trouble_manifest.v1"
GENERATOR = "sha256_named_draw_v1"
BASIS = "synthetic_distribution_not_historical_incident_rates"
# body, first eligible phase, inclusive delay range, component selection.
FAMILIES = MappingProxyType({
    "ascent_engine_out": ("ship", "stack_ascent", (20., 100.), tuple(f"booster_main_{i}" for i in range(33))),
    "ship_engine_out": ("ship", "ship_ascent", (5., 60.), tuple(f"ship_main_{i}" for i in range(6))),
    "rcs_channel_loss": ("ship", "orbital_coast", (0., 90.), tuple(f"rcs_{a}_{s}" for a in range(3) for s in (-1, 1))),
    "flap_jam": ("ship", "ballistic_return", (0., 60.), tuple(f"movable_panel_{i}" for i in range(4))),
    "payload_jam": ("ship", "orbital_coast", (0., 10.), ("dispenser",)),
    "tower_unavailable": ("booster", "booster_boostback_slew", (0., 0.), ("tower",)),
    "booster_restart_failure": ("booster", "booster_boostback", (0., 0.), tuple(f"booster_main_{i}" for i in range(13))),
    "return_propulsion_80": ("ship", "accepted_return_dispatch", (0., 0.), ("landing_engine_group",)),
    "return_sensor_gain_80": ("ship", "accepted_return_dispatch", (0., 0.), ("axial_accelerometer",)),
})
# No verified actuator-jam precedent is claimed. This family remains an explicit
# synthetic sensitivity fixture; it is not in the historically motivated pool.
DEFAULT_FAMILIES = tuple(family for family in FAMILIES
                         if family not in ("flap_jam", "return_propulsion_80", "return_sensor_gain_80"))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _draw(seed, name):
    data = f"{GENERATOR}\0{seed}\0{name}".encode("ascii")
    return int.from_bytes(sha256(data).digest()[:8], "big") / 2**64


def _sample_events(seed, pool, maximum, nominal_probability):
    if maximum == 0 or _draw(seed, "nominal") < nominal_probability:
        return []
    count = 1 + min(maximum - 1, int(_draw(seed, "event_count") * maximum))
    selected = sorted(pool, key=lambda family: (_draw(seed, f"select.{family}"), family))[:count]
    rows = []
    for index, family in enumerate(selected):
        body, phase, bounds, targets = FAMILIES[family]
        target = targets[min(len(targets)-1, int(_draw(seed, f"target.{family}") * len(targets)))]
        delay = round(bounds[0] + (bounds[1]-bounds[0]) * _draw(seed, f"delay.{family}"), 6)
        rows.append(dict(id=f"fault_{index+1:02d}", family=family, body=body,
                         phase=phase, delay_s=delay, target=target))
    return rows


def validate_manifest(value):
    """Return a detached canonical JSON copy; reject unknown fields/families."""
    fields = {"schema", "generator", "parameter_basis", "seed", "allowed_families",
              "max_events", "nominal_probability", "events"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid_trouble_manifest_shape")
    if value["schema"] != SCHEMA or value["parameter_basis"] != BASIS or value["generator"] not in (GENERATOR, "explicit_fixture_v1"):
        raise ValueError("invalid_trouble_manifest_identity")
    seed, pool, maximum, nominal = (value[k] for k in ("seed", "allowed_families", "max_events", "nominal_probability"))
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("invalid_trouble_seed")
    if type(pool) is not list or any(type(x) is not str or x not in FAMILIES for x in pool) or pool != sorted(set(pool)):
        raise ValueError("invalid_trouble_allowed_families")
    if type(maximum) is not int or not 0 <= maximum <= len(pool) or not _number(nominal) or not 0 <= nominal <= 1:
        raise ValueError("invalid_trouble_distribution")
    events = value["events"]
    if type(events) is not list or len(events) > maximum:
        raise ValueError("invalid_trouble_events")
    seen = set()
    for index, event in enumerate(events):
        if type(event) is not dict or set(event) != {"id", "family", "body", "phase", "delay_s", "target"}:
            raise ValueError("invalid_trouble_event_shape")
        family = event["family"]
        if type(family) is not str or family not in pool or family in seen or event["id"] != f"fault_{index+1:02d}":
            raise ValueError("invalid_trouble_event_identity")
        body, phase, bounds, targets = FAMILIES[family]
        if event["body"] != body or event["phase"] != phase or event["target"] not in targets:
            raise ValueError("invalid_trouble_target")
        if not _number(event["delay_s"]) or not bounds[0] <= event["delay_s"] <= bounds[1]:
            raise ValueError("invalid_trouble_delay")
        seen.add(family)
    if value["generator"] == GENERATOR and events != _sample_events(seed, pool, maximum, nominal):
        raise ValueError("trouble_schedule_does_not_match_seed")
    return json.loads(_json(value))


def make_manifest(seed, *, allowed_families=None, max_events=1, nominal_probability=.2):
    """Sample once. Event count is uniform 1..max_events on non-nominal draws."""
    if allowed_families is None:
        allowed_families = list(DEFAULT_FAMILIES)
    if not isinstance(allowed_families, (list, tuple)) or any(type(x) is not str for x in allowed_families):
        raise ValueError("invalid_trouble_allowed_families")
    pool = sorted(allowed_families)
    value = dict(schema=SCHEMA, generator="explicit_fixture_v1", parameter_basis=BASIS,
                 seed=seed, allowed_families=pool, max_events=max_events,
                 nominal_probability=nominal_probability, events=[])
    validate_manifest(value)
    value.update(generator=GENERATOR, events=_sample_events(seed, pool, max_events, nominal_probability))
    return validate_manifest(value)


def fixture_manifest(events):
    """Explicit test schedule, visibly distinct from a randomized trial.

    Each entry supplies family, delay_s and target. The registry supplies the
    body and phase; fixture callers cannot silently move a fault to another body.
    """
    if type(events) is not list or any(type(row) is not dict or set(row) != {"family", "delay_s", "target"} for row in events):
        raise ValueError("invalid_trouble_fixture")
    rows = []
    for i, row in enumerate(events):
        if type(row["family"]) is not str or row["family"] not in FAMILIES:
            raise ValueError("invalid_trouble_fixture_family")
        body, phase, _, _ = FAMILIES[row["family"]]
        rows.append(dict(id=f"fault_{i+1:02d}", body=body, phase=phase, **row))
    return validate_manifest(dict(schema=SCHEMA, generator="explicit_fixture_v1", parameter_basis=BASIS,
        seed=0, allowed_families=sorted({row["family"] for row in rows}), max_events=len(rows),
        nominal_probability=0., events=rows))


def manifest_digest(value):
    return sha256(_json(validate_manifest(value)).encode()).hexdigest()


class TroubleEngine:
    """One flight's mutable executor, holding an immutable private schedule.

    Call before_step before observations/control at each plant tick, limit_step
    before integration, and command after control. Phase starts are first observed
    entries, never inferred from a future nominal timeline. A phase not reached
    cannot activate its fault. Each body has its own monotonic clock.
    """
    def __init__(self, manifest):
        self._manifest_json = _json(validate_manifest(manifest))
        self.manifest_sha256 = sha256(self._manifest_json.encode()).hexdigest()
        self._events = json.loads(self._manifest_json)["events"]
        self._phase_starts, self._last_time, self._applied = {}, {}, set()
        self._receipts, self._jams = [], {}

    def _clock(self, time_s, body):
        if body not in ("ship", "booster") or not _number(time_s) or time_s < 0:
            raise ValueError("invalid_trouble_clock")
        if time_s < self._last_time.get(body, time_s):
            raise ValueError("trouble_clock_reversed")

    def before_step(self, state, vehicle, body, phase):
        self._clock(state.time_s, body)
        if not isinstance(phase, str) or not phase:
            raise ValueError("invalid_trouble_phase")
        if len(state.engine_states) != len(vehicle.engines) or len(state.flap_angles_rad) != len(vehicle.aero_panels):
            raise ValueError("trouble_state_vehicle_mismatch")
        self._last_time[body] = state.time_s
        start = self._phase_starts.setdefault((body, phase), state.time_s)
        for event in self._events:
            if (event["id"] in self._applied or event["body"] != body or event["phase"] != phase
                    or state.time_s < start + event["delay_s"]):
                continue
            if event["family"] == "return_propulsion_80":
                names = {f"ship_main_{i}" for i in range(3)}
                if not names.issubset({e.name for e in vehicle.engines}):
                    raise ValueError("trouble_return_engine_group_missing")
                effect = [dict(component=e.name, max_thrust_before_n=e.max_thrust_n,
                               max_thrust_after_n=e.max_thrust_n*.8)
                          for e in vehicle.engines if e.name in names]
                vehicle = replace(vehicle, engines=tuple(
                    replace(e, max_thrust_n=e.max_thrust_n*.8) if e.name in names else e
                    for e in vehicle.engines))
            elif event["family"] == "return_sensor_gain_80":
                effect = [dict(component=event["target"], gain_before=1., gain_after=.8)]
            else:
                state, effect = self._apply(event, state, vehicle, body)
            receipt = dict(event_id=event["id"], family=event["family"], body=body, phase=phase,
                scheduled_at_s=start+event["delay_s"], applied_at_s=state.time_s, effects=effect,
                previous_sha256=self._receipts[-1]["receipt_sha256"] if self._receipts else self.manifest_sha256)
            receipt["receipt_sha256"] = sha256(_json(receipt).encode()).hexdigest()
            self._receipts.append(receipt)
            self._applied.add(event["id"])
        return state, vehicle

    def _apply(self, event, state, vehicle, body):
        family, target = event["family"], event["target"]
        if family in ("ascent_engine_out", "ship_engine_out", "booster_restart_failure", "rcs_channel_loss"):
            targets = [f"{target}_{side}" for side in (-1, 1)] if family == "rcs_channel_loss" else [target]
            by_name = {engine.name: i for i, engine in enumerate(vehicle.engines)}
            if any(name not in by_name for name in targets):
                raise ValueError("trouble_engine_target_missing")
            states, effects = list(state.engine_states), []
            for name in targets:
                i = by_name[name]
                effects.append(dict(component=name, available_before=states[i].available, available_after=False,
                                    throttle_at_injection=states[i].throttle))
                # Loads become zero through the existing availability mechanism;
                # the actual throttle state decays through its existing dynamics.
                states[i] = replace(states[i], available=False)
            return replace(state, engine_states=tuple(states)), effects
        if family == "flap_jam":
            movable = [i for i, panel in enumerate(vehicle.aero_panels) if panel.max_deflection_rad > 0]
            ordinal = int(target.rsplit("_", 1)[1])
            if ordinal >= len(movable):
                raise ValueError("trouble_flap_target_missing")
            i = movable[ordinal]
            name, angle = vehicle.aero_panels[i].name, state.flap_angles_rad[i]
            self._jams[(body, name)] = (angle, state.time_s)
            return state, [dict(component=name, actuator_before="free", actuator_after="jammed",
                                held_angle_rad=angle)]
        key = "payload_release_blocked" if family == "payload_jam" else "tower_ready"
        return state, [dict(component=target, signal=key, before=family == "tower_unavailable", after=family == "payload_jam")]

    def limit_step(self, time_s, dt_s, body, phase):
        """Split at the next known trigger; do not delay activation to a sample."""
        self._clock(time_s, body)
        if not _number(dt_s) or dt_s <= 0 or (body, phase) not in self._phase_starts:
            raise ValueError("invalid_trouble_step")
        start = self._phase_starts[(body, phase)]
        for event in self._events:
            if event["id"] not in self._applied and event["body"] == body and event["phase"] == phase:
                remaining = start+event["delay_s"]-time_s
                if remaining <= 0:
                    raise ValueError("trouble_due_event_requires_before_step")
                dt_s = min(dt_s, remaining)
        return dt_s

    def command(self, command, state, vehicle, body):
        self._clock(state.time_s, body)
        if len(command.flap_angles_rad) != len(vehicle.aero_panels):
            raise ValueError("trouble_command_vehicle_mismatch")
        angles = list(command.flap_angles_rad)
        changed = False
        for i, panel in enumerate(vehicle.aero_panels):
            jam = self._jams.get((body, panel.name))
            if jam is not None and jam[1] <= state.time_s:
                angles[i], changed = jam[0], True
        return replace(command, flap_angles_rad=tuple(angles)) if changed else command

    def _signal_active(self, family, time_s):
        if not _number(time_s) or time_s < 0:
            raise ValueError("invalid_trouble_signal_time")
        return any(row["family"] == family and row["applied_at_s"] <= time_s for row in self._receipts)

    def payload_blocked(self, time_s):
        return self._signal_active("payload_jam", time_s)

    def tower_ready(self, time_s):
        return not self._signal_active("tower_unavailable", time_s)

    def accelerometer_gain(self, time_s):
        """Private sensor-harness setting, never a model observation field."""
        return .8 if self._signal_active("return_sensor_gain_80", time_s) else 1.

    def observation(self, state, vehicle, body):
        """Synthetic current component diagnostics, not a fault schedule oracle.

        This idealized diagnostic channel exposes current availability/jam flags;
        it is not a noisy sensor model or a reconstruction of real avionics.
        """
        self._clock(state.time_s, body)
        if len(state.engine_states) != len(vehicle.engines) or len(state.flap_angles_rad) != len(vehicle.aero_panels):
            raise ValueError("trouble_state_vehicle_mismatch")
        return dict(observed_at_s=state.time_s, channel="synthetic_component_health_v1",
            engine_health=[dict(component=spec.name, available=status.available)
                           for spec, status in zip(vehicle.engines, state.engine_states)],
            actuator_health=[dict(component=panel.name,
                jammed=(body, panel.name) in self._jams and self._jams[(body, panel.name)][1] <= state.time_s)
                for panel in vehicle.aero_panels if panel.max_deflection_rad > 0],
            payload_mechanism="blocked" if self.payload_blocked(state.time_s) else "clear",
            tower_ready=self.tower_ready(state.time_s))

    def audit(self):
        """Detached harness-only evidence, including unreached/pending events."""
        return json.loads(_json(dict(schema="missionos.starship_trouble_audit.v1",
            manifest_sha256=self.manifest_sha256, manifest=json.loads(self._manifest_json),
            phase_starts=[dict(body=body, phase=phase, time_s=time) for (body, phase), time in sorted(self._phase_starts.items())],
            receipts=self._receipts, unapplied_event_ids=[event["id"] for event in self._events if event["id"] not in self._applied])))
