"""Planning-only payload split artifacts for MissionOS delivery requests.

The planner may use prior MissionOS task records as evidence, but the hard
vehicle contract remains the authority. It produces a bounded per-sortie payload
plan and never dispatches, uploads, or claims delivery progress.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any

from src.runtime.mission_designer_envelope_violation_advisory import (
    MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG,
)


MISSIONOS_PAYLOAD_SPLIT_PLAN_SCHEMA_VERSION = "missionos_payload_split_plan.v1"
MISSIONOS_PAYLOAD_HISTORY_SAMPLE_SCHEMA_VERSION = (
    "missionos_payload_history_sample.v1"
)
MISSIONOS_PAYLOAD_SPLIT_TOOL_NAME = "missionos_payload_split_planner"
MISSIONOS_PAYLOAD_SPLIT_ROUTE_SOURCE = "missionos_payload_split_plan"
MISSIONOS_PAYLOAD_SPLIT_DEFAULT_RESERVE_FRACTION = 0.1

_PAYLOAD_HISTORY_FIELDS: tuple[tuple[str, str], ...] = (
    ("mission_designer_coordinate_pair_route", "payload_weight_kg"),
    ("mission_designer_coordinate_pair_route", "payload_weight_kg_operator_requested"),
    ("mission_designer_coordinate_pair_route", "requested_total_payload_weight_kg"),
    ("px4_gazebo_mission_scenario_proposal", "payload_weight_kg"),
    ("scenario_proposal", "payload_weight_kg"),
    ("mission_scenario_designer_summary", "payload_weight_kg"),
    ("summary", "payload_weight_kg"),
)


def _utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _rounded(value: float) -> float:
    return round(float(value), 3)


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def requested_payload_weight_from_route(
    coordinate_route: Mapping[str, Any] | None,
) -> float | None:
    """Return the operator's total requested payload for a route, if present."""

    route = _as_mapping(coordinate_route)
    for key in (
        "requested_total_payload_weight_kg",
        "payload_weight_kg_operator_requested_total",
        "payload_weight_kg_operator_requested",
        "payload_weight_kg",
    ):
        value = _as_float(route.get(key))
        if value is not None:
            return value
    return None


def _planning_max_payload_kg(
    *,
    hard_contract_max_payload_weight_kg: float,
    reserve_fraction: float,
) -> float:
    reserve = min(max(float(reserve_fraction), 0.0), 0.5)
    limit = hard_contract_max_payload_weight_kg * (1.0 - reserve)
    return _rounded(max(0.001, limit))


def _payload_history_example(task: Mapping[str, Any]) -> dict[str, Any] | None:
    artifacts = _as_mapping(task.get("artifacts"))
    for artifact_key, field_name in _PAYLOAD_HISTORY_FIELDS:
        artifact = _as_mapping(artifacts.get(artifact_key))
        value = _as_float(artifact.get(field_name))
        if value is not None:
            return {
                "task_id": str(task.get("task_id") or ""),
                "task_ref": f"task:{task.get('task_id')}",
                "task_kind": str(task.get("kind") or ""),
                "task_status": str(task.get("status") or ""),
                "payload_weight_kg": _rounded(value),
                "source_field": f"{artifact_key}.{field_name}",
                "updated_at": task.get("updated_at"),
            }
    direct_value = _as_float(artifacts.get("payload_weight_kg"))
    if direct_value is not None:
        return {
            "task_id": str(task.get("task_id") or ""),
            "task_ref": f"task:{task.get('task_id')}",
            "task_kind": str(task.get("kind") or ""),
            "task_status": str(task.get("status") or ""),
            "payload_weight_kg": _rounded(direct_value),
            "source_field": "artifacts.payload_weight_kg",
            "updated_at": task.get("updated_at"),
        }
    return None


def _query_payload_history(
    *,
    task_store: Any | None,
    max_records: int,
) -> list[dict[str, Any]]:
    if task_store is None:
        try:
            from src.runtime.task_store import get_task_store

            task_store = get_task_store()
        except Exception:
            return []
    try:
        result = task_store.query(q="payload_weight_kg", page=1, page_size=max_records)
        tasks = result.get("tasks") if isinstance(result, Mapping) else []
    except Exception:
        tasks = []
    if not tasks:
        try:
            tasks = task_store.list(limit=max_records)
        except Exception:
            tasks = []
    examples: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for task in (tasks if isinstance(tasks, list) else []):
        if not isinstance(task, Mapping):
            continue
        example = _payload_history_example(task)
        if not example:
            continue
        task_id = str(example.get("task_id") or "")
        if task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)
        examples.append(example)
    return examples


def _build_historical_evidence(
    *,
    task_store: Any | None,
    max_records: int,
) -> dict[str, Any]:
    examples = _query_payload_history(task_store=task_store, max_records=max_records)
    status_counts: dict[str, int] = {}
    for example in examples:
        status = str(example.get("task_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    payload_values = [
        float(example["payload_weight_kg"])
        for example in examples
        if _as_float(example.get("payload_weight_kg")) is not None
    ]
    source_task_refs = [str(example["task_ref"]) for example in examples[:10]]
    return {
        "schema_version": MISSIONOS_PAYLOAD_HISTORY_SAMPLE_SCHEMA_VERSION,
        "evidence_source": "task_store_query",
        "query": "payload_weight_kg",
        "task_sample_count": len(examples),
        "payload_observation_count": len(payload_values),
        "source_task_refs": source_task_refs,
        "status_counts": status_counts,
        "max_observed_payload_weight_kg": (
            _rounded(max(payload_values)) if payload_values else None
        ),
        "examples": examples[:5],
        "historical_evidence_is_authority": False,
        "memory_used_for_planning_only": True,
    }


def _split_payloads(
    *,
    requested_payload_weight_kg: float,
    planning_max_payload_weight_kg_per_drone: float,
) -> list[float]:
    if requested_payload_weight_kg <= 0:
        return []
    count = max(
        1,
        math.ceil(
            requested_payload_weight_kg / planning_max_payload_weight_kg_per_drone
        ),
    )
    while True:
        base = round(requested_payload_weight_kg / count, 3)
        payloads = [base for _ in range(count)]
        delta = round(requested_payload_weight_kg - sum(payloads), 3)
        if payloads:
            payloads[-1] = round(payloads[-1] + delta, 3)
        if max(payloads, default=0.0) <= planning_max_payload_weight_kg_per_drone + 1e-9:
            return payloads
        count += 1


def build_missionos_payload_split_plan(
    *,
    requested_payload_weight_kg: float | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    task_store: Any | None = None,
    now: datetime | None = None,
    max_history_records: int = 50,
    reserve_fraction: float = MISSIONOS_PAYLOAD_SPLIT_DEFAULT_RESERVE_FRACTION,
) -> dict[str, Any]:
    """Build a planning artifact that splits a heavy package across sorties."""

    route = _as_mapping(coordinate_route)
    requested = (
        _as_float(requested_payload_weight_kg)
        if requested_payload_weight_kg is not None
        else requested_payload_weight_from_route(route)
    )
    observed_at = _utc(now).isoformat()
    hard_limit = float(MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG)
    planning_limit = _planning_max_payload_kg(
        hard_contract_max_payload_weight_kg=hard_limit,
        reserve_fraction=reserve_fraction,
    )
    evidence = _build_historical_evidence(
        task_store=task_store,
        max_records=max(1, min(int(max_history_records or 50), 100)),
    )
    if requested is None or requested <= 0:
        payloads: list[float] = []
        plan_status = "not_applicable"
    else:
        payloads = _split_payloads(
            requested_payload_weight_kg=requested,
            planning_max_payload_weight_kg_per_drone=planning_limit,
        )
        plan_status = (
            "split_required"
            if len(payloads) > 1
            else "single_vehicle_within_planning_limit"
        )

    plan_id = _stable_id(
        "missionos_payload_split_plan",
        {
            "route_id": route.get("route_id") or "",
            "requested_payload_weight_kg": _rounded(requested) if requested else None,
            "planning_max_payload_weight_kg_per_drone": planning_limit,
            "payloads": payloads,
            "source_task_refs": evidence.get("source_task_refs") or [],
        },
    )
    plan_ref = f"missionos_payload_split_plan:{plan_id}"
    sorties = [
        {
            "sortie_id": f"{plan_id}_sortie_{index + 1:02d}",
            "vehicle_label": f"drone-{index + 1:02d}",
            "payload_weight_kg": payload,
            "within_hard_contract": payload <= hard_limit + 1e-9,
            "within_planning_margin": payload <= planning_limit + 1e-9,
            "dispatch_authority_created": False,
            "px4_mission_upload_performed": False,
            "delivery_completion_claimed": False,
            "progress_counted": False,
        }
        for index, payload in enumerate(payloads)
    ]
    source_refs = list(evidence.get("source_task_refs") or [])
    if route.get("route_id"):
        source_refs.insert(0, f"missionos_chief_coordinate_route:{route['route_id']}")

    return {
        "schema_version": MISSIONOS_PAYLOAD_SPLIT_PLAN_SCHEMA_VERSION,
        "tool_name": MISSIONOS_PAYLOAD_SPLIT_TOOL_NAME,
        "plan_id": plan_id,
        "plan_ref": plan_ref,
        "plan_status": plan_status,
        "payload_split_required": plan_status == "split_required",
        "requested_payload_weight_kg": _rounded(requested) if requested else None,
        "hard_contract_max_payload_weight_kg": hard_limit,
        "planning_max_payload_weight_kg_per_drone": planning_limit,
        "planning_margin_fraction": min(max(float(reserve_fraction), 0.0), 0.5),
        "planning_basis": "contract_limit_with_reserved_margin",
        "minimum_drone_count": len(sorties),
        "sortie_count": len(sorties),
        "sorties": sorties,
        "historical_evidence": evidence,
        "source_refs": source_refs,
        "required_action": "operator_review_payload_split_plan_before_any_dispatch",
        "forbidden_action": "automatic_dispatch_or_payload_contract_override",
        "memory_used_for_planning_only": True,
        "historical_evidence_is_authority": False,
        "operator_approval_required": True,
        "automatic_dispatch_suppressed": True,
        "dispatch_authority_created": False,
        "px4_mission_upload_performed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "payload_dropoff_success_claimed": False,
        "progress_counted": False,
        "observed_at": observed_at,
    }


def apply_payload_split_plan_to_coordinate_route(
    *,
    coordinate_route: Mapping[str, Any],
    payload_split_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a route for the first bounded sortie in a split payload plan."""

    route = dict(coordinate_route)
    if payload_split_plan.get("plan_status") != "split_required":
        return route
    sorties = payload_split_plan.get("sorties")
    if not isinstance(sorties, list) or not sorties:
        return route
    first = _as_mapping(sorties[0])
    first_payload = _as_float(first.get("payload_weight_kg"))
    requested_total = _as_float(payload_split_plan.get("requested_payload_weight_kg"))
    if first_payload is None or requested_total is None:
        return route

    plan_ref = str(payload_split_plan.get("plan_ref") or "")
    source_refs = list(route.get("source_refs") or [])
    if plan_ref and plan_ref not in source_refs:
        source_refs.append(plan_ref)

    route.pop("payload_weight_kg_operator_requested", None)
    route.update(
        {
            "payload_weight_kg": _rounded(first_payload),
            "requested_total_payload_weight_kg": _rounded(requested_total),
            "payload_weight_kg_operator_requested_total": _rounded(requested_total),
            "payload_weight_source": MISSIONOS_PAYLOAD_SPLIT_ROUTE_SOURCE,
            "payload_split_plan_ref": plan_ref,
            "payload_split_sortie_id": first.get("sortie_id"),
            "payload_split_sortie_index": 1,
            "payload_split_sortie_count": payload_split_plan.get("sortie_count"),
            "payload_split_planning_max_payload_weight_kg_per_drone": (
                payload_split_plan.get("planning_max_payload_weight_kg_per_drone")
            ),
            "source_refs": source_refs,
            "dispatch_authority_created": False,
            "progress_counted": False,
        }
    )
    return route
