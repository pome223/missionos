#!/usr/bin/env python3
"""Exercise the final supplemental host/worker inherited-pipe boundary.

Synthetic handler only: no aircraft integration, socket, secret lookup or model.
Run explicitly; importing this module starts no child process or execution.
"""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def public_observation():
    return {"schema": "missionos.starship_final_supervision_observation.v1", "scope_sha256": "c"*64,
        "facts": {"altitude_m": 200.}, "allowed_actions": ["continue_mission", "withhold_deployment"]}


def worker(read_fd, write_fd, case):
    from src.runtime.starship_final_protocol import FinalObservationClient, request
    from src.runtime.starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter
    if any(any(part in key.upper() for part in ("SECRET", "TOKEN", "API_KEY", "CREDENTIAL")) for key in os.environ):
        raise ValueError("smoke_worker_received_private_environment")
    reader, writer = ApprovalPipeReader(read_fd), ApprovalPipeWriter(write_fd)
    try:
        client = FinalObservationClient(writer, reader, plan_sha256="a"*64, source_sha256="b"*64)
        client.begin(request("smoke-"+case, "ascent", 100., 135., public_observation()))
        stop = time.monotonic()+5.
        result = None
        while result is None and time.monotonic() < stop:
            result = client.poll(100.)
            time.sleep(.001)
        expected = {"status": "unavailable", "action": None} if case == "handler_failure" else {
            "status": "fixture_only", "action": "request_independent_tracking", "model_inference_invoked": False}
        if result != expected:
            raise ValueError("smoke_unexpected_result")
        if case == "spent_request":
            try:
                client.begin(request("smoke-"+case, "ascent", 100., 135., public_observation()))
            except ValueError:
                pass
            else:
                raise AssertionError("spent_request_retried")
        print(json.dumps({"passed": True, "case": case, "worker_provider_calls": 0,
            "physical_steps": 0, "result": result, "permission_granted": False}))
    finally:
        reader.close()
        writer.close()


def run_case(case):
    from src.runtime.starship_final_protocol import FinalObservationHost
    from src.runtime.starship_inflight_runtime import approval_pipe
    hr, cw = approval_pipe()
    cr, hw = approval_pipe()
    calls = []
    def handler(value, *, remaining_s, outer_deadline_monotonic):
        calls.append(value)
        if remaining_s <= 0 or outer_deadline_monotonic <= time.monotonic():
            raise AssertionError("expired_request_reached_handler")
        if case == "handler_failure":
            raise RuntimeError("private error body is intentionally omitted")
        return {"status": "fixture_only", "action": "request_independent_tracking", "model_inference_invoked": False}
    host = FinalObservationHost(hr, hw, plan_sha256="a"*64, source_sha256="b"*64, handler=handler)
    child = None
    try:
        # Exact allowlist; no inherited credentials and no .env loader.
        environment = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TMPDIR", "LANG") if k in os.environ}
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", case,
            "--read-fd", str(cr.fd), "--write-fd", str(cw.fd)],
            cwd=ROOT, env=environment, pass_fds=(cr.fd, cw.fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cr.close()
        cw.close()
        deadline = time.monotonic()+10.
        while child.poll() is None and time.monotonic() < deadline:
            host.tick()
            time.sleep(.001)
        if child.poll() is None:
            child.terminate()
            raise TimeoutError("synthetic_child_deadline")
        out, err = child.communicate(timeout=2.)
        if child.returncode != 0:
            raise RuntimeError("synthetic_child_failed: "+err[-2000:])
        value = json.loads(out)
        if value.get("passed") is not True or len(calls) != 1:
            raise AssertionError("synthetic_boundary_failed")
        value["host_handler_calls"] = len(calls)
        value["boundary"] = "real_child_inherited_pipes_with_no_provider_or_plant"
        return value
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=2.)
        for endpoint in (hr, hw, cr, cw):
            try:
                endpoint.close()
            except OSError as error:
                if error.errno != 9:
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("valid", "handler_failure", "spent_request"))
    parser.add_argument("--read-fd", type=int)
    parser.add_argument("--write-fd", type=int)
    args = parser.parse_args()
    if args.worker:
        worker(args.read_fd, args.write_fd, args.worker)
    else:
        results = [run_case(case) for case in ("valid", "handler_failure", "spent_request")]
        print(json.dumps({"passed": all(x["passed"] for x in results), "cases": results,
            "model_calls": 0, "physical_steps": 0, "flight_result": False}, indent=2))


if __name__ == "__main__":
    main()
