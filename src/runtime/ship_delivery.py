"""One stationary-ship delivery mission in an explicit kinematic fixture.

The fixture advances one vehicle and one payload in one local ENU world. It
does not start PX4/Gazebo, implement flight dynamics, or invoke learned models.
The existing parent coordinator supplies approval lineage and sequencing; the
ship-specific verifier checks observed fixture state, never promotes the
generic parent's outcome to physical or shared-world evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from math import ceil, dist, hypot, isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    FrozenParentMissionContract,
    HardwareExecutionMode,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
)
from src.runtime.parent_mission_coordinator import run_parent_mission_coordinator


STAGES = (
    "load",
    "launch",
    "sea_outbound",
    "urban_entry",
    "deliver",
    "urban_exit",
    "sea_return",
    "recover",
)
PREDICATE = {"version": 1, "scope": "stationary_ship_kinematic_fixture", "stages": STAGES}
LIMITATIONS = [
    "Kinematic fixture with synthetic observations, not PX4/Gazebo flight dynamics.",
    "Constant power and along-route wind model are uncalibrated; no endurance claim.",
    "Urban blockage timing is a scripted fixture input, not a WAM forecast.",
    "No VLA, WAM, LLM, real payload transfer, moving-deck landing, or fleet execution.",
    "Fixture digests detect accidental mutation; they do not authenticate external telemetry.",
]


class ShipDeliveryScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    scenario_id: str = Field(default="stationary-ship-v1", min_length=1, max_length=100)
    vehicle_id: str = Field(default="drone-1", min_length=1, max_length=100)
    payload_id: str = Field(default="parcel-1", min_length=1, max_length=100)
    ship_id: str = Field(default="ship-1", min_length=1, max_length=100)
    backend: Literal["kinematic_fixture"] = "kinematic_fixture"
    vehicle_count: Literal[1] = 1
    ship_speed_mps: Literal[0] = 0
    offshore_distance_m: float = Field(default=1000, gt=0, le=5000)
    urban_distance_m: float = Field(default=200, gt=50, le=2000)
    cruise_altitude_m: float = Field(default=30, ge=5, le=120)
    wind_mps: float = Field(default=4, ge=0, le=20)
    max_wind_mps: float = Field(default=10, gt=0, le=20)
    airspeed_mps: float = Field(default=12, gt=0, le=30)
    battery_wh: float = Field(default=220, gt=0, le=10000)
    reserve_wh: float = Field(default=40, gt=0, le=10000)
    power_w: float = Field(default=1500, gt=0, le=100000)
    urban_blockage_s: float = Field(default=0, ge=0, le=600)
    max_wait_s: float = Field(default=30, ge=0, le=120)
    urban_detour_offset_m: float = Field(default=80, ge=30, le=300)
    max_mission_s: float = Field(default=900, gt=0, le=3600)
    urban_perception_ready: bool = True

    @model_validator(mode="after")
    def validate_reserve(self) -> "ShipDeliveryScenario":
        if self.reserve_wh >= self.battery_wh:
            raise ValueError("reserve_wh must be below battery_wh")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def _child_contract(s: ShipDeliveryScenario, stage: str) -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id=f"ship-fixture:{s.scenario_id}:{stage}",
        contract_version="v1",
        execution_scope=HardwareExecutionMode.LOOPBACK,
        reference_inputs=(
            ReferenceInput(
                input_id="ship_scenario",
                kind="kinematic_fixture_scenario",
                content_sha256=s.sha256,
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=f"ship_{stage}",
                evidence_kind="ship_fixture_stage_receipt",
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=30,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="One synthetic fixture stage only.",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=f"ship_{stage}_verified",
            statement=f"Fixture {stage} observed.",
            claim_scope="kinematic_fixture_only",
        ),
        predicate_package=PredicatePackageBinding(
            package_id="ship_fixture_predicate",
            package_version="v1",
            content_sha256=canonical_sha256(PREDICATE),
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            )
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def build_ship_delivery_contract(scenario: ShipDeliveryScenario) -> FrozenParentMissionContract:
    return FrozenParentMissionContract(
        parent_mission_id=f"ship-fixture:{scenario.scenario_id}",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=scenario.sha256,
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="Sequencing only; ship fixture continuity is verified separately.",
        ),
        stages=tuple(
            build_parent_mission_stage_binding(
                stage_index=index,
                stage_ref=stage,
                executor_ref="fixture:ship-delivery",
                child_contract=_child_contract(scenario, stage),
            )
            for index, stage in enumerate(STAGES, start=1)
        ),
    )


def _urban_choice(s: ShipDeliveryScenario) -> str:
    if s.urban_blockage_s == 0:
        return "proceed"
    return "wait" if s.urban_blockage_s <= s.max_wait_s else "detour"


def _minimum_budget(s: ShipDeliveryScenario) -> float:
    # Conservative: slowest sea groundspeed for all horizontal legs, plus
    # vertical motion at 2 m/s and load/release/recovery dwell.
    speed = s.airspeed_mps - s.wind_mps
    if speed <= 0:
        return float("inf")
    choice = _urban_choice(s)
    distance = 2 * (s.offshore_distance_m + s.urban_distance_m)
    if choice == "detour":
        distance += 4 * s.urban_detour_offset_m
    wait = s.urban_blockage_s if choice == "wait" else 0
    return distance / speed + 4 * s.cruise_altitude_m / 2 + wait + 6


def _preflight_reasons(s: ShipDeliveryScenario) -> list[str]:
    reasons = []
    duration = _minimum_budget(s)
    if s.wind_mps > s.max_wind_mps:
        reasons.append("wind_limit_exceeded")
    if s.wind_mps >= s.airspeed_mps:
        reasons.append("return_groundspeed_unavailable")
    if not s.urban_perception_ready:
        reasons.append("urban_perception_not_ready")
    if duration > s.max_mission_s:
        reasons.append("mission_time_budget_exceeded")
    if s.battery_wh - duration * s.power_w / 3600 < s.reserve_wh:
        reasons.append("insufficient_return_reserve")
    return reasons


class _FixtureWorld:
    def __init__(self, scenario: ShipDeliveryScenario):
        self.s = scenario
        self.identity = {
            "run_id": str(uuid4()),
            "world_id": str(uuid4()),
            "vehicle_id": scenario.vehicle_id,
            "payload_id": scenario.payload_id,
            "ship_id": scenario.ship_id,
        }
        self.position = [0.0, 0.0, 0.0]
        self.payload_position = list(self.position)
        self.payload_attached = True
        self.armed = False
        self.landed = True
        self.time_s = 0.0
        self.battery_wh = scenario.battery_wh
        self.sequence = 0
        self.urban_started_at: float | None = None

    def observe(self) -> dict[str, Any]:
        return {
            **self.identity,
            "sequence": self.sequence,
            "time_s": self.time_s,
            "position_enu_m": list(self.position),
            "payload_position_enu_m": list(self.payload_position),
            "ship_position_enu_m": [0.0, 0.0, 0.0],
            "battery_wh": self.battery_wh,
            "payload_attached": self.payload_attached,
            "armed": self.armed,
            "landed": self.landed,
            "source": "kinematic_fixture",
            "scenario_sha256": self.s.sha256,
        }

    def advance(self, target: list[float], seconds: float) -> dict[str, Any]:
        if seconds <= 0:
            raise ValueError("Fixture time must advance")
        self.time_s += seconds
        self.battery_wh -= self.s.power_w * seconds / 3600
        self.position = list(target)
        if self.payload_attached:
            self.payload_position = list(target)
        self.sequence += 1
        return self.observe()

    def move(self, target: list[float], samples: list[dict[str, Any]]) -> None:
        start = list(self.position)
        horizontal = hypot(target[0] - start[0], target[1] - start[1])
        speed = self.s.airspeed_mps
        if max(start[0], target[0]) <= self.s.offshore_distance_m:
            speed += self.s.wind_mps if target[0] > start[0] else -self.s.wind_mps
        duration = max(horizontal / speed, abs(target[2] - start[2]) / 2)
        if duration == 0:
            return
        count = max(1, ceil(duration))
        for step in range(1, count + 1):
            point = [a + (b - a) * step / count for a, b in zip(start, target)]
            samples.append(self.advance(point, duration / count))


def _receipt_hash(receipt: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})


def _execute_stage(world: _FixtureWorld, stage: str) -> dict[str, Any]:
    s = world.s
    coast, destination, altitude = (
        s.offshore_distance_m,
        s.offshore_distance_m + s.urban_distance_m,
        s.cruise_altitude_m,
    )
    samples = [world.observe()]
    decision = None
    if stage == "load":
        samples.append(world.advance(world.position, 1))
    elif stage == "launch":
        world.armed, world.landed = True, False
        world.move([0, 0, altitude], samples)
    elif stage == "sea_outbound":
        world.move([coast, 0, altitude], samples)
    elif stage == "urban_entry":
        world.urban_started_at = world.time_s
        choice = _urban_choice(s)
        decision = {
            "choice": choice,
            "source": "deterministic_fixture_policy",
            "blockage_duration_s": s.urban_blockage_s,
            "prediction_source": "scripted_fixture_schedule",
            "vla_invoked": False,
            "wam_invoked": False,
        }
        if choice == "wait":
            samples.append(world.advance(world.position, s.urban_blockage_s))
        elif choice == "detour":
            world.move([coast, s.urban_detour_offset_m, altitude], samples)
            world.move([destination, s.urban_detour_offset_m, altitude], samples)
        world.move([destination, 0, altitude], samples)
    elif stage == "deliver":
        world.move([destination, 0, 0], samples)
        world.armed, world.landed = False, True
        samples.append(world.advance(world.position, 1))
        world.payload_attached = False
        samples.append(world.advance(world.position, 1))
    elif stage == "urban_exit":
        world.armed, world.landed = True, False
        world.move([destination, 0, altitude], samples)
        # The same obstacle can still be present on the return leg.
        if (
            world.urban_started_at is not None
            and world.time_s - world.urban_started_at < s.urban_blockage_s
        ):
            world.move([destination, s.urban_detour_offset_m, altitude], samples)
            world.move([coast, s.urban_detour_offset_m, altitude], samples)
        world.move([coast, 0, altitude], samples)
    elif stage == "sea_return":
        world.move([0, 0, altitude], samples)
    elif stage == "recover":
        world.move([0, 0, 0], samples)
        world.armed, world.landed = False, True
        samples.append(world.advance(world.position, 1))
        samples.append(world.advance(world.position, 2))
    receipt = {
        "stage": stage,
        "samples": samples,
        "decision": decision,
        "scenario_sha256": s.sha256,
        "schema_version": "ship_fixture_stage.v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt["sha256"] = _receipt_hash(receipt)
    return receipt


def _near(point: Any, expected: list[float]) -> bool:
    return isinstance(point, list) and len(point) == 3 and dist(point, expected) < 1e-6


def _segment_hits_obstacle(
    start: dict[str, Any],
    end: dict[str, Any],
    s: ShipDeliveryScenario,
    urban_started: float,
) -> bool:
    """Intersect the continuous segment with the rectangle while it is active."""
    dt = end["time_s"] - start["time_s"]
    remaining = urban_started + s.urban_blockage_s - start["time_s"]
    if dt <= 0 or remaining <= 0:
        return False
    enter, leave = 0.0, min(1.0, remaining / dt)
    active_end = leave
    limits = (
        (
            s.offshore_distance_m + 0.4 * s.urban_distance_m,
            s.offshore_distance_m + 0.6 * s.urban_distance_m,
        ),
        (-25.0, 25.0),
    )
    for axis, (minimum, maximum) in enumerate(limits):
        origin = start["position_enu_m"][axis]
        delta = end["position_enu_m"][axis] - origin
        if abs(delta) < 1e-12:
            if not minimum <= origin <= maximum:
                return False
        else:
            a, b = sorted(((minimum - origin) / delta, (maximum - origin) / delta))
            enter, leave = max(enter, a), min(leave, b)
        if enter > leave:
            return False
    return enter <= leave and enter < active_end


def verify_ship_delivery_receipts(
    scenario: ShipDeliveryScenario,
    receipts: list[dict[str, Any]],
    *,
    require_complete: bool = True,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Re-evaluate fixture observations; never accept a runner's success flag.

    Full verification requires every stage, consecutive observations, coherent
    energy/time, the same identities, payload remaining at the destination,
    and a stationary disarmed recovery observation lasting two seconds.
    """
    s = scenario
    coast, destination, altitude = (
        s.offshore_distance_m,
        s.offshore_distance_m + s.urban_distance_m,
        s.cruise_altitude_m,
    )
    ends = {
        "load": [0, 0, 0],
        "launch": [0, 0, altitude],
        "sea_outbound": [coast, 0, altitude],
        "urban_entry": [destination, 0, altitude],
        "deliver": [destination, 0, 0],
        "urban_exit": [coast, 0, altitude],
        "sea_return": [0, 0, altitude],
        "recover": [0, 0, 0],
    }
    reasons: list[str] = _preflight_reasons(s)
    evaluation_time = evaluated_at or datetime.now(timezone.utc)
    previous_receipt_time = None
    identity = None
    previous = None
    delivered = recovered = False
    urban_started = None
    identity_keys = ("run_id", "world_id", "vehicle_id", "payload_id", "ship_id")
    if not receipts or (require_complete and len(receipts) != len(STAGES)):
        reasons.append("incomplete_stage_sequence")
    try:
        for index, receipt in enumerate(receipts):
            if index >= len(STAGES) or receipt["stage"] != STAGES[index]:
                reasons.append("stage_order_mismatch")
                break
            stage = receipt["stage"]
            observed_at = datetime.fromisoformat(receipt["observed_at"])
            if observed_at.tzinfo is None or evaluation_time.tzinfo is None:
                reasons.append("receipt_clock_invalid")
            elif not 0 <= (evaluation_time - observed_at).total_seconds() <= 30:
                reasons.append("receipt_stale_or_future")
            if previous_receipt_time is not None and observed_at < previous_receipt_time:
                reasons.append("receipt_clock_reversed")
            previous_receipt_time = observed_at
            if (
                receipt["sha256"] != _receipt_hash(receipt)
                or receipt["scenario_sha256"] != s.sha256
            ):
                reasons.append(f"{stage}:receipt_binding_mismatch")
            samples = receipt["samples"]
            if len(samples) < 2:
                reasons.append(f"{stage}:missing_observations")
                continue
            if previous is not None and samples[0] != previous:
                reasons.append(f"{stage}:observation_chain_broken")
            if index == 0 and (
                not _near(samples[0]["position_enu_m"], [0, 0, 0])
                or samples[0]["sequence"] != 0
                or samples[0]["time_s"] != 0
                or samples[0]["battery_wh"] != s.battery_wh
                or samples[0]["payload_attached"] is not True
                or samples[0]["landed"] is not True
                or samples[0]["armed"] is not False
            ):
                reasons.append("initial_state_invalid")
            if stage == "urban_entry":
                urban_started = samples[0]["time_s"]
                choice = _urban_choice(s)
                if not isinstance(receipt["decision"], dict) or (
                    receipt["decision"].get("choice") != choice
                    or receipt["decision"].get("source") != "deterministic_fixture_policy"
                    or receipt["decision"].get("prediction_source") != "scripted_fixture_schedule"
                    or receipt["decision"].get("vla_invoked") is not False
                    or receipt["decision"].get("wam_invoked") is not False
                ):
                    reasons.append("urban_decision_mismatch")
                if choice == "wait" and (
                    samples[1]["time_s"] - samples[0]["time_s"] < s.urban_blockage_s
                    or samples[1]["position_enu_m"] != samples[0]["position_enu_m"]
                ):
                    reasons.append("wait_not_observed")
                if (
                    choice == "detour"
                    and max(p["position_enu_m"][1] for p in samples) < s.urban_detour_offset_m
                ):
                    reasons.append("detour_not_observed")
            for n, sample in enumerate(samples):
                sample_identity = {key: sample[key] for key in identity_keys}
                if identity is None:
                    identity = sample_identity
                if (
                    sample_identity != identity
                    or any(not v for v in sample_identity.values())
                    or sample["vehicle_id"] != s.vehicle_id
                    or sample["payload_id"] != s.payload_id
                    or sample["ship_id"] != s.ship_id
                ):
                    reasons.append("identity_mismatch")
                scalars = [
                    sample["time_s"],
                    sample["battery_wh"],
                    *sample["position_enu_m"],
                    *sample["payload_position_enu_m"],
                ]
                if not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
                    for v in scalars
                ):
                    reasons.append("nonfinite_observation")
                    continue
                if sample["source"] != "kinematic_fixture" or sample["scenario_sha256"] != s.sha256:
                    reasons.append("observation_source_mismatch")
                if not isinstance(sample["landed"], bool) or not isinstance(sample["armed"], bool):
                    reasons.append("flight_state_invalid")
                if sample["position_enu_m"][2] > 1e-6 and (
                    sample["landed"] is not False or sample["armed"] is not True
                ):
                    reasons.append("airborne_state_invalid")
                if not _near(sample["ship_position_enu_m"], [0, 0, 0]):
                    reasons.append("ship_not_stationary")
                if sample["battery_wh"] < s.reserve_wh or sample["time_s"] > s.max_mission_s:
                    reasons.append("operating_budget_exceeded")
                if sample["payload_attached"] is True:
                    if delivered or not _near(
                        sample["payload_position_enu_m"], sample["position_enu_m"]
                    ):
                        reasons.append("payload_continuity_broken")
                elif sample["payload_attached"] is False:
                    if stage not in STAGES[4:] or not _near(
                        sample["payload_position_enu_m"], [destination, 0, 0]
                    ):
                        reasons.append("payload_not_at_delivery_site")
                else:
                    reasons.append("payload_state_invalid")
                if n:
                    prior = samples[n - 1]
                    dt = sample["time_s"] - prior["time_s"]
                    if dt <= 0 or sample["sequence"] != prior["sequence"] + 1:
                        reasons.append("observation_time_or_sequence_invalid")
                    if (
                        abs(prior["battery_wh"] - sample["battery_wh"] - s.power_w * dt / 3600)
                        > 1e-6
                    ):
                        reasons.append("energy_observation_inconsistent")
                    delta = dist(sample["position_enu_m"], prior["position_enu_m"])
                    if delta > (s.airspeed_mps + s.wind_mps + 2) * max(dt, 0) + 1e-6:
                        reasons.append("unobserved_teleport")
                    point, previous_point = sample["position_enu_m"], prior["position_enu_m"]
                    horizontal = hypot(point[0] - previous_point[0], point[1] - previous_point[1])
                    speed = s.airspeed_mps
                    if max(point[0], previous_point[0]) <= coast:
                        speed += s.wind_mps if point[0] > previous_point[0] else -s.wind_mps
                    if horizontal > speed * max(dt, 0) + 1e-6:
                        reasons.append("groundspeed_limit_exceeded")
                    if abs(point[2] - previous_point[2]) > 2 * max(dt, 0) + 1e-6:
                        reasons.append("vertical_speed_limit_exceeded")
                    if horizontal > 1e-6 and (
                        abs(point[2] - altitude) > 1e-6 or abs(previous_point[2] - altitude) > 1e-6
                    ):
                        reasons.append("cruise_altitude_not_maintained")
                    if urban_started is not None and _segment_hits_obstacle(
                        prior, sample, s, urban_started
                    ):
                        reasons.append("urban_obstacle_contact")
                    if (
                        prior["payload_attached"]
                        and not sample["payload_attached"]
                        and (
                            stage != "deliver"
                            or prior["landed"] is not True
                            or prior["armed"] is not False
                            or not _near(prior["position_enu_m"], [destination, 0, 0])
                        )
                    ):
                        reasons.append("release_before_landing")
                if (
                    urban_started is not None
                    and sample["time_s"] - urban_started < s.urban_blockage_s
                ):
                    x, y, _ = sample["position_enu_m"]
                    if (
                        coast + s.urban_distance_m * 0.4 <= x <= coast + s.urban_distance_m * 0.6
                        and abs(y) < 25
                    ):
                        reasons.append("urban_obstacle_contact")
            last = samples[-1]
            if not _near(last["position_enu_m"], ends[stage]):
                reasons.append(f"{stage}:endpoint_not_observed")
            if stage == "deliver":
                delivered = (
                    last["payload_attached"] is False
                    and last["landed"] is True
                    and last["armed"] is False
                )
                if not delivered:
                    reasons.append("delivery_not_observed")
            if stage == "recover":
                recovered = (
                    last["landed"] is True
                    and last["armed"] is False
                    and _near(samples[-2]["position_enu_m"], [0, 0, 0])
                    and samples[-2]["landed"] is True
                    and samples[-2]["armed"] is False
                    and last["time_s"] - samples[-2]["time_s"] >= 2
                    and last["payload_attached"] is False
                )
                if not recovered:
                    reasons.append("stable_recovery_not_observed")
            previous = last
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError):
        reasons.append("malformed_receipt")
    reasons = list(dict.fromkeys(reasons))
    return {
        "verified": not reasons,
        "reasons": reasons,
        "delivery_verified": delivered and not reasons,
        "recovery_verified": recovered and not reasons,
        "identity_continuity_verified": identity is not None and not reasons,
        "scope": "kinematic_fixture_only",
        "evaluated_at": evaluation_time.isoformat(),
    }


def run_ship_delivery_fixture(
    scenario: ShipDeliveryScenario,
    *,
    operator_approved: bool = False,
) -> dict[str, Any]:
    """Execute a bounded local fixture only after explicit fixture approval."""
    s = scenario
    parent = build_ship_delivery_contract(s)
    approval = (
        build_parent_mission_approval_binding(
            contract=parent,
            operator_approval_ref="operator:explicit-fixture-opt-in",
            authority_bundle_ref=f"fixture:ship:{s.sha256}",
        )
        if operator_approved is True
        else None
    )
    world = _FixtureWorld(s)
    receipts: list[dict[str, Any]] = []
    preflight = _preflight_reasons(s)

    def stage_runner(index: int):
        def run() -> dict[str, Any]:
            binding = parent.stages[index]
            reasons = preflight if index == 0 else []
            if not reasons:
                receipts.append(_execute_stage(world, binding.stage_ref))
                reasons = verify_ship_delivery_receipts(s, receipts, require_complete=False)[
                    "reasons"
                ]
            return {
                "contract_id": binding.child_contract_id,
                "contract_sha256": binding.child_contract_sha256,
                "predicate_package_id": binding.predicate_package.package_id,
                "predicate_package_version": binding.predicate_package.package_version,
                "predicate_package_sha256": binding.predicate_package.content_sha256,
                "status": "not_satisfied" if reasons else "satisfied",
                "evaluated_outcome_claim": not reasons,
                "actual_verification_basis": "deterministic",
                "predicate_package_evaluated": True,
                "reasons": reasons,
                "approval_created": False,
                "dispatch_authority_created": False,
                "runtime_effect_requested": False,
                "operational_closure_created": False,
                "physical_execution_invoked": False,
            }

        return run

    coordinator = run_parent_mission_coordinator(
        contract=parent,
        approval=approval,
        stage_runners={stage: stage_runner(i) for i, stage in enumerate(STAGES)},
    )
    verification = verify_ship_delivery_receipts(s, receipts)
    completed = coordinator["coordinator_status"] == "stages_satisfied" and verification["verified"]
    return {
        "schema_version": "ship_delivery_fixture_run.v1",
        "status": "completed" if completed else "blocked",
        "scenario": s.model_dump(mode="json"),
        "scenario_sha256": s.sha256,
        "parent_contract_sha256": parent.parent_mission_sha256,
        "fixture_mission_completed": completed,
        **verification,
        "stage_receipts": deepcopy(receipts),
        "trajectory": [sample for receipt in receipts for sample in receipt["samples"][1:]],
        "decisions": [r["decision"] for r in receipts if r["decision"] is not None],
        "elapsed_s": world.time_s,
        "remaining_battery_wh": world.battery_wh,
        "coordinator": coordinator,
        "blocking_reasons": list(
            dict.fromkeys(preflight + coordinator["blocking_reasons"] + verification["reasons"])
        ),
        "mission_completion_claimed": False,
        "physical_execution_invoked": False,
        "px4_runtime_invoked": False,
        "vla_invoked": False,
        "wam_invoked": False,
        "limitations": list(LIMITATIONS),
    }
