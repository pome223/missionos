"""PX4 bench Action Feasibility through the backend-neutral MissionOS Core.

The bench slice is a props-removed, physically secured PX4 airframe whose only
allowed bounded action is arm/disarm. This module expresses the bench physical
safety conditions as a Core verifier extension. It calculates feasibility only.

It creates no approval, no dispatch authority, no execution, no progress, and no
completion. It opens no serial port and touches no vehicle. Deciding that an
arm/disarm is feasible is not permission to perform one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from missionos_core import (
    ActionCandidate,
    ExtensionVerdict,
    FeasibilityStatus,
    HazardState,
    PolicyBinding,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    verify_action_candidate,
)


PX4_BENCH_ADAPTER_ID = "missionos.px4_bench.action_feasibility.v1"
PX4_BENCH_EXTENSION_ID = "missionos.px4_bench.physical_safety.v1"

# The bench slice arms a secured airframe. Nothing else is in scope, and no
# action here may move the vehicle through space.
PX4_BENCH_ALLOWED_ACTIONS = frozenset({"px4_arm_disarm_bench", "safe_stop"})

# A link class is publishable. A link endpoint is not; see
# `corpus_publication_sanitation.py`. Only a real serial link may support a
# physical-execution claim.
PX4_BENCH_PHYSICAL_LINK_KINDS = frozenset({"serial"})
PX4_BENCH_KNOWN_LINK_KINDS = frozenset({"serial", "loopback", "sim"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _fact_values(hazard_state: HazardState) -> dict[str, Any]:
    return {fact.name: fact.value for fact in hazard_state.observed_facts}


class Px4BenchPhysicalSafetyExtension:
    """Deterministic bench physical-safety verifier.

    Every condition is fail-closed. An unobservable condition yields
    `unverified`; an observed unsafe condition yields `blocked`. Neither
    produces authority.
    """

    extension_id = PX4_BENCH_EXTENSION_ID

    def verify(
        self,
        *,
        hazard_state: HazardState,
        candidate: ActionCandidate,
    ) -> ExtensionVerdict:
        blocked: list[str] = []
        unverified: list[str] = []
        facts = _fact_values(hazard_state)
        measurements: dict[str, Any] = {}

        # Allowlist. A bench adapter must refuse anything that could fly the
        # airframe, regardless of how well-evidenced the request is.
        action = str(candidate.action or "")
        measurements["requested_action"] = action
        if action not in PX4_BENCH_ALLOWED_ACTIONS:
            blocked.append("bench_action_not_in_allowlist")

        # Link class. This guard protects the *grade of the evidence*, not the
        # airframe: a loopback or sim link may never back a bench claim.
        link_kind = facts.get("link_kind")
        measurements["link_kind"] = link_kind
        if link_kind is None:
            unverified.append("bench_link_kind_unverified")
        elif str(link_kind) not in PX4_BENCH_KNOWN_LINK_KINDS:
            unverified.append("bench_link_kind_unrecognized")
        elif str(link_kind) not in PX4_BENCH_PHYSICAL_LINK_KINDS:
            unverified.append("bench_link_not_physical")

        # A caller declaring a physical execution mode over a connection that is
        # not labeled real is an observed contradiction, not an absence, so it
        # blocks. This is the case where someone would otherwise present an
        # injected fake as bench evidence.
        declaration_consistent = facts.get("link_declaration_consistent")
        measurements["link_declaration_consistent"] = declaration_consistent
        if declaration_consistent is False:
            blocked.append("bench_link_declaration_contradicted")

        # Physical safety preconditions, mirroring the operator attestation in
        # `PX4RealHardwarePhysicalAttestation`.
        #
        # That model types every safety field as `Literal[True]`, so today an
        # unsafe bench reaches us as a *missing* fact, which is `unverified`.
        # The False branches below are the semantics for an explicit
        # operator-declared-unsafe channel; they are exercised by contract test,
        # not by the corpus, because the current runtime cannot produce a False.
        for fact_name, blocked_reason in (
            ("physical_estop_available", "bench_physical_estop_missing"),
            ("vehicle_physically_secured", "bench_vehicle_not_secured"),
            ("power_disconnect_available", "bench_power_disconnect_missing"),
            ("operator_physically_present", "bench_operator_not_present"),
        ):
            value = facts.get(fact_name)
            measurements[fact_name] = value
            if value is None:
                unverified.append(f"{fact_name}_unverified")
            elif value is not True:
                blocked.append(blocked_reason)

        # Props. An attested-attached propeller blocks the slice outright.
        props_removed = facts.get("props_removed_attested")
        measurements["props_removed_attested"] = props_removed
        if props_removed is None:
            unverified.append("bench_props_attestation_unverified")
        elif props_removed is not True:
            blocked.append("bench_props_attached")

        # Link liveness. A missing heartbeat cannot be treated as a live link.
        heartbeat = facts.get("heartbeat_alive")
        measurements["heartbeat_alive"] = heartbeat
        if heartbeat is None:
            unverified.append("bench_heartbeat_unverified")
        elif heartbeat is not True:
            unverified.append("bench_heartbeat_lost")

        blocked = list(dict.fromkeys(blocked))
        unverified = list(dict.fromkeys(unverified))
        if blocked:
            status = FeasibilityStatus.BLOCKED
        elif unverified:
            status = FeasibilityStatus.UNVERIFIED
        else:
            status = FeasibilityStatus.VERIFIED_FEASIBLE
        item_status = (
            VerificationItemStatus.BLOCKED
            if blocked
            else VerificationItemStatus.PENDING
            if unverified
            else VerificationItemStatus.PASS
        )
        item_id = "bench_physical_safety_constraints"
        return ExtensionVerdict(
            extension_id=self.extension_id,
            status=status,
            blocked_reasons=tuple(blocked),
            unverified_reasons=tuple(unverified),
            measurements=measurements,
            assumptions=(
                "props_removed and vehicle_secured are operator attestations, "
                "not machine observations",
            ),
            verification_items=(
                VerificationItem(
                    item_id=item_id,
                    predicate=(
                        "the bounded bench action satisfies the declared "
                        "physical-safety and link constraints"
                    ),
                    status=item_status,
                    verification_basis=(
                        VerificationBasis.UNVERIFIED
                        if unverified
                        else VerificationBasis.DETERMINISTIC
                    ),
                    evidence_refs=tuple(candidate.evidence_refs),
                ),
            ),
            required_verification_item_ids=(item_id,),
        )


def verify_px4_bench_core_action_candidate(
    *,
    hazard_state: Mapping[str, Any],
    candidate: Mapping[str, Any],
    active_policy: Mapping[str, Any],
    evaluated_at: str,
    extensions: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Verify one bench candidate through the Core contract.

    Returns the Core result only. No adapter here sends, approves, or executes.
    """

    state = HazardState.from_dict(_mapping(hazard_state))
    policy = PolicyBinding(**_mapping(active_policy))
    result = verify_action_candidate(
        hazard_state=state,
        candidate=ActionCandidate.from_dict(_mapping(candidate)),
        active_policy=policy,
        evaluated_at=evaluated_at,
        extensions=(
            list(extensions)
            if extensions is not None
            else [Px4BenchPhysicalSafetyExtension()]
        ),
    )
    return {
        "adapter_id": PX4_BENCH_ADAPTER_ID,
        "action_feasibility": {
            "status": result.status,
            "blocked_reasons": list(result.blocked_reasons),
            "unverified_reasons": list(result.unverified_reasons),
            "assumptions": list(result.assumptions),
            "policy_sha256": result.policy_sha256,
            "evaluated_at": result.evaluated_at,
            "verification_basis": result.verification_basis,
            "verification_items": [
                item.to_dict()
                for verdict in result.extension_verdicts
                for item in verdict.verification_items
            ],
        },
        # Authority outputs are constant. The adapter cannot set them.
        "approval_created": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "physical_execution_invoked": False,
    }


__all__ = [
    "PX4_BENCH_ADAPTER_ID",
    "PX4_BENCH_ALLOWED_ACTIONS",
    "PX4_BENCH_EXTENSION_ID",
    "PX4_BENCH_KNOWN_LINK_KINDS",
    "PX4_BENCH_PHYSICAL_LINK_KINDS",
    "Px4BenchPhysicalSafetyExtension",
    "verify_px4_bench_core_action_candidate",
]
