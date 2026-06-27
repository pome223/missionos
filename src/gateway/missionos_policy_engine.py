"""File-backed MissionOS runtime policy engine."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


POLICY_ENGINE_STATE_SCHEMA_VERSION = "missionos_policy_engine_runtime_state.v1"
POLICY_ENGINE_REPLAY_SCHEMA_VERSION = "missionos_policy_engine_replay.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True).encode("utf-8")).hexdigest()


class MissionOSPolicyEngine:
    """Minimal runtime policy engine used by MissionOS authority promotion."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def load_active_policy_version(
        self,
        active_policy: Mapping[str, Any],
        *,
        artifact_path: str,
    ) -> dict[str, Any]:
        policy_version_id = str(active_policy.get("policy_version_id") or "")
        if not policy_version_id:
            raise ValueError("policy_version_id_required")
        state = {
            "schema_version": POLICY_ENGINE_STATE_SCHEMA_VERSION,
            "engine_status": "active_policy_loaded",
            "loaded_at": _utc_now(),
            "active_policy_version_id": policy_version_id,
            "active_policy_ref": f"active_policy_version:{policy_version_id}",
            "active_policy_artifact_path": artifact_path,
            "policy_update_candidate_ref": active_policy.get("policy_update_candidate_ref"),
            "source_lesson_ref": active_policy.get("source_lesson_ref"),
            "rollback_ref": active_policy.get("rollback_ref"),
            "active_policy_source_projection_sha256": active_policy.get(
                "runtime_source_projection_sha256"
            )
            or _sha256_json(
                {
                    "schema_version": active_policy.get("schema_version"),
                    "policy_version_id": policy_version_id,
                    "policy_update_candidate_ref": active_policy.get(
                        "policy_update_candidate_ref"
                    ),
                    "approval_ref": active_policy.get("approval_ref"),
                    "rollback_ref": active_policy.get("rollback_ref"),
                    "source_lesson_ref": active_policy.get("source_lesson_ref"),
                    "operator_approval_required": active_policy.get(
                        "operator_approval_required"
                    )
                    is True,
                }
            ),
        }
        _write_json(self.state_path, state)
        return state

    def evaluate(self, mission_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if state.get("engine_status") == "active_policy_loaded":
            decision_kind = "operator_gated_recovery_recommendation"
            policy_loaded = True
            recommended_action = "inspect_failure_receipt_then_operator_gated_recovery"
        else:
            decision_kind = "diagnostic_only_no_runtime_policy"
            policy_loaded = False
            recommended_action = "preserve_operator_review"
        return {
            "schema_version": "missionos_policy_decision.v1",
            "evaluated_at": _utc_now(),
            "policy_loaded": policy_loaded,
            "decision_kind": decision_kind,
            "recommended_action": recommended_action,
            "source_failure_mode_id": mission_state.get("source_failure_mode_id"),
            "active_policy_ref": state.get("active_policy_ref"),
        }

    def replay_before_after(self, mission_state: Mapping[str, Any]) -> dict[str, Any]:
        before_path = self.state_path.with_suffix(".before.json")
        after_path = self.state_path
        before_engine = MissionOSPolicyEngine(before_path)
        before = before_engine.evaluate(mission_state)
        after = self.evaluate(mission_state)
        replay = {
            "schema_version": POLICY_ENGINE_REPLAY_SCHEMA_VERSION,
            "replayed_at": _utc_now(),
            "mission_state": dict(mission_state),
            "before_policy_decision": before,
            "after_policy_decision": after,
            "baseline_state_kind": "empty_runtime_policy_state",
            "policy_loaded_from_empty_baseline": before.get("policy_loaded") is not True
            and after.get("policy_loaded") is True,
            "before_state_path": str(before_path),
            "after_state_path": str(after_path),
        }
        replay_path = self.state_path.parent / "policy_engine_replay.json"
        _write_json(replay_path, replay)
        replay["replay_artifact_path"] = str(replay_path)
        return replay


__all__ = [
    "MissionOSPolicyEngine",
    "POLICY_ENGINE_REPLAY_SCHEMA_VERSION",
    "POLICY_ENGINE_STATE_SCHEMA_VERSION",
]
