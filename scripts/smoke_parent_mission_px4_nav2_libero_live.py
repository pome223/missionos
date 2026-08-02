#!/usr/bin/env python3
"""Opt-in PX4 -> Nav2 -> GR00T/LIBERO live parent-mission smoke.

The parent contract, one human approval binding, all three child contracts,
and the LIBERO run/episode identities are frozen before the first child runner
starts.  The coordinator invokes each live child exactly once and consumes the
predicate evaluation created by that invocation; it never imports an older
run artifact as a stage result.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

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
from scripts.run_libero_panda_instrumented_live import (
    build_runner_configuration,
)
from scripts.smoke_parent_mission_px4_nav2_live import (
    NAV2_STAGE_REF,
    PX4_STAGE_REF,
    _run_child,
    parent_mission_job_status_task_payload,
)
from scripts.smoke_px4_gazebo_sitl_e2e_delivery import (
    build_px4_gazebo_sitl_e2e_delivery_contract,
    build_px4_gazebo_sitl_e2e_mission_contract,
)
from scripts.smoke_ros2_nav2_turtlebot3_bounded_dispatch import (
    build_nav2_turtlebot3_bounded_goal,
    build_nav2_turtlebot3_bounded_mission_contract,
)
from src.runtime.libero_panda_predicate_package import (
    build_libero_panda_replay_contract,
)
from src.runtime.parent_mission_coordinator import (
    run_parent_mission_coordinator,
)


OPT_IN_ENV = "RUN_MISSIONOS_PARENT_MISSION_PX4_NAV2_LIBERO_LIVE_SMOKE"
ARTIFACT_ROOT_ENV = "MISSIONOS_PARENT_MISSION_3_STAGE_ARTIFACT_ROOT"
OPERATOR_APPROVAL_REF_ENV = "MISSIONOS_PARENT_MISSION_OPERATOR_APPROVAL_REF"
LIBERO_STAGE_COMMAND_ENV = "MISSIONOS_LIBERO_LIVE_STAGE_COMMAND_JSON"
ROOT_DIR = Path(__file__).resolve().parents[1]
LIBERO_STAGE_REF = "groot_libero_panda"
SHARED_DESCRIPTOR = {
    "schema_version": "missionos_parent_shared_target_descriptor.v1",
    "descriptor_id": "issue184:px4-nav2-libero-separate-simulator-worlds",
    "mission_intent": (
        "Run three approved simulator stages under one parent authority and "
        "evidence lineage."
    ),
    "simulation_world_count": 3,
    "physical_identity_asserted": False,
    "shared_world_asserted": False,
    "relationship": "ordered_governance_demonstration_only",
}


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
            "artifacts/parent_mission_px4_nav2_libero",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_run_dir(parent_run_identity: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_identity = parent_run_identity.rsplit(":", maxsplit=1)[-1]
    run_dir = _artifact_root() / f"parent_mission_3_stage_{stamp}_{safe_identity}"
    run_dir.mkdir()
    return run_dir


def build_goal_a_parent_mission_bundle(
    *,
    parent_run_identity: str,
    operator_approval_ref: str,
) -> tuple[
    FrozenParentMissionContract,
    ParentMissionApprovalBinding,
    dict[str, FrozenMissionContract],
    str,
]:
    """Freeze the three existing concrete packages into one live parent."""

    delivery_contract = build_px4_gazebo_sitl_e2e_delivery_contract()
    px4_contract = build_px4_gazebo_sitl_e2e_mission_contract(
        delivery_contract
    )
    nav2_goal = build_nav2_turtlebot3_bounded_goal()
    nav2_contract = build_nav2_turtlebot3_bounded_mission_contract(nav2_goal)
    libero_episode_identity = f"{parent_run_identity}:libero-episode-1"
    libero_contract = build_libero_panda_replay_contract(
        contract_id=f"libero-panda-contract:{libero_episode_identity}",
        contract_version="v1",
        runner_configuration=build_runner_configuration(),
        run_identity=parent_run_identity,
        episode_identity=libero_episode_identity,
        maximum_observation_age_seconds=30.0,
    )
    children = {
        PX4_STAGE_REF: px4_contract,
        NAV2_STAGE_REF: nav2_contract,
        LIBERO_STAGE_REF: libero_contract,
    }
    parent = FrozenParentMissionContract(
        parent_mission_id=parent_run_identity,
        parent_mission_version="2026-08-01",
        shared_target_descriptor_sha256=canonical_sha256(SHARED_DESCRIPTOR),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "The stages run in three separate simulator worlds. The "
                "parent proves ordered authority and evidence lineage only."
            ),
        ),
        stages=tuple(
            build_parent_mission_stage_binding(
                stage_index=index,
                stage_ref=stage_ref,
                executor_ref=executor_ref,
                child_contract=children[stage_ref],
            )
            for index, (stage_ref, executor_ref) in enumerate(
                (
                    (PX4_STAGE_REF, "sim:px4-gazebo-sitl-delivery"),
                    (NAV2_STAGE_REF, "sim:nav2-turtlebot3-bounded-goal"),
                    (LIBERO_STAGE_REF, "vla:groot-n17-libero-panda"),
                ),
                start=1,
            )
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref=operator_approval_ref,
        authority_bundle_ref="catalog:issue184:three-live-stages:v1",
    )
    return parent, approval, children, libero_episode_identity


def _run_libero_child(
    *,
    command: Sequence[str],
    run_dir: Path,
    parent_run_identity: str,
    episode_identity: str,
    expected_contract: FrozenMissionContract,
    stage_audits: dict[str, dict[str, Any]],
    subprocess_runner,
) -> dict[str, Any]:
    if not command or any(not str(item).strip() for item in command):
        raise RuntimeError("LIBERO live stage command is invalid")
    output = run_dir / "libero" / "live_result.json"
    output.parent.mkdir()
    if output.exists():
        raise RuntimeError("LIBERO live result exists before stage invocation")
    started_monotonic = time.monotonic()
    audit: dict[str, Any] = {
        "stage_ref": LIBERO_STAGE_REF,
        "subprocess_started_at": _utc_now(),
        "subprocess_completed_at": None,
        "duration_seconds": None,
        "returncode": None,
        "child_contract_matched_prefrozen_material": False,
        "child_artifact_digests": {},
        "command_sha256": canonical_sha256({"argv": list(command)}),
        "result_created_by_this_invocation": False,
        "failure": None,
        "failure_reason": None,
    }
    stage_audits[LIBERO_STAGE_REF] = audit
    env = {
        **os.environ,
        "MISSIONOS_PARENT_RUN_IDENTITY": parent_run_identity,
        "MISSIONOS_LIBERO_EPISODE_IDENTITY": episode_identity,
        "MISSIONOS_LIBERO_RESULT_PATH": str(output),
        "MISSIONOS_EXPECTED_LIBERO_CONTRACT_SHA256": (
            expected_contract.contract_sha256
        ),
    }
    try:
        completed = subprocess_runner(
            list(command),
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
            timeout=1200,
            env=env,
            check=False,
        )
        audit["returncode"] = completed.returncode
        (output.parent / "stage.stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (output.parent / "stage.stderr.log").write_text(
            completed.stderr,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError("LIBERO live stage returned non-zero")
        if not output.is_file():
            raise RuntimeError("LIBERO live stage did not create its result")
        audit["result_created_by_this_invocation"] = True
        report = json.loads(output.read_text(encoding="utf-8"))
        episode = report.get("instrumented_episode")
        if not isinstance(episode, dict):
            raise RuntimeError("LIBERO instrumented episode is missing")
        if episode.get("run_identity") != parent_run_identity:
            raise RuntimeError("LIBERO parent run identity mismatch")
        if episode.get("episode_identity") != episode_identity:
            raise RuntimeError("LIBERO episode identity mismatch")
        if episode.get("contract_sha256") != expected_contract.contract_sha256:
            raise RuntimeError("LIBERO emitted a different child contract")
        evaluation = episode.get("predicate_evaluation")
        if not isinstance(evaluation, dict):
            raise RuntimeError("LIBERO predicate evaluation is missing")
        audit["child_contract_matched_prefrozen_material"] = True
        audit["child_contract_sha256"] = episode["contract_sha256"]
        audit["predicate_evaluation_sha256"] = canonical_sha256(evaluation)
        audit["child_artifact_digests"] = {
            "live_result.json": _sha256_path(output),
        }
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


def run_goal_a_live_parent_mission(
    *,
    run_dir: Path,
    parent_run_identity: str,
    operator_approval_ref: str,
    libero_stage_command: Sequence[str],
    subprocess_runner=subprocess.run,
) -> dict[str, Any]:
    """Run all three live stages inside one coordinator invocation."""

    parent, approval, children, libero_episode_identity = (
        build_goal_a_parent_mission_bundle(
            parent_run_identity=parent_run_identity,
            operator_approval_ref=operator_approval_ref,
        )
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

    def run_libero() -> dict[str, Any]:
        return _run_libero_child(
            command=libero_stage_command,
            run_dir=run_dir,
            parent_run_identity=parent_run_identity,
            episode_identity=libero_episode_identity,
            expected_contract=children[LIBERO_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess_runner,
        )

    coordinator_record = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={
            PX4_STAGE_REF: run_px4,
            NAV2_STAGE_REF: run_nav2,
            LIBERO_STAGE_REF: run_libero,
        },
    )
    record = {
        "schema_version": "missionos_goal_a_three_live_stages.v1",
        "parent_run_identity": parent_run_identity,
        "parent_contract_frozen_at": frozen_at,
        "parent_mission_sha256": parent.parent_mission_sha256,
        "approval_binding_sha256": approval.approval_binding_sha256,
        "shared_target_descriptor_sha256": canonical_sha256(SHARED_DESCRIPTOR),
        "execution_mode": "sequential",
        "simulator_overlap_tested": False,
        "stage_audits": stage_audits,
        "coordinator_record": coordinator_record,
        "missionos_parent_coordinator_in_live_loop": True,
        "mission_completion_claimed": False,
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "This record establishes one live parent coordinator invocation "
            "with three ordered simulator-scoped child results. It does not "
            "establish one shared world, target identity continuity, parent "
            "mission completion, physical safety, or physical execution."
        ),
    }
    _write_json(run_dir / "parent_mission_run_record.json", record)
    _write_json(
        run_dir / "job_status_task_payload.json",
        parent_mission_job_status_task_payload(record),
    )
    return record


def _command_from_environment() -> tuple[str, ...]:
    raw = os.environ.get(LIBERO_STAGE_COMMAND_ENV, "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{LIBERO_STAGE_COMMAND_ENV} must be a JSON array") from exc
    if not isinstance(parsed, list) or not parsed:
        raise SystemExit(f"{LIBERO_STAGE_COMMAND_ENV} must be a non-empty JSON array")
    command = tuple(str(item) for item in parsed)
    if any(not item.strip() for item in command):
        raise SystemExit(f"{LIBERO_STAGE_COMMAND_ENV} contains an empty argument")
    return command


def main() -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        print(
            json.dumps(
                {
                    "smoke": "parent_mission_px4_nav2_libero_live",
                    "ran": False,
                    "reason": f"Set {OPT_IN_ENV}=1 to run all three stages.",
                    "mission_completion_claimed": False,
                    "physical_execution_invoked": False,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    approval_ref = os.environ.get(OPERATOR_APPROVAL_REF_ENV, "").strip()
    if not approval_ref:
        raise SystemExit(f"Set {OPERATOR_APPROVAL_REF_ENV} before execution")
    command = _command_from_environment()
    parent_run_identity = f"goal-a-parent-run:{uuid4()}"
    run_dir = _new_run_dir(parent_run_identity)
    record = run_goal_a_live_parent_mission(
        run_dir=run_dir,
        parent_run_identity=parent_run_identity,
        operator_approval_ref=approval_ref,
        libero_stage_command=command,
    )
    print(json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True))
    coordinator = record["coordinator_record"]
    if coordinator["coordinator_status"] != "stages_satisfied":
        return 2
    if coordinator["stages_satisfied"] != 3:
        return 2
    if record["mission_completion_claimed"] is not False:
        return 2
    if record["physical_execution_invoked"] is not False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
