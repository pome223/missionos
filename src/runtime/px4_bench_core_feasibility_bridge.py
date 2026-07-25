"""Bridge the existing PX4 bench runtime onto the Core feasibility contract.

`missionos_real_hardware_dispatch_runtime` already produces a bench preflight
artifact and an operator physical attestation. This module translates those into
the backend-neutral Core `HazardState` and `ActionCandidate` so the bench slice
is judged by the same contract that PX4/Gazebo and Nav2/TurtleBot3 pass.

Two boundaries matter here.

Publication: the runtime knows the serial device, the attesting operator's id,
and the bench photo reference. None of them may enter a hazard state, because
hazard states are what the conformance corpus freezes and publishes. Only the
link *class* crosses.

Claim: this module calculates feasibility. It performs no preflight of its own,
opens no serial port, and creates no approval, dispatch authority, execution,
progress, or completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.runtime.hardware_adapter_contract import (
    HardwareActionKind,
    HardwareExecutionMode,
)
from src.runtime.px4_bench_core_action_feasibility_adapter import (
    PX4_BENCH_ADAPTER_ID,
    verify_px4_bench_core_action_candidate,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    LINK_KIND_INJECTED_FAKE,
    LINK_KIND_REAL_SERIAL_PYMAVLINK,
)


BENCH_CURSOR_CONTRACT = "missionos.px4_bench.monotonic_boot_us.v1"

# The connection's own label is the authority for the link class.
#
# `mark_connection_real_serial` is applied only by the real serial opener and is
# deliberately not exported, so a caller cannot label a fake as real. The
# actuator backend enforces `physical_execution_invoked == (link_kind is
# real_serial_pymavlink)` as a model invariant. This bridge inherits that
# authority rather than re-deriving it.
#
# `execution_mode` is NOT that authority: it is a caller-supplied parameter,
# decided before any connection is opened. Deriving the link class from it would
# let `execution_mode=BENCH` plus an injected fake connection reach a bench
# verdict — precisely the promotion this slice exists to prevent.
_LINK_KIND_CLASS = {
    LINK_KIND_REAL_SERIAL_PYMAVLINK: "serial",
    LINK_KIND_INJECTED_FAKE: "loopback",
}

# Execution modes whose *declaration* implies a physical link. Used only to
# detect a contradiction against the connection label, never to establish one.
_PHYSICAL_EXECUTION_MODES = frozenset(
    {
        HardwareExecutionMode.BENCH,
        HardwareExecutionMode.CAGE,
        HardwareExecutionMode.FIELD,
        HardwareExecutionMode.HITL,
    }
)

_ACTION_KIND_TO_CORE_ACTION = {
    HardwareActionKind.PX4_ARM_DISARM_BENCH: "px4_arm_disarm_bench",
    HardwareActionKind.SAFE_STOP: "safe_stop",
}

# Fields the runtime holds that must never reach a published hazard state.
UNPUBLISHABLE_RUNTIME_FIELDS = frozenset(
    {
        "approval_actor",
        "attesting_operator_id",
        "bench_photo_evidence_ref",
        "baudrate",
        "serial_device",
    }
)


def _link_class(link_kind: str | None) -> str | None:
    """Map a connection label to a publishable link class.

    An unlabeled connection returns None. Unlabeled means not real, so it must
    not resolve to any class — the verifier then reports `unverified` rather
    than assuming either a real or a fake link.
    """

    if not link_kind:
        return None
    return _LINK_KIND_CLASS.get(str(link_kind))


def _declares_physical_link(
    execution_mode: HardwareExecutionMode | str | None,
) -> bool:
    if execution_mode is None:
        return False
    if isinstance(execution_mode, HardwareExecutionMode):
        return execution_mode in _PHYSICAL_EXECUTION_MODES
    try:
        return HardwareExecutionMode(str(execution_mode)) in _PHYSICAL_EXECUTION_MODES
    except ValueError:
        return False


def _core_action(action_kind: HardwareActionKind | str) -> str:
    if isinstance(action_kind, HardwareActionKind):
        return _ACTION_KIND_TO_CORE_ACTION.get(action_kind, action_kind.value)
    try:
        kind = HardwareActionKind(str(action_kind))
    except ValueError:
        return str(action_kind)
    return _ACTION_KIND_TO_CORE_ACTION.get(kind, kind.value)


def _source(
    fact_name: str,
    *,
    observed_at: str,
    freshness_deadline: str | None,
) -> dict[str, Any]:
    return {
        "source_id": f"bench_{fact_name}",
        "evidence_kind": "bench_preflight_observation",
        "observed_at": observed_at,
        "freshness_deadline": freshness_deadline,
        "content_sha256": None,
        "freshness_proof": "timestamp",
    }


def build_bench_hazard_state(
    *,
    preflight: Mapping[str, Any],
    policy_binding: Mapping[str, Any],
    observed_at: str,
    boot_us: int,
    link_kind: str | None = None,
    execution_mode: HardwareExecutionMode | str | None = None,
    physical_attestation: Mapping[str, Any] | None = None,
    freshness_deadline: str | None = None,
    state_id: str = "bench_hazard_state",
) -> dict[str, Any]:
    """Translate runtime preflight and attestation into a Core hazard state.

    `link_kind` is the connection's own label from the actuator backend and is
    the only thing that can establish a physical link. `execution_mode` is the
    caller's declaration; it can contradict the label but never create one.

    A fact is emitted only when the runtime actually observed it. A missing
    attestation yields missing facts, which the verifier reports as
    `unverified`. It does not become a False, because the runtime cannot
    distinguish an unsafe bench from an unstated one.
    """

    facts: dict[str, Any] = {}

    link_class = _link_class(link_kind)
    if link_class is not None:
        facts["link_kind"] = link_class
        # Corroboration only. A caller declaring a physical mode over a
        # connection that is not labeled real is an observed contradiction, and
        # the verifier blocks it.
        if execution_mode is not None:
            facts["link_declaration_consistent"] = not (
                _declares_physical_link(execution_mode) and link_class != "serial"
            )

    for name in ("heartbeat_alive",):
        if preflight.get(name) is not None:
            facts[name] = bool(preflight[name])

    # These three come from the preflight artifact, whose builder defaults them
    # to False as a fail-closed default. In this runtime a False therefore means
    # "not established", not "the operator observed an unsafe bench" — the only
    # real source is the attestation, and it types them as Literal[True]. So a
    # False is dropped rather than forwarded: an unestablished condition is
    # unverified, and reporting it as blocked would claim an observation nobody
    # made. See `test_preflight_false_is_unestablished_not_observed_unsafe`.
    for name in (
        "physical_estop_available",
        "vehicle_physically_secured",
        "power_disconnect_available",
    ):
        if preflight.get(name) is True:
            facts[name] = True

    # Props-removed and operator presence live only on the attestation. Their
    # absence is the runtime's way of saying the operator did not attest.
    if physical_attestation:
        if physical_attestation.get("propellers_removed") is not None:
            facts["props_removed_attested"] = bool(
                physical_attestation["propellers_removed"]
            )
        if physical_attestation.get("operator_physically_present") is not None:
            facts["operator_physically_present"] = bool(
                physical_attestation["operator_physically_present"]
            )

    observed_facts = [
        {
            "name": name,
            "value": value,
            "unit": None,
            "source": _source(
                name,
                observed_at=observed_at,
                freshness_deadline=freshness_deadline,
            ),
            "frame": None,
            "status": "observed",
        }
        for name, value in facts.items()
    ]
    return {
        "state_id": state_id,
        "collected_at": observed_at,
        "cursor": {
            "adapter_id": PX4_BENCH_ADAPTER_ID,
            "comparison_contract": BENCH_CURSOR_CONTRACT,
            "value": {"boot_us": int(boot_us)},
        },
        "policy_binding": dict(policy_binding),
        "observed_facts": observed_facts,
        "derived_facts": [],
        "assumptions": [
            "props_removed and vehicle_secured are operator attestations",
        ],
    }


def build_bench_action_candidate(
    *,
    candidate: Mapping[str, Any],
    hazard_state: Mapping[str, Any],
    candidate_id: str = "bench_candidate",
) -> dict[str, Any]:
    """Translate a runtime dispatch candidate into a Core action candidate.

    `adapter_parameters` is deliberately not forwarded. The bench arm/disarm
    action takes none, and forwarding an opaque runtime mapping is how a serial
    device path would eventually reach a published corpus.
    """

    evidence_refs = [
        str(fact.get("source", {}).get("source_id"))
        for fact in hazard_state.get("observed_facts", ())
        if isinstance(fact, Mapping)
    ]
    return {
        "candidate_id": candidate_id,
        "action": _core_action(candidate.get("adapter_action_kind", "")),
        "parameters": {},
        "evidence_refs": evidence_refs,
        "extension_inputs": {},
    }


def verify_bench_dispatch_feasibility(
    *,
    preflight: Mapping[str, Any],
    candidate: Mapping[str, Any],
    active_policy: Mapping[str, Any],
    observed_at: str,
    evaluated_at: str,
    boot_us: int,
    link_kind: str | None = None,
    execution_mode: HardwareExecutionMode | str | None = None,
    physical_attestation: Mapping[str, Any] | None = None,
    freshness_deadline: str | None = None,
) -> dict[str, Any]:
    """Judge a runtime bench dispatch through the Core contract.

    Returns the Core feasibility result. `verified_feasible` is a calculation,
    not permission: approval, the dispatch token, and execution remain with the
    Gateway and the executor.
    """

    hazard_state = build_bench_hazard_state(
        preflight=preflight,
        link_kind=link_kind,
        execution_mode=execution_mode,
        policy_binding=active_policy,
        observed_at=observed_at,
        boot_us=boot_us,
        physical_attestation=physical_attestation,
        freshness_deadline=freshness_deadline,
    )
    return {
        **verify_px4_bench_core_action_candidate(
            hazard_state=hazard_state,
            candidate=build_bench_action_candidate(
                candidate=candidate, hazard_state=hazard_state
            ),
            active_policy=active_policy,
            evaluated_at=evaluated_at,
        ),
        "hazard_state": hazard_state,
    }


__all__ = [
    "BENCH_CURSOR_CONTRACT",
    "UNPUBLISHABLE_RUNTIME_FIELDS",
    "build_bench_action_candidate",
    "build_bench_hazard_state",
    "verify_bench_dispatch_feasibility",
]
