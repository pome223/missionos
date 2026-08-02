from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts import smoke_parent_mission_px4_nav2_libero_live as live


PARENT_RUN_IDENTITY = "goal-a-parent-run:test"


def _evaluation(contract, *, satisfied: bool = True) -> dict:
    return {
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "predicate_package_id": contract.predicate_package.package_id,
        "predicate_package_version": contract.predicate_package.package_version,
        "predicate_package_sha256": contract.predicate_package.content_sha256,
        "status": "satisfied" if satisfied else "not_satisfied",
        "evaluated_outcome_claim": satisfied,
        "actual_verification_basis": "deterministic",
        "predicate_package_evaluated": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
        "reasons": [] if satisfied else ["fixture_predicate_not_satisfied"],
    }


def _fake_runner(
    *,
    run_dir: Path,
    nav2_satisfied: bool = True,
    libero_identity: str = PARENT_RUN_IDENTITY,
):
    _, _, children, episode_identity = live.build_goal_a_parent_mission_bundle(
        parent_run_identity=PARENT_RUN_IDENTITY,
        operator_approval_ref="approval:test",
    )
    calls: list[str] = []

    def run(args, *, env, **_kwargs):
        assert (run_dir / "parent_mission_contract.json").is_file()
        assert (run_dir / "parent_mission_approval.json").is_file()
        if any(
            str(arg).endswith("smoke_px4_gazebo_sitl_e2e_delivery.py")
            for arg in args
        ):
            stage_ref = live.PX4_STAGE_REF
            artifact_root = Path(env["PX4_GAZEBO_SITL_E2E_ARTIFACT_ROOT"])
            child_run = artifact_root / "sitl_e2e_delivery_fixture"
            satisfied = True
        elif any("mission_contract_docker" in str(arg) for arg in args):
            stage_ref = live.NAV2_STAGE_REF
            artifact_root = Path(
                env["ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT"]
            )
            child_run = artifact_root / "nav2_turtlebot3_fixture"
            satisfied = nav2_satisfied
        else:
            stage_ref = live.LIBERO_STAGE_REF
            calls.append(stage_ref)
            output = Path(env["MISSIONOS_LIBERO_RESULT_PATH"])
            report = {
                "schema_version": (
                    "missionos_groot_n17_libero_panda_live_instrumented_run.v2"
                ),
                "instrumented_episode": {
                    "run_identity": libero_identity,
                    "episode_identity": episode_identity,
                    "contract_sha256": children[stage_ref].contract_sha256,
                    "predicate_evaluation": _evaluation(children[stage_ref]),
                },
            }
            output.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        calls.append(stage_ref)
        child_run.mkdir()
        for name, payload in {
            "mission_contract.json": children[stage_ref].to_material(),
            "mission_contract_predicate_content.json": {"fixture": stage_ref},
            "mission_contract_predicate_evaluation.json": _evaluation(
                children[stage_ref],
                satisfied=satisfied,
            ),
            "summary.json": {"fixture": stage_ref},
        }.items():
            (child_run / name).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    return calls, run


def test_three_live_stage_results_share_one_parent_invocation(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(run_dir=run_dir)

    record = live.run_goal_a_live_parent_mission(
        run_dir=run_dir,
        parent_run_identity=PARENT_RUN_IDENTITY,
        operator_approval_ref="approval:test",
        libero_stage_command=("fake-libero-stage",),
        subprocess_runner=runner,
    )

    assert calls == [
        live.PX4_STAGE_REF,
        live.NAV2_STAGE_REF,
        live.LIBERO_STAGE_REF,
    ]
    assert record["parent_run_identity"] == PARENT_RUN_IDENTITY
    assert record["missionos_parent_coordinator_in_live_loop"] is True
    assert record["coordinator_record"]["coordinator_status"] == (
        "stages_satisfied"
    )
    assert record["coordinator_record"]["stages_satisfied"] == 3
    assert record["mission_completion_claimed"] is False
    assert record["shared_world_claimed"] is False
    assert record["physical_execution_invoked"] is False
    assert all(
        audit["child_contract_matched_prefrozen_material"] is True
        for audit in record["stage_audits"].values()
    )
    task = json.loads(
        (run_dir / "job_status_task_payload.json").read_text(
            encoding="utf-8"
        )
    )
    assert task["task_id"] == PARENT_RUN_IDENTITY
    assert task["status"] == "completed"


def test_unsatisfied_nav2_never_invokes_live_libero(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(run_dir=run_dir, nav2_satisfied=False)

    record = live.run_goal_a_live_parent_mission(
        run_dir=run_dir,
        parent_run_identity=PARENT_RUN_IDENTITY,
        operator_approval_ref="approval:test",
        libero_stage_command=("fake-libero-stage",),
        subprocess_runner=runner,
    )

    assert calls == [live.PX4_STAGE_REF, live.NAV2_STAGE_REF]
    assert record["coordinator_record"]["coordinator_status"] == "blocked"
    assert record["coordinator_record"]["unreached_stage_refs"] == [
        live.LIBERO_STAGE_REF
    ]


def test_old_libero_result_is_refused_before_live_command(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(run_dir=run_dir)
    stale = run_dir / "libero" / "live_result.json"
    stale.parent.mkdir()
    stale.write_text("{}", encoding="utf-8")

    record = live.run_goal_a_live_parent_mission(
        run_dir=run_dir,
        parent_run_identity=PARENT_RUN_IDENTITY,
        operator_approval_ref="approval:test",
        libero_stage_command=("fake-libero-stage",),
        subprocess_runner=runner,
    )

    assert calls == [live.PX4_STAGE_REF, live.NAV2_STAGE_REF]
    assert record["coordinator_record"]["coordinator_status"] == "blocked"
    assert live.LIBERO_STAGE_REF not in record["stage_audits"]


def test_mismatched_libero_parent_identity_blocks_stage(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(
        run_dir=run_dir,
        libero_identity="different-parent-run",
    )

    record = live.run_goal_a_live_parent_mission(
        run_dir=run_dir,
        parent_run_identity=PARENT_RUN_IDENTITY,
        operator_approval_ref="approval:test",
        libero_stage_command=("fake-libero-stage",),
        subprocess_runner=runner,
    )

    assert calls[-1] == live.LIBERO_STAGE_REF
    assert record["coordinator_record"]["coordinator_status"] == "blocked"
    assert record["stage_audits"][live.LIBERO_STAGE_REF]["failure"] == (
        "RuntimeError"
    )
    assert record["stage_audits"][live.LIBERO_STAGE_REF][
        "failure_reason"
    ] == "LIBERO parent run identity mismatch"


def test_main_is_opt_in(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(live.OPT_IN_ENV, raising=False)
    monkeypatch.setenv(live.ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))

    assert live.main() == 0
    assert not (tmp_path / "artifacts").exists()
