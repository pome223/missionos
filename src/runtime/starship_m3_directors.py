"""Body-specific fixture grants; the Ship keeps its unchanged M2 authority."""
from copy import deepcopy
from pathlib import Path

from .starship_deployment_forecast import ObservedDeploymentDirector
from .starship_mission_director import MissionDirector, contract, digest


class M3Directors:
    """Route only prefix operations; never merge one body's grant into another."""

    def __init__(self, ship_envelope, booster_envelope, tracker, *, fixture_decider=None, run_id, mode="fixture", mailbox=None):
        if (digest(ship_envelope) != digest(contract(mode))
                or digest(booster_envelope) != digest(contract(mode, splashdown=True))):
            raise ValueError("m3_body_authority_not_registered")
        if mode == "live" and (fixture_decider is not None or mailbox is None):
            raise ValueError("m3_live_directors_require_phase_mailboxes")
        root = Path(mailbox) if mailbox is not None else None
        self.ship = ObservedDeploymentDirector(ship_envelope, tracker,
            fixture_decider=fixture_decider, run_id=run_id + "-deployment",
            mailbox=root / "deployment" if mode == "live" else None)
        self.booster = MissionDirector(booster_envelope, fixture_decider=fixture_decider,
                                      run_id=run_id + "-booster",
                                      mailbox=root / "booster" if mode == "live" else None)
        # The simulator's goal guard reads this view. Every actual decision is
        # checked by its body's separate actor, whose original grant is saved.
        self.envelope = deepcopy(booster_envelope)

    @property
    def pending(self):
        return self.ship.pending

    @property
    def finished(self):
        return self.ship.finished

    @property
    def observation_requests(self):
        return self.ship.observation_requests

    @property
    def hold_used_s(self):
        return self.ship.hold_used_s

    @property
    def tracker(self):
        return self.ship.tracker

    @property
    def trace(self):
        return self.ship.trace

    def _body(self, clock_id):
        if clock_id not in ("ship", "booster"):
            raise ValueError("m3_unregistered_body")
        return self.ship if clock_id == "ship" else self.booster

    def pace(self, clock_id, time_s):
        return self._body(clock_id).pace(clock_id, time_s)

    def confirm(self, clock_id, row):
        actor = self._body(clock_id)
        expected = "orbital_coast" if clock_id == "ship" else "booster_return"
        if row.get("phase") != expected:
            raise ValueError("m3_cross_body_observation")
        return actor.confirm(clock_id, row)

    def update(self, point, row, **kwargs):
        if point == "booster_selection":
            if row.get("phase") != "booster_return":
                raise ValueError("m3_cross_body_observation")
            return self.booster.update(point, row, **kwargs)
        if point not in {"deployment_start", "deployment_monitor", "deployment_diagnostic", "deployment_reassessment"}:
            raise ValueError("m3_unregistered_prefix_point")
        if row.get("phase") != "orbital_coast":
            raise ValueError("m3_cross_body_observation")
        return self.ship.update(point, row, **kwargs)

    def finish(self):
        return self.ship.finish()

    def finish_booster(self):
        return self.booster.finish()
