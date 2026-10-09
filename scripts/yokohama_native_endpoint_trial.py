"""One L4 adapted endpoint trial, gated by fresh CPU flight and exact USD8 approval.

Default operation validates/describes a local plan; it never contacts GCP.
The inherited infrastructure retains identity-checked cleanup and absolute DELETE.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import yokohama_native_trial as cloud  # noqa: E402
from scripts.yokohama_native_endpoint_contract import CONTRACT, arguments  # noqa: E402
from src.runtime import yokohama_execution_service as vehicle  # noqa: E402

SCHEMA = "yokohama.native-adapted-endpoint-cloud-trial.v1"


def validate_plan(plan, *, require_cpu=True):
    if (
        plan.get("schema") != SCHEMA
        or plan.get("contract") != CONTRACT
        or plan.get("zone") != "us-west1-a"
        or plan.get("image") != cloud.IMAGES[1]
        or plan.get("max_runtime_s") != 3600
        or plan.get("max_attempts") != 1
        or plan.get("reserve_usd") != 8
        or plan.get("aggregate_budget_usd") != 8
        or plan.get("budget_scope") != "single_native_endpoint_attempt"
        or plan.get("hard_network_byte_cap") is not True
        or plan.get("external_egress_limit_bytes") != 1024**3
        or plan.get("external_ingress_limit_bytes") != 64 * 1024**3
        or plan.get("guaranteed_spend_cap") is not False
        or not re.fullmatch(r"[a-f0-9]{32}", plan.get("budget_id", ""))
        or not Path(plan.get("trial_directory", "")).is_absolute()
    ):
        raise ValueError("Unsupported native endpoint cloud scope or budget")
    for field in ("project", "instance"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{5,60}", plan.get(field, "")):
            raise ValueError("Invalid dedicated resource identity")
    cloud.local.image_id(plan.get("local_image_id"))
    if plan.get("source_sha256") != vehicle.recovery_input_hashes(REPO):
        raise ValueError("Native endpoint sources changed")
    estimate = plan.get("cost_estimate", {}).get("usd_before_tax_fx")
    if type(estimate) not in (int, float) or not 0 < estimate < 8:
        raise ValueError("Invalid full incremental cost estimate")
    payload = Path(plan["payload_directory"])
    if set(plan.get("payload_sha256", {})) != set(cloud.PAYLOAD):
        raise ValueError("Exactly the reviewed nine-file upload manifest is required")
    for name in cloud.PAYLOAD:
        if cloud.digest_file(payload / name) != plan["payload_sha256"][name]:
            raise ValueError("Payload changed: " + name)
        if name.endswith(".py") and name != "remote_lifecycle.py":
            if cloud.digest_file(REPO / "scripts" / name) != plan["payload_sha256"][name]:
                raise ValueError("Remote service differs from qualified source")
    if require_cpu:
        from scripts.verify_yokohama_native_endpoint import verify

        run = Path(plan["cpu_qualification_run"])
        config = json.loads((run / "config.json").read_text())
        approved = json.loads((run.parent / "execution-approval.json").read_text())
        if (
            config["decisions"]["backend"] != "fixture"
            or approved["proposal"]["input_sha256"] != plan["source_sha256"]
            or verify(run).get("mission_outcome") != "passed"
            or plan.get("cpu_verdict_sha256")
            != cloud.digest_file(run / "verification-native-endpoint.json")
        ):
            raise ValueError("Fresh full-source CPU endpoint flight qualification is required")
        verdict = json.loads((run / "verification-native-endpoint.json").read_text())
        if (
            verdict.get("status") != "passed"
            or verdict.get("adapter_goal_control_verified") is not True
            or verdict.get("native_model_control_verified") is not False
        ):
            raise ValueError("CPU qualification cannot be native inference or only recovery")


class EndpointTrial(cloud.Trial):
    def validate_plan(self):
        validate_plan(self.plan)

    def validate_operator_approval(self, approval):
        if (
            approval.get("approved_plan_sha256") != vehicle.proposal_digest(self.plan)
            or approval.get("approved_budget_usd") != 8
            or not all(approval.get(k) for k in ("reference", "actor", "approved_at"))
            or approval.get("approve_ephemeral_cleanup") is not True
            or approval.get("acknowledge_network_quota_cutoff") is not True
            or approval.get("acknowledge_estimate_not_spend_cap") is not True
        ):
            raise ValueError(
                "Fresh exact-plan USD8 and owned cleanup/network-quota approval required"
            )

    def validate_execution_binding(self, approval):
        self.validate_operator_approval(approval)
        self.validate_plan()

    def prepare_network_budget(self):
        # On the new owned VM only, count all non-loopback IPv4 bytes before
        # uploads/downloads. Exhaustion blocks traffic; provider/API deletion
        # still works independently. Refuse external IPv6 rather than bypass it.
        command = 'set -eu; test -z "$(ip -6 -o addr show scope global)"; '
        for direction, chain, size, flag in (
            ("OUTPUT", "MISSIONOS_EGRESS", 1024**3, "-o"),
            ("INPUT", "MISSIONOS_INGRESS", 64 * 1024**3, "-i"),
        ):
            command += (
                f"sudo -n iptables -w 5 -N {chain}; "
                f"sudo -n iptables -w 5 -A {chain} -m quota --quota {size} -j RETURN; "
                f"sudo -n iptables -w 5 -A {chain} -j REJECT; "
                f"sudo -n iptables -w 5 -I {direction} 1 ! {flag} lo -j {chain}; "
                f"sudo -n iptables -w 5 -S {chain}; "
                f"sudo -n iptables -w 5 -S {direction}; "
            )
        self.ssh(command, "network-budget-install", timeout=30)
        raw = (self.directory / "network-budget-install.stdout").read_text()
        for chain, size in (("MISSIONOS_EGRESS", 1024**3), ("MISSIONOS_INGRESS", 64 * 1024**3)):
            if (
                f"-N {chain}" not in raw
                or f"--quota {size}" not in raw
                or f"-A {chain} -j REJECT" not in raw
            ):
                raise ValueError("Owned VM transfer quota not confirmed; no bootstrap")
        cloud.write(
            self.directory / "network-budget.json",
            dict(
                installed=True,
                external_egress_limit_bytes=1024**3,
                external_ingress_limit_bytes=64 * 1024**3,
                excludes_loopback=True,
                deletion_control="GCP API remains outside VM packet quota",
            ),
        )

    def fly(self, approval, deadline):
        from scripts.yokohama_candidate_recovery import LIMITS
        from scripts.yokohama_endpoint_feedback import FIXED
        from scripts.yokohama_goal_distance_adapter import POLICY

        self.validate_execution_binding(approval)
        self.qualify_local()
        self.local_preparation.assert_fresh()
        if (deadline - cloud.now()).total_seconds() < 1500:
            raise TimeoutError("900-second flight plus 600-second cleanup reserve unavailable")
        resource = dict(
            schema="missionos.yokohama-cloud-resource.v1",
            **{k: self.plan[k] for k in ("project", "zone", "instance", "gcloud")},
            readiness_wait_s=0,
            ssh_key_file=self.plan["ssh_key"],
            known_hosts_file=str(self.directory / "known_hosts"),
        )
        cloud.write(self.directory / "resource.json", resource)
        service = dict(vla_port=18117, wam_port=18118)
        for op in ("start", "stop"):
            service[op + "_argv"] = [
                self.plan["python"],
                str(REPO / "scripts/yokohama_cloud_lifecycle.py"),
                "--resource-json",
                str(self.directory / "resource.json"),
                op,
            ]
        service_path = self.directory / "native-service.json"
        cloud.write(service_path, service)
        service_path.chmod(0o400)
        job = self.directory / "flight"
        job.mkdir()
        proposal = dict(
            schema="yokohama.adapted-endpoint-proposal.v1",
            proposal_id=self.plan["instance"],
            contract=copy.deepcopy(CONTRACT),
            limits=copy.deepcopy(LIMITS),
            feedback_limits=copy.deepcopy(FIXED),
            adapter_policy=copy.deepcopy(POLICY),
            city_models="native",
            physical_execution_invoked=False,
            image_id=self.plan["local_image_id"],
            native_service_config=str(service_path),
            native_service_config_sha256=cloud.digest_file(service_path),
            simulator_arguments=arguments("native", self.plan["local_image_id"], service_path),
            input_sha256=self.plan["source_sha256"],
        )
        approved = dict(
            approved_proposal_sha256=vehicle.proposal_digest(proposal),
            operator_approval_ref=approval["reference"],
            actor_session_id=approval["actor"],
            approved_at=approval["approved_at"],
            maximum_actual_flight_trials=1,
        )
        approval_path = job / "execution-approval.json"
        cloud.write(approval_path, dict(proposal=proposal, approval=approved))
        approval_path.chmod(0o400)
        self.validate_execution_binding(approval)
        with (self.directory / "tunnel.log").open("w") as log:
            self.local_preparation.assert_age()
            self.tunnel = subprocess.Popen(
                [
                    self.plan["gcloud"],
                    *self.ssh_args(),
                    "--",
                    "-N",
                    "-L",
                    "127.0.0.1:18117:127.0.0.1:18117",
                    "-L",
                    "127.0.0.1:18118:127.0.0.1:18118",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "ServerAliveInterval=20",
                    "-o",
                    "ServerAliveCountMax=3",
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        with (job / "service.log").open("w") as log:
            self.local_preparation.assert_age()
            self.flight = subprocess.Popen(
                [
                    self.plan["python"],
                    str(REPO / "scripts/yokohama_sitl.py"),
                    *proposal["simulator_arguments"],
                    "--approve-sitl",
                    "--approval-manifest",
                    str(approval_path),
                    "--output-dir",
                    str(job / "run"),
                ],
                cwd=REPO,
                env=self.local_environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        while self.flight.poll() is None:
            if self.tunnel.poll() is not None or (deadline - cloud.now()).total_seconds() < 600:
                raise TimeoutError("Tunnel exited or cleanup reserve reached; no retry")
            time.sleep(1)
        # A recovered mission has a nonzero runner exit; verify it after cloud cleanup.
        if not (job / "run/result.json").is_file():
            raise RuntimeError("Endpoint worker produced no result")
        return job, proposal, approved

    def verify_flight(self, job, proposal, execution_approval):
        from scripts.verify_yokohama_native_endpoint import verify

        self.validate_plan()
        result = verify(job / "run")
        cloud.write(job / "run/verification-native-endpoint.json", result)
        if result["status"] != "passed":
            raise RuntimeError("Independent native endpoint verification failed")

    def execute(self, approval):
        self.validate_execution_binding(approval)
        code = super().execute(approval)
        path = self.directory / "controller-result.json"
        if path.exists():
            value = json.loads(path.read_text())
            verdict_path = self.directory / "flight/run/verification-native-endpoint.json"
            verdict = json.loads(verdict_path.read_text()) if verdict_path.exists() else {}
            value.update(
                mission_outcome=verdict.get("mission_outcome", "unverified"),
                recovery_outcome=verdict.get("recovery_outcome", "unverified"),
                raw_model_output_control_verified=False,
                payload_delivery_verified=False,
            )
            cloud.write(path, value)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--operator-approval", type=Path)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text())
    validate_plan(plan, require_cpu=args.execute)
    if args.execute:
        if args.operator_approval is None:
            parser.error("Execution requires fresh exact-plan USD8 approval")
        return EndpointTrial(plan, args.plan.parent).execute(
            json.loads(args.operator_approval.read_text())
        )
    print(
        json.dumps(
            dict(
                status="planned",
                cloud_calls=0,
                flight_attempts=0,
                cpu_qualification_required=True,
                paid_approval_required=True,
                proposed_budget_usd=8,
                create_arguments=cloud.create_arguments(
                    plan, cloud.now() + cloud.timedelta(seconds=3600)
                ),
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
