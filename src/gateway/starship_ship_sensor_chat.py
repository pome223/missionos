"""Opt-in authenticated HTTP boundary for the synthetic sensor study."""
from hmac import compare_digest
import re
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.intelligence.space_ops_investigator import _json
from src.intelligence.starship_ship_sensor_agent import validate_envelope

PATH = "/api/starship/ship-sensor/decision"


def build_router(agent, token, authority_sha256, *, allowed_run_ids=None):
    if (not callable(getattr(agent, "assess", None)) or type(token) is not str
            or not 32 <= len(token) <= 256 or any(c.isspace() for c in token)
            or type(authority_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", authority_sha256) is None):
        raise ValueError("invalid_sensor_router_configuration")
    if (allowed_run_ids is not None and (type(allowed_run_ids) not in (list, tuple, set, frozenset)
            or not allowed_run_ids or any(type(run) is not str or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run) is None for run in allowed_run_ids))):
        raise ValueError("invalid_sensor_router_runs")
    allowed_runs = frozenset(allowed_run_ids) if allowed_run_ids is not None else None
    router, seen, lock = APIRouter(), set(), Lock()

    @router.post(PATH)
    async def decide(request: Request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not compare_digest(header[7:].encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(401, "sensor_authentication_failed")
        if request.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json":
            raise HTTPException(415, "sensor_json_required")
        body = await request.body()
        if len(body) > 128*1024:
            raise HTTPException(413, "sensor_request_too_large")
        try:
            envelope = validate_envelope(_json(body))
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            raise HTTPException(400, "invalid_sensor_envelope") from None
        if not compare_digest(envelope["authorization_sha256"], authority_sha256):
            raise HTTPException(403, "sensor_authorization_mismatch")
        if allowed_runs is not None and envelope["run_id"] not in allowed_runs:
            raise HTTPException(403, "sensor_run_outside_authority")
        identity = (envelope["run_id"], envelope["request_sha256"], envelope["provider"])
        with lock:
            if identity in seen:
                raise HTTPException(409, "sensor_request_already_consumed")
            seen.add(identity)
        try:
            return await run_in_threadpool(agent.assess, envelope)
        except (ValueError, TypeError, KeyError, OSError):
            raise HTTPException(400, "sensor_assessment_failed_no_retry") from None

    return router
