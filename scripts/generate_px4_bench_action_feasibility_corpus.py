"""Deterministically generate the PX4 bench Action Feasibility corpus.

The corpus freezes the bench physical-safety boundary: one positive case and
seven refusals. Every case is contract-derived. No case is produced by touching
an airframe, and none claims that a physical bench run occurred.

Regenerate:

    PYTHONPATH=. .venv/bin/python \
      scripts/generate_px4_bench_action_feasibility_corpus.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.runtime.px4_bench_action_feasibility_corpus import (
    BENCH_CORPUS_CASE_SCHEMA,
    BENCH_CORPUS_SCHEMA,
    seal_px4_bench_corpus_case,
)


CORPUS_ROOT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "golden"
    / "action_feasibility"
    / "px4_bench_v1"
)

EVALUATED_AT = "2026-07-25T00:00:00+00:00"
OBSERVED_AT = "2026-07-25T00:00:00+00:00"
FRESHNESS_DEADLINE = "2026-07-25T00:00:05+00:00"

POLICY = {
    "policy_id": "missionos.px4_bench.arm_disarm_policy",
    "policy_version": "1",
    "policy_sha256": hashlib.sha256(
        b"missionos.px4_bench.arm_disarm_policy/1"
    ).hexdigest(),
}

# Contract evidence backing every case in this corpus. These are repository
# paths, never workstation paths.
CONTRACT_EVIDENCE_REFS = [
    "src/runtime/hardware_adapter_contract.py",
    "tests/contract/test_hardware_adapter_conformance.py",
    "docs/agents/bench-conformance-corpus-design.md",
]

# Every observed bench fact and its safe default. A case overrides only what it
# is about, so a refusal differs from the positive case in exactly one respect.
SAFE_FACTS: dict[str, Any] = {
    "link_kind": "serial",
    "heartbeat_alive": True,
    "physical_estop_available": True,
    "vehicle_physically_secured": True,
    "power_disconnect_available": True,
    "operator_physically_present": True,
    "props_removed_attested": True,
}


def _source_ref(source_id: str, *, stale: bool = False) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "evidence_kind": "bench_preflight_observation",
        "observed_at": OBSERVED_AT,
        # A stale source keeps its observation time but its freshness window
        # has already closed at evaluation time.
        "freshness_deadline": (
            "2026-07-24T23:59:59+00:00" if stale else FRESHNESS_DEADLINE
        ),
        "content_sha256": hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
        "freshness_proof": "timestamp",
    }


def _hazard_state(
    *,
    state_id: str,
    facts: dict[str, Any],
    stale_sources: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    observed = [
        {
            "name": name,
            "value": value,
            "unit": None,
            "source": _source_ref(
                f"bench_{name}", stale=name in stale_sources
            ),
            "frame": None,
            "status": "observed",
        }
        for name, value in facts.items()
        if value is not None
    ]
    return {
        "state_id": state_id,
        "collected_at": OBSERVED_AT,
        "cursor": {
            "adapter_id": "missionos.px4_bench.action_feasibility.v1",
            "comparison_contract": "missionos.px4_bench.monotonic_boot_us.v1",
            "value": {"boot_us": 1_000_000},
        },
        "policy_binding": dict(POLICY),
        "observed_facts": observed,
        "derived_facts": [],
        "assumptions": [
            "props_removed and vehicle_secured are operator attestations",
        ],
    }


def _candidate(
    *,
    candidate_id: str,
    action: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "action": action,
        "parameters": {},
        "evidence_refs": evidence_refs,
        "extension_inputs": {},
    }


def _truth_boundary(*, case_id: str) -> dict[str, Any]:
    return {
        "artifact_truth": {
            "fixture_kind": "contract_derived_bench_case",
            "case_id": case_id,
            "deterministic": True,
        },
        "runtime_truth": {
            # No live bench run exists yet. When #107 produces one, the
            # positive case is resealed with these fields raised.
            "source_runtime_evidence_available": False,
            "source_runtime_evidence_refs": [],
            "source_contract_evidence_available": True,
            "source_contract_evidence_refs": list(CONTRACT_EVIDENCE_REFS),
            "source_execution_mode": "none",
            "runtime_invoked_by_this_replay": False,
            "source_runtime_reexecuted": False,
            "physical_execution_invoked_by_this_replay": False,
            "llm_invoked_by_this_replay": False,
        },
    }


def _authority_chain(*, case_id: str, scenario_class: str) -> dict[str, Any]:
    base = {
        "proposal": {
            "artifact_ref": f"proposal:{case_id}",
            "approval_created": False,
            "dispatch_authority_created": False,
        },
        "human_approval": {"artifact_ref": f"approval:{case_id}"},
        "dispatch_revalidation": {"artifact_ref": f"revalidation:{case_id}"},
        "dispatch_authority": {"artifact_ref": f"authority:{case_id}"},
        "adapter_ack": {
            "artifact_ref": f"ack:{case_id}",
            "ack_is_execution_effect": False,
        },
        "observed_effect": {"artifact_ref": f"effect:{case_id}"},
        "completion": {
            "artifact_ref": f"completion:{case_id}",
            "mission_completion_claimed": False,
            "delivery_completion_claimed": False,
            "flight_claimed": False,
        },
    }
    if scenario_class == "positive":
        base["proposal"]["status"] = "created"
        base["human_approval"].update(
            {"status": "approved", "human_approval_performed": True}
        )
        base["dispatch_revalidation"]["status"] = "valid"
        base["dispatch_authority"]["created"] = True
        base["adapter_ack"]["observed"] = True
        base["observed_effect"].update(
            {
                "armed_state_readback_observed": True,
                "disarm_observed": True,
            }
        )
        base["completion"]["completion_scope"] = "adapter_action"
        return base
    base["proposal"]["status"] = "rejected_before_proposal"
    base["human_approval"].update(
        {"status": "not_requested", "human_approval_performed": False}
    )
    base["dispatch_revalidation"]["status"] = "not_attempted"
    base["dispatch_authority"]["created"] = False
    base["adapter_ack"]["observed"] = False
    base["observed_effect"].update(
        {
            "armed_state_readback_observed": False,
            "disarm_observed": False,
        }
    )
    base["completion"]["completion_scope"] = "none"
    return base


def _case(
    *,
    case_id: str,
    scenario_class: str,
    summary: str,
    expected_status: str,
    required_reason: str | None,
    fact_overrides: dict[str, Any] | None = None,
    action: str = "px4_arm_disarm_bench",
    stale_sources: frozenset[str] = frozenset(),
    drop_facts: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    facts = dict(SAFE_FACTS)
    facts.update(fact_overrides or {})
    for name in drop_facts:
        facts.pop(name, None)
    evidence_refs = [f"bench_{name}" for name in facts]
    return seal_px4_bench_corpus_case(
        {
            "schema_version": BENCH_CORPUS_CASE_SCHEMA,
            "case_id": case_id,
            "scenario_class": scenario_class,
            "summary": summary,
            "evaluated_at": EVALUATED_AT,
            "active_policy": dict(POLICY),
            "hazard_state": _hazard_state(
                state_id=f"hazard:{case_id}",
                facts=facts,
                stale_sources=stale_sources,
            ),
            "candidate": _candidate(
                candidate_id=f"candidate:{case_id}",
                action=action,
                evidence_refs=evidence_refs,
            ),
            "expected": {
                "status": expected_status,
                "required_reason": required_reason,
            },
            "truth_boundary": _truth_boundary(case_id=case_id),
            "authority_chain": _authority_chain(
                case_id=case_id, scenario_class=scenario_class
            ),
        }
    )


def build_cases() -> list[dict[str, Any]]:
    return [
        _case(
            case_id="px4-bench-positive-verified-arm-disarm",
            scenario_class="positive",
            summary=(
                "Secured, props-removed airframe on a real serial link; "
                "arm/disarm is feasible. Feasible is not permission."
            ),
            expected_status="verified_feasible",
            required_reason=None,
        ),
        # The three attestation refusals below are `unverified`, not `blocked`.
        # `PX4RealHardwarePhysicalAttestation` types every safety field as
        # `Literal[True]`, so the runtime cannot emit a False. An unsafe bench
        # appears as a *missing* attestation, which is an unobserved condition,
        # not an observed unsafe one. Freezing these as `blocked` would claim an
        # observation the system never makes.
        _case(
            case_id="px4-bench-refusal-estop-unattested",
            scenario_class="refusal",
            summary="No E-stop attestation is present for this bench session.",
            expected_status="unverified",
            required_reason="physical_estop_available_unverified",
            drop_facts=frozenset({"physical_estop_available"}),
        ),
        _case(
            case_id="px4-bench-refusal-vehicle-secured-unattested",
            scenario_class="refusal",
            summary="No restraint attestation is present for this session.",
            expected_status="unverified",
            required_reason="vehicle_physically_secured_unverified",
            drop_facts=frozenset({"vehicle_physically_secured"}),
        ),
        _case(
            case_id="px4-bench-refusal-props-unattested",
            scenario_class="refusal",
            summary=(
                "No props-removed attestation is present. Silence is not an "
                "observation that the propellers are attached."
            ),
            expected_status="unverified",
            required_reason="bench_props_attestation_unverified",
            drop_facts=frozenset({"props_removed_attested"}),
        ),
        _case(
            case_id="px4-bench-refusal-loopback-link-kind",
            scenario_class="refusal",
            summary=(
                "A loopback link cannot back a bench claim. This guards the "
                "grade of the evidence, not the airframe."
            ),
            expected_status="unverified",
            required_reason="bench_link_not_physical",
            fact_overrides={"link_kind": "loopback"},
        ),
        _case(
            case_id="px4-bench-refusal-stale-telemetry",
            scenario_class="refusal",
            summary="The heartbeat observation is past its freshness window.",
            expected_status="unverified",
            required_reason="evidence_stale",
            stale_sources=frozenset({"heartbeat_alive"}),
        ),
        _case(
            case_id="px4-bench-refusal-heartbeat-loss",
            scenario_class="refusal",
            summary="The link heartbeat is observed as lost.",
            expected_status="unverified",
            required_reason="bench_heartbeat_lost",
            fact_overrides={"heartbeat_alive": False},
        ),
        _case(
            case_id="px4-bench-refusal-action-not-in-allowlist",
            scenario_class="refusal",
            summary=(
                "A bounded local move is refused even though every physical "
                "precondition is satisfied."
            ),
            expected_status="blocked",
            required_reason="bench_action_not_in_allowlist",
            action="bounded_local_move",
        ),
    ]


def build_manifest(cases: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [
        {
            "case_id": case["case_id"],
            "path": f"cases/{case['case_id']}.json",
            "sha256": hashlib.sha256(
                json.dumps(
                    case,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        }
        for case in cases
    ]
    manifest = {
        "schema_version": BENCH_CORPUS_SCHEMA,
        "adapter_id": "missionos.px4_bench.action_feasibility.v1",
        "corpus_id": "px4_bench_v1",
        "description": (
            "PX4 bench arm/disarm Action Feasibility boundary. Contract "
            "derived. Replay invokes no hardware and claims no physical "
            "execution."
        ),
        "cases": entries,
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return manifest


def main() -> None:
    cases = build_cases()
    case_dir = CORPUS_ROOT / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        (case_dir / f"{case['case_id']}.json").write_text(
            json.dumps(case, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    manifest = build_manifest(cases)
    (CORPUS_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "corpus_id": manifest["corpus_id"],
                "case_count": len(cases),
                "manifest_sha256": manifest["manifest_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
