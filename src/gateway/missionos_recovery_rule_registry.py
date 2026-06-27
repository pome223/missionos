"""File-backed MissionOS runtime recovery rule registry."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


RECOVERY_RULE_REGISTRY_SCHEMA_VERSION = "missionos_recovery_rule_registry_runtime_state.v1"


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


class RecoveryRuleRegistry:
    """Runtime registry for operator-gated MissionOS recovery rules."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def _state(self) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if not state:
            state = {
                "schema_version": RECOVERY_RULE_REGISTRY_SCHEMA_VERSION,
                "registry_status": "initialized",
                "rules": {},
            }
        return state

    def register(self, rule: Mapping[str, Any], *, artifact_path: str) -> dict[str, Any]:
        rule_id = str(rule.get("recovery_rule_id") or "")
        if not rule_id:
            raise ValueError("recovery_rule_id_required")
        state = self._state()
        rules = state.setdefault("rules", {})
        rules[rule_id] = {
            "registered_at": _utc_now(),
            "rule_id": rule_id,
            "rule_ref": f"automatic_recovery_rule:{rule_id}",
            "rule_artifact_path": artifact_path,
            "active_policy_ref": rule.get("active_policy_ref"),
            "bounded_action_ref": rule.get("bounded_action_ref"),
            "recommended_action": rule.get("recommended_action"),
            "operator_approval_required": rule.get("operator_approval_required") is True,
            "recovery_rule_source_projection_sha256": rule.get(
                "runtime_source_projection_sha256"
            )
            or _sha256_json(
                {
                    "schema_version": rule.get("schema_version"),
                    "recovery_rule_id": rule_id,
                    "active_policy_ref": rule.get("active_policy_ref"),
                    "bounded_action_ref": rule.get("bounded_action_ref"),
                    "recommended_action": rule.get("recommended_action"),
                    "operator_approval_required": rule.get("operator_approval_required")
                    is True,
                    "automatic_dispatch_suppressed": rule.get(
                        "automatic_dispatch_suppressed"
                    )
                    is True,
                }
            ),
        }
        state["registry_status"] = "rule_registered"
        state["updated_at"] = _utc_now()
        _write_json(self.state_path, state)
        return dict(rules[rule_id])

    def lookup(self, rule_id: str) -> dict[str, Any]:
        return dict(self._state().get("rules", {}).get(rule_id, {}))

    def select_recovery_rule(
        self,
        policy_decision: Mapping[str, Any],
        mission_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        for rule in self._state().get("rules", {}).values():
            if (
                policy_decision.get("decision_kind") == "operator_gated_recovery_recommendation"
                and rule.get("operator_approval_required") is True
            ):
                return {
                    "schema_version": "missionos_recovery_rule_selection.v1",
                    "selection_status": "selected",
                    "selected_at": _utc_now(),
                    "selected_rule_ref": rule.get("rule_ref"),
                    "selected_rule_artifact_path": rule.get("rule_artifact_path"),
                    "bounded_action_ref": rule.get("bounded_action_ref"),
                    "source_failure_mode_id": mission_state.get("source_failure_mode_id"),
                }
        return {
            "schema_version": "missionos_recovery_rule_selection.v1",
            "selection_status": "no_rule_selected",
            "selected_at": _utc_now(),
            "source_failure_mode_id": mission_state.get("source_failure_mode_id"),
        }


__all__ = ["RECOVERY_RULE_REGISTRY_SCHEMA_VERSION", "RecoveryRuleRegistry"]
