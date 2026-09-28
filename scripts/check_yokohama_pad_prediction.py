#!/usr/bin/env python3
"""CPU wiring smoke: synthetic WAM evidence, fixed VLA step, fixture mission judge."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from missionos_core.prediction import (  # noqa: E402
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
)
from src.intelligence.mission_assurance_agent import MissionSituation, ModelJudgment  # noqa: E402
from src.intelligence.prediction_evidence import capture_prediction_evidence  # noqa: E402
from src.runtime.yokohama_pad_prediction import evaluate  # noqa: E402


class FixtureJudge:
    """Reads the actual prompt; no case name, expected answer or actor schedule."""

    def judge(self, prompt):
        situation = prompt["mission_situation"]
        forecasts = situation["uncertainty"]["prediction_evidence"]["forecasts"]
        by_id = {f["option_id"]: f["future_state"] for f in forecasts}
        current = situation["observations"]
        can_step = (
            current["pad_clear"]
            and current["approach_clear"]
            and not by_id["vla"]["conflict_during_interval"]
            and by_id["vla"]["pad_state_at_horizon"] == "clear"
        )
        kind = "continue" if can_step else "hold"
        candidate = "vla" if can_step else "hold"
        return ModelJudgment(
            output=dict(
                proposed_response_kind=kind,
                parameters={"candidate_id": candidate},
                rationale="Authored fixture reads current occupancy and candidate-conditioned WAM evidence.",
                expected_outcome="Bounded candidate selected for independent approval and Rules.",
                uncertainty="Synthetic semantic forecasts and deterministic fixture judgment; no learned result.",
                operator_question="Review within the existing simulator scope.",
            ),
            invocation_evidence={"invocation_kind": "deterministic_fixture"},
            model_inference_invoked=False,
        )


def fixture(*, predicted_step_conflict=False, current_clear=True):
    binding = PredictionBinding(
        "pad-wam-fixture",
        "a" * 64,
        "pad-wait-or-step.v1",
        "b" * 64,
        "authored-pad-fixture.v1",
        "pad-history.v1",
    )
    vla = dict(delta_body_frd=[2, 0, 0, 0], response_sha256="c" * 64)
    request = PredictionRequest(
        "request-1",
        "observation-1",
        100.0,
        binding,
        {"history_sha256": "d" * 64},
        (
            PredictionOption("hold", 10, {"delta_body_frd": [0, 0, 0, 0]}),
            PredictionOption("vla", 10, vla),
        ),
    )

    class Predictor:
        def predict(self, request):
            return tuple(
                OptionForecast(
                    o.option_id,
                    o.horizon_seconds,
                    0.9 if o.option_id == "vla" and predicted_step_conflict else 0.1,
                    dict(
                        conflict_during_interval=o.option_id == "vla" and predicted_step_conflict,
                        pad_state_at_horizon="occupied" if predicted_step_conflict else "clear",
                        coverage="observation_through_horizon",
                    ),
                )
                for o in request.options
            )

    predictor = Predictor()
    predictor.binding = binding
    registry = PredictionRegistry()
    registry.register(predictor)
    envelope = capture_prediction_evidence(
        request,
        registry.forecast(request, now=100),
        execution_id="fixture-run",
        state_revision="revision-1",
        source_ref="fixture:camera-history",
    )
    situation = MissionSituation(
        "pad-situation-1",
        "2026-09-28T00:00:00Z",
        {"prediction_contract": binding.mission_contract, "objective": "deliver then return"},
        {"region": "city", "phase": "pad_wait"},
        dict(
            observed_sim_s=100.5,
            pad_clear=current_clear,
            approach_clear=True,
            bounded_hold_available=True,
            wait_budget_available=True,
        ),
        dict(
            prediction_context=deepcopy(envelope["context"]),
            vla_candidate=deepcopy(vla),
            candidate_duration_sim_s={"hold": 2, "vla": 3},
            pad_forecast_profile=dict(
                binding=asdict(binding),
                clock="sim",
                source="fixture",
                semantic_validation_ref="fixture:assumed-dynamic-interval-labels",
                time_calibration_ref="fixture:authored-simulator-clock",
            ),
        ),
        {"source": "synthetic CPU fixture; no native model or flight"},
        ("fixture:camera-history",),
        "pad-fixture.v1",
        request.digest(),
        "fixture",
    )
    return situation, envelope


def run_cases():
    records = []
    for name, conflict, clear, expected in (
        ("forecast_conflict", True, True, "hold"),
        ("forecast_clear", False, True, "vla"),
        ("currently_busy_future_clear", False, False, "hold"),
    ):
        situation, envelope = fixture(predicted_step_conflict=conflict, current_clear=clear)
        result = evaluate(situation, envelope, FixtureJudge(), now=101)
        selected = result["selected_candidate"]
        passed = result["selection_valid_for_review"] and selected["option_id"] == expected
        records.append(dict(case=name, passed=passed, envelope=envelope, result=result))
    for name, expected_reason in (
        ("late_response", "forecast_expires_before_action_finishes"),
        ("uncalibrated_time", "unqualified_pad_forecast_profile"),
        ("reoccupied_revision", "context_invalidated"),
        ("hold_lost", "current_hold_or_wait_budget_unavailable"),
        ("sea_phase", "outside_city_pad_phase"),
    ):
        situation, envelope = fixture()
        now = 101
        if name == "late_response":
            now = 108
        elif name == "uncalibrated_time":
            situation.constraints["pad_forecast_profile"]["time_calibration_ref"] = ""
        elif name == "reoccupied_revision":
            situation.constraints["prediction_context"]["state_revision"] = "revision-2"
        elif name == "hold_lost":
            situation.observations["bounded_hold_available"] = False
        else:
            situation.progress["region"] = "sea"
        result = evaluate(situation, envelope, FixtureJudge(), now=now, max_age_seconds=20)
        passed = result["reason"] == expected_reason and not result["judge_invoked"]
        records.append(dict(case=name, passed=passed, envelope=envelope, result=result))
    return records


def audit_native(probe):
    from scripts.evaluate_yokohama_pad_models import evaluate as reopen
    from scripts.probe_yokohama_pad_models import sha

    old = reopen(probe / "inputs", probe / "results")
    identity_path = probe / "results/wam/identity.json"
    identity = json.loads(identity_path.read_text())
    return dict(
        source_identity_sha256=sha(identity_path),
        evidence_integrity=old["evidence_integrity"],
        physical_future_time_calibration_verified=identity[
            "physical_future_time_calibration_verified"
        ],
        reference_eligibility_passed=old["reference_eligibility_passed"],
        dynamic_pad_semantic_reader_implemented=False,
        native_forecasts_admitted=False,
        reason="Existing images have no qualified dynamic pad semantics or execution-time forecast contract",
        vla_wait_discrimination_required_for_new_selection=False,
        prior_vla_diagnostic_unchanged=True,
        new_inference_invoked=False,
        aircraft_flown=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-probe", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve existing evidence; choose a new output")
    records = run_cases()
    output = dict(
        schema="yokohama_pad_prediction_smoke.v1",
        status="passed" if all(r["passed"] for r in records) else "failed",
        scope="CPU synthetic WAM forecasts and fixture judge through shared Mission Assurance; no AP dispatch",
        native_model_inference_invoked=False,
        gpu_requested=False,
        cost_usd=0,
        cases=records,
    )
    if args.native_probe:
        output["native_audit"] = audit_native(args.native_probe)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": output["status"], "cases": len(records), "gpu_requested": False}))
    return 0 if output["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
