#!/usr/bin/env python3
"""Real inherited subprocess pipes with synthetic production-decoder answers.

No credentials, hosted inference, physics, approval or authority is exercised.
Each output directory is exclusive and records source and artifact hashes.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime.starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, approval_pipe  # noqa: E402
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient, RestoredDecisionHost  # noqa: E402

CASES = {"jev_maintain": ("valid", "select_nominal", 1),
    "explicit_delegation": ("valid", "select_next_orbit", 2), "invalid_response": ("no_valid_judgment", None, 1)}


def encode(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()+b"\n"


def save(path, value):
    with path.open("xb") as stream:
        stream.write(encode(value))


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sources():
    return {name: sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        "scripts/smoke_starship_restored_decision_pipe.py", "src/runtime/starship_restored_decision_pipe.py",
        "src/runtime/starship_inflight_runtime.py", "src/intelligence/starship_restored_agent.py")}


def request(case):
    return {"schema": "missionos.starship_restored_decision_request.v1", "request_id": "smoke-"+case,
        "phase": "return", "scope_sha256": "a"*64, "time_s": 2000., "deadline_s": 2180.,
        "facts": {"time_s": 2000., "released_count": 13, "retained_count": 13,
            "candidate.nominal.contact_speed_mps": 4.7}, "notice_summary": "Synthetic transport smoke only.",
        "allowed_actions": ["select_nominal", "select_next_orbit", "request_deep_reasoning"]}


class SyntheticTransport:
    """Invoke the production decoder; no provider send or native-call receipt."""

    def __init__(self, case):
        self.case, self.calls = case, []

    def _invoke(self, provider, payload, receipt, decode):
        self.calls.append(provider)
        receipt.update(mode="synthetic_fixture", invocation_kind="synthetic_decoder_only",
            model_inference_invoked=False, model_inference_status="not_invoked", call_attempted=False,
            call_succeeded=False, synthetic_transport=True)
        if provider == "typesafe":
            actions = payload["state"]["allowed_actions"]
            chosen = "request_deep_reasoning" if self.case == "explicit_delegation" else "select_nominal"
            value = {"model": "invalid-model" if self.case == "invalid_response" else payload["model"],
                "answers": {"decision": {"type": "choice", "choice": chosen,
                    "probabilities": {action: 1./len(actions) for action in actions}, "confidence": .5}}}
        else:
            value = {"model": payload["model"], "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"action": "select_next_orbit",
                    "summary": "Compare only the supplied synthetic candidate.",
                    "reference_ids": ["candidate.nominal.contact_speed_mps"]})}}]}
        try:
            result, model = decode(value)
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
            receipt["status"] = "invalid_response"
            return None
        receipt.update(status="succeeded", model_id=model)
        return result


def blocked(*_a, **_kw):
    raise AssertionError("Decision pipe smoke must not use network, models or physics")


def no_network_or_physics(stack):
    from src.runtime import starship_sixdof as dyn
    for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
            (socket, "create_connection"), (socket, "getaddrinfo"), (dyn, "step"), (dyn, "_rk4")):
        stack.enter_context(patch.object(owner, name, blocked))


def child(args):
    from src.intelligence.starship_restored_agent import RestoredAgent
    plan = json.loads((args.output_dir/"plan.json").read_bytes())
    forbidden = [key for key in os.environ if any(word in key.lower() for word in ("api_key", "token", "credential", "secret", "hmac"))]
    require(not forbidden, "child_received_credential_environment")
    reader, writer = ApprovalPipeReader(args.response_fd), ApprovalPipeWriter(args.request_fd)
    context = {"run_id": args._worker, "mission_id": "decision-pipe-smoke", "vehicle_id": "ship"}
    client = RestoredDecisionClient(context=context, plan_sha256=plan["plan_sha256"], source_sha256=plan["source_sha256"],
        request_writer=writer, response_reader=reader)
    try:
        with ExitStack() as stack:
            no_network_or_physics(stack)
            stack.enter_context(patch.object(RestoredAgent, "assess", blocked))
            client.begin(request(args._worker))
            started, result = time.monotonic(), None
            while result is None and time.monotonic()-started < 10.:
                result = client.poll(2000.)
                time.sleep(.001)
            require(result is not None, "synthetic_child_result_missing")
            expected = CASES[args._worker]
            require((result["status"], result["action"], len(result["attempts"])) == expected,
                "synthetic_child_result_differed")
            require(result["model_inference_invoked"] is False and result["execution_permission_granted"] is False,
                "synthetic_child_claim_changed")
            save(args.output_dir/args._worker/"child.json", {"schema": "missionos.restored_decision_pipe_smoke_child.v1",
                "case": args._worker, "result": result, "client": client.record(), "credential_environment_absent": True,
                "models": 0, "physics_steps": 0, "synthetic_only": True})
    finally:
        reader.close()
        writer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--_worker", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--request-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--response-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        require(args.request_fd is not None and args.response_fd is not None, "inherited_child_fds_required")
        return child(args)
    require(args.request_fd is None and args.response_fd is None and not args.output_dir.exists(), "exclusive_smoke_output_required")
    args.output_dir.mkdir(parents=True)
    pinned = sources()
    source_sha = sha256(encode(pinned)).hexdigest()
    plan = {"schema": "missionos.restored_decision_pipe_smoke_plan.v1", "sources": pinned,
        "source_sha256": source_sha, "cases": list(CASES), "models": 0, "physics_steps": 0, "synthetic_only": True}
    plan["plan_sha256"] = sha256(encode(plan)).hexdigest()
    save(args.output_dir/"plan.json", plan)
    from src.intelligence.starship_restored_agent import RestoredAgent
    results = []
    for case in CASES:
        folder = args.output_dir/case
        folder.mkdir()
        reader, child_writer = approval_pipe()
        child_reader, writer = approval_pipe()
        transport = SyntheticTransport(case)
        host = RestoredDecisionHost(context={"run_id": case, "mission_id": "decision-pipe-smoke", "vehicle_id": "ship"},
            plan_sha256=plan["plan_sha256"], source_sha256=source_sha, reader=reader, writer=writer,
            agent_factory=lambda **kwargs: RestoredAgent(transport=transport, **kwargs))
        environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")}
        environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--output-dir", str(args.output_dir.resolve()),
            "--_worker", case, "--request-fd", str(child_writer.fd), "--response-fd", str(child_reader.fd)],
            cwd=ROOT, env=environment, pass_fds=(child_writer.fd, child_reader.fd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        child_writer.close()
        child_reader.close()
        try:
            with ExitStack() as stack:
                no_network_or_physics(stack)
                start = time.monotonic()
                while proc.poll() is None:
                    require(time.monotonic()-start < 15., "synthetic_subprocess_wall_limit")
                    host.tick()
                    time.sleep(.001)
            stdout = proc.communicate(timeout=2.)[0]
            with (folder/"worker.log").open("xb") as stream:
                stream.write(stdout)
            require(proc.returncode == 0, "synthetic_subprocess_failed")
            report = host.record()
            require(len(report["requests"]) == 1 and len(transport.calls) == CASES[case][2], "synthetic_attempt_count_changed")
            save(folder/"host.json", report)
            results.append({"case": case, "status": report["requests"][0]["delivered_result"]["status"],
                "action": report["requests"][0]["delivered_result"]["action"], "synthetic_decoder_attempts": len(transport.calls)})
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            reader.close()
            writer.close()
    require(sources() == pinned, "smoke_source_changed")
    manifest = {str(path.relative_to(args.output_dir)): {"sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        for path in sorted(args.output_dir.rglob("*")) if path.is_file()}
    summary = {"schema": "missionos.restored_decision_pipe_smoke_result.v1", "passed": True, "cases": results,
        "sources": pinned, "files": manifest, "inherited_subprocesses": 3, "synthetic_only": True,
        "hosted_model_calls": 0, "physics_steps": 0, "approval_granted": False, "execution_permission_granted": False}
    save(args.output_dir/"result.json", summary)
    print(encode({"passed": True, "cases": len(results), "hosted_model_calls": 0, "physics_steps": 0}).decode())


if __name__ == "__main__":
    main()
