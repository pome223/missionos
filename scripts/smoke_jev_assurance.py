"""Opt-in paired hosted judgments through the PR104 governed HTTP boundary.

Synthetic prediction forecasts and a fixture executor: no simulator or physical claims.
Outputs are local experimental records, not a public provider benchmark.
"""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import statistics
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from missionos_core.prediction import OptionForecast, PredictionBinding, prediction_digest
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.prediction.stacking import ENVIRONMENT, INPUT_SCHEMA, MACRO, MISSION
from src.prediction.stacking_mission import GovernedStackingSession, DeepSeekJudge
from src.prediction.service import make_server


class Predictor:
    binding = PredictionBinding(
        "synthetic-predictor", "a" * 64, MISSION, "b" * 64, ENVIRONMENT, INPUT_SCHEMA
    )
    threshold = 0.5
    horizon_steps = 568

    def __init__(self, risks):
        self.risks = risks

    def predict(self, request):
        return tuple(
            OptionForecast(k, h, r, {})
            for k, h, r in zip(("continue", "bank"), (28.4, 14.2), self.risks)
        )


def body(count):
    return {
        "request_id": f"fixture-step-{count}",
        "seed": 1,
        "state_revision": count,
        "observation_id": f"fixture:{count}",
        "observed_at": time.time(),
        "binding": asdict(Predictor.binding),
        "options": [
            {
                "option_id": k,
                "horizon_seconds": h,
                "parameters": {"macro": "stacking.fixed_vla_placement.v1"},
            }
            for k, h in [("continue", 28.4), ("bank", 14.2)]
        ],
        "state": {
            "count": count,
            "objects": [[0, 0, 0.82, 1, 0, 0, 0]] * 10,
            "velocity": [[0] * 6] * 10,
            "physics": [[0.05, 0.04, 0.04, 0.07, 0.7, 0.005, 0.0001, 0, 0, 0, 1]] * 10,
            "robot": [0] * 12,
            "accepted": [[0] * 3] * 10,
            "plan": [0, 0, 0.82] + MACRO,
        },
    }


class Capture:
    def __init__(self, delegate):
        self.delegate = delegate

    def judge(self, prompt):
        self.prompt = prompt
        start = time.perf_counter()
        result = self.delegate.judge(prompt)
        self.elapsed_ms = (time.perf_counter() - start) * 1000
        return result


def run_case(root, name, count, risks, expected, backend):
    out = root / name / backend
    out.mkdir(parents=True)
    judge = Capture(
        JevAssuranceJudge() if backend == "jev" else DeepSeekJudge("deepseek-v4-flash", out / "llm")
    )
    session = GovernedStackingSession(
        Predictor(risks),
        out / "service",
        agent=MissionAssuranceAgent(judge),
        seeds=[1],
        approve=True,
        operator="explicit fixture smoke authorization",
    )
    session.revisions[1] = count  # A synthetic single-step state, not earlier executed placements.
    server = make_server(session)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def post(path, payload):
        with urlopen(
            Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=240,
        ) as r:
            return json.load(r)

    blocked = []
    executor_calls = 0
    try:
        request = body(count)
        decision = post("/decide", request)
        (out / "prompt.json").write_text(json.dumps(judge.prompt, indent=2))
        selected = decision["selected_option"]
        assert not decision["dispatch_authority_created"]
        dispatch_request = {
            "request_id": request["request_id"],
            "decision_sha256": prediction_digest(decision),
            "approval_policy_sha256": decision["approval_policy_sha256"],
            "state_revision": count,
            "observation_id": request["observation_id"],
            "state_sha256": prediction_digest(request["state"]),
            "observed_at": time.time(),
        }
        for field, value in [
            ("approval_policy_sha256", "missing"),
            ("state_revision", count + 1),
            ("observed_at", time.time() - 400),
            ("decision_sha256", "tampered"),
        ]:
            try:
                post("/dispatch", dispatch_request | {field: value})
            except HTTPError as exc:
                assert exc.code == 400
                blocked.append(field)
            else:
                raise AssertionError("invalid dispatch accepted: " + field)
        receipt = None
        if selected in ("continue", "bank"):
            dispatch = post("/dispatch", dispatch_request)
            assert dispatch["dispatch_authority_created"] and not dispatch["executor_invoked"]
            try:
                post("/dispatch", dispatch_request)
            except HTTPError as exc:
                assert exc.code == 400
                blocked.append("replay")
            else:
                raise AssertionError("replayed dispatch")
            # Fixture implementation of the simulator IO contract; no simulator is running.
            executor_calls += 1
            observed_count = count if selected == "continue" else count - 1
            outcome = {
                "request_id": request["request_id"],
                "request_sha256": decision["request_sha256"],
                "observation_id": request["observation_id"],
                "option_id": selected,
                "role": "selected_execution",
                "outcome_ref": "fixture:executor-output",
                "ticket": dispatch["ticket"],
                "runtime_invocation": {
                    "invocation_kind": "simulator_motor_loop",
                    "motor_steps": 284,
                    "fixture_only": True,
                },
                "result": {
                    "horizon_steps": 284,
                    "per_object_drop": [0] * 10,
                    "collapsed": False,
                    "technical_failure": None,
                    "score": observed_count,
                    "count_after": observed_count,
                },
            }
            receipt = post("/observe", outcome)
            assert (
                receipt["observed_score"] == observed_count
                and not receipt["physical_execution_invoked"]
            )
        result = {
            "case": name,
            "backend": backend,
            "selected": selected,
            "expected_under_fixture_proxy": expected,
            "matches_fixture_proxy": selected == expected,
            "model_latency_ms": judge.elapsed_ms,
            "blocked_requests": blocked,
            "fixture_executor_calls": executor_calls,
            "verifier_accepted_fixture_measurements": receipt is not None,
            "invocation": decision["proposal"]["model_invocation_evidence"],
            "simulator_invoked": False,
            "physical_execution_invoked": False,
        }
        (out / "result.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.getenv("RUN_MISSIONOS_JEV_ASSURANCE_SMOKE") != "1":
        raise SystemExit("RUN_MISSIONOS_JEV_ASSURANCE_SMOKE=1 required for hosted calls")
    args.output.mkdir(parents=True, exist_ok=False)
    # The early-stop numeric condition has headroom in both latency and choice.
    cases = [
        ("premature_stop_probe", 6, (0.084593, 0.007822), "continue"),
        ("delayed_collapse_probe", 9, (0.818211, 0.168163), "bank"),
        ("close_tradeoff_probe", 9, (0.14, 0.04), "continue"),
    ]
    results = []
    for name, count, risks, expected in cases:
        for backend in ("deepseek", "jev"):
            result = run_case(args.output, name, count, risks, expected, backend)
            results.append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "invocation"}), flush=True)
    summary = {
        "schema_version": "runtime_invocation_evidence.v1",
        "scope": "synthetic paired HTTP fixture",
        "cases": results,
        "physical_execution_invoked": False,
        "median_model_ms": {
            b: statistics.median(r["model_latency_ms"] for r in results if r["backend"] == b)
            for b in ("deepseek", "jev")
        },
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
