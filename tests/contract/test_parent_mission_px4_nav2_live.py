from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts import smoke_parent_mission_px4_nav2_live as live


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
    first_satisfied: bool = True,
    mutate_first_contract: bool = False,
):
    _, _, children = live.build_issue164_parent_mission_bundle(
        operator_approval_ref="approval:test"
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
            satisfied = first_satisfied
        else:
            stage_ref = live.NAV2_STAGE_REF
            artifact_root = Path(
                env["ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT"]
            )
            child_run = artifact_root / "nav2_turtlebot3_fixture"
            satisfied = True
        calls.append(stage_ref)
        child_run.mkdir()
        contract_material = children[stage_ref].to_material()
        if stage_ref == live.PX4_STAGE_REF and mutate_first_contract:
            contract_material["contract_version"] = "mutated"
        for name, payload in {
            "mission_contract.json": contract_material,
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


def test_live_parent_freezes_before_running_exact_child_contracts(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(run_dir=run_dir)

    record = live.run_issue164_live_parent_mission(
        run_dir=run_dir,
        operator_approval_ref="approval:test",
        subprocess_runner=runner,
    )

    assert calls == [live.PX4_STAGE_REF, live.NAV2_STAGE_REF]
    task_payload = json.loads(
        (run_dir / "job_status_task_payload.json").read_text(encoding="utf-8")
    )
    assert task_payload["kind"] == "parent_mission_execution"
    assert task_payload["status"] == "completed"
    assert (
        task_payload["artifacts"]["missionos_parent_mission_run_record"][
            "parent_mission_sha256"
        ]
        == record["parent_mission_sha256"]
    )
    assert (
        task_payload["artifacts"]["missionos_parent_mission_run_record"][
            "coordinator_record"
        ]["coordinator_status"]
        == "stages_satisfied"
    )
    assert record["coordinator_record"]["coordinator_status"] == (
        "stages_satisfied"
    )
    assert record["coordinator_record"]["stages_satisfied"] == 2
    assert all(
        audit["child_contract_matched_prefrozen_material"] is True
        for audit in record["stage_audits"].values()
    )
    assert record["execution_mode"] == "sequential"
    assert record["simulator_overlap_tested"] is False
    assert record["mission_completion_claimed"] is False
    assert record["shared_world_claimed"] is False
    assert record["physical_execution_invoked"] is False


def test_unsatisfied_px4_stage_never_invokes_nav2(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(
        run_dir=run_dir,
        first_satisfied=False,
    )

    record = live.run_issue164_live_parent_mission(
        run_dir=run_dir,
        operator_approval_ref="approval:test",
        subprocess_runner=runner,
    )

    assert calls == [live.PX4_STAGE_REF]
    assert record["coordinator_record"]["coordinator_status"] == "blocked"
    assert record["coordinator_record"]["unreached_stage_refs"] == [
        live.NAV2_STAGE_REF
    ]


def test_mutated_child_contract_blocks_before_nav2(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls, runner = _fake_runner(
        run_dir=run_dir,
        mutate_first_contract=True,
    )

    record = live.run_issue164_live_parent_mission(
        run_dir=run_dir,
        operator_approval_ref="approval:test",
        subprocess_runner=runner,
    )

    assert calls == [live.PX4_STAGE_REF]
    assert record["coordinator_record"]["coordinator_status"] == "blocked"
    assert record["stage_audits"][live.PX4_STAGE_REF]["failure"] == (
        "RuntimeError"
    )
    assert record["coordinator_record"]["unreached_stage_refs"] == [
        live.NAV2_STAGE_REF
    ]


def test_main_is_opt_in_and_does_not_create_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(live.OPT_IN_ENV, raising=False)
    monkeypatch.setenv(live.ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))

    assert live.main() == 0
    assert not (tmp_path / "artifacts").exists()
