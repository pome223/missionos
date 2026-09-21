"""Recompute the public governed ACWM chain; no private simulator or weights needed."""

from __future__ import annotations
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def verify(root=ROOT, *, manifest=True):
    if manifest:
        files = json.loads((root / "manifest.json").read_text())
        assert files, "empty manifest"
        for name, sha in files.items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha, name
    data = json.loads((root / "run.json").read_text())
    games = data["games"]
    assert [g["seed"] for g in games] == [68000, 68001]
    totals = dict(
        seeds=[68000, 68001],
        scores=[],
        llm_judgments=0,
        motor_steps=0,
        vla_inference_chunks=0,
        authority_rejection_probes=0,
        completed_run_technical_failures=0,
    )
    legacy_flag_count = 0
    ids, tickets, generations, responses = set(), set(), set(), set()
    for game in games:
        steps = game["steps"]
        assert steps and [s["step"] for s in steps] == list(range(1, len(steps) + 1))
        assert game["mission"]["bounded_preapproval_recorded"] is True
        for s in steps:
            d, dispatch, result, receipt = (
                s[k] for k in ("decision", "dispatch", "outcome", "receipt")
            )
            ident = d["request_id"]
            assert ident not in ids
            ids.add(ident)
            assert ident == f'game-{game["seed"]}-step-{s["step"]}'
            forecast = d["forecast"]
            assert forecast["verification_basis"] == "model_inferred"
            assert (
                not forecast["dispatch_authority_created"] and not d["dispatch_authority_created"]
            )
            assert len(forecast["forecasts"]) == 1
            f = forecast["forecasts"][0]
            assert f["option_id"] == "continue" and f["horizon_seconds"] == 14.2
            g = s["generation"]
            assert g["invocation_id"] == f["future_state"]["generation_invocation_id"]
            assert g["invocation_id"] not in generations
            generations.add(g["invocation_id"])
            assert g["future_action_tape_access"] is False
            assert g["readout_output"] == f["risk_score"] and 0 <= f["risk_score"] <= 1
            assert g["model_sha256"] == forecast["binding"]["model_sha256"]
            assert g["readout_sha256"] == f["future_state"]["readout_sha256"]
            assert s["generation_shape"] == [37, 240, 240, 3]
            assert s["input_field_names"] == sorted(
                ["image", "objects", "velocity", "physics", "robot", "count", "plan"]
            )
            assert s["dispatch_observation_identical"] is True
            assert d["admission"]["status"] == s["admission"]["receipt"]["status"] == "adopted"
            proposal = d["proposal"]
            # Frozen historical evidence: preserve and explicitly audit the stale
            # top-level flag rather than silently treating it as current truth.
            assert d["llm_invoked"] is False, "unexpected change to known legacy llm_invoked field"
            assert proposal["model_inference_invoked"] is True, "actual model invocation required"
            legacy_flag_count += 1
            ev = proposal["model_invocation_evidence"]
            assert ev["invocation_kind"] == "llm_api" and ev["provider"] == "deepseek"
            llm = json.loads((root / ev["record_ref"]).read_text())
            assert digest(llm["prompt"]) == ev["prompt_sha256"]
            assert digest(llm["response"]) == ev["response_sha256"]
            assert digest(llm["request_payload"]) == ev["request_payload_sha256"]
            assert json.loads(llm["request_payload"]["messages"][1]["content"]) == llm["prompt"]
            assert llm["response"]["id"] == ev["response_id"]
            assert ev["response_id"] not in responses
            responses.add(ev["response_id"])
            situation = llm["prompt"]["mission_situation"]
            evidence = situation["uncertainty"]["prediction_evidence"]
            assert evidence["forecasts"] == forecast["forecasts"]
            scope = situation["constraints"]["stacking_score_comparison"]
            assert scope["bank_risk"] is None and scope["bank_forecast_available"] is False
            assert scope["continue_then_bank_proxy_points"] is None
            answer = json.loads(llm["response"]["choices"][0]["message"]["content"])
            assert answer["rationale"] == proposal["rationale"]
            assert {"continue": "continue", "hold": "bank"}[answer["proposed_response_kind"]] == d[
                "selected_option"
            ]
            assert dispatch["decision_sha256"] == digest(d)
            assert (
                dispatch["policy_sha256"]
                == d["approval_policy_sha256"]
                == game["mission"]["policy_sha256"]
            )
            assert dispatch["dispatch_authority_created"] and not dispatch["blocking_reasons"]
            assert (
                dispatch["ticket"] == receipt["dispatch_ticket"]
                and dispatch["ticket"] not in tickets
            )
            tickets.add(dispatch["ticket"])
            assert receipt["observed_result_sha256"] == digest(result)
            assert receipt["verification_basis"] == "simulator_measured"
            assert result["horizon_steps"] == receipt["observed_horizon_steps"] == 284
            assert result["collapsed"] == (max(result["per_object_drop"]) > 0.03)
            count = s["step"] - (d["selected_option"] == "bank")
            assert result["count_after"] == count
            assert (
                result["score"]
                == (0 if result["collapsed"] else count)
                == receipt["observed_score"]
            )
            assert result["technical_failure"] is None
            assert dispatch["option"] == d["selected_option"]
            assert receipt["status"] == (
                "incomparable" if d["selected_option"] == "bank" else "compared"
            )
            runtime = receipt["runtime_invocation"]
            assert runtime["motor_steps"] == 284
            assert runtime["actions_sha256"] == s["actions_sha256"]
            assert (
                g["prediction_finished_at"] <= datetime.fromisoformat(ev["started_at"]).timestamp()
            )
            assert (
                datetime.fromisoformat(ev["completed_at"]).timestamp()
                <= runtime["started_at"]
                <= runtime["completed_at"]
            )
            assert [v["invocation_id"] for v in s["vla_inferences"]] == result["vla_inference_ids"]
            for v in s["vla_inferences"]:
                assert v["model_sha256"] == forecast["binding"]["policy_sha256"]
                assert v["bridge"]["actual_remote_inference"] is True
                assert runtime["started_at"] <= v["bridge"]["request_started_at"]
                assert v["bridge"]["request_started_at"] <= v["started_at"] <= v["finished_at"]
                assert (
                    v["finished_at"]
                    <= v["bridge"]["response_received_at"]
                    <= runtime["completed_at"]
                )
            totals["llm_judgments"] += 1
            totals["motor_steps"] += runtime["motor_steps"]
            totals["vla_inference_chunks"] += len(result["vla_inference_ids"])
        assert steps[-1]["decision"]["selected_option"] == "bank"
        assert steps[-1]["receipt"]["mission_complete"] is True
        assert game["score"] == steps[-1]["outcome"]["score"]
        totals["scores"].append(game["score"])
        probes = game["rejection_probes"]
        assert {p["mutation"] for p in probes} == {
            "approval_policy_sha256",
            "state_revision",
            "observed_at",
        }
        assert all(p["status"] == 400 and p["motor_calls"] == 0 for p in probes)
        totals["authority_rejection_probes"] += len(probes)
    assert totals == data["summary"]
    assert totals["scores"] == [9, 4] and totals["llm_judgments"] == 15
    assert len(data["prefix_comparison_with_pr110"]) == 15
    assert all(
        p["state_max_abs_difference"] == p["motor_action_max_abs_difference"] == 0
        for p in data["prefix_comparison_with_pr110"]
    )
    assert legacy_flag_count == totals["llm_judgments"] == 15
    return totals | {
        "known_legacy_inconsistencies": {
            "decision.llm_invoked": {
                "recorded_value": False,
                "actual_invocation_field": "proposal.model_inference_invoked",
                "actual_invocation_value": True,
                "affected_decisions": legacy_flag_count,
                "scope": "preserved historical run only; current runtime field corrected",
            }
        }
    }


if __name__ == "__main__":
    print("PASS", json.dumps(verify(), sort_keys=True))
