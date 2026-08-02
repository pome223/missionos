#!/usr/bin/env python3
"""Opt-in live PX4/Gazebo -> Nav2/TurtleBot3 parent mission smoke.

The parent Mission Contract and both child contracts are frozen before either
simulator runner is invoked. The coordinator calls the already-existing child
smokes in order and consumes only their stored predicate evaluations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from missionos_core import (
    FrozenMissionContract,
    FrozenParentMissionContract,
    ParentMissionApprovalBinding,
    QuantificationScope,
    QuantificationScopeKind,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
)
from scripts.smoke_px4_gazebo_sitl_e2e_delivery import (
    build_px4_gazebo_sitl_e2e_delivery_contract,
    build_px4_gazebo_sitl_e2e_mission_contract,
)
from scripts.smoke_ros2_nav2_turtlebot3_bounded_dispatch import (
    build_nav2_turtlebot3_bounded_goal,
    build_nav2_turtlebot3_bounded_mission_contract,
)
from src.runtime.parent_mission_coordinator import (
    run_parent_mission_coordinator,
)


OPT_IN_ENV = "RUN_MISSIONOS_PARENT_MISSION_PX4_NAV2_LIVE_SMOKE"
ARTIFACT_ROOT_ENV = "MISSIONOS_PARENT_MISSION_ARTIFACT_ROOT"
OPERATOR_APPROVAL_REF_ENV = "MISSIONOS_PARENT_MISSION_OPERATOR_APPROVAL_REF"
ROOT_DIR = Path(__file__).resolve().parents[1]
PX4_STAGE_REF = "px4_gazebo_delivery"
NAV2_STAGE_REF = "nav2_turtlebot3_bounded_goal"
SHARED_DESCRIPTOR = {
    "schema_version": "missionos_parent_shared_target_descriptor.v1",
    "descriptor_id": "issue164:px4-nav2-separate-simulator-worlds",
    "mission_intent": (
        "Run two approved simulator stages under one authority and evidence "
        "lineage."
    ),
    "physical_identity_asserted": False,
    "shared_world_asserted": False,
    "relationship": "ordered_governance_demonstration_only",
}

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_root() -> Path:
    root = Path(
        os.environ.get(
            ARTIFACT_ROOT_ENV,
            "artifacts/parent_mission_px4_nav2",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = _artifact_root()
    candidate = root / f"parent_mission_px4_nav2_{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"parent_mission_px4_nav2_{stamp}_{suffix}"
    candidate.mkdir()
    return candidate


def build_issue164_parent_mission_bundle(
    *,
    operator_approval_ref: str,
) -> tuple[
    FrozenParentMissionContract,
    ParentMissionApprovalBinding,
    dict[str, FrozenMissionContract],
]:
    """Freeze the exact existing PX4 and Nav2 child contracts into one parent."""

    delivery_contract = build_px4_gazebo_sitl_e2e_delivery_contract()
    px4_contract = build_px4_gazebo_sitl_e2e_mission_contract(
        delivery_contract
    )
    nav2_goal = build_nav2_turtlebot3_bounded_goal()
    nav2_contract = build_nav2_turtlebot3_bounded_mission_contract(nav2_goal)
    children = {
        PX4_STAGE_REF: px4_contract,
        NAV2_STAGE_REF: nav2_contract,
    }
    parent = FrozenParentMissionContract(
        parent_mission_id="issue164:px4-gazebo-then-nav2-turtlebot3",
        parent_mission_version="2026-07-30",
        shared_target_descriptor_sha256=canonical_sha256(SHARED_DESCRIPTOR),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "The stages run in separate simulator worlds. This parent "
                "proves ordered authority and evidence lineage only."
            ),
        ),
        stages=(
            build_parent_mission_stage_binding(
                stage_index=1,
                stage_ref=PX4_STAGE_REF,
                executor_ref="sim:px4-gazebo-sitl-delivery",
                child_contract=px4_contract,
            ),
            build_parent_mission_stage_binding(
                stage_index=2,
                stage_ref=NAV2_STAGE_REF,
                executor_ref="sim:nav2-turtlebot3-bounded-goal",
                child_contract=nav2_contract,
            ),
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref=operator_approval_ref,
        authority_bundle_ref=(
            "catalog:issue164:px4-nav2-simulator-composition:v1"
        ),
    )
    return parent, approval, children


def parent_mission_job_status_task_payload(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Project one live record into the task artifact shape used by job-status."""

    coordinator = record.get("coordinator_record")
    coordinator = coordinator if isinstance(coordinator, dict) else {}
    return {
        "task_id": str(
            coordinator.get("parent_mission_id")
            or "issue164:parent-mission"
        ),
        "kind": "parent_mission_execution",
        "status": (
            "completed"
            if coordinator.get("coordinator_status") == "stages_satisfied"
            else "blocked"
        ),
        "artifacts": {
            "missionos_parent_mission_run_record": record,
        },
    }


def _find_created_run(
    *,
    root: Path,
    pattern: str,
    before: set[Path],
) -> Path:
    created = sorted(
        {
            path
            for path in set(root.glob(pattern)) - before
            if path.is_dir()
        },
        key=lambda path: path.stat().st_mtime,
    )
    if len(created) != 1:
        raise RuntimeError(
            f"expected one new child artifact for {pattern}, got {len(created)}"
        )
    return created[0]


def _artifact_digests(run_dir: Path) -> dict[str, str]:
    names = (
        "mission_contract.json",
        "mission_contract_predicate_content.json",
        "mission_contract_predicate_evaluation.json",
        "summary.json",
    )
    return {
        name: _sha256_path(run_dir / name)
        for name in names
        if (run_dir / name).is_file()
    }


def _run_child(
    *,
    stage_ref: str,
    command: Sequence[str],
    env: Mapping[str, str],
    artifact_root: Path,
    artifact_pattern: str,
    expected_contract: FrozenMissionContract,
    stage_audits: dict[str, dict[str, Any]],
    subprocess_runner: SubprocessRunner,
) -> dict[str, Any]:
    before = set(artifact_root.glob(artifact_pattern))
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    audit: dict[str, Any] = {
        "stage_ref": stage_ref,
        "subprocess_started_at": started_at,
        "subprocess_completed_at": None,
        "duration_seconds": None,
        "returncode": None,
        "child_contract_matched_prefrozen_material": False,
        "child_artifact_digests": {},
        "failure": None,
        "failure_reason": None,
    }
    stage_audits[stage_ref] = audit
    merged_env = {**os.environ, **dict(env)}
    try:
        completed = subprocess_runner(
            list(command),
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
            timeout=900,
            env=merged_env,
            check=False,
        )
        audit["returncode"] = completed.returncode
        (artifact_root / f"{stage_ref}.stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (artifact_root / f"{stage_ref}.stderr.log").write_text(
            completed.stderr,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"{stage_ref} subprocess returned non-zero")
        child_run = _find_created_run(
            root=artifact_root,
            pattern=artifact_pattern,
            before=before,
        )
        contract_material = json.loads(
            (child_run / "mission_contract.json").read_text(encoding="utf-8")
        )
        if contract_material != expected_contract.to_material():
            raise RuntimeError(
                f"{stage_ref} emitted a different child contract"
            )
        audit["child_contract_matched_prefrozen_material"] = True
        audit["child_contract_sha256"] = canonical_sha256(contract_material)
        evaluation = json.loads(
            (
                child_run / "mission_contract_predicate_evaluation.json"
            ).read_text(encoding="utf-8")
        )
        audit["child_artifact_digests"] = _artifact_digests(child_run)
        audit["predicate_evaluation_sha256"] = canonical_sha256(evaluation)
        return evaluation
    except Exception as exc:
        audit["failure"] = type(exc).__name__
        audit["failure_reason"] = str(exc)
        raise
    finally:
        audit["subprocess_completed_at"] = _utc_now()
        audit["duration_seconds"] = round(
            time.monotonic() - started_monotonic,
            6,
        )


def run_issue164_live_parent_mission(
    *,
    run_dir: Path,
    operator_approval_ref: str,
    subprocess_runner: SubprocessRunner = subprocess.run,
) -> dict[str, Any]:
    """Run both existing simulator smokes through the frozen parent."""

    parent, approval, children = build_issue164_parent_mission_bundle(
        operator_approval_ref=operator_approval_ref
    )
    frozen_at = _utc_now()
    _write_json(run_dir / "shared_target_descriptor.json", SHARED_DESCRIPTOR)
    _write_json(run_dir / "parent_mission_contract.json", parent.to_material())
    _write_json(run_dir / "parent_mission_approval.json", approval.to_material())
    for stage_ref, contract in children.items():
        _write_json(
            run_dir / "child_contracts" / f"{stage_ref}.json",
            contract.to_material(),
        )

    common_pythonpath = os.pathsep.join(
        (
            str(ROOT_DIR),
            str(ROOT_DIR / "packages/missionos-core/src"),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    px4_root = run_dir / "px4"
    nav2_root = run_dir / "nav2"
    px4_root.mkdir()
    nav2_root.mkdir()
    stage_audits: dict[str, dict[str, Any]] = {}

    def run_px4() -> dict[str, Any]:
        return _run_child(
            stage_ref=PX4_STAGE_REF,
            command=(
                sys.executable,
                "scripts/smoke_px4_gazebo_sitl_e2e_delivery.py",
            ),
            env={
                "RUN_PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE": "1",
                "PX4_GAZEBO_SITL_E2E_ARTIFACT_ROOT": str(px4_root),
                "PYTHONPATH": common_pythonpath,
            },
            artifact_root=px4_root,
            artifact_pattern="sitl_e2e_delivery_*",
            expected_contract=children[PX4_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess_runner,
        )

    def run_nav2() -> dict[str, Any]:
        return _run_child(
            stage_ref=NAV2_STAGE_REF,
            command=(
                "bash",
                "scripts/smoke_ros2_nav2_turtlebot3_mission_contract_docker.sh",
            ),
            env={
                "ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT": str(nav2_root),
                "PYTHONPATH": common_pythonpath,
            },
            artifact_root=nav2_root,
            artifact_pattern="nav2_turtlebot3_*",
            expected_contract=children[NAV2_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess_runner,
        )

    coordinator_record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            PX4_STAGE_REF: run_px4,
            NAV2_STAGE_REF: run_nav2,
        },
    )
    record = {
        "schema_version": "missionos_issue164_live_parent_mission.v1",
        "parent_contract_frozen_at": frozen_at,
        "parent_mission_sha256": parent.parent_mission_sha256,
        "approval_binding_sha256": approval.approval_binding_sha256,
        "shared_target_descriptor_sha256": canonical_sha256(
            SHARED_DESCRIPTOR
        ),
        "execution_mode": "sequential",
        "simulator_overlap_tested": False,
        "stage_clock_bindings": {
            stage.stage_ref: {
                "observation_clock_bindings": [
                    binding.to_material()
                    for binding in stage.observation_clock_bindings
                ],
                "evaluation_clock_domain_ref": (
                    stage.evaluation_clock_domain_ref
                ),
            }
            for stage in parent.stages
        },
        "stage_audits": stage_audits,
        "coordinator_record": coordinator_record,
        "mission_completion_claimed": False,
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "This record can establish ordered simulator-stage authority and "
            "evidence lineage only. It does not establish one shared physical "
            "world, target identity continuity, parent mission completion, "
            "concurrent simulator resource safety, or physical execution."
        ),
    }
    _write_json(run_dir / "parent_mission_run_record.json", record)
    _write_json(
        run_dir / "job_status_task_payload.json",
        parent_mission_job_status_task_payload(record),
    )
    return record


def main() -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        print(
            json.dumps(
                {
                    "smoke": "parent_mission_px4_nav2_live",
                    "ran": False,
                    "reason": f"Set {OPT_IN_ENV}=1 to run both simulators.",
                    "dispatch_request_sent": False,
                    "mission_completion_claimed": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    operator_approval_ref = os.environ.get(
        OPERATOR_APPROVAL_REF_ENV,
        "",
    ).strip()
    if not operator_approval_ref:
        raise SystemExit(
            f"Set {OPERATOR_APPROVAL_REF_ENV} to the pre-existing simulator "
            "approval reference."
        )
    run_dir = _new_run_dir()
    record = run_issue164_live_parent_mission(
        run_dir=run_dir,
        operator_approval_ref=operator_approval_ref,
    )
    print(json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True))
    if (
        record["coordinator_record"]["coordinator_status"]
        != "stages_satisfied"
    ):
        return 2
    if record["coordinator_record"]["stages_satisfied"] != 2:
        return 2
    if record["mission_completion_claimed"] is not False:
        return 2
    if record["physical_execution_invoked"] is not False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
