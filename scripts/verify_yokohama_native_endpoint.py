"""Offline verdict for actual adapted endpoint SITL logs, including safe failure."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def verify(root):
    from scripts.verify_yokohama_candidate_recovery import (
        read,
        require,
        rows,
        sha,
        verify as recovery,
    )
    from scripts.verify_yokohama_endpoint_feedback import verify as endpoint
    from scripts.verify_yokohama_sitl import verify as sitl
    from scripts.yokohama_native_endpoint_contract import CONTRACT, arguments, validate_config
    from scripts.yokohama_candidate_recovery import LIMITS
    from scripts.yokohama_endpoint_feedback import FIXED
    from scripts.yokohama_goal_distance_adapter import POLICY
    from src.runtime.yokohama_execution_service import proposal_digest
    from src.runtime.yokohama_native import digest

    root = Path(root).resolve()
    try:
        config, result = read(root / "config.json"), read(root / "result.json")
        validate_config(config)
        approval_raw = (root.parent / "execution-approval.json").read_bytes()
        approved = json.loads(approval_raw)
        p, a = approved["proposal"], approved["approval"]
        require(
            hashlib.sha256(approval_raw).hexdigest() == config["operator_approval_manifest_sha256"]
            and a["approved_proposal_sha256"] == proposal_digest(p)
            and p["city_models"] == config["decisions"]["backend"]
            and a["maximum_actual_flight_trials"] == 1
            and p.get("schema") == "yokohama.adapted-endpoint-proposal.v1"
            and p.get("contract") == CONTRACT
            and p.get("limits") == LIMITS
            and p.get("feedback_limits") == FIXED
            and p.get("adapter_policy") == POLICY
            and p.get("physical_execution_invoked") is False
            and p.get("simulator_arguments")
            == arguments(p["city_models"], p["image_id"], p.get("native_service_config"))
            and all(a.get(k) for k in ("operator_approval_ref", "actor_session_id", "approved_at")),
            "operator_approval_binding",
        )
        require(result["config_sha256"] == digest(config), "runtime_config_binding")
        require(
            result["image_id"]
            == p["image_id"]
            == read(root / "container-inspect.json")[0]["Image"],
            "runtime_image_binding",
        )
        require(
            {
                "yokohama_native_endpoint_contract.py",
                "yokohama_goal_distance_adapter.py",
                "yokohama_candidate_recovery.py",
                "yokohama_flight_worker.py",
                "verify_yokohama_native_endpoint.py",
                "verify_yokohama_endpoint_feedback.py",
            }
            <= result["source_sha256"].keys(),
            "required_runtime_sources_missing",
        )
        for name, value in result["source_sha256"].items():
            require(
                sha(root / name) == value
                and value in [v for k, v in p["input_sha256"].items() if Path(k).name == name],
                "approved_runtime_sources",
            )
        require(
            result.get("worker_reaped") is True and result.get("cleanup") is True,
            "local_runtime_cleanup_unconfirmed",
        )
        events = rows(root / "flight-events.jsonl")
        requests = []
        for path in sorted((root / "decisions").glob("*-request.json")):
            request = read(path)
            require(
                request["config_sha256"] == digest(config)
                and request["run_id"] == config["run_id"],
                "mailbox_request_binding",
            )
            sent = [
                e
                for e in events
                if e["event"] == "city_request"
                and e["operation"] == request["operation"]
                and e["cycle"] == request["cycle"]
            ]
            require(
                len(sent) == 1 and sent[0]["request_sha256"] == digest(request),
                "mailbox_request_event_binding",
            )
            response_path = path.with_name(path.name.replace("-request", "-response"))
            if response_path.exists():
                response = read(response_path)
                require(
                    response["request_sha256"] == digest(request)
                    and response["operation"] == request["operation"]
                    and response["run_id"] == request["run_id"],
                    "mailbox_response_binding",
                )
                got = [
                    e
                    for e in events
                    if e["event"] == "city_response"
                    and e["operation"] == request["operation"]
                    and e["cycle"] == request["cycle"]
                ]
                require(
                    len(got) == 1
                    and got[0]["response_sha256"] == digest(response)
                    and got[0]["wall_s"] >= sent[0]["wall_s"],
                    "mailbox_response_event_binding",
                )
            else:
                require(request["operation"] in {"vla", "wam"}, "terminal_response_missing")
            requests.append(request)
        for op in ("vla", "wam"):
            require(
                len([r for r in requests if r["operation"] == op]) <= 2,
                "model_request_budget_exceeded",
            )
        require(requests and requests[-1]["operation"] == "stop", "model_revocation_missing")
        stop_path = sorted((root / "decisions").glob("*-response.json"))[-1]
        stop = read(stop_path)
        require(
            stop["operation"] == "stop"
            and "error" not in stop
            and stop["value"]["session_revoked"] is True,
            "terminal_revocation_failed",
        )
        if config["decisions"]["backend"] == "native":
            require(
                stop["value"].get("remote_model_processes_absent") is True,
                "remote_model_shutdown_unconfirmed",
            )
        require(
            not any(
                e["event"] == "city_segment_dispatched"
                and e["wall_s"]
                >= next(e["wall_s"] for e in events if e["event"] == "city_session_revoked")
                for e in events
            ),
            "dispatch_after_revocation",
        )
        if result.get("observed", {}).get("status") == "failed_recovered":
            outcome = recovery(root, adapted=True)
            require(
                outcome["status"] == "passed",
                "independent_recovery_failed:" + outcome.get("reason", ""),
            )
            return dict(
                outcome,
                native_model_control_verified=False,
                raw_model_output_control_verified=False,
                adapter_goal_control_verified=False,
                native_model_inference=False,
                limitation="Safe recovery only; model inference/control is not established by this verdict",
            )
        require(
            result["observed"]["model_goal_reached"] is True, "native_endpoint_goal_not_observed"
        )
        control = endpoint(root)
        require(
            control["status"] == "passed" and control["observed_goal_arrival_verified"],
            "independent_endpoint_failed:" + str(control.get("reasons")),
        )
        flight = sitl(root, REPO / "docs/examples/yokohama-urban-scene")
        require(
            flight["status"] == "passed", "independent_full_flight_failed:" + str(flight["checks"])
        )
        return dict(
            status="passed",
            mission_outcome="passed",
            recovery_outcome="not_needed",
            endpoint=control,
            full_flight=flight,
            adapter_goal_control_verified=True,
            native_model_control_verified=control["native_model_use_verified"],
            raw_model_output_control_verified=False,
            physical_execution_invoked=False,
            payload_delivery_verified=False,
        )
    except (OSError, ValueError, KeyError, TypeError, StopIteration, IndexError) as exc:
        return dict(
            status="failed",
            reason=type(exc).__name__ + ": " + str(exc),
            native_model_control_verified=False,
            adapter_goal_control_verified=False,
            raw_model_output_control_verified=False,
            physical_execution_invoked=False,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify(args.run)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
