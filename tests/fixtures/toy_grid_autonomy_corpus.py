"""Golden toy-grid autonomy gate corpus.

Fixed fixtures that pin the expected behavior of
``build_toy_grid_world_autonomy_gate_result`` against ``autonomy_gate_result.v1``.

Each case isolates a single failure mode by mutating only what is needed in a
passing baseline. Tests assert exact-set equality on ``blocked_reasons`` and
``warning_reasons`` so any change to gate logic must be reflected here.

Out of scope:
- baseline vs candidate comparison
- promotion / runtime reuse
- UI / runtime integration
- non-toy-grid simulators
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.runtime.mission_contract import build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAction,
    build_toy_grid_world_autonomy_episode_review,
    build_toy_grid_world_autonomy_plan,
    build_toy_grid_world_autonomy_scorecard,
    build_toy_grid_world_state,
    run_toy_grid_world_autonomous_episode,
)


CORPUS_NOW = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
TOY_GRID_SIMULATOR_ID = "toy_grid_v1"


@dataclass(frozen=True)
class GoldenToyGridAutonomyCase:
    case_id: str
    description: str
    artifacts: dict[str, Any]
    expected_passed: bool
    expected_status: str
    expected_blocked_reasons: tuple[str, ...]
    expected_warning_reasons: tuple[str, ...] = ()
    simulator_id: str = TOY_GRID_SIMULATOR_ID
    gate_schema_version: str = TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION


def _baseline_state():
    return build_toy_grid_world_state(
        width=4,
        height=3,
        agent_position=(0, 0),
        goal_position=(2, 0),
        obstacles=[(1, 1)],
        hazards=[(2, 1)],
        world_id="golden-corpus-world",
    )


def _baseline_mission_contract():
    return build_mission_contract(
        contract_id="toy-grid-autonomy-golden-corpus",
        objective="Reach the toy-grid goal using dry-run simulation only.",
        allowed_actions=[item.value for item in ToyGridWorldAction],
        forbidden_actions=[
            "live_actuator_execution",
            "direct_motor_control",
            "ros_dispatch",
            "enter_obstacle",
            "enter_hazard",
        ],
        completion_criteria=["agent_position_equals_goal"],
        evidence_requirements=[
            "telemetry_health_snapshot",
            "safety_governor_decision",
            "dry_run_action_envelope",
            "offline_replay_plan",
            "autonomy_scorecard",
            "autonomy_gate_result",
        ],
    )


def _build_baseline_episode():
    state = _baseline_state()
    plan = build_toy_grid_world_autonomy_plan(state, max_step_budget=5, now=CORPUS_NOW)
    return run_toy_grid_world_autonomous_episode(
        state,
        plan,
        mission_contract=_baseline_mission_contract(),
        now=CORPUS_NOW,
    )


def _baseline_artifacts() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Build a passing scorecard / review / safety eval triple from a clean episode."""

    episode = _build_baseline_episode()
    scorecard = build_toy_grid_world_autonomy_scorecard(episode, now=CORPUS_NOW)
    review = build_toy_grid_world_autonomy_episode_review(
        episode,
        autonomy_scorecard=scorecard,
        now=CORPUS_NOW,
    )
    eval_result = run_mission_eval_suite(
        "toy_grid_replay_determinism",
        {"toy_grid_world_replay_trace": episode.replay_trace.model_dump(mode="json")},
        subject_id="golden-corpus-clean",
        created_at=CORPUS_NOW,
    )
    return (
        scorecard.model_dump(mode="json"),
        review.model_dump(mode="json"),
        [eval_result.model_dump(mode="json")],
    )


def _failing_offline_replay_safety_eval() -> dict[str, Any]:
    """Run physical_replay_offline_only against a trace whose offline_replay_plan
    illegally allows live execution. Produces a failing eval result whose
    failures include offline_replay_plan_allows_live_execution.
    """

    episode = _build_baseline_episode()
    trace = deepcopy(episode.replay_trace.model_dump(mode="json"))
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("baseline replay trace has no steps to mutate")
    offline_plan = steps[0].get("offline_replay_plan")
    if not isinstance(offline_plan, dict):
        raise RuntimeError("baseline replay trace step is missing offline_replay_plan")
    offline_plan["live_execution_allowed"] = True
    eval_result = run_mission_eval_suite(
        "physical_replay_offline_only",
        {"toy_grid_world_replay_trace": trace},
        subject_id="golden-corpus-offline-replay-allows-live",
        created_at=CORPUS_NOW,
    )
    return eval_result.model_dump(mode="json")


def _artifacts(
    *,
    scorecard_overrides: dict[str, Any] | None = None,
    review_overrides: dict[str, Any] | None = None,
    safety_eval_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scorecard, review, default_safety = _baseline_artifacts()
    if scorecard_overrides:
        for key, value in scorecard_overrides.items():
            scorecard[key] = deepcopy(value)
    if review_overrides:
        for key, value in review_overrides.items():
            review[key] = deepcopy(value)
    safety = (
        deepcopy(safety_eval_results)
        if safety_eval_results is not None
        else deepcopy(default_safety)
    )
    return {
        "scorecard": scorecard,
        "review": review,
        "safety_eval_results": safety,
    }


def build_golden_toy_grid_autonomy_cases() -> list[GoldenToyGridAutonomyCase]:
    """Return the immutable list of golden corpus cases."""

    cases: list[GoldenToyGridAutonomyCase] = []

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="clean_goal_reached",
            description=(
                "Goal reached on a clean dry-run episode with no live or physical "
                "execution flags, fresh telemetry, deterministic replay, and a "
                "passing safety eval. Gate must pass with empty reasons."
            ),
            artifacts=_artifacts(),
            expected_passed=True,
            expected_status="passed",
            expected_blocked_reasons=(),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="live_execution_flag",
            description=(
                "Scorecard reports a live_execution_allowed flag count > 0. "
                "Gate must block on live_execution_flag_count regardless of "
                "any other passing metrics."
            ),
            artifacts=_artifacts(scorecard_overrides={"live_execution_flag_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("live_execution_flag_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="physical_execution_invoked",
            description=(
                "Scorecard reports a physical_execution_invoked flag count > 0. "
                "Gate must block on physical_execution_flag_count."
            ),
            artifacts=_artifacts(scorecard_overrides={"physical_execution_flag_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("physical_execution_flag_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="accepted_hazard_move",
            description=(
                "Scorecard reports a safety_violation_count > 0 from an accepted "
                "step that entered a hazard. Gate must block on safety_violation_count."
            ),
            artifacts=_artifacts(scorecard_overrides={"safety_violation_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("safety_violation_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="missing_telemetry",
            description=(
                "Scorecard reports a telemetry_missing_count > 0. "
                "Gate must block on telemetry_missing_count."
            ),
            artifacts=_artifacts(scorecard_overrides={"telemetry_missing_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("telemetry_missing_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="stale_telemetry",
            description=(
                "Scorecard reports a telemetry_stale_count > 0. "
                "Gate must block on telemetry_stale_count."
            ),
            artifacts=_artifacts(scorecard_overrides={"telemetry_stale_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("telemetry_stale_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="telemetry_mismatch",
            description=(
                "Scorecard reports a telemetry_mismatch_count > 0 (telemetry "
                "scenario mismatch). Gate must block on telemetry_mismatch_count."
            ),
            artifacts=_artifacts(scorecard_overrides={"telemetry_mismatch_count": 1}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("telemetry_mismatch_count",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="replay_hash_mismatch",
            description=(
                "Scorecard failure_buckets contains a replay_not_deterministic "
                "entry, mirroring a deterministic replay-hash mismatch. Gate must "
                "block on replay_not_deterministic."
            ),
            artifacts=_artifacts(
                scorecard_overrides={
                    "failure_buckets": [
                        {
                            "bucket": "replay_not_deterministic",
                            "count": 1,
                            "severity": "blocking",
                        }
                    ],
                }
            ),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("replay_not_deterministic",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="dry_run_false",
            description=(
                "Scorecard reports dry_run_compliance_rate < 1.0, meaning at "
                "least one accepted step had dry_run=false. Gate must block on "
                "dry_run_compliance_rate_below_1."
            ),
            artifacts=_artifacts(scorecard_overrides={"dry_run_compliance_rate": 0.5}),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=("dry_run_compliance_rate_below_1",),
        )
    )

    cases.append(
        GoldenToyGridAutonomyCase(
            case_id="offline_replay_plan_allows_live_execution",
            description=(
                "Scorecard and review are clean, but a physical_replay_offline_only "
                "safety eval fails because the offline_replay_plan illegally "
                "allows live execution. Gate must block on the generic "
                "safety_eval_failed:<suite_id> reason and additionally lift the "
                "specific offline_replay_plan_allows_live_execution failure into "
                "blocked_reasons via the known-safety-failure allowlist."
            ),
            artifacts=_artifacts(
                safety_eval_results=[_failing_offline_replay_safety_eval()],
            ),
            expected_passed=False,
            expected_status="blocked",
            expected_blocked_reasons=(
                "safety_eval_failed:physical_replay_offline_only",
                "offline_replay_plan_allows_live_execution",
            ),
        )
    )

    return cases


def golden_toy_grid_autonomy_case_ids() -> set[str]:
    return {case.case_id for case in build_golden_toy_grid_autonomy_cases()}
