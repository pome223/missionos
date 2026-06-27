"""Gateway live-runtime process-boundary artifacts.

This module records real Gateway HTTP boundaries used by the C5b strict probe.
Route invocation is intentionally only a pre-runtime boundary. A supervisor
process probe is the first boundary kind that can support live observation and
recovery process materialization, but it still does not by itself claim full
Gateway autonomous runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
from pathlib import Path
import secrets
from typing import Any


GATEWAY_ROUTE_INVOCATION_BOUNDARY_SCHEMA = "gateway_route_invocation_boundary.v1"
GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH = "/gateway/live-runtime/process-boundary"
GATEWAY_PROCESS_BOUNDARY_KIND_ROUTE_INVOCATION = "gateway_route_invocation"
GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_SCHEMA = (
    "gateway_supervisor_process_probe_boundary.v1"
)
GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH = (
    "/gateway/live-runtime/supervisor-process-probe"
)
GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE = (
    "gateway_supervisor_process_probe"
)
GATEWAY_OBSERVATION_PROCESS_PROBE_KIND = "gateway_live_observation_process_probe"
GATEWAY_RECOVERY_DECISION_PROCESS_PROBE_KIND = (
    "gateway_live_recovery_decision_process_probe"
)

AUTHORITY_KEYS_FALSE = (
    "causal_verification_transferred",
    "physical_execution_invoked",
    "hardware_target_allowed",
    "physical_form1_claimed",
    "dispatch_authority_created",
    "delivery_completion_claimed",
    "full_gateway_runtime_loop",
    "gateway_autonomous_runtime_claimed",
)
_GATEWAY_PROCESS_BOUNDARY_SESSION_SECRET = secrets.token_hex(32)


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _require_nonempty_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not _nonempty_string(value):
        raise ValueError(f"{key} is required")
    return str(value)


def _verify_source_artifact_hash(path_value: str, expected_sha256: str) -> bool:
    path = Path(path_value)
    try:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("source_runtime_artifact_path must be readable") from exc
    if actual_sha256 != expected_sha256:
        raise ValueError("source_runtime_artifact_sha256 does not match source file")
    return True


def _authority_boundary_false(payload: dict[str, Any]) -> bool:
    return all(payload.get(key) is False for key in AUTHORITY_KEYS_FALSE)


_PROCESS_PROBE_EVIDENCE_SIGNATURE_FIELD_NAMES = (
    "schema_version",
    "process_kind",
    "process_ref",
    "process_evidence_ref",
    "process_started",
    "process_completed",
    "gateway_process_probe_task_observed",
    "gateway_process_probe_task_name",
    "source_runtime_artifact_read_observed",
    "source_runtime_artifact_sha256_verified",
    "source_runtime_supervisor_chain_observed",
    "source_bound",
    "gateway_mission_session_ref",
    "supervisor_session_ref",
    "gateway_supervisor_lifecycle_ref",
    "source_runtime_artifact_ref",
    "source_runtime_artifact_path",
    "source_runtime_artifact_sha256",
    "causal_verification_transferred",
    "physical_form1_required",
    "physical_form1_claimed",
    "physical_execution_invoked",
    "hardware_target_allowed",
    "dispatch_authority_created",
    "delivery_completion_claimed",
    "full_gateway_runtime_loop",
    "gateway_autonomous_runtime_claimed",
)


def _process_probe_evidence_signature_payload(evidence: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: evidence.get(key)
            for key in _PROCESS_PROBE_EVIDENCE_SIGNATURE_FIELD_NAMES
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _route_process_probe_signing_context_observed() -> bool:
    """Return whether signing is happening inside the Gateway route probe task."""

    for frame in inspect.stack()[1:8]:
        if (
            Path(frame.filename).as_posix().endswith("/src/gateway/server.py")
            and frame.function == "_run_gateway_live_runtime_process_probe"
        ):
            return True
    return False


def _sign_gateway_live_process_probe_evidence(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Attach a process-local route-handler signature to probe evidence."""

    if not _route_process_probe_signing_context_observed():
        raise ValueError(
            "gateway live process probe evidence can only be signed by the "
            "Gateway route probe task"
        )
    signed = dict(evidence)
    signed["gateway_route_handler_process_probe_signature"] = hmac.new(
        _GATEWAY_PROCESS_BOUNDARY_SESSION_SECRET.encode("utf-8"),
        _process_probe_evidence_signature_payload(signed).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signed


def _process_probe_evidence_signature_supported(evidence: dict[str, Any]) -> bool:
    signature = evidence.get("gateway_route_handler_process_probe_signature")
    if not _nonempty_string(signature):
        return False
    expected_signature = hmac.new(
        _GATEWAY_PROCESS_BOUNDARY_SESSION_SECRET.encode("utf-8"),
        _process_probe_evidence_signature_payload(evidence).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(str(signature), expected_signature)


def _process_probe_evidence_supported(
    evidence: dict[str, Any],
    *,
    process_kind: str,
    process_ref_prefix: str,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    gateway_supervisor_lifecycle_ref: str,
    source_runtime_artifact_ref: str,
    source_runtime_artifact_path: str,
    source_runtime_artifact_sha256: str,
) -> bool:
    return (
        isinstance(evidence, dict)
        and evidence.get("schema_version") == "gateway_live_process_probe_evidence.v1"
        and evidence.get("process_kind") == process_kind
        and _nonempty_string(evidence.get("process_ref"))
        and str(evidence.get("process_ref")).startswith(process_ref_prefix)
        and _nonempty_string(evidence.get("process_evidence_ref"))
        and str(evidence.get("process_evidence_ref")).startswith(
            "gateway_live_process_probe_evidence:"
        )
        and evidence.get("process_started") is True
        and evidence.get("process_completed") is True
        and evidence.get("gateway_process_probe_task_observed") is True
        and evidence.get("source_runtime_artifact_read_observed") is True
        and evidence.get("source_runtime_artifact_sha256_verified") is True
        and evidence.get("source_runtime_supervisor_chain_observed") is True
        and evidence.get("source_bound") is True
        and evidence.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and evidence.get("supervisor_session_ref") == supervisor_session_ref
        and evidence.get("gateway_supervisor_lifecycle_ref")
        == gateway_supervisor_lifecycle_ref
        and evidence.get("source_runtime_artifact_ref") == source_runtime_artifact_ref
        and evidence.get("source_runtime_artifact_path") == source_runtime_artifact_path
        and evidence.get("source_runtime_artifact_sha256")
        == source_runtime_artifact_sha256
        and _authority_boundary_false(evidence)
        and evidence.get("physical_form1_required") is True
        and _process_probe_evidence_signature_supported(evidence)
    )


def _gateway_route_handler_signature_payload(
    *,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    gateway_supervisor_lifecycle_ref: str,
    source_runtime_artifact_ref: str,
    source_runtime_artifact_path: str,
    source_runtime_artifact_sha256: str,
    gateway_process_boundary_ref: str,
    gateway_supervisor_process_probe_ref: str,
    gateway_observation_process_probe_ref: Any,
    gateway_recovery_decision_process_probe_ref: Any,
) -> str:
    return json.dumps(
        {
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "gateway_supervisor_lifecycle_ref": gateway_supervisor_lifecycle_ref,
            "source_runtime_artifact_ref": source_runtime_artifact_ref,
            "source_runtime_artifact_path": source_runtime_artifact_path,
            "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
            "gateway_process_boundary_ref": gateway_process_boundary_ref,
            "gateway_supervisor_process_probe_ref": gateway_supervisor_process_probe_ref,
            "gateway_observation_process_probe_ref": gateway_observation_process_probe_ref,
            "gateway_recovery_decision_process_probe_ref": (
                gateway_recovery_decision_process_probe_ref
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sign_gateway_route_handler_boundary(**payload: Any) -> str:
    return hmac.new(
        _GATEWAY_PROCESS_BOUNDARY_SESSION_SECRET.encode("utf-8"),
        _gateway_route_handler_signature_payload(**payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _gateway_route_handler_signature_supported(artifact: dict[str, Any]) -> bool:
    signature = artifact.get("gateway_route_handler_signature")
    if not _nonempty_string(signature):
        return False
    expected_signature = _sign_gateway_route_handler_boundary(
        gateway_mission_session_ref=str(artifact.get("gateway_mission_session_ref") or ""),
        supervisor_session_ref=str(artifact.get("supervisor_session_ref") or ""),
        gateway_supervisor_lifecycle_ref=str(
            artifact.get("gateway_supervisor_lifecycle_ref") or ""
        ),
        source_runtime_artifact_ref=str(artifact.get("source_runtime_artifact_ref") or ""),
        source_runtime_artifact_path=str(artifact.get("source_runtime_artifact_path") or ""),
        source_runtime_artifact_sha256=str(
            artifact.get("source_runtime_artifact_sha256") or ""
        ),
        gateway_process_boundary_ref=str(
            artifact.get("gateway_process_boundary_ref") or ""
        ),
        gateway_supervisor_process_probe_ref=str(
            artifact.get("gateway_supervisor_process_probe_ref") or ""
        ),
        gateway_observation_process_probe_ref=str(
            artifact.get("gateway_observation_process_probe_ref") or ""
        ),
        gateway_recovery_decision_process_probe_ref=str(
            artifact.get("gateway_recovery_decision_process_probe_ref") or ""
        ),
    )
    return hmac.compare_digest(str(signature), expected_signature)


def build_gateway_route_invocation_boundary(
    payload: dict[str, Any],
    *,
    route_path: str = GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH,
    http_method: str = "POST",
    http_status_code: int = 200,
    client_host: str = "loopback_not_recorded",
) -> dict[str, Any]:
    """Build a source-bound route-invocation boundary artifact.

    The caller supplies the Gateway/session/source-runtime references that the
    later C5b materializer must match. The route invocation itself only proves a
    Gateway HTTP boundary was crossed; it does not prove live Gateway ownership
    of the supervisor runtime.
    """

    gateway_mission_session_ref = _require_nonempty_string(
        payload, "gateway_mission_session_ref"
    )
    supervisor_session_ref = _require_nonempty_string(payload, "supervisor_session_ref")
    source_runtime_artifact_ref = _require_nonempty_string(
        payload, "source_runtime_artifact_ref"
    )
    source_runtime_artifact_path = _require_nonempty_string(
        payload, "source_runtime_artifact_path"
    )
    source_runtime_artifact_sha256 = _require_nonempty_string(
        payload, "source_runtime_artifact_sha256"
    )
    source_runtime_artifact_verified = _verify_source_artifact_hash(
        source_runtime_artifact_path,
        source_runtime_artifact_sha256,
    )

    boundary_seed = {
        "schema_version": GATEWAY_ROUTE_INVOCATION_BOUNDARY_SCHEMA,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "source_runtime_artifact_path": source_runtime_artifact_path,
        "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
        "route_path": route_path,
        "http_method": http_method.upper(),
        "client_host": client_host,
    }
    boundary_id = _stable_id("gateway_route_invocation_boundary", boundary_seed)
    boundary_ref = f"gateway_process_boundary:{boundary_id}"
    route_invocation_ref = f"gateway_route_invocation:{boundary_id}"

    artifact = {
        "schema_version": GATEWAY_ROUTE_INVOCATION_BOUNDARY_SCHEMA,
        "gateway_route_invocation_boundary_id": boundary_id,
        "gateway_route_invocation_boundary_ref": (
            f"gateway_route_invocation_boundary:{boundary_id}"
        ),
        "gateway_process_boundary_observed": True,
        "gateway_process_boundary_kind": GATEWAY_PROCESS_BOUNDARY_KIND_ROUTE_INVOCATION,
        "gateway_process_boundary_ref": boundary_ref,
        "gateway_route_invocation_observed": True,
        "gateway_route_invocation_ref": route_invocation_ref,
        "gateway_route_path": route_path,
        "gateway_route_method": http_method.upper(),
        "gateway_route_http_status": http_status_code,
        "gateway_route_invocation_client_host": client_host,
        "gateway_route_invocation_loopback_only": True,
        "gateway_route_invocation_source_bound": True,
        "source_bound": True,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "source_runtime_artifact_ref": source_runtime_artifact_ref,
        "source_runtime_artifact_path": source_runtime_artifact_path,
        "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
        "source_runtime_artifact_verified": source_runtime_artifact_verified,
        "materializer_compatible": True,
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "causal_verification_transferred": False,
        "physical_form1_required": True,
        "physical_form1_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "full_gateway_runtime_loop": False,
        "gateway_autonomous_runtime_claimed": False,
        "scope_boundary_notes": [
            "gateway_route_invocation_boundary_only",
            "does_not_claim_live_gateway_runtime",
            "does_not_transfer_sitl_causal_verification_to_physical",
            "does_not_create_dispatch_hardware_or_delivery_authority",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    return artifact


def _build_gateway_supervisor_process_probe_boundary(
    payload: dict[str, Any],
    *,
    route_path: str = GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
    http_method: str = "POST",
    http_status_code: int = 200,
    client_host: str = "loopback_not_recorded",
    process_probe_evidence: dict[str, Any] | None = None,
    gateway_route_handler_observed: bool = False,
) -> dict[str, Any]:
    """Build a source-bound Gateway supervisor process-probe boundary artifact.

    This boundary proves a loopback Gateway route was invoked specifically to
    start the Gateway-owned observation and recovery decision process probes for
    an already source-bound supervisor runtime artifact. It is still Form 0b:
    the C5b probe must separately validate the materialized sidecar evidence and
    same-session decision/action/outcome chain before claiming full runtime.
    """

    gateway_mission_session_ref = _require_nonempty_string(
        payload, "gateway_mission_session_ref"
    )
    supervisor_session_ref = _require_nonempty_string(payload, "supervisor_session_ref")
    gateway_supervisor_lifecycle_ref = _require_nonempty_string(
        payload, "gateway_supervisor_lifecycle_ref"
    )
    source_runtime_artifact_ref = _require_nonempty_string(
        payload, "source_runtime_artifact_ref"
    )
    source_runtime_artifact_path = _require_nonempty_string(
        payload, "source_runtime_artifact_path"
    )
    source_runtime_artifact_sha256 = _require_nonempty_string(
        payload, "source_runtime_artifact_sha256"
    )
    source_runtime_artifact_verified = _verify_source_artifact_hash(
        source_runtime_artifact_path,
        source_runtime_artifact_sha256,
    )
    boundary_seed = {
        "schema_version": GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_SCHEMA,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "gateway_supervisor_lifecycle_ref": gateway_supervisor_lifecycle_ref,
        "source_runtime_artifact_path": source_runtime_artifact_path,
        "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
        "route_path": route_path,
        "http_method": http_method.upper(),
        "client_host": client_host,
    }
    boundary_id = _stable_id("gateway_supervisor_process_probe_boundary", boundary_seed)
    boundary_ref = f"gateway_process_boundary:{boundary_id}"
    supervisor_process_probe_ref = f"gateway_supervisor_process_probe:{boundary_id}"
    process_probe_evidence = (
        process_probe_evidence if isinstance(process_probe_evidence, dict) else {}
    )
    observation_evidence = process_probe_evidence.get("observation")
    observation_evidence = (
        observation_evidence if isinstance(observation_evidence, dict) else {}
    )
    recovery_evidence = process_probe_evidence.get("recovery_decision")
    recovery_evidence = recovery_evidence if isinstance(recovery_evidence, dict) else {}
    observation_probe_observed = _process_probe_evidence_supported(
        observation_evidence,
        process_kind=GATEWAY_OBSERVATION_PROCESS_PROBE_KIND,
        process_ref_prefix="gateway_live_observation_process_probe:",
        gateway_mission_session_ref=gateway_mission_session_ref,
        supervisor_session_ref=supervisor_session_ref,
        gateway_supervisor_lifecycle_ref=gateway_supervisor_lifecycle_ref,
        source_runtime_artifact_ref=source_runtime_artifact_ref,
        source_runtime_artifact_path=source_runtime_artifact_path,
        source_runtime_artifact_sha256=source_runtime_artifact_sha256,
    )
    recovery_probe_observed = _process_probe_evidence_supported(
        recovery_evidence,
        process_kind=GATEWAY_RECOVERY_DECISION_PROCESS_PROBE_KIND,
        process_ref_prefix="gateway_live_recovery_decision_process_probe:",
        gateway_mission_session_ref=gateway_mission_session_ref,
        supervisor_session_ref=supervisor_session_ref,
        gateway_supervisor_lifecycle_ref=gateway_supervisor_lifecycle_ref,
        source_runtime_artifact_ref=source_runtime_artifact_ref,
        source_runtime_artifact_path=source_runtime_artifact_path,
        source_runtime_artifact_sha256=source_runtime_artifact_sha256,
    )
    loopback_route_observed = (
        gateway_route_handler_observed is True
        and http_method.upper() == "POST"
        and http_status_code == 200
        and client_host in {"127.0.0.1", "::1", "localhost"}
    )
    process_probe_evidence_observed = (
        loopback_route_observed and observation_probe_observed and recovery_probe_observed
    )
    route_handler_signature = (
        _sign_gateway_route_handler_boundary(
            gateway_mission_session_ref=gateway_mission_session_ref,
            supervisor_session_ref=supervisor_session_ref,
            gateway_supervisor_lifecycle_ref=gateway_supervisor_lifecycle_ref,
            source_runtime_artifact_ref=source_runtime_artifact_ref,
            source_runtime_artifact_path=source_runtime_artifact_path,
            source_runtime_artifact_sha256=source_runtime_artifact_sha256,
            gateway_process_boundary_ref=boundary_ref,
            gateway_supervisor_process_probe_ref=supervisor_process_probe_ref,
            gateway_observation_process_probe_ref=observation_evidence.get(
                "process_ref"
            ),
            gateway_recovery_decision_process_probe_ref=recovery_evidence.get(
                "process_ref"
            ),
        )
        if process_probe_evidence_observed
        else None
    )

    return {
        "schema_version": GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_SCHEMA,
        "gateway_supervisor_process_probe_boundary_id": boundary_id,
        "gateway_supervisor_process_probe_boundary_ref": (
            f"gateway_supervisor_process_probe_boundary:{boundary_id}"
        ),
        "gateway_process_boundary_observed": True,
        "gateway_process_boundary_kind": (
            GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE
        ),
        "gateway_process_boundary_ref": boundary_ref,
        "gateway_supervisor_process_probe_observed": True,
        "gateway_supervisor_process_probe_ref": supervisor_process_probe_ref,
        "gateway_process_probe_evidence_observed": process_probe_evidence_observed,
        "gateway_observation_process_probe_ref": observation_evidence.get(
            "process_ref"
        ),
        "gateway_observation_process_probe_evidence_ref": observation_evidence.get(
            "process_evidence_ref"
        ),
        "gateway_observation_process_probe_observed": observation_probe_observed,
        "gateway_recovery_decision_process_probe_ref": recovery_evidence.get(
            "process_ref"
        ),
        "gateway_recovery_decision_process_probe_evidence_ref": recovery_evidence.get(
            "process_evidence_ref"
        ),
        "gateway_recovery_decision_process_probe_observed": recovery_probe_observed,
        "gateway_observation_process_start_observed": process_probe_evidence_observed,
        "gateway_recovery_decision_process_start_observed": (
            process_probe_evidence_observed
        ),
        "gateway_route_path": route_path,
        "gateway_route_method": http_method.upper(),
        "gateway_route_http_status": http_status_code,
        "gateway_route_invocation_client_host": client_host,
        "gateway_route_invocation_loopback_only": True,
        "gateway_route_handler_observed": loopback_route_observed,
        "gateway_route_handler_signature": route_handler_signature,
        "gateway_supervisor_process_probe_source_bound": True,
        "source_bound": True,
        "gateway_mission_session_ref": gateway_mission_session_ref,
        "supervisor_session_ref": supervisor_session_ref,
        "gateway_supervisor_lifecycle_ref": gateway_supervisor_lifecycle_ref,
        "source_runtime_artifact_ref": source_runtime_artifact_ref,
        "source_runtime_artifact_path": source_runtime_artifact_path,
        "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
        "source_runtime_artifact_verified": source_runtime_artifact_verified,
        "materializer_compatible": process_probe_evidence_observed,
        "causal_form": "Form 0b",
        "progress_counted": False,
        "gateway_capability_progress_counted": False,
        "causal_verification_transferred": False,
        "physical_form1_required": True,
        "physical_form1_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "full_gateway_runtime_loop": False,
        "gateway_autonomous_runtime_claimed": False,
        "scope_boundary_notes": [
            "gateway_supervisor_process_probe_boundary_only",
            (
                "gateway_live_process_probe_evidence_observed"
                if process_probe_evidence_observed
                else "gateway_live_process_probe_evidence_missing"
            ),
            (
                "gateway_route_handler_observed"
                if loopback_route_observed
                else "gateway_route_handler_not_observed"
            ),
            "does_not_claim_full_gateway_runtime_without_c5b_probe",
            "does_not_transfer_sitl_causal_verification_to_physical",
            "does_not_create_dispatch_hardware_or_delivery_authority",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def build_gateway_supervisor_process_probe_boundary(
    payload: dict[str, Any],
    *,
    route_path: str = GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
    http_method: str = "POST",
    http_status_code: int = 200,
    client_host: str = "loopback_not_recorded",
) -> dict[str, Any]:
    """Build a scaffold-only supervisor-process boundary artifact.

    This public builder intentionally cannot make a materializer-compatible live
    boundary. The Gateway HTTP route must produce route-handler evidence.
    """

    return _build_gateway_supervisor_process_probe_boundary(
        payload,
        route_path=route_path,
        http_method=http_method,
        http_status_code=http_status_code,
        client_host=client_host,
    )


def build_gateway_supervisor_process_probe_boundary_from_route(
    payload: dict[str, Any],
    *,
    process_probe_evidence: dict[str, Any],
    route_path: str = GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
    http_method: str = "POST",
    http_status_code: int = 200,
    client_host: str,
) -> dict[str, Any]:
    """Build a route-observed supervisor-process boundary artifact."""

    return _build_gateway_supervisor_process_probe_boundary(
        payload,
        route_path=route_path,
        http_method=http_method,
        http_status_code=http_status_code,
        client_host=client_host,
        process_probe_evidence=process_probe_evidence,
        gateway_route_handler_observed=True,
    )


def gateway_route_invocation_boundary_supported(
    artifact: dict[str, Any],
    *,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    source_runtime_artifact_path: str,
    source_runtime_artifact_sha256: str,
) -> bool:
    """Return whether a route-boundary artifact can support C5b materialization."""

    return (
        artifact.get("schema_version") == GATEWAY_ROUTE_INVOCATION_BOUNDARY_SCHEMA
        and artifact.get("gateway_process_boundary_observed") is True
        and artifact.get("gateway_process_boundary_kind")
        == GATEWAY_PROCESS_BOUNDARY_KIND_ROUTE_INVOCATION
        and _nonempty_string(artifact.get("gateway_process_boundary_ref"))
        and str(artifact.get("gateway_process_boundary_ref")).startswith(
            "gateway_process_boundary:"
        )
        and _nonempty_string(artifact.get("gateway_route_invocation_ref"))
        and str(artifact.get("gateway_route_invocation_ref")).startswith(
            "gateway_route_invocation:"
        )
        and artifact.get("gateway_route_invocation_observed") is True
        and artifact.get("gateway_route_path") == GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH
        and artifact.get("gateway_route_method") == "POST"
        and artifact.get("gateway_route_http_status") == 200
        and artifact.get("gateway_route_invocation_loopback_only") is True
        and artifact.get("gateway_route_invocation_source_bound") is True
        and artifact.get("source_bound") is True
        and artifact.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and artifact.get("supervisor_session_ref") == supervisor_session_ref
        and artifact.get("source_runtime_artifact_path") == source_runtime_artifact_path
        and artifact.get("source_runtime_artifact_sha256")
        == source_runtime_artifact_sha256
        and artifact.get("source_runtime_artifact_verified") is True
        and artifact.get("materializer_compatible") is True
        and all(artifact.get(key) is False for key in AUTHORITY_KEYS_FALSE)
        and artifact.get("physical_form1_required") is True
    )


def gateway_supervisor_process_probe_boundary_supported(
    artifact: dict[str, Any],
    *,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
    gateway_supervisor_lifecycle_ref: str,
    source_runtime_artifact_ref: str,
    source_runtime_artifact_path: str,
    source_runtime_artifact_sha256: str,
) -> bool:
    """Return whether a supervisor process-probe boundary can support materialization."""

    return (
        artifact.get("schema_version")
        == GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_SCHEMA
        and artifact.get("gateway_process_boundary_observed") is True
        and artifact.get("gateway_process_boundary_kind")
        == GATEWAY_PROCESS_BOUNDARY_KIND_SUPERVISOR_PROCESS_PROBE
        and _nonempty_string(artifact.get("gateway_process_boundary_ref"))
        and str(artifact.get("gateway_process_boundary_ref")).startswith(
            "gateway_process_boundary:"
        )
        and _nonempty_string(artifact.get("gateway_supervisor_process_probe_ref"))
        and str(artifact.get("gateway_supervisor_process_probe_ref")).startswith(
            "gateway_supervisor_process_probe:"
        )
        and artifact.get("gateway_supervisor_process_probe_observed") is True
        and artifact.get("gateway_process_probe_evidence_observed") is True
        and _nonempty_string(artifact.get("gateway_observation_process_probe_ref"))
        and str(artifact.get("gateway_observation_process_probe_ref")).startswith(
            "gateway_live_observation_process_probe:"
        )
        and _nonempty_string(
            artifact.get("gateway_observation_process_probe_evidence_ref")
        )
        and str(
            artifact.get("gateway_observation_process_probe_evidence_ref")
        ).startswith("gateway_live_process_probe_evidence:")
        and artifact.get("gateway_observation_process_probe_observed") is True
        and _nonempty_string(
            artifact.get("gateway_recovery_decision_process_probe_ref")
        )
        and str(
            artifact.get("gateway_recovery_decision_process_probe_ref")
        ).startswith("gateway_live_recovery_decision_process_probe:")
        and _nonempty_string(
            artifact.get("gateway_recovery_decision_process_probe_evidence_ref")
        )
        and str(
            artifact.get("gateway_recovery_decision_process_probe_evidence_ref")
        ).startswith("gateway_live_process_probe_evidence:")
        and artifact.get("gateway_recovery_decision_process_probe_observed") is True
        and artifact.get("gateway_observation_process_start_observed") is True
        and artifact.get("gateway_recovery_decision_process_start_observed") is True
        and artifact.get("gateway_route_path")
        == GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH
        and artifact.get("gateway_route_method") == "POST"
        and artifact.get("gateway_route_http_status") == 200
        and artifact.get("gateway_route_invocation_loopback_only") is True
        and artifact.get("gateway_route_invocation_client_host")
        in {"127.0.0.1", "::1", "localhost"}
        and artifact.get("gateway_route_handler_observed") is True
        and _gateway_route_handler_signature_supported(artifact)
        and artifact.get("gateway_supervisor_process_probe_source_bound") is True
        and artifact.get("source_bound") is True
        and artifact.get("gateway_mission_session_ref") == gateway_mission_session_ref
        and artifact.get("supervisor_session_ref") == supervisor_session_ref
        and artifact.get("gateway_supervisor_lifecycle_ref")
        == gateway_supervisor_lifecycle_ref
        and _nonempty_string(artifact.get("gateway_supervisor_lifecycle_ref"))
        and str(artifact.get("gateway_supervisor_lifecycle_ref")).startswith(
            "gateway_supervisor_lifecycle:"
        )
        and artifact.get("source_runtime_artifact_ref") == source_runtime_artifact_ref
        and _nonempty_string(artifact.get("source_runtime_artifact_ref"))
        and artifact.get("source_runtime_artifact_path") == source_runtime_artifact_path
        and artifact.get("source_runtime_artifact_sha256")
        == source_runtime_artifact_sha256
        and artifact.get("source_runtime_artifact_verified") is True
        and artifact.get("materializer_compatible") is True
        and _authority_boundary_false(artifact)
        and artifact.get("physical_form1_required") is True
    )
