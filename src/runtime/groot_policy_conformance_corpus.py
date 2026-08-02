"""Validation for the derived GR00T policy-client conformance manifest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.runtime.corpus_publication_sanitation import publication_findings
from src.runtime.groot_policy_client import (
    GROOT_MODEL_SNAPSHOT,
    GROOT_REPOSITORY_REVISION,
    GROOT_REQUEST_SCHEMA,
    GROOT_RESPONSE_SCHEMA,
)


GROOT_CORPUS_SCHEMA = "missionos_groot_policy_conformance_corpus.v1"
EXPECTED_OBSERVATION_FIELDS = {
    "annotation.human.action.task_description": {
        "dtype": "string",
        "shape": [1],
    },
    "state.left_arm": {"dtype": "float64", "shape": [1, 7]},
    "state.left_hand": {"dtype": "float64", "shape": [1, 6]},
    "state.right_arm": {"dtype": "float64", "shape": [1, 7]},
    "state.right_hand": {"dtype": "float64", "shape": [1, 6]},
    "video.ego_view": {"dtype": "uint8", "shape": [1, 256, 256, 3]},
}
EXPECTED_ACTION_FIELDS = {
    "action.left_arm": {"dtype": "float32", "shape": [16, 7]},
    "action.left_hand": {"dtype": "float32", "shape": [16, 6]},
    "action.right_arm": {"dtype": "float32", "shape": [16, 7]},
    "action.right_hand": {"dtype": "float32", "shape": [16, 6]},
}
REQUIRED_NEGATIVE_CASES = frozenset(
    {
        "empty_instruction",
        "missing_request_field",
        "wrong_image_dtype",
        "wrong_image_shape",
        "non_finite_state",
        "missing_response_field",
        "malformed_response_field",
        "non_finite_action_output",
        "transport_timeout",
        "unknown_policy_revision",
        "unknown_schema_revision",
    }
)


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def verify_groot_policy_corpus(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if manifest.get("schema_version") != GROOT_CORPUS_SCHEMA:
        reasons.append("groot_corpus_schema_not_supported")
    material = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != _sha256(material):
        reasons.append("groot_corpus_hash_mismatch")
    if publication_findings(manifest):
        reasons.append("groot_corpus_publication_boundary_violated")

    source = manifest.get("source_contract")
    if not isinstance(source, Mapping):
        reasons.append("groot_corpus_source_contract_missing")
        source = {}
    expected_source = {
        "repository_revision": GROOT_REPOSITORY_REVISION,
        "model_snapshot": GROOT_MODEL_SNAPSHOT,
        "request_schema": GROOT_REQUEST_SCHEMA,
        "response_schema": GROOT_RESPONSE_SCHEMA,
        "action_horizon_samples": 16,
        "source_runtime_reexecuted": False,
        "robot_dispatch_observed": False,
        "motion_observed": False,
        "physical_execution_invoked": False,
    }
    if dict(source) != expected_source:
        reasons.append("groot_corpus_source_contract_changed")
    if manifest.get("observation_fields") != EXPECTED_OBSERVATION_FIELDS:
        reasons.append("groot_corpus_observation_contract_changed")
    if manifest.get("action_fields") != EXPECTED_ACTION_FIELDS:
        reasons.append("groot_corpus_action_contract_changed")
    if manifest.get("execution_scope") != "loopback":
        reasons.append("groot_corpus_execution_scope_invalid")
    if manifest.get("fixture_kind") != "schema_example_only":
        reasons.append("groot_corpus_fixture_kind_invalid")

    publication = manifest.get("publication_boundary")
    if not isinstance(publication, Mapping) or any(
        value is not False for value in publication.values()
    ):
        reasons.append("groot_corpus_publication_declaration_invalid")

    negative_cases = manifest.get("negative_cases")
    if not isinstance(negative_cases, list):
        reasons.append("groot_corpus_negative_cases_invalid")
        negative_cases = []
    if set(negative_cases) != REQUIRED_NEGATIVE_CASES:
        reasons.append("groot_corpus_negative_cases_incomplete")

    boundary = manifest.get("authority_boundary")
    if not isinstance(boundary, Mapping) or boundary.get(
        "verification_basis"
    ) != "model_inferred":
        reasons.append("groot_corpus_verification_basis_invalid")
    if not isinstance(boundary, Mapping) or any(
        boundary.get(flag) is not False
        for flag in (
            "approval_created",
            "dispatch_authority_created",
            "dispatch_request_sent",
            "execution_claimed",
            "progress_claimed",
            "safe_stop_claimed",
            "completion_claimed",
            "physical_execution_invoked",
        )
    ):
        reasons.append("groot_corpus_authority_boundary_invalid")

    return {
        "schema_version": "missionos_groot_policy_conformance_verdict.v1",
        "status": "verified" if not reasons else "failed",
        "reasons": list(dict.fromkeys(reasons)),
        "source_runtime_reexecuted": False,
        "physical_execution_invoked": False,
    }


__all__ = [
    "EXPECTED_ACTION_FIELDS",
    "EXPECTED_OBSERVATION_FIELDS",
    "GROOT_CORPUS_SCHEMA",
    "REQUIRED_NEGATIVE_CASES",
    "verify_groot_policy_corpus",
]
