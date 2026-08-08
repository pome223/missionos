from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

import pytest

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable


DISPATCH_REF = "missionos_dispatch:task_fixture:action_fixture"
REQUEST_PAYLOAD = {
    "task_id": "task_fixture",
    "bounded_action_ref": "bounded_action:fixture",
    "backend_target": "fixture_backend",
    "command": {"kind": "hold", "duration_seconds": 1},
}


def _table(path: Path) -> DispatchAuthorityTable:
    return DispatchAuthorityTable(path / "dispatch_authority_state.json")


def test_dispatch_ref_replays_existing_receipt_without_second_send_after_restart(
    tmp_path: Path,
) -> None:
    table = _table(tmp_path)
    claim = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
        correlation={"task_id": "task_fixture", "adk_node_id": "dispatch_node"},
    )

    assert claim["idempotency_status"] == "claimed"
    assert claim["send_permitted"] is True
    assert claim["attempt_ordinal"] == 1
    assert claim["receipt_present"] is False

    send_start = table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
    )
    assert send_start["idempotency_status"] == "send_started"
    assert send_start["send_permitted"] is True

    send_count = 1
    recorded = table.record_dispatch_receipt(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
        receipt={
            "dispatch_ref": DISPATCH_REF,
            "send_attempted": True,
            "ack_observed": False,
            "effect_observed": False,
            "verifier_passed": False,
            "completion_claimed": False,
            "physical_execution_invoked": False,
        },
    )
    assert recorded["idempotency_status"] == "receipt_recorded"
    assert recorded["send_permitted"] is False
    assert recorded["receipt_present"] is True

    restarted_table = _table(tmp_path)
    replay = restarted_table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
        correlation={"task_id": "task_fixture", "adk_node_id": "resumed_dispatch_node"},
    )

    assert send_count == 1
    assert replay["idempotency_status"] == "existing_receipt"
    assert replay["send_permitted"] is False
    assert replay["idempotent_replay"] is True
    assert replay["automatic_redispatch_performed"] is False
    receipt = replay["existing_receipt"]
    assert receipt["ack_observed"] is False
    assert receipt["effect_observed"] is False
    assert receipt["verifier_passed"] is False
    assert receipt["completion_claimed"] is False
    assert receipt["physical_execution_invoked"] is False


def test_unknown_send_outcome_blocks_retry_after_restart(tmp_path: Path) -> None:
    table = _table(tmp_path)
    claim = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    started = table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
    )
    assert started["send_permitted"] is True

    unknown = table.record_unknown_dispatch_outcome(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
        error_type="ConnectionResetError",
    )
    assert unknown["idempotency_status"] == "unknown_send_outcome"
    assert unknown["send_outcome_known"] is False
    assert unknown["safe_to_retry"] is False
    assert unknown["automatic_redispatch_performed"] is False

    replay = _table(tmp_path).claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    assert replay["idempotency_status"] == "unknown_send_outcome"
    assert replay["send_permitted"] is False
    assert replay["receipt_present"] is False
    assert replay["blocking_reasons"] == [
        "dispatch_outcome_unknown_do_not_retry"
    ]


def test_existing_receipt_blocks_send_in_new_python_process(tmp_path: Path) -> None:
    table = _table(tmp_path)
    claim = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
    )
    table.record_dispatch_receipt(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
        receipt={
            "dispatch_ref": DISPATCH_REF,
            "send_attempted": True,
            "ack_observed": False,
            "effect_observed": False,
        },
    )

    child_code = """
import json
from pathlib import Path
import sys
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable

state_path = Path(sys.argv[1])
dispatch_ref = sys.argv[2]
request_payload = json.loads(sys.argv[3])
result = DispatchAuthorityTable(state_path).claim_dispatch_ref(
    dispatch_ref=dispatch_ref,
    request_payload=request_payload,
)
print(json.dumps(result, sort_keys=True))
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(table.state_path),
            DISPATCH_REF,
            json.dumps(REQUEST_PAYLOAD, sort_keys=True),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    replay = json.loads(completed.stdout)

    assert replay["idempotency_status"] == "existing_receipt"
    assert replay["send_permitted"] is False
    assert replay["idempotent_replay"] is True
    assert replay["automatic_redispatch_performed"] is False


def test_repeated_send_start_authorizes_external_invocation_once(
    tmp_path: Path,
) -> None:
    table = _table(tmp_path)
    claim = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )

    first = table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
    )
    repeated = table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=claim["claim_id"],
    )

    assert first["send_permitted"] is True
    assert repeated["idempotency_status"] == "send_already_started"
    assert repeated["send_permitted"] is False
    assert repeated["blocking_reasons"] == [
        "dispatch_outcome_unknown_do_not_retry"
    ]


def test_same_dispatch_ref_with_changed_payload_fails_closed(tmp_path: Path) -> None:
    table = _table(tmp_path)
    first = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    assert first["send_permitted"] is True

    changed = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload={
            **REQUEST_PAYLOAD,
            "command": {"kind": "land"},
        },
    )

    assert changed["idempotency_status"] == "dispatch_ref_payload_mismatch"
    assert changed["send_permitted"] is False
    assert changed["blocking_reasons"] == ["dispatch_ref_payload_mismatch"]


def test_confirmed_pre_send_cancel_allows_same_payload_retry(tmp_path: Path) -> None:
    table = _table(tmp_path)
    first = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    cancelled = table.cancel_dispatch_before_send(
        dispatch_ref=DISPATCH_REF,
        claim_id=first["claim_id"],
        reason="fixture_validation_failed_before_sender_invocation",
    )
    assert cancelled["safe_to_retry"] is True
    assert cancelled["send_outcome_known"] is True

    retried = table.claim_dispatch_ref(
        dispatch_ref=DISPATCH_REF,
        request_payload=REQUEST_PAYLOAD,
    )
    assert retried["idempotency_status"] == "claimed"
    assert retried["send_permitted"] is True
    assert retried["attempt_ordinal"] == 2
    assert retried["claim_id"] != first["claim_id"]

    started = table.mark_dispatch_send_started(
        dispatch_ref=DISPATCH_REF,
        claim_id=retried["claim_id"],
    )
    assert started["send_permitted"] is True
    with pytest.raises(ValueError, match="dispatch_send_may_have_started"):
        table.cancel_dispatch_before_send(
            dispatch_ref=DISPATCH_REF,
            claim_id=retried["claim_id"],
            reason="too_late",
        )


def test_concurrent_dispatch_ref_claims_permit_one_sender(tmp_path: Path) -> None:
    state_path = tmp_path / "dispatch_authority_state.json"
    worker_count = 12
    barrier = threading.Barrier(worker_count)

    def claim(_index: int) -> dict[str, Any]:
        barrier.wait()
        return DispatchAuthorityTable(state_path).claim_dispatch_ref(
            dispatch_ref=DISPATCH_REF,
            request_payload=REQUEST_PAYLOAD,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(claim, range(worker_count)))

    permitted = [result for result in results if result["send_permitted"] is True]
    blocked = [result for result in results if result["send_permitted"] is False]
    assert len(permitted) == 1
    assert len(blocked) == worker_count - 1
    assert {result["idempotency_status"] for result in blocked} == {
        "unknown_send_outcome"
    }
    assert all(
        result["automatic_redispatch_performed"] is False for result in results
    )


def test_concurrent_process_claims_permit_one_sender(tmp_path: Path) -> None:
    state_path = tmp_path / "dispatch_authority_state.json"
    child_code = """
import json
from pathlib import Path
import sys
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable

result = DispatchAuthorityTable(Path(sys.argv[1])).claim_dispatch_ref(
    dispatch_ref=sys.argv[2],
    request_payload=json.loads(sys.argv[3]),
)
print(json.dumps(result, sort_keys=True))
"""
    command = [
        sys.executable,
        "-c",
        child_code,
        str(state_path),
        DISPATCH_REF,
        json.dumps(REQUEST_PAYLOAD, sort_keys=True),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))

    permitted = [result for result in results if result["send_permitted"] is True]
    blocked = [result for result in results if result["send_permitted"] is False]
    assert len(permitted) == 1
    assert len(blocked) == 3
    assert {result["idempotency_status"] for result in blocked} == {
        "unknown_send_outcome"
    }


def test_concurrent_process_dispatch_sequence_invokes_sender_once() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/smoke_atomic_dispatch.py"],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = json.loads(completed.stdout)

    assert result["verification_status"] == "passed"
    assert result["process_count"] == 8
    assert result["lock_scope"] == "same_host_shared_state_and_lock_files"
    assert result["dispatch_attempt_count"] == 1
    assert result["fixture_sender_invocation_count"] == 1
    assert result["external_transport_invoked"] is False
    assert result["physical_execution_invoked"] is False
    assert result["automatic_redispatch_performed"] is False
    assert result["receipt_recorded"] is True


def test_dispatch_authority_single_use_contract_remains_intact(
    tmp_path: Path,
) -> None:
    table = _table(tmp_path)
    authority_id = "authority_fixture"
    table.register_authority(
        {
            "dispatch_authority_id": authority_id,
            "dispatch_ref": DISPATCH_REF,
            "operator_approval_required": True,
            "automatic_dispatch_suppressed": True,
        },
        artifact_path="fixture/authority.json",
    )
    operator_approval = {
        "approval_id": "approval_fixture",
        "operator_approved": True,
        "automatic_dispatch_executed": False,
    }
    deterministic_gate = {
        "gate_result_id": "gate_fixture",
        "deterministic_gate_passed": True,
        "automatic_dispatch_executed": False,
    }

    accepted = table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval=operator_approval,
        deterministic_gate=deterministic_gate,
    )
    replay = table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval=operator_approval,
        deterministic_gate=deterministic_gate,
    )

    assert accepted["validation_status"] == "valid"
    assert accepted["dispatch_replay_detected"] is False
    assert replay["validation_status"] == "blocked"
    assert replay["dispatch_replay_detected"] is True
