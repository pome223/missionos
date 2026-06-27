#!/usr/bin/env python3
"""Apply MissionOS policy artifacts to runtime engine state."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.gateway.missionos_policy_engine import MissionOSPolicyEngine
from src.gateway.missionos_recovery_rule_registry import RecoveryRuleRegistry


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_json(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _policy_source_projection(policy: dict) -> dict:
    return {
        "schema_version": policy.get("schema_version"),
        "policy_version_id": policy.get("policy_version_id"),
        "policy_update_candidate_ref": policy.get("policy_update_candidate_ref"),
        "approval_ref": policy.get("approval_ref"),
        "rollback_ref": policy.get("rollback_ref"),
        "source_lesson_ref": policy.get("source_lesson_ref"),
        "operator_approval_required": policy.get("operator_approval_required") is True,
    }


def _rule_source_projection(rule: dict) -> dict:
    return {
        "schema_version": rule.get("schema_version"),
        "recovery_rule_id": rule.get("recovery_rule_id"),
        "active_policy_ref": rule.get("active_policy_ref"),
        "bounded_action_ref": rule.get("bounded_action_ref"),
        "recommended_action": rule.get("recommended_action"),
        "operator_approval_required": rule.get("operator_approval_required") is True,
        "automatic_dispatch_suppressed": rule.get("automatic_dispatch_suppressed") is True,
    }


def _authority_source_projection(authority: dict) -> dict:
    return {
        "schema_version": authority.get("schema_version"),
        "dispatch_authority_id": authority.get("dispatch_authority_id"),
        "dispatch_ref": authority.get("dispatch_ref"),
        "active_policy_ref": authority.get("active_policy_ref"),
        "automatic_recovery_rule_ref": authority.get("automatic_recovery_rule_ref"),
        "approval_ref": authority.get("approval_ref"),
        "bounded_action_ref": authority.get("bounded_action_ref"),
        "operator_approval_required": authority.get("operator_approval_required") is True,
        "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--active-policy-path", required=True)
    parser.add_argument("--active-policy-artifact-path", required=True)
    parser.add_argument("--recovery-rule-path", required=True)
    parser.add_argument("--recovery-rule-artifact-path", required=True)
    parser.add_argument("--dispatch-authority-path", required=True)
    parser.add_argument("--dispatch-authority-artifact-path", required=True)
    parser.add_argument("--runtime-state-dir", required=True)
    parser.add_argument("--source-failure-mode-id", required=True)
    args = parser.parse_args()

    runtime_state_dir = Path(args.runtime_state_dir)
    runtime_state_dir.mkdir(parents=True, exist_ok=True)

    active_policy = _read_json(Path(args.active_policy_path))
    recovery_rule = _read_json(Path(args.recovery_rule_path))
    dispatch_authority = _read_json(Path(args.dispatch_authority_path))
    active_policy_projection_sha256 = _sha256_json(_policy_source_projection(active_policy))
    recovery_rule_projection_sha256 = _sha256_json(_rule_source_projection(recovery_rule))
    dispatch_authority_projection_sha256 = _sha256_json(
        _authority_source_projection(dispatch_authority)
    )
    active_policy["runtime_source_projection_sha256"] = active_policy_projection_sha256
    recovery_rule["runtime_source_projection_sha256"] = recovery_rule_projection_sha256
    dispatch_authority["runtime_source_projection_sha256"] = dispatch_authority_projection_sha256

    mission_state = {"source_failure_mode_id": args.source_failure_mode_id}
    policy_engine = MissionOSPolicyEngine(runtime_state_dir / "policy_engine_state.json")
    policy_state = policy_engine.load_active_policy_version(
        active_policy,
        artifact_path=args.active_policy_artifact_path,
    )
    replay = policy_engine.replay_before_after(mission_state)
    after_decision = replay["after_policy_decision"]

    registry = RecoveryRuleRegistry(runtime_state_dir / "recovery_rule_registry_state.json")
    registered_rule = registry.register(
        recovery_rule,
        artifact_path=args.recovery_rule_artifact_path,
    )
    selected_rule = registry.select_recovery_rule(after_decision, mission_state)

    authority_table = DispatchAuthorityTable(runtime_state_dir / "dispatch_authority_table_state.json")
    registered_authority = authority_table.register_authority(
        dispatch_authority,
        artifact_path=args.dispatch_authority_artifact_path,
    )
    lookup_authority = authority_table.lookup(str(dispatch_authority.get("dispatch_authority_id") or ""))
    policy_state_path = runtime_state_dir / "policy_engine_state.json"
    replay_path = Path(str(replay.get("replay_artifact_path") or ""))
    registry_state_path = runtime_state_dir / "recovery_rule_registry_state.json"
    authority_state_path = runtime_state_dir / "dispatch_authority_table_state.json"
    policy_state_payload = _read_json(policy_state_path)
    replay_payload = _read_json(replay_path) if replay_path else {}
    registry_state_payload = _read_json(registry_state_path)
    authority_state_payload = _read_json(authority_state_path)
    runtime_registry_hashes_match = bool(
        policy_state.get("active_policy_source_projection_sha256")
        == active_policy_projection_sha256
        and registered_rule.get("recovery_rule_source_projection_sha256")
        == recovery_rule_projection_sha256
        and registered_authority.get("dispatch_authority_source_projection_sha256")
        == dispatch_authority_projection_sha256
    )

    result = {
        "worker_status": "completed",
        "policy_engine_state_path": str(policy_state_path),
        "policy_engine_replay_path": replay.get("replay_artifact_path"),
        "recovery_rule_registry_state_path": str(registry_state_path),
        "dispatch_authority_table_state_path": str(authority_state_path),
        "active_policy_source_projection_sha256": active_policy_projection_sha256,
        "policy_engine_active_policy_source_projection_sha256": policy_state.get(
            "active_policy_source_projection_sha256"
        ),
        "policy_engine_state_sha256": _sha256_json(policy_state_payload),
        "policy_engine_replay_sha256": _sha256_json(replay_payload),
        "recovery_rule_source_projection_sha256": recovery_rule_projection_sha256,
        "registry_rule_source_projection_sha256": registered_rule.get(
            "recovery_rule_source_projection_sha256"
        ),
        "recovery_rule_registry_state_sha256": _sha256_json(registry_state_payload),
        "dispatch_authority_source_projection_sha256": dispatch_authority_projection_sha256,
        "authority_table_source_projection_sha256": registered_authority.get(
            "dispatch_authority_source_projection_sha256"
        ),
        "dispatch_authority_table_entry_state_sha256": _sha256_json(lookup_authority),
        "dispatch_authority_table_state_sha256": _sha256_json(lookup_authority),
        "runtime_registry_hashes_match": runtime_registry_hashes_match,
        "policy_loaded": policy_state.get("engine_status") == "active_policy_loaded",
        "policy_loaded_from_empty_baseline": replay.get("policy_loaded_from_empty_baseline") is True,
        "policy_replay_baseline_state_kind": replay.get("baseline_state_kind"),
        "registered_rule_ref": registered_rule.get("rule_ref"),
        "selected_rule_ref": selected_rule.get("selected_rule_ref"),
        "registered_dispatch_ref": registered_authority.get("dispatch_ref"),
        "dispatch_authority_lookup_status": "found" if lookup_authority else "missing",
    }
    _write_json(runtime_state_dir / "policy_runtime_worker_result.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
