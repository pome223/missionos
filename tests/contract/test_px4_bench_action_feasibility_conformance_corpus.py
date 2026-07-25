from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from missionos_core import run_conformance_corpus
from src.runtime.px4_bench_action_feasibility_corpus import (
    seal_px4_bench_corpus_case,
    verify_px4_bench_corpus,
    verify_px4_bench_corpus_case,
    verify_px4_bench_corpus_through_core,
)


pytestmark = pytest.mark.contract

CORPUS_ROOT = (
    Path(__file__).parents[1]
    / "golden"
    / "action_feasibility"
    / "px4_bench_v1"
)
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"

EXPECTED_CASES = {
    "px4-bench-positive-verified-arm-disarm": ("verified_feasible", None),
    "px4-bench-refusal-estop-unavailable": (
        "blocked",
        "bench_physical_estop_missing",
    ),
    "px4-bench-refusal-vehicle-not-secured": (
        "blocked",
        "bench_vehicle_not_secured",
    ),
    "px4-bench-refusal-props-attached": ("blocked", "bench_props_attached"),
    "px4-bench-refusal-loopback-link-kind": (
        "unverified",
        "bench_link_not_physical",
    ),
    "px4-bench-refusal-stale-telemetry": ("unverified", "evidence_stale"),
    "px4-bench-refusal-heartbeat-loss": (
        "unverified",
        "bench_heartbeat_lost",
    ),
    "px4-bench-refusal-action-not-in-allowlist": (
        "blocked",
        "bench_action_not_in_allowlist",
    ),
}


def _case(case_id: str) -> dict:
    return json.loads(
        (CORPUS_ROOT / "cases" / f"{case_id}.json").read_text(encoding="utf-8")
    )


def test_bench_manifest_replays_offline_through_core() -> None:
    verdict = verify_px4_bench_corpus(MANIFEST_PATH)

    assert verdict["status"] == "verified"
    assert verdict["case_count"] == 8
    assert verdict["positive_case_count"] == 1
    assert verdict["refusal_case_count"] == 7
    assert verdict["reasons"] == []


def test_bench_corpus_uses_the_same_core_runner_as_other_backends() -> None:
    verdict = run_conformance_corpus(
        MANIFEST_PATH, execute_case=verify_px4_bench_corpus_case
    )

    assert verdict["status"] == "verified"
    assert verdict["schema_version"] == "missionos_core_conformance_run.v1"
    assert verify_px4_bench_corpus_through_core(MANIFEST_PATH) == verdict


@pytest.mark.parametrize(
    ("case_id", "expected"), sorted(EXPECTED_CASES.items())
)
def test_bench_case_status_is_frozen(
    case_id: str, expected: tuple[str, str | None]
) -> None:
    expected_status, required_reason = expected

    verdict = verify_px4_bench_corpus_case(_case(case_id))

    assert verdict["passed"] is True
    assert verdict["status"] == expected_status
    assert verdict["reasons"] == []
    if required_reason:
        assert required_reason in [
            *verdict["blocked_reasons"],
            *verdict["unverified_reasons"],
        ]


def test_replay_never_claims_physical_execution() -> None:
    """The corpus must not become the place a physical claim is made."""

    verdict = verify_px4_bench_corpus(MANIFEST_PATH)

    for case_verdict in verdict["case_verdicts"]:
        flags = case_verdict["output_flags"]
        assert flags["physical_execution_invoked"] is False
        assert all(value is False for value in flags.values())
        assert case_verdict["adapter_result"]["source_runtime_reexecuted"] is False


def test_no_case_claims_live_bench_evidence_before_a_live_run() -> None:
    """#105 is contract-derived. #107 reseals the positive case, not #105."""

    for case_id in EXPECTED_CASES:
        runtime_truth = _case(case_id)["truth_boundary"]["runtime_truth"]

        assert runtime_truth["source_runtime_evidence_available"] is False
        assert runtime_truth["source_runtime_evidence_refs"] == []
        assert runtime_truth["source_contract_evidence_refs"]
        assert runtime_truth["runtime_invoked_by_this_replay"] is False
        assert (
            runtime_truth["physical_execution_invoked_by_this_replay"] is False
        )


def test_positive_case_completion_stays_at_adapter_action_scope() -> None:
    completion = _case("px4-bench-positive-verified-arm-disarm")[
        "authority_chain"
    ]["completion"]

    assert completion["completion_scope"] == "adapter_action"
    assert completion["flight_claimed"] is False
    assert completion["mission_completion_claimed"] is False
    assert completion["delivery_completion_claimed"] is False


def test_allowlist_refusal_holds_with_every_physical_condition_satisfied() -> None:
    """An allowlist refusal must not depend on some other condition failing."""

    case = _case("px4-bench-refusal-action-not-in-allowlist")
    facts = {
        item["name"]: item["value"]
        for item in case["hazard_state"]["observed_facts"]
    }

    assert facts["physical_estop_available"] is True
    assert facts["vehicle_physically_secured"] is True
    assert facts["power_disconnect_available"] is True
    assert facts["props_removed_attested"] is True
    assert facts["link_kind"] == "serial"

    verdict = verify_px4_bench_corpus_case(case)

    assert verdict["blocked_reasons"] == ["bench_action_not_in_allowlist"]


def test_loopback_refusal_differs_from_positive_only_by_link_kind() -> None:
    """The evidence-grade guard must be the sole cause of the refusal."""

    positive = _case("px4-bench-positive-verified-arm-disarm")
    loopback = _case("px4-bench-refusal-loopback-link-kind")

    def facts(case: dict) -> dict:
        return {
            item["name"]: item["value"]
            for item in case["hazard_state"]["observed_facts"]
        }

    positive_facts = facts(positive)
    loopback_facts = facts(loopback)
    differing = {
        name
        for name in positive_facts.keys() | loopback_facts.keys()
        if positive_facts.get(name) != loopback_facts.get(name)
    }

    assert differing == {"link_kind"}
    assert verify_px4_bench_corpus_case(loopback)["unverified_reasons"] == [
        "bench_link_not_physical"
    ]


@pytest.mark.parametrize(
    "private_value",
    [
        {"task_id": "task_deadbeefcafebabe"},
        {"artifact_path": "/Users/operator/private/evidence.json"},
        {"credential": "sk-private-example-not-for-publication"},
        {"link_endpoint": "/dev/tty.usbmodem14201"},
        {"link_endpoint": "COM3"},
        {"autopilot_uid": "000200000000383832343437511800230026"},
        {"board_serial": "26003b000a51383236343437"},
        {"approval_actor": "an-operator-real-name"},
    ],
)
def test_bench_publication_sanitizer_rejects_private_material(
    private_value: dict,
) -> None:
    unsafe = copy.deepcopy(_case("px4-bench-positive-verified-arm-disarm"))
    unsafe["unsafe"] = private_value
    unsafe = seal_px4_bench_corpus_case(unsafe)

    verdict = verify_px4_bench_corpus_case(unsafe)

    assert verdict["passed"] is False
    assert "bench_corpus_publication_boundary_violated" in verdict["reasons"]


def test_case_integrity_rejects_semantic_tampering() -> None:
    tampered = copy.deepcopy(_case("px4-bench-refusal-props-attached"))
    for item in tampered["hazard_state"]["observed_facts"]:
        if item["name"] == "props_removed_attested":
            item["value"] = True

    verdict = verify_px4_bench_corpus_case(tampered)

    assert verdict["passed"] is False
    assert "bench_corpus_case_hash_mismatch" in verdict["reasons"]


def test_resealed_tampering_is_still_caught_by_the_frozen_expectation() -> None:
    """Resealing repairs the hash, so the expectation must be the real guard."""

    tampered = copy.deepcopy(_case("px4-bench-refusal-props-attached"))
    for item in tampered["hazard_state"]["observed_facts"]:
        if item["name"] == "props_removed_attested":
            item["value"] = True
    tampered = seal_px4_bench_corpus_case(tampered)

    verdict = verify_px4_bench_corpus_case(tampered)

    assert verdict["passed"] is False
    assert "bench_corpus_case_hash_mismatch" not in verdict["reasons"]
    assert "bench_corpus_status_changed" in verdict["reasons"]
    assert "bench_corpus_refusal_became_feasible" in verdict["reasons"]
