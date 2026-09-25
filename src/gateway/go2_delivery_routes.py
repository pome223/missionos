"""Authenticated, read-only access to the Go2 task's public-shaped media."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.gateway.go2_delivery_chat import KIND, service


def build_go2_delivery_router():
    router = APIRouter()

    @router.get("/missionos/go2/{task_id}/view/{filename}")
    def media(task_id: str, filename: str):
        current = service()
        task = current.store.get(task_id)
        if (
            not task
            or task["kind"] != KIND
            or filename not in {"live.jpg", "delivery.mp4", "result.json"}
        ):
            raise HTTPException(404, "Go2 artifact not found")
        path = current.outputs / task_id / filename
        if not path.is_file():
            raise HTTPException(404, "Go2 artifact not ready")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    return router
