"""Run Core -> Assurance CLI admission with public synthetic evidence; no LLM/model files."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
)
from src.intelligence.mission_assurance_agent import (
    MissionSituation,
    build_mission_assurance_prompt,
)
from src.intelligence.prediction_evidence import capture_prediction_evidence


class FixturePredictor:
    binding = PredictionBinding(
        "fixture", "a" * 64, "delivery.v1", "b" * 64, "fixture.v1", "state.v1"
    )

    def predict(self, request):
        return (OptionForecast("wait", 2.0, 0.2, {"battery_remaining": 0.6}),)


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="assurance-prediction-") as directory:
        root = Path(directory)
        for name in (
            "available",
            "unavailable",
            "stale",
            "binding_mismatch",
            "action_invalidated",
            "missing_context",
        ):
            now = time.time()
            request = PredictionRequest(
                "req",
                "obs",
                now - (120 if name == "stale" else 0),
                FixturePredictor.binding,
                {"battery": 0.7},
                (PredictionOption("wait", 2.0, {}),),
            )
            registry = PredictionRegistry()
            if name != "unavailable":
                registry.register(FixturePredictor())
            envelope = capture_prediction_evidence(
                request,
                registry.forecast(request, now=request.observed_at),
                execution_id="fixture-run",
                state_revision="1",
                source_ref="fixture:sensor",
            )
            context = deepcopy(envelope["context"])
            if name == "binding_mismatch":
                context["binding"]["policy_sha256"] = "c" * 64
            if name == "action_invalidated":
                context["state_revision"] = "2"
            situation = MissionSituation(
                "fixture-situation",
                "2026-09-19T00:00:00Z",
                {"prediction_contract": "delivery.v1"},
                {},
                {"battery": 0.7},
                {} if name == "missing_context" else {"prediction_context": context},
                {},
                ("fixture:sensor",),
                "fixture.v1",
                "fixture-input",
                "fixture",
            )
            source = root / f"{name}-situation.json"
            evidence = root / f"{name}-evidence.json"
            output = root / f"{name}-result.json"
            source.write_text(json.dumps(asdict(situation)))
            evidence.write_text(json.dumps(envelope))
            command = [
                sys.executable,
                "-m",
                "missionos_cli",
                "prediction",
                "admit-evidence",
                "--situation",
                str(source),
                "--evidence",
                str(evidence),
                "--max-age-seconds",
                "10",
                "--output",
                str(output),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            result = json.loads(output.read_text())
            receipt = result["receipt"]
            assert receipt["status"] == ("adopted" if name == "available" else "rejected")
            updated = MissionSituation.from_dict(result["situation"])
            prompt = build_mission_assurance_prompt(updated)
            admitted = prompt["mission_situation"]["uncertainty"]["prediction_evidence"]
            assert ("forecasts" in admitted) == (name == "available")
            assert updated.observations == situation.observations
            assert all(
                receipt[k] is False
                for k in (
                    "feasibility_established",
                    "approval_recorded",
                    "dispatch_authority_created",
                    "physical_execution_invoked",
                    "completion_claimed",
                    "llm_judgment_invoked",
                )
            )
            results[name] = {
                "status": receipt["status"],
                "reason": receipt["reason"],
                "cli_exit_code": 0,
            }
        # Never silently overwrite an existing receipt.
        duplicate = subprocess.run(command, capture_output=True, text=True)
        assert duplicate.returncode != 0
    print(
        json.dumps(
            {
                "cases": results,
                "overwrite_rejected": True,
                "llm_invoked": False,
                "simulator_invoked": False,
                "executor_invoked": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
