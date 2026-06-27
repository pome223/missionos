"""Golden mock simulator adapter corpus for #211."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.runtime.mock_simulator_adapter import build_mock_simulator_adapter_smoke_chain


CORPUS_NOW = datetime(2026, 4, 30, 17, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class GoldenMockSimulatorCase:
    case_id: str
    artifacts: dict[str, Any]
    expected_gate_passed: bool
    expected_blocked_reasons: tuple[str, ...]


def _nominal_telemetry() -> dict[str, Any]:
    return {
        "timestamp": CORPUS_NOW.isoformat(),
        "signals": {
            "battery": "ok",
            "localization": "ok",
            "comms": "ok",
            "safety": "nominal",
        },
    }


def build_golden_mock_simulator_cases() -> list[GoldenMockSimulatorCase]:
    stale = _nominal_telemetry()
    stale["timestamp"] = (CORPUS_NOW - timedelta(seconds=120)).isoformat()

    missing = {
        "timestamp": CORPUS_NOW.isoformat(),
        "signals": {
            "battery": "ok",
            "localization": "ok",
            "safety": "nominal",
        },
    }

    unsafe = _nominal_telemetry()
    unsafe["signals"] = {
        **unsafe["signals"],
        "safety": "unsafe",
    }

    return [
        GoldenMockSimulatorCase(
            case_id="mock_simulator_nominal_pass",
            artifacts=build_mock_simulator_adapter_smoke_chain(now=CORPUS_NOW),
            expected_gate_passed=True,
            expected_blocked_reasons=(),
        ),
        GoldenMockSimulatorCase(
            case_id="mock_simulator_stale_telemetry_blocks",
            artifacts=build_mock_simulator_adapter_smoke_chain(
                telemetry_payload=stale,
                now=CORPUS_NOW,
            ),
            expected_gate_passed=False,
            expected_blocked_reasons=(
                "mock_simulator_scorecard_failed",
                "mock_simulator_stale_telemetry",
                "mock_simulator_governor_blocked",
                "mock_simulator_review_blocked",
            ),
        ),
        GoldenMockSimulatorCase(
            case_id="mock_simulator_missing_telemetry_blocks",
            artifacts=build_mock_simulator_adapter_smoke_chain(
                telemetry_payload=missing,
                now=CORPUS_NOW,
            ),
            expected_gate_passed=False,
            expected_blocked_reasons=(
                "mock_simulator_scorecard_failed",
                "mock_simulator_malformed_telemetry",
                "mock_simulator_governor_blocked",
                "mock_simulator_review_blocked",
            ),
        ),
        GoldenMockSimulatorCase(
            case_id="mock_simulator_unsafe_telemetry_blocks",
            artifacts=build_mock_simulator_adapter_smoke_chain(
                telemetry_payload=unsafe,
                now=CORPUS_NOW,
            ),
            expected_gate_passed=False,
            expected_blocked_reasons=(
                "mock_simulator_scorecard_failed",
                "mock_simulator_unsafe_telemetry",
                "mock_simulator_governor_blocked",
                "mock_simulator_review_blocked",
            ),
        ),
        GoldenMockSimulatorCase(
            case_id="mock_simulator_replay_hash_mismatch_blocks",
            artifacts=build_mock_simulator_adapter_smoke_chain(
                break_replay_hash=True,
                now=CORPUS_NOW,
            ),
            expected_gate_passed=False,
            expected_blocked_reasons=(
                "mock_simulator_scorecard_failed",
                "mock_simulator_replay_not_deterministic",
                "mock_simulator_review_blocked",
            ),
        ),
    ]


def golden_mock_simulator_case_ids() -> set[str]:
    return {case.case_id for case in build_golden_mock_simulator_cases()}
