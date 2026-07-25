from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from missionos_core import run_conformance_corpus
from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    seal_action_feasibility_corpus_case,
    verify_action_feasibility_corpus,
    verify_action_feasibility_corpus_case,
    verify_action_feasibility_corpus_through_core,
)


pytestmark = pytest.mark.contract

CORPUS_ROOT = (
    Path(__file__).parents[1]
    / "golden"
    / "action_feasibility"
    / "px4_v1"
)
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
EXPECTED_CASES = {
    "px4-positive-verified-avoidance": (
        "verified_feasible",
        None,
    ),
    "px4-refusal-missing-obstacle-geometry": (
        "unverified",
        "action_feasibility_obstacle_geometry_unverified",
    ),
    "px4-refusal-missing-thermal-readback": (
        "unverified",
        "action_feasibility_temperature_model_unverified",
    ),
    "px4-refusal-missing-performance-envelope": (
        "unverified",
        "action_feasibility_offboard_performance_envelope_unverified",
    ),
    "px4-refusal-stale-telemetry": (
        "unverified",
        "hazard_state_telemetry_stale",
    ),
    "px4-refusal-cursor-regression": (
        "unverified",
        "hazard_state_telemetry_cursor_regression",
    ),
    "px4-refusal-policy-drift": (
        "unverified",
        "action_feasibility_policy_drift",
    ),
    "px4-refusal-negative-wind-control-margin": (
        "blocked",
        "action_feasibility_wind_control_margin_not_positive",
    ),
}


def _case(case_id: str) -> dict:
    return json.loads(
        (CORPUS_ROOT / "cases" / f"{case_id}.json").read_text(
            encoding="utf-8"
        )
    )


def test_manifest_replays_positive_and_refusal_cases_offline() -> None:
    verdict = verify_action_feasibility_corpus(MANIFEST_PATH)

    assert verdict["verification_status"] == "verified"
    assert verdict["case_count"] == 8
    assert verdict["positive_case_count"] == 1
    assert verdict["refusal_case_count"] == 7
    assert verdict["blocking_reasons"] == []
    assert verdict["source_runtime_reexecuted"] is False
    assert verdict["llm_invoked"] is False
    assert verdict["approval_created"] is False
    assert verdict["dispatch_authority_created"] is False
    assert verdict["physical_execution_invoked"] is False
    assert verdict["completion_claimed"] is False


def test_manifest_runs_through_backend_neutral_core_suite() -> None:
    verdict = verify_action_feasibility_corpus_through_core(MANIFEST_PATH)

    assert verdict["status"] == "verified"
    assert verdict["case_count"] == 8
    assert verdict["reasons"] == []
    assert all(item["passed"] for item in verdict["case_verdicts"])
    assert verdict["approval_created"] is False
    assert verdict["dispatch_authority_created"] is False
    assert verdict["execution_invoked"] is False
    assert verdict["progress_claimed"] is False
    assert verdict["completion_claimed"] is False


def test_core_suite_rejects_an_authority_creating_adapter() -> None:
    def unsafe_adapter(_case):
        return {
            "passed": True,
            "output_flags": {
                "llm_invoked": False,
                "approval_created": True,
                "dispatch_authority_created": False,
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
                "progress_claimed": False,
                "completion_claimed": False,
                "delivery_completion_claimed": False,
            },
        }

    verdict = run_conformance_corpus(
        MANIFEST_PATH,
        execute_case=unsafe_adapter,
    )

    assert verdict["status"] == "failed"
    assert "adapter_created_authority" in verdict["reasons"]


@pytest.mark.parametrize(
    ("case_id", "expected_status", "required_reason"),
    [
        (case_id, status, reason)
        for case_id, (status, reason) in EXPECTED_CASES.items()
    ],
)
def test_each_case_preserves_exact_feasibility_semantics(
    case_id: str,
    expected_status: str,
    required_reason: str | None,
) -> None:
    case = _case(case_id)

    verdict = verify_action_feasibility_corpus_case(case)

    assert verdict["verification_status"] == "verified"
    assert verdict["feasibility_status"] == expected_status
    assert verdict["blocking_reasons"] == []
    expected = case["expected"]
    if required_reason:
        assert required_reason in (
            expected["blocking_reasons"] + expected["unverified_reasons"]
        )
    assert expected["required_assumptions"]


def test_positive_case_keeps_authority_and_observation_artifacts_separate() -> None:
    case = _case("px4-positive-verified-avoidance")
    chain = case["authority_chain"]
    refs = [chain[name]["artifact_ref"] for name in chain]

    assert len(refs) == len(set(refs)) == 7
    assert chain["proposal"]["llm_judgment_observed"] is True
    assert chain["proposal"]["approval_created"] is False
    assert chain["proposal"]["dispatch_authority_created"] is False
    assert chain["human_approval"]["human_approval_performed"] is True
    assert chain["dispatch_revalidation"]["status"] == "valid"
    assert chain["dispatch_authority"]["created"] is True
    assert chain["runner_ack"]["observed"] is True
    assert chain["runner_ack"]["ack_is_execution_effect"] is False
    assert chain["observed_effect"]["target_reached"] is True
    assert (
        chain["observed_effect"]["resume_status"] == "resumed_auto_mission"
    )
    assert chain["completion"]["landed"] is True
    assert chain["completion"]["disarmed"] is True
    assert chain["completion"]["delivery_completion_claimed"] is False
    assert chain["completion"]["physical_execution_invoked"] is False


def test_replay_truth_does_not_claim_a_new_runtime_invocation() -> None:
    case = _case("px4-positive-verified-avoidance")
    truth = case["truth_boundary"]

    assert truth["artifact_truth"]["case_is_replay_fixture"] is True
    assert truth["runtime_truth"]["source_runtime_evidence_available"] is True
    assert truth["runtime_truth"]["source_contract_evidence_available"] is True
    assert truth["runtime_truth"]["runtime_invoked_by_this_replay"] is False
    assert truth["runtime_truth"]["llm_invoked_by_this_replay"] is False
    assert truth["runtime_truth"]["simulator_invoked_by_this_replay"] is False


def test_contract_only_refusal_does_not_overclaim_live_runtime_evidence() -> None:
    case = _case("px4-refusal-missing-obstacle-geometry")
    runtime_truth = case["truth_boundary"]["runtime_truth"]

    assert runtime_truth["source_runtime_evidence_available"] is False
    assert runtime_truth["source_runtime_evidence_refs"] == []
    assert runtime_truth["source_contract_evidence_available"] is True
    assert runtime_truth["source_contract_evidence_refs"]


@pytest.mark.parametrize(
    "private_value",
    [
        {"task_id": "task_deadbeefcafebabe"},
        {"artifact_path": "/Users/operator/private/evidence.json"},
        {"database_path": "/tmp/private-task-store.sqlite3"},
        {"credential": "sk-private-example-not-for-publication"},
        # Bench-era rules. A hardware corpus must not carry the serial endpoint
        # that identifies one workstation, the identifier of one board, or the
        # name of the human who approved.
        {"link_endpoint": "/dev/tty.usbmodem14201"},
        {"link_endpoint": "/dev/ttyACM0"},
        {"link_endpoint": "COM3"},
        {"link_endpoint": "\\\\.\\COM17"},
        {"autopilot_uid": "000200000000383832343437511800230026"},
        {"board_serial": "26003b000a51383236343437"},
        {"hardware_uid": "0x1f2e3d4c5b6a7988"},
        {"serial_number": "FT9K3PZQ"},
        {"serial_port": "usb-modem-endpoint"},
        {"device_path": "relative/looking/but/still/an/endpoint"},
        {"approval_actor": "an-operator-real-name"},
        {"operator_name": "an-operator-real-name"},
    ],
)
def test_publication_sanitizer_rejects_private_material(
    private_value: dict,
) -> None:
    case = _case("px4-positive-verified-avoidance")
    unsafe = copy.deepcopy(case)
    unsafe["unsafe"] = private_value
    unsafe = seal_action_feasibility_corpus_case(unsafe)

    verdict = verify_action_feasibility_corpus_case(unsafe)

    assert verdict["verification_status"] == "failed"
    assert "corpus_case_publication_boundary_violated" in verdict[
        "blocking_reasons"
    ]


@pytest.mark.parametrize(
    "safe_value",
    [
        # The bench corpus records the link *class*, which must stay publishable.
        {"link_kind": "serial"},
        {"link_kind": "loopback"},
        # The device patterns must not fire on ordinary prose or status words.
        {"note": "COMPLETED without a device endpoint"},
        {"note": "the operator used a serial link class, not a port"},
        {"raw_logs_ref": "evidence/20260725-bench-arm-disarm.json"},
    ],
)
def test_publication_sanitizer_accepts_publishable_bench_values(
    safe_value: dict,
) -> None:
    case = _case("px4-positive-verified-avoidance")
    safe = copy.deepcopy(case)
    safe["bench_probe"] = safe_value
    safe = seal_action_feasibility_corpus_case(safe)

    verdict = verify_action_feasibility_corpus_case(safe)

    assert "corpus_case_publication_boundary_violated" not in verdict[
        "blocking_reasons"
    ]


def test_case_integrity_rejects_semantic_tampering() -> None:
    case = _case("px4-refusal-stale-telemetry")
    case["expected"]["feasibility_status"] = "verified_feasible"

    verdict = verify_action_feasibility_corpus_case(case)

    assert verdict["verification_status"] == "failed"
    assert "corpus_case_hash_mismatch" in verdict["blocking_reasons"]
    assert "corpus_case_feasibility_status_changed" in verdict[
        "blocking_reasons"
    ]
