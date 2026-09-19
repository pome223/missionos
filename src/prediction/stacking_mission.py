"""Opt-in stacking mission composition: real Assurance, bounded Rules and measured IO.

This is a simulator adapter, not a Gateway or a hardware dispatch endpoint.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen
import uuid

from missionos_core.prediction import (
    PredictionBinding,
    PredictionOption,
    PredictionRequest,
    prediction_digest,
)
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
    MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA,
)
from src.intelligence.mission_assurance_policy import (
    AssurancePolicy,
    PolicyStore,
    digest,
    timestamp,
)
from src.intelligence.prediction_evidence import (
    capture_prediction_evidence,
    receive_prediction_evidence,
)
from src.prediction.service import PredictionSession


def utc():
    return datetime.now(timezone.utc).isoformat()


def stacking_score_comparison(forecast: dict, next_count: int) -> dict:
    """Arithmetic evidence under an explicit proxy assumption, never an action selector.

    Frozen ExtraTrees uses balanced class weights. Its predict_proba output is
    not a calibrated physical frequency; the following is not measured utility.
    """
    semantics = {
        "schema_version": "stacking_score_comparison.v1",
        "risk_meaning": "Uncalibrated ExtraTrees positive-class score; training uses balanced class weights. It is not an established physical collapse probability.",
        "positive_class": "Any score-bearing block drops more than 0.03 m within the option's rollout horizon.",
        "objective": "Maximize banked block count with linear point utility; collapse yields zero. No additional loss-aversion penalty is specified.",
        "assumption": "For this provisional arithmetic comparison ONLY, substitute risk_score for failure probability. This is not calibration and not a guaranteed expected score.",
        "calibrated": False,
        "recommended_option": None,
        "dispatch_authority_created": False,
        "regression_note": "Per-object drops and poses are a separate regression head describing the same rollout. They are not independent probabilities or proof of certain collapse; do not double-count the same hazard.",
    }
    if forecast.get("status") != "available":
        return semantics | {"status": "unavailable"}
    options = {f["option_id"]: f for f in forecast["forecasts"]}
    if (
        set(options) != {"continue", "bank"}
        or type(next_count) is not int
        or not 1 <= next_count <= 10
    ):
        raise ValueError("unsupported_score_comparison")
    if options["continue"]["horizon_seconds"] != 28.4 or options["bank"]["horizon_seconds"] != 14.2:
        raise ValueError("score_comparison_requires_placement_plus_terminal_hold")
    current = next_count - 1
    continued = next_count * (1 - options["continue"]["risk_score"])
    banked = current * (1 - options["bank"]["risk_score"])
    return semantics | {
        "status": "illustrative_proxy_only",
        "current_count": current,
        "next_count": next_count,
        "formula": "bank=n*(1-bank_risk); continue_then_bank=(n+1)*(1-continue_risk)",
        "bank_proxy_points": banked,
        "continue_then_bank_proxy_points": continued,
        "continue_minus_bank_proxy_points": continued - banked,
        "break_even_continue_risk_under_proxy": 1 - banked / next_count,
        "continue_then_bank_horizon_seconds": 28.4,
        "bank_horizon_seconds": 14.2,
        "planning_limit": "One placement followed by terminal hold, not optimal planning through ten blocks; choosing continue here does not precommit later actions.",
    }


class LocalOllamaJudge:
    """Actual local model IO, with raw prompt/response and invocation provenance."""

    def __init__(self, model: str, output: Path):
        self.model, self.output = model, output
        output.mkdir()
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=10) as response:
            tags = json.load(response)["models"]
        self.model_digest = next(m["digest"] for m in tags if m["name"] == model)

    def judge(self, prompt):
        ident = uuid.uuid4().hex
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA,
            "options": {"temperature": 0, "seed": 104, "num_predict": 512, "num_ctx": 8192},
            "messages": [
                {
                    "role": "system",
                    "content": "You are the Mission Assurance judge. Read the mission contract and admitted model-inferred evidence. Choose exactly one allowed response. Return the required JSON only, with empty parameters. Do not claim approval, feasibility, or execution. Be concise. Risk is fallible evidence; choose to maximize banked score. Read constraints.stacking_score_comparison: risk is a class-reweighted score, not a calibrated probability. Cite both supplied proxy point values in your rationale, and call them proxy values rather than true expected points. The collapse-to-zero loss is already included in this arithmetic; do not add an unspecified loss-aversion penalty. For these bounded simulator games the objective is linear point utility. If you choose against the proxy comparison, identify concrete additional evidence and explain why it changes the comparison; unknown calibration alone does not establish that bank maximizes expected points. Do not infer safety from count alone.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
        }
        started = utc()
        with urlopen(
            Request(
                "http://127.0.0.1:11434/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=240,
        ) as response:
            raw = json.load(response)
        record = {
            "prompt": prompt,
            "response": raw,
            "started_at": started,
            "completed_at": utc(),
            "model_digest": self.model_digest,
        }
        (self.output / f"{ident}.json").write_text(json.dumps(record, indent=2))
        if raw.get("done") is not True or raw.get("model") != self.model:
            raise ValueError("incomplete_or_wrong_model_response")
        return ModelJudgment(
            output=json.loads(raw["message"]["content"]),
            invocation_evidence={
                "invocation_kind": "http_loopback",
                "model_id": self.model,
                "model_sha256": self.model_digest,
                "record_ref": f"llm/{ident}.json",
                "prompt_sha256": prediction_digest(prompt),
                "response_sha256": prediction_digest(raw),
                "started_at": started,
                "completed_at": record["completed_at"],
                "eval_count": raw.get("eval_count"),
            },
        )


class DeepSeekJudge:
    """Actual DeepSeek IO. Credentials remain host-side and are never persisted."""

    def __init__(self, model: str, output: Path):
        self.model, self.output = model, output
        self.model_digest = None  # Remote weights are not accessible; do not invent a digest.
        self.key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not self.key:
            raise ValueError("DEEPSEEK_API_KEY_required_in_host_process")
        output.mkdir()

    def judge(self, prompt):
        ident = uuid.uuid4().hex
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 1024,
            "thinking": {"type": "disabled"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are the Mission Assurance judge. Read the mission contract and admitted model-inferred evidence. Choose exactly one allowed response. Return one JSON object only with these six keys: proposed_response_kind, parameters, rationale, expected_outcome, uncertainty, operator_question. parameters must be an empty object; all other fields must be nonempty strings. Do not claim approval, feasibility, or execution. Risk is fallible evidence; choose to maximize banked score. Read constraints.stacking_score_comparison: risk is a class-reweighted score, not a calibrated probability. Cite both supplied proxy point values in your rationale, and call them proxy values rather than true expected points. The collapse-to-zero loss is already included in this arithmetic; do not add an unspecified loss-aversion penalty. For these bounded simulator games the objective is linear point utility. If you choose against the proxy comparison, identify concrete additional evidence and explain why it changes the comparison; unknown calibration alone does not establish that bank maximizes expected points. Do not infer safety from count alone.",
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
        }
        started = utc()
        with urlopen(
            Request(
                "https://api.deepseek.com/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
            ),
            timeout=240,
        ) as response:
            raw = json.load(response)
        record = {
            "prompt": prompt,
            "response": raw,
            "started_at": started,
            "completed_at": utc(),
            "requested_model": self.model,
            "model_digest": None,
        }
        (self.output / f"{ident}.json").write_text(json.dumps(record, indent=2))
        choice = raw["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ValueError("incomplete_deepseek_response")
        return ModelJudgment(
            output=json.loads(choice["message"]["content"]),
            invocation_evidence={
                "invocation_kind": "llm_api",
                "provider": "deepseek",
                "model_id": raw["model"],
                "requested_model_id": self.model,
                "response_id": raw["id"],
                "usage": raw.get("usage", {}),
                "model_sha256": None,
                "record_ref": f"llm/{ident}.json",
                "prompt_sha256": prediction_digest(prompt),
                "response_sha256": prediction_digest(raw),
                "started_at": started,
                "completed_at": record["completed_at"],
            },
        )


class GovernedStackingSession(PredictionSession):
    def __init__(self, provider, output: Path, *, agent, seeds, approve: bool, operator: str):
        super().__init__(provider, output, allow_simulator_decisions=True)
        self.agent = agent
        self.store = PolicyStore(output / "policy.sqlite")
        self.pending = {}
        self.revisions = {int(seed): 1 for seed in seeds}
        self.finished = set()
        self.contracts, self.policies = {}, {}
        for seed in self.revisions:
            contract = {
                "mission_id": f"stacking-{seed}",
                "seed": seed,
                "max_blocks": 10,
                "collapse_score": 0,
                "terminal_hold_seconds": 14.2,
                "binding": asdict(provider.binding),
                "execution_scope": "simulator",
                "macro": "stacking.fixed_vla_placement.v1",
                "maximum_steps_per_option": 568,
            }
            policy = AssurancePolicy.model_validate(
                {
                    "version": 1,
                    "policy_id": f"stacking-e2e-{seed}",
                    "mission_id": contract["mission_id"],
                    "mission_contract_sha256": digest(contract),
                    "execution_scope": "simulator",
                    "mode": "bounded",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                    "max_observation_age_seconds": 300,
                    "max_total_actions": 10,
                    "preserve": ["same_observation", "registered_macro", "simulation_only"],
                    "actions": {
                        k: {"parameters": {}, "max_uses": 10} for k in ("continue", "bank")
                    },
                    "on_unresolved": "request_human",
                }
            )
            if approve:
                self.store.approve(policy, operator=operator, expected_sha256=policy.sha256)
            self.contracts[seed], self.policies[seed] = contract, policy
            self.save(
                f"mission-{seed}",
                {
                    "contract": contract,
                    "policy": policy.model_dump(),
                    "policy_sha256": policy.sha256,
                    "bounded_preapproval_recorded": approve,
                    "authorization_source": operator if approve else None,
                    "individual_action_human_approval": False,
                },
            )

    def save(self, name, data):
        (self.output / f"{name}.json").write_text(json.dumps(data, indent=2, allow_nan=False))

    def decide(self, body):
        seed, revision = int(body["seed"]), int(body["state_revision"])
        if seed not in self.revisions or seed in self.finished or revision != self.revisions[seed]:
            raise ValueError("mission_revision_mismatch")
        if any(p["seed"] == seed for p in self.pending.values()):
            raise ValueError("previous_step_not_verified")
        if int(body["state"]["count"]) != revision:
            raise ValueError("count_revision_mismatch")
        # Retain the previous lab policy only as an audit reference. It is not dispatched or shown to the LLM.
        decision = super().decide(body)
        reference = decision["selected_option"]
        request = PredictionRequest(
            body["request_id"],
            body["observation_id"],
            body["observed_at"],
            PredictionBinding(**body["binding"]),
            body["state"],
            tuple(PredictionOption(**o) for o in body["options"]),
        )
        evidence = capture_prediction_evidence(
            request,
            decision["forecast"],
            execution_id=f"stacking-{seed}",
            state_revision=str(revision),
            source_ref=f"simulator:{seed}",
        )
        policy = self.policies[seed]
        situation = MissionSituation(
            situation_id=body["request_id"],
            observed_at=utc(),
            mission_contract={
                "prediction_contract": self.provider.binding.mission_contract,
                "mission": self.contracts[seed],
                "objective": "Maximize stable banked count, up to ten blocks. Collapse loses all points.",
                "response_mapping": {
                    "continue": "Execute the registered VLA placement macro for the next block.",
                    "hold": "Do not place another block; finish by the registered bank macro, which only holds position for 14.2 seconds. It does not move toward or release a block.",
                    "operator_escalation": "No dispatch; pause this mission for human review.",
                },
            },
            progress={
                "existing_count": revision - 1,
                "next_count": revision,
                "task_id": f"stacking-{seed}",
            },
            observations={
                "source_ref": f"simulator:{seed}",
                "state_sha256": prediction_digest(body["state"]),
            },
            constraints={
                "prediction_context": evidence["context"],
                "assurance_policy": policy.model_dump(),
                "reference_classifier_threshold": self.provider.threshold,
                "stacking_score_comparison": stacking_score_comparison(
                    decision["forecast"], revision
                ),
                "unknown_calibration": True,
                "actions_require_separate_policy_revalidation": True,
            },
            uncertainty={
                "model_limit": "Exact-state mission-specific model, known to miss some collapses and stop some safe placements."
            },
            source_refs=(f"simulator:{seed}",),
            source_schema_version="stacking.exact_state.v1",
            input_digest=request.digest(),
            execution_scope="simulator",
            allowed_response_kinds=("continue", "hold", "operator_escalation"),
        )
        updated, admission = receive_prediction_evidence(
            situation, evidence, now=time.time(), max_age_seconds=300
        )
        self.save(
            body["request_id"] + "-admission",
            {"evidence": evidence, "situation": updated.to_dict(), "receipt": admission},
        )
        if admission["status"] != "adopted":
            raise ValueError("prediction_not_adopted")
        proposal = self.agent.evaluate(updated).to_dict()
        option = {"continue": "continue", "hold": "bank"}.get(proposal["proposed_response_kind"])
        if proposal["judgment_status"] != "proposal_guardrail_passed" or proposal["parameters"]:
            option = None
        decision.update(
            selected_option=option,
            decision_policy="mission_assurance_llm",
            baseline_reference_option=reference,
            proposal=proposal,
            admission=admission,
            approval_policy_sha256=policy.sha256,
            dispatch_authority_created=False,
        )
        self.save(body["request_id"] + "-decision", decision)
        self.requests[body["request_id"]] = decision
        if option is None:
            raise ValueError("assurance_requires_human_no_dispatch")
        self.pending[body["request_id"]] = {
            "seed": seed,
            "revision": revision,
            "body": body,
            "request": request,
            "situation": situation,
            "evidence": evidence,
            "decision": decision,
            "ticket": None,
        }
        return decision

    def dispatch(self, body):
        ident = body["request_id"]
        pending = self.pending[ident]
        decision, seed = pending["decision"], pending["seed"]
        reasons = []
        policy = self.policies[seed]
        try:
            approved = self.store.approved(policy.sha256)
            if approved.mode != "bounded" or timestamp(approved.expires_at) <= datetime.now(
                timezone.utc
            ):
                reasons.append("policy_not_active_bounded")
        except ValueError:
            reasons.append("approved_active_policy_required")
        if body.get("approval_policy_sha256") != policy.sha256:
            reasons.append("approval_binding_mismatch")
        if body.get("decision_sha256") != prediction_digest(decision):
            reasons.append("judgment_binding_mismatch")
        if (
            body.get("state_revision") != self.revisions[seed]
            or body.get("observation_id") != pending["request"].observation_id
        ):
            reasons.append("current_observation_mismatch")
        if body.get("state_sha256") != prediction_digest(pending["body"]["state"]):
            reasons.append("world_state_changed")
        observed = body.get("observed_at")
        if type(observed) not in (int, float) or not 0 <= time.time() - observed <= 300:
            reasons.append("fresh_observation_required")
        _, readmission = receive_prediction_evidence(
            pending["situation"], pending["evidence"], now=time.time(), max_age_seconds=300
        )
        if readmission["status"] != "adopted":
            reasons.append("forecast_no_longer_current")
        if pending["ticket"] is not None:
            reasons.append("already_dispatched")
        option = decision["selected_option"]
        if option not in policy.actions or policy.mission_contract_sha256 != digest(
            self.contracts[seed]
        ):
            reasons.append("unregistered_action_or_contract")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revoked FROM policies WHERE sha=?", (policy.sha256,)
            ).fetchone()
            if not row or row[0]:
                reasons.append("approved_active_policy_required")
            total = db.execute(
                "SELECT COUNT(*) FROM reservations WHERE mission=?", (policy.mission_id,)
            ).fetchone()[0]
            if total >= policy.max_total_actions:
                reasons.append("budget_exhausted")
            if db.execute(
                "SELECT 1 FROM reservations WHERE mission=? AND proposal=?",
                (policy.mission_id, ident),
            ).fetchone():
                reasons.append("already_reserved")
            if not reasons:
                db.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?)",
                    (policy.mission_id, ident, option, policy.sha256, digest(body)),
                )
        receipt = {
            "schema_version": "missionos_stacking_dispatch.v1",
            "request_id": ident,
            "decision_sha256": prediction_digest(decision),
            "policy_sha256": policy.sha256,
            "option": option,
            "blocking_reasons": sorted(set(reasons)),
            "dispatch_authority_created": not reasons,
            "execution_scope": "simulator",
            "physical_execution_invoked": False,
            "executor_invoked": False,
            "maximum_motor_steps": 568
            if pending["revision"] == 10 and option == "continue"
            else 284,
            "individual_action_human_approval": False,
            "authority_source": "bounded_user_preapproval",
        }
        if reasons:
            self.save(ident + "-rejected-" + uuid.uuid4().hex[:8], receipt)
            raise ValueError(",".join(receipt["blocking_reasons"]))
        pending["ticket"] = uuid.uuid4().hex
        receipt["ticket"] = pending["ticket"]
        self.save(ident + "-dispatch", receipt)
        return receipt

    def observe(self, body):
        ident = body["request_id"]
        pending = self.pending[ident]
        if pending["ticket"] is None or body.get("ticket") != pending["ticket"]:
            raise ValueError("matching_dispatch_required")
        terminal = body["role"] == "terminal_hold"
        if terminal and (
            pending["revision"] != 10 or pending["decision"]["selected_option"] != "continue"
        ):
            raise ValueError("terminal_hold_not_authorized")
        if body["role"] not in ("selected_execution", "terminal_hold"):
            raise ValueError("unapproved_counterfactual")
        if terminal and (ident, "selected_execution") not in self.observations:
            raise ValueError("selected_execution_must_be_verified_before_terminal_hold")
        invocation = body["runtime_invocation"]
        expected_steps = 284
        if (
            invocation.get("motor_steps") != expected_steps
            or invocation.get("invocation_kind") != "simulator_motor_loop"
        ):
            raise ValueError("motor_invocation_evidence_required")
        receipt = super().observe(
            body | {"role": "counterfactual_hold" if terminal else "selected_execution"}
        )
        receipt.update(
            role=body["role"],
            runtime_invocation=invocation,
            dispatch_ticket=pending["ticket"],
            effect_observed=True,
            simulator_execution_invoked=True,
            verification_basis="simulator_measured",
            mission_complete=False,
        )
        done = (
            terminal
            or pending["decision"]["selected_option"] == "bank"
            or body["result"]["collapsed"]
        )
        if done:
            self.finished.add(pending["seed"])
            receipt["mission_complete"] = True
        self.save(ident + "-" + body["role"], receipt)
        if terminal or pending["revision"] < 10 or done:
            self.revisions[pending["seed"]] += 1
            del self.pending[ident]
        return receipt


def serve_mission(
    *,
    model,
    model_sha256,
    policy_sha256,
    output,
    llm_model,
    seeds,
    llm_backend="deepseek",
    approve_simulator_mission=False,
    operator="",
    port=0,
):
    import signal
    from src.prediction.service import make_server
    from src.prediction.stacking import StackingPredictor

    if approve_simulator_mission and not operator.strip():
        raise ValueError("operator_authorization_reference_required")
    provider = StackingPredictor(model, model_sha256, policy_sha256)
    if llm_backend not in ("deepseek", "ollama"):
        raise ValueError("unsupported_llm_backend")
    judge_type = DeepSeekJudge if llm_backend == "deepseek" else LocalOllamaJudge
    judge = judge_type(llm_model, output.parent / "llm")
    session = GovernedStackingSession(
        provider,
        output,
        agent=MissionAssuranceAgent(judge),
        seeds=seeds,
        approve=approve_simulator_mission,
        operator=operator,
    )
    server = make_server(session, port=port)
    session.save(
        "ready",
        {
            "port": server.server_port,
            "binding": asdict(provider.binding),
            "llm_model": llm_model,
            "llm_model_sha256": judge.model_digest,
        },
    )
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
