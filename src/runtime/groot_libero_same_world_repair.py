from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
from typing import Any, Protocol
from uuid import uuid4

import numpy as np

from missionos_core import canonical_sha256

from src.runtime.bounded_chunk_authority import (
    BoundedChunkAuthorityPolicy,
    begin_bounded_chunk_authority,
)
from src.runtime.libero_panda_predicate_package import (
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
    libero_panda_goal_predicate_specs,
)


# v4 additionally binds the concrete action-execution adapter. This prevents an
# approval for the 8-step Isaac-GR00T/ZMQ path from being reused by the 16-step
# LeRobot policy path (or vice versa).
SAME_WORLD_REPAIR_PROPOSAL_SCHEMA_VERSION = "missionos_groot_libero_same_world_repair_proposal.v4"
SAME_WORLD_REPAIR_APPROVAL_SCHEMA_VERSION = "missionos_groot_libero_same_world_repair_approval.v3"
SAME_WORLD_REPAIR_DISPATCH_SCHEMA_VERSION = "missionos_groot_libero_same_world_repair_dispatch.v3"
# v4 split frame diagnostics from the receipt digest. v5 records the
# instruction-ablation variant. v6 separates terminal predicate observation
# from completion authority for diagnostic state clones. v7 binds the action
# execution adapter used by the live runtime.
SAME_WORLD_REPAIR_RESULT_SCHEMA_VERSION = "missionos_groot_libero_same_world_repair_result.v7"
PRESERVATION_STEP_TRACE_SCHEMA_VERSION = "missionos_groot_libero_preservation_step_trace.v1"
FRAME_CAPTURE_SCHEMA_VERSION = "missionos_groot_libero_repair_frame_capture.v1"
PRESERVATION_INVARIANT_SCHEMA_VERSION = "missionos_groot_libero_preservation_invariant.v1"

# Three evidence types with different evaluation timing and different authority.
# Keeping them separate stops a continuously-evaluated invariant from being
# mistaken for a terminal completion judgment.
EVIDENCE_TYPE_COMPLETION_PREDICATE = "completion_predicate"
EVIDENCE_TYPE_PRESERVATION_INVARIANT = "preservation_invariant"
EVIDENCE_TYPE_STOP_ONLY_ANOMALY_CLAIM = "stop_only_anomaly_claim"

DEFAULT_PRESERVED_OBJECT_MAX_DISPLACEMENT_METRES = 0.005
DEFAULT_REPAIR_INSTRUCTION_VARIANT = "semantic_preserve"
DEFAULT_EXECUTION_ADAPTER = "isaac_groot_zmq_multistep_v1"
LEROBOT_GROOT_N17_EXECUTION_ADAPTER = "lerobot_groot_n17_select_action_v1"
VLA0_LIBERO_EXECUTION_ADAPTER = "vla0_libero_qwen_text_action_v1"
SUPPORTED_EXECUTION_ADAPTERS = frozenset(
    {
        DEFAULT_EXECUTION_ADAPTER,
        LEROBOT_GROOT_N17_EXECUTION_ADAPTER,
        VLA0_LIBERO_EXECUTION_ADAPTER,
    }
)
REPAIR_INSTRUCTION_VARIANTS = frozenset({"semantic_preserve", "original_task", "short_target"})
REPAIR_INSTRUCTION_ABLATION_METRICS = (
    "target_minimum_end_effector_distance_metres",
    "target_gripper_contact_steps",
    "target_maximum_displacement_metres",
    "protected_object_gripper_contact_steps",
    "protected_object_maximum_displacement_metres",
    "chunk_predicate_timeline",
    "terminal_status",
)
STATE_CONTINUITY_LIVE_SAME_WORLD = "live_same_world"
STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE = "diagnostic_mujoco_state_clone"
STATE_CONTINUITY_BASES = frozenset(
    {
        STATE_CONTINUITY_LIVE_SAME_WORLD,
        STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    }
)

# Frames are an investigation artifact. Nothing in the Repair loop, the
# preservation decision, or the receipt may read them, so the record carries its
# own authority label and is asserted powerless by contract test.
FRAME_CAPTURE_AUTHORITY = "diagnostic_only"
FRAME_CAPTURE_STATUSES = frozenset({"not_requested", "captured", "capture_failed", "unusable"})
_OBSERVATION_KEY_PATTERN = re.compile(r"video\.[a-z0-9_]{1,64}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_FAILURE_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


class DispatchLedger(Protocol):
    def claim_dispatch_ref(
        self,
        *,
        dispatch_ref: str,
        request_payload: Mapping[str, Any],
        correlation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def mark_dispatch_send_started(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
    ) -> dict[str, Any]: ...

    def record_dispatch_receipt(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def record_unknown_dispatch_outcome(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        error_type: str,
    ) -> dict[str, Any]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _without_digest(material: Mapping[str, Any], digest_name: str) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in material.items() if key != digest_name}


def _require_digest(material: Mapping[str, Any], digest_name: str) -> None:
    if material.get(digest_name) != canonical_sha256(_without_digest(material, digest_name)):
        raise ValueError(f"{digest_name}_mismatch")


def _predicate_id(index: int, name: str, arguments: Sequence[str]) -> str:
    return canonical_sha256(
        {
            "predicate_index": index,
            "predicate_name": name,
            "arguments": list(arguments),
        }
    )


def normalize_goal_predicates(
    *,
    environment: str,
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected = libero_panda_goal_predicate_specs(environment)
    if len(observations) != len(expected):
        raise ValueError("goal_predicate_count_mismatch")
    normalized: list[dict[str, Any]] = []
    for index, (observation, expected_spec) in enumerate(zip(observations, expected, strict=True)):
        name = str(observation.get("predicate_name") or "")
        arguments = observation.get("arguments")
        satisfied = observation.get("satisfied")
        if (
            observation.get("predicate_index") != index
            or not isinstance(arguments, (list, tuple))
            or any(not isinstance(argument, str) for argument in arguments)
            or not isinstance(satisfied, bool)
            or (name, *arguments) != expected_spec
        ):
            raise ValueError("goal_predicate_definition_mismatch")
        expected_id = _predicate_id(index, name, arguments)
        if observation.get("predicate_id") != expected_id:
            raise ValueError("goal_predicate_digest_mismatch")
        normalized.append(
            {
                "predicate_index": index,
                "predicate_id": expected_id,
                "predicate_name": name,
                "arguments": list(arguments),
                "satisfied": satisfied,
            }
        )
    return normalized


def _finite_number(value: Any, *, field: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise RuntimeError(f"repair_step_trace_{field}_not_numeric")
    normalized = float(value)
    if not np.isfinite(normalized):
        raise RuntimeError(f"repair_step_trace_{field}_not_finite")
    if minimum is not None and normalized < minimum:
        raise RuntimeError(f"repair_step_trace_{field}_below_minimum")
    return normalized


def _numeric_vector(value: Any, *, field: str, length: int) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RuntimeError(f"repair_step_trace_{field}_not_sequence")
    if len(value) != length:
        raise RuntimeError(f"repair_step_trace_{field}_length_mismatch")
    return [_finite_number(item, field=field) for item in value]


def _frame_capture_record(status: str, **extra: Any) -> dict[str, Any]:
    if status not in FRAME_CAPTURE_STATUSES:
        raise RuntimeError("repair_frame_capture_status_unknown")
    return {
        "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
        "authority": FRAME_CAPTURE_AUTHORITY,
        "status": status,
        "cameras": [],
        **extra,
    }


def _normalize_frame_camera(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    observation_key = raw.get("observation_key")
    image_sha256 = raw.get("image_sha256")
    relative_path = raw.get("artifact_relative_path")
    if (
        not isinstance(observation_key, str)
        or not _OBSERVATION_KEY_PATTERN.fullmatch(observation_key)
        or not isinstance(image_sha256, str)
        or not _SHA256_PATTERN.fullmatch(image_sha256)
        or not isinstance(relative_path, str)
        or not relative_path
    ):
        return None
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        return None
    dimensions: dict[str, int] = {}
    for field in ("height_pixels", "width_pixels", "channels"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        dimensions[field] = value
    encoding = raw.get("encoding")
    if not isinstance(encoding, str) or encoding not in ("png", "npy"):
        return None
    return {
        "observation_key": observation_key,
        "image_sha256": image_sha256,
        "artifact_relative_path": str(path),
        "encoding": encoding,
        **dimensions,
    }


def normalize_frame_capture(raw: Any) -> dict[str, Any]:
    """Normalize the diagnostic frame record captured beside one traced step.

    Frames answer "why did it fall" faster than a margin series, but they are
    read by humans and models, not by the Repair loop. A lost render therefore
    degrades to a recorded status instead of an exception: an expensive live run
    that the numeric predicates can still decide must not die for a missing PNG.
    Anything malformed is downgraded to ``unusable`` rather than trusted.
    """

    if raw is None:
        return _frame_capture_record("not_requested")
    if not isinstance(raw, Mapping):
        return _frame_capture_record("unusable", failure_code="record_not_mapping")
    if raw.get("schema_version") != FRAME_CAPTURE_SCHEMA_VERSION:
        return _frame_capture_record("unusable", failure_code="schema_mismatch")
    status = raw.get("status")
    if status == "not_requested":
        return _frame_capture_record("not_requested")
    if status == "capture_failed":
        failure_code = raw.get("failure_code")
        if not isinstance(failure_code, str) or not _FAILURE_CODE_PATTERN.fullmatch(failure_code):
            failure_code = "unspecified"
        return _frame_capture_record("capture_failed", failure_code=failure_code)
    if status != "captured":
        return _frame_capture_record("unusable", failure_code="status_unknown")

    raw_cameras = raw.get("cameras")
    if isinstance(raw_cameras, (str, bytes)) or not isinstance(raw_cameras, Sequence):
        return _frame_capture_record("unusable", failure_code="cameras_not_sequence")
    if not raw_cameras:
        return _frame_capture_record("unusable", failure_code="cameras_empty")
    cameras: list[dict[str, Any]] = []
    for raw_camera in raw_cameras:
        camera = _normalize_frame_camera(raw_camera)
        if camera is None:
            return _frame_capture_record("unusable", failure_code="camera_invalid")
        cameras.append(camera)
    observation_keys = [camera["observation_key"] for camera in cameras]
    if len(set(observation_keys)) != len(observation_keys):
        return _frame_capture_record("unusable", failure_code="camera_key_duplicated")
    record = _frame_capture_record("captured")
    record["cameras"] = sorted(cameras, key=lambda camera: camera["observation_key"])
    return record


def preserved_object_names(
    *,
    goal_predicate_observations: Sequence[Mapping[str, Any]],
    preserve_predicate_ids: Sequence[str],
) -> list[str]:
    """Name the objects a preserved predicate depends on.

    Expressed over the preserve set rather than over this task's object names,
    so the invariant ports to any backend that can observe an object pose. A
    predicate whose first argument is not a pose-bearing object (``turnon`` on a
    stove, for example) simply contributes no object here.
    """

    preserve_ids = set(preserve_predicate_ids)
    names: list[str] = []
    for observation in goal_predicate_observations:
        if str(observation.get("predicate_id")) not in preserve_ids:
            continue
        arguments = observation.get("arguments")
        if not isinstance(arguments, Sequence) or isinstance(arguments, (str, bytes)):
            continue
        if not arguments or not isinstance(arguments[0], str):
            continue
        if arguments[0] not in names:
            names.append(arguments[0])
    return names


def build_preservation_invariant(
    *,
    goal_predicate_observations: Sequence[Mapping[str, Any]],
    preserve_predicate_ids: Sequence[str],
    source_object_poses: Mapping[str, Sequence[float]] | None,
    maximum_displacement_metres: float,
) -> dict[str, Any]:
    """Bind the continuous preservation invariant into the Repair Contract.

    The reference poses are the ones observed when the human approved. "Keep it
    where it is" is only meaningful against the state that was approved, so the
    reference travels inside the Contract digest instead of being re-derived at
    run time where it could drift.
    """

    if isinstance(maximum_displacement_metres, bool) or not isinstance(
        maximum_displacement_metres, (int, float)
    ):
        raise ValueError("preservation_invariant_displacement_not_numeric")
    threshold = float(maximum_displacement_metres)
    if not np.isfinite(threshold) or threshold <= 0.0:
        raise ValueError("preservation_invariant_displacement_invalid")

    candidates = preserved_object_names(
        goal_predicate_observations=goal_predicate_observations,
        preserve_predicate_ids=preserve_predicate_ids,
    )
    reference: dict[str, list[float]] = {}
    for name in candidates:
        pose = (source_object_poses or {}).get(name)
        if pose is None:
            continue
        if isinstance(pose, (str, bytes)) or not isinstance(pose, Sequence) or len(pose) != 3:
            raise ValueError("preservation_invariant_reference_pose_invalid")
        coordinates = [float(value) for value in pose]
        if any(not np.isfinite(value) for value in coordinates):
            raise ValueError("preservation_invariant_reference_pose_not_finite")
        reference[name] = coordinates

    return {
        "schema_version": PRESERVATION_INVARIANT_SCHEMA_VERSION,
        "evidence_type": EVIDENCE_TYPE_PRESERVATION_INVARIANT,
        "enabled": bool(reference),
        "evaluated_every_simulator_step": True,
        "stops_run": True,
        "claims_completion": False,
        "protected_object_names": sorted(reference),
        "reference_position_metres": {name: reference[name] for name in sorted(reference)},
        "maximum_displacement_metres": threshold,
        "requires_contact_observation": True,
    }


def _preservation_invariant_breach(
    *,
    invariant: Mapping[str, Any],
    trace_entry: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Detect a preserved object being moved while something is touching it.

    This fires before the completion predicate breaks. Displacement alone is not
    enough — an object nudged by settling physics is not the same event as an
    object being carried — so contact must be observed in the same step.
    """

    if invariant.get("enabled") is not True:
        return None
    threshold = float(invariant["maximum_displacement_metres"])
    witnesses = trace_entry.get("object_witnesses")
    if not isinstance(witnesses, Mapping):
        return None
    for name in invariant["protected_object_names"]:
        witness = witnesses.get(name)
        if not isinstance(witness, Mapping):
            continue
        if witness.get("gripper_contact_observed") is not True:
            continue
        reference = invariant["reference_position_metres"][name]
        position = witness.get("position_metres")
        if isinstance(position, (str, bytes)) or not isinstance(position, Sequence):
            continue
        if len(position) != 3:
            continue
        displacement = float(
            np.linalg.norm(np.asarray(position, dtype=np.float64) - np.asarray(reference))
        )
        if displacement <= threshold:
            continue
        return {
            "evidence_type": EVIDENCE_TYPE_PRESERVATION_INVARIANT,
            "object_name": name,
            "chunk_index": trace_entry["chunk_index"],
            "action_step_index": trace_entry["action_step_index"],
            "action_step_number": trace_entry["action_step_number"],
            "global_repair_step_index": trace_entry["global_repair_step_index"],
            "global_repair_step_number": trace_entry["global_repair_step_number"],
            "reference_position_metres": list(reference),
            "observed_position_metres": [float(value) for value in position],
            "displacement_metres": displacement,
            "maximum_displacement_metres": threshold,
            "contact_observed": True,
            "goal_predicate_still_satisfied": trace_entry["goal_predicate_observations"],
            "root_cause_claimed": False,
        }
    return None


def _without_frame_capture(value: Any) -> Any:
    """Strip every frame record so the Repair receipt digest cannot depend on one.

    Frames are diagnostic. If they reached ``result_sha256`` then a lost render
    would change run identity, which would make an investigation artifact
    load-bearing on the receipt. They are digested separately instead.
    """

    if isinstance(value, Mapping):
        return {
            key: _without_frame_capture(item)
            for key, item in value.items()
            if key != "frame_capture"
        }
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return value
    return [_without_frame_capture(item) for item in value]


def _frame_capture_material(chunk_evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Digest the frame records on their own so they stay tamper-evident."""

    records: list[dict[str, Any]] = []
    for chunk in chunk_evidence:
        trace = chunk.get("preservation_step_trace")
        if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
            continue
        for entry in trace:
            if not isinstance(entry, Mapping) or "frame_capture" not in entry:
                continue
            records.append(
                {
                    "global_repair_step_index": entry.get("global_repair_step_index"),
                    "frame_capture": deepcopy(entry["frame_capture"]),
                }
            )
    return {
        "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
        "authority": FRAME_CAPTURE_AUTHORITY,
        "records": records,
    }


def normalize_preservation_step_trace(
    *,
    environment: str,
    chunk_index: int,
    n_action_steps: int,
    trace: Any,
) -> list[dict[str, Any]]:
    """Validate observations captured after every simulator step in one chunk."""

    if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
        raise RuntimeError("repair_preservation_step_trace_not_sequence")
    if len(trace) != n_action_steps:
        raise RuntimeError("repair_preservation_step_trace_count_mismatch")

    normalized_trace: list[dict[str, Any]] = []
    for action_step_index, raw_entry in enumerate(trace):
        if not isinstance(raw_entry, Mapping):
            raise RuntimeError("repair_preservation_step_trace_entry_not_mapping")
        if raw_entry.get("schema_version") != PRESERVATION_STEP_TRACE_SCHEMA_VERSION:
            raise RuntimeError("repair_preservation_step_trace_schema_mismatch")
        expected_global_index = chunk_index * n_action_steps + action_step_index
        expected_global_number = expected_global_index + 1
        if (
            raw_entry.get("chunk_index") != chunk_index
            or raw_entry.get("action_step_index") != action_step_index
            or raw_entry.get("action_step_number") != action_step_index + 1
            or raw_entry.get("global_repair_step_index") != expected_global_index
            or raw_entry.get("global_repair_step_number") != expected_global_number
        ):
            raise RuntimeError("repair_preservation_step_trace_index_mismatch")
        action_step_sha256 = raw_entry.get("action_step_sha256")
        if not isinstance(action_step_sha256, str) or not action_step_sha256:
            raise RuntimeError("repair_preservation_step_trace_action_digest_missing")

        predicates = normalize_goal_predicates(
            environment=environment,
            observations=raw_entry.get("goal_predicate_observations") or [],
        )
        vector_sha256 = canonical_sha256({"goal_predicate_observations": predicates})
        if raw_entry.get("goal_predicate_vector_sha256") != vector_sha256:
            raise RuntimeError("repair_preservation_step_trace_predicate_digest_mismatch")
        conjunction = all(item["satisfied"] for item in predicates)
        official_result = raw_entry.get("official_predicate_result")
        if not isinstance(official_result, bool):
            raise RuntimeError("repair_preservation_step_trace_official_result_missing")
        if (
            raw_entry.get("official_predicate_conjunction") is not conjunction
            or official_result is not conjunction
            or raw_entry.get("conjunction_matches_official_result") is not True
        ):
            raise RuntimeError("repair_preservation_step_trace_conjunction_mismatch")

        raw_witnesses = raw_entry.get("object_witnesses")
        if not isinstance(raw_witnesses, Mapping) or set(raw_witnesses) != {
            "moka_pot_1",
            "moka_pot_2",
        }:
            raise RuntimeError("repair_preservation_step_trace_object_witnesses_invalid")
        witnesses: dict[str, dict[str, Any]] = {}
        for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
            raw_witness = raw_witnesses[object_name]
            if (
                not isinstance(raw_witness, Mapping)
                or raw_witness.get("object_name") != object_name
            ):
                raise RuntimeError("repair_preservation_step_trace_object_witness_invalid")
            raw_region = raw_witness.get("stove_region_witness")
            if not isinstance(raw_region, Mapping):
                raise RuntimeError("repair_preservation_step_trace_region_witness_missing")
            raw_margins = raw_region.get("axis_margins_metres")
            if not isinstance(raw_margins, Mapping) or set(raw_margins) != {
                "x",
                "y",
                "z_lower",
                "z_upper",
            }:
                raise RuntimeError("repair_preservation_step_trace_region_margins_invalid")
            margins = {
                name: _finite_number(value, field=f"{object_name}_{name}_margin")
                for name, value in raw_margins.items()
            }
            local_delta = _numeric_vector(
                raw_region.get("local_delta_metres"),
                field=f"{object_name}_region_delta",
                length=3,
            )
            half_extent = _numeric_vector(
                raw_region.get("half_extent_metres"),
                field=f"{object_name}_region_half_extent",
                length=3,
            )
            expected_margins = {
                "x": half_extent[0] - abs(local_delta[0]),
                "y": half_extent[1] - abs(local_delta[1]),
                "z_lower": local_delta[2] - (half_extent[2] - 0.005),
                "z_upper": (half_extent[2] + 0.10) - local_delta[2],
            }
            if any(
                not np.isclose(margins[name], expected, rtol=1e-9, atol=1e-12)
                for name, expected in expected_margins.items()
            ):
                raise RuntimeError("repair_preservation_step_trace_region_margin_mismatch")
            inside_region = raw_region.get("inside_under_region")
            parent_contact = raw_region.get("stove_parent_contact_observed")
            on_witness = raw_region.get("on_predicate_witness")
            if not all(
                isinstance(value, bool) for value in (inside_region, parent_contact, on_witness)
            ):
                raise RuntimeError("repair_preservation_step_trace_region_boolean_missing")
            expected_inside = all(value > 0.0 for value in margins.values())
            if inside_region is not expected_inside or on_witness is not (
                inside_region and parent_contact
            ):
                raise RuntimeError("repair_preservation_step_trace_region_witness_mismatch")
            if on_witness is not predicates[predicate_index]["satisfied"]:
                raise RuntimeError("repair_preservation_step_trace_on_predicate_mismatch")
            gripper_contact = raw_witness.get("gripper_contact_observed")
            if not isinstance(gripper_contact, bool):
                raise RuntimeError("repair_preservation_step_trace_gripper_contact_missing")
            witnesses[object_name] = {
                "object_name": object_name,
                "position_metres": _numeric_vector(
                    raw_witness.get("position_metres"),
                    field=f"{object_name}_position",
                    length=3,
                ),
                "quaternion_wxyz": _numeric_vector(
                    raw_witness.get("quaternion_wxyz"),
                    field=f"{object_name}_quaternion",
                    length=4,
                ),
                "linear_velocity_metres_per_second": _numeric_vector(
                    raw_witness.get("linear_velocity_metres_per_second"),
                    field=f"{object_name}_linear_velocity",
                    length=3,
                ),
                "angular_velocity_radians_per_second": _numeric_vector(
                    raw_witness.get("angular_velocity_radians_per_second"),
                    field=f"{object_name}_angular_velocity",
                    length=3,
                ),
                "step_translation_distance_metres": _finite_number(
                    raw_witness.get("step_translation_distance_metres"),
                    field=f"{object_name}_step_translation_distance",
                    minimum=0.0,
                ),
                "end_effector_distance_metres": _finite_number(
                    raw_witness.get("end_effector_distance_metres"),
                    field=f"{object_name}_end_effector_distance",
                    minimum=0.0,
                ),
                "gripper_contact_observed": gripper_contact,
                "stove_region_witness": {
                    "region_name": "flat_stove_1_cook_region",
                    "local_delta_metres": local_delta,
                    "half_extent_metres": half_extent,
                    "axis_margins_metres": margins,
                    "inside_under_region": inside_region,
                    "stove_parent_contact_observed": parent_contact,
                    "on_predicate_witness": on_witness,
                },
            }

        normalized_trace.append(
            {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": chunk_index,
                "action_step_index": action_step_index,
                "action_step_number": action_step_index + 1,
                "global_repair_step_index": expected_global_index,
                "global_repair_step_number": expected_global_number,
                "action_step_sha256": action_step_sha256,
                "goal_predicate_observations": predicates,
                "goal_predicate_vector_sha256": vector_sha256,
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": official_result,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
                "frame_capture": normalize_frame_capture(raw_entry.get("frame_capture")),
            }
        )
    return normalized_trace


def _preservation_transitions(
    *,
    previous_vector: Sequence[Mapping[str, Any]],
    current_vector: Sequence[Mapping[str, Any]],
    preserve_ids: set[str],
    trace_entry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    previous_by_id = {str(item["predicate_id"]): item for item in previous_vector}
    transitions: list[dict[str, Any]] = []
    for item in current_vector:
        predicate_id = str(item["predicate_id"])
        previous = previous_by_id[predicate_id]
        if (
            predicate_id not in preserve_ids
            or previous.get("satisfied") is not True
            or item.get("satisfied") is not False
        ):
            continue
        arguments = list(item["arguments"])
        object_name = arguments[0] if item["predicate_name"] == "on" else None
        object_witness = (
            deepcopy(trace_entry["object_witnesses"][object_name])
            if object_name in trace_entry["object_witnesses"]
            else None
        )
        mechanism = "predicate_changed_without_object_witness"
        if object_witness is not None:
            region = object_witness["stove_region_witness"]
            if region["inside_under_region"] is False:
                mechanism = "object_left_stove_region"
            elif region["stove_parent_contact_observed"] is False:
                mechanism = "object_lost_stove_contact"
            else:
                raise RuntimeError("repair_preservation_transition_witness_inconsistent")
        transitions.append(
            {
                "chunk_index": trace_entry["chunk_index"],
                "action_step_index": trace_entry["action_step_index"],
                "action_step_number": trace_entry["action_step_number"],
                "global_repair_step_index": trace_entry["global_repair_step_index"],
                "global_repair_step_number": trace_entry["global_repair_step_number"],
                "predicate_id": predicate_id,
                "predicate_name": item["predicate_name"],
                "arguments": arguments,
                "prior_satisfied": True,
                "current_satisfied": False,
                "observed_failure_mechanism": mechanism,
                "object_witness": object_witness,
                "root_cause_claimed": False,
            }
        )
    return transitions


def _scene8_repair_instruction(
    target_indices: set[int],
    *,
    instruction_variant: str,
) -> str:
    semantic_instructions = {
        frozenset({0}): (
            "Place the first moka pot on the stove. Keep the second moka pot on the "
            "stove and keep the stove turned on."
        ),
        frozenset({1}): (
            "Place the second moka pot on the stove. Keep the first moka pot on the "
            "stove and keep the stove turned on."
        ),
    }
    short_target_instructions = {
        frozenset({0}): "put the first moka pot on the stove",
        frozenset({1}): "put the second moka pot on the stove",
    }
    if instruction_variant not in REPAIR_INSTRUCTION_VARIANTS:
        raise ValueError("repair_instruction_variant_not_supported")
    try:
        if instruction_variant == "original_task":
            # This exact instruction is the frozen LIBERO task language. It is
            # a controlled ablation, not a semantic Repair instruction.
            semantic_instructions[frozenset(target_indices)]
            return "put both moka pots on the stove"
        if instruction_variant == "short_target":
            return short_target_instructions[frozenset(target_indices)]
        return semantic_instructions[frozenset(target_indices)]
    except KeyError as error:
        raise ValueError("scene8_repair_target_not_supported") from error


def _instruction_ablation_material(variant: str) -> dict[str, Any]:
    return {
        "controlled_variable": "repair_instruction",
        "variant": variant,
        "target_specific_instruction": variant != "original_task",
        "fixed_comparison_metrics": list(REPAIR_INSTRUCTION_ABLATION_METRICS),
        "root_cause_established": False,
    }


def _validate_proposal_instruction_binding(proposal: Mapping[str, Any]) -> None:
    """Re-derive the fixed instruction at the authority boundary."""

    variant = str(proposal.get("repair_instruction_variant") or "")
    if variant not in REPAIR_INSTRUCTION_VARIANTS:
        raise ValueError("repair_instruction_variant_not_supported")
    normalized = normalize_goal_predicates(
        environment=str(proposal.get("environment") or ""),
        observations=proposal.get("source_goal_predicate_observations") or (),
    )
    unsatisfied = [item for item in normalized if not item["satisfied"]]
    expected_target_ids = [item["predicate_id"] for item in unsatisfied]
    if list(proposal.get("target_predicate_ids") or ()) != expected_target_ids:
        raise ValueError("repair_instruction_target_binding_mismatch")
    expected_instruction = _scene8_repair_instruction(
        {int(item["predicate_index"]) for item in unsatisfied},
        instruction_variant=variant,
    )
    if proposal.get("repair_instruction") != expected_instruction:
        raise ValueError("repair_instruction_catalog_binding_mismatch")
    expected_digest = canonical_sha256({"repair_instruction": expected_instruction})
    if proposal.get("repair_instruction_sha256") != expected_digest:
        raise ValueError("repair_instruction_digest_binding_mismatch")
    contract = proposal.get("repair_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("repair_contract_required")
    if contract.get("repair_instruction_variant") != variant:
        raise ValueError("repair_contract_instruction_variant_binding_mismatch")
    if contract.get("repair_instruction_sha256") != expected_digest:
        raise ValueError("repair_contract_instruction_digest_binding_mismatch")
    if contract.get("instruction_ablation") != _instruction_ablation_material(variant):
        raise ValueError("repair_contract_instruction_ablation_binding_mismatch")
    execution_adapter = proposal.get("execution_adapter")
    if execution_adapter not in SUPPORTED_EXECUTION_ADAPTERS:
        raise ValueError("same_world_repair_execution_adapter_not_supported")
    if contract.get("execution_adapter") != execution_adapter:
        raise ValueError("repair_contract_execution_adapter_binding_mismatch")
    n_action_steps = contract.get("n_action_steps")
    if execution_adapter == DEFAULT_EXECUTION_ADAPTER and n_action_steps != 8:
        raise ValueError("isaac_groot_zmq_requires_eight_action_steps")
    if execution_adapter == LEROBOT_GROOT_N17_EXECUTION_ADAPTER and n_action_steps != 16:
        raise ValueError("lerobot_groot_n17_requires_sixteen_action_steps")
    if execution_adapter == VLA0_LIBERO_EXECUTION_ADAPTER and n_action_steps != 1:
        raise ValueError("vla0_libero_requires_one_action_step")
    _validate_state_continuity_binding(proposal)


def _validate_state_continuity_binding(proposal: Mapping[str, Any]) -> None:
    """Re-derive the diagnostic/live split from bound proposal material."""

    contract = proposal.get("repair_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("repair_contract_required")
    basis = proposal.get("state_continuity_basis", STATE_CONTINUITY_LIVE_SAME_WORLD)
    snapshot_sha256 = proposal.get("diagnostic_handoff_snapshot_sha256")
    eligibility = proposal.get("semantic_repair_claim_eligible", True)
    if basis == STATE_CONTINUITY_LIVE_SAME_WORLD:
        if snapshot_sha256 is not None or eligibility is not True:
            raise ValueError("live_state_continuity_binding_mismatch")
        if contract.get("state_continuity_basis") is not None:
            raise ValueError("live_contract_state_continuity_binding_mismatch")
        if contract.get("diagnostic_handoff_snapshot_sha256") is not None:
            raise ValueError("live_contract_snapshot_binding_mismatch")
        if contract.get("semantic_repair_claim_eligible") is not None:
            raise ValueError("live_contract_claim_eligibility_binding_mismatch")
        return
    if basis != STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE:
        raise ValueError("state_continuity_basis_not_supported")
    if not _SHA256_PATTERN.fullmatch(str(snapshot_sha256 or "")):
        raise ValueError("diagnostic_handoff_snapshot_sha256_invalid")
    if eligibility is not False:
        raise ValueError("diagnostic_state_continuity_claim_eligibility_mismatch")
    if contract.get("state_continuity_basis") != basis:
        raise ValueError("diagnostic_contract_state_continuity_binding_mismatch")
    if contract.get("diagnostic_handoff_snapshot_sha256") != snapshot_sha256:
        raise ValueError("diagnostic_contract_snapshot_binding_mismatch")
    if contract.get("semantic_repair_claim_eligible") is not False:
        raise ValueError("diagnostic_contract_claim_eligibility_binding_mismatch")


def verify_exact_repair_instruction_payload(
    *,
    payload: Any,
    expected_instruction: str,
) -> dict[str, Any]:
    """Read back and digest the exact language value sent to the policy."""

    instruction = str(expected_instruction or "")
    if not instruction:
        raise ValueError("repair_instruction_required")
    if isinstance(payload, np.ndarray):
        if payload.size != 1:
            raise ValueError("repair_instruction_payload_must_contain_one_value")
        value = payload.reshape(-1)[0]
        actual_instruction = value.item() if hasattr(value, "item") else value
        payload_kind = "numpy.ndarray"
        payload_dtype = str(payload.dtype)
        payload_shape = list(payload.shape)
    elif isinstance(payload, (list, tuple)):
        if len(payload) != 1:
            raise ValueError("repair_instruction_payload_must_contain_one_value")
        actual_instruction = payload[0]
        payload_kind = type(payload).__name__
        payload_dtype = None
        payload_shape = [1]
    else:
        raise ValueError("repair_instruction_payload_container_not_supported")
    if not isinstance(actual_instruction, str):
        raise ValueError("repair_instruction_payload_value_not_string")

    actual_sha256 = canonical_sha256({"repair_instruction": actual_instruction})
    expected_sha256 = canonical_sha256({"repair_instruction": instruction})
    exact_match = actual_instruction == instruction and actual_sha256 == expected_sha256
    evidence = {
        "repair_instruction_payload_exact_match": exact_match,
        "repair_instruction_payload_sha256": actual_sha256,
        "repair_instruction_payload_length": len(actual_instruction),
        "repair_instruction_payload_kind": payload_kind,
        "repair_instruction_payload_dtype": payload_dtype,
        "repair_instruction_payload_shape": payload_shape,
    }
    if not exact_match:
        raise ValueError("repair_instruction_payload_exact_match_failed")
    return evidence


def build_exact_repair_instruction_payload(
    *,
    current_language: Any,
    instruction: str,
) -> tuple[Any, dict[str, Any]]:
    """Build a one-item policy payload without reusing a truncating string dtype."""

    if isinstance(current_language, np.ndarray):
        if current_language.size != 1:
            raise ValueError("repair_instruction_source_must_contain_one_value")
        if current_language.dtype.kind not in {"O", "U"}:
            raise ValueError("repair_instruction_source_dtype_not_supported")
        payload = np.asarray([instruction], dtype=f"<U{max(1, len(instruction))}").reshape(
            current_language.shape
        )
    elif isinstance(current_language, tuple):
        if len(current_language) != 1:
            raise ValueError("repair_instruction_source_must_contain_one_value")
        payload = (instruction,)
    elif isinstance(current_language, list):
        if len(current_language) != 1:
            raise ValueError("repair_instruction_source_must_contain_one_value")
        payload = [instruction]
    else:
        raise ValueError("repair_instruction_source_container_not_supported")
    evidence = verify_exact_repair_instruction_payload(
        payload=payload,
        expected_instruction=instruction,
    )
    return payload, evidence


def build_same_world_repair_proposal(
    *,
    environment: str,
    environment_session_id: str,
    source_contract_sha256: str,
    source_goal_predicates: Sequence[Mapping[str, Any]],
    reset_count: int,
    maximum_repair_chunks: int = 90,
    n_action_steps: int = 8,
    execution_adapter: str = DEFAULT_EXECUTION_ADAPTER,
    proposal_id: str | None = None,
    proposed_at: str | None = None,
    source_object_poses: Mapping[str, Sequence[float]] | None = None,
    repair_instruction_variant: str = DEFAULT_REPAIR_INSTRUCTION_VARIANT,
    preserved_object_max_displacement_metres: float = (
        DEFAULT_PRESERVED_OBJECT_MAX_DISPLACEMENT_METRES
    ),
    state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
    diagnostic_handoff_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Create an approval-eligible proposal only while the source world is live."""

    if environment != LIBERO_PANDA_SCENE8_ENVIRONMENT:
        raise ValueError("same_world_repair_environment_not_supported")
    if not str(environment_session_id or "").strip():
        raise ValueError("environment_session_id_required")
    if len(str(source_contract_sha256)) != 64:
        raise ValueError("source_contract_sha256_invalid")
    if reset_count != 1:
        raise ValueError("same_world_repair_requires_exactly_one_reset")
    if isinstance(maximum_repair_chunks, bool) or maximum_repair_chunks <= 0:
        raise ValueError("maximum_repair_chunks_invalid")
    if isinstance(n_action_steps, bool) or not 1 <= n_action_steps <= 64:
        raise ValueError("n_action_steps_out_of_bounds")
    if execution_adapter not in SUPPORTED_EXECUTION_ADAPTERS:
        raise ValueError("same_world_repair_execution_adapter_not_supported")
    if execution_adapter == DEFAULT_EXECUTION_ADAPTER and n_action_steps != 8:
        raise ValueError("isaac_groot_zmq_requires_eight_action_steps")
    if execution_adapter == LEROBOT_GROOT_N17_EXECUTION_ADAPTER and n_action_steps != 16:
        raise ValueError("lerobot_groot_n17_requires_sixteen_action_steps")
    if execution_adapter == VLA0_LIBERO_EXECUTION_ADAPTER and n_action_steps != 1:
        raise ValueError("vla0_libero_requires_one_action_step")
    if state_continuity_basis not in STATE_CONTINUITY_BASES:
        raise ValueError("state_continuity_basis_not_supported")
    if state_continuity_basis == STATE_CONTINUITY_LIVE_SAME_WORLD:
        if diagnostic_handoff_snapshot_sha256 is not None:
            raise ValueError("live_same_world_cannot_bind_diagnostic_snapshot")
    elif not _SHA256_PATTERN.fullmatch(str(diagnostic_handoff_snapshot_sha256 or "")):
        raise ValueError("diagnostic_handoff_snapshot_sha256_invalid")

    normalized = normalize_goal_predicates(
        environment=environment,
        observations=source_goal_predicates,
    )
    satisfied = [item for item in normalized if item["satisfied"]]
    unsatisfied = [item for item in normalized if not item["satisfied"]]
    if not satisfied:
        raise ValueError("semantic_repair_has_no_completed_predicate_to_preserve")
    if not unsatisfied:
        raise ValueError("semantic_repair_has_no_unmet_predicate_to_target")
    target_indices = {int(item["predicate_index"]) for item in unsatisfied}
    repair_instruction = _scene8_repair_instruction(
        target_indices,
        instruction_variant=repair_instruction_variant,
    )
    vector_sha256 = canonical_sha256({"goal_predicate_observations": normalized})
    instruction_sha256 = canonical_sha256({"repair_instruction": repair_instruction})
    contract_material = {
        "environment": environment,
        "environment_session_id": environment_session_id,
        "source_contract_sha256": source_contract_sha256,
        "source_goal_predicate_vector_sha256": vector_sha256,
        "preserve_predicate_ids": [item["predicate_id"] for item in satisfied],
        "target_predicate_ids": [item["predicate_id"] for item in unsatisfied],
        "repair_instruction_variant": repair_instruction_variant,
        "repair_instruction_sha256": instruction_sha256,
        "instruction_ablation": _instruction_ablation_material(repair_instruction_variant),
        "maximum_repair_chunks": maximum_repair_chunks,
        "n_action_steps": n_action_steps,
        "execution_adapter": execution_adapter,
        "maximum_repair_steps": maximum_repair_chunks * n_action_steps,
        "verify_after_each_chunk": True,
        "stop_on_success": True,
        "stop_on_preservation_violation": True,
        "preservation_invariant": build_preservation_invariant(
            goal_predicate_observations=normalized,
            preserve_predicate_ids=[item["predicate_id"] for item in satisfied],
            source_object_poses=source_object_poses,
            maximum_displacement_metres=preserved_object_max_displacement_metres,
        ),
        "additional_attempts_allowed": 0,
        "physical_execution_invoked": False,
    }
    if state_continuity_basis != STATE_CONTINUITY_LIVE_SAME_WORLD:
        contract_material.update(
            {
                "state_continuity_basis": state_continuity_basis,
                "diagnostic_handoff_snapshot_sha256": (diagnostic_handoff_snapshot_sha256),
                "semantic_repair_claim_eligible": False,
            }
        )
    repair_contract_sha256 = canonical_sha256(contract_material)
    base = {
        "schema_version": SAME_WORLD_REPAIR_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id or f"groot-libero-same-world-repair:{uuid4()}",
        "proposal_status": "awaiting_operator_approval",
        "proposed_at": proposed_at or _now(),
        "proposal_source": "deterministic_non_model_predicate_diagnosis",
        "environment": environment,
        "environment_session_id": environment_session_id,
        "source_contract_sha256": source_contract_sha256,
        "source_goal_predicate_observations": normalized,
        "source_goal_predicate_vector_sha256": vector_sha256,
        "preserve_predicate_ids": contract_material["preserve_predicate_ids"],
        "target_predicate_ids": contract_material["target_predicate_ids"],
        "repair_instruction_variant": repair_instruction_variant,
        "execution_adapter": execution_adapter,
        "repair_instruction": repair_instruction,
        "repair_instruction_sha256": instruction_sha256,
        "repair_contract": contract_material,
        "repair_contract_sha256": repair_contract_sha256,
        "requires_new_human_approval": True,
        "automatic_retry_allowed": False,
        "same_world_state_observed": (state_continuity_basis == STATE_CONTINUITY_LIVE_SAME_WORLD),
        "diagnostic_cloned_state_observed": (
            state_continuity_basis == STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
        ),
        "state_continuity_basis": state_continuity_basis,
        "diagnostic_handoff_snapshot_sha256": diagnostic_handoff_snapshot_sha256,
        "semantic_repair_claim_eligible": (
            state_continuity_basis == STATE_CONTINUITY_LIVE_SAME_WORLD
        ),
        "reset_count_at_proposal": reset_count,
        "model_judgment_used_for_verifier": False,
        "execution_authority_created": False,
        "physical_execution_invoked": False,
    }
    return {**base, "proposal_sha256": canonical_sha256(base)}


def approve_same_world_repair(
    *,
    proposal: Mapping[str, Any],
    operator_approval_ref: str,
    approval_id: str | None = None,
    approved_at: str | None = None,
) -> dict[str, Any]:
    if proposal.get("schema_version") != SAME_WORLD_REPAIR_PROPOSAL_SCHEMA_VERSION:
        raise ValueError("repair_proposal_not_approval_eligible")
    if proposal.get("proposal_status") != "awaiting_operator_approval":
        raise ValueError("repair_proposal_not_awaiting_operator_approval")
    _require_digest(proposal, "proposal_sha256")
    _validate_proposal_instruction_binding(proposal)
    operator_ref = str(operator_approval_ref or "").strip()
    if not operator_ref:
        raise ValueError("operator_approval_ref_required")
    base = {
        "schema_version": SAME_WORLD_REPAIR_APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id or f"groot-libero-same-world-approval:{uuid4()}",
        "approved_at": approved_at or _now(),
        "operator_approval_ref": operator_ref,
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "repair_contract_sha256": proposal["repair_contract_sha256"],
        "repair_instruction_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_variant": proposal["repair_instruction_variant"],
        "execution_adapter": proposal["execution_adapter"],
        "single_use": True,
        "automatic_dispatch_allowed": False,
        "physical_execution_invoked": False,
    }
    return {**base, "approval_sha256": canonical_sha256(base)}


def build_same_world_repair_dispatch(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    dispatch_ref: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    _require_digest(proposal, "proposal_sha256")
    _validate_proposal_instruction_binding(proposal)
    _require_digest(approval, "approval_sha256")
    if approval.get("proposal_sha256") != proposal.get("proposal_sha256"):
        raise ValueError("approval_proposal_binding_mismatch")
    if approval.get("repair_contract_sha256") != proposal.get("repair_contract_sha256"):
        raise ValueError("approval_contract_binding_mismatch")
    if approval.get("repair_instruction_sha256") != proposal.get("repair_instruction_sha256"):
        raise ValueError("approval_instruction_binding_mismatch")
    if approval.get("repair_instruction_variant") != proposal.get("repair_instruction_variant"):
        raise ValueError("approval_instruction_variant_binding_mismatch")
    if approval.get("execution_adapter") != proposal.get("execution_adapter"):
        raise ValueError("approval_execution_adapter_binding_mismatch")
    normalized_ref = str(dispatch_ref or "").strip()
    if not normalized_ref:
        raise ValueError("dispatch_ref_required")
    base = {
        "schema_version": SAME_WORLD_REPAIR_DISPATCH_SCHEMA_VERSION,
        "dispatch_ref": normalized_ref,
        "created_at": created_at or _now(),
        "environment_session_id": proposal["environment_session_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "approval_sha256": approval["approval_sha256"],
        "repair_contract_sha256": proposal["repair_contract_sha256"],
        "repair_instruction_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_variant": proposal["repair_instruction_variant"],
        "execution_adapter": proposal["execution_adapter"],
        "maximum_repair_chunks": proposal["repair_contract"]["maximum_repair_chunks"],
        "n_action_steps": proposal["repair_contract"]["n_action_steps"],
        "physical_execution_invoked": False,
    }
    return {**base, "dispatch_sha256": canonical_sha256(base)}


def run_same_world_repair(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    dispatch_ledger: DispatchLedger,
    initial_observation: Any,
    invoke_model: Callable[[Any, str, int], tuple[Any, Mapping[str, Any]]],
    apply_action_chunk: Callable[[Any, int], tuple[Any, Mapping[str, Any]]],
    observe_goal_predicates: Callable[[], Sequence[Mapping[str, Any]]],
    observed_reset_count: Callable[[], int],
    observed_state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
) -> dict[str, Any]:
    """Execute one bounded, closed-loop dispatch under the proposal's state basis."""

    _require_digest(proposal, "proposal_sha256")
    _validate_proposal_instruction_binding(proposal)
    proposal_state_continuity_basis = proposal.get(
        "state_continuity_basis", STATE_CONTINUITY_LIVE_SAME_WORLD
    )
    if observed_state_continuity_basis != proposal_state_continuity_basis:
        raise ValueError("observed_state_continuity_basis_mismatch")
    _require_digest(approval, "approval_sha256")
    _require_digest(dispatch, "dispatch_sha256")
    if dispatch.get("proposal_sha256") != proposal.get("proposal_sha256"):
        raise ValueError("dispatch_proposal_binding_mismatch")
    if dispatch.get("approval_sha256") != approval.get("approval_sha256"):
        raise ValueError("dispatch_approval_binding_mismatch")
    if dispatch.get("repair_contract_sha256") != proposal.get("repair_contract_sha256"):
        raise ValueError("dispatch_contract_binding_mismatch")
    if approval.get("execution_adapter") != proposal.get("execution_adapter"):
        raise ValueError("approval_execution_adapter_binding_mismatch")
    if dispatch.get("execution_adapter") != proposal.get("execution_adapter"):
        raise ValueError("dispatch_execution_adapter_binding_mismatch")
    repair_contract = proposal["repair_contract"]
    if dispatch.get("n_action_steps") != repair_contract.get("n_action_steps"):
        raise ValueError("dispatch_action_steps_binding_mismatch")
    if dispatch.get("maximum_repair_chunks") != repair_contract.get("maximum_repair_chunks"):
        raise ValueError("dispatch_chunk_budget_binding_mismatch")
    if observed_reset_count() != 1:
        raise ValueError("same_world_reset_count_changed_before_dispatch")

    request_payload = {
        "dispatch_sha256": dispatch["dispatch_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "approval_sha256": approval["approval_sha256"],
        "repair_contract_sha256": proposal["repair_contract_sha256"],
        "repair_instruction_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_variant": proposal["repair_instruction_variant"],
        "execution_adapter": proposal["execution_adapter"],
        "environment_session_id": proposal["environment_session_id"],
    }
    authority = begin_bounded_chunk_authority(
        policy=BoundedChunkAuthorityPolicy(
            authority_contract_sha256=str(proposal["repair_contract_sha256"]),
            maximum_chunks=int(dispatch["maximum_repair_chunks"]),
            terminal_verdicts=(
                "satisfied",
                "satisfied_diagnostic_observation",
                "predicate_improved",
                "stopped_on_preservation_invariant",
                "stopped_on_preservation_violation",
            ),
            verifier_passed_verdicts=(
                "satisfied",
                "satisfied_diagnostic_observation",
                "predicate_improved",
            ),
            completion_verdicts=("satisfied",),
            budget_exhausted_verdict="budget_exhausted_without_improvement",
        ),
        dispatch_ref=str(dispatch["dispatch_ref"]),
        dispatch_request_payload=request_payload,
        correlation={"environment_session_id": proposal["environment_session_id"]},
        dispatch_ledger=dispatch_ledger,
        contract_binding_field="repair_contract_sha256",
    )

    source_vector = normalize_goal_predicates(
        environment=str(proposal["environment"]),
        observations=proposal["source_goal_predicate_observations"],
    )
    preserve_ids = set(proposal["preserve_predicate_ids"])
    target_ids = set(proposal["target_predicate_ids"])
    prior_target_satisfied = sum(
        item["predicate_id"] in target_ids and item["satisfied"] for item in source_vector
    )
    observation = initial_observation
    chunk_evidence: list[dict[str, Any]] = []
    status = "budget_exhausted_without_improvement"
    final_vector = source_vector
    previous_step_vector = source_vector
    first_preservation_violation: dict[str, Any] | None = None
    first_preservation_invariant_breach: dict[str, Any] | None = None
    preservation_invariant = proposal["repair_contract"].get("preservation_invariant") or {
        "enabled": False
    }
    try:
        for chunk_index in authority.chunk_indices:
            if observed_reset_count() != 1:
                raise RuntimeError("same_world_reset_count_changed_during_repair")
            action_chunk, invocation = invoke_model(
                observation,
                str(proposal["repair_instruction"]),
                chunk_index,
            )
            if invocation.get("model_runtime_invoked") is not True:
                raise RuntimeError("repair_model_runtime_not_invoked")
            if invocation.get("repair_instruction_sha256") != proposal.get(
                "repair_instruction_sha256"
            ):
                raise RuntimeError("repair_model_instruction_binding_mismatch")
            if invocation.get("repair_instruction_payload_exact_match") is not True:
                raise RuntimeError("repair_model_instruction_payload_not_exact")
            if invocation.get("repair_instruction_payload_sha256") != proposal.get(
                "repair_instruction_sha256"
            ):
                raise RuntimeError("repair_model_instruction_payload_digest_mismatch")
            observation, application = apply_action_chunk(action_chunk, chunk_index)
            if application.get("simulator_step_return_observed") is not True:
                raise RuntimeError("repair_simulator_step_return_not_observed")
            if application.get("simulator_effect_observed") is not True:
                raise RuntimeError("repair_simulator_effect_not_observed")
            final_vector = normalize_goal_predicates(
                environment=str(proposal["environment"]),
                observations=observe_goal_predicates(),
            )
            step_trace = normalize_preservation_step_trace(
                environment=str(proposal["environment"]),
                chunk_index=chunk_index,
                n_action_steps=int(dispatch["n_action_steps"]),
                trace=application.get("preservation_step_trace"),
            )
            if step_trace[-1]["goal_predicate_observations"] != final_vector:
                raise RuntimeError("repair_preservation_step_trace_final_vector_mismatch")
            chunk_first_preservation_violation = None
            chunk_observed_violation_ids: set[str] = set()
            chunk_first_invariant_breach = None
            for trace_entry in step_trace:
                transitions = _preservation_transitions(
                    previous_vector=previous_step_vector,
                    current_vector=trace_entry["goal_predicate_observations"],
                    preserve_ids=preserve_ids,
                    trace_entry=trace_entry,
                )
                chunk_observed_violation_ids.update(
                    transition["predicate_id"] for transition in transitions
                )
                if transitions and chunk_first_preservation_violation is None:
                    chunk_first_preservation_violation = transitions[0]
                if transitions and first_preservation_violation is None:
                    first_preservation_violation = transitions[0]
                breach = _preservation_invariant_breach(
                    invariant=preservation_invariant,
                    trace_entry=trace_entry,
                )
                if breach is not None and chunk_first_invariant_breach is None:
                    chunk_first_invariant_breach = breach
                if breach is not None and first_preservation_invariant_breach is None:
                    first_preservation_invariant_breach = breach
                previous_step_vector = trace_entry["goal_predicate_observations"]
            preservation_violations = sorted(
                chunk_observed_violation_ids
                | {
                    item["predicate_id"]
                    for item in final_vector
                    if item["predicate_id"] in preserve_ids and not item["satisfied"]
                }
            )
            target_satisfied = sum(
                item["predicate_id"] in target_ids and item["satisfied"] for item in final_vector
            )
            conjunction = all(item["satisfied"] for item in final_vector)
            official_predicate_result = application.get("official_predicate_result")
            if not isinstance(official_predicate_result, bool):
                raise RuntimeError("repair_official_predicate_result_not_observed")
            if official_predicate_result is not conjunction:
                raise RuntimeError("repair_goal_predicate_conjunction_mismatch")
            if official_predicate_result is not step_trace[-1]["official_predicate_result"]:
                raise RuntimeError("repair_preservation_step_trace_final_official_mismatch")
            if preservation_violations and chunk_first_preservation_violation is None:
                raise RuntimeError("repair_preservation_violation_not_localized_to_step")
            chunk_evidence.append(
                {
                    "chunk_index": chunk_index,
                    "policy_request_sha256": invocation.get("policy_request_sha256"),
                    "policy_response_sha256": invocation.get("policy_response_sha256"),
                    "repair_instruction_payload_exact_match": True,
                    "repair_instruction_payload_sha256": invocation.get(
                        "repair_instruction_payload_sha256"
                    ),
                    "repair_instruction_payload_length": invocation.get(
                        "repair_instruction_payload_length"
                    ),
                    "repair_instruction_payload_kind": invocation.get(
                        "repair_instruction_payload_kind"
                    ),
                    "repair_instruction_payload_dtype": invocation.get(
                        "repair_instruction_payload_dtype"
                    ),
                    "repair_instruction_payload_shape": invocation.get(
                        "repair_instruction_payload_shape"
                    ),
                    "action_chunk_sha256": application.get("action_chunk_sha256"),
                    "simulator_step_return_observed": True,
                    "controller_ack_observed": False,
                    "simulator_effect_observed": True,
                    "goal_predicate_observations": final_vector,
                    "goal_predicate_vector_sha256": canonical_sha256(
                        {"goal_predicate_observations": final_vector}
                    ),
                    "preservation_step_trace": step_trace,
                    "first_preservation_violation": chunk_first_preservation_violation,
                    "first_preservation_invariant_breach": chunk_first_invariant_breach,
                    "preservation_violation_predicate_ids": preservation_violations,
                    "target_satisfied_count": target_satisfied,
                    "official_predicate_conjunction": conjunction,
                    "official_predicate_result": official_predicate_result,
                    "conjunction_matches_official_result": True,
                }
            )
            # The invariant is designed to trip before the completion predicate
            # breaks, so when both fire the earlier step is the one that
            # describes what actually stopped the run.
            if chunk_first_invariant_breach is not None and (
                chunk_first_preservation_violation is None
                or chunk_first_invariant_breach["global_repair_step_index"]
                <= chunk_first_preservation_violation["global_repair_step_index"]
            ):
                status = "stopped_on_preservation_invariant"
                break
            if preservation_violations:
                status = "stopped_on_preservation_violation"
                break
            if conjunction:
                status = (
                    "satisfied"
                    if proposal.get("semantic_repair_claim_eligible", True) is True
                    else "satisfied_diagnostic_observation"
                )
                break
            if target_satisfied > prior_target_satisfied:
                status = "predicate_improved"
                break
        improvement_observed = any(
            item["predicate_id"] in target_ids and item["satisfied"] for item in final_vector
        ) and not any(
            item["predicate_id"] in target_ids and item["satisfied"] for item in source_vector
        )
        receipt = authority.complete(
            status=status,
            chunks_executed=len(chunk_evidence),
            effect_observed=bool(chunk_evidence),
        )
    except Exception as error:
        authority.record_unknown(error)
        raise

    result = {
        "schema_version": SAME_WORLD_REPAIR_RESULT_SCHEMA_VERSION,
        "recorded_at": _now(),
        "environment": proposal["environment"],
        "environment_session_id": proposal["environment_session_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "approval_sha256": approval["approval_sha256"],
        "dispatch_sha256": dispatch["dispatch_sha256"],
        "dispatch_ref": dispatch["dispatch_ref"],
        "repair_contract_sha256": proposal["repair_contract_sha256"],
        "repair_instruction_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_variant": proposal["repair_instruction_variant"],
        "execution_adapter": proposal["execution_adapter"],
        "instruction_ablation": deepcopy(proposal["repair_contract"]["instruction_ablation"]),
        "state_continuity_basis": proposal.get(
            "state_continuity_basis", STATE_CONTINUITY_LIVE_SAME_WORLD
        ),
        "diagnostic_handoff_snapshot_sha256": proposal.get("diagnostic_handoff_snapshot_sha256"),
        "semantic_repair_claim_eligible": proposal.get("semantic_repair_claim_eligible", True),
        "source_goal_predicate_observations": source_vector,
        "final_goal_predicate_observations": final_vector,
        "chunks_executed": len(chunk_evidence),
        "maximum_repair_chunks": dispatch["maximum_repair_chunks"],
        "n_action_steps": dispatch["n_action_steps"],
        "chunk_evidence": chunk_evidence,
        "first_preservation_violation": first_preservation_violation,
        "preservation_violation_localized_to_simulator_step": (
            first_preservation_violation is not None
        ),
        "preservation_invariant_enabled": preservation_invariant.get("enabled") is True,
        "first_preservation_invariant_breach": first_preservation_invariant_breach,
        "preservation_invariant_breach_observed": (first_preservation_invariant_breach is not None),
        "evidence_types_separated": [
            EVIDENCE_TYPE_COMPLETION_PREDICATE,
            EVIDENCE_TYPE_PRESERVATION_INVARIANT,
        ],
        "preservation_stop_granularity": "before_next_model_chunk",
        "already_admitted_chunk_remainder_may_have_executed": (
            first_preservation_violation is not None
        ),
        "admitted_steps_executed_after_first_preservation_violation": (
            int(dispatch["n_action_steps"])
            - int(first_preservation_violation["action_step_number"])
            if first_preservation_violation is not None
            else 0
        ),
        "status": status,
        "predicate_improvement_observed": improvement_observed,
        "predicate_conjunction_observed": status
        in {"satisfied", "satisfied_diagnostic_observation"},
        "task_completion_claimed": (
            status == "satisfied" and proposal.get("semantic_repair_claim_eligible", True) is True
        ),
        "same_world_reset_count": observed_reset_count(),
        "single_reset_observed": observed_reset_count() == 1,
        "same_world_state_preserved": (
            observed_reset_count() == 1
            and observed_state_continuity_basis == STATE_CONTINUITY_LIVE_SAME_WORLD
        ),
        "model_inference_invoked": bool(chunk_evidence),
        "simulator_execution_observed": bool(chunk_evidence),
        "controller_ack_observed": False,
        "dispatch_receipt_present": receipt["receipt_present"],
        "additional_attempt_authorized": False,
        "physical_execution_invoked": False,
        "real_world_safety_claimed": False,
    }
    return {
        **result,
        "frame_capture_authority": FRAME_CAPTURE_AUTHORITY,
        "frame_capture_sha256": canonical_sha256(_frame_capture_material(chunk_evidence)),
        "result_sha256": canonical_sha256(_without_frame_capture(result)),
    }


__all__ = [
    "DEFAULT_REPAIR_INSTRUCTION_VARIANT",
    "REPAIR_INSTRUCTION_ABLATION_METRICS",
    "REPAIR_INSTRUCTION_VARIANTS",
    "approve_same_world_repair",
    "build_exact_repair_instruction_payload",
    "build_same_world_repair_dispatch",
    "build_same_world_repair_proposal",
    "build_preservation_invariant",
    "normalize_frame_capture",
    "normalize_goal_predicates",
    "preserved_object_names",
    "normalize_preservation_step_trace",
    "run_same_world_repair",
    "verify_exact_repair_instruction_payload",
]
