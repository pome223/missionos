"""Depth navigation preparation; execution uses the existing SITL endpoints."""

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.runtime import px4_depth_navigation as depth


def build_depth_navigation_router(*, task_store, resolve_http_user_id):
    router = APIRouter()

    @router.post("/px4-gazebo/depth-navigation/prepare")
    async def prepare(request: Request, payload: dict = Body(...)):
        if set(payload) != {"scene"}:
            raise HTTPException(400, "only an explicit scene is accepted")
        try:
            return depth.prepare(
                task_store,
                payload["scene"],
                owner=resolve_http_user_id(
                    request, None, default_user_id="loopback_local_operator"
                ),
            )
        except depth.DepthNavigationError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router


def approve_depth_navigation(gateway, task, body, request):
    if body.get("mission_assurance_on_deviation") is True:
        raise HTTPException(
            400, "depth navigation does not support deviation-agent mode"
        )
    try:
        return depth.approve(
            gateway.task_store,
            task["task_id"],
            actor=gateway._resolve_http_user_id(
                request, None, default_user_id="loopback_local_operator"
            ),
        )
    except depth.DepthNavigationError as exc:
        raise HTTPException(409, str(exc)) from exc


async def execute_depth_navigation(gateway, task, body):
    if (
        body.get("live_flight_mode") is not True
        or body.get("mission_assurance_on_deviation") is True
    ):
        raise HTTPException(
            400, "depth navigation requires live flight without deviation-agent mode"
        )
    try:
        return await run_in_threadpool(
            depth.execute,
            gateway.task_store,
            task["task_id"],
            body["execution_approval_id"],
        )
    except depth.DepthNavigationError as exc:
        raise HTTPException(409, str(exc)) from exc
