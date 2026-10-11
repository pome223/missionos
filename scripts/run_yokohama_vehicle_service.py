#!/usr/bin/env python3
"""Single-use local vehicle execution service; no hardware or provisioning API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_execution_service import (  # noqa: E402
    admit_request, proposal_digest, validate_request,
)


def execute(manifest_path: Path, folder: Path, *, root: Path = REPO, runner=None) -> int:
    raw = manifest_path.read_bytes()
    manifest, native_bytes = admit_request(raw, root)
    if folder.resolve() != (manifest_path.parent / "run").resolve() or folder.exists():
        raise ValueError("Execution needs a fresh run directory beside the approval")
    job = manifest_path.parent / "vehicle-service"
    # Atomic single-use claim survives crashes and prevents replay/concurrency.
    job.mkdir(exist_ok=False)
    snapshot = job / "approved.json"
    snapshot.write_bytes(raw)
    proposal = manifest["proposal"]
    run_arguments = list(proposal["simulator_arguments"])
    if native_bytes is not None:
        native_snapshot = job / "native-service.json"
        native_snapshot.write_bytes(native_bytes)
        native_snapshot.chmod(0o400)
        run_arguments[run_arguments.index("--native-service-config") + 1] = str(native_snapshot)
    status = dict(
        schema="missionos.yokohama-vehicle-service.v1",
        proposal_id=proposal["proposal_id"], proposal_sha256=proposal_digest(proposal),
        approval_manifest_sha256=sha256(raw).hexdigest(), city_models=proposal["city_models"],
        pid=os.getpid(), physical_execution_invoked=False,
        native_service_config_sha256=(sha256(native_bytes).hexdigest()
                                      if native_bytes is not None else None),
    )

    def record(state, **fields):
        status.update(state=state, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
        temporary = job / "status.tmp"
        temporary.write_text(json.dumps(status, indent=2) + "\n")
        temporary.replace(job / "status.json")

    record("accepted")
    code = 1
    try:
        if runner is None:
            from scripts.yokohama_sitl import main as runner
        record("running")
        # This service process owns DecisionHost, its model lifecycle, and the
        # network-isolated aircraft worker containing CityDecisions. SIGINT
        # reaches the existing runner's finally/owned-resource cleanup.
        code = runner([
            *run_arguments, "--approve-sitl",
            "--approval-manifest", str(snapshot), "--output-dir", str(folder),
        ])
        return code
    except KeyboardInterrupt:
        code = 130
        raise
    except BaseException as exc:
        status["error_type"] = type(exc).__name__
        raise
    finally:
        result = folder / "result.json"
        record(
            "finished", exit_code=code,
            result_sha256=sha256(result.read_bytes()).hexdigest() if result.is_file() else None,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--approve-sitl", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    if args.validate_only:
        manifest = validate_request(args.approval_manifest.read_bytes(), REPO)
        print(json.dumps(dict(status="valid", proposal_sha256=proposal_digest(manifest["proposal"]))))
        return 0
    if not args.approve_sitl or args.output_dir is None:
        parser.error("Execution requires --approve-sitl and --output-dir")
    return execute(args.approval_manifest, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
