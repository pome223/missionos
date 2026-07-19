from __future__ import annotations

from collections.abc import Mapping

from src.runtime import px4_gazebo_mission_designer_sitl_live_flight_run as live_run
from src.runtime.px4_gazebo_route.recovery_decision_signature import (
    RECOVERY_DECISION_SIGNATURE_VERSION,
    build_semantic_numeric_delta,
    build_semantic_numeric_state,
    build_semantic_recovery_decision_signature,
)
from src.runtime.task_store import TaskStore


def _summary(
    *,
    elapsed_s: float,
    wind_mps: float = 0.0,
    cross_track_m: float = 0.0,
    terrain_margin_m: float = 10.0,
    progress_stall_s: float = 0.0,
    telemetry_stale_count: int = 0,
) -> dict:
    if progress_stall_s > 0.0:
        bucket = {
            "elapsed_start_s": elapsed_s - progress_stall_s,
            "elapsed_end_s": elapsed_s,
            "sample_count": 2,
            "progress_delta_m": 0.0,
        }
        progress_delta_m = 0.0
    else:
        bucket = {
            "elapsed_start_s": elapsed_s - 5.0,
            "elapsed_end_s": elapsed_s,
            "sample_count": 2,
            "progress_delta_m": 5.0,
        }
        progress_delta_m = 5.0
    return {
        "schema_version": "missionos_recovery_window_summary.v1",
        "summary_status": "computed",
        "window_end_elapsed_s": elapsed_s,
        "bucket_s": 5.0,
        "thresholds": {
            "min_terrain_clearance_m": 30.0,
            "terrain_soft_margin_m": 5.0,
            "cross_track_soft_limit_m": 25.0,
            "wind_soft_limit_mps": 6.0,
        },
        "overall": {
            "wind_speed_max_mps": wind_mps,
            "cross_track_max_m": cross_track_m,
            "terrain_clearance_margin_min_m": terrain_margin_m,
            "progress_delta_m": progress_delta_m,
            "trailing_telemetry_stale_count": telemetry_stale_count,
        },
        "hard_breaches": {
            "terrain_clearance_below_minimum": terrain_margin_m < 0.0,
            "battery_critical": False,
            "telemetry_lost": telemetry_stale_count >= 2,
            "obstacle_or_building_risk": False,
            "any": terrain_margin_m < 0.0 or telemetry_stale_count >= 2,
        },
        "soft_signals": {
            "terrain_clearance_near_minimum": 0.0 <= terrain_margin_m <= 5.0,
            "cross_track_above_soft_limit": cross_track_m >= 25.0,
            "progress_non_positive": progress_stall_s > 0.0,
            "battery_drop_above_soft_limit": False,
            "nav_state_changed": False,
            "wind_speed_above_soft_limit": wind_mps >= 6.0,
            "any": (
                0.0 <= terrain_margin_m <= 5.0
                or cross_track_m >= 25.0
                or progress_stall_s > 0.0
                or wind_mps >= 6.0
            ),
        },
        "buckets": [bucket],
    }


def _telemetry(*, elapsed_s: float, return_margin_percent: float = 25.0) -> dict:
    return {
        "elapsed_seconds": elapsed_s,
        "battery": {
            "return_home_projection": {
                "projection_status": "computed",
                "projected_return_reserve_margin_percent": (return_margin_percent),
            }
        },
    }


def _policy() -> dict:
    return {
        "max_wind_speed_mps": 6.0,
        "battery_return_threshold_percent": 20.0,
    }


def _state(
    summary: Mapping,
    *,
    return_margin_percent: float = 25.0,
    prior: Mapping | None = None,
) -> dict:
    elapsed_s = float(summary["window_end_elapsed_s"])
    return build_semantic_numeric_state(
        summary,
        telemetry_snapshot=_telemetry(
            elapsed_s=elapsed_s,
            return_margin_percent=return_margin_percent,
        ),
        recovery_policy=_policy(),
        prior_state=prior,
    )


def test_material_wind_and_cross_track_deltas_create_one_new_signature() -> None:
    initial = _state(_summary(elapsed_s=30.0, wind_mps=7.0, cross_track_m=26.0))
    first_worsened = _state(
        _summary(elapsed_s=60.0, wind_mps=12.0, cross_track_m=45.0),
        prior=initial,
    )
    confirmed_worsened = _state(
        _summary(elapsed_s=66.0, wind_mps=12.0, cross_track_m=45.0),
        prior=first_worsened,
    )
    delta = build_semantic_numeric_delta(first_worsened, confirmed_worsened)
    first_signature = build_semantic_recovery_decision_signature(
        legacy_signature="same-legacy-signature",
        semantic_state=initial,
    )
    second_signature = build_semantic_recovery_decision_signature(
        legacy_signature="same-legacy-signature",
        semantic_state=confirmed_worsened,
    )

    assert build_semantic_numeric_delta(initial, first_worsened)[
        "material_change"
    ] is False
    assert delta["material_change"] is True
    assert delta["changed_dimensions"] == [
        "cross_track_margin_band",
        "wind_margin_band",
    ]
    assert first_signature != second_signature
    assert delta["proposal_created"] is False
    assert delta["approval_created"] is False
    assert delta["dispatch_authority_created"] is False

    stable = _state(
        _summary(elapsed_s=90.0, wind_mps=12.1, cross_track_m=45.2),
        prior=confirmed_worsened,
    )
    stable_delta = build_semantic_numeric_delta(confirmed_worsened, stable)
    stable_signature = build_semantic_recovery_decision_signature(
        legacy_signature="same-legacy-signature",
        semantic_state=stable,
    )
    assert stable_delta["material_change"] is False
    assert stable_signature == second_signature


def test_active_v2_signature_does_not_inherit_legacy_threshold_jitter() -> None:
    state = _state(_summary(elapsed_s=30.0, wind_mps=7.0, cross_track_m=26.0))
    categorical = {
        "battery_critical": False,
        "obstacle_or_building_risk": False,
        "battery_drop_above_soft_limit": False,
        "nav_state": 3,
        "landed": False,
        "recovery_observation_state": "idle",
    }
    before = build_semantic_recovery_decision_signature(
        legacy_signature="legacy-before-transient-stale-sample",
        semantic_state=state,
        categorical_state=categorical,
    )
    after_legacy_jitter = build_semantic_recovery_decision_signature(
        legacy_signature="legacy-after-transient-stale-sample",
        semantic_state=state,
        categorical_state=categorical,
    )
    after_real_state_change = build_semantic_recovery_decision_signature(
        legacy_signature="legacy-after-transient-stale-sample",
        semantic_state=state,
        categorical_state={**categorical, "obstacle_or_building_risk": True},
    )

    assert after_legacy_jitter == before
    assert after_real_state_change != before


def test_wind_limit_jitter_is_absorbed_by_hysteresis() -> None:
    initial = _state(_summary(elapsed_s=30.0, wind_mps=5.9))
    just_above = _state(
        _summary(elapsed_s=40.0, wind_mps=6.1),
        prior=initial,
    )
    just_below = _state(
        _summary(elapsed_s=50.0, wind_mps=5.95),
        prior=just_above,
    )

    bands = [
        state["dimensions"]["wind_margin_band"]["band"]
        for state in (initial, just_above, just_below)
    ]
    assert bands == ["near_limit", "near_limit", "near_limit"]
    assert build_semantic_numeric_delta(initial, just_above)["material_change"] is False
    assert build_semantic_numeric_delta(just_above, just_below)["material_change"] is False


def test_risk_improvement_is_audited_without_opening_a_new_epoch() -> None:
    severe = _state(_summary(elapsed_s=30.0, wind_mps=12.0))
    first_improved = _state(
        _summary(elapsed_s=60.0, wind_mps=7.0),
        prior=severe,
    )
    confirmed_improved = _state(
        _summary(elapsed_s=66.0, wind_mps=7.0),
        prior=first_improved,
    )
    delta = build_semantic_numeric_delta(first_improved, confirmed_improved)

    assert build_semantic_numeric_delta(severe, first_improved)[
        "observed_changed_dimensions"
    ] == []
    assert delta["material_change"] is False
    assert delta["changed_dimensions"] == []
    assert delta["observed_changed_dimensions"] == ["wind_margin_band"]
    assert delta["changes"]["wind_margin_band"]["direction"] == "improving"
    assert delta["changes"]["wind_margin_band"][
        "material_for_decision_epoch"
    ] is False


def test_historical_stale_samples_do_not_override_a_fresh_latest_sample() -> None:
    summary = _summary(
        elapsed_s=30.0,
        wind_mps=7.0,
        telemetry_stale_count=3,
    )
    summary["latest"] = {"telemetry_stale": False}
    state = _state(summary)
    stale = state["dimensions"]["telemetry_stale_band"]

    assert stale["observed_value"] == 0.0
    assert stale["band"] == "below_watch"


def test_battery_return_margin_requires_persistent_adjacent_band_change() -> None:
    initial = _state(
        _summary(elapsed_s=30.0),
        return_margin_percent=18.0,
    )
    first_low = _state(
        _summary(elapsed_s=40.0),
        return_margin_percent=7.0,
        prior=initial,
    )
    confirmed_low = _state(
        _summary(elapsed_s=50.0),
        return_margin_percent=7.0,
        prior=first_low,
    )

    first_dimension = first_low["dimensions"]["battery_return_margin_band"]
    confirmed_dimension = confirmed_low["dimensions"]["battery_return_margin_band"]
    assert first_dimension["band"] == "below_watch"
    assert first_dimension["pending_band"] == "near_limit"
    assert first_dimension["pending_observations"] == 1
    assert build_semantic_numeric_delta(initial, first_low)["material_change"] is False
    assert confirmed_dimension["band"] == "near_limit"
    assert confirmed_dimension["pending_band"] is None
    assert build_semantic_numeric_delta(first_low, confirmed_low)["changed_dimensions"] == [
        "battery_return_margin_band"
    ]


def test_persistent_progress_stall_creates_a_new_epoch() -> None:
    initial = _state(_summary(elapsed_s=30.0, progress_stall_s=10.0))
    first_stalled = _state(
        _summary(elapsed_s=65.0, progress_stall_s=35.0),
        prior=initial,
    )
    confirmed_stalled = _state(
        _summary(elapsed_s=70.0, progress_stall_s=35.0),
        prior=first_stalled,
    )
    delta = build_semantic_numeric_delta(first_stalled, confirmed_stalled)

    assert build_semantic_numeric_delta(initial, first_stalled)[
        "material_change"
    ] is False
    assert confirmed_stalled["dimensions"]["progress_stall_band"]["band"] == (
        "above_1_5x"
    )
    assert confirmed_stalled["dimensions"]["progress_stall_band"][
        "breach_persistence_band"
    ] == "above_30s"
    assert delta["changed_dimensions"] == ["progress_stall_band"]


def test_same_band_rapid_worsening_creates_one_directional_epoch() -> None:
    initial = _state(_summary(elapsed_s=30.0, wind_mps=6.3))
    rapidly_worsening = _state(
        _summary(elapsed_s=40.0, wind_mps=7.3),
        prior=initial,
    )
    delta = build_semantic_numeric_delta(initial, rapidly_worsening)

    assert initial["dimensions"]["wind_margin_band"]["band"] == (
        "limit_to_1_25x"
    )
    assert rapidly_worsening["dimensions"]["wind_margin_band"]["band"] == (
        "limit_to_1_25x"
    )
    assert delta["changed_dimensions"] == ["wind_margin_band"]
    assert delta["changes"]["wind_margin_band"]["reasons"] == ["trend_worsened"]
    assert build_semantic_recovery_decision_signature(
        legacy_signature="initial",
        semantic_state=initial,
    ) != build_semantic_recovery_decision_signature(
        legacy_signature="rapidly-worsening",
        semantic_state=rapidly_worsening,
    )


def test_urgent_time_to_limit_is_audited_in_the_directional_epoch() -> None:
    initial = _state(_summary(elapsed_s=30.0, cross_track_m=7.5))
    converging = _state(
        _summary(elapsed_s=35.0, cross_track_m=12.0),
        prior=initial,
    )
    delta = build_semantic_numeric_delta(initial, converging)

    dimension = converging["dimensions"]["cross_track_margin_band"]
    assert dimension["band"] == "below_watch"
    assert dimension["time_to_limit_band"] == "within_30s"
    assert delta["changed_dimensions"] == ["cross_track_margin_band"]
    assert delta["changes"]["cross_track_margin_band"]["reasons"] == [
        "trend_worsened",
        "time_to_limit_worsened",
    ]


def test_same_band_minor_change_keeps_direction_and_signature_stable() -> None:
    initial = _state(_summary(elapsed_s=30.0, wind_mps=7.0))
    minor = _state(
        _summary(elapsed_s=40.0, wind_mps=7.1),
        prior=initial,
    )

    assert minor["dimensions"]["wind_margin_band"]["trend"] == "stable"
    assert build_semantic_numeric_delta(initial, minor)["material_change"] is False
    assert build_semantic_recovery_decision_signature(
        legacy_signature="initial",
        semantic_state=initial,
    ) == build_semantic_recovery_decision_signature(
        legacy_signature="minor",
        semantic_state=minor,
    )


def test_subthreshold_stall_persistence_does_not_change_decision_signature() -> None:
    moving = _state(_summary(elapsed_s=30.0, progress_stall_s=0.0))
    short_pause = _state(
        _summary(elapsed_s=35.0, progress_stall_s=5.0),
        prior=moving,
    )

    assert short_pause["dimensions"]["progress_stall_band"]["band"] == (
        "below_watch"
    )
    assert short_pause["dimensions"]["progress_stall_band"][
        "breach_persistence_band"
    ] == "under_10s"
    assert build_semantic_numeric_delta(moving, short_pause)[
        "material_change"
    ] is False
    assert build_semantic_recovery_decision_signature(
        legacy_signature="moving",
        semantic_state=moving,
    ) == build_semantic_recovery_decision_signature(
        legacy_signature="short-pause",
        semantic_state=short_pause,
    )


def _raw_snapshot(*, sample_index: int, elapsed_s: float, wind_mps: float) -> dict:
    return {
        "sample_index": sample_index,
        "elapsed_seconds": elapsed_s,
        "progress_m": 50.0 + sample_index * 10.0,
        "local_x_m": 0.0,
        "local_y_m": 50.0 + sample_index * 10.0,
        "local_z_m": -30.0,
        "altitude_above_home_m": 30.0,
        "distance_to_home_m": 50.0,
        "battery_remaining_percent": 80.0,
        "battery_remaining_delta_percent": -2.0,
        "heartbeat_observed": True,
        "nav_state": 3,
        "arming_state": 2,
        "landed": False,
        "wind_speed_mps": wind_mps,
    }


def _skipped_agent_result() -> dict:
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_skipped",
        "assessment": {},
        "agent_invocations": [],
        "blocking_reasons": ["fixture_no_proposal"],
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def _proposal_agent_result() -> dict:
    return {
        "schema_version": "missionos_runtime_recovery_agent_result.v1",
        "runtime_status": "proposal_guardrail_passed",
        "blocking_reasons": [],
        "assessment": {
            "assessment_status": "proposal_guardrail_passed",
            "selected_bounded_action": "adjust_altitude",
            "requires_human_approval": True,
            "proposed_parameters": {"target_altitude_m": 45.0},
        },
        "agent_output": {"intent": "runtime_recovery"},
        "agent_invocations": [
            {
                "agent_name": "missionos_runtime_recovery_agent",
                "provider": "fixture_hosted_model",
                "model_id": "fixture-model",
                "invocation_kind": "fixture",
            }
        ],
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def test_semantic_only_runtime_delta_invokes_once_without_creating_authority(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_semantic_numeric_runtime_delta",
        kind="contract_test",
        title="Semantic numeric runtime delta",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0),
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=12.0),
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=3, elapsed_s=76.0, wind_mps=12.0),
    )

    changed = store.get(task["task_id"])
    assert changed is not None
    artifacts = changed["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 3]
    assert bridge["decision_signature_version"] == (RECOVERY_DECISION_SIGNATURE_VERSION)
    assert bridge["semantic_numeric_delta"]["changed_dimensions"] == ["wind_margin_band"]
    assert bridge["signature_shadow_comparison"] == {
        "legacy_changed": False,
        "semantic_v2_changed": True,
        "semantic_only_material_change": True,
    }
    assert "missionos_runtime_recovery_last_proposal" not in artifacts
    assert "missionos_runtime_recovery_dispatch_receipt" not in artifacts
    assert bridge["dispatch_authority_created"] is False

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=4, elapsed_s=100.0, wind_mps=12.1),
    )
    stable = store.get(task["task_id"])
    assert stable is not None
    stable_bridge = stable["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 3]
    assert stable_bridge["agent_refresh_status"] == "decision_unchanged"


def test_same_band_directional_runtime_delta_invokes_once(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_same_band_directional_runtime_delta",
        kind="contract_test",
        title="Same-band directional runtime delta",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    for snapshot in (
        _raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=6.3),
        _raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=7.3),
        _raw_snapshot(sample_index=3, elapsed_s=100.0, wind_mps=7.3),
    ):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot=snapshot,
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 2]
    assert len(bridge["judged_recovery_decision_signatures"]) == 2
    assert bridge["agent_refresh_status"] in {
        "decision_already_judged",
        "decision_unchanged",
    }
    assert "missionos_runtime_recovery_last_proposal" not in artifacts
    assert "missionos_runtime_recovery_dispatch_receipt" not in artifacts
    assert bridge["dispatch_authority_created"] is False


def test_persistent_battery_margin_delta_overrides_boolean_no_news(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_semantic_battery_margin_delta",
        kind="contract_test",
        title="Semantic battery return margin delta",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    initial = {
        **_raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=0.0),
        "progress_m": 60.0,
        "distance_to_home_m": 60.0,
        "battery_remaining_percent": 35.0,
    }
    first_low = {
        **_raw_snapshot(sample_index=2, elapsed_s=71.0, wind_mps=0.0),
        "progress_m": 70.0,
        "distance_to_home_m": 70.0,
        "battery_remaining_percent": 24.0,
    }
    confirmed_low = {
        **first_low,
        "sample_index": 3,
        "elapsed_seconds": 77.0,
        "progress_m": 76.0,
    }
    for snapshot in (initial, first_low, confirmed_low):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot=snapshot,
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [3]
    assert "battery_return_margin_band" in bridge["semantic_numeric_delta"][
        "changed_dimensions"
    ]
    assert bridge["decision_epoch_reason"] == "semantic_numeric_material_change"
    assert bridge["agent_refresh_status"] == "agent_invoked"
    assert bridge["dispatch_authority_created"] is False


def test_semantic_delta_does_not_mint_a_second_proposal_while_awaiting_approval(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_semantic_delta_approval_wait",
        kind="contract_test",
        title="Semantic delta approval wait",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _proposal_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0),
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_proposal = first["artifacts"]["missionos_runtime_recovery_last_proposal"]

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=12.0),
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=3, elapsed_s=76.0, wind_mps=12.0),
    )
    waiting = store.get(task["task_id"])
    assert waiting is not None
    artifacts = waiting["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1]
    assert (
        artifacts["missionos_runtime_recovery_last_proposal"]["proposal_id"]
        == first_proposal["proposal_id"]
    )
    assert bridge["agent_refresh_status"] == "awaiting_operator_approval"
    assert bridge["semantic_numeric_delta"]["material_change"] is True
    assert bridge["recovery_decision_signature"] != bridge["last_recovery_decision_signature"]
    assert "missionos_runtime_recovery_dispatch_receipt" not in artifacts
    assert not any("approval" in key for key in artifacts)


def test_origin_drift_stales_proposal_without_reinvoking_same_decision(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_same_decision_after_origin_drift",
        kind="contract_test",
        title="Same decision after proposal origin drift",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _proposal_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    initial = _raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0)
    drifted = {
        **_raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=7.0),
        "local_y_m": 100.0,
    }
    stable = {
        **_raw_snapshot(sample_index=3, elapsed_s=100.0, wind_mps=7.0),
        "local_y_m": 101.0,
    }
    for snapshot in (initial, drifted, stable):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot=snapshot,
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    proposal = artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1]
    assert proposal["proposal_status"] == "stale"
    assert "runtime_recovery_proposal_origin_drift_exceeded" in proposal[
        "invalidation_reasons"
    ]
    assert bridge["agent_refresh_status"] == "decision_unchanged"


def test_legacy_hard_breach_cannot_reinvoke_an_unchanged_v2_decision(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_same_v2_after_legacy_hard_breach",
        kind="contract_test",
        title="Same semantic decision after legacy hard breach",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _proposal_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    monkeypatch.setattr(
        live_run,
        "build_semantic_recovery_decision_signature",
        lambda **_kwargs: "semantic-v2-unchanged",
    )
    initial = _raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0)
    first_transient_stale = {
        **_raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=7.0),
        "heartbeat_observed": False,
        "telemetry_stale": True,
    }
    confirmed_legacy_hard_breach = {
        **_raw_snapshot(sample_index=3, elapsed_s=76.0, wind_mps=7.0),
        "heartbeat_observed": False,
        "telemetry_stale": True,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=initial,
    )
    first = store.get(task["task_id"])
    assert first is not None
    first_signature = first["artifacts"][
        "missionos_runtime_recovery_agent_live_bridge"
    ]["recovery_decision_signature"]

    for snapshot in (first_transient_stale, confirmed_legacy_hard_breach):
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot=snapshot,
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    artifacts = stored["artifacts"]
    bridge = artifacts["missionos_runtime_recovery_agent_live_bridge"]
    proposal = artifacts["missionos_runtime_recovery_last_proposal"]
    assert invocations == [1]
    assert bridge["recovery_decision_signature"] == first_signature
    assert bridge["agent_refresh_status"] == "decision_unchanged"
    assert proposal["proposal_status"] in {"stale", "superseded"}
    assert bridge["dispatch_authority_created"] is False


def test_waiting_poll_does_not_replace_the_last_judged_signature(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_waiting_poll_keeps_judged_signature",
        kind="contract_test",
        title="Waiting poll keeps judged decision baseline",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    initial = _raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0)
    transient_nav = {
        **_raw_snapshot(sample_index=2, elapsed_s=45.0, wind_mps=7.0),
        "nav_state": 4,
    }
    back_to_judged_state = _raw_snapshot(
        sample_index=3,
        elapsed_s=70.0,
        wind_mps=7.0,
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=initial,
    )
    first = store.get(task["task_id"])
    assert first is not None
    judged_signature = first["artifacts"][
        "missionos_runtime_recovery_agent_live_bridge"
    ]["last_recovery_decision_signature"]

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=transient_nav,
    )
    waiting = store.get(task["task_id"])
    assert waiting is not None
    waiting_bridge = waiting["artifacts"][
        "missionos_runtime_recovery_agent_live_bridge"
    ]
    assert waiting_bridge["agent_refresh_status"] == "waiting"
    assert waiting_bridge["recovery_decision_signature"] != judged_signature
    assert waiting_bridge["last_recovery_decision_signature"] == judged_signature

    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=back_to_judged_state,
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1]
    assert bridge["recovery_decision_signature"] == judged_signature
    assert bridge["last_recovery_decision_signature"] == judged_signature
    assert bridge["agent_refresh_status"] == "decision_unchanged"


def test_recurring_material_signature_reuses_its_prior_judgment(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_recurring_signature_reuses_judgment",
        kind="contract_test",
        title="Recurring signature reuses prior judgment",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    snapshots = (
        _raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0),
        _raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=12.0),
        _raw_snapshot(sample_index=3, elapsed_s=76.0, wind_mps=12.0),
        _raw_snapshot(sample_index=4, elapsed_s=100.0, wind_mps=7.0),
        _raw_snapshot(sample_index=5, elapsed_s=106.0, wind_mps=7.0),
        _raw_snapshot(sample_index=6, elapsed_s=130.0, wind_mps=12.0),
        _raw_snapshot(sample_index=7, elapsed_s=136.0, wind_mps=12.0),
    )
    for snapshot in snapshots:
        live_run._attach_auto_runtime_recovery_agent_proposal(
            store=store,
            task_id=task["task_id"],
            snapshot=snapshot,
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1, 3]
    assert bridge["agent_refresh_status"] in {
        "decision_already_judged",
        "decision_unchanged",
    }
    assert len(bridge["judged_recovery_decision_signatures"]) == 2
    assert bridge["dispatch_authority_created"] is False


def test_semantic_delta_is_suppressed_while_recovery_is_in_progress(
    tmp_path,
    monkeypatch,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        task_id="task_semantic_delta_recovery_in_progress",
        kind="contract_test",
        title="Semantic delta recovery in progress",
        status="running",
    )
    invocations: list[int] = []

    def _agent(**kwargs) -> dict:
        invocations.append(int(kwargs["telemetry_snapshot"]["sample_index"]))
        return _skipped_agent_result()

    monkeypatch.setattr(
        live_run,
        "_run_auto_runtime_recovery_agent_with_timeout",
        _agent,
    )
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=_raw_snapshot(sample_index=1, elapsed_s=40.0, wind_mps=7.0),
    )
    in_progress = {
        **_raw_snapshot(sample_index=2, elapsed_s=70.0, wind_mps=12.0),
        "operator_recovery_request_observed": True,
        "operator_recovery_action": "adjust_altitude",
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_assist_attempted": True,
        "operator_recovery_assist_status": "streaming",
        "operator_recovery_target_reached": False,
        "operator_recovery_resume_auto_status": "not_attempted",
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=in_progress,
    )
    confirmed_in_progress = {
        **in_progress,
        "sample_index": 3,
        "elapsed_seconds": 76.0,
        "progress_m": 80.0,
    }
    live_run._attach_auto_runtime_recovery_agent_proposal(
        store=store,
        task_id=task["task_id"],
        snapshot=confirmed_in_progress,
    )

    stored = store.get(task["task_id"])
    assert stored is not None
    bridge = stored["artifacts"]["missionos_runtime_recovery_agent_live_bridge"]
    assert invocations == [1]
    assert bridge["semantic_numeric_delta"]["material_change"] is True
    assert bridge["agent_refresh_status"] == "recovery_in_progress"
    assert bridge["dispatch_authority_created"] is False
