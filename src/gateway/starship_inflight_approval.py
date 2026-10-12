"""Bearer-authenticated local operator HTTP; no public request/grant producer."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from src.runtime.starship_inflight_approval import InflightApprovalError, InflightApprovalLedger, digest

PREFIX = "/missionos/starship/inflight"
_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
_BASE = PREFIX+r"/runs/"+_ID+r"/missions/"+_ID+r"/vehicles/"+_ID
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def same_origin_inflight_operator(request, host, port):
    """Narrow local-console exception; configured Gateway API key still applies."""
    if (host not in {"127.0.0.1", "::1", "localhost"} or request.client is None
            or request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}
            or request.headers.get("X-MissionOS-Operator") != "starship-inflight-v1"
            or request.headers.get("Sec-Fetch-Site") != "same-origin"):
        return False
    path = request.url.path
    valid = (request.method == "GET" and re.fullmatch(_BASE+r"/(pending|requests/"+_ID+r")", path)
        or request.method == "POST" and re.fullmatch(_BASE+r"/requests/"+_ID+r"/(approve|reject)", path)
        and request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json")
    if not valid:
        return False
    try:
        origin, referer = urlsplit(request.headers.get("Origin", "")), urlsplit(request.headers.get("Referer", ""))
        expected_host = f"[{host}]" if host == "::1" else host
        scheme = request.url.scheme
        expected = f"{scheme}://{expected_host}" + (f":{port}" if port != (443 if scheme == "https" else 80) else "")
        return (scheme in {"http", "https"} and request.url.hostname == host
            and (request.url.port or (443 if scheme == "https" else 80)) == port
            and request.headers.get("Origin") == expected and origin.netloc == urlsplit(expected).netloc
            and referer.scheme == origin.scheme and referer.netloc == origin.netloc
            and referer.path == PREFIX and not referer.username and not referer.password)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class _Run:
    mission_id: str
    vehicle_id: str
    ledger: InflightApprovalLedger
    snapshot: object


class InflightApprovalService:
    """Host-only registration connects one live run to its private ledger."""

    def __init__(self, *, clock=time.monotonic):
        self._runs, self._lock, self._clock = {}, RLock(), clock

    def register_run(self, *, run_id, mission_id, vehicle_id, ledger, snapshot):
        if not (all(type(v) is str and re.fullmatch(_ID, v) for v in (run_id, mission_id, vehicle_id))
                and isinstance(ledger, InflightApprovalLedger) and callable(snapshot)):
            raise ValueError("invalid_inflight_run_registration")
        with self._lock:
            if run_id in self._runs or any(item.ledger._path.resolve() == ledger._path.resolve() for item in self._runs.values()):
                raise ValueError("inflight_run_or_ledger_already_registered")
            self._runs[run_id] = _Run(mission_id, vehicle_id, ledger, snapshot)

    def unregister_run(self, run_id):
        with self._lock:
            self._runs.pop(run_id, None)

    def _run(self, run_id, mission_id, vehicle_id, token):
        with self._lock:
            item = self._runs.get(run_id)
        if item is None or (item.mission_id, item.vehicle_id) != (mission_id, vehicle_id):
            raise HTTPException(404, "inflight_run_unavailable")
        try:
            item.ledger.authenticate_operator(token)
        except InflightApprovalError:
            raise HTTPException(401, "operator_authentication_failed") from None
        return item

    def _view(self, item, request_id=None):
        try:
            state = item.snapshot()
        except (ValueError, KeyError, TypeError):
            # A newly spawned flight has no return-phase telemetry yet.
            # This is a transient not-ready state, not an unhandled HTTP 500.
            raise HTTPException(409, "live_worker_snapshot_unavailable") from None
        if (type(state) is not dict or set(state) != {"run_active", "simulation_time_s", "revision", "scope", "observation", "pending_request_id"}
                or type(state["run_active"]) is not bool or type(state["revision"]) is not int
                or type(state["simulation_time_s"]) not in (int, float) or not math.isfinite(state["simulation_time_s"])):
            raise HTTPException(409, "live_worker_snapshot_unavailable")
        request_id = request_id or state["pending_request_id"]
        if request_id is None:
            return {"request": None, "receipt": None, "consumption": None, "resolvable": False,
                    "reason": "no_pending_request", "run_active": state["run_active"]}, state
        if not (type(request_id) is str and re.fullmatch(_ID, request_id)):
            raise HTTPException(409, "invalid_worker_request_id")
        try:
            value = item.ledger.status(request_id)
            request = value["request"]
            if (request["mission_id"], request["vehicle_id"]) != (item.mission_id, item.vehicle_id):
                raise HTTPException(404, "inflight_request_unavailable")
            wall, simulation, obs = self._clock(), state["simulation_time_s"], state["observation"]
            fresh = (type(obs) is dict and set(obs) == {"state_sha256", "simulation_time_s", "wall_time_s"}
                and type(obs["state_sha256"]) is str and re.fullmatch(r"[a-f0-9]{64}", obs["state_sha256"])
                and all(type(obs[k]) in (int, float) and math.isfinite(obs[k]) for k in ("simulation_time_s", "wall_time_s"))
                and 0 <= simulation-obs["simulation_time_s"] <= request["maximum_observation_age_s"]
                and 0 <= wall-obs["wall_time_s"] <= request["maximum_observation_age_s"])
            if not state["run_active"]:
                reason = "run_inactive"
            elif state["pending_request_id"] != request_id:
                reason = "request_not_current"
            elif state["revision"] != request["current_revision"] or digest(state["scope"]) != request["current_scope_sha256"]:
                reason = "current_context_changed"
            elif not fresh:
                reason = "live_observation_not_fresh"
            elif simulation < request["issued_simulation_time_s"] or wall < request["issued_wall_time_s"]:
                reason = "clock_regressed"
            elif simulation >= request["simulation_deadline_s"] or wall >= request["wall_deadline_s"]:
                reason = "approval_deadline_expired"
            elif value["receipt"] is not None or value["consumption"] is not None:
                reason = "request_already_resolved"
            else:
                reason = "awaiting_operator"
            value.update(resolvable=reason == "awaiting_operator", reason=reason, run_active=state["run_active"],
                current_simulation_time_s=simulation, current_wall_time_s=wall,
                local_operator_identity_kind="configured_bearer_principal", sso_identity_verified=False,
                approval_binding="candidate_bound_scope_amendment" if "candidate_sha256" in request else "scope_only_amendment")
            return value, state
        except (InflightApprovalError, KeyError, TypeError, ValueError):
            raise HTTPException(409, "inflight_record_unavailable") from None

    def get(self, *, run_id, mission_id, vehicle_id, token, request_id=None):
        item = self._run(run_id, mission_id, vehicle_id, token)
        return self._view(item, request_id)[0]

    def resolve(self, *, run_id, mission_id, vehicle_id, token, request_id, choice, body):
        item = self._run(run_id, mission_id, vehicle_id, token)
        value, state = self._view(item, request_id)
        if not value["resolvable"]:
            raise HTTPException(409, value["reason"])
        expected_keys = {"request_sha256", "observed_state_sha256"}
        if "candidate_sha256" in value["request"]:
            expected_keys.add("candidate_sha256")
        if type(body) is not dict or set(body) != expected_keys:
            raise HTTPException(400, "displayed_request_hashes_required")
        try:
            receipt = item.ledger.resolve(request_id=request_id, request_sha256=body["request_sha256"],
                observed_state_sha256=body["observed_state_sha256"], choice=choice, bearer_token=token,
                simulation_time_s=state["simulation_time_s"], candidate_sha256=body.get("candidate_sha256"))
        except InflightApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"receipt": receipt, "executor_command_issued": False, "requires_worker_consumption": True}


_SERVICE = InflightApprovalService()


def get_inflight_approval_service():
    return _SERVICE


def build_inflight_approval_router(service=None):
    service = service or get_inflight_approval_service()
    router = APIRouter()
    base = PREFIX+"/runs/{run_id}/missions/{mission_id}/vehicles/{vehicle_id}"

    def token(request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "operator_bearer_required")
        return auth[7:]

    @router.get(PREFIX)
    async def operator_page():
        return FileResponse(Path(__file__).resolve().parents[1]/"runtime/assets/starship_inflight_operator.html",
            media_type="text/html", headers={**_HEADERS, "Content-Security-Policy": "default-src 'none'; "
            "script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"})

    @router.get(base+"/pending")
    @router.get(base+"/requests/{request_id}")
    async def get_request(request: Request, run_id: str, mission_id: str, vehicle_id: str, request_id: str | None = None):
        value = await run_in_threadpool(service.get, run_id=run_id, mission_id=mission_id, vehicle_id=vehicle_id,
            token=token(request), request_id=request_id)
        return JSONResponse(value, headers=_HEADERS)

    @router.post(base+"/requests/{request_id}/{choice}")
    async def resolve_request(request: Request, run_id: str, mission_id: str, vehicle_id: str, request_id: str, choice: str):
        operator_token = token(request)
        if choice not in ("approve", "reject"):
            raise HTTPException(404, "inflight_action_unavailable")
        if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        raw = await request.body()
        if len(raw) > 2048:
            raise HTTPException(413, "operator_response_too_large")
        try:
            pairs = json.loads(raw, object_pairs_hook=lambda entries: entries)
            if not isinstance(pairs, list) or any(not isinstance(p, tuple) or len(p) != 2 for p in pairs):
                raise ValueError
            body = dict(pairs)
            if len(body) != len(pairs):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid_operator_response") from None
        value = await run_in_threadpool(service.resolve, run_id=run_id, mission_id=mission_id, vehicle_id=vehicle_id,
            token=operator_token, request_id=request_id, choice=choice, body=body)
        return JSONResponse(value, headers=_HEADERS)

    return router
