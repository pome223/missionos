"""CLI completion must reflect observed behavior, evidence, and owned cleanup."""

import json
from pathlib import Path
import sys
import uuid

from click.testing import CliRunner
import pytest

from missionos_cli.cli import missionos
import missionos_cli.navigation as nav


@pytest.fixture
def configured(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts/tb3_prediction"
    scripts.mkdir(parents=True)
    for name in ("run.py",):
        (scripts / name).write_text("pass\n")
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            dict(
                status="complete",
                head_kind="NWM_CDiT_future_obstacle_mask_hold_4s",
                context_period_s=0.5,
            )
        )
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            dict(
                repository=str(repo),
                python=str(python),
                bundle=str(model),
                tb3_manifest=str(model),
            )
        )
    )
    return config, repo / "output/run", python


def test_preview_does_not_launch_and_preserves_venv(configured, monkeypatch):
    config, output, python = configured
    monkeypatch.setattr(nav, "execute", lambda _: pytest.fail("preview launched runtime"))
    result = CliRunner().invoke(
        missionos, ["navigation", "run", "tb3", "--config", str(config), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["status"] == "not_run"
    assert receipt["command"][0] == str(python)
    assert receipt["simulator_invoked"] is False
    assert not output.exists()


def test_invalid_cadence_and_output_rejected(configured):
    config, output, _ = configured
    with pytest.raises(ValueError, match="inside repository"):
        nav.prepare(config, "tb3", "nwm", "clearing", output.parent.parent.parent / "outside")
    model = Path(json.loads(config.read_text())["tb3_manifest"])
    data = json.loads(model.read_text())
    data["context_period_s"] = 0.1
    model.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="2 Hz"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


def evidence(configured, backend="tb3"):
    config, output, _ = configured
    receipt = nav.prepare(config, backend, "nwm", "clearing", output)
    folder = output / "backend" / (receipt["scenario"] + "-nwm")
    folder.mkdir(parents=True)
    (output / "backend/manifest.json").write_text(
        json.dumps({"episodes": [dict(policy="nwm", container_removed=True)]})
    )
    result = dict(
        policy="nwm",
        status="completed",
        physical_execution_invoked=False,
        navigation_completion_verified=True,
        learned_wam_invoked=True,
        goal_reached_observed=True,
        robot_motion_observed=True,
        bridge_goals_succeeded=1,
        landing_observed=True,
        executed_candidate="wait_then_direct",
        decision=dict(
            candidate="wait", provenance=dict(task_manifest_sha256=receipt["model_manifest_sha256"])
        ),
        post_wait_verification=dict(passed=True),
    )
    path = folder / "result.json"
    path.write_text(json.dumps(result))
    return output, receipt, path, result


@pytest.mark.parametrize(
    "backend,changes",
    [
        ("tb3", {"goal_reached_observed": False}),
        ("tb3", {"bridge_goals_succeeded": 0}),
        ("tb3", {"post_wait_verification": {"passed": False}}),
    ],
)
def test_completion_requires_observed_effect(configured, backend, changes):
    output, receipt, path, result = evidence(configured, backend)
    result.update(changes)
    path.write_text(json.dumps(result))
    nav.collect(output, receipt)
    assert receipt["navigation_completion_verified"] is False


def test_model_binding_is_required(configured):
    output, receipt, path, result = evidence(configured)
    result["decision"]["provenance"]["task_manifest_sha256"] = "wrong"
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="provenance"):
        nav.collect(output, receipt)


def test_status_checks_evidence_and_completion(configured):
    output, receipt, path, result = evidence(configured)
    nav.collect(output, receipt)
    receipt.update(status="completed", cleanup_verified=True, runner_exit_code=0)
    nav.write_receipt(output, receipt)
    runner = CliRunner()
    assert runner.invoke(missionos, ["navigation", "status", str(output)]).exit_code == 0
    result["goal_reached_observed"] = False
    path.write_text(json.dumps(result))
    assert (
        "evidence changed" in runner.invoke(missionos, ["navigation", "status", str(output)]).output
    )
    receipt["evidence"][str(path.relative_to(output))] = nav.digest(path)
    nav.write_receipt(output, receipt)
    assert (
        "completion claims"
        in runner.invoke(missionos, ["navigation", "status", str(output)]).output
    )


def test_zero_process_exit_does_not_mean_navigation_success(configured, monkeypatch):
    config, output, _ = configured
    receipt = nav.prepare(config, "tb3", "nwm", "clearing", output)

    class Process:
        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(nav.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(
        nav,
        "collect",
        lambda folder, r: r.update(
            navigation_completion_verified=False, runner_container_removed=True
        ),
    )
    monkeypatch.setattr(nav, "cleanup_owned", lambda _: True)
    assert nav.execute(receipt)["status"] == "failed"


def test_termination_error_still_cleans_owned_resources(configured, monkeypatch):
    config, output, _ = configured
    receipt = nav.prepare(config, "tb3", "nwm", "clearing", output)
    cleaned = []

    class Process:
        def wait(self, **kwargs):
            raise TimeoutError("timeout")

        def poll(self):
            return None

        def send_signal(self, _):
            raise ProcessLookupError("already gone")

    monkeypatch.setattr(nav.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(nav, "cleanup_owned", lambda run_id: cleaned.append(run_id) or True)
    final = nav.execute(receipt)
    assert final["status"] == "failed" and final["cleanup_verified"]
    assert cleaned == [receipt["run_id"]]
    assert (output / "run.json").is_file()


def test_cleanup_rejects_mismatched_owner(monkeypatch):
    run_id = str(uuid.uuid4())

    class Found:
        stdout = "container1"

    calls = []
    monkeypatch.setattr(nav.subprocess, "run", lambda args, **kw: calls.append(args) or Found())
    monkeypatch.setattr(
        nav.subprocess,
        "check_output",
        lambda *a, **kw: json.dumps([{"Config": {"Labels": {nav.OWNER: "unrelated"}}}]),
    )
    with pytest.raises(ValueError, match="ownership"):
        nav.cleanup_owned(run_id)
    assert all("rm" not in args for args in calls)


def test_agent_run_requires_separate_fixed_wait_opt_in(configured, monkeypatch):
    config, output, _ = configured
    monkeypatch.setattr(nav, "execute", lambda _: pytest.fail("unapproved wait launched"))
    result = CliRunner().invoke(
        missionos,
        [
            "navigation",
            "run",
            "tb3",
            "--policy",
            "agent-nwm",
            "--config",
            str(config),
            "--output",
            str(output),
            "--run-sim",
        ],
    )
    assert result.exit_code != 0
    assert "--approve-bounded-wait" in result.output
    assert not output.exists()


@pytest.mark.parametrize("missing", [None, "budget", "observed_wait", "post_assurance"])
def test_agent_completion_requires_full_chain(configured, missing):
    output, receipt, path, result = evidence(configured)
    receipt["policy"] = "agent_nwm"
    folder = path.parent.with_name("clearing-agent_nwm")
    path.parent.rename(folder)
    path = folder / "result.json"
    result.update(
        policy="agent_nwm",
        agent_chain_completed_before_dispatch=True,
        executed_wait_sim_s=0.25,
        agent_plan_response={"status": "authorized_wait"},
        post_wait_agent_response={"status": "continue"},
    )
    events = [
        {"phase": p, "result": {}} for p in ("initial_assurance", "recovery", "post_wait_assurance")
    ]
    events.append(
        {"phase": "rules", "result": {"policy_authorized": True, "budget_reserved": True}}
    )
    if missing == "budget":
        events[-1]["result"]["budget_reserved"] = False
    elif missing == "observed_wait":
        result["executed_wait_sim_s"] = 0
    elif missing == "post_assurance":
        result["post_wait_agent_response"]["status"] = "blocked"
    path.write_text(json.dumps(result))
    (folder / "agent-chain.json").write_text(json.dumps({"events": events}))
    (output / "backend/manifest.json").write_text(
        json.dumps({"episodes": [{"policy": "agent_nwm", "container_removed": True}]})
    )
    nav.collect(output, receipt)
    assert receipt["navigation_completion_verified"] is (missing is None)
    assert "backend/clearing-agent_nwm/agent-chain.json" in receipt["evidence"]


@pytest.mark.parametrize("value", [-0.1, 0.31, float("nan"), True, "0.18"])
def test_actor_speed_rejects_out_of_scope_configuration(configured, value):
    config, output, _ = configured
    data = json.loads(config.read_text())
    data["tb3_actor_speed_mps"] = value
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="actor speed"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


def test_speed_override_reaches_runtime_and_delay_cannot_affect_baseline(configured):
    config, output, _ = configured
    data = json.loads(config.read_text())
    data["tb3_actor_speed_mps"] = 0.18
    config.write_text(json.dumps(data))
    command = nav.prepare(config, "tb3", "nwm", "clearing", output)["command"]
    assert command[command.index("--actor-speed") + 1] == "0.18"
    data["test_agent_plan_delay_wall_s"] = 8
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="requires agent-nwm"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


@pytest.mark.parametrize("value", [-1, 11, float("nan"), True])
def test_fault_delay_is_bounded(configured, value):
    config, output, _ = configured
    data = json.loads(config.read_text())
    data["test_agent_plan_delay_wall_s"] = value
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="between 0 and 10"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


@pytest.mark.parametrize("missing", [None, "approval", "rules", "second_goal", "recovery"])
def test_agent_detour_completion_requires_separate_authority_and_observed_route(
    configured, missing
):
    output, receipt, path, result = evidence(configured)
    receipt.update(policy="agent_nwm", bounded_detour_approved=missing != "approval")
    folder = path.parent.with_name("clearing-agent_nwm")
    path.parent.rename(folder)
    result.update(
        policy="agent_nwm",
        agent_chain_completed_before_dispatch=True,
        executed_wait_sim_s=1.5,
        agent_plan_response={"status": "authorized_wait"},
        executed_candidate="agent_detour_after_wait_observation",
        post_wait_verification={"passed": False, "observed_exposure": 0.4},
        detour_agent_response={"status": "authorized_detour"},
        bridge_goals_succeeded=1 if missing == "second_goal" else 2,
        pre_dispatch_detour_plan={"status": "succeeded", "waypoints": [[0.35, -0.7], [1.0, 0.0]]},
    )
    events = [
        {"phase": p, "result": {}}
        for p in (
            "initial_assurance",
            "recovery",
            "failed_wait_assurance",
            "detour_recovery",
            "detour_assurance_graph",
        )
    ]
    for phase in ("rules", "detour_rules"):
        events.append(
            {
                "phase": phase,
                "result": {
                    "policy_authorized": True,
                    "budget_reserved": not (missing == "rules" and phase == "detour_rules"),
                },
            }
        )
    if missing == "recovery":
        events = [e for e in events if e["phase"] != "detour_recovery"]
    (folder / "result.json").write_text(json.dumps(result))
    (folder / "agent-chain.json").write_text(json.dumps({"events": events}))
    (output / "backend/manifest.json").write_text(
        json.dumps({"episodes": [{"policy": "agent_nwm", "container_removed": True}]})
    )
    for name in ("detour-plan-before-agent.json", "detour-plan-before-dispatch.json"):
        (folder / name).write_text(json.dumps(result["pre_dispatch_detour_plan"]))
    nav.collect(output, receipt)
    assert receipt["navigation_completion_verified"] is (missing is None)


def test_public_controller_profile_preserves_legacy(configured):
    config, output, _ = configured
    data = json.loads(config.read_text())
    receipt = nav.prepare(config, "tb3", "nwm", "clearing", output)
    assert receipt["command"][receipt["command"].index("--controller-profile") + 1] == "legacy"
    data["tb3_controller_profile"] = "aligned-goal"
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="controller profile"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


def test_via_heading_is_opt_in_and_keeps_controller(configured):
    config, output, _ = configured
    original = json.loads(config.read_text())
    receipt = nav.prepare(config, "tb3", "nwm", "clearing", output)
    i = receipt["command"].index("--via-heading")
    assert receipt["command"][i + 1] == "outgoing"
    config.write_text(json.dumps(dict(original, tb3_via_heading="incoming")))
    changed = nav.prepare(config, "tb3", "nwm", "clearing", output)
    assert changed["command"][i + 1] == "incoming"
    assert changed["command"][changed["command"].index("--controller-profile") + 1] == "legacy"
    assert not output.exists()
    with pytest.raises(ValueError, match="TB3 only"):
        nav.prepare(config, "px4", "nwm", "clearing", output)
    with pytest.raises(ValueError, match="via heading|controller profile"):
        nav.prepare(config, "tb3", "nwm", "persistent", output)
    config.write_text(
        json.dumps(
            dict(original, tb3_via_heading="incoming", tb3_controller_profile="aligned-goal")
        )
    )
    with pytest.raises(ValueError, match="via heading|controller profile"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


@pytest.mark.parametrize("goals", [1, 2])
def test_incoming_heading_completion_binds_requests_and_final_heading(configured, goals):
    import math

    output, receipt, path, result = evidence(configured)
    receipt["command"][receipt["command"].index("--via-heading") + 1] = "incoming"
    nav.collect(output, receipt)
    assert not receipt["navigation_completion_verified"]
    result.update(via_heading="incoming", bridge_goals_succeeded=goals)
    readback = dict(
        profile="legacy",
        before_mission_start=True,
        parameters={
            "FollowPath.xy_goal_tolerance": 0.05,
            "general_goal_checker.xy_goal_tolerance": 0.25,
            "general_goal_checker.yaw_goal_tolerance": 0.25,
            "FollowPath.max_vel_x": 0.20,
            "FollowPath.RotateToGoal.lookahead_time": -1.0,
        },
    )
    result["controller_profile_readback"] = readback
    (path.parent / "controller-profile-readback.json").write_text(json.dumps(readback))
    stages = []
    for i in range(goals):
        xy = [0.35, -0.7] if goals == 2 and i == 0 else [1.0, 0.0]
        yaw = math.atan2(-0.7, 1.35) if goals == 2 and i == 0 else 0.0
        stages.append(dict(robot_xy_m=[-1.0, 0.0], end_robot_xy_m=xy, requested_yaw_rad=yaw))
        (path.parent / f"goal-{i}-request.json").write_text(
            json.dumps(
                dict(
                    physical_execution_invoked=False,
                    payload=dict(x_m=xy[0], y_m=xy[1], yaw_rad=yaw),
                )
            )
        )
        (path.parent / f"goal-{i}.json").write_text(json.dumps(dict(nav2_status="succeeded")))
    result["goal_stages"] = stages
    path.write_text(json.dumps(result))
    nav.collect(output, receipt)
    assert receipt["navigation_completion_verified"]
    assert str((path.parent / "goal-0-request.json").relative_to(output)) in receipt["evidence"]
    final_path = path.parent / f"goal-{goals - 1}-request.json"
    final = json.loads(final_path.read_text())
    final["payload"]["yaw_rad"] = 0.1
    final_path.write_text(json.dumps(final))
    nav.collect(output, receipt)
    assert not receipt["navigation_completion_verified"]
    final["payload"]["yaw_rad"] = 0.0
    final_path.write_text(json.dumps(final))
    result["goal_stages"][0]["end_robot_xy_m"] = [1.4, 0.9]
    path.write_text(json.dumps(result))
    nav.collect(output, receipt)
    assert not receipt["navigation_completion_verified"]


def test_actor_initial_position_reaches_runtime_and_default_is_preserved(configured):
    config, output, _ = configured
    original = nav.prepare(config, "tb3", "nwm", "clearing", output)["command"]
    assert original[original.index("--actor-initial-y") + 1] == "-0.88"
    data = json.loads(config.read_text())
    data["tb3_actor_initial_y_m"] = -0.95
    config.write_text(json.dumps(data))
    command = nav.prepare(config, "tb3", "nwm", "clearing", output)["command"]
    assert command[command.index("--actor-initial-y") + 1] == "-0.95"
    for backend, scene in [("px4", "clearing"), ("tb3", "persistent")]:
        with pytest.raises(ValueError, match="initial y|TB3 only"):
            nav.prepare(config, backend, "nwm", scene, output)


@pytest.mark.parametrize("value", [-1.21, -0.59, True, "-0.9", float("nan"), float("inf")])
def test_actor_initial_position_rejects_outside_scope(configured, value):
    config, output, _ = configured
    data = json.loads(config.read_text())
    data["tb3_actor_initial_y_m"] = value
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="initial y"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


@pytest.mark.parametrize(
    "missing", [None, "approval", "rules", "image", "deadline", "clearance", "plan"]
)
def test_original_route_continuation_requires_distinct_observed_chain(configured, missing):
    output, receipt, path, result = evidence(configured)
    receipt.update(policy="agent_nwm", observed_continue_approved=missing != "approval")
    folder = path.parent.with_name("clearing-agent_nwm")
    path.parent.rename(folder)
    image = folder / "continue.png"
    image.write_bytes(b"fixture actual image")
    result.update(
        policy="agent_nwm",
        decision=dict(result["decision"], candidate="direct"),
        executed_candidate="observed_original_route_continue",
        executed_wait_sim_s=0,
        agent_chain_completed_before_dispatch=True,
        agent_plan_response=dict(status="observe_original_route"),
        continue_agent_response=dict(status="authorized_continue", dispatch_before_sim_s=4.0),
        continue_verification=dict(
            passed=True,
            observed_exposure=0.0,
            image=dict(path=image.name, sha256=nav.digest(image)),
        ),
        pre_dispatch_observation=dict(robot_stationary=True, exposure=0.0, sim_s=3.0),
    )
    events = [
        dict(phase=p, result={})
        for p in ["initial_assurance", "continue_recovery", "continue_assurance_graph"]
    ]
    events.append(
        dict(
            phase="continue_rules",
            result=dict(policy_authorized=True, budget_reserved=missing != "rules"),
        )
    )
    if missing == "image":
        image.write_bytes(b"changed")
    if missing == "deadline":
        result["pre_dispatch_observation"]["sim_s"] = 5.0
    if missing == "clearance":
        result["pre_dispatch_observation"]["exposure"] = 0.4
    for name in ["continue-plan-before-agent.json", "continue-plan-before-dispatch.json"]:
        (folder / name).write_text(
            json.dumps(
                dict(
                    status="succeeded",
                    waypoints=[[2.0, 0.0]] if missing == "plan" else [[1.0, 0.0]],
                )
            )
        )
    (folder / "goal-0-request.json").write_text(
        json.dumps(dict(payload=dict(x_m=1.0, y_m=0.0, yaw_rad=0.0, max_speed_mps=0.2)))
    )
    (folder / "goal-0.json").write_text(json.dumps(dict(nav2_status="succeeded")))
    (folder / "result.json").write_text(json.dumps(result))
    (folder / "agent-chain.json").write_text(json.dumps(dict(events=events)))
    (output / "backend/manifest.json").write_text(
        json.dumps(dict(episodes=[dict(policy="agent_nwm", container_removed=True)]))
    )
    nav.collect(output, receipt)
    assert receipt["navigation_completion_verified"] is (missing is None)


@pytest.mark.parametrize("value", [-1, 11, True, float("nan")])
def test_first_plan_fault_is_bounded(configured, value):
    config, output, _ = configured
    data = json.loads(config.read_text())
    data["test_agent_first_plan_delay_wall_s"] = value
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="first-plan delay"):
        nav.prepare(config, "tb3", "nwm", "clearing", output)


def test_continue_approval_and_first_only_delay_reach_runner(configured):
    config, output, python = configured
    data = json.loads(config.read_text())
    for name in (
        "src/intelligence/navigation_agents.py",
        "src/intelligence/navigation_prediction.py",
        "src/runtime/tb3_predictive_recovery.py",
        "src/intelligence/mission_assurance_agent.py",
        "src/intelligence/missionos_mission_incident_graph.py",
        "src/intelligence/mission_assurance_policy.py",
    ):
        p = output.parent.parent / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture source\n")
    data.update(agent_python=str(python), test_agent_first_plan_delay_wall_s=8)
    config.write_text(json.dumps(data))
    args = [
        "navigation",
        "run",
        "tb3",
        "--policy",
        "agent-nwm",
        "--config",
        str(config),
        "--output",
        str(output),
        "--approve-bounded-wait",
    ]
    preview = CliRunner().invoke(missionos, args)
    assert preview.exit_code == 0, preview.output
    before = json.loads(preview.output)
    assert "--approve-observed-continue" not in before["command"]
    preview = CliRunner().invoke(missionos, args + ["--approve-observed-continue"])
    assert preview.exit_code == 0, preview.output
    receipt = json.loads(preview.output)
    assert receipt["observed_continue_approved"] is True
    assert "--approve-observed-continue" in receipt["command"]
    assert (
        receipt["command"][receipt["command"].index("--test-agent-first-plan-delay-wall-s") + 1]
        == "8"
    )
    assert not output.exists()
