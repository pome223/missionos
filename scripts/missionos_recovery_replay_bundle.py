#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_route.replay_bundle import (
    build_anonymized_recovery_replay_bundle,
    verify_anonymized_recovery_replay_bundle,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export or verify a publication-safe MissionOS Recovery replay bundle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="sanitize one task JSON record")
    export.add_argument("--task-json", type=Path, required=True)
    export.add_argument("--public-run-ref", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--max-telemetry-samples", type=int, default=240)

    verify = subparsers.add_parser("verify", help="verify a replay bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "export":
        task = _read_json(args.task_json)
        bundle = build_anonymized_recovery_replay_bundle(
            task,
            public_run_ref=args.public_run_ref,
            max_telemetry_samples=max(1, args.max_telemetry_samples),
        )
        _write_json(args.output, bundle)
        print(
            json.dumps(
                {
                    "status": "exported",
                    "schema_version": bundle["schema_version"],
                    "public_run_ref": bundle["public_run_ref"],
                    "recovery_epoch_count": len(bundle["recovery_epochs"]),
                    "telemetry_sample_count": bundle["telemetry"]["sample_count"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    bundle = _read_json(args.bundle)
    verdict = verify_anonymized_recovery_replay_bundle(bundle)
    if args.output:
        _write_json(args.output, verdict)
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["verification_status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
