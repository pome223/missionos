"""Loopback-only isolated map Gateway for CPU/offline verification.

Production uses the identical router on the normal authenticated Gateway.
"""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from src.gateway.yokohama_map import build_yokohama_map_router
from src.runtime.task_store import TaskStore

app = FastAPI()


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse({"detail": "Loopback only"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "Same origin only"}, status_code=403)
    return await call_next(request)


app.include_router(build_yokohama_map_router(TaskStore(os.environ.get("MISSIONOS_YOKOHAMA_MAP_DB", "output/yokohama-map/tasks.db")), lambda request: "loopback-operator"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MISSIONOS_YOKOHAMA_MAP_PORT", "8897")))
