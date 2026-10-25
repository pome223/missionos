"""Read-only, causal deployment geometry from the actual 6DOF plant.

Only one selected payload is drawn: the existing vehicle model co-locates all
retained payload mass at one attachment. This is a synthetic internal-bay view,
not dispenser CAD, joint/contact mechanics, or clearance certification. Child
propagation runs on copies from the recorded release, never on future replay
samples and never writes a parent state or a release counter.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import math
import re

from . import starship_sixdof as dyn


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _inverse_rotate(q, vector):
    return dyn.rotate((q[0], -q[1], -q[2], -q[3]), tuple(vector))


class DeploymentWorld:
    """A bounded observer of one selected payload, with no execution methods."""

    def __init__(self, profile, *, target_payload_id="satellite_01", max_child_steps=2048):
        self.profile = deepcopy(profile)
        payload = self.profile["payload"]
        dimensions = payload["dimensions_m"]
        if (type(payload["count"]) is not int or not 1 <= payload["count"] <= 100
                or len(dimensions) != 3 or any(not _number(x) or x <= 0 for x in dimensions)
                or not _number(payload["center_z_m"])
                or type(max_child_steps) is not int or not 1 <= max_child_steps <= 10000):
            raise ValueError("invalid_deployment_world_configuration")
        self.max_child_steps = max_child_steps
        self._state = self._vehicle = None
        self._releases = {}
        self._release_digests = {}
        self._child_grid = {}
        self._clock = None
        self.target_payload_id = None
        self.select_target(target_payload_id)

    def select_target(self, payload_id):
        match = re.fullmatch(r"satellite_(\d{2,3})", payload_id) if type(payload_id) is str else None
        if not match or not 1 <= int(match[1]) <= self.profile["payload"]["count"]:
            raise ValueError("unregistered_visual_payload")
        self.target_payload_id = payload_id

    @property
    def time_s(self):
        return self._clock

    def observe_plant(self, state, vehicle, released_payloads, now=None):
        """Copy a present plant observation and immutable completed releases.

        released_payloads is the simulator's existing satellites list. Extra
        post-run samples/outcomes are intentionally never read or hashed.
        """
        state = dyn.state_from_dict(deepcopy(state)) if isinstance(state, dict) else deepcopy(state)
        vehicle = dyn.vehicle_from_dict(deepcopy(vehicle)) if isinstance(vehicle, dict) else deepcopy(vehicle)
        if not isinstance(state, dyn.State6DOF) or not isinstance(vehicle, dyn.Vehicle6DOF):
            raise ValueError("invalid_visual_plant")
        now = state.time_s if now is None else now
        if (not _number(now) or now != state.time_s or now < 0
                or self._clock is not None and now < self._clock):
            raise ValueError("noncausal_visual_plant_clock")
        if not isinstance(released_payloads, (list, tuple)) or len(released_payloads) > self.profile["payload"]["count"]:
            raise ValueError("invalid_visual_release_records")
        releases, identities = {}, {}
        for item in released_payloads:
            if type(item) is not dict:
                raise ValueError("invalid_visual_release_record")
            identity = item.get("id")
            match = re.fullmatch(r"satellite_(\d{2,3})", identity) if type(identity) is str else None
            if (not match or not 1 <= int(match[1]) <= self.profile["payload"]["count"]
                    or identity in releases):
                raise ValueError("invalid_visual_release_identity")
            child = dyn.state_from_dict(deepcopy(item["release_state"]))
            child_vehicle = dyn.vehicle_from_dict(deepcopy(item["vehicle"]))
            receipt = item["receipt"]
            if (child.time_s > now or receipt.get("schema") != "missionos.sixdof_payload_release.v1"
                    or receipt.get("momentum_check_passed") is not True
                    or receipt.get("time_s") != child.time_s
                    or digest(receipt.get("child_state")) != digest(asdict(child))
                    or receipt.get("child_position_body_m") != [0., 0., self.profile["payload"]["center_z_m"]]
                    or child_vehicle.dry_mass_kg != self.profile["payload"]["mass_each_kg"]):
                raise ValueError("visual_release_receipt_mismatch")
            record = {"release_state": asdict(child), "vehicle": asdict(child_vehicle), "receipt": deepcopy(receipt)}
            identity_digest = digest(record)
            if identity in self._release_digests and self._release_digests[identity] != identity_digest:
                raise ValueError("visual_release_changed")
            releases[identity], identities[identity] = record, identity_digest
        if not self._release_digests.keys() <= identities.keys():
            raise ValueError("visual_release_disappeared")
        # Commit only after the complete input was validated.
        self._state, self._vehicle, self._clock = state, vehicle, now
        self._releases, self._release_digests = releases, identities

    def snapshot(self):
        """Private render input at exactly the latest observed plant time."""
        if self._state is None:
            raise ValueError("visual_plant_not_observed")
        state, vehicle = self._state, self._vehicle
        center = dyn.mass_properties(vehicle, state.propellant_kg).com_body_m
        anchor = (0., 0., self.profile["payload"]["center_z_m"])
        target = self._releases.get(self.target_payload_id)
        steps = 0
        if target is None:
            position, rotation = anchor, ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
            child_state = None
        else:
            child = dyn.state_from_dict(target["release_state"])
            child_vehicle = dyn.vehicle_from_dict(target["vehicle"])
            # Cached full ten-second grid cells are anchored at the actual
            # release. Partial cells never update the cache, so requesting an
            # extra camera cannot change a later propagated state.
            required = math.ceil(max(0., state.time_s-child.time_s)/10.)
            if required > self.max_child_steps:
                raise ValueError("visual_child_propagation_budget_exceeded")
            full_cells = math.floor((state.time_s-child.time_s)/10.)
            cached_cells, cached = self._child_grid.get(self.target_payload_id, (0, child))
            if cached_cells > full_cells:
                cached_cells, cached = 0, child
            for _ in range(cached_cells, full_cells):
                cached = dyn.step(cached, child_vehicle, dyn.Command6DOF(), 10., atmosphere=False)
            self._child_grid[self.target_payload_id] = (full_cells, cached)
            child = cached
            if child.time_s < state.time_s:
                child = dyn.step(child, child_vehicle, dyn.Command6DOF(), state.time_s-child.time_s, atmosphere=False)
            steps = required
            relative = _inverse_rotate(state.q_body_to_eci, [a-b for a, b in zip(child.r_eci_m, state.r_eci_m)])
            position = tuple(a+b for a, b in zip(relative, center))
            rotation = tuple(_inverse_rotate(state.q_body_to_eci, dyn.rotate(child.q_body_to_eci, axis))
                             for axis in ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)))
            child_state = asdict(child)
        return deepcopy({
            "schema": "missionos.starship_deployment_world.v1", "time_s": state.time_s,
            "attachment_body_m": list(anchor), "payload_center_body_m": list(position),
            "payload_rotation_columns": [list(v) for v in rotation],
            "payload_dimensions_m": list(self.profile["payload"]["dimensions_m"]),
            "parent_state_sha256": digest(asdict(state)), "parent_vehicle_sha256": digest(asdict(vehicle)),
            "target_release_sha256": self._release_digests.get(self.target_payload_id),
            "child_state_sha256": digest(child_state), "child_propagation_steps": steps,
            "geometry_scope": "single_target_internal_bay_proxy_no_joint_or_clearance_model",
            "physical_execution": False,
        })
