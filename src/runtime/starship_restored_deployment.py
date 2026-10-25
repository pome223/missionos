"""One judged request per actual release slot, with measured coast pacing.

This callable fits RestoredPrefixRuntime's existing sanitized callback. It
cannot clear a release interlock, change inventory, grant authority or replace
plant state. An unavailable judgment uses the separately registered sticky
withhold action; valid choices are never overwritten with a preferred outcome.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
import time

from .starship_restored_decision_pipe import RestoredDecisionClient
from .starship_restored_prefix import PENDING_DEPLOYMENT_DECISION
from .starship_restored_wait import RestoredPendingPacer, RestoredWaitEpoch
from .starship_restored_intent import OperationalIntentNotice, REQUEST_SCHEMA_V2, numeric_projection


class RestoredDeploymentJudgments:
    def __init__(self, client, *, scope_sha256, notice=lambda _summary: "",
                 maximum_existing_step_s=2., clock=time.monotonic, sleep=time.sleep):
        if (type(client) is not RestoredDecisionClient or type(scope_sha256) is not str
                or re.fullmatch(r"[a-f0-9]{64}", scope_sha256) is None
                or not callable(notice) or not callable(clock) or not callable(sleep)):
            raise ValueError("restored_deployment_requires_worker_decision_pipe")
        self.client, self.scope, self.notice = client, scope_sha256, notice
        RestoredWaitEpoch(0., 0., 35., 35., maximum_existing_step_s)
        self.maximum_step, self.clock, self.sleep = maximum_existing_step_s, clock, sleep
        self.pending, self.seen, self.completed = None, set(), []

    def __call__(self, summary):
        slot, now = summary["released_count"], summary["time_s"]
        if type(slot) is not int or slot not in (0, 13) or summary["retained_count"] != 26-slot:
            raise ValueError("restored_deployment_actual_inventory_slot_mismatch")
        if self.pending is None:
            if slot in self.seen or slot == 13 and 0 not in self.seen:
                raise ValueError("restored_deployment_duplicate_or_skipped_judgment")
            notice = self.notice(summary)
            intent = None
            if type(notice) is OperationalIntentNotice:
                intent = notice.observe(source_sha256=self.client.binding["source_sha256"], scope_sha256=self.scope)
                text = notice.notice_summary
            else:
                if type(notice) is not str:
                    raise ValueError("restored_deployment_notice_or_typed_intent_required")
                text = notice
            request = {"schema": "missionos.starship_restored_decision_request.v1",
                "request_id": f"restored-deployment-{slot}", "phase": "deployment",
                "scope_sha256": self.scope, "time_s": now, "deadline_s": now+35.,
                "facts": {"time_s": now, "released_count": slot, "retained_count": 26-slot,
                    "release_ready": summary["release_ready"], "sequence_closed": False},
                "notice_summary": text,
                "allowed_actions": ["continue_deployment", "withhold_deployment"]}
            if intent is not None:
                request.update(schema=REQUEST_SCHEMA_V2, operational_intent=intent)
                request["facts"].update(numeric_projection(intent))
            frame = self.client.begin(request)
            epoch = RestoredWaitEpoch(now, frame["started_wall_s"], now+35.,
                frame["wall_deadline_s"], self.maximum_step)
            self.pending = {"slot": slot, "request": request,
                "pacer": RestoredPendingPacer(epoch, clock=self.clock, sleep=self.sleep), "exchange": None}
            self.seen.add(slot)
            return PENDING_DEPLOYMENT_DECISION
        pending = self.pending
        if pending["slot"] != slot or summary["decision_started_s"] != pending["request"]["time_s"]:
            raise ValueError("restored_deployment_pending_request_rebound")

        def pump():
            if pending["exchange"] is None:
                pending["exchange"] = self.client.poll(now)

        paced = pending["pacer"].at_actual_tick(now, pump=pump)
        exchange = pending["exchange"]
        if exchange is None and not paced["pending_deadline_expired"]:
            return PENDING_DEPLOYMENT_DECISION
        valid = (not paced["pending_deadline_expired"] and exchange is not None
            and exchange["status"] == "valid"
            and exchange["action"] in ("continue_deployment", "withhold_deployment"))
        action = exchange["action"] if valid else "withhold_deployment"
        raw = json.dumps(pending["request"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.completed.append({"actual_slot": slot, "request": deepcopy(pending["request"]),
            "request_sha256": sha256(raw).hexdigest(), "exchange": deepcopy(exchange),
            "applied_action": action, "applied_at_actual_time_s": now,
            "fallback_used": not valid, "pacing": pending["pacer"].record(),
            "model_inference_inferred_from_choice": False, "release_or_authority_granted": False})
        self.pending = None
        return action

    def record(self):
        return {"schema": "missionos.starship_restored_deployment_judgments.v1",
            "decisions": deepcopy(self.completed),
            "pending_request": None if self.pending is None else deepcopy(self.pending["request"]),
            "closed_inventory_inferred": False, "physical_release_inferred": False,
            "native_model_calls_inferred": False}

    def finalize_prefix(self, prefix):
        """Drain the existing request after the plant's own sticky timeout.

        The prefix intentionally stops calling this adapter at its deadline.
        This finalizer does not choose again or send anything: it records the
        already-applied withhold and frees the judgment channel for return.
        Other abnormal prefix terminations stop the outer cohort instead.
        """
        if self.pending is None:
            return self.record()
        pending = self.pending
        evidence = prefix["restored_prefix"]
        decision = evidence["decisions"][-1]
        now = prefix["final_state"]["time_s"]
        if (evidence["closed"] is not True or decision["action"] != "withhold_deployment"
                or decision["callback_error_type"] != "SimulationDecisionDeadline"
                or decision["summary"]["released_count"] != pending["slot"]
                or decision["pending"]["started_s"] != pending["request"]["time_s"]
                or now < pending["request"]["deadline_s"]):
            raise ValueError("restored_deployment_pending_prefix_not_a_terminal_timeout")
        exchange = self.client.poll(now)
        if exchange is None or exchange["status"] != "no_valid_judgment":
            raise ValueError("restored_deployment_timeout_was_not_closed")
        self.completed.append({"actual_slot": pending["slot"], "request": deepcopy(pending["request"]),
            "request_sha256": sha256(json.dumps(pending["request"], sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "exchange": deepcopy(exchange), "applied_action": "withhold_deployment",
            "applied_at_actual_time_s": now, "fallback_used": True,
            "pacing": pending["pacer"].record(), "prefix_timeout_already_applied": True,
            "model_inference_inferred_from_choice": False, "release_or_authority_granted": False})
        self.pending = None
        return self.record()
