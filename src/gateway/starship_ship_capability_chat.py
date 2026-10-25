"""Bearer-authenticated host HTTP boundary for one registered capability decision."""
from hmac import compare_digest
import re
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.intelligence.starship_ship_capability_agent import validate_envelope

PATH = "/api/starship/ship-capability/decision"


def build_router(agent, token, authority_sha256):
    if (not callable(getattr(agent, "assess", None)) or type(token) is not str
            or not 32 <= len(token) <= 256 or any(c.isspace() for c in token)
            or type(authority_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", authority_sha256) is None):
        raise ValueError("invalid_capability_router_configuration")
    router, seen, lock = APIRouter(), set(), Lock()

    @router.post(PATH)
    async def decide(request: Request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not compare_digest(header[7:].encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(401, "capability_authentication_failed")
        if request.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json":
            raise HTTPException(415, "capability_json_required")
        body = await request.body()
        if len(body) > 128*1024:
            raise HTTPException(413, "capability_request_too_large")
        try:
            from src.intelligence.space_ops_investigator import _json
            envelope = validate_envelope(_json(body))
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            raise HTTPException(400, "invalid_capability_envelope") from None
        if not compare_digest(envelope["authorization_sha256"], authority_sha256):
            raise HTTPException(403, "capability_authorization_mismatch")
        identity = (envelope["run_id"], envelope["request_sha256"], envelope["provider"])
        with lock:
            if identity in seen:
                raise HTTPException(409, "capability_request_already_consumed")
            seen.add(identity)  # No replay after an attempted provider request or failure.
        try:
            result = await run_in_threadpool(agent.assess, envelope)
        except (ValueError, TypeError, KeyError, OSError):
            raise HTTPException(400, "capability_assessment_failed_no_retry") from None
        return result

    return router
