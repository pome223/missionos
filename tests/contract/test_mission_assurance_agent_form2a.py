from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.gateway import missionos_knowledge_sharing as knowledge
from src.intelligence.mission_assurance_agent import (
    MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA,
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
    _adk_output_schema,
)
from src.runtime.px4_gazebo_route.mission_assurance_adapter import (
    compile_mission_response_proposal,
    observe_form1_mission_situation,
)

pytestmark = pytest.mark.contract


class _Judge:
    def __init__(self, output: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.output = dict(output or {})
        self.error = error
        self.prompts: list[Mapping[str, Any]] = []

    def judge(self, prompt: Mapping[str, Any]) -> ModelJudgment:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return ModelJudgment(
            output=self.output,
            invocation_evidence={"invocation_kind": "fixture_llm", "model_id": "fixture"},
        )


def _judgment(response_kind: str, *, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "proposed_response_kind": response_kind,
        "parameters": dict(parameters or {}),
        "rationale": "The observed mission context justifies this proposal.",
        "expected_outcome": "The operator can review a bounded next step.",
        "uncertainty": "Fixture judgment; no execution outcome is claimed.",
        "operator_question": "Approve or revise the proposed mission response?",
    }


def _synthetic_situation() -> MissionSituation:
    return MissionSituation(
        situation_id="mission_situation_synthetic",
        observed_at=datetime.now(UTC).isoformat(),
        mission_contract={"objective": "process the queued work"},
        progress={"completed_units": 4, "planned_units": 10},
        observations={"queue_depth": 6, "worker_health": "degraded"},
        constraints={"maximum_latency_seconds": 30},
        uncertainty={"worker_recovery_time": "unknown"},
        source_refs=("synthetic_adapter:observation_1",),
        source_schema_version="synthetic_queue_observation.v1",
        input_digest="a" * 64,
        execution_scope="simulator",
    )


def _payload_form1() -> dict[str, Any]:
    return {
        "schema_version": "drone_behavior_delta_under_payload_mass.v1",
        "audit_id": "payload_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "causal_form": "Form 1a",
        "form1_scope": "drone_physics_or_mission_behavior",
        "condition_kind": "payload_mass_drone_behavior_delta",
        "form1_claim_supported": True,
        "payload_behavior_delta_observed": True,
        "raw_behavior_delta_above_threshold": True,
        "drone_behavior_affected": True,
        "source_binding": {
            "source_boundary_flags_safe": True,
            "source_runs_interpretable": True,
            "route_geometry_match": True,
        },
        "metrics": {
            "max_observed_delta_m": 0.8,
            "delta_threshold_m": 0.25,
            "climb_time_delta_threshold_seconds": 1.0,
            "climb_elapsed_seconds_delta_at_target_z": 1.4,
        },
        "requested": {"light_payload_kg": 0.0, "heavy_payload_kg": 1.0},
        "progress_counted": True,
        "drone_physics_affected": True,
    }


def _runtime_evidence(invocation_id: str) -> dict[str, Any]:
    empty_sha = hashlib.sha256(b"").hexdigest()
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_id": invocation_id,
        "invocation_kind": "subprocess",
        "invocation_target": "fixture",
        "invocation_started_at": now,
        "invocation_completed_at": now,
        "invocation_stdout_sha256": empty_sha,
        "invocation_stderr_sha256": empty_sha,
        "invocation_stdout_preimage": "",
        "invocation_stderr_preimage": "",
        "invocation_exit_code": 0,
        "process_pid": 1234,
        "runtime_summary_path": "fixture-summary.json",
    }


def _wind_form1() -> dict[str, Any]:
    return {
        "schema_version": "drone_behavior_delta_under_wind.v1",
        "audit_id": "wind_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "causal_form": "Form 1a",
        "form1_scope": "drone_physics_or_mission_behavior",
        "condition_kind": "wind_drone_behavior_delta",
        "progress_counted": True,
        "drone_physics_affected": True,
        "raw_trajectory_delta_above_threshold": True,
        "observed_delta_margin_ratio": 3.0,
        "source_binding": {
            "runtime_invocation_evidence_complete": True,
            "runtime_pairing_complete": True,
            "source_boundary_flags_safe": True,
        },
        "runtime_pairing": {
            "command_argv_sha256_equal": True,
            "condition_only_env_delta": True,
        },
        "baseline_runtime_invocation_evidence": _runtime_evidence("baseline"),
        "condition_runtime_invocation_evidence": _runtime_evidence("condition"),
        "metrics": {"max_observed_delta_m": 0.75, "delta_threshold_m": 0.25},
        "requested": {"observed_wind_a_mps": 2.0, "observed_wind_b_mps": 4.0},
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _feasibility(
    status: str,
    *,
    mission_situation_input_digest: str,
    execution_scope: str = "simulator",
    action: str = "return_to_launch",
    parameters: Mapping[str, Any] | None = None,
    sample_index: int = 100,
    evaluated_at: datetime | None = None,
    policy_sha256: str = "c" * 64,
    battery_model_id: str = "fixture_battery_model.v1",
) -> dict[str, Any]:
    runtime_evidence = _runtime_evidence(f"feasibility-{sample_index}")
    evaluated_at = evaluated_at or datetime.now(UTC)
    payload = {
        "schema_version": "missionos_runtime_recovery_action_feasibility.v1",
        "feasibility_status": status,
        "action": action,
        "candidate_parameters": dict(parameters or {}),
        "source_hazard_state_id": "hazard_fixture",
        "source_hazard_state_sha256": "b" * 64,
        "policy_ref": "policy_fixture",
        "policy_sha256": policy_sha256,
        "model_refs": {
            "battery_action_energy": battery_model_id,
            "temperature": None,
        },
        "telemetry_cursor": {
            "cursor_status": "complete",
            "sample_index": sample_index,
            "elapsed_seconds": float(sample_index),
        },
        "evaluated_at": evaluated_at.isoformat(),
        "freshness_deadline": (
            evaluated_at + timedelta(seconds=20)
        ).isoformat(),
        "runtime_invocation_evidence": runtime_evidence,
        "mission_situation_input_digest": mission_situation_input_digest,
        "execution_scope": execution_scope,
        "blocking_reasons": ["fixture_blocked"] if status == "blocked" else [],
        "unverified_reasons": ["fixture_unverified"] if status == "unverified" else [],
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


def test_common_agent_uses_contextual_judge_without_backend_vocabulary() -> None:
    judge = _Judge(_judgment("hold"))
    proposal = MissionAssuranceAgent(judge).evaluate(_synthetic_situation())

    assert proposal.proposed_response_kind == "hold"
    assert proposal.model_inference_invoked is True
    assert proposal.to_dict()["dispatch_authority_created"] is False
    assert judge.prompts[0]["mission_situation"]["observations"]["queue_depth"] == 6
    assert "thresholds_are_inputs_not_final_judgment" in judge.prompts[0][
        "decision_contract"
    ]
    assert (
        judge.prompts[0]["decision_contract"]
        ["human_approval_is_always_a_separate_downstream_boundary"]
        is True
    )
    assert "does not approve or dispatch" in judge.prompts[0]["response_semantics"][
        "replan"
    ]
    common_source = Path("src/intelligence/mission_assurance_agent.py").read_text(
        encoding="utf-8"
    )
    assert "src.runtime.px4_gazebo_route" not in common_source
    assert "backend ==" not in common_source


def test_adk_response_schema_requires_the_complete_judgment_contract() -> None:
    assert MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA["required"] == [
        "proposed_response_kind",
        "parameters",
        "rationale",
        "expected_outcome",
        "uncertainty",
        "operator_question",
    ]
    assert MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA["additionalProperties"] is False
    assert set(
        MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA["properties"][
            "proposed_response_kind"
        ]["enum"]
    ) == {"continue", "hold", "replan", "return", "abort", "operator_escalation"}


def test_px4_hold_is_a_no_dispatch_response_not_an_unbound_action() -> None:
    compilation = compile_mission_response_proposal(
        MissionAssuranceAgent(_Judge(output=_judgment("hold"))).evaluate(
            _synthetic_situation()
        )
    )

    assert compilation["compile_status"] == "no_action_required"
    assert compilation["bounded_action_kind"] is None
    assert compilation["dispatch_request_sent"] is False
    assert compilation["blocking_reasons"] == []


def test_deepseek_mission_assurance_uses_prompt_json_not_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSION_ASSURANCE_AGENT_LLM_BACKEND", "deepseek"
    )

    assert _adk_output_schema() is None


def test_non_deepseek_mission_assurance_keeps_provider_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MISSIONOS_AGENT_MISSION_ASSURANCE_AGENT_LLM_BACKEND", "ollama"
    )

    assert _adk_output_schema() is not None


def test_common_agent_failure_escalates_without_static_action() -> None:
    proposal = MissionAssuranceAgent(_Judge(error=TimeoutError())).evaluate(
        _synthetic_situation()
    )

    assert proposal.proposed_response_kind == "operator_escalation"
    assert proposal.judgment_status == "failed"
    assert proposal.fallback_mode == "operator_escalation_only"
    assert proposal.model_inference_invoked is True
    assert proposal.parameters == {}


def test_px4_adapter_observes_then_compiles_without_choosing() -> None:
    form1 = _wind_form1()
    source_check = knowledge._form1_runtime_delta_source_check(form1)
    situation = observe_form1_mission_situation(
        form1=form1,
        source_check=source_check,
        source_ref="fixture:wind",
        input_digest="d" * 64,
    )
    proposal = MissionAssuranceAgent(_Judge(_judgment("return"))).evaluate(situation)
    compilation = compile_mission_response_proposal(proposal)

    assert situation.observations["behavior_delta"]["condition_kind"].startswith("wind")
    assert compilation["proposed_response_kind"] == "return"
    assert compilation["bounded_action_kind"] == "return_to_launch"
    assert compilation["dispatch_authority_created"] is False


def test_payload_climb_delay_is_advisory_only_and_creates_no_form2a_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path / "source" / "payload.json", _payload_form1())

    def _unexpected_agent():
        raise AssertionError("payload advisory must not enter the action judgment path")

    monkeypatch.setattr(knowledge, "configured_mission_assurance_agent", _unexpected_agent)
    summary = knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
    )

    assert summary["summary_status"] == "form2_advisory_selected"
    selection = summary["response_selection"]
    assert selection["mission_response_kind"] == "advisory"
    assert selection["trigger_level"] == "level_2_inferred"
    assert selection["selected_response_kind"] == "payload_feasibility_advisory"
    assert selection["automatic_dispatch_suppressed"] is True
    assert selection["bounded_action_kind"] is None
    assert summary["operator_approval_token"]["status"] == "missing"
    assert selection["dispatch_ref"] is None
    assert not list(tmp_path.rglob("missionos_form2a_response_selection.json"))
    assert not list(tmp_path.rglob("missionos_form2a_operator_approval_token.json"))


def test_llm_failure_creates_operator_escalation_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(error=TimeoutError())),
    )

    summary = knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
    )

    selection = summary["response_selection"]
    assert summary["summary_status"] == "form2_advisory_selected"
    assert selection["selected_response_kind"] == "operator_escalation"
    assert selection["mission_response_kind"] == "advisory"
    assert selection["model_inference_invoked"] is True
    assert selection["bounded_action_kind"] is None
    assert selection["approval_ref"] is None
    assert selection["dispatch_ref"] is None
    assert not list(tmp_path.rglob("missionos_form2a_operator_approval_token.json"))


@pytest.mark.parametrize("status", ["blocked", "unverified"])
def test_return_loses_eligibility_when_action_feasibility_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    feasibility = _write_json(
        tmp_path / "feasibility" / f"{status}.json",
        _feasibility(
            status,
            mission_situation_input_digest=_file_sha256(source),
        ),
    )
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(_judgment("return"))),
    )

    summary = knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
        action_feasibility_artifact_path=feasibility,
    )

    selection = summary["response_selection"]
    assert summary["summary_status"] == "form2_advisory_selected"
    assert selection["selected_response_kind"] == "operator_escalation"
    assert selection["bounded_action_kind"] is None
    assert selection["action_feasibility_status"] == status
    assert selection["approval_ref"] is None
    assert selection["dispatch_ref"] is None


def test_verified_return_is_only_approval_request_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    feasibility = _write_json(
        tmp_path / "feasibility" / "verified.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
        ),
    )
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(_judgment("return"))),
    )

    summary = knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
        action_feasibility_artifact_path=feasibility,
    )

    selection = summary["response_selection"]
    assert summary["summary_status"] == "form2a_response_selected"
    assert selection["proposed_response_kind"] == "return"
    assert selection["bounded_action_kind"] == "return_to_launch"
    assert selection["action_feasibility_status"] == "verified_feasible"
    assert summary["operator_approval_token"]["status"] == "issued_unconsumed"
    assert summary["authority_boundary"]["dispatch_executed_in_runtime"] is False
    assert summary["authority_boundary"]["physical_execution_invoked"] is False

    consumption = knowledge.run_form2a_action_consumption(artifact_root=tmp_path)
    assert consumption["summary_status"] == "blocked"
    assert (
        "mission_assurance_dispatch_time_revalidation_artifact_missing"
        in consumption["authority_boundary"]["blocking_reasons"]
    )
    assert consumption["authority_boundary"][
        "operator_approval_token_consumed_in_runtime"
    ] is False
    assert consumption["authority_boundary"]["dispatch_executed_in_runtime"] is False


def test_simulator_situation_rejects_hardware_scoped_feasibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    feasibility = _write_json(
        tmp_path / "feasibility" / "hardware.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
            execution_scope="physical_hardware",
        ),
    )
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(_judgment("return"))),
    )

    summary = knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
        action_feasibility_artifact_path=feasibility,
    )

    assert summary["summary_status"] == "form2_advisory_selected"
    assert summary["response_selection"]["selected_response_kind"] == (
        "operator_escalation"
    )
    assert "action_feasibility_execution_scope_mismatch" in summary[
        "response_selection"
    ]["blocking_reasons"]
    assert summary["authority_boundary"]["physical_execution_invoked"] is False


def test_fresh_revalidation_binds_current_feasibility_before_token_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    original = _write_json(
        tmp_path / "feasibility" / "original.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
        ),
    )
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(_judgment("return"))),
    )
    knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
        action_feasibility_artifact_path=original,
    )
    current = _write_json(
        tmp_path / "feasibility" / "current.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
            sample_index=101,
        ),
    )

    revalidation = knowledge.run_form2a_action_revalidation(
        artifact_root=tmp_path,
        current_action_feasibility_artifact_path=current,
    )
    consumption = knowledge.run_form2a_action_consumption(
        artifact_root=tmp_path,
        action_revalidation_artifact_path=revalidation["artifact_path"],
    )

    assert revalidation["schema_version"] == "missionos_core_action_revalidation.v1"
    assert revalidation["revalidation_status"] == "valid"
    assert revalidation["dispatch_authority_created"] is False
    assert consumption["action_consumption"]["action_revalidation_status"] == "valid"
    assert not any(
        reason.startswith(
            (
                "action_revalidation",
                "mission_assurance_dispatch_time_revalidation",
            )
        )
        for reason in consumption["authority_boundary"]["blocking_reasons"]
    )
    assert consumption["authority_boundary"][
        "operator_approval_token_consumed_in_runtime"
    ] is False
    assert consumption["authority_boundary"]["dispatch_executed_in_runtime"] is False


@pytest.mark.parametrize(
    ("current_overrides", "expected_reason"),
    [
        (
            {"policy_sha256": "e" * 64},
            "action_revalidation_policy_drift",
        ),
        (
            {"sample_index": 99},
            "action_revalidation_cursor_regression",
        ),
        (
            {"battery_model_id": "fixture_battery_model.v2"},
            "action_revalidation_model_drift",
        ),
        (
            {"evaluated_at": datetime.now(UTC) - timedelta(minutes=2)},
            "action_revalidation_evidence_stale",
        ),
    ],
)
def test_revalidation_fails_closed_on_policy_cursor_or_freshness_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_overrides: Mapping[str, Any],
    expected_reason: str,
) -> None:
    source = _write_json(tmp_path / "source" / "wind.json", _wind_form1())
    original = _write_json(
        tmp_path / "feasibility" / "original.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
        ),
    )
    monkeypatch.setattr(
        knowledge,
        "configured_mission_assurance_agent",
        lambda: MissionAssuranceAgent(_Judge(_judgment("return"))),
    )
    knowledge.run_form2a_response_selection_from_form1(
        artifact_root=tmp_path,
        form1_artifact_path=source,
        action_feasibility_artifact_path=original,
    )
    current = _write_json(
        tmp_path / "feasibility" / "current.json",
        _feasibility(
            "verified_feasible",
            mission_situation_input_digest=_file_sha256(source),
            **current_overrides,
        ),
    )

    revalidation = knowledge.run_form2a_action_revalidation(
        artifact_root=tmp_path,
        current_action_feasibility_artifact_path=current,
    )

    assert revalidation["revalidation_status"] == "blocked"
    assert expected_reason in revalidation["reasons"]
    assert revalidation["dispatch_authority_created"] is False
    assert revalidation["execution_invoked"] is False
