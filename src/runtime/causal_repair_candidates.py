"""Propose distinct repair programs; never select, approve, or dispatch them.

Coordinates are world-frame metres. Backend capability declarations are necessary
but not sufficient for feasibility; the caller must also check each whole program.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Callable


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def finite(value: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Primitive:
    operation: str
    object_id: str
    target_m: tuple[float, float, float]
    maximum_steps: int


@dataclass(frozen=True)
class Candidate:
    family: str
    intervention: str
    start_digest: str
    hypothesis_ref: str
    operations: tuple[Primitive, ...]

    @property
    def program_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class RepairContext:
    start_digest: str
    object_id: str
    position_m: tuple[float, float, float]
    goal_m: tuple[float, float, float]
    staging_m: tuple[float, float, float]
    workspace_min_m: tuple[float, float, float]
    workspace_max_m: tuple[float, float, float]
    protected_objects: tuple[str, ...]
    supported_operations: tuple[str, ...]
    held: bool
    maximum_steps: int
    steps_per_operation: int = 40
    clearance_m: float = 0.06
    detour_m: float = 0.08
    unilateral_grasp_observed: bool = False


@dataclass(frozen=True)
class FamilyProposal:
    family: str
    hypothesis_ref: str


FAMILIES = (
    "increase_clearance",
    "lateral_detour",
    "release_and_reacquire",
)
REGISTERED_FAMILIES = (*FAMILIES, "recover_bilateral_grasp")


def generate_candidates(
    context: RepairContext,
    proposals: tuple[FamilyProposal, ...],
    *,
    feasible: Callable[[Candidate], bool],
) -> tuple[tuple[Candidate, ...], dict[str, str]]:
    """Compile a fixed direct baseline plus up to three proposed mechanisms.

    An LLM may supply family IDs and references to its grounded cause hypotheses.
    It cannot supply commands, coordinates, budgets or an approval. The context
    and feasibility callback are caller-owned. Rejection reasons stay visible.
    Regrasp requires an adapter-supported, bounded opposite-side acquisition.
    """
    c = context
    points = (c.position_m, c.goal_m, c.staging_m, c.workspace_min_m, c.workspace_max_m)
    if (
        not c.start_digest
        or not c.object_id
        or any(len(p) != 3 or not all(finite(v) for v in p) for p in points)
        or any(a >= b for a, b in zip(c.workspace_min_m, c.workspace_max_m, strict=True))
        or type(c.maximum_steps) is not int
        or type(c.steps_per_operation) is not int
        or not 1 <= c.steps_per_operation <= c.maximum_steps <= 10000
        or not finite(c.clearance_m)
        or not finite(c.detour_m)
        or c.clearance_m <= 0
        or c.detour_m <= 0
        or type(c.held) is not bool
        or type(c.unilateral_grasp_observed) is not bool
        or len(proposals) > 3
        or len({p.family for p in proposals}) != len(proposals)
        or any(p.family not in REGISTERED_FAMILIES or not p.hypothesis_ref for p in proposals)
    ):
        raise ValueError("invalid repair context or family proposal")
    x, y, z = c.position_m
    gx, gy, gz = c.goal_m
    height = max(z, gz) + c.clearance_m
    # Different intervention variables, not nearby parameter samples.
    templates = {
        "recover_bilateral_grasp": (
            "loaded_grasp_contact",
            (("recover_bilateral_grasp", c.position_m), ("translate", c.goal_m)),
        ),
        "direct": ("transport_path", (("translate", c.goal_m),)),
        "increase_clearance": (
            "vertical_clearance",
            (("raise", (x, y, height)), ("translate", (gx, gy, height)), ("lower", c.goal_m)),
        ),
        "lateral_detour": (
            "lateral_corridor",
            (
                ("translate", (x, y + c.detour_m, z)),
                ("translate", (gx, gy + c.detour_m, gz)),
                ("translate", c.goal_m),
            ),
        ),
        "release_and_reacquire": (
            "grasp_side",
            (
                ("place_then_stabilize", c.staging_m),
                ("release", c.staging_m),
                ("acquire_opposite_side", c.staging_m),
                ("translate", c.goal_m),
            ),
        ),
    }
    accepted, rejected = [], {}
    for proposal in (FamilyProposal("direct", "fixed-baseline"), *proposals):
        mechanism, operations = templates[proposal.family]
        operations = (
            *operations,
            ("place_then_stabilize", c.goal_m),
            ("release", c.goal_m),
            ("hold_and_observe", c.goal_m),
        )
        candidate = Candidate(
            proposal.family,
            mechanism,
            c.start_digest,
            proposal.hypothesis_ref,
            tuple(
                Primitive(op, c.object_id, target, c.steps_per_operation)
                for op, target in operations
            ),
        )
        reason = None
        if proposal.family == "recover_bilateral_grasp" and not c.unilateral_grasp_observed:
            reason = "requires_observed_unilateral_grasp"
        elif not c.held and proposal.family != "recover_bilateral_grasp":
            reason = "requires_observed_grasp"
        elif c.object_id in c.protected_objects:
            reason = "protected_object"
        elif any(op.operation not in c.supported_operations for op in candidate.operations):
            reason = "unsupported_primitive"
        elif sum(op.maximum_steps for op in candidate.operations) > c.maximum_steps:
            reason = "budget_exceeded"
        elif any(
            not all(
                lo <= v <= hi
                for lo, v, hi in zip(c.workspace_min_m, point, c.workspace_max_m, strict=True)
            )
            for point in (c.position_m, *(op.target_m for op in candidate.operations))
        ):
            reason = "outside_workspace"
        elif feasible(candidate) is not True:
            reason = "feasibility_not_confirmed"
        if reason:
            rejected[proposal.family] = reason
        else:
            accepted.append(candidate)
    return tuple(accepted), rejected
