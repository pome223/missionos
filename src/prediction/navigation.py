"""Opt-in navigation forecast transport; models never acquire actuator authority.

A compatible navigation model is supplied by the operator. Stacking models and
heuristic substitutes are deliberately not registered by this adapter.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from missionos_core.prediction import (
    PREDICTION_SCHEMA,
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
    prediction_digest,
)
from src.intelligence.prediction_evidence import (
    AUTHORITY_FLAGS,
    capture_prediction_evidence,
    receive_prediction_evidence,
    snapshot,
)

MODE_ENV = "MISSIONOS_NAVIGATION_WAM_MODE"
CONFIG_ENV = "MISSIONOS_NAVIGATION_WAM_CONFIG"
CONFIG_SCHEMA = "missionos_navigation_wam_config.v1"
INPUT_SCHEMA = "missionos_navigation_prediction_input.v1"
BACKEND_CONTRACTS = {
    "px4": ("missionos.navigation.px4.v1", "px4_gazebo_sitl.v1"),
    "nav2": ("missionos.navigation.nav2.v1", "ros2_nav2_turtlebot3_sim.v1"),
}
MAX_RESPONSE_BYTES = 1_048_576


def _failure_code(exc):
    message = str(exc)
    if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_]{0,150}", message):
        return message
    return type(exc).__name__


def _config_digest(config):
    return prediction_digest({**config, "binding": asdict(config["binding"])})


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _number(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name)
    return float(value)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _endpoint(value):
    if not isinstance(value, str) or any(ch.isspace() for ch in value):
        raise ValueError("invalid_navigation_wam_endpoint")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid_navigation_wam_endpoint")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid_navigation_wam_endpoint") from exc
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("navigation_wam_endpoint_requires_https_or_loopback")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("navigation_wam_redirect_rejected")


def load_navigation_config(backend, policy_sha256):
    """Read only an explicit local manifest, and compare its policy to the caller."""
    if backend not in BACKEND_CONTRACTS:
        raise ValueError("unsupported_navigation_backend")
    if not _sha(policy_sha256):
        raise ValueError("navigation_policy_digest_required")
    path = os.environ.get(CONFIG_ENV, "").strip()
    if not path:
        raise ValueError("navigation_wam_config_required")
    with Path(path).open("rb") as handle:
        raw = handle.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("navigation_wam_config_too_large")
    manifest = json.loads(raw)
    if (
        set(manifest) != {"schema_version", "backends"}
        or manifest["schema_version"] != CONFIG_SCHEMA
    ):
        raise ValueError("invalid_navigation_wam_config")
    backends = manifest["backends"]
    if not isinstance(backends, dict) or not set(backends) <= set(BACKEND_CONTRACTS):
        raise ValueError("invalid_navigation_wam_backends")
    config = dict(backends[backend])
    expected = {
        "endpoint",
        "binding",
        "horizon_seconds",
        "max_age_seconds",
        "timeout_seconds",
        "provider_kind",
    }
    if set(config) != expected:
        raise ValueError("invalid_navigation_wam_backend_config")
    binding = PredictionBinding(**config["binding"])
    mission, environment = BACKEND_CONTRACTS[backend]
    if (
        not isinstance(binding.model_id, str)
        or not binding.model_id.strip()
        or not _sha(binding.model_sha256)
        or binding.policy_sha256 != policy_sha256
        or binding.mission_contract != mission
        or binding.environment_contract != environment
        or binding.input_schema != INPUT_SCHEMA
    ):
        raise ValueError("navigation_wam_binding_mismatch")
    if config["provider_kind"] not in {"learned_model", "fixture"}:
        raise ValueError("invalid_navigation_wam_provider_kind")
    config["endpoint"] = _endpoint(config["endpoint"])
    config["horizon_seconds"] = _number(
        config["horizon_seconds"], 0.01, 300, "invalid_navigation_horizon"
    )
    config["max_age_seconds"] = _number(
        config["max_age_seconds"], 0.01, 30, "invalid_navigation_freshness"
    )
    config["timeout_seconds"] = _number(
        config["timeout_seconds"], 0.01, 30, "invalid_navigation_timeout"
    )
    config["binding"] = binding
    return config


class NavigationHTTPPredictor:
    """Digest-pinned protocol client; hashes identify responses, not their accuracy."""

    def __init__(self, config):
        self.config = config
        self.binding = config["binding"]
        self.invocation_evidence = None
        self.rejection_reason = None

    def predict(self, request):
        data = json.dumps(asdict(request), sort_keys=True, allow_nan=False).encode()
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("navigation_wam_request_too_large")
        start = time.monotonic()
        evidence = {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "prediction_http",
            "provider": "navigation_wam_http",
            "provider_kind": self.config["provider_kind"],
            "fixture_invocation": self.config["provider_kind"] == "fixture",
            "model_id": self.binding.model_id,
            "model_sha256": self.binding.model_sha256,
            "request_sha256": request.digest(),
            "http_request_body_sha256": hashlib.sha256(data).hexdigest(),
            "started_at": _utc(),
            "request_attempted": True,
            "response_received": False,
            "model_execution_verified": False,
            "dispatch_authority_created": False,
        }
        self.invocation_evidence = evidence
        try:
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            req = Request(
                self.config["endpoint"],
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(req, timeout=self.config["timeout_seconds"]) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                evidence["response_received"] = True
                evidence["http_status"] = response.status
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("navigation_wam_response_too_large")
            evidence["response_sha256"] = hashlib.sha256(raw).hexdigest()
            result = json.loads(
                raw,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_response")),
            )
            if (
                result.get("schema_version") != PREDICTION_SCHEMA
                or result.get("request_id") != request.request_id
                or result.get("request_sha256") != request.digest()
                or result.get("observation_id") != request.observation_id
                or result.get("binding") != asdict(self.binding)
                or result.get("verification_basis") != "model_inferred"
                or any(result.get(key) is not False for key in AUTHORITY_FLAGS)
            ):
                raise ValueError("navigation_wam_response_binding_mismatch")
            if result.get("status") != "available":
                raise ValueError("navigation_wam_unavailable")
            forecasts = result.get("forecasts")
            if not isinstance(forecasts, list) or len(forecasts) != len(request.options):
                raise ValueError("navigation_wam_invalid_options")
            by_id = {item["option_id"]: item for item in forecasts}
            if set(by_id) != {option.option_id for option in request.options}:
                raise ValueError("navigation_wam_invalid_options")
            for option in request.options:
                item = by_id[option.option_id]
                if set(item) != {"option_id", "horizon_seconds", "risk_score", "future_state"}:
                    raise ValueError("navigation_wam_invalid_forecast")
                if item["horizon_seconds"] != option.horizon_seconds or type(
                    item["horizon_seconds"]
                ) not in (int, float):
                    raise ValueError("navigation_wam_horizon_mismatch")
                _number(item["risk_score"], 0, 1, "navigation_wam_invalid_risk")
                if not isinstance(item["future_state"], dict):
                    raise ValueError("navigation_wam_invalid_future_state")
            evidence["response_contract_validated"] = True
            return tuple(OptionForecast(**item) for item in forecasts)
        except Exception as exc:
            self.rejection_reason = _failure_code(exc)
            evidence["response_contract_validated"] = False
            evidence["error_type"] = type(exc).__name__
            raise
        finally:
            evidence["completed_at"] = _utc()
            evidence["latency_ms"] = (time.monotonic() - start) * 1000


def navigation_state(telemetry, compiled_candidate=None):
    """Only trusted runtime observations and compiled action geometry reach the model."""
    if not isinstance(telemetry, dict) or not telemetry:
        raise ValueError("navigation_runtime_telemetry_required")
    world = {
        key: value
        for key, value in telemetry.items()
        if key not in {"observed_at", "sample_index", "elapsed_seconds", "telemetry_arbitration"}
    }
    if not world:
        raise ValueError("navigation_runtime_state_required")
    return snapshot({"runtime_telemetry": world, "compiled_candidate": compiled_candidate or {}})


def _observed_timestamp(telemetry):
    raw = telemetry.get("observed_at")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("navigation_observation_timestamp_required")
    observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("navigation_observation_timezone_required")
    return observed.timestamp()


def prepare_navigation_prediction(
    situation, *, backend, policy_sha256=None, action=None, parameters=None, now=None
):
    """Forecast a trusted simulator snapshot; required-mode rejection must block callers.

    ``policy_sha256`` must be computed from the active runtime policy, never read
    from the manifest. Shadow evidence is returned only in the side record.
    """
    mode = os.environ.get(MODE_ENV, "off")
    record = {
        "schema_version": "missionos_navigation_prediction.v1",
        "mode": mode,
        "backend": backend,
        "status": "off",
        "reason": "disabled",
        "required_blocked": False,
        "dispatch_authority_created": False,
    }
    if mode == "off":
        return situation, record
    provider = None
    try:
        if mode not in {"shadow", "required"}:
            raise ValueError("invalid_navigation_wam_mode")
        if situation.execution_scope not in {"simulator", "fixture"}:
            raise ValueError("navigation_wam_simulator_scope_required")
        config = load_navigation_config(backend, policy_sha256)
        telemetry = dict(situation.observations.get("runtime_telemetry") or {})
        context = dict(situation.constraints.get("mission_context") or {})
        compiled = (
            context.get("compiled_candidate")
            or situation.constraints.get("compiled_candidate")
            or {}
        )
        state = navigation_state(telemetry, compiled)
        if action is not None and (not isinstance(action, str) or not action.strip()):
            raise ValueError("invalid_navigation_action")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("invalid_navigation_parameters")
        if action is None and parameters:
            raise ValueError("navigation_parameters_require_action")
        option = action or "continue"
        options = [PredictionOption(option, config["horizon_seconds"], snapshot(parameters or {}))]
        if option != "hold":
            options.append(PredictionOption("hold", config["horizon_seconds"], {}))
        observation_id = "navigation_observation_" + prediction_digest(state)
        revision = prediction_digest(
            {
                "state": state,
                "options": [asdict(item) for item in options],
                "policy_sha256": policy_sha256,
            }
        )
        execution_id = str(
            situation.progress.get("task_id") or situation.progress.get("execution_id") or ""
        )
        if not execution_id or not situation.source_refs:
            raise ValueError("navigation_observation_provenance_required")
        request = PredictionRequest(
            "navigation_request_"
            + prediction_digest({"situation": situation.input_digest, "revision": revision}),
            observation_id,
            _observed_timestamp(telemetry),
            config["binding"],
            state,
            tuple(options),
        )
        provider = NavigationHTTPPredictor(config)
        registry = PredictionRegistry()
        registry.register(provider)
        evaluated_at = time.time() if now is None else now
        forecast = registry.forecast(
            request, now=evaluated_at, max_age_seconds=config["max_age_seconds"]
        )
        # Runtime-owned context is constructed before inspecting model output.
        current_context = {
            "execution_id": execution_id,
            "state_revision": revision,
            "source_ref": situation.source_refs[0],
            "observation_id": observation_id,
            "binding": asdict(config["binding"]),
        }
        envelope = capture_prediction_evidence(
            request,
            forecast,
            execution_id=execution_id,
            state_revision=revision,
            source_ref=situation.source_refs[0],
        )
        bound = replace(
            situation,
            mission_contract={
                **situation.mission_contract,
                "prediction_contract": config["binding"].mission_contract,
            },
            constraints={**situation.constraints, "prediction_context": current_context},
        )
        admitted, admission = receive_prediction_evidence(
            bound,
            envelope,
            now=time.time() if now is None else now,
            max_age_seconds=config["max_age_seconds"],
        )
        record.update(
            status="adopted"
            if mode == "required" and admission["status"] == "adopted"
            else "shadow"
            if admission["status"] == "adopted"
            else "rejected",
            reason=provider.rejection_reason or forecast.get("reason") or admission["reason"],
            required_blocked=mode == "required" and admission["status"] != "adopted",
            provider_kind=config["provider_kind"],
            config_sha256=_config_digest(config),
            max_age_seconds=config["max_age_seconds"],
            envelope=envelope,
            admission=admission,
            prediction_context=current_context,
            source_state_sha256=prediction_digest(state),
            policy_sha256=policy_sha256,
            invocation_evidence=provider.invocation_evidence,
        )
        return (admitted if mode == "required" else situation), snapshot(record)
    except Exception as exc:
        record.update(
            status="rejected",
            reason=_failure_code(exc),
            required_blocked=mode != "shadow",
            invocation_evidence=provider.invocation_evidence if provider else None,
        )
        return situation, record


def navigation_prediction_required(record):
    """Current policy can strengthen a frozen judgment, never weaken its needs."""
    if os.environ.get(MODE_ENV, "off") not in {"off", "shadow"}:
        return True
    if not record:
        return False
    return not isinstance(record, dict) or record.get("mode") not in {"off", "shadow"}


def revalidate_navigation_prediction(
    record,
    *,
    backend,
    telemetry,
    action=None,
    parameters=None,
    compiled_candidate=None,
    policy_sha256=None,
    now=None,
):
    """Compare original evidence with independently supplied current runtime state."""
    if not navigation_prediction_required(record):
        return {"status": "not_required", "reasons": [], "dispatch_authority_created": False}
    reasons = []
    try:
        if os.environ.get(MODE_ENV, "off") not in {"off", "shadow", "required"}:
            raise ValueError("invalid_navigation_wam_mode")
        if not isinstance(record, dict):
            raise ValueError("navigation_prediction_not_adopted")
        if (
            record.get("mode") != "required"
            or record.get("status") != "adopted"
            or record.get("required_blocked")
        ):
            raise ValueError("navigation_prediction_not_adopted")
        if record.get("backend") != backend or record.get("policy_sha256") != policy_sha256:
            raise ValueError("navigation_prediction_binding_changed")
        config = load_navigation_config(backend, policy_sha256)
        if record.get("config_sha256") != _config_digest(config):
            raise ValueError("navigation_prediction_config_changed")
        envelope = record["envelope"]
        request = envelope["request"]
        state = navigation_state(telemetry, compiled_candidate)
        if prediction_digest(state) != record["source_state_sha256"] or state != request["state"]:
            raise ValueError("navigation_prediction_state_changed")
        option = action or "continue"
        matching = [item for item in request["options"] if item["option_id"] == option]
        if len(matching) != 1 or matching[0]["parameters"] != (parameters or {}):
            raise ValueError("navigation_prediction_candidate_changed")
        current = time.time() if now is None else now
        if (
            not 0 <= current - request["observed_at"] <= record["max_age_seconds"]
            or not 0 <= current - _observed_timestamp(telemetry) <= record["max_age_seconds"]
        ):
            raise ValueError("navigation_prediction_stale")
        if record["admission"].get("evidence_sha256") != prediction_digest(envelope):
            raise ValueError("navigation_prediction_evidence_changed")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        reasons.append(
            str(exc) if isinstance(exc, ValueError) else "navigation_prediction_malformed"
        )
    return {
        "status": "blocked" if reasons else "valid",
        "reasons": reasons,
        "dispatch_authority_created": False,
    }
