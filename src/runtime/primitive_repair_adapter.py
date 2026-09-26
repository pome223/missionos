"""Opt-in closed-loop primitive execution for resettable robotics research.

The session supplies actual robot control, observations and a terminal verifier.
This is a research execution boundary, not a MissionOS approval or dispatch path.
"""

from dataclasses import dataclass
import math
from typing import Protocol

from src.runtime.grasp_recovery import GraspEvidence, GraspRecovery
from src.runtime.causal_repair_candidates import Candidate
from src.runtime.candidate_informativeness import Measurement, Start


@dataclass(frozen=True)
class RobotObservation:
    object_m: tuple[float, float, float]
    hand_m: tuple[float, float, float]
    held: bool
    supported: bool
    preserved: bool
    predicates: tuple[bool, ...]
    minimum_clearance_m: float
    grasp: GraspEvidence | None = None


class RobotSession(Protocol):
    contract_digest: str
    evidence_kind: str  # fixture | simulator; must describe the actual session

    def restore(self, start: Start) -> str: ...

    def observe(self) -> RobotObservation: ...

    def step(self, translation_world_m: tuple[float, float, float], gripper: float) -> None:
        """Apply bounded closed-loop motion, retain registered hand orientation.

        gripper +1 closes and -1 opens. Readback/verification must follow EVERY
        control step, including partial physics progress followed by an error.
        """
        ...


class PrimitiveRepairAdapter:
    supported_operations = (
        "recover_bilateral_grasp",
        "close_and_qualify_grasp",
        "translate",
        "raise",
        "lower",
        "place_then_stabilize",
        "release",
        "hold_and_observe",
    )

    def __init__(self, session: RobotSession, *, allow_simulator: bool = False):
        if allow_simulator is not True:
            raise ValueError("explicit simulator opt-in required")
        if getattr(session, "evidence_kind", None) not in {"fixture", "simulator"}:
            raise ValueError("explicit session evidence kind required")
        self.session = session
        self.evidence_kind = session.evidence_kind
        self.contract_digest = session.contract_digest
        self.stop_reasons: dict[tuple[str, str], str] = {}
        self._restored = None

    def restore(self, start: Start) -> str:
        self._restored = None
        actual = self.session.restore(start)
        if actual == start.snapshot_digest:
            self._restored = (start.start_id, actual)
        return actual

    @staticmethod
    def _validate(obs):
        if (
            len(obs.object_m) != 3
            or len(obs.hand_m) != 3
            or not all(
                type(v) in (int, float) and math.isfinite(v)
                for v in (*obs.object_m, *obs.hand_m, obs.minimum_clearance_m)
            )
            or any(
                type(v) is not bool
                for v in (obs.held, obs.supported, obs.preserved, *obs.predicates)
            )
            or not obs.predicates
        ):
            raise ValueError("invalid robot observation")

    def execute(self, start: Start, candidate: Candidate, maximum_steps: int) -> Measurement:
        if self._restored != (start.start_id, start.snapshot_digest):
            raise ValueError("fresh independent restore required")
        self._restored = None  # one-shot: a second arm needs another restore
        if candidate.start_digest != start.snapshot_digest:
            raise ValueError("candidate/start mismatch")
        if any(op.operation not in self.supported_operations for op in candidate.operations):
            raise ValueError("unsupported primitive; reacquisition is not implemented")
        if (
            type(maximum_steps) is not int
            or not 1 <= maximum_steps <= 10000
            or not candidate.operations
            or any(
                type(op.maximum_steps) is not int or op.maximum_steps < 1
                for op in candidate.operations
            )
            or any(
                len(op.target_m) != 3
                or not all(type(v) in (int, float) and math.isfinite(v) for v in op.target_m)
                for op in candidate.operations
            )
            or sum(op.maximum_steps for op in candidate.operations) > maximum_steps
        ):
            raise ValueError("invalid primitive budget")
        obs = self.session.observe()
        self._validate(obs)
        initial = obs.object_m
        predicate_count = len(obs.predicates)
        steps = 0
        preserved = obs.preserved
        clearance = obs.minimum_clearance_m
        reason = "program_complete"
        released = False

        def advance(delta, grip):
            nonlocal obs, steps, preserved, clearance
            self.session.step(tuple(max(-0.006, min(0.006, v)) for v in delta), grip)
            steps += 1
            obs = self.session.observe()
            self._validate(obs)
            if len(obs.predicates) != predicate_count:
                raise ValueError("terminal predicate contract changed")
            preserved &= obs.preserved
            clearance = min(clearance, obs.minimum_clearance_m)

        for op in candidate.operations:
            settled = 0
            reached = False
            release_hand = obs.hand_m
            recovery = GraspRecovery(obs, recenter=op.operation == "recover_bilateral_grasp")
            if not preserved:
                reason = "preservation_stop"
                break
            for local_step in range(op.maximum_steps):
                if steps >= maximum_steps:
                    reason = "program_budget"
                    break
                if op.operation in {"recover_bilateral_grasp", "close_and_qualify_grasp"}:
                    status, delta = recovery.update(obs)
                    if status == "qualified":
                        reached = True
                        break
                    if status != "running":
                        reason = status
                        break
                    advance(delta, 1.0)
                elif op.operation in {"translate", "raise", "lower", "place_then_stabilize"}:
                    if not obs.held:
                        reason = "grasp_lost"
                        break
                    error = tuple(a - b for a, b in zip(op.target_m, obs.object_m, strict=True))
                    near = math.dist(op.target_m, obs.object_m) <= 0.003
                    if op.operation == "place_then_stabilize":
                        settled = settled + 1 if obs.supported and near else 0
                        reached = settled >= 5
                    else:
                        reached = near
                    if reached:
                        break
                    advance(error, 1.0)
                elif op.operation == "release":
                    if local_step == 0 and not obs.supported:
                        reason = "release_without_support_denied"
                        break
                    # Open first, then withdraw vertically while staying open.
                    delta = (
                        (0.0, 0.0, 0.0)
                        if local_step < 12
                        else (
                            release_hand[0] - obs.hand_m[0],
                            release_hand[1] - obs.hand_m[1],
                            release_hand[2] + 0.04 - obs.hand_m[2],
                        )
                    )
                    advance(delta, -1.0)
                    reached = (
                        local_step >= 24
                        and not obs.held
                        and obs.hand_m[2] >= release_hand[2] + 0.037
                    )
                    released = reached
                else:
                    if not released:
                        reason = "observation_before_release_denied"
                        break
                    advance((0.0, 0.0, 0.0), -1.0)
                    reached = local_step >= 20 and all(obs.predicates)
                if not preserved:
                    reason = "preservation_stop"
                    break
                if reached:
                    break
            if reason != "program_complete":
                break
            if not reached:
                reason = "primitive_timeout:" + op.operation
                break
        self.stop_reasons[(start.start_id, candidate.family)] = reason
        # Interrupted programs may still have a legitimately satisfied terminal
        # predicate. Preserve Verifier output instead of inventing a failure.
        return Measurement(
            start.start_id,
            start.snapshot_digest,
            candidate.program_digest,
            "observed",
            obs.predicates,
            preserved,
            steps,
            clearance,
            math.dist(initial, obs.object_m),
            self.contract_digest,
        )
