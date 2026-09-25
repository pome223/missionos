"""Mission-level Go2 delivery supervision over a goal-level simulator adapter.

One operator-authorized mission contains outbound and return goals. Locomotion
and local path following belong to the simulator backend. Arrival never substitutes
for a recipient receipt. Optional host Agent proposals are constrained by explicit
rules and the initial operator authorization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Any, Callable

from src.runtime.go2_delivery_navigation import DeliveryGoal, dispatch_navigation
from src.runtime.go2_supervision import MAX_DECISIONS, MAX_WAIT_SIM_S, guard_decision


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Go2DeliveryPlan:
    mission_id: str
    destination: str = "meeting_room_a"
    parcel_id: str = "demo_parcel_001"
    home_xy: tuple[float, float] = (-2.5, 0.0)
    destination_xy: tuple[float, float] = (2.5, 0.0)
    arrival_tolerance_m: float = 0.4
    maximum_speed_mps: float = 0.25
    maximum_retries_per_leg: int = 1
    supervision_mode: str = "rules"

    def __post_init__(self):
        object.__setattr__(self, "home_xy", tuple(self.home_xy))
        object.__setattr__(self, "destination_xy", tuple(self.destination_xy))

    def validate(self):
        if not self.mission_id.strip() or not self.parcel_id.strip():
            raise ValueError("mission and parcel identities are required")
        if self.destination != "meeting_room_a":
            raise ValueError("destination is not in the declared office")
        if not 0 < self.arrival_tolerance_m <= 0.5:
            raise ValueError("arrival tolerance outside simulation bounds")
        if not 0 < self.maximum_speed_mps <= 0.25:
            raise ValueError("speed outside adapter capability")
        if self.maximum_retries_per_leg not in (0, 1):
            raise ValueError("at most one retry per leg")
        if self.supervision_mode not in ("rules", "agent"):
            raise ValueError("unknown supervision mode")
        for xy in (self.home_xy, self.destination_xy):
            if len(xy) != 2 or not all(math.isfinite(v) for v in xy) or math.hypot(*xy) > 3:
                raise ValueError("goal outside approved map radius")
        if math.dist(self.home_xy, self.destination_xy) < 1:
            raise ValueError("delivery requires distinct pickup and destination zones")

    @property
    def digest(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class Go2DeliveryMission:
    def __init__(
        self,
        plan: Go2DeliveryPlan,
        client: Any,
        *,
        operator_approval_ref: str,
        approved_plan_sha256: str,
        emit: Callable[[dict], None],
        supervisor: Callable | None = None,
    ):
        plan.validate()
        if not operator_approval_ref.strip() or approved_plan_sha256 != plan.digest:
            raise ValueError("operator approval must bind this exact delivery plan")
        self.plan, self.client, self.emit = plan, client, emit
        if plan.supervision_mode == "agent" and supervisor is None:
            raise ValueError("agent mode requires an explicit supervisor connection")
        self.supervisor = supervisor
        self.decisions: list[dict] = []
        self.waits_used = 0.0
        self.next_decision_id = None
        self.approval_ref = operator_approval_ref
        self.events: list[dict] = []
        self.legs: list[dict] = []
        self.status = "approved"
        self.arrived_at: str | None = None
        self.receipt: dict | None = None
        self.record(
            "mission_approved",
            plan=asdict(plan),
            plan_sha256=plan.digest,
            operator_approval_ref=operator_approval_ref,
        )

    def record(self, event: str, **detail):
        item = dict(event=event, observed_at=now(), mission_id=self.plan.mission_id, **detail)
        self.events.append(item)
        self.emit(item)

    def _navigate(self, leg: str, xy: tuple[float, float], yaw: float) -> bool:
        expected = {"outbound": self.plan.destination_xy, "returning": self.plan.home_xy}
        if leg not in expected or tuple(xy) != expected[leg]:
            raise ValueError("navigation goal is not in the approved mission")
        for attempt in range(self.plan.maximum_retries_per_leg + 1):
            self.status = leg
            decision_id, self.next_decision_id = self.next_decision_id, None
            self.record(
                "navigation_requested",
                leg=leg,
                goal_xy=xy,
                attempt=attempt,
                source_decision_id=decision_id,
            )
            state = self.client.read_state()
            goal = DeliveryGoal(
                x_m=xy[0],
                y_m=xy[1],
                yaw_rad=yaw,
                tolerance_m=0.25,
                max_speed_mps=self.plan.maximum_speed_mps,
                label=leg,
            )
            evidence = dispatch_navigation(
                client=self.client,
                goal=goal,
                state=state,
                action_ref=f"{self.plan.mission_id}:{leg}:{attempt}",
                operator_approval_ref=self.approval_ref,
            ).model_dump(mode="json")
            state = self.client.read_state()
            position = state.get("ground_truth_xy")
            error = math.dist(position, xy) if position is not None else None
            verified = (
                evidence["completion_claimed"]
                and state.get("telemetry_fresh") is True
                and state.get("base_stable") is True
                and error is not None
                and error <= self.plan.arrival_tolerance_m
            )
            item = dict(
                leg=leg,
                source_decision_id=decision_id,
                attempt=attempt,
                adapter_evidence=evidence,
                ground_truth_error_m=error,
                state=state,
                verified=bool(verified),
            )
            self.legs.append(item)
            self.record("navigation_verified" if verified else "navigation_failed", **item)
            if verified:
                return True
            # Retry only a clean navigation abort, within the approved mission.
            # Falls, stale state, rejected dispatch and cancellation never retry.
            if (
                attempt < self.plan.maximum_retries_per_leg
                and state.get("navigation_status") == "aborted"
                and state.get("telemetry_fresh") is True
                and state.get("base_stable") is True
                and state.get("geofence_satisfied") is True
                and not state.get("safety_violation_observed")
            ):
                if self.plan.supervision_mode == "agent":
                    if self._supervise(leg, attempt):
                        continue
                    return False
                self.status = "waiting_to_retry"
                self.record(
                    "bounded_retry_selected",
                    leg=leg,
                    decision_source="mission_rule",
                    approval_ref=self.approval_ref,
                    approved_plan_sha256=self.plan.digest,
                )
                self.client.wait(5.0)
            else:
                break
        self.status = "needs_attention"
        self.record("operator_attention_required", leg=leg)
        return False

    def _supervise(self, leg, attempt):
        while len(self.decisions) < MAX_DECISIONS:
            state = self.client.supervision_state(self.plan, leg)
            request = dict(
                schema_version="go2_supervision_observation.v1",
                observation_id=f"decision_{len(self.decisions) + 1}",
                mission_id=self.plan.mission_id,
                approved_plan_sha256=self.plan.digest,
                leg=leg,
                goal_attempt=attempt,
                state=state,
                remaining_wait_sim_s=MAX_WAIT_SIM_S - self.waits_used,
                remaining_decisions=MAX_DECISIONS - len(self.decisions),
                prior_decisions=[d.get("proposal", {}) for d in self.decisions],
                authorized_actions=["wait", "reroute", "return_home", "request_operator"],
                mission_objective="Deliver to meeting room A, observe receipt, return to reception. An undelivered return is not delivery completion.",
            )
            response = self.supervisor(request, self.client)
            current = self.client.supervision_state(self.plan, leg)
            reasons = guard_decision(request, response, current, waits_used=self.waits_used)
            entry = dict(observation=request, **response, rule_blocking_reasons=reasons)
            self.decisions.append(entry)
            self.record("go2_supervision_decided", decision=entry)
            if reasons:
                self.status = "needs_attention"
                self.record("go2_supervision_blocked", reasons=reasons)
                return False
            proposal = response["proposal"]
            action = proposal["action"]
            self.record(
                "go2_supervision_action_authorized",
                action=action,
                observation_id=request["observation_id"],
                approval_ref=self.approval_ref,
                approved_plan_sha256=self.plan.digest,
            )
            if action == "wait":
                self.client.phase = "Waiting for passage"
                seconds = proposal["wait_seconds"]
                self.waits_used += seconds
                self.record("go2_supervision_wait_started", duration_sim_s=seconds)
                self.client.wait(seconds)
                self.record(
                    "go2_supervision_wait_observed",
                    state=self.client.supervision_state(self.plan, leg),
                )
                continue
            if action == "reroute":
                self.next_decision_id = request["observation_id"]
                self.record(
                    "bounded_retry_selected",
                    leg=leg,
                    decision_source="missionos_go2_supervisor_agent",
                    observation_id=request["observation_id"],
                    approval_ref=self.approval_ref,
                )
                return True
            if action == "return_home":
                self.next_decision_id = request["observation_id"]
                self.record("go2_undelivered_return_requested", delivery_complete=False)
                if self._navigate("returning", self.plan.home_xy, math.pi):
                    self._verify_return(delivered=False)
                return False
            self.status = "needs_attention"
            self.record(
                "operator_attention_required", leg=leg, reason="supervisor_requested_operator"
            )
            return False
        self.status = "needs_attention"
        self.record("operator_attention_required", leg=leg, reason="supervision_budget_exhausted")
        return False

    def start(self) -> bool:
        if self.status != "approved":
            raise ValueError("mission has already started")
        state = self.client.read_state()
        position = state.get("ground_truth_xy")
        if (
            not state.get("telemetry_fresh")
            or not state.get("base_stable")
            or position is None
            or math.dist(position, self.plan.home_xy) > self.plan.arrival_tolerance_m
        ):
            self.status = "needs_attention"
            self.record("pickup_zone_not_verified", state=state)
            return False
        if not self._navigate("outbound", self.plan.destination_xy, 0.0):
            return False
        self.arrived_at = now()
        self.status = "awaiting_receipt"
        self.record(
            "recipient_confirmation_required",
            destination=self.plan.destination,
            parcel_id=self.plan.parcel_id,
            delivery_complete=False,
        )
        return True

    def confirm_receipt(self, receipt: dict) -> bool:
        if self.status != "awaiting_receipt" or self.arrived_at is None:
            raise ValueError("receipt is only accepted after verified arrival")
        issued = datetime.fromisoformat(receipt["issued_at"])
        arrival = datetime.fromisoformat(self.arrived_at)
        if (
            receipt.get("mission_id") != self.plan.mission_id
            or receipt.get("parcel_id") != self.plan.parcel_id
            or receipt.get("destination") != self.plan.destination
            or receipt.get("received") is not True
            or receipt.get("source") not in ("simulation_recipient", "operator")
            or issued.tzinfo is None
            or issued < arrival
            or issued > datetime.now(timezone.utc)
        ):
            raise ValueError("receipt does not confirm this delivery after arrival")
        state = self.client.read_state()
        position = state.get("ground_truth_xy")
        if (
            not state.get("telemetry_fresh")
            or not state.get("base_stable")
            or position is None
            or math.dist(position, self.plan.destination_xy) > self.plan.arrival_tolerance_m
        ):
            raise ValueError("robot is no longer verified at the recipient")
        self.receipt = dict(receipt)
        self.record("parcel_receipt_observed", receipt=receipt)
        if not self._navigate("returning", self.plan.home_xy, math.pi):
            return False
        return self._verify_return(delivered=True)

    def _verify_return(self, *, delivered):
        self.status = "verifying_return"
        self.record("terminal_hold_started", duration_sim_s=5.0)
        for _ in range(5):
            self.client.wait(1.0)
            state = self.client.read_state()
            position = state.get("ground_truth_xy")
            if (
                not state.get("telemetry_fresh")
                or not state.get("base_stable")
                or not state.get("geofence_satisfied")
                or state.get("safety_violation_observed")
                or state.get("operator_cancel_requested")
                or state.get("obstacle_contacts")
                or position is None
                or math.dist(position, self.plan.home_xy) > self.plan.arrival_tolerance_m
            ):
                self.status = "needs_attention"
                self.record("terminal_hold_failed", state=state)
                return False
        self.record("terminal_hold_verified", duration_sim_s=5.0, state=state)
        self.status = "completed" if delivered else "returned_undelivered"
        self.record(
            "mission_completed" if delivered else "undelivered_return_verified",
            completion_scope="simulated_delivery_and_return" if delivered else "none",
        )
        return True

    def summary(self) -> dict:
        return dict(
            schema_version="missionos_go2_delivery_result.v1",
            mission_id=self.plan.mission_id,
            status=self.status,
            plan_sha256=self.plan.digest,
            legs=self.legs,
            receipt=self.receipt,
            completion_claimed=self.status == "completed",
            completion_scope="simulated_delivery_and_return"
            if self.status == "completed"
            else "none",
            physical_execution_invoked=False,
            llm_judgment_invoked=any(
                d.get("invocation", {}).get("provider") == "google_adk_litellm_deepseek"
                and d.get("invocation", {}).get("response_sha256")
                not in (None, sha256(b"").hexdigest())
                for d in self.decisions
            ),
            supervision_mode=self.plan.supervision_mode,
            supervision_decisions=self.decisions,
            parcel_model="logical_parcel_with_explicit_recipient_event",
            backend=self.client.metadata,
            events=self.events,
        )
