"""Nav2 adapter for the backend-neutral Core action-feasibility contract.

The adapter translates source-backed Nav2 planning observations into Core
``HazardState`` and ``ActionCandidate`` records.  Nav2-specific geometry,
costmap, and duration checks stay in this module; MissionOS Core never branches
on a robot, middleware, or simulator identity.

This module is evidence-only.  It never invokes an LLM, creates approval or
dispatch authority, sends a Nav2 goal, or claims progress/completion.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from missionos_core import (
    ActionCandidate,
    CursorOrder,
    EvidenceSourceRef,
    ExtensionVerdict,
    FeasibilityStatus,
    HazardState,
    ObservationCursor,
    ObservedFact,
    PolicyBinding,
    canonical_sha256,
    verify_action_candidate,
)

from src.runtime.trajectory_clearance_3d import (
    assess_ground_robot_trajectory_clearance_3d,
)


NAV2_CORE_ADAPTER_ID = "missionos.nav2.action_feasibility.v1"
NAV2_CURSOR_CONTRACT = "missionos.nav2.costmap_cursor.v1"
NAV2_POLICY_ID = "missionos.nav2.recovery_policy"
NAV2_POLICY_VERSION = "1"
NAV2_ACTION_FEASIBILITY_ARTIFACT_SCHEMA = (
    "missionos_nav2_core_action_feasibility.v1"
)

_SUPPORTED_ACTIONS = frozenset({"avoid_obstacle", "reroute", "hold"})
_DEFAULT_POLICY = {
    "minimum_surface_clearance_m": 0.15,
    "maximum_costmap_age_s": 2.0,
    "maximum_path_duration_s": 30.0,
    "maximum_speed_mps": 0.26,
    "require_dual_costmap": True,
    "require_3d_collision_envelope": True,
}
_AUTHORITY_FLAGS = {
    "llm_invoked": False,
    "approval_created": False,
    "dispatch_authority_created": False,
    "dispatch_request_sent": False,
    "physical_execution_invoked": False,
    "progress_claimed": False,
    "completion_claimed": False,
    "delivery_completion_claimed": False,
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return list(value)
    return []


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def nav2_recovery_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the active deterministic Nav2 recovery policy."""

    supplied = {
        key: value
        for key, value in _mapping(overrides).items()
        if key not in {"policy_id", "policy_version", "policy_sha256"}
    }
    policy = {**_DEFAULT_POLICY, **supplied}
    return {
        **policy,
        "policy_id": NAV2_POLICY_ID,
        "policy_version": NAV2_POLICY_VERSION,
        "policy_sha256": canonical_sha256(policy),
    }


def nav2_policy_binding(
    policy: Mapping[str, Any],
) -> PolicyBinding:
    normalized = nav2_recovery_policy(policy)
    return PolicyBinding(
        policy_id=str(normalized["policy_id"]),
        policy_version=str(normalized["policy_version"]),
        policy_sha256=str(normalized["policy_sha256"]),
    )


class Nav2CostmapCursorComparator:
    """Compare paired global/local costmap source timestamps."""

    comparator_id = NAV2_CURSOR_CONTRACT
    comparison_contract = NAV2_CURSOR_CONTRACT

    def compare(
        self,
        earlier: ObservationCursor,
        later: ObservationCursor,
    ) -> CursorOrder:
        if (
            earlier.adapter_id != NAV2_CORE_ADAPTER_ID
            or later.adapter_id != NAV2_CORE_ADAPTER_ID
            or earlier.comparison_contract != NAV2_CURSOR_CONTRACT
            or later.comparison_contract != NAV2_CURSOR_CONTRACT
        ):
            return CursorOrder.INCOMPARABLE
        earlier_global = _positive_int(
            earlier.value.get("global_costmap_stamp_ns")
        )
        earlier_local = _positive_int(
            earlier.value.get("local_costmap_stamp_ns")
        )
        later_global = _positive_int(
            later.value.get("global_costmap_stamp_ns")
        )
        later_local = _positive_int(
            later.value.get("local_costmap_stamp_ns")
        )
        if None in {
            earlier_global,
            earlier_local,
            later_global,
            later_local,
        }:
            return CursorOrder.INCOMPARABLE
        left = (int(earlier_global), int(earlier_local))
        right = (int(later_global), int(later_local))
        if (
            left[0] == right[0]
            and earlier.value.get("global_costmap_snapshot_hash")
            != later.value.get("global_costmap_snapshot_hash")
        ) or (
            left[1] == right[1]
            and earlier.value.get("local_costmap_snapshot_hash")
            != later.value.get("local_costmap_snapshot_hash")
        ):
            # A costmap sample is immutable at one source timestamp.  Advancing
            # the other stream must not hide same-stamp content divergence.
            return CursorOrder.INCOMPARABLE
        if left == right:
            return CursorOrder.EQUAL
        if all(a <= b for a, b in zip(left, right, strict=True)):
            return CursorOrder.BEFORE
        if all(a >= b for a, b in zip(left, right, strict=True)):
            return CursorOrder.AFTER
        return CursorOrder.INCOMPARABLE


def _obstacle_volume(
    obstacle: Mapping[str, Any],
) -> dict[str, Any]:
    obstacle = _mapping(obstacle)
    return {
        "obstacle_ref": str(
            obstacle.get("runtime_obstacle_scene_ref")
            or obstacle.get("obstacle_ref")
            or ""
        ),
        "x_m": obstacle.get("runtime_obstacle_x_m", obstacle.get("x_m")),
        "y_m": obstacle.get("runtime_obstacle_y_m", obstacle.get("y_m")),
        "z_m": obstacle.get(
            "runtime_obstacle_z_m",
            obstacle.get("z_m"),
        ),
        "size_x_m": obstacle.get(
            "runtime_obstacle_collision_size_x_m",
            obstacle.get("size_x_m"),
        ),
        "size_y_m": obstacle.get(
            "runtime_obstacle_collision_size_y_m",
            obstacle.get("size_y_m"),
        ),
        "size_z_m": obstacle.get(
            "runtime_obstacle_size_z_m",
            obstacle.get("size_z_m"),
        ),
        "frame_id": str(
            obstacle.get("runtime_obstacle_frame_id")
            or obstacle.get("frame_id")
            or ""
        ),
        "geometry_source": str(
            obstacle.get("runtime_obstacle_geometry_source")
            or obstacle.get("geometry_source")
            or ""
        ),
        "semantic_candidate": (
            str(obstacle.get("semantic_candidate") or "") or None
        ),
        "evidence_ref": str(
            obstacle.get("runtime_obstacle_source")
            or obstacle.get("evidence_ref")
            or ""
        )
        or None,
    }


def _source(
    *,
    source_id: str,
    evidence_kind: str,
    observed_at: str,
    content_sha256: str | None,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=evidence_kind,
        observed_at=observed_at or None,
        content_sha256=content_sha256 or None,
        freshness_proof="adapter_cursor_verified",
    )


def build_nav2_core_hazard_state(
    *,
    evaluation: Mapping[str, Any],
    obstacle: Mapping[str, Any],
    robot_collision_envelope: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
    collected_at: str,
) -> HazardState:
    """Project one read-only Nav2 planning snapshot into Core."""

    evaluation = _mapping(evaluation)
    normalized_policy = nav2_recovery_policy(policy)
    obstacle_volume = _obstacle_volume(obstacle)
    robot_envelope = _mapping(robot_collision_envelope)
    global_hash = str(
        evaluation.get("global_costmap_snapshot_hash") or ""
    )
    local_hash = str(
        evaluation.get("local_costmap_snapshot_hash") or ""
    )
    obstacle_digest = _sha256(obstacle_volume)
    envelope_digest = _sha256(robot_envelope) if robot_envelope else ""
    global_source = _source(
        source_id=f"nav2:global_costmap:{global_hash or 'missing'}",
        evidence_kind="nav2_global_costmap_snapshot",
        observed_at=collected_at,
        content_sha256=global_hash,
    )
    local_source = _source(
        source_id=f"nav2:local_costmap:{local_hash or 'missing'}",
        evidence_kind="nav2_local_costmap_snapshot",
        observed_at=collected_at,
        content_sha256=local_hash,
    )
    obstacle_source = _source(
        source_id=(
            "nav2:obstacle:"
            f"{obstacle_volume.get('obstacle_ref') or obstacle_digest[:12]}"
        ),
        evidence_kind="source_backed_obstacle_collision_volume",
        observed_at=collected_at,
        content_sha256=obstacle_digest,
    )
    envelope_source = _source(
        source_id=f"nav2:robot_envelope:{envelope_digest[:12] or 'missing'}",
        evidence_kind="robot_collision_envelope",
        observed_at=collected_at,
        content_sha256=envelope_digest,
    )
    observed_facts = (
        ObservedFact(
            name="global_costmap_snapshot",
            value={
                "snapshot_hash": global_hash,
                "frame_id": evaluation.get("global_costmap_frame_id"),
                "stamp_ns": evaluation.get("global_costmap_stamp_ns"),
                "age_s": evaluation.get("global_costmap_age_s"),
            },
            unit=None,
            frame=str(evaluation.get("global_costmap_frame_id") or "")
            or None,
            source=global_source,
        ),
        ObservedFact(
            name="local_costmap_snapshot",
            value={
                "snapshot_hash": local_hash,
                "frame_id": evaluation.get("local_costmap_frame_id"),
                "stamp_ns": evaluation.get("local_costmap_stamp_ns"),
                "age_s": evaluation.get("local_costmap_age_s"),
                "frame_transform_verified": evaluation.get(
                    "local_frame_transform_verified"
                ),
            },
            unit=None,
            frame=str(evaluation.get("local_costmap_frame_id") or "")
            or None,
            source=local_source,
        ),
        ObservedFact(
            name="obstacle_collision_volume",
            value=obstacle_volume,
            unit="m",
            frame=str(obstacle_volume.get("frame_id") or "") or None,
            source=obstacle_source,
        ),
        ObservedFact(
            name="robot_collision_envelope",
            value=robot_envelope,
            unit="m",
            frame=str(robot_envelope.get("frame_id") or "") or None,
            source=envelope_source,
        ),
    )
    state_material = {
        "collected_at": collected_at,
        "cursor": {
            "global_costmap_stamp_ns": evaluation.get(
                "global_costmap_stamp_ns"
            ),
            "local_costmap_stamp_ns": evaluation.get(
                "local_costmap_stamp_ns"
            ),
            "global_costmap_snapshot_hash": global_hash,
            "local_costmap_snapshot_hash": local_hash,
        },
        "policy_sha256": normalized_policy["policy_sha256"],
        "observed_facts": [asdict(item) for item in observed_facts],
    }
    state_digest = _sha256(state_material)
    return HazardState(
        state_id=f"nav2_hazard_state_{state_digest[:12]}",
        collected_at=collected_at,
        cursor=ObservationCursor(
            adapter_id=NAV2_CORE_ADAPTER_ID,
            comparison_contract=NAV2_CURSOR_CONTRACT,
            value=dict(state_material["cursor"]),
        ),
        policy_binding=PolicyBinding(
            policy_id=str(normalized_policy["policy_id"]),
            policy_version=str(normalized_policy["policy_version"]),
            policy_sha256=str(normalized_policy["policy_sha256"]),
        ),
        observed_facts=observed_facts,
        assumptions=("planar_base_z_from_robot_runtime_profile",),
    )


def build_nav2_core_action_candidate(
    *,
    candidate_evaluation: Mapping[str, Any],
    hazard_state: HazardState,
    policy: Mapping[str, Any],
) -> ActionCandidate:
    """Project one exact Nav2 path evaluation into a Core candidate."""

    candidate_evaluation = _mapping(candidate_evaluation)
    path_frame = str(
        candidate_evaluation.get("path_frame_id")
        or candidate_evaluation.get("frame_id")
        or ""
    )
    path_points = [
        {
            "x_m": item.get("x_m"),
            "y_m": item.get("y_m"),
            "frame_id": str(item.get("frame_id") or path_frame),
        }
        for item in _sequence(candidate_evaluation.get("path_points"))
        if isinstance(item, Mapping)
    ]
    sources = tuple(
        dict.fromkeys(
            fact.source.source_id for fact in hazard_state.observed_facts
        )
    )
    action = str(
        candidate_evaluation.get("action")
        or candidate_evaluation.get("selected_action")
        or "avoid_obstacle"
    )
    return ActionCandidate(
        candidate_id=str(
            candidate_evaluation.get("candidate_id") or "nav2_candidate"
        ),
        action=action,
        parameters={
            "target_x_m": candidate_evaluation.get("x_m"),
            "target_y_m": candidate_evaluation.get("y_m"),
            "target_yaw_rad": candidate_evaluation.get("yaw_rad"),
            "max_speed_mps": candidate_evaluation.get("max_speed_mps"),
        },
        evidence_refs=sources,
        extension_inputs={
            "adapter_id": NAV2_CORE_ADAPTER_ID,
            "candidate_evaluation": dict(candidate_evaluation),
            "path_points": path_points,
            "path_frame_id": path_frame,
            "policy": dict(nav2_recovery_policy(policy)),
        },
    )


def _fact_value(
    hazard_state: HazardState,
    name: str,
) -> dict[str, Any]:
    for fact in hazard_state.observed_facts:
        if fact.name == name:
            return _mapping(fact.value)
    return {}


class Nav2FeasibilityVerifierExtension:
    """Deterministically verify Nav2 path evidence and swept clearance."""

    extension_id = NAV2_CORE_ADAPTER_ID

    def __init__(
        self,
        policy: Mapping[str, Any],
        *,
        previous_policy_sha256: str | None = None,
        previous_state_invalid: bool = False,
    ) -> None:
        self._policy = nav2_recovery_policy(policy)
        self._previous_policy_sha256 = previous_policy_sha256
        self._previous_state_invalid = previous_state_invalid

    def verify(
        self,
        *,
        hazard_state: HazardState,
        candidate: ActionCandidate,
    ) -> ExtensionVerdict:
        blocked: list[str] = []
        unverified: list[str] = []
        measurements: dict[str, Any] = {}
        candidate_input = _mapping(
            candidate.extension_inputs.get("candidate_evaluation")
        )
        global_costmap = _fact_value(
            hazard_state,
            "global_costmap_snapshot",
        )
        local_costmap = _fact_value(
            hazard_state,
            "local_costmap_snapshot",
        )
        obstacle = _fact_value(
            hazard_state,
            "obstacle_collision_volume",
        )
        robot_envelope = _fact_value(
            hazard_state,
            "robot_collision_envelope",
        )
        if self._previous_state_invalid:
            unverified.append("nav2_previous_hazard_state_invalid")
        if (
            self._previous_policy_sha256 is not None
            and self._previous_policy_sha256
            != self._policy["policy_sha256"]
        ):
            blocked.append("nav2_policy_binding_drift")

        if candidate.action not in _SUPPORTED_ACTIONS:
            blocked.append("nav2_action_not_supported")

        global_hash = str(global_costmap.get("snapshot_hash") or "")
        local_hash = str(local_costmap.get("snapshot_hash") or "")
        if self._policy["require_dual_costmap"] and (
            not global_hash or not local_hash
        ):
            unverified.append("nav2_dual_costmap_evidence_missing")
        for label, snapshot in (
            ("global", global_costmap),
            ("local", local_costmap),
        ):
            stamp_ns = _positive_int(snapshot.get("stamp_ns"))
            age_s = _finite(snapshot.get("age_s"))
            if stamp_ns is None:
                unverified.append(f"nav2_{label}_costmap_cursor_unverified")
            if age_s is None or age_s < 0:
                unverified.append(f"nav2_{label}_costmap_age_unverified")
            elif age_s > float(self._policy["maximum_costmap_age_s"]):
                unverified.append("nav2_dynamic_observation_stale")
        if (
            local_costmap.get("frame_id") != global_costmap.get("frame_id")
            and local_costmap.get("frame_transform_verified") is not True
        ):
            unverified.append("nav2_local_costmap_frame_unverified")
        if candidate.action == "hold":
            blocked = list(dict.fromkeys(blocked))
            unverified = list(dict.fromkeys(unverified))
            return ExtensionVerdict(
                extension_id=self.extension_id,
                status=(
                    FeasibilityStatus.BLOCKED
                    if blocked
                    else FeasibilityStatus.UNVERIFIED
                    if unverified
                    else FeasibilityStatus.VERIFIED_FEASIBLE
                ),
                blocked_reasons=tuple(blocked),
                unverified_reasons=tuple(unverified),
                measurements={"motion_command_required": False},
                assumptions=("hold_creates_no_nav2_motion_candidate",),
            )

        required_obstacle_values = (
            "obstacle_ref",
            "x_m",
            "y_m",
            "z_m",
            "size_x_m",
            "size_y_m",
            "size_z_m",
            "frame_id",
            "geometry_source",
        )
        if any(obstacle.get(name) in (None, "") for name in required_obstacle_values):
            unverified.append("nav2_obstacle_geometry_unverified")
        if self._policy["require_3d_collision_envelope"] and (
            any(
                robot_envelope.get(name) in (None, "")
                for name in (
                    "radius_m",
                    "z_min_m",
                    "z_max_m",
                    "frame_id",
                    "geometry_source",
                )
            )
        ):
            unverified.append("nav2_robot_collision_envelope_unverified")

        path_frame = str(
            candidate.extension_inputs.get("path_frame_id") or ""
        )
        obstacle_frame = str(obstacle.get("frame_id") or "")
        global_frame = str(global_costmap.get("frame_id") or "")
        if (
            not path_frame
            or not obstacle_frame
            or not global_frame
            or path_frame != obstacle_frame
            or path_frame != global_frame
        ):
            unverified.append("nav2_path_obstacle_frame_mismatch")

        path_points = [
            dict(item)
            for item in _sequence(
                candidate.extension_inputs.get("path_points")
            )
            if isinstance(item, Mapping)
        ]
        if len(path_points) < 2:
            unverified.append("nav2_candidate_path_geometry_unverified")
        elif any(
            str(item.get("frame_id") or "") != path_frame
            for item in path_points
        ):
            unverified.append("nav2_candidate_path_point_frame_mismatch")

        if candidate_input.get("path_valid") is not True:
            blocked.append("nav2_path_not_traversable")
        declared_path_hash = str(candidate_input.get("path_sha256") or "")
        normalized_path = [
            [round(float(item["x_m"]), 6), round(float(item["y_m"]), 6)]
            for item in path_points
            if _finite(item.get("x_m")) is not None
            and _finite(item.get("y_m")) is not None
        ]
        computed_path_hash = hashlib.sha256(
            json.dumps(normalized_path, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not declared_path_hash:
            unverified.append("nav2_candidate_path_digest_missing")
        elif declared_path_hash != computed_path_hash:
            blocked.append("nav2_candidate_path_digest_mismatch")

        path_length_m = _finite(candidate_input.get("path_length_m"))
        computed_path_length_m = (
            sum(
                math.hypot(
                    float(end["x_m"]) - float(start["x_m"]),
                    float(end["y_m"]) - float(start["y_m"]),
                )
                for start, end in zip(
                    path_points,
                    path_points[1:],
                    strict=False,
                )
            )
            if len(path_points) >= 2
            and len(normalized_path) == len(path_points)
            else None
        )
        path_goal_error_m = _finite(
            candidate_input.get("path_goal_error_m")
        )
        path_goal_tolerance_m = _finite(
            candidate_input.get("path_goal_tolerance_m")
        )
        target_x_m = _finite(candidate.parameters.get("target_x_m"))
        target_y_m = _finite(candidate.parameters.get("target_y_m"))
        computed_goal_error_m = (
            math.hypot(
                float(path_points[-1]["x_m"]) - target_x_m,
                float(path_points[-1]["y_m"]) - target_y_m,
            )
            if path_points
            and len(normalized_path) == len(path_points)
            and target_x_m is not None
            and target_y_m is not None
            else None
        )
        speed_mps = _finite(candidate.parameters.get("max_speed_mps"))
        if path_length_m is None or path_length_m < 0:
            unverified.append("nav2_candidate_path_length_unverified")
        elif computed_path_length_m is not None and not math.isclose(
                path_length_m,
                computed_path_length_m,
                rel_tol=0.01,
                abs_tol=0.02,
            ):
            blocked.append("nav2_candidate_path_length_mismatch")
        elif computed_path_length_m is not None and computed_path_length_m <= 1e-6:
            blocked.append("nav2_candidate_no_motion")
        if (
            path_goal_error_m is None
            or path_goal_tolerance_m is None
            or path_goal_tolerance_m <= 0
            or computed_goal_error_m is None
        ):
            unverified.append("nav2_candidate_goal_reach_unverified")
        elif not math.isclose(
            path_goal_error_m,
            computed_goal_error_m,
            rel_tol=0.01,
            abs_tol=0.01,
        ):
            blocked.append("nav2_candidate_goal_error_mismatch")
        elif computed_goal_error_m > path_goal_tolerance_m:
            blocked.append("nav2_candidate_goal_not_reached")
        measurements["computed_path_goal_error_m"] = computed_goal_error_m
        if speed_mps is None or speed_mps <= 0:
            unverified.append("nav2_candidate_velocity_unverified")
        elif speed_mps > float(self._policy["maximum_speed_mps"]):
            blocked.append("nav2_candidate_velocity_exceeds_policy")
        if (
            path_length_m is not None
            and path_length_m >= 0
            and speed_mps is not None
            and speed_mps > 0
        ):
            duration_s = path_length_m / speed_mps
            measurements["projected_duration_s"] = round(duration_s, 6)
            if duration_s > float(self._policy["maximum_path_duration_s"]):
                blocked.append("nav2_candidate_duration_exceeds_policy")

        clearance = assess_ground_robot_trajectory_clearance_3d(
            trajectory_streams=(path_points,),
            robot_collision_envelope=robot_envelope,
            obstacle_volumes=(obstacle,),
        )
        measurements["trajectory_clearance_3d"] = clearance.model_dump(
            mode="json"
        )
        measurements["minimum_surface_clearance_m"] = (
            clearance.minimum_surface_clearance_m
        )
        if clearance.status == "collision_observed":
            blocked.append("nav2_candidate_collision_envelope_intersection")
        elif clearance.status != "verified_clear":
            unverified.append("nav2_candidate_3d_clearance_unverified")
        elif (
            clearance.minimum_surface_clearance_m is None
            or clearance.minimum_surface_clearance_m
            < float(self._policy["minimum_surface_clearance_m"])
        ):
            blocked.append("nav2_candidate_clearance_below_policy")

        blocked = list(dict.fromkeys(blocked))
        unverified = list(dict.fromkeys(unverified))
        status = (
            FeasibilityStatus.BLOCKED
            if blocked
            else FeasibilityStatus.UNVERIFIED
            if unverified
            else FeasibilityStatus.VERIFIED_FEASIBLE
        )
        return ExtensionVerdict(
            extension_id=self.extension_id,
            status=status,
            blocked_reasons=tuple(blocked),
            unverified_reasons=tuple(unverified),
            measurements=measurements,
            assumptions=("planar_base_z_from_robot_runtime_profile",),
        )


def verify_nav2_core_action_candidate(
    *,
    evaluation: Mapping[str, Any],
    candidate_evaluation: Mapping[str, Any],
    obstacle: Mapping[str, Any],
    robot_collision_envelope: Mapping[str, Any] | None,
    active_policy: Mapping[str, Any],
    evaluated_at: str,
    previous_hazard_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one candidate through Core and return a hash-bound artifact."""

    policy = nav2_recovery_policy(active_policy)
    hazard_state = build_nav2_core_hazard_state(
        evaluation=evaluation,
        obstacle=obstacle,
        robot_collision_envelope=robot_collision_envelope,
        policy=policy,
        collected_at=evaluated_at,
    )
    candidate = build_nav2_core_action_candidate(
        candidate_evaluation=candidate_evaluation,
        hazard_state=hazard_state,
        policy=policy,
    )
    previous_cursor = None
    previous_state = None
    previous_state_invalid = False
    if isinstance(previous_hazard_state, Mapping):
        try:
            previous_state = HazardState.from_dict(previous_hazard_state)
        except (KeyError, TypeError, ValueError):
            previous_state_invalid = True
    if previous_state is not None:
        previous_cursor = previous_state.cursor
    result = verify_action_candidate(
        hazard_state=hazard_state,
        candidate=candidate,
        active_policy=nav2_policy_binding(policy),
        evaluated_at=evaluated_at,
        extensions=(
            Nav2FeasibilityVerifierExtension(
                policy,
                previous_policy_sha256=(
                    previous_state.policy_binding.policy_sha256
                    if previous_state is not None
                    else None
                ),
                previous_state_invalid=previous_state_invalid,
            ),
        ),
        previous_cursor=previous_cursor,
        cursor_comparator=Nav2CostmapCursorComparator(),
    )
    material = {
        "schema_version": NAV2_ACTION_FEASIBILITY_ARTIFACT_SCHEMA,
        "adapter_id": NAV2_CORE_ADAPTER_ID,
        "hazard_state": hazard_state.to_dict(),
        "action_candidate": candidate.to_dict(),
        "action_feasibility": result.to_dict(),
        "active_policy": policy,
        **_AUTHORITY_FLAGS,
    }
    digest = _sha256(material)
    return {
        **material,
        "artifact_sha256": digest,
        "artifact_id": f"nav2_action_feasibility_{digest[:12]}",
    }


def evaluate_nav2_recovery_candidates_through_core(
    *,
    evaluation: Mapping[str, Any],
    obstacle: Mapping[str, Any],
    robot_collision_envelope: Mapping[str, Any] | None,
    active_policy: Mapping[str, Any],
    evaluated_at: str,
    previous_hazard_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach Core verdicts and expose only verified candidates as selectable."""

    normalized = dict(evaluation)
    evaluated_candidates: list[dict[str, Any]] = []
    for raw_candidate in _sequence(evaluation.get("candidate_evaluations")):
        if not isinstance(raw_candidate, Mapping):
            continue
        artifact = verify_nav2_core_action_candidate(
            evaluation=evaluation,
            candidate_evaluation=raw_candidate,
            obstacle=obstacle,
            robot_collision_envelope=robot_collision_envelope,
            active_policy=active_policy,
            evaluated_at=evaluated_at,
            previous_hazard_state=previous_hazard_state,
        )
        result = _mapping(artifact.get("action_feasibility"))
        status = result.get("status")
        status = status.value if isinstance(status, FeasibilityStatus) else status
        evaluated_candidates.append(
            {
                **dict(raw_candidate),
                "core_action_feasibility_status": status,
                "core_action_feasibility": artifact,
            }
        )
    verified = [
        item
        for item in evaluated_candidates
        if item.get("path_valid") is True
        and item.get("core_action_feasibility_status")
        == FeasibilityStatus.VERIFIED_FEASIBLE.value
        and item.get("sequence_only") is not True
    ]
    verified.sort(
        key=lambda item: (
            int(item.get("local_maximum_path_cost") or 0),
            int(item.get("selection_priority", 100)),
            int(item.get("maximum_path_cost") or 0),
            float(item.get("path_length_m") or math.inf),
            str(item.get("candidate_id") or ""),
        )
    )
    original_selected = _mapping(evaluation.get("selected_candidate"))
    original_selected_id = str(
        original_selected.get("candidate_id") or ""
    )
    selected = next(
        (
            item
            for item in verified
            if str(item.get("candidate_id") or "") == original_selected_id
        ),
        verified[0] if verified else None,
    )
    core_state = (
        _mapping(
            _mapping(
                evaluated_candidates[0].get("core_action_feasibility")
            ).get("hazard_state")
        )
        if evaluated_candidates
        else {}
    )
    return {
        **normalized,
        "evaluation_status": "validated" if selected else "blocked",
        "candidate_evaluations": evaluated_candidates,
        "selected_candidate": selected,
        "core_adapter_id": NAV2_CORE_ADAPTER_ID,
        "core_hazard_state": core_state,
        "core_hazard_state_sha256": (
            canonical_sha256(core_state) if core_state else ""
        ),
        "core_policy_binding": (
            _mapping(core_state.get("policy_binding")) if core_state else {}
        ),
        "blocking_reasons": (
            []
            if selected
            else list(
                dict.fromkeys(
                    [
                        *(normalized.get("blocking_reasons") or []),
                        "no_core_verified_recovery_candidate",
                    ]
                )
            )
        ),
        **_AUTHORITY_FLAGS,
    }


__all__ = [
    "NAV2_ACTION_FEASIBILITY_ARTIFACT_SCHEMA",
    "NAV2_CORE_ADAPTER_ID",
    "NAV2_CURSOR_CONTRACT",
    "Nav2CostmapCursorComparator",
    "Nav2FeasibilityVerifierExtension",
    "build_nav2_core_action_candidate",
    "build_nav2_core_hazard_state",
    "evaluate_nav2_recovery_candidates_through_core",
    "nav2_policy_binding",
    "nav2_recovery_policy",
    "verify_nav2_core_action_candidate",
]
