from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.runtime.corpus_publication_sanitation import publication_findings
from src.runtime.groot_policy_client import (
    GROOT_REPOSITORY_REVISION,
    GrootPolicyBinding,
    GrootPolicyBoundaryError,
    assess_groot_action_chunk,
    build_groot_sim_freshness_policy,
    request_groot_action_chunk,
    validate_groot_request,
    validate_groot_response,
)
from src.runtime.groot_policy_conformance_corpus import verify_groot_policy_corpus


pytestmark = pytest.mark.contract
CORPUS = (
    Path(__file__).parents[1]
    / "golden"
    / "groot_policy_client_v1"
    / "manifest.json"
)


def _request() -> dict[str, Any]:
    return {
        "annotation.human.action.task_description": ["place item in bin"],
        "state.left_arm": np.zeros((1, 7), dtype=np.float64),
        "state.left_hand": np.zeros((1, 6), dtype=np.float64),
        "state.right_arm": np.zeros((1, 7), dtype=np.float64),
        "state.right_hand": np.zeros((1, 6), dtype=np.float64),
        "video.ego_view": np.zeros((1, 256, 256, 3), dtype=np.uint8),
    }


def _response() -> dict[str, Any]:
    return {
        "action.left_arm": np.zeros((16, 7), dtype=np.float32),
        "action.left_hand": np.zeros((16, 6), dtype=np.float32),
        "action.right_arm": np.zeros((16, 7), dtype=np.float32),
        "action.right_hand": np.zeros((16, 6), dtype=np.float32),
    }


def _binding() -> GrootPolicyBinding:
    return GrootPolicyBinding(
        instruction_ref="instruction-001",
        preparation_sha256="a" * 64,
        observed_at="2026-07-26T00:00:00Z",
        freshness_deadline="2026-07-26T00:00:03Z",
        freshness_policy=build_groot_sim_freshness_policy(),
    )


class _Transport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def get_action(self, payload: Any) -> dict[str, Any]:
        self.calls += 1
        return self.response


class _Clock:
    def __init__(self, *values: str) -> None:
        self.values = [
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in values
        ]
        self.index = 0

    def __call__(self) -> datetime:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def test_valid_output_remains_only_a_model_inferred_proposal() -> None:
    transport = _Transport(_response())

    proposal = request_groot_action_chunk(
        transport=transport,
        payload=_request(),
        binding=_binding(),
        clock=_Clock(
            "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:01.1Z",
        ),
    )

    assert proposal.verification_basis == "model_inferred"
    assert proposal.policy_revision == GROOT_REPOSITORY_REVISION
    assert proposal.request_sha256 != proposal.observation_sha256
    assert proposal.response_received_at == "2026-07-26T00:00:01.100000Z"
    assert transport.calls == 1
    assert all(
        getattr(proposal, name) is False
        for name in (
            "approval_created",
            "dispatch_authority_created",
            "dispatch_request_sent",
            "execution_claimed",
            "progress_claimed",
            "safe_stop_claimed",
            "completion_claimed",
            "physical_execution_invoked",
        )
    )
    with pytest.raises(ValueError):
        proposal.actions["action.left_arm"][0, 0] = 1.0


def test_instruction_changes_request_digest_but_not_observation_digest() -> None:
    first = request_groot_action_chunk(
        transport=_Transport(_response()),
        payload=_request(),
        binding=_binding(),
        clock=_Clock(
            "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:01.1Z",
        ),
    )
    changed = _request()
    changed["annotation.human.action.task_description"] = ["move item to bin"]
    second = request_groot_action_chunk(
        transport=_Transport(_response()),
        payload=changed,
        binding=_binding(),
        clock=_Clock(
            "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:01.1Z",
        ),
    )

    assert first.request_sha256 != second.request_sha256
    assert first.observation_sha256 == second.observation_sha256


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value.__setitem__(
                "annotation.human.action.task_description", [""]
            ),
            "groot_instruction_empty_or_invalid",
        ),
        (
            lambda value: value.pop("state.left_arm"),
            "groot_request_fields_invalid",
        ),
        (
            lambda value: value.__setitem__(
                "video.ego_view",
                np.zeros((1, 256, 256, 3), dtype=np.float64),
            ),
            "groot_request_video_ego_view_dtype_invalid",
        ),
        (
            lambda value: value.__setitem__(
                "video.ego_view",
                np.zeros((256, 256, 3), dtype=np.uint8),
            ),
            "groot_request_video_ego_view_shape_invalid",
        ),
        (
            lambda value: value["state.left_arm"].__setitem__((0, 0), np.nan),
            "groot_request_state_left_arm_non_finite",
        ),
    ],
)
def test_invalid_request_is_rejected_before_transport(
    mutation: Any,
    reason: str,
) -> None:
    payload = _request()
    mutation(payload)
    transport = _Transport(_response())

    with pytest.raises(GrootPolicyBoundaryError, match=reason):
        request_groot_action_chunk(
            transport=transport,
            payload=payload,
            binding=_binding(),
            clock=_Clock("2026-07-26T00:00:01Z"),
        )

    assert transport.calls == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value.pop("action.left_arm"),
            "groot_response_fields_invalid",
        ),
        (
            lambda value: value.__setitem__("action.left_arm", "bad"),
            "groot_response_action_left_arm_type_invalid",
        ),
        (
            lambda value: value.__setitem__(
                "action.left_arm",
                value["action.left_arm"].astype(np.float64),
            ),
            "groot_response_action_left_arm_dtype_invalid",
        ),
        (
            lambda value: value["action.left_arm"].__setitem__((0, 0), np.inf),
            "groot_response_action_left_arm_non_finite",
        ),
    ],
)
def test_malformed_model_output_fails_closed(
    mutation: Any,
    reason: str,
) -> None:
    response = _response()
    mutation(response)

    with pytest.raises(GrootPolicyBoundaryError, match=reason):
        request_groot_action_chunk(
            transport=_Transport(response),
            payload=_request(),
            binding=_binding(),
            clock=_Clock(
                "2026-07-26T00:00:01Z",
                "2026-07-26T00:00:01.1Z",
            ),
        )


@pytest.mark.parametrize(
    ("binding", "evaluated_at", "reason"),
    [
        (
            replace(_binding(), policy_revision="unknown"),
            "2026-07-26T00:00:01Z",
            "groot_policy_revision_not_supported",
        ),
        (
            replace(_binding(), request_schema="unknown"),
            "2026-07-26T00:00:01Z",
            "groot_request_schema_not_supported",
        ),
        (
            _binding(),
            "2026-07-26T00:00:04Z",
            "groot_observation_stale",
        ),
    ],
)
def test_unknown_contract_or_stale_observation_fails_before_transport(
    binding: GrootPolicyBinding,
    evaluated_at: str,
    reason: str,
) -> None:
    transport = _Transport(_response())

    with pytest.raises(GrootPolicyBoundaryError, match=reason):
        request_groot_action_chunk(
            transport=transport,
            payload=_request(),
            binding=binding,
            clock=_Clock(evaluated_at),
        )

    assert transport.calls == 0


@pytest.mark.parametrize(
    ("binding", "reason"),
    [
        (
            replace(
                _binding(),
                freshness_policy=replace(
                    build_groot_sim_freshness_policy(),
                    maximum_observation_age_seconds=2.0,
                ),
            ),
            "groot_freshness_policy_not_supported",
        ),
        (
            replace(
                _binding(),
                freshness_policy=replace(
                    build_groot_sim_freshness_policy(),
                    maximum_observation_age_seconds=30.0,
                ),
            ),
            "groot_freshness_policy_not_supported",
        ),
        (
            replace(
                _binding(),
                freshness_policy=replace(
                    build_groot_sim_freshness_policy(),
                    policy_sha256="0" * 64,
                ),
            ),
            "groot_freshness_policy_digest_invalid",
        ),
        (
            replace(
                _binding(),
                freshness_deadline="2026-07-26T00:00:02Z",
            ),
            "groot_freshness_deadline_policy_mismatch",
        ),
    ],
)
def test_caller_cannot_widen_or_detach_freshness_policy(
    binding: GrootPolicyBinding,
    reason: str,
) -> None:
    transport = _Transport(_response())

    with pytest.raises(GrootPolicyBoundaryError, match=reason):
        request_groot_action_chunk(
            transport=transport,
            payload=_request(),
            binding=binding,
            clock=_Clock("2026-07-26T00:00:01Z"),
        )

    assert transport.calls == 0


def test_response_arriving_after_freshness_deadline_is_rejected() -> None:
    transport = _Transport(_response())

    with pytest.raises(
        GrootPolicyBoundaryError,
        match="groot_observation_stale",
    ):
        request_groot_action_chunk(
            transport=transport,
            payload=_request(),
            binding=_binding(),
            clock=_Clock(
                "2026-07-26T00:00:01Z",
                "2026-07-26T00:00:04Z",
            ),
        )

    assert transport.calls == 1


def test_schema_and_freshness_are_assessed_independently() -> None:
    response = _response()
    response.pop("action.right_hand")
    assessment = assess_groot_action_chunk(
        transport=_Transport(response),
        payload=_request(),
        binding=_binding(),
        clock=_Clock(
            "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:04Z",
        ),
    )

    assert assessment.response_received is True
    assert assessment.response_schema_valid is False
    assert assessment.response_schema_reason == "groot_response_fields_invalid"
    assert assessment.temporal_freshness_valid is False
    assert assessment.temporal_freshness_reason == "groot_observation_stale"
    assert assessment.response_sha256 is None
    assert assessment.proposal is None


def test_valid_but_stale_response_does_not_create_proposal() -> None:
    assessment = assess_groot_action_chunk(
        transport=_Transport(_response()),
        payload=_request(),
        binding=_binding(),
        clock=_Clock(
            "2026-07-26T00:00:01Z",
            "2026-07-26T00:00:04Z",
        ),
    )

    assert assessment.response_schema_valid is True
    assert assessment.response_sha256 is not None
    assert assessment.temporal_freshness_valid is False
    assert assessment.proposal is None


def test_regressed_clock_after_transport_fails_closed() -> None:
    with pytest.raises(GrootPolicyBoundaryError, match="groot_clock_regressed"):
        request_groot_action_chunk(
            transport=_Transport(_response()),
            payload=_request(),
            binding=_binding(),
            clock=_Clock(
                "2026-07-26T00:00:01Z",
                "2026-07-26T00:00:00.5Z",
            ),
        )


def test_derived_corpus_is_complete_and_publication_safe() -> None:
    assert verify_groot_policy_corpus(CORPUS) == {
        "schema_version": "missionos_groot_policy_conformance_verdict.v1",
        "status": "verified",
        "reasons": [],
        "source_runtime_reexecuted": False,
        "physical_execution_invoked": False,
    }


def test_resealed_shape_change_cannot_redefine_observed_contract(
    tmp_path: Path,
) -> None:
    manifest = json.loads(CORPUS.read_text(encoding="utf-8"))
    manifest["observation_fields"]["state.left_arm"]["shape"] = [1, 8]
    material = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_sha256"
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    verdict = verify_groot_policy_corpus(path)

    assert verdict["status"] == "failed"
    assert "groot_corpus_observation_contract_changed" in verdict["reasons"]


@pytest.mark.parametrize(
    "unsafe",
    [
        {"source_path": "third-party/source/service.py"},
        {"input_image": "captured-frame.png"},
        {"model_action_array": [0.1, 0.2]},
        {"raw_cloud_evidence": {"host": "gpu-worker"}},
        {"payload": b"binary-model-or-image-data"},
        {"token": "redacted-example"},
    ],
)
def test_publication_sanitizer_rejects_vla_artifact_classes(
    unsafe: dict[str, Any],
) -> None:
    assert publication_findings(unsafe)


def test_validation_helpers_reject_unexpected_fields() -> None:
    with pytest.raises(GrootPolicyBoundaryError):
        validate_groot_request({"unexpected": True})
    with pytest.raises(GrootPolicyBoundaryError):
        validate_groot_response({"unexpected": True})
