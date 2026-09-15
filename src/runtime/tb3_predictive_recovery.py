"""One approved simulation wait, connected to real Recovery/Assurance judgments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
import time
from pathlib import Path
import uuid

from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.intelligence.mission_assurance_policy import AssurancePolicy, PolicyStore, digest
from src.intelligence.missionos_mission_incident_graph import run_missionos_mission_incident_graph
from src.intelligence.navigation_agents import PredictionAssuranceJudge, assurance, infer
from src.intelligence.navigation_prediction import NavigationPrediction
from scripts.tb3_prediction.route_feasibility import validate_detour_plan

ACTION = "wait_until_observed_clear"
DETOUR_PARAMETERS = {"via_x_m": 0.35, "via_y_m": -0.7, "goal_x_m": 1.0, "goal_y_m": 0.0}
CONTINUE_PARAMETERS = {"goal_x_m": 1.0, "goal_y_m": 0.0}
PARAMETERS = {"maximum_wait_s": 5.0, "resume_goal_x_m": 1.0, "resume_goal_y_m": 0.0}


def wait_policy(mission_id: str, contract: dict) -> AssurancePolicy:
    return AssurancePolicy.model_validate(
        {
            "version": 1,
            "policy_id": "tb3-predicted-clearance-wait",
            "mission_id": mission_id,
            "mission_contract_sha256": digest(contract),
            "execution_scope": "simulator",
            "mode": "bounded",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            "max_observation_age_seconds": 2,
            "max_total_actions": 1,
            "preserve": ["robot_stationary", "prediction_current", "mission_contract_unchanged"],
            "actions": {
                ACTION: {
                    "parameters": {k: {"minimum": v, "maximum": v} for k, v in PARAMETERS.items()},
                    "max_uses": 1,
                }
            },
            "on_unresolved": "request_human",
        }
    )


def validate_route_explanation(output):
    # Route safety is a runtime-owned fact, never a free-text Agent guarantee.
    text = " ".join(str(output.get(k, "")) for k in ("rationale", "uncertainty"))
    if re.search(r"collis|collid|\bsafe|safety|衝突|安全", text, re.IGNORECASE):
        raise ValueError("route_safety_explanation_is_runtime_owned")


class PredictiveRecovery:
    def __init__(
        self,
        folder: Path,
        *,
        operator_authorized: bool,
        detour_authorized: bool = False,
        continue_authorized: bool = False,
    ):
        if not operator_authorized:
            raise ValueError("explicit_local_simulation_authorization_required")
        self.folder = folder
        self.mission_id = "tb3-predictive-" + uuid.uuid4().hex
        self.contract = {
            "robot": "turtlebot3",
            "scope": "simulator",
            "goal_xy_m": [1.0, 0.0],
            "maximum_wait_s": 5.0,
            "resume_requires_observed_clearance": True,
            "forecast_horizon_s": 4.0,
            "extension_requires_observed_clearance_progress": True,
            "maximum_observation_extension_s": 1.0,
            "no_physical_hardware": True,
        }
        if detour_authorized:
            self.contract.pop("resume_requires_observed_clearance")
            self.contract["direct_resume_requires_observed_clearance"] = True
            self.contract["failed_wait_detour_requires_separate_policy"] = True
            self.contract["fixed_detour_parameters"] = DETOUR_PARAMETERS.copy()
        self.store = PolicyStore(folder / "operator-policy.sqlite")
        self.policy = wait_policy(self.mission_id, self.contract)
        self.store.approve(
            self.policy,
            operator="local_operator_explicit_approve_bounded_wait_flag",
            expected_sha256=self.policy.sha256,
        )
        (folder / "approved-wait-policy.json").write_text(
            json.dumps(self.store.context(self.policy.sha256), indent=2)
        )
        self.records = []
        self.grant = None
        self.resume_consumed = False
        self.detour_consumed = False
        self.continue_policy = None
        self.continue_pending = None
        if continue_authorized:
            data = wait_policy(self.mission_id, self.contract).model_dump()
            data.update(
                policy_id="tb3-observed-original-route-continuation",
                preserve=["robot_stationary", "actual_route_clear", "mission_contract_unchanged"],
                actions={
                    "resume_original_route": {
                        "parameters": {
                            k: {"minimum": v, "maximum": v} for k, v in CONTINUE_PARAMETERS.items()
                        },
                        "max_uses": 1,
                    }
                },
            )
            self.continue_policy = AssurancePolicy.model_validate(data)
            self.store.approve(
                self.continue_policy,
                operator="local_operator_explicit_approve_observed_continue_flag",
                expected_sha256=self.continue_policy.sha256,
            )
            (folder / "approved-continue-policy.json").write_text(
                json.dumps(self.store.context(self.continue_policy.sha256), indent=2)
            )
        self.detour_policy = None
        if detour_authorized:
            data = wait_policy(self.mission_id, self.contract).model_dump()
            data.update(
                policy_id="tb3-observed-wait-failure-detour",
                max_total_actions=2,
                preserve=["robot_stationary", "actual_wait_failed", "mission_contract_unchanged"],
                actions={
                    "reroute": {
                        "parameters": {
                            k: {"minimum": v, "maximum": v} for k, v in DETOUR_PARAMETERS.items()
                        },
                        "max_uses": 1,
                    }
                },
            )
            self.detour_policy = AssurancePolicy.model_validate(data)
            self.store.approve(
                self.detour_policy,
                operator="local_operator_explicit_approve_bounded_detour_flag",
                expected_sha256=self.detour_policy.sha256,
            )
            (folder / "approved-detour-policy.json").write_text(
                json.dumps(self.store.context(self.detour_policy.sha256), indent=2)
            )

    def live(self):
        deadline = time.monotonic() + 0.25
        while True:
            live = json.loads((self.folder / "live-observation.json").read_text())
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(live["observed_at"])
            ).total_seconds()
            camera_age = live["sim_s"] - live["camera_stamp_sim_s"]
            if 0 <= age <= 2 and math.isfinite(camera_age) and 0 <= camera_age < 0.5:
                break
            if not (-0.05 < camera_age < 0 and 0 <= age <= 2) or time.monotonic() >= deadline:
                raise ValueError("fresh_actual_observation_required")
            # ROS image and clock callbacks can arrive out of order. Wait for actual
            # clock catch-up; never alter timestamps or accept a future/stale frame.
            time.sleep(0.01)
        if not isinstance(live.get("image_sha256"), str) or len(live["image_sha256"]) != 64:
            raise ValueError("actual_image_binding_required")
        return live

    def persist(self):
        (self.folder / "agent-chain.json").write_text(
            json.dumps(
                {
                    "mission_id": self.mission_id,
                    "contract": self.contract,
                    "policy_sha256": self.policy.sha256,
                    "events": self.records,
                },
                indent=2,
            )
        )

    def plan(self, packet: dict):
        if self.grant is not None or self.resume_consumed:
            return {"status": "blocked", "reason": "one_wait_already_reserved"}
        prediction = NavigationPrediction(
            request=packet["request"], prediction=packet["prediction"], clock_domain=self.mission_id
        )
        live = self.live()
        if not prediction.current(live["sim_s"], self.mission_id):
            self.records.append(
                {
                    "phase": "stale_prediction",
                    "reason": "prediction_expired_before_assurance",
                    "prediction_ref": prediction.read()["prediction_ref"],
                    "observed_now_sim_s": live["sim_s"],
                    "observation_at_sim_s": prediction.read()["observed_at_sim_s"],
                    "target_at_sim_s": prediction.read()["target_at_sim_s"],
                    "dispatch_authority_created": False,
                }
            )
            self.persist()
            return {"status": "reobserve", "reason": "prediction_expired_before_assurance"}
        evidence = {
            "prediction": prediction.read(),
            "observation": packet["observation"],
            "available_actions": {
                "wait_until_observed_clear": PARAMETERS,
                "detour": "existing bounded Nav2 detour",
            },
            "mission_contract": self.contract,
        }
        initial = assurance("initial", evidence, mission_id=self.mission_id, contract=self.contract)
        self.records.append(
            {
                "phase": "initial_assurance",
                "result": initial,
                "prediction_ref": prediction.read()["prediction_ref"],
            }
        )
        self.persist()
        if not prediction.current(self.live()["sim_s"], self.mission_id):
            return {"status": "reobserve", "reason": "prediction_expired_during_assurance"}
        if (
            initial["judgment_status"] == "proposal_guardrail_passed"
            and initial["proposed_response_kind"] == "continue"
            and self.continue_policy is not None
        ):
            self.continue_pending = prediction.read()
            return {
                "status": "observe_original_route",
                "prediction_ref": prediction.read()["prediction_ref"],
                "dispatch_authority_created": False,
            }
        if (
            initial["judgment_status"] != "proposal_guardrail_passed"
            or initial["proposed_response_kind"] != "hold"
        ):
            return {"status": "blocked", "reason": "initial_assurance_did_not_request_intervention"}
        recovery_record = {}

        def recovery_runner(**_):
            nonlocal recovery_record
            output, invocation = infer("recovery", "select_existing_response", evidence)
            if set(output) != {"action", "prediction_ref", "rationale", "uncertainty"} or any(
                not isinstance(v, str) or not v.strip() for v in output.values()
            ):
                raise ValueError("recovery_output_fields_invalid")
            if (
                output["prediction_ref"] != prediction.read()["prediction_ref"]
                or not output["rationale"]
            ):
                raise ValueError("recovery_prediction_binding_missing")
            recovery_record = {"output": output, "invocation": invocation}
            self.records.append({"phase": "recovery", "result": recovery_record})
            self.persist()
            if output["action"] != ACTION:
                return {
                    "runtime_status": "guardrail_blocked",
                    "blocking_reasons": ["bounded_wait_not_selected:" + str(output["action"])],
                }
            live = self.live()
            stationary = live.get("robot_stationary") is True
            return {
                "runtime_status": "proposal_guardrail_passed",
                "assessment": {
                    "selected_bounded_action": ACTION,
                    "proposed_parameters": PARAMETERS,
                    "compiled_candidate": {
                        "prediction_ref": prediction.read()["prediction_ref"],
                        "target_at_sim_s": prediction.read()["target_at_sim_s"],
                    },
                    "action_feasibility": {
                        "action": ACTION,
                        "feasibility_status": "verified_feasible" if stationary else "unverified",
                        "source": "fresh simulator odometry confirms stationary wait; resume separately guarded by observed camera clearance",
                    },
                },
                "agent_invocations": [invocation],
            }

        context = {
            "task_id": self.mission_id,
            "mission_phase": "predicted_obstacle_clearance",
            "execution_scope": "simulator",
            "mission_contract": self.contract,
            "observations": evidence,
            "constraints": {"assurance_policy": self.store.context(self.policy.sha256)},
            "allowed_response_kinds": ["hold", "continue", "operator_escalation"],
        }
        graph = run_missionos_mission_incident_graph(
            telemetry_snapshot=evidence,
            mission_context=context,
            recovery_policy=self.policy.model_dump(),
            recovery_runner=recovery_runner,
            mission_assurance_agent=MissionAssuranceAgent(
                PredictionAssuranceJudge("candidate_alignment", evidence)
            ),
            mission_assurance_timeout_seconds=45,
        )
        self.records.append({"phase": "recovery_assurance_graph", "result": graph})
        live = self.live()
        if not prediction.current(live["sim_s"], self.mission_id):
            self.records.append(
                {
                    "phase": "stale_prediction",
                    "prediction_ref": prediction.read()["prediction_ref"],
                    "observed_now_sim_s": live["sim_s"],
                }
            )
            self.persist()
            return {
                "status": "reobserve",
                "reason": "prediction_target_elapsed_during_agent_processing",
            }
        facts = {
            "mission_id": self.mission_id,
            "execution_scope": "simulator",
            "mission_contract_sha256": digest(self.contract),
            "observed_at": live["observed_at"],
            "source_ref": live["image_sha256"],
            "predicates": {
                "robot_stationary": live["robot_stationary"],
                "prediction_current": prediction.current(live["sim_s"], self.mission_id),
                "mission_contract_unchanged": True,
            },
        }
        request = {
            "task_id": self.mission_id,
            "proposal_id": "prediction-" + prediction.read()["prediction_ref"],
            "recovery_action": ACTION,
            "recovery_parameters": PARAMETERS,
        }
        grant = self.store.check(
            self.policy.sha256, graph=graph, request=request, facts=facts, consume=True
        )
        self.records.append({"phase": "rules", "result": grant, "facts": facts})
        self.persist()
        if not grant["policy_authorized"]:
            return {
                "status": "blocked",
                "reason": "policy_denied",
                "blocking_reasons": grant["blocking_reasons"],
            }
        self.grant = {
            "prediction": prediction.read(),
            "policy": grant,
            "graph_id": graph["mission_incident_graph_id"],
        }
        return {
            "status": "authorized_wait",
            "target_at_sim_s": prediction.read()["target_at_sim_s"],
            "prediction_ref": prediction.read()["prediction_ref"],
            "authority": grant,
            "recovery_proposal": recovery_record,
            "graph_id": graph["mission_incident_graph_id"],
        }

    def post_wait(self, packet):
        if self.grant is None or self.resume_consumed:
            raise ValueError("approved_wait_required_before_resume")
        if packet["verification"].get("passed") is not True:
            return {"status": "blocked", "reason": "actual_post_wait_clearance_failed"}
        observation = packet["observation"]
        if (
            observation["sim_s"] < self.grant["prediction"]["target_at_sim_s"]
            or packet["verification"].get("image", {}).get("stamp_s")
            != observation["camera_stamp_sim_s"]
            or observation.get("robot_stationary") is not True
        ):
            return {"status": "blocked", "reason": "post_wait_observation_binding_failed"}
        self.live()
        evidence = {
            "prediction": self.grant["prediction"],
            "post_wait_verification": packet["verification"],
            "observation": packet["observation"],
            "mission_contract": self.contract,
            "available_actions": ["continue", "hold", "operator_escalation"],
        }
        result = assurance(
            "post_wait", evidence, mission_id=self.mission_id, contract=self.contract
        )
        self.records.append(
            {"phase": "post_wait_assurance", "result": result, "evidence": evidence}
        )
        self.persist()
        self.resume_consumed = True
        return {
            "status": "continue"
            if result["judgment_status"] == "proposal_guardrail_passed"
            and result["proposed_response_kind"] == "continue"
            else "blocked",
            "assurance": result,
            "approved_wait_graph_id": self.grant["graph_id"],
        }

    def observed_continue(self, packet):
        if (
            self.continue_policy is None
            or self.continue_pending is None
            or self.grant is not None
            or self.resume_consumed
        ):
            return {
                "status": "blocked",
                "reason": "unused_separately_approved_original_route_required",
            }
        verification, observation = packet["verification"], packet["observation"]
        current = self.live()
        exposure = verification.get("observed_exposure")
        if (
            verification.get("passed") is not True
            or type(exposure) not in (int, float)
            or not 0 <= exposure <= 0.06
            or not 0 <= current["sim_s"] - observation["sim_s"] < 0.5
            or verification.get("image", {}).get("stamp_s") != observation["camera_stamp_sim_s"]
            or observation.get("robot_stationary") is not True
            or current.get("robot_stationary") is not True
            or not isinstance(verification.get("image", {}).get("sha256"), str)
            or len(verification["image"]["sha256"]) != 64
        ):
            return {"status": "blocked", "reason": "actual_clear_observation_binding_required"}
        route = validate_detour_plan(packet.get("route_plan", {}), [[1.0, 0.0]], current["sim_s"])
        source = {"verification": verification, "observation": observation}
        evidence = {
            "actual_clearance": source,
            "observation_ref": digest(source),
            "route_feasibility": route,
            "mission_contract": self.contract,
            "approved_continue_policy": self.store.context(self.continue_policy.sha256),
            "available_actions": {
                "resume_original_route": CONTINUE_PARAMETERS,
                "operator_review": {},
            },
        }
        proposal = {}

        def recovery_runner(**_):
            nonlocal proposal
            output, invocation = infer("recovery", "observed_continue", evidence)
            if (
                set(output) != {"action", "observation_ref", "rationale", "uncertainty"}
                or any(not isinstance(v, str) or not v.strip() for v in output.values())
                or output["observation_ref"] != evidence["observation_ref"]
            ):
                raise ValueError("continue_observation_binding_required")
            proposal = {"output": output, "invocation": invocation}
            self.records.append(
                {"phase": "continue_recovery", "result": proposal, "evidence": evidence}
            )
            self.persist()
            validate_route_explanation(output)
            if output["action"] != "resume_original_route":
                return {
                    "runtime_status": "guardrail_blocked",
                    "blocking_reasons": ["continue_not_selected"],
                }
            return {
                "runtime_status": "proposal_guardrail_passed",
                "assessment": {
                    "selected_bounded_action": "resume_original_route",
                    "proposed_parameters": CONTINUE_PARAMETERS,
                    "compiled_candidate": {"observation_ref": evidence["observation_ref"]},
                    "action_feasibility": {
                        "action": "resume_original_route",
                        "feasibility_status": "verified_feasible",
                        "source": "Observed clear stationary state and actual bounded original-route plan; dispatch requires fresh checks",
                    },
                },
                "agent_invocations": [invocation],
            }

        context = {
            "task_id": self.mission_id,
            "mission_phase": "observed_original_route_clear",
            "execution_scope": "simulator",
            "mission_contract": self.contract,
            "observations": evidence,
            "constraints": {"assurance_policy": self.store.context(self.continue_policy.sha256)},
            "allowed_response_kinds": ["continue", "hold", "operator_escalation"],
        }
        graph = run_missionos_mission_incident_graph(
            telemetry_snapshot=evidence,
            mission_context=context,
            recovery_policy=self.continue_policy.model_dump(),
            recovery_runner=recovery_runner,
            mission_assurance_agent=MissionAssuranceAgent(
                PredictionAssuranceJudge("continue_alignment", evidence)
            ),
            mission_assurance_timeout_seconds=45,
        )
        self.records.append({"phase": "continue_assurance_graph", "result": graph})
        current = self.live()
        current_exposure = current.get("observed_exposure")
        facts = {
            "mission_id": self.mission_id,
            "execution_scope": "simulator",
            "mission_contract_sha256": digest(self.contract),
            "observed_at": current["observed_at"],
            "source_ref": current["image_sha256"],
            "predicates": {
                "robot_stationary": current["robot_stationary"],
                "actual_route_clear": type(current_exposure) in (int, float)
                and 0 <= current_exposure <= 0.06,
                "mission_contract_unchanged": True,
            },
        }
        request = {
            "task_id": self.mission_id,
            "proposal_id": "continue-" + evidence["observation_ref"],
            "recovery_action": "resume_original_route",
            "recovery_parameters": CONTINUE_PARAMETERS,
        }
        grant = self.store.check(
            self.continue_policy.sha256, graph=graph, request=request, facts=facts, consume=True
        )
        self.records.append(
            {
                "phase": "continue_rules",
                "result": grant,
                "facts": facts,
                "observation_ref": evidence["observation_ref"],
                "live_observation": current,
            }
        )
        self.persist()
        if not grant["policy_authorized"]:
            return {"status": "blocked", "reason": "continue_policy_denied", "authority": grant}
        self.resume_consumed = True
        return {
            "status": "authorized_continue",
            "authority": grant,
            "recovery_proposal": proposal,
            "observation_ref": evidence["observation_ref"],
            "waypoints": [[1.0, 0.0]],
            "dispatch_before_sim_s": current["sim_s"] + 2.0,
            "graph_id": graph["mission_incident_graph_id"],
            "route_safety": "Route planned; collision freedom is not guaranteed.",
        }

    def failed_wait(self, packet):
        """Propose a separately approved fixed detour from actual failed clearance."""
        if self.grant is None or self.resume_consumed or self.detour_consumed:
            return {"status": "blocked", "reason": "unused_executed_wait_required"}
        if self.detour_policy is None:
            return {"status": "blocked", "reason": "separate_detour_approval_required"}
        verification, observation = packet["verification"], packet["observation"]
        current = self.live()
        exposure = verification.get("observed_exposure")
        if (
            verification.get("passed") is not False
            or type(exposure) not in (int, float)
            or not 0.06 < exposure <= 1
            or observation["sim_s"] < self.grant["prediction"]["target_at_sim_s"]
            or verification.get("image", {}).get("stamp_s") != observation["camera_stamp_sim_s"]
            or not 0 <= current["sim_s"] - observation["sim_s"] < 0.5
            or observation.get("robot_stationary") is not True
            or current.get("robot_stationary") is not True
            or not isinstance(verification.get("image", {}).get("sha256"), str)
            or len(verification["image"]["sha256"]) != 64
        ):
            return {"status": "blocked", "reason": "actual_failed_wait_binding_required"}
        route = validate_detour_plan(
            packet.get("route_plan", {}), [[0.35, -0.7], [1.0, 0.0]], current["sim_s"]
        )
        # This immutable observation is the event being recovered from, not a fresh forecast.
        source = {"verification": verification, "observation": observation}
        evidence = {
            "failed_wait": source,
            "observation_ref": digest(source),
            "approved_detour_policy": self.store.context(self.detour_policy.sha256),
            "route_feasibility": route,
            "previous_prediction": self.grant["prediction"],
            "forecast_expired": True,
            "available_actions": {"detour": DETOUR_PARAMETERS, "operator_review": {}},
            "mission_contract": self.contract,
        }
        initial = assurance(
            "failed_wait", evidence, mission_id=self.mission_id, contract=self.contract
        )
        self.records.append(
            {"phase": "failed_wait_assurance", "result": initial, "evidence": evidence}
        )
        self.persist()
        if (
            initial["judgment_status"] != "proposal_guardrail_passed"
            or initial["proposed_response_kind"] != "hold"
        ):
            return {"status": "blocked", "reason": "failed_wait_intervention_not_supported"}
        proposal = {}

        def recovery_runner(**_):
            nonlocal proposal
            output, invocation = infer("recovery", "failed_wait_detour", evidence)
            if (
                set(output) != {"action", "observation_ref", "rationale", "uncertainty"}
                or any(not isinstance(v, str) or not v.strip() for v in output.values())
                or output["observation_ref"] != evidence["observation_ref"]
            ):
                raise ValueError("detour_observation_binding_required")
            proposal = {"output": output, "invocation": invocation}
            self.records.append({"phase": "detour_recovery", "result": proposal})
            self.persist()
            validate_route_explanation(output)
            if output["action"] != "detour":
                return {
                    "runtime_status": "guardrail_blocked",
                    "blocking_reasons": ["detour_not_selected"],
                }
            stationary = self.live()["robot_stationary"] is True
            return {
                "runtime_status": "proposal_guardrail_passed",
                "assessment": {
                    "selected_bounded_action": "reroute",
                    "proposed_parameters": DETOUR_PARAMETERS,
                    "compiled_candidate": {
                        "observation_ref": evidence["observation_ref"],
                        "nav2_plan_ref": route["plan_ref"],
                    },
                    "action_feasibility": {
                        "action": "reroute",
                        "feasibility_status": "verified_feasible" if stationary else "unverified",
                        "source": "Actual Nav2 route computed before proposal; fresh replanning required after Rules before dispatch; no collision-free guarantee",
                        "nav2_plan_ref": route["plan_ref"],
                    },
                },
                "agent_invocations": [invocation],
            }

        context = {
            "task_id": self.mission_id,
            "mission_phase": "observed_wait_failure",
            "execution_scope": "simulator",
            "mission_contract": self.contract,
            "observations": evidence,
            "constraints": {"assurance_policy": self.store.context(self.detour_policy.sha256)},
            "allowed_response_kinds": ["replan", "hold", "operator_escalation"],
        }
        graph = run_missionos_mission_incident_graph(
            telemetry_snapshot=evidence,
            mission_context=context,
            recovery_policy=self.detour_policy.model_dump(),
            recovery_runner=recovery_runner,
            mission_assurance_agent=MissionAssuranceAgent(
                PredictionAssuranceJudge("detour_alignment", evidence)
            ),
            mission_assurance_timeout_seconds=45,
        )
        self.records.append({"phase": "detour_assurance_graph", "result": graph})
        current = self.live()
        facts = {
            "mission_id": self.mission_id,
            "execution_scope": "simulator",
            "mission_contract_sha256": digest(self.contract),
            "observed_at": current["observed_at"],
            "source_ref": current["image_sha256"],
            "predicates": {
                "robot_stationary": current["robot_stationary"],
                "actual_wait_failed": True,
                "mission_contract_unchanged": True,
            },
        }
        request = {
            "task_id": self.mission_id,
            "proposal_id": "detour-" + evidence["observation_ref"],
            "recovery_action": "reroute",
            "recovery_parameters": DETOUR_PARAMETERS,
        }
        grant = self.store.check(
            self.detour_policy.sha256, graph=graph, request=request, facts=facts, consume=True
        )
        self.records.append(
            {
                "phase": "detour_rules",
                "result": grant,
                "facts": facts,
                "observation_ref": evidence["observation_ref"],
            }
        )
        self.persist()
        if not grant["policy_authorized"]:
            return {"status": "blocked", "reason": "detour_policy_denied", "authority": grant}
        self.detour_consumed = self.resume_consumed = True
        return {
            "status": "authorized_detour",
            "route_safety": "Route planned; collision freedom is not guaranteed.",
            "authority": grant,
            "recovery_proposal": proposal,
            "observation_ref": evidence["observation_ref"],
            "waypoints": [
                [DETOUR_PARAMETERS["via_x_m"], DETOUR_PARAMETERS["via_y_m"]],
                [DETOUR_PARAMETERS["goal_x_m"], DETOUR_PARAMETERS["goal_y_m"]],
            ],
            "dispatch_before_sim_s": current["sim_s"] + 2.0,
            "graph_id": graph["mission_incident_graph_id"],
        }
