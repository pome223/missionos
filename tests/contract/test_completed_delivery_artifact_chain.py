from pathlib import Path

from src.runtime.delivery_episode_review import (
    DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION,
    DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED,
    DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK,
    DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK,
    DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED,
    DELIVERY_SCORECARD_SCHEMA_VERSION,
)
from src.runtime.delivery_recovery_decision import (
    DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import (
    build_completed_delivery_artifact_chain,
)


FORBIDDEN_AUTHORITY_ARTIFACTS = {
    "approval",
    "promotion_package",
    "reuse_plan",
    "runtime_reuse",
}


def test_completed_delivery_review_and_recovery_decision_share_one_chain(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Completed delivery artifact chain contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    chain = build_completed_delivery_artifact_chain(
        store=store,
        task_id=task["task_id"],
    )
    scorecard = chain.review_artifacts["delivery_scorecard"]
    review = chain.review_artifacts["delivery_episode_review"]

    assert scorecard["schema_version"] == DELIVERY_SCORECARD_SCHEMA_VERSION
    assert review["schema_version"] == DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    assert scorecard["delivery_completed"] is True
    assert scorecard["dropoff_completed"] is True
    assert scorecard["staged_ascent_completed"] is True
    assert review["status"] == "passed"
    assert review["passed"] is True
    assert DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED in review["buckets"]
    assert DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED in review["warning_buckets"]
    assert DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK in review["warning_buckets"]
    assert DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK in review["warning_buckets"]

    stored = store.get(task["task_id"])
    decision = chain.decision_artifacts["delivery_recovery_decision"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert FORBIDDEN_AUTHORITY_ARTIFACTS.isdisjoint(stored["artifacts"])
    assert decision["schema_version"] == DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    assert decision["decision_source"] == "delivery_episode_review"
    assert decision["primary_action"] == "completed_no_recovery_needed"
    assert decision["completed_no_recovery_needed"] is True
    assert decision["continue_recommended"] is True
    assert decision["recommendations_only"] is True

    for artifact in (scorecard, decision):
        assert artifact["hardware_target_allowed"] is False
        assert artifact["physical_execution_invoked"] is False
        assert artifact["mavlink_dispatch_allowed"] is False
        assert artifact["ros_dispatch_allowed"] is False
        assert artifact["actuator_execution_allowed"] is False
        assert artifact["approval_free_stronger_execution_allowed"] is False
