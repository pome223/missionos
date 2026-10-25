"""Opt-in ascent reference history at the actual command/plant boundary.

Phase requests may be evaluated repeatedly at one clock. Only a command which
will advance the plant updates the reference. This helper never integrates or
assigns an actual vehicle state and grants no mission execution authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from .starship_ascent import AscentController
from .starship_reference_tracking import ReferenceTracker


class AscentControlRuntime:
    def __init__(self, settings, profile):
        if not AscentController(settings).control_trace_mode:
            raise ValueError("ascent_control_runtime_requires_explicit_v6")
        self.settings = settings
        self.profile = profile
        self.tracker = None
        self.rows = []
        self.terminal_commands = []
        self.pending = None

    def command(self, state, craft, raw_goal, throttle, count, *, phase,
                control_interval_s, control_fn, advancing=True, terminal_reason=None):
        if self.pending is not None:
            raise ValueError("ascent_control_previous_command_not_integrated")
        before = asdict(state)
        requested, tracking, kwargs = raw_goal, None, {}
        if advancing and self.settings.control_options.moving_reference_tracking:
            if self.tracker is None:
                self.tracker = ReferenceTracker(self.profile, state.q_body_to_eci, state.time_s)
            requested, tracking = self.tracker.update(raw_goal, state.q_body_to_eci,
                                                     state.omega_body_rad_s, state.time_s)
            kwargs = {"reference_rate_body_rad_s": tracking["reference_rate_body_rad_s"],
                      "reference_acceleration_body_rad_s": tracking["reference_acceleration_body_rad_s"]}
        command, diagnostics = control_fn(state, craft, requested, throttle, count,
            self.profile, control_interval_s=control_interval_s, **kwargs)
        if asdict(state) != before:
            raise ValueError("ascent_control_changed_actual_state")
        diagnostics["ascent_control"] = {
            "schema": "missionos.starship_ascent_control_request.v1", "phase": phase,
            "raw_goal_q_body_to_eci": list(raw_goal),
            "requested_q_body_to_eci": list(requested),
            "first_burn_only_bias": self.settings.control_options.first_burn_only_bias,
            "moving_reference_tracking": self.settings.control_options.moving_reference_tracking}
        if tracking is not None:
            diagnostics["reference_tracking"] = tracking["receipt"]
        if advancing:
            self.pending = (state, before, phase, raw_goal, requested, command, diagnostics)
        else:
            self.terminal_commands.append({"phase": phase, "time_s": state.time_s,
                "state": before, "raw_goal_q_body_to_eci": list(raw_goal),
                "command": asdict(command), "controller": deepcopy(diagnostics),
                "reason": terminal_reason, "reference_updated": False, "integrated": False})
        return command, diagnostics

    def record_step(self, step_index, state_given_to_plant, state_returned_by_plant, interval_s):
        if self.pending is None:
            raise ValueError("ascent_control_step_without_command")
        state, before, phase, raw_goal, requested, command, diagnostics = self.pending
        if state_given_to_plant is not state or asdict(state_given_to_plant) != before:
            raise ValueError("ascent_control_plant_input_changed")
        self.rows.append({"step_index": step_index, "phase": phase, "time_s": state.time_s,
            "interval_s": state_returned_by_plant.time_s-state.time_s,
            "requested_interval_s": interval_s,
            "body_id": "stack" if phase == "stack_ascent" else "ship",
            "state_before_control": before, "state_given_to_plant": asdict(state_given_to_plant),
            "state_returned_by_plant": asdict(state_returned_by_plant),
            "raw_goal_q_body_to_eci": list(raw_goal), "requested_q_body_to_eci": list(requested),
            "command": asdict(command), "controller": deepcopy(diagnostics)})
        self.pending = None

    def record_failed_step(self, reason):
        """Keep an attempted command when integration failed; never call it a tick."""
        if self.pending is None:
            return
        state, before, phase, raw_goal, _, command, diagnostics = self.pending
        self.terminal_commands.append({"phase": phase, "time_s": state.time_s,
            "state": before, "raw_goal_q_body_to_eci": list(raw_goal),
            "command": asdict(command), "controller": deepcopy(diagnostics),
            "reason": reason, "reference_updated": self.tracker is not None,
            "integrated": False, "plant_step_failed": True})
        self.pending = None

    def record(self):
        if self.pending is not None:
            raise ValueError("ascent_control_unfinished_plant_step")
        return {"schema": "missionos.starship_ascent_control_trace.v1",
            "settings": self.settings.record(), "rows": self.rows,
            "terminal_commands": self.terminal_commands, "actual_state_assigned": False,
            "mission_authority_granted": False}
