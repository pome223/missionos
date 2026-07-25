"""Parity between the live bench runtime artifacts and the frozen corpus.

The corpus proves the bench contract in the abstract. These tests prove the
*existing* runtime artifacts land on the same verdicts, so the corpus is a
statement about the shipped bench path and not a parallel fiction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.hardware_adapter_contract import (
    HardwareActionKind,
    HardwareExecutionMode,
    build_px4_bench_hardware_adapter_preflight,
    build_px4_bench_hardware_dispatch_candidate,
)
from src.runtime.px4_bench_action_feasibility_corpus import (
    verify_px4_bench_corpus_case,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    LINK_KIND_INJECTED_FAKE,
    LINK_KIND_REAL_SERIAL_PYMAVLINK,
)
from src.runtime.px4_bench_core_feasibility_bridge import (
    UNPUBLISHABLE_RUNTIME_FIELDS,
    build_bench_action_candidate,
    build_bench_hazard_state,
    verify_bench_dispatch_feasibility,
)
from src.runtime.corpus_publication_sanitation import publication_findings


pytestmark = pytest.mark.contract

CORPUS_ROOT = (
    Path(__file__).parents[1]
    / "golden"
    / "action_feasibility"
    / "px4_bench_v1"
)

OBSERVED_AT = "2026-07-25T00:00:00+00:00"
EVALUATED_AT = "2026-07-25T00:00:00+00:00"
FRESHNESS_DEADLINE = "2026-07-25T00:00:05+00:00"


def _corpus_case(case_id: str) -> dict:
    return json.loads(
        (CORPUS_ROOT / "cases" / f"{case_id}.json").read_text(encoding="utf-8")
    )


def _policy() -> dict:
    return _corpus_case("px4-bench-positive-verified-arm-disarm")[
        "active_policy"
    ]


def _attestation(**overrides) -> dict:
    """The publishable projection of a real physical attestation.

    Mirrors `PX4RealHardwarePhysicalAttestation`, including the identity and
    photo fields the runtime really carries, so the redaction tests below have
    something real to redact.
    """

    return {
        "propellers_removed": True,
        "operator_physically_present": True,
        "vehicle_physically_secured": True,
        "physical_estop_available": True,
        "power_disconnect_available": True,
        "attesting_operator_id": "an-operator-real-name",
        "attested_at": OBSERVED_AT,
        "bench_photo_evidence_ref": "bench-photo-001.jpg",
        **overrides,
    }


def _preflight(**kwargs) -> dict:
    defaults = {
        "physical_estop_available": True,
        "vehicle_physically_secured": True,
        "power_disconnect_available": True,
    }
    defaults.update(kwargs)
    return build_px4_bench_hardware_adapter_preflight(**defaults).model_dump(
        mode="json"
    )


def _candidate(preflight: dict) -> dict:
    return build_px4_bench_hardware_dispatch_candidate(
        missionos_action_ref="missionos_real_hardware_dispatch",
        preflight=build_px4_bench_hardware_adapter_preflight(
            physical_estop_available=preflight["physical_estop_available"],
            vehicle_physically_secured=preflight[
                "vehicle_physically_secured"
            ],
            power_disconnect_available=preflight[
                "power_disconnect_available"
            ],
        ),
    ).model_dump(mode="json")


def _verify(
    *,
    link_kind: str | None = LINK_KIND_REAL_SERIAL_PYMAVLINK,
    execution_mode=HardwareExecutionMode.BENCH,
    attestation: dict | None = None,
    preflight_kwargs: dict | None = None,
    candidate_override: dict | None = None,
) -> dict:
    preflight = _preflight(**(preflight_kwargs or {}))
    candidate = candidate_override or _candidate(preflight)
    return verify_bench_dispatch_feasibility(
        preflight=preflight,
        candidate=candidate,
        link_kind=link_kind,
        execution_mode=execution_mode,
        active_policy=_policy(),
        observed_at=OBSERVED_AT,
        evaluated_at=EVALUATED_AT,
        boot_us=1_000_000,
        physical_attestation=(
            _attestation() if attestation is None else attestation
        ),
        freshness_deadline=FRESHNESS_DEADLINE,
    )


def _status(result: dict) -> str:
    status = result["action_feasibility"]["status"]
    return status.value if hasattr(status, "value") else str(status)


def test_runtime_artifacts_reach_the_corpus_positive_verdict() -> None:
    """A fully attested bench on a real serial link is verified_feasible."""

    result = _verify()

    assert _status(result) == "verified_feasible"
    assert result["action_feasibility"]["blocked_reasons"] == []
    assert result["action_feasibility"]["unverified_reasons"] == []
    assert (
        _status(result)
        == verify_px4_bench_corpus_case(
            _corpus_case("px4-bench-positive-verified-arm-disarm")
        )["status"]
    )


@pytest.mark.parametrize(
    ("dropped_field", "corpus_case_id", "expected_reason"),
    [
        (
            "propellers_removed",
            "px4-bench-refusal-props-unattested",
            "bench_props_attestation_unverified",
        ),
    ],
)
def test_missing_attestation_field_matches_the_corpus_refusal(
    dropped_field: str, corpus_case_id: str, expected_reason: str
) -> None:
    attestation = _attestation()
    attestation.pop(dropped_field)

    result = _verify(attestation=attestation)

    assert _status(result) == "unverified"
    assert expected_reason in result["action_feasibility"][
        "unverified_reasons"
    ]
    assert _status(result) == verify_px4_bench_corpus_case(
        _corpus_case(corpus_case_id)
    )["status"]


def test_absent_attestation_is_unverified_not_blocked() -> None:
    """The runtime's real unsafe signal is an absent attestation."""

    result = _verify(attestation={})

    assert _status(result) == "unverified"
    assert result["action_feasibility"]["blocked_reasons"] == []
    unverified = result["action_feasibility"]["unverified_reasons"]
    assert "bench_props_attestation_unverified" in unverified


def test_injected_fake_connection_cannot_reach_a_bench_verdict() -> None:
    """A test double must not be promoted into hardware evidence.

    The connection's own label is the authority: `mark_connection_real_serial`
    is applied only by the real serial opener and is not exported, so an
    injected fake stays `injected_fake` no matter what the caller declares.
    """

    result = _verify(
        link_kind=LINK_KIND_INJECTED_FAKE,
        execution_mode=HardwareExecutionMode.LOOPBACK,
    )

    assert _status(result) == "unverified"
    assert "bench_link_not_physical" in result["action_feasibility"][
        "unverified_reasons"
    ]
    assert _status(result) == verify_px4_bench_corpus_case(
        _corpus_case("px4-bench-refusal-loopback-link-kind")
    )["status"]


def test_declared_bench_over_a_fake_connection_is_blocked() -> None:
    """The attack this rewire closes.

    Deriving the link class from `execution_mode` would let a caller pass BENCH
    while running an injected fake and reach a bench verdict. execution_mode is
    caller-supplied and decided before any connection is opened, so it can only
    corroborate the label — and a disagreement is an observed contradiction.
    """

    result = _verify(
        link_kind=LINK_KIND_INJECTED_FAKE,
        execution_mode=HardwareExecutionMode.BENCH,
    )

    assert _status(result) == "blocked"
    assert "bench_link_declaration_contradicted" in result[
        "action_feasibility"
    ]["blocked_reasons"]
    assert _status(result) == verify_px4_bench_corpus_case(
        _corpus_case("px4-bench-refusal-link-declaration-contradicted")
    )["status"]


def test_unlabeled_connection_is_unverified() -> None:
    """Unlabeled means not real, and it must not resolve to any class."""

    result = _verify(link_kind=None)

    assert _status(result) == "unverified"
    assert "bench_link_kind_unverified" in result["action_feasibility"][
        "unverified_reasons"
    ]


def test_execution_mode_alone_cannot_establish_a_physical_link() -> None:
    """Even a field-mode declaration establishes nothing without the label."""

    for mode in (
        HardwareExecutionMode.BENCH,
        HardwareExecutionMode.CAGE,
        HardwareExecutionMode.FIELD,
        HardwareExecutionMode.HITL,
    ):
        result = _verify(link_kind=None, execution_mode=mode)

        assert _status(result) != "verified_feasible"


def test_non_allowlisted_action_is_blocked_through_the_bridge() -> None:
    preflight = _preflight()
    candidate = _candidate(preflight)
    candidate["adapter_action_kind"] = HardwareActionKind.BOUNDED_LOCAL_MOVE.value

    result = _verify(candidate_override=candidate)

    assert _status(result) == "blocked"
    assert "bench_action_not_in_allowlist" in result["action_feasibility"][
        "blocked_reasons"
    ]


def test_bridge_never_creates_authority() -> None:
    result = _verify()

    assert result["approval_created"] is False
    assert result["dispatch_authority_created"] is False
    assert result["dispatch_request_sent"] is False
    assert result["physical_execution_invoked"] is False


def test_hazard_state_drops_every_unpublishable_runtime_field() -> None:
    """The runtime knows the serial port and the operator's name. Core must not."""

    result = _verify()
    serialized = json.dumps(result["hazard_state"])

    assert publication_findings(result["hazard_state"]) == []
    for field in UNPUBLISHABLE_RUNTIME_FIELDS:
        assert field not in serialized
    assert "an-operator-real-name" not in serialized
    assert "bench-photo-001.jpg" not in serialized


def test_candidate_does_not_forward_adapter_parameters() -> None:
    preflight = _preflight()
    candidate = _candidate(preflight)
    candidate["adapter_parameters"] = {"serial_device": "/dev/ttyACM0"}
    hazard_state = build_bench_hazard_state(
        preflight=preflight,
        link_kind=LINK_KIND_REAL_SERIAL_PYMAVLINK,
        execution_mode=HardwareExecutionMode.BENCH,
        policy_binding=_policy(),
        observed_at=OBSERVED_AT,
        boot_us=1_000_000,
        physical_attestation=_attestation(),
    )

    core_candidate = build_bench_action_candidate(
        candidate=candidate, hazard_state=hazard_state
    )

    assert core_candidate["parameters"] == {}
    assert publication_findings(core_candidate) == []


def test_bridge_hazard_state_matches_the_corpus_fact_names() -> None:
    """A drift in fact names would silently decouple runtime from corpus."""

    result = _verify()
    runtime_names = {
        fact["name"] for fact in result["hazard_state"]["observed_facts"]
    }
    corpus_names = {
        fact["name"]
        for fact in _corpus_case("px4-bench-positive-verified-arm-disarm")[
            "hazard_state"
        ]["observed_facts"]
    }

    assert runtime_names == corpus_names


def test_preflight_false_is_unestablished_not_observed_unsafe() -> None:
    """`build_px4_bench_hardware_adapter_preflight` defaults these to False.

    That default is a fail-closed "not established", not a report that the
    operator looked and found the bench unsafe. Forwarding it as a False would
    reach the verifier's observed-unsafe branch and produce `blocked`, claiming
    an observation nobody made. It must land on `unverified` instead.
    """

    default_preflight = build_px4_bench_hardware_adapter_preflight().model_dump(
        mode="json"
    )
    assert default_preflight["physical_estop_available"] is False
    assert default_preflight["preflight_status"] == "blocked"

    result = verify_bench_dispatch_feasibility(
        preflight=default_preflight,
        candidate=_candidate(_preflight()),
        link_kind=LINK_KIND_REAL_SERIAL_PYMAVLINK,
        execution_mode=HardwareExecutionMode.BENCH,
        active_policy=_policy(),
        observed_at=OBSERVED_AT,
        evaluated_at=EVALUATED_AT,
        boot_us=1_000_000,
        physical_attestation=None,
        freshness_deadline=FRESHNESS_DEADLINE,
    )

    assert _status(result) == "unverified"
    assert result["action_feasibility"]["blocked_reasons"] == []
    unverified = result["action_feasibility"]["unverified_reasons"]
    assert "physical_estop_available_unverified" in unverified
    assert "vehicle_physically_secured_unverified" in unverified
    assert "power_disconnect_available_unverified" in unverified


def test_a_blocked_preflight_never_becomes_verified_feasible() -> None:
    """Whatever else changes, a blocked preflight must not verify."""

    for attestation in (None, {}, _attestation()):
        result = verify_bench_dispatch_feasibility(
            preflight=build_px4_bench_hardware_adapter_preflight().model_dump(
                mode="json"
            ),
            candidate=_candidate(_preflight()),
            link_kind=LINK_KIND_REAL_SERIAL_PYMAVLINK,
            execution_mode=HardwareExecutionMode.BENCH,
            active_policy=_policy(),
            observed_at=OBSERVED_AT,
            evaluated_at=EVALUATED_AT,
            boot_us=1_000_000,
            physical_attestation=attestation,
            freshness_deadline=FRESHNESS_DEADLINE,
        )

        assert _status(result) != "verified_feasible"
