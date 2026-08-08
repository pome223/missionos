from pathlib import Path
import re

from src.runtime.turtlebot3_chat_e2e_runner import (
    _ADK_ENV_KEYS,
    _approve_turtlebot3_recovery_checkpoint,
    _disabled_recovery_decision_demo_summary,
    _gateway_env,
    _recovery_decision_demo_summary,
)


def test_turtlebot3_docker_smoke_forwards_deepseek_configuration() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh"
    ).read_text(encoding="utf-8")

    assert "-e DEEPSEEK_API_KEY" in script
    assert '-e "DEEPSEEK_API_KEY=' not in script
    assert "-e GOOGLE_API_KEY" in script
    assert '-e "GOOGLE_API_KEY=' not in script
    assert "MISSIONOS_DEEPSEEK_MODEL" in script
    assert "MISSIONOS_DEEPSEEK_API_BASE" in script
    assert "import litellm" in script
    assert "import redis" in script
    assert '"google-adk[extensions]>=2.5.0,<3.0.0"' in script
    assert '"redis>=5.0.0,<7.0.0"' in script
    assert re.search(r"\bsk-[A-Za-z0-9]{20,}\b", script) is None

    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert '"google-adk[extensions]>=2.5.0,<3.0.0"' in pyproject
    assert '"redis>=5.0.0,<7.0.0"' in pyproject

    dockerfile = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "ros2_nav2_turtlebot3"
        / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "'google-adk[extensions]>=2.5.0,<3.0.0'" in dockerfile
    assert "'redis>=5.0.0,<7.0.0'" in dockerfile

    gateway_script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "start_ros2_nav2_turtlebot3_gateway_docker.sh"
    ).read_text(encoding="utf-8")
    assert 'gateway_llm_backend="${MISSIONOS_LLM_BACKEND:-deepseek}"' in gateway_script
    assert (
        'planner_backend="${MISSIONOS_AGENT_MISSIONOS_TURTLEBOT3_RECOVERY_'
        'PLANNER_AGENT_LLM_BACKEND:-deepseek}"' in gateway_script
    )
    assert "-e DEEPSEEK_API_KEY" in gateway_script
    assert '-e "DEEPSEEK_API_KEY=' not in gateway_script
    assert "-e GOOGLE_API_KEY" in gateway_script
    assert '-e "GOOGLE_API_KEY=' not in gateway_script
    assert "-e GATEWAY_API_KEY" in gateway_script
    assert '-e "GATEWAY_API_KEY=' not in gateway_script
    assert "MISSIONOS_DEEPSEEK_MODEL" in gateway_script
    assert "MISSIONOS_DEEPSEEK_API_BASE" in gateway_script
    assert "gateway_default_model_id=gemini-3.1-flash-lite" in gateway_script
    assert "planner_default_model_id=gemini-3.1-flash-lite" in gateway_script
    assert re.search(r"\bsk-[A-Za-z0-9]{20,}\b", gateway_script) is None


def test_recovery_smoke_approves_exact_pending_checkpoint(monkeypatch) -> None:
    captured: dict = {}

    def fake_post_json(url, payload, *, timeout_s):
        captured.update({"url": url, "payload": payload, "timeout_s": timeout_s})
        return {
            "task": {
                "artifacts": {
                    "summary": {"task_id": "task_tb3_contract"},
                    "turtlebot3_recovery_checkpoint": {
                        "checkpoint_status": "consumed"
                    },
                }
            }
        }

    monkeypatch.setattr(
        "src.runtime.turtlebot3_chat_e2e_runner._post_json",
        fake_post_json,
    )
    result = _approve_turtlebot3_recovery_checkpoint(
        base_url="http://127.0.0.1:12345",
        executed={
            "routed_action": "execute",
            "operation_result": {
                "summary": {"task_id": "task_tb3_contract"},
                "turtlebot3_recovery_checkpoint": {
                    "checkpoint_status": "awaiting_operator_approval",
                    "checkpoint_id": "checkpoint-1",
                    "checkpoint_hash": "hash-1",
                    "selected_action": "avoid_obstacle",
                    "approved_parameters": {"target_x_m": 0.5},
                },
            },
        },
    )

    assert captured["url"].endswith(
        "/px4-gazebo/mission-scenarios/recovery-dispatch"
    )
    assert captured["payload"] == {
        "task_id": "task_tb3_contract",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": {"target_x_m": 0.5},
        "explicit_recovery_dispatch_approval": True,
        "expected_recovery_checkpoint_id": "checkpoint-1",
        "expected_recovery_checkpoint_hash": "hash-1",
    }
    assert result["routed_action"] == "execute"
    assert result["operation_result"]["summary"]["task_id"] == (
        "task_tb3_contract"
    )


def test_turtlebot3_chat_smoke_gateway_env_defaults_to_deepseek(monkeypatch) -> None:
    monkeypatch.delenv("MISSIONOS_LLM_BACKEND", raising=False)
    for key in _ADK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    env = _gateway_env()

    assert env["MISSIONOS_GATEWAY_BACKEND"] == "production"
    assert env["MISSIONOS_LLM_BACKEND"] == "deepseek"
    for key in _ADK_ENV_KEYS:
        assert env[key] == "1"


def test_turtlebot3_chat_smoke_gateway_env_explicit_off_disables_adk(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MISSIONOS_LLM_BACKEND", "off")
    for key in _ADK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    env = _gateway_env()

    assert env["MISSIONOS_LLM_BACKEND"] == "off"
    for key in _ADK_ENV_KEYS:
        assert env[key] == "0"


def test_recovery_decision_demo_counts_llm_judgment_without_new_approval() -> None:
    summary = {
        "runtime_recovery_triggered": True,
        "runtime_recovery_action_kind": "avoid_obstacle",
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "route_resumed_after_recovery": True,
        "route_completed_after_recovery": True,
        "runtime_recovery_motion_context": {"odom_delta_m": 1.2},
        "recovery_planner_status": "proposal_guardrail_passed",
        "completion_scope": "sim_action",
        "completion_claimed": True,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "recovery_proposal_classifications": [
            {
                "execution_class": "auto_executable",
                "requires_new_human_approval": False,
                "execution_permitted_by_envelope": True,
                "proposal_allowed": True,
            }
        ],
    }
    proposals = [
        {
            "proposal_source": "llm",
            "approval_created": False,
            "input_observations": {
                "runtime_obstacle_observed": True,
                "recommended_recovery_action": "avoid_obstacle",
                "odom_delta_m": 1.2,
            },
        }
    ]

    decision = _recovery_decision_demo_summary(
        scenario="dynamic_obstacle_recovery",
        trigger="runtime_obstacle",
        approved={"routed_action": "approve"},
        executed={"routed_action": "execute"},
        summary=summary,
        proposals=proposals,
        planner_result={"planner_status": "proposal_guardrail_passed"},
    )

    assert decision["enabled"] is True
    assert decision["approve_route"] == "approve"
    assert decision["execute_route"] == "execute"
    assert decision["judgment_required"] is True
    assert decision["accepted_recovery_proposal_count"] == 1
    assert decision["llm_recovery_judgment_count"] == 1
    assert decision["guardrail_blocked_llm_output_count"] == 0
    assert decision["mission_operator_approval_count"] == 1
    assert decision["fresh_recovery_operator_approval_count"] == 0
    assert decision["operator_approval_reused_for_recovery"] is True
    assert decision["selected_action"] == "avoid_obstacle"
    assert decision["rules_execution_class"] == "auto_executable"
    assert decision["requires_new_human_approval"] is False
    assert decision["mission_delivery_completion_claimed"] is False
    assert decision["physical_execution_invoked"] is False
    assert decision["source_backed_input_observation_keys"] == [
        "odom_delta_m",
        "recommended_recovery_action",
        "runtime_obstacle_observed",
    ]


def test_recovery_decision_demo_counts_guardrail_fallback_separately() -> None:
    summary = {
        "runtime_recovery_triggered": True,
        "recovery_action_suggested": "avoid_obstacle",
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "route_resumed_after_recovery": True,
        "route_completed_after_recovery": True,
        "recovery_planner_status": "guardrail_blocked",
        "completion_scope": "sim_action",
        "completion_claimed": True,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "recovery_proposal_classifications": [
            {
                "execution_class": "auto_executable",
                "requires_new_human_approval": False,
                "execution_permitted_by_envelope": True,
                "proposal_allowed": True,
            }
        ],
    }
    proposals = [
        {
            "proposal_source": "deterministic_fallback",
            "approval_created": False,
            "input_observations": {"runtime_obstacle_observed": True},
        }
    ]

    decision = _recovery_decision_demo_summary(
        scenario="dynamic_obstacle_recovery",
        trigger="runtime_obstacle",
        approved={"routed_action": "approve"},
        executed={"routed_action": "execute"},
        summary=summary,
        proposals=proposals,
        planner_result={
            "planner_status": "guardrail_blocked",
            "guardrail": {"guardrail_passed": False},
        },
    )

    assert decision["llm_recovery_judgment_count"] == 0
    assert decision["approve_route"] == "approve"
    assert decision["execute_route"] == "execute"
    assert decision["guardrail_blocked_llm_output_count"] == 1
    assert decision["deterministic_fallback_count"] == 1
    assert decision["fresh_recovery_operator_approval_count"] == 0
    assert decision["operator_approval_reused_for_recovery"] is True


def test_recovery_decision_demo_counts_fresh_operator_approval() -> None:
    summary = {
        "runtime_recovery_triggered": True,
        "runtime_recovery_action_kind": "avoid_obstacle",
        "recovery_dispatch_request_sent": True,
        "recovery_completion_claimed": True,
        "route_resumed_after_recovery": True,
        "route_completed_after_recovery": True,
        "runtime_recovery_motion_context": {"odom_delta_m": 1.2},
        "recovery_planner_status": "proposal_guardrail_passed",
        "completion_scope": "sim_action",
        "completion_claimed": True,
        "mission_delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "fresh_recovery_operator_approval_count": 1,
        "fresh_recovery_operator_approvals": [
            {
                "operator_approval_ref": "operator_approval:codex_e2e_recovery",
                "approval_actor": "codex_e2e_operator",
                "approved_action": "avoid_obstacle",
            }
        ],
        "recovery_execution_permitted_by_operator_approval": True,
        "recovery_dispatch_authority_source": "fresh_operator_approval",
        "recovery_proposal_classifications": [
            {
                "execution_class": "requires_human_approval",
                "requires_new_human_approval": True,
                "execution_permitted_by_envelope": False,
                "proposal_allowed": True,
            }
        ],
    }
    proposals = [
        {
            "proposal_source": "llm",
            "approval_created": False,
            "input_observations": {
                "runtime_obstacle_observed": True,
                "recommended_recovery_action": "avoid_obstacle",
                "odom_delta_m": 1.2,
            },
        }
    ]

    decision = _recovery_decision_demo_summary(
        scenario="dynamic_obstacle_recovery",
        trigger="runtime_obstacle",
        approved={"routed_action": "approve"},
        executed={"routed_action": "execute"},
        summary=summary,
        proposals=proposals,
        planner_result={"planner_status": "proposal_guardrail_passed"},
    )

    assert decision["llm_recovery_judgment_count"] == 1
    assert decision["mission_operator_approval_count"] == 1
    assert decision["fresh_recovery_operator_approval_count"] == 1
    assert decision["fresh_recovery_operator_approvals"] == [
        {
            "operator_approval_ref": "operator_approval:codex_e2e_recovery",
            "approval_actor": "codex_e2e_operator",
            "approved_action": "avoid_obstacle",
        }
    ]
    assert decision["operator_approval_created_for_recovery"] is True
    assert decision["operator_approval_reused_for_recovery"] is False
    assert decision["rules_execution_class"] == "requires_human_approval"
    assert decision["requires_new_human_approval"] is True
    assert decision["execution_permitted_by_envelope"] is False
    assert decision["recovery_execution_permitted_by_operator_approval"] is True
    assert decision["recovery_dispatch_authority_source"] == "fresh_operator_approval"
    assert decision["mission_delivery_completion_claimed"] is False
    assert decision["physical_execution_invoked"] is False


def test_recovery_decision_demo_disabled_shape_is_explicit() -> None:
    decision = _disabled_recovery_decision_demo_summary()

    assert decision == {
        "schema_version": "missionos_turtlebot3_recovery_decision_demo.v1",
        "enabled": False,
    }
