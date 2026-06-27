#!/usr/bin/env python3
"""Opt-in Flight Readiness Package smoke over source-backed Digital Twin SITL."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from scripts import smoke_source_backed_world_bound_sitl_e2e
from src.runtime.flight_readiness_package import (
    FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION,
    build_flight_readiness_package_from_source_backed_e2e_summary,
    flight_readiness_package_ref,
)


OPT_IN_ENV = "RUN_FLIGHT_READINESS_PACKAGE_SMOKE"
NOW = datetime(2026, 5, 8, 2, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run Flight Readiness Package smoke.")


def run_smoke() -> dict[str, object]:
    _require_opt_in()
    os.environ[smoke_source_backed_world_bound_sitl_e2e.OPT_IN_ENV] = "1"
    e2e_summary = smoke_source_backed_world_bound_sitl_e2e.run_smoke()
    package = build_flight_readiness_package_from_source_backed_e2e_summary(
        e2e_summary,
        now=NOW,
    )
    summary = {
        "schema_version": package.schema_version,
        "schema_version_expected": FLIGHT_READINESS_PACKAGE_SCHEMA_VERSION,
        "flight_readiness_package_ref": flight_readiness_package_ref(package),
        "readiness_status": package.readiness_status,
        "execution_result_ref": package.execution_result_ref,
        "source_backed_inputs_summary": package.source_backed_inputs_summary,
        "mission_upload_observed": package.mission_upload_observed,
        "mission_ack_observed": package.mission_ack_observed,
        "heartbeat_observed": package.heartbeat_observed,
        "flight_telemetry_observed": package.flight_telemetry_observed,
        "payload_release_status": package.payload_release_status,
        "dropoff_verification_status": package.dropoff_verification_status,
        "blocked_reasons": list(package.blocked_reasons),
        "warning_reasons": list(package.warning_reasons),
        "operator_checklist": list(package.operator_checklist),
        "hardware_target_allowed": package.hardware_target_allowed,
        "physical_execution_invoked": package.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            package.approval_free_stronger_execution_allowed
        ),
        "package_hash_equals_sha256": package.package_hash == package.sha256,
        "e2e_execution_status": e2e_summary["execution_status"],
        "e2e_terrain_sampling_mode": e2e_summary["terrain_sampling_mode"],
        "e2e_terrain_provider_response_status": e2e_summary[
            "terrain_provider_response_status"
        ],
        "e2e_weather_provider_response_status": e2e_summary[
            "weather_provider_response_status"
        ],
    }
    assert summary["schema_version"] == summary["schema_version_expected"]
    assert summary["readiness_status"] == "ready_for_human_hardware_review"
    assert summary["mission_upload_observed"] is True
    assert summary["mission_ack_observed"] is True
    assert summary["heartbeat_observed"] is True
    assert summary["flight_telemetry_observed"] is True
    assert summary["payload_release_status"] == "pending"
    assert summary["dropoff_verification_status"] == "pending"
    assert "payload_release_pending" in summary["warning_reasons"]
    assert "dropoff_verification_pending" in summary["warning_reasons"]
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["approval_free_stronger_execution_allowed"] is False
    assert summary["package_hash_equals_sha256"] is True
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(
        "SMOKE_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
