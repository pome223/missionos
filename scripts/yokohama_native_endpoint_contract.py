"""Separate source-bound single-attempt endpoint admission; no runtime effects."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT = {
    "schema": "yokohama.adapted-endpoint-trial.v1",
    "timeout_s": 900,
    "recovery_trigger_margin_s": 5,
    "maximum_flight_attempts": 1,
    "sea_or_payload_allowed": False,
    "physical_execution_allowed": False,
}


def validate_config(config):
    """Admit the same bounded controller with an explicit CPU double or native."""
    if __package__:
        from .yokohama_goal_distance_adapter import POLICY
        from .yokohama_candidate_recovery import LIMITS
    else:
        from yokohama_goal_distance_adapter import POLICY
        from yokohama_candidate_recovery import LIMITS
    if (
        config.get("endpoint_adapter_trial") != CONTRACT
        or config.get("timeout_s") != 900
        or config.get("candidate_recovery") != LIMITS
        or config.get("fixture_reject_second_candidate", False) is not False
        or config.get("decisions", {}).get("backend") not in {"fixture", "native"}
        or config["decisions"].get("goal_distance_adapter") != POLICY
        or not config.get("operator_approval_manifest_sha256")
    ):
        raise ValueError("Unapproved adapted endpoint trial contract")


def arguments(models, image, service=None):
    from scripts.yokohama_local_preflight import image_id

    if models not in {"fixture", "native"} or (service is not None) != (models == "native"):
        raise ValueError("Explicit native service or CPU double required")
    values = [
        "--phase",
        "flight",
        "--endpoint-feedback",
        "--goal-distance-adapter",
        "--endpoint-adapter-trial",
        "--decision-backend",
        models,
        "--wam-profile",
        "motion-v4",
        "--timeout-seconds",
        "900",
        "--local-image-id",
        image_id(image),
    ]
    if service is not None:
        values += ["--native-service-config", str(Path(service).resolve())]
    return values


def admit(raw, root, supplied):
    from scripts.yokohama_candidate_recovery import LIMITS
    from scripts.yokohama_endpoint_feedback import FIXED
    from scripts.yokohama_goal_distance_adapter import POLICY
    from src.runtime.yokohama_execution_service import proposal_digest, recovery_input_hashes

    manifest = json.loads(raw)
    proposal, approval = manifest["proposal"], manifest["approval"]
    models = proposal.get("city_models")
    service = proposal.get("native_service_config")
    if (
        proposal.get("schema") != "yokohama.adapted-endpoint-proposal.v1"
        or not proposal.get("proposal_id")
        or proposal.get("contract") != CONTRACT
        or proposal.get("limits") != LIMITS
        or proposal.get("feedback_limits") != FIXED
        or proposal.get("adapter_policy") != POLICY
        or proposal.get("physical_execution_invoked") is not False
        or proposal.get("simulator_arguments")
        != arguments(models, proposal.get("image_id"), service)
        or supplied != proposal["simulator_arguments"]
        or proposal.get("input_sha256") != recovery_input_hashes(Path(root))
        or approval.get("approved_proposal_sha256") != proposal_digest(proposal)
        or approval.get("maximum_actual_flight_trials") != 1
        or not all(
            approval.get(k) for k in ("operator_approval_ref", "actor_session_id", "approved_at")
        )
    ):
        raise ValueError("Adapted endpoint approval/source/arguments mismatch")
    service_bytes = Path(service).read_bytes() if service is not None else None
    expected = hashlib.sha256(service_bytes).hexdigest() if service_bytes is not None else None
    if proposal.get("native_service_config_sha256") != expected:
        raise ValueError("Native endpoint service changed since approval")
    return manifest, service_bytes
