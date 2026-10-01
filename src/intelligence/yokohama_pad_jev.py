"""Jev pad adapter with explicit fixture and separately bounded live modes.
No credentials are loaded. The provider transport is always explicitly supplied.
continue = no additional intervention; hold = bounded additional wait, never dispatch.
"""

import json
import math
import os
from pathlib import Path
import time

from src.intelligence.jev_assurance import JevAssuranceJudge
from src.runtime.yokohama_payload import digest
from src.runtime.yokohama_pad_queue import ENTER


def configuration():
    mode = os.environ.get("MISSIONOS_YOKOHAMA_JEV_MODE", "fixture")
    if mode == "live":
        from src.intelligence.yokohama_jev_live import BUDGET_ID, LIVE_ENABLED, MODEL
        if not LIVE_ENABLED:
            raise ValueError("Public demonstration live budget is closed; fixture only")
        if os.environ.get("MISSIONOS_YOKOHAMA_JEV_BUDGET_ID") != BUDGET_ID:
            raise ValueError("Prior live budget is closed; separately approved budget required")
        return dict(backend="jev", mode="live", model=MODEL, live_enabled=True,
                    budget_id=BUDGET_ID, max_http_requests=2, max_deliveries=1,
                    max_total_usd=0.01, max_payload_bytes=32768, retries=0, redirects=0,
                    prior_live_budget="closed", judge_timeout_seconds=20)
    if mode != "fixture":
        raise ValueError("Unknown Jev mode")
    return dict(backend="jev", mode="fixture", fixture_choice="continue", model="jev-latest",
                live_enabled=False, external_api_calls=0, prior_live_budget="closed",
                judge_timeout_seconds=20)


def judge(request, expected, *, transport=None):
    base = dict(judge_request_id=request["judge_request_id"], request_sha256=digest(request))
    try:
        if configuration() != expected:
            raise ValueError("Configuration changed")
        if (
            request["situation"]["rules_action"] != ENTER
            or request["allowed_actions"] != ["enter", "wait"]
            or type(request["decisions_remaining"]) is not int
            or request["decisions_remaining"] < 0
            or not math.isfinite(request["remaining_wait_seconds"])
            or request["remaining_wait_seconds"] < 0
        ):
            raise ValueError("Invalid pad contract")
        # decisions_remaining excludes the current already-issued request.
        choices = ["continue"]
        if request["remaining_wait_seconds"] >= 1:
            choices.append("hold")
        semantics = {
            "continue": "No additional intervention; independent Rules retain authority.",
            "hold": "Add at most five seconds of wait; no route or control changes.",
        }
        prompt = {
            "decision_contract": {"allowed_response_kinds": choices},
            "mission_situation": {
                "mission_contract": {"response_mapping": semantics},
                "observation_id": request["observation_id"],
                "situation": request["situation"],
                "remaining_wait_seconds": request["remaining_wait_seconds"],
                "decisions_remaining": request["decisions_remaining"],
            },
            "response_semantics": semantics,
        }

        def fixture(payload):
            return {
                "model": "jev-contract-fixture",
                "answers": {
                    "response": {
                        "type": "choice",
                        "choice": "continue",
                        "probabilities": {c: float(c == "continue") for c in choices},
                        "confidence": 1.0,
                    },
                    "review": {"choice": "bounded"},
                },
            }

        if expected["mode"] == "live" and transport is None:
            raise ValueError("Bounded live transport required")
        model = JevAssuranceJudge(model=expected["model"], transport=transport or fixture)
        result = model.judge(prompt)
        choice = result.output["proposed_response_kind"]
        seconds = min(5, request["remaining_wait_seconds"]) if choice == "hold" else 0
        output = dict(
            observation_id=request["observation_id"],
            action="wait" if choice == "hold" else "enter",
            wait_seconds=seconds,
            rationale="Adapter template: " + semantics[choice],
        )
        return dict(
            base,
            judge_status="valid",
            decision=output,
            invocation={
                **result.invocation_evidence,
                "invocation_kind": "injected_contract_test"
                if transport
                else "deterministic_fixture",
                "inference_invoked": False,
                "external_api_calls": 0,
                **(transport.evidence() if hasattr(transport, "evidence") else {}),
                "rationale_source": "adapter_template",
            },
        )
    except Exception as exc:
        return dict(
            base,
            judge_status="unavailable",
            error_type=type(exc).__name__,
            invocation={"inference_invoked": False, "external_api_calls": 0,
                        **(transport.evidence() if hasattr(transport, "evidence") else {})},
        )


def admit_mailbox(folder, request_path, request, plan_sha256=None):
    """Bind owned worker elapsed clock to fresh mailbox, never to host Unix.
    mtime is only a conservative host freshness fence, not the observation clock.
    Request rereads/replays are separately fenced by irreversible DB reservations.
    """
    folder, request_path = Path(folder), Path(request_path)
    if request_path.is_symlink() or not request_path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("Foreign mailbox")
    now = time.time()
    age = now - request_path.stat().st_mtime
    if not 0 <= age <= 20:
        raise ValueError("Stale or future mailbox")
    if (
        digest({k: v for k, v in request.items() if k != "judge_request_id"})
        != request["judge_request_id"]
    ):
        raise ValueError("Unbound judge request")
    config = json.loads((folder / "config.json").read_text())
    if request["run_id"] != config["run_id"]:
        raise ValueError("Foreign run")
    if plan_sha256 is not None and config.get("map_plan_sha256") != plan_sha256:
        raise ValueError("Foreign plan")
    from src.runtime.yokohama_pad_queue import propose, judge_situation

    pad = None
    for path in sorted((folder / "pad-decisions").glob("*/request.json")):
        if path.is_symlink():
            raise ValueError("Foreign pad mailbox")
        value = json.loads(path.read_text())
        if value.get("request_id") == request["pad_request_id"]:
            pad = value
            break
    if pad is None:
        raise ValueError("Missing bound pad request")
    response = propose(config, pad)  # verifies run/config/request digest and Rules
    saved = json.loads((request_path.parent / "input-response.json").read_text())
    if digest(saved) != request.get("rules_response_sha256"):
        raise ValueError("Unbound pre-judge response")
    advisory = saved.get("advisory")
    if advisory:
        from src.runtime.yokohama_pad_advisory_contract import selected_action
        # Reapply the independent advisory contract; it may only restrict Rules entry.
        response = dict(response, advisory=advisory,
                        proposed_action=selected_action(config, pad, advisory, response["proposed_action"]))
    if saved["proposed_action"] != response["proposed_action"]:
        raise ValueError("Advisory/Rules action mismatch")
    if response["proposed_action"] != ENTER or request["situation"] != judge_situation(
        config, pad, response
    ):
        raise ValueError("Rules veto or unbound observation")
    tail_path = folder / "flight-trajectory.jsonl"
    tail_age = now - tail_path.stat().st_mtime
    if not 0 <= tail_age <= 2:
        raise ValueError("Stale worker heartbeat")
    with tail_path.open("rb") as f:
        f.seek(max(0, tail_path.stat().st_size - 65536))
        lines = f.read().splitlines()
    latest = next(json.loads(x) for x in reversed(lines) if x.endswith(b"}"))
    observed = latest["wall_s"]
    issued = request["issued_wall_s"]
    if (
        latest["run_id"] != config["run_id"]
        or latest["world_sha256"] != config["world"]["world_sha256"]
        or not all(type(x) in (int, float) and math.isfinite(x) for x in (observed, issued))
        or not 0 <= observed - issued <= 20
    ):
        raise ValueError("Stale elapsed observation")
    return dict(
        clock_domain="owned_worker_elapsed_monotonic",
        issued_elapsed_s=issued,
        observed_elapsed_s=observed,
        mailbox_age_s=age,
        host_deadline_monotonic_s=time.monotonic() + 20,
    )
