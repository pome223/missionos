"""Host-only sensor judgment; exact public evidence, no private plant input.

The numerical worker asks for observations or proposes a model update. This
adapter reuses the strict final native decoder without weakening earlier
retained-zero adapters. Its saved-session checker sends no model request.
"""
from copy import deepcopy
import math
import re
import time

from .starship_final_supervision_agent import FinalSupervisionAgent, public_input, DELEGATE, DEEPSEEK_MODEL
from .space_ops_investigator import JEV_MODEL
from src.runtime.starship_final_protocol import request as rpc_request, digest, require
from src.runtime.starship_final_sensor_execution import validate_public_request, validate_judgment, JUDGMENT_SCHEMA

SCHEMA = "missionos.starship_final_sensor_agent.v1"


def project(request):
    request = validate_public_request(request)
    return rpc_request(request["request_id"], "sensor", request["issued_at_s"], request["deadline_s"], request)


def verify_saved_session(session, request, response, mode):
    """Check raw host facts and exact response bytes; hashes are not signatures."""
    expected = project(request)
    require(type(session) is dict and set(session) == {"request", "rpc_request", "assessments", "result"}
        and session["request"] == request and session["rpc_request"] == expected
        and session["result"] == response and mode in ("fixture", "live"), "final_sensor_saved_session_binding")
    calls = session["assessments"]
    require(type(calls) is list and 1 <= len(calls) <= 2, "final_sensor_saved_session_calls")
    valid_status = "valid" if mode == "live" else "fixture_valid"
    for index, call in enumerate(calls):
        provider = "typesafe" if index == 0 else "deepseek"
        public = public_input(expected, provider)
        receipt = call.get("provider_receipt") if type(call) is dict else None
        require(type(call) is dict and call.get("request_sha256") == digest(expected)
            and call.get("request_id") == request["request_id"] and call.get("domain") == "sensor"
            and call.get("provider") == provider and call.get("mode") == mode
            and call.get("execution_permission_granted") is False and type(receipt) is dict
            and receipt.get("request_input_sha256") == digest(public)
            and receipt.get("original_request_sha256") == digest(expected), "final_sensor_saved_provider_binding")
        if index:
            require(calls[0]["status"] == valid_status and calls[0]["action"] == DELEGATE
                and receipt["original_deadline_monotonic"] == calls[0]["provider_receipt"]["original_deadline_monotonic"],
                "final_sensor_saved_explicit_delegation")
        if call.get("status") == valid_status:
            require(call.get("action") in public["allowed_actions"]
                and receipt.get("validation_error") is None
                and all(receipt.get(key) is False for key in ("approval_granted", "dispatch", "execution_authorized",
                    "physical_execution", "raw_prompt_recorded", "raw_response_recorded", "raw_reasoning_recorded"))
                and call.get("free_prose_verified") is False
                and call.get("internal_reasoning_verified") is False, "final_sensor_saved_accepted_receipt")
            times = [receipt.get(key) for key in ("decision_started_wall_s", "decision_finished_wall_s",
                "decision_deadline_monotonic", "original_deadline_monotonic")]
            require(all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in times)
                and times[0] <= times[1] < times[2] <= times[3], "final_sensor_saved_original_epoch")
            if mode == "live":
                model = JEV_MODEL if provider == "typesafe" else DEEPSEEK_MODEL
                require(receipt.get("status") == "succeeded"
                    and receipt.get("requested_model_id") == receipt.get("model_id") == model
                    and all(receipt.get(k) is True for k in ("call_attempted", "response_received",
                        "complete_response_observed", "call_succeeded", "model_inference_invoked"))
                    and receipt.get("schema_version") == "runtime_invocation_evidence.v1"
                    and receipt.get("invocation_kind") == "decision_api" and receipt.get("mode") == "live"
                    and receipt.get("provider") == provider
                    and receipt.get("model_inference_status") == "confirmed_by_valid_response"
                    and all(type(receipt.get(key)) is str and re.fullmatch(r"[a-f0-9]{64}", receipt[key])
                        for key in ("request_sha256", "response_sha256"))
                    and all(type(receipt.get(key)) is int and receipt[key] > 0
                        for key in ("request_bytes", "response_bytes_read"))
                    and receipt.get("retries") == receipt.get("redirects") == 0
                    and call.get("model_inference_invoked") is True, "final_sensor_saved_native_receipt")
            else:
                require(receipt.get("status") == "fixture_succeeded" and receipt.get("local_fixture_attempted") is True
                    and receipt.get("model_inference_invoked") is False
                    and call.get("model_inference_invoked") is False, "final_sensor_saved_fixture_receipt")
            if provider == "typesafe":
                probabilities = receipt.get("native_choice_probabilities")
                require(type(probabilities) is dict and set(probabilities) == set(public["allowed_actions"])
                    and all(type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1 for x in probabilities.values())
                    and math.isclose(sum(probabilities.values()), 1., abs_tol=.001)
                    and type(receipt.get("native_choice_confidence")) in (int, float)
                    and math.isfinite(receipt["native_choice_confidence"])
                    and 0 <= receipt["native_choice_confidence"] <= 1, "final_sensor_saved_distribution")
            refs = call.get("references")
            require(type(refs) is list and (provider != "deepseek" or 1 <= len(refs) <= 16)
                and all(type(ref) is dict and set(ref) == {"reference_id", "value"}
                and ref["reference_id"] in public["evidence"]
                and ref["value"] == public["evidence"][ref["reference_id"]] for ref in refs)
                and len({ref["reference_id"] for ref in refs}) == len(refs),
                "final_sensor_saved_numeric_references")
    if response.get("schema") == JUDGMENT_SCHEMA:
        validate_judgment(response, request, mode)
        final = calls[-1]
        require(final["status"] == valid_status and final["action"] != DELEGATE
            and final["action"] == response["action"]
            and response["provenance"] == {"model_inference_invoked": mode == "live",
                "host_assessments_sha256": digest(calls), "explicit_delegation": len(calls) == 2},
            "final_sensor_saved_selected_action")
        return {"valid_judgment": True, "native_judgments": len(calls) if mode == "live" else 0,
            "fixture_judgments": len(calls) if mode == "fixture" else 0,
            "explicit_delegation": len(calls) == 2, "free_prose_verified": False}
    require(response == {"status": "unavailable", "action": None}, "final_sensor_saved_unavailable_response")
    return {"valid_judgment": False, "native_judgments": 0, "fixture_judgments": 0,
        "explicit_delegation": len(calls) == 2, "free_prose_verified": False}


class FinalSensorAgent:
    def __init__(self, *, mode, transport=None, live_authorized=lambda _r, _p: False,
                 active=lambda: True, clock=time.monotonic):
        self.agent = FinalSupervisionAgent(mode=mode, transport=transport,
            live_authorized=live_authorized, active=active, clock=clock)
        self.mode, self.clock, self.sessions = mode, clock, []

    def assess(self, request, *, remaining_s, outer_deadline_monotonic):
        checked = validate_public_request(request)
        rpc = project(checked)
        row = {"request": checked, "rpc_request": rpc, "assessments": [], "result": None}
        self.sessions.append(row)  # Preserve even a failed attempt; no retry.
        call = self.agent.assess(rpc, provider="typesafe", remaining_s=remaining_s,
            outer_deadline_monotonic=outer_deadline_monotonic)
        row["assessments"].append(call)
        valid = "valid" if self.mode == "live" else "fixture_valid"
        if call["status"] == valid and call["action"] == DELEGATE:
            call = self.agent.assess(rpc, provider="deepseek",
                remaining_s=max(0., outer_deadline_monotonic-self.clock()),
                outer_deadline_monotonic=outer_deadline_monotonic)
            row["assessments"].append(call)
        if call["status"] == valid and call["action"] != DELEGATE:
            response = {"schema": JUDGMENT_SCHEMA, "request_sha256": digest(checked), "action": call["action"],
                "mode": self.mode, "provenance": {"model_inference_invoked": self.mode == "live",
                    "host_assessments_sha256": digest(row["assessments"]),
                    "explicit_delegation": len(row["assessments"]) == 2}}
            response["decision_id"] = digest(response)
        else:
            response = {"status": "unavailable", "action": None}
        row["result"] = response
        verify_saved_session(row, checked, response, self.mode)
        return deepcopy(response)

    def record(self):
        return {"schema": SCHEMA, "mode": self.mode, "sessions": deepcopy(self.sessions),
            "automatic_retry": False, "maximum_instance_calls": None, "execution_permission_granted": False}
