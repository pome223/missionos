#!/usr/bin/env python3
"""Bind a residual LIBERO predicate to a proven diagnostic repair skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SKILL_ID = "privileged_push_moka_pot_2_to_stove_region.v1"
EXPECTED_SOURCE_VECTOR = [True, False, True]
EXPECTED_TERMINAL_VECTOR = [True, True, True]


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bind(*, snapshot_path: Path, oracle_report_path: Path, output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise ValueError("libero_known_skill_binding_output_exists")
    oracle = json.loads(oracle_report_path.read_text(encoding="utf-8"))
    supplied = oracle.get("result_sha256")
    material = {key: value for key, value in oracle.items() if key != "result_sha256"}
    snapshot_sha256 = _sha256_path(snapshot_path)
    if supplied != _canonical_sha256(material):
        raise RuntimeError("libero_known_skill_binding_oracle_digest_mismatch")
    if (
        oracle.get("schema_version") != "missionos.vla0_same_interface_oracle_recoverability.v2"
        or oracle.get("snapshot_sha256") != snapshot_sha256
        or oracle.get("source_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or oracle.get("terminal_goal_predicate_vector") != EXPECTED_TERMINAL_VECTOR
        or oracle.get("stable_success_observed") is not True
        or oracle.get("preservation_violation_observed") is not False
        or oracle.get("claim_boundary", {}).get("privileged_object_state_used_for_oracle_planning")
        is not True
    ):
        raise RuntimeError("libero_known_skill_binding_oracle_contract_invalid")
    report_without_digest = {
        "schema_version": "missionos.libero_known_skill_repair_binding.v1",
        "status": "diagnostic_known_skill_binding_observed",
        "snapshot_sha256": snapshot_sha256,
        "residual_predicate": {
            "predicate": "on",
            "arguments": ["moka_pot_2", "flat_stove_1_cook_region"],
            "observed_satisfied": False,
        },
        "selected_skill": {
            "skill_id": SKILL_ID,
            "selection_basis": "exact_registered_residual_predicate_match",
            "execution_interface": "original_libero_7d_action_interface",
            "proven_actions_applied": oracle["actions_applied"],
            "first_contact_after_action": oracle["trajectory_events"][
                "first_contact_after_action"
            ],
            "first_success_after_action": oracle["success_first_observed_after_action"],
            "oracle_report_sha256": supplied,
        },
        "verifier_result": {
            "authority": "actual_libero_goal_predicates_from_scripted_oracle",
            "terminal_goal_predicate_vector": EXPECTED_TERMINAL_VECTOR,
            "stable_success_observed": True,
            "preservation_violation_observed": False,
        },
        "claim_boundary": {
            "authority": "diagnostic_only",
            "privileged_object_state_used": True,
            "deterministic_skill_selection_only": True,
            "cosmos_policy_used_for_skill_selection": False,
            "learned_policy_repair_established": False,
            "missionos_proposal_created": False,
            "human_approval_created": False,
            "governed_dispatch_created": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": _canonical_sha256(report_without_digest),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--oracle-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(bind(
        snapshot_path=args.snapshot.resolve(),
        oracle_report_path=args.oracle_report.resolve(),
        output_path=args.output.resolve(),
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
