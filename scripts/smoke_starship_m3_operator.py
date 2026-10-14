#!/usr/bin/env python3
"""Real Gateway M3 flight with an explicitly scripted authenticated test operator.

The AI must itself request an amendment. This driver never requests it, modifies
candidate evidence, dispatches a burn, retries a flight, or impersonates a human.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.smoke_starship_chat_gateway import run
from scripts.start_starship_gateway import read_operator_token


class ScriptedOperator:
    def __init__(self, port, token, output, choice):
        self.origin = f"http://127.0.0.1:{port}"
        self.token, self.output, self.choice = token, Path(output), choice
        self.rows, self.resolved, self.run_id = [], False, None

    def _call(self, path, body=None):
        headers = {"Authorization": "Bearer " + self.token,
            "Origin": self.origin, "Referer": self.origin + "/missionos/starship/inflight",
            "Sec-Fetch-Site": "same-origin", "X-MissionOS-Operator": "starship-inflight-v1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                code, value = response.status, json.load(response)
        except HTTPError as error:
            code, value = error.code, json.load(error)
        # Auth credentials deliberately excluded. Request/receipt hashes are evidence.
        self.rows.append({"path": path, "method": "POST" if body is not None else "GET",
            "request_body": body, "status": code, "response_body": value})
        self.save()
        return code, value

    def save(self):
        self.output.write_text(json.dumps({
            "schema": "missionos.starship_m3_scripted_operator_http.v1",
            "operator_response_source": "scripted_test_driver", "run_id": self.run_id,
            "authenticated_human_identity_verified": False, "physical_execution": False,
            "choice": self.choice, "resolved": self.resolved, "http": self.rows,
        }, indent=2) + "\n")

    def __call__(self, launched):
        self.run_id = launched["execution"]["run_id"]
        if self.resolved:
            return
        base = ("/missionos/starship/inflight/runs/" + self.run_id + "-return/missions/"
                + self.run_id + "/vehicles/ship")
        code, value = self._call(base + "/pending")
        if code in (404, 409):  # Host registration / first trusted telemetry can lag spawn.
            return
        if code != 200:
            raise RuntimeError("scripted_operator_http_unavailable")
        if not value["resolvable"] or self.choice == "expire":
            return
        request = value["request"]
        body = {key: request[key] for key in ("observed_state_sha256", "candidate_sha256")}
        body["request_sha256"] = request["sha256"]
        path = base + "/requests/" + request["request_id"] + "/" + self.choice
        code, receipt = self._call(path, body)
        if code != 200 or receipt.get("executor_command_issued") is not False:
            raise RuntimeError("scripted_operator_resolution_failed")
        self.resolved = True
        # Immediate replay must not create another grant, regardless of worker speed.
        if self._call(path, body)[0] != 409:
            raise RuntimeError("scripted_operator_replay_not_rejected")
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", default="sixdof_m3_inflight_op_trial_02")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operator-token-file", type=Path, required=True)
    parser.add_argument("--choice", choices=("approve", "reject", "expire"), required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("invalid loopback port")
    if args.output_dir.exists():
        parser.error("fresh output directory required")
    token = read_operator_token(args.operator_token_file)
    args.output_dir.mkdir(parents=True, mode=0o700)
    operator = ScriptedOperator(args.port, token, args.output_dir / "operator-http.json", args.choice)
    try:
        summary = run(args.port, args.scenario, args.output_dir / "http", operator_poll=operator)
    finally:
        operator.save()
    verdict = json.loads((args.output_dir / "http/verification.json").read_text())
    amendment = verdict.get("inflight_verification") or {}
    request_observed = any(row["method"] == "GET" and row["status"] == 200
        and isinstance(row["response_body"].get("request"), dict) for row in operator.rows)
    if args.choice == "approve":
        exercised = (request_observed and operator.resolved
            and amendment.get("scope_amendment_applied") is True
            and amendment.get("reservation_observed") is True)
    else:
        exercised = (request_observed and amendment.get("negative_disposition_verified") is True
            and (operator.resolved if args.choice == "reject" else not operator.resolved))
    print(json.dumps({"flight": summary, "scripted_operator_resolved": operator.resolved,
                     "requested_operator_branch_exercised": exercised}, indent=2))
    # Non-use is preserved as evidence, never a reason to rerun until AI asks.
    return 0 if summary["m3"]["case_accepted"] and exercised else 3


if __name__ == "__main__":
    raise SystemExit(main())
