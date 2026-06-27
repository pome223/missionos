from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from src.gateway.api_schema import (
    ControlSupervisorAcceptedResponse,
    ControlSupervisorRequest,
    TaskAnalyticsResponse,
    TaskCancelResponse,
    TaskCompareResponse,
    TaskEnvelope,
    TaskQueryResponse,
    TaskReplayAcceptedResponse,
    TaskReplayRequest,
    TaskTimelineResponse,
)
from src.gateway.task_analytics import compute_analytics
from src.gateway.control_supervisor import SupervisorStartResult
from src.gateway.route_utils import normalize_constraints
from src.gateway.task_replay import build_partial_replay_seed, build_task_compare_payload

if TYPE_CHECKING:
    from src.gateway.server import GatewayServer


_TERRAIN_HEIGHTMAP_PREVIEW_MAX_DIM = 33


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _terrain_heightmap_bbox(
    payload: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> list[float] | None:
    raw_bbox = payload.get("bbox") or artifact.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        return None
    bbox = [_coerce_float(value) for value in raw_bbox]
    if any(value is None for value in bbox):
        return None
    return [float(value) for value in bbox if value is not None]


def _decimate_heightmap_grid(
    *,
    normalized_heights: list[float],
    width: int,
    height: int,
) -> tuple[int, int, list[float]]:
    preview_width = min(width, _TERRAIN_HEIGHTMAP_PREVIEW_MAX_DIM)
    preview_height = min(height, _TERRAIN_HEIGHTMAP_PREVIEW_MAX_DIM)
    if width == preview_width and height == preview_height:
        return width, height, list(normalized_heights)

    def source_index(preview_index: int, preview_size: int, source_size: int) -> int:
        if preview_size <= 1:
            return 0
        return round(preview_index * (source_size - 1) / (preview_size - 1))

    preview: list[float] = []
    for preview_y in range(preview_height):
        source_y = source_index(preview_y, preview_height, height)
        for preview_x in range(preview_width):
            source_x = source_index(preview_x, preview_width, width)
            preview.append(normalized_heights[source_y * width + source_x])
    return preview_width, preview_height, preview


def _missionos_terrain_heightmap_preview_grid(
    artifact: Mapping[str, Any],
) -> dict[str, Any] | None:
    file_path_raw = str(artifact.get("file_path_or_artifact_uri") or "").strip()
    expected_sha256 = str(artifact.get("file_sha256") or "").strip()
    if not file_path_raw or not expected_sha256:
        return None
    if not file_path_raw.endswith(".heightmap.json"):
        return None

    heightmap_path = Path(file_path_raw)
    if not heightmap_path.is_absolute():
        heightmap_path = Path.cwd() / heightmap_path
    try:
        payload_bytes = heightmap_path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(payload_bytes).hexdigest() != expected_sha256:
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None

    width = _coerce_positive_int(payload.get("pixel_width") or artifact.get("pixel_width"))
    height = _coerce_positive_int(payload.get("pixel_height") or artifact.get("pixel_height"))
    raw_heights = payload.get("normalized_heights")
    if width is None or height is None or not isinstance(raw_heights, list):
        return None
    if len(raw_heights) != width * height:
        return None

    normalized_heights: list[float] = []
    for raw_value in raw_heights:
        value = _coerce_float(raw_value)
        if value is None or value < 0.0 or value > 1.0:
            return None
        normalized_heights.append(value)

    preview_width, preview_height, preview_heights = _decimate_heightmap_grid(
        normalized_heights=normalized_heights,
        width=width,
        height=height,
    )
    preview: dict[str, Any] = {
        "schema_version": "missionos_terrain_heightmap_preview_grid.v1",
        "source": "terrain_heightmap_file_artifact",
        "source_file_sha256": expected_sha256,
        "source_pixel_width": width,
        "source_pixel_height": height,
        "pixel_width": preview_width,
        "pixel_height": preview_height,
        "normalized_heights": preview_heights,
        "heightfield_decimated": preview_width != width or preview_height != height,
    }
    sample_source = str(payload.get("heightmap_sample_source") or "").strip()
    if sample_source:
        preview["heightmap_sample_source"] = sample_source
    bbox = _terrain_heightmap_bbox(payload, artifact)
    if bbox is not None:
        preview["bbox"] = bbox
    for key in ("elevation_min_m", "elevation_max_m", "vertical_scale_m"):
        value = _coerce_float(payload.get(key) if payload.get(key) is not None else artifact.get(key))
        if value is not None:
            preview[key] = value
    return preview


def enrich_terrain_heightmap_preview_fields(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    heightmap_artifact = record.get("terrain_heightmap_file_artifact")
    if not isinstance(heightmap_artifact, Mapping):
        return dict(record)
    preview = _missionos_terrain_heightmap_preview_grid(heightmap_artifact)
    if not preview:
        return dict(record)

    enriched_record = dict(record)
    enriched_heightmap_artifact = dict(heightmap_artifact)
    enriched_heightmap_artifact["terrain_heightmap_preview_grid"] = preview
    enriched_record["terrain_heightmap_file_artifact"] = enriched_heightmap_artifact
    enriched_record["terrain_heightmap_preview_grid"] = preview
    return enriched_record


def _enrich_task_with_terrain_heightmap_preview(task: dict[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, dict):
        return task
    enriched_artifacts = enrich_terrain_heightmap_preview_fields(artifacts)
    if enriched_artifacts == artifacts:
        return task
    enriched_task = deepcopy(task)
    enriched_task["artifacts"] = enriched_artifacts
    return enriched_task


def build_task_router(server: "GatewayServer") -> APIRouter:
    router = APIRouter(tags=["tasks"])

    @router.get("/tasks", response_model=TaskQueryResponse)
    async def task_list_endpoint(
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: Optional[int] = None,
        limit: int = 20,
    ):
        resolved_page_size = max(1, min(int(page_size or limit or 20), 100))
        payload = server.task_store.query(
            owner_session_id=session_id,
            kind=kind,
            status=status,
            parent_task_id=parent_task_id,
            q=q,
            page=page,
            page_size=resolved_page_size,
        )
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            payload = dict(payload)
            payload["tasks"] = [
                _enrich_task_with_terrain_heightmap_preview(task)
                if isinstance(task, dict)
                else task
                for task in tasks
            ]
        return payload

    @router.get("/tasks/analytics", response_model=TaskAnalyticsResponse)
    async def task_analytics_endpoint(
        user_id: Optional[str] = None,
    ):
        return compute_analytics(
            server.task_store,
            owner_user_id=user_id or None,
        )

    @router.get("/tasks/{task_id}", response_model=TaskEnvelope)
    async def task_get_endpoint(task_id: str):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return {"task": _enrich_task_with_terrain_heightmap_preview(task)}

    @router.get("/tasks/{task_id}/timeline", response_model=TaskTimelineResponse)
    async def task_timeline_endpoint(
        task_id: str,
        page: int = 1,
        page_size: Optional[int] = None,
        limit: int = 50,
    ):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        resolved_page_size = max(1, min(int(page_size or limit or 50), 200))
        return server._build_task_timeline_payload(
            task,
            page=page,
            page_size=resolved_page_size,
        )

    @router.post("/tasks/{task_id}/replay", response_model=TaskReplayAcceptedResponse)
    async def task_replay_endpoint(
        request: Request,
        task_id: str,
        replay_request: TaskReplayRequest | None = Body(default=None),
    ):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if str(task.get("kind") or "") != "control_loop":
            raise HTTPException(status_code=400, detail="task replay currently supports control_loop tasks only")
        artifacts = task.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        resume_context = artifacts.get("resume_context")
        resume_context = resume_context if isinstance(resume_context, dict) else {}
        goal = str(resume_context.get("goal") or task.get("title") or "").strip()
        constraints = normalize_constraints(resume_context.get("constraints"))
        session_id = str(task.get("owner_session_id") or "").strip()
        user_id = str(task.get("owner_user_id") or "").strip()
        if not goal or not session_id or not user_id:
            raise HTTPException(status_code=400, detail="task is missing replay context")
        effective_user_id = server._resolve_http_user_id(
            request,
            user_id,
            default_user_id=user_id,
        )
        if effective_user_id != user_id:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        replay_request = replay_request or TaskReplayRequest()
        replay_from_step = str(replay_request.from_step or "").strip()
        replay_mode = "tail" if replay_from_step else "full"
        initial_state = None
        if replay_from_step:
            initial_state = build_partial_replay_seed(
                task,
                from_step=replay_from_step,
            )

        replay_task = server._create_control_loop_task_record(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=constraints,
            request_id=None,
            source="http",
            parent_task_id=task_id,
            replay_of_task_id=task_id,
            compare_to_task_id=task_id,
            replay_from_step=replay_from_step or None,
            replay_mode=replay_mode,
        )
        replay_task_id = str(replay_task["task_id"])
        await server._start_control_loop_run(
            session_id=session_id,
            user_id=user_id,
            goal=goal,
            constraints=constraints,
            task_id=replay_task_id,
            parent_task_id=task_id,
            replay_of_task_id=task_id,
            compare_to_task_id=task_id,
            initial_state=initial_state,
            reset_if_terminal=True,
        )
        return {
            "accepted": True,
            "task": replay_task,
            "replay_of_task_id": task_id,
            "compare_to_task_id": task_id,
            "replay_from_step": replay_from_step or None,
            "replay_mode": replay_mode,
        }

    @router.get("/tasks/{task_id}/compare", response_model=TaskCompareResponse)
    async def task_compare_endpoint(task_id: str, other_task_id: Optional[str] = None):
        left_task = server.task_store.get(task_id)
        if left_task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

        candidate_task_id = str(other_task_id or "").strip()
        if not candidate_task_id:
            candidate_task_id = str(left_task.get("parent_task_id") or "").strip()
        if not candidate_task_id:
            children = server.task_store.query(
                owner_session_id=left_task.get("owner_session_id"),
                parent_task_id=left_task.get("task_id"),
                page=1,
                page_size=1,
            )
            child_tasks = children.get("tasks")
            child_tasks = child_tasks if isinstance(child_tasks, list) else []
            if child_tasks:
                candidate_task_id = str(child_tasks[0].get("task_id") or "").strip()
        if not candidate_task_id:
            raise HTTPException(status_code=400, detail="comparison task could not be determined")

        right_task = server.task_store.get(candidate_task_id)
        if right_task is None:
            raise HTTPException(status_code=404, detail=f"comparison task not found: {candidate_task_id}")
        return build_task_compare_payload(
            left_task,
            right_task,
            build_task_timeline_payload=server._build_task_timeline_payload,
        )

    @router.post(
        "/tasks/supervisors/control-loop",
        response_model=ControlSupervisorAcceptedResponse,
    )
    async def start_control_supervisor_endpoint(
        request: Request,
        payload: ControlSupervisorRequest,
    ):
        requested_user_id = str(payload.user_id or "api_user")
        user_id = server._resolve_http_user_id(
            request,
            requested_user_id,
            default_user_id="api_user",
        )
        session = await server._get_or_create_gateway_session(
            user_id=user_id,
            session_id=str(payload.session_id) if payload.session_id else None,
        )
        mission_contract = payload.mission_contract
        objective = str(payload.goal or "").strip()
        if mission_contract is not None:
            objective = mission_contract.objective
        if not objective:
            raise HTTPException(
                status_code=400,
                detail="goal or mission_contract.objective is required",
            )
        result: SupervisorStartResult = await server.control_supervisor.start(
            user_id=user_id,
            owner_session_id=session.id,
            objective=objective,
            constraints=normalize_constraints(payload.constraints),
            duration_seconds=payload.duration_seconds,
            interval_seconds=payload.interval_seconds,
            source="http",
            maintenance_goal=str(payload.maintenance_goal or "").strip() or None,
            request_id=None,
            mission_contract=mission_contract,
            approved_promotion_artifacts=payload.approved_promotion_artifacts,
        )
        return {
            "accepted": True,
            "task": result.task,
            "control_session_id": result.control_session_id,
            "duration_seconds": payload.duration_seconds,
            "interval_seconds": payload.interval_seconds,
            "max_iterations": result.max_iterations,
            "ends_at": result.ends_at,
            "next_run_at": result.next_run_at,
            "mission_contract": result.mission_contract,
            "reuse_plan": result.reuse_plan,
        }

    @router.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
    async def cancel_task_endpoint(request: Request, task_id: str):
        task = server.task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        user_id = str(task.get("owner_user_id") or "").strip()
        effective_user_id = server._resolve_http_user_id(
            request,
            user_id,
            default_user_id=user_id or "api_user",
        )
        if user_id and effective_user_id != user_id:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if str(task.get("kind") or "") != "control_supervisor":
            raise HTTPException(status_code=400, detail="task cancel currently supports control_supervisor tasks only")
        updated = await server.control_supervisor.request_stop(task_id)
        if updated is None:
            raise HTTPException(status_code=409, detail="task is not currently running")
        return {
            "accepted": True,
            "task": updated,
            "message": "Graceful stop requested; the supervisor will stop after the current iteration.",
        }

    return router
