#!/usr/bin/env python3
"""Smoke Section 9.2 operational-envelope consumption without physical authority."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from scripts.smoke_operational_envelope import _source_run
from src.runtime.operational_envelope import (
    build_physical_run_operational_envelope_consumption,
    build_operational_envelope,
)


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def main() -> int:
    envelope = build_operational_envelope(
        source_runs=[_source_run(index) for index in range(10)],
        now=NOW,
    )
    active_consumption = build_physical_run_operational_envelope_consumption(
        envelope=envelope,
        physical_run_ref="physical_run:operational_envelope_smoke",
        backend_context=envelope["backend_context"],
        now=NOW,
    )
    expired_context = {
        **envelope["backend_context"],
        "image_version": "px4-gazebo-smoke@sha256:new",
    }
    expired_consumption = build_physical_run_operational_envelope_consumption(
        envelope=envelope,
        physical_run_ref="physical_run:operational_envelope_expired_smoke",
        backend_context=expired_context,
        now=NOW,
    )
    if active_consumption["consumption_status"] != "parameter_knowledge_consumed":
        raise RuntimeError("active envelope was not consumed as parameter knowledge")
    if active_consumption["causal_verification_transferred"] is not False:
        raise RuntimeError("active consumption transferred causal verification")
    if active_consumption["physical_form1_required"] is not True:
        raise RuntimeError("active consumption did not require physical Form 1")
    if active_consumption["physical_execution_invoked"] is not False:
        raise RuntimeError("active consumption invoked physical execution")
    if expired_consumption["consumption_status"] != "blocked":
        raise RuntimeError("expired envelope context was not blocked")
    if (
        expired_consumption["envelope_status_at_run"]
        != "expired_due_to_image_version_change"
    ):
        raise RuntimeError("expired envelope did not report image-version change")

    artifact = {
        "schema_version": "physical_run_operational_envelope_consumption_smoke.v1",
        "active_consumption": active_consumption,
        "expired_consumption": expired_consumption,
    }
    output_dir = Path("output/mission_designer_behavior_delta_audits")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "physical_run_operational_envelope_consumption_smoke.json"
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
