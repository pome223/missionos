#!/usr/bin/env python3
"""Build operational_envelope.v1 from historical artifact JSON files."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.runtime.operational_envelope import DEFAULT_MIN_SIM_RUN_COUNT
from src.runtime.operational_envelope_source_ingestion import (
    build_operational_envelope_from_artifacts,
)


def _backend_context_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "backend_type": args.backend_type,
        "image_version": args.image_version,
        "sim_version": args.sim_version,
        "sdf_hash": args.sdf_hash,
        "applicator_chain_refs": args.applicator_chain_ref or [],
        "verifier_version": args.verifier_version,
        "audit_script_version": args.audit_script_version,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an operational envelope from historical Mission Designer artifacts."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--min-sim-run-count", type=int, default=DEFAULT_MIN_SIM_RUN_COUNT)
    parser.add_argument("--mission-contract-ref", default="")
    parser.add_argument("--task-graph-ref", default="")
    parser.add_argument("--source-backend-type", default="")
    parser.add_argument("--backend-type", default="")
    parser.add_argument("--image-version", default="")
    parser.add_argument("--sim-version", default="")
    parser.add_argument("--sdf-hash", default="")
    parser.add_argument("--applicator-chain-ref", action="append")
    parser.add_argument("--verifier-version", default="")
    parser.add_argument("--audit-script-version", default="")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits"),
    )
    args = parser.parse_args()
    context_overrides = {
        "mission_contract_ref": args.mission_contract_ref,
        "task_graph_ref": args.task_graph_ref,
        "source_backend_type": args.source_backend_type,
        "backend_context": _backend_context_from_args(args),
    }
    artifact = build_operational_envelope_from_artifacts(
        artifact_paths=args.paths,
        min_sim_run_count=args.min_sim_run_count,
        context_overrides=context_overrides,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir / f"operational_envelope_source_ingestion_{stamp}"
    output_path = output_dir / "operational_envelope_source_ingestion.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact["output_path"] = str(output_path)
    artifact["observed_at"] = datetime.now(timezone.utc).isoformat()
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if artifact["ingestion_status"] == "envelope_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
