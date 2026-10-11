#!/usr/bin/env python3
"""Reviewable single-attempt GCP controller. Default action is offline description.

No cloud mutation without --execute and an approval bound to the exact plan.
The plan's egress reserve is an estimate, NOT a provider-enforced byte quota.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime import yokohama_execution_service as vehicle  # noqa: E402
from scripts import yokohama_local_preflight as local  # noqa: E402

SCHEMA = "missionos.yokohama-native-trial.v2"
ZONES = ("us-west1-a", "us-west1-b")
IMAGES = (
    "pytorch-2-9-cu129-ubuntu-2204-nvidia-580-v20260909",
    "pytorch-2-9-cu129-ubuntu-2204-nvidia-580-v20261001",
)
PAYLOAD = (
    "bootstrap.sh", "remote_lifecycle.py", "motion-adapter.pt", "ship_anwm_server.py",
    "ship_anwm.py", "ship_aerovla_server.py", "ship_aerovla.py",
    "yokohama_appearance.py", "yokohama_wam_profile.py",
)
VERIFIERS = ("decisions", "pad_queue", "pad_advisory", "payload", "sitl")
VERIFIER_SOURCES = tuple(f"scripts/verify_yokohama_{name}.py" for name in VERIFIERS)
RUNTIME_S = 3600
EVIDENCE_LIMIT = 32 * 1024**2
CREATE_SETTLE_S = 180
GROUP_SCOPE = "bounded_capacity_retry"


def now():
    return datetime.now(timezone.utc)


def digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def validate_plan(plan, root=REPO):
    if plan.get("schema") != SCHEMA:
        raise ValueError("Unknown trial plan")
    if not Path(plan.get("trial_directory", "")).is_absolute():
        raise ValueError("Plan must bind an absolute single-use trial directory")
    for name in ("project", "instance"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{5,60}", plan.get(name, "")):
            raise ValueError("Invalid resource " + name)
    if plan.get("zone") not in ZONES or plan.get("image") not in IMAGES:
        raise ValueError("Trial is limited to reviewed Oregon/image choices")
    group = plan.get("budget_scope") == GROUP_SCOPE
    reserve = 5 if group else 2
    if (
        plan.get("max_runtime_s") != RUNTIME_S or plan.get("max_attempts") != 1
        or plan.get("reserve_usd") != reserve or plan.get("aggregate_budget_usd") != reserve
        or plan.get("budget_scope") not in ("incremental_single_attempt", GROUP_SCOPE)
        or not re.fullmatch(r"[a-f0-9]{32}", plan.get("budget_id", ""))
        or plan.get("hard_network_byte_cap") is not False
        or plan.get("guaranteed_spend_cap") is not False
    ):
        raise ValueError("Unsupported trial budget or misleading egress claim")
    if group and (
        not re.fullmatch(r"[a-f0-9]{32}", plan.get("capacity_group_id", ""))
        or plan.get("group_controller_sha256") != digest_file(
            root / "scripts/yokohama_capacity_retry.py")
    ):
        raise ValueError("Capacity group controller changed or group identity missing")
    estimate = plan.get("cost_estimate", {}).get("usd_before_tax_fx")
    if type(estimate) not in (int, float) or not math.isfinite(estimate) or not 0 < estimate <= 2:
        raise ValueError("A finite positive estimate within the incremental USD 2 reserve is required")
    if plan.get("controller_sha256") != digest_file(root / "scripts/yokohama_native_trial.py"):
        raise ValueError("Controller changed since planning")
    local.image_id(plan.get("local_image_id"))
    if plan.get("local_preflight_sha256") != digest_file(root / "scripts/yokohama_local_preflight.py"):
        raise ValueError("Local preflight changed since planning")
    if plan.get("lifecycle_helper_sha256") != digest_file(root / "scripts/yokohama_cloud_lifecycle.py"):
        raise ValueError("Lifecycle helper changed since planning")
    if plan.get("source_sha256") != vehicle.input_hashes(root):
        raise ValueError("Vehicle sources changed since planning")
    if plan.get("verifier_sha256") != {name: digest_file(root / name) for name in VERIFIER_SOURCES}:
        raise ValueError("Verifier entrypoints changed since planning")
    payload = Path(plan["payload_directory"])
    if set(plan["payload_sha256"]) != set(PAYLOAD):
        raise ValueError("Payload must contain exactly the reviewed native bundle")
    for name in PAYLOAD:
        if digest_file(payload / name) != plan["payload_sha256"][name]:
            raise ValueError("Payload changed: " + name)
        if name.endswith(".py") and name != "remote_lifecycle.py":
            if digest_file(root / "scripts" / name) != plan["payload_sha256"][name]:
                raise ValueError("Remote model source differs from this worktree: " + name)
    for name, expected in plan["cpu_qualification_sha256"].items():
        path = Path(plan["cpu_run_directory"]) / name
        if digest_file(path) != expected or json.loads(path.read_text()).get("status") != "passed":
            raise ValueError("CPU qualification missing or changed")
    if set(plan["cpu_qualification_sha256"]) != {"result.json", *(
        f"verification-{name}.json" for name in (*VERIFIERS, "vehicle_service")
    )}:
        raise ValueError("CPU qualification must include every verifier")
    if json.loads((Path(plan["cpu_run_directory"]) / "result.json").read_text()).get("image_id") != plan["local_image_id"]:
        raise ValueError("Local image must match the CPU qualification")


def labels(plan):
    return {"managed-by": "missionos-native-trial", "missionos-task": "yokohama-service",
            "missionos-attempt": vehicle.proposal_digest(plan)[:16]}


def create_arguments(plan, deadline):
    return [
        "compute", "instances", "create", plan["instance"], "--project", plan["project"],
        "--zone", plan["zone"], "--quiet", "--machine-type", "g2-standard-16",
        "--image-project", "deeplearning-platform-release", "--image", plan["image"],
        "--boot-disk-size", "200GB", "--boot-disk-type", "pd-balanced",
        "--boot-disk-auto-delete", "--no-service-account", "--no-scopes",
        "--maintenance-policy", "TERMINATE", "--no-restart-on-failure",
        "--provisioning-model", "STANDARD", "--termination-time", deadline.isoformat(),
        "--instance-termination-action", "DELETE",
        "--metadata", "block-project-ssh-keys=TRUE",
        "--labels", ",".join(f"{k}={v}" for k, v in labels(plan).items()),
        "--async", "--format=json",
    ]


def validate_approval(plan, approval):
    if (
        approval.get("approved_plan_sha256") != vehicle.proposal_digest(plan)
        or not approval.get("reference") or not approval.get("actor")
        or not approval.get("approved_at")
        or approval.get("approve_ephemeral_cleanup") is not True
        or approval.get("acknowledge_no_hard_network_byte_cap") is not True
        or approval.get("acknowledge_estimate_not_spend_cap") is not True
    ):
        raise ValueError("Exact trial/cleanup approval and non-guaranteed-cost acknowledgment required")


class Trial:
    def __init__(self, plan, directory, *, group_gate=None):
        self.plan, self.directory = copy.deepcopy(plan), Path(directory).resolve()
        if self.directory != Path(plan["trial_directory"]).resolve():
            raise ValueError("Approval cannot be replayed in a different trial directory")
        self.attempt = self.directory / "attempt.json"
        self.common = ["--project", plan["project"], "--zone", plan["zone"], "--quiet"]
        self.instance_id = None
        self.disk_ids = {}
        self.remote_accessible = False
        self.payload_directory = Path(plan["payload_directory"])
        self.flight = self.tunnel = None
        self.create_submitted = False
        self.create_operation = None
        self.create_reserved_at = None
        self.group_gate = group_gate
        self.execution_approval = None
        self.local_environment = local.environment()
        self.local_preparation = None
        self.inventory_sequence = max((int(path.stem.rsplit("-", 1)[1])
            for path in self.directory.glob("inventory-history-*.json")
            if path.stem.rsplit("-", 1)[1].isdigit()), default=0)
        if self.attempt.exists():
            ledger = json.loads(self.attempt.read_text())
            if ledger.get("plan_sha256") != vehicle.proposal_digest(self.plan):
                raise ValueError("Attempt ledger belongs to another plan")
            if (ledger.get("budget_id") != self.plan["budget_id"]
                    or ledger.get("reserved_usd") != self.plan["reserve_usd"]):
                raise ValueError("Attempt ledger belongs to another budget reservation")
            self.create_reserved_at = datetime.fromisoformat(ledger["reserved_at"])
            # Legacy/uncertain ledgers cannot be interpreted as no submission.
            self.create_submitted = ledger.get("create_submitted", True)
            if type(self.create_submitted) is not bool:
                raise ValueError("Invalid create submission state")

    def validate_plan(self):
        validate_plan(self.plan)

    def validate_operator_approval(self, approval):
        validate_approval(self.plan, approval)

    def validate_execution_binding(self, approval):
        # Never turn the files presently on disk into new operator approval.
        self.validate_operator_approval(approval)
        self.validate_plan()
        if self.plan.get("budget_scope") == GROUP_SCOPE:
            if self.group_gate is None:
                raise ValueError("Capacity group execution requires the owning group controller")
            self.group_gate(self.plan)

    def operation_matches(self, operation):
        target = (f"/projects/{self.plan['project']}/zones/{self.plan['zone']}/instances/"
                  + self.plan["instance"])
        try:
            inserted = datetime.fromisoformat(operation["insertTime"].replace("Z", "+00:00"))
            return (operation.get("operationType") == "insert"
                    and re.fullmatch(r"[a-z][a-z0-9-]{1,200}", operation.get("name", "")) is not None
                    and operation.get("targetLink", "").endswith(target)
                    and operation.get("zone", "").endswith("/" + self.plan["zone"])
                    and self.create_reserved_at is not None and inserted >= self.create_reserved_at)
        except (KeyError, ValueError, TypeError):
            return False

    def settle_create(self, timeout=CREATE_SETTLE_S):
        """Require terminal insert evidence, even when create CLI timed out.

        An empty operation/resource list is not proof that a submitted request
        cannot materialize later. No resubmission occurs on this recovery path.
        """
        if not self.create_submitted:
            return None
        end = time.monotonic() + timeout
        while True:
            if self.create_operation is None:
                rows = self.query(["operations", "list", "--zones", self.plan["zone"],
                                   "--filter", "operationType=insert AND targetLink:" + self.plan["instance"]],
                                  "create-operation-discovery")
                matches = [row for row in rows if self.operation_matches(row)]
                if len(matches) > 1:
                    raise RuntimeError("Ambiguous create operations; cleanup cannot be confirmed")
                if matches:
                    self.create_operation = matches[0]["name"]
            if self.create_operation is not None:
                operation = self.query(["operations", "describe", self.create_operation,
                                        "--zone", self.plan["zone"]], "create-operation")
                if not self.operation_matches(operation):
                    raise RuntimeError("Create operation identity changed")
                write(self.directory / "create-operation.json", operation)
                if operation.get("status") == "DONE":
                    return operation
            if time.monotonic() >= end:
                raise TimeoutError("Create operation is unknown or nonterminal; absence is not final")
            time.sleep(min(2, max(0, end - time.monotonic())))

    def call(self, args, name, timeout=60, check=True):
        with (self.directory / (name + ".stdout")).open("w") as out, (
            self.directory / (name + ".stderr")
        ).open("w") as err:
            result = subprocess.run([self.plan["gcloud"], *args], stdout=out, stderr=err,
                                    timeout=timeout, stdin=subprocess.DEVNULL)
        if check and result.returncode:
            raise RuntimeError(name + " failed; see retained stderr")
        return result.returncode

    def query(self, args, name, project=None):
        self.call(["compute", *args, "--project", project or self.plan["project"], "--format=json"], name)
        return json.loads((self.directory / (name + ".stdout")).read_text())

    def resources(self):
        # List is intentionally used: an authorization/network failure is not
        # misclassified as a missing resource by swallowing describe errors.
        inventory = {}
        for kind in ("instances", "disks"):
            rows = self.query([kind, "list", "--filter", "name=" + self.plan["instance"]],
                              "inventory-" + kind)
            inventory[kind] = rows
            if self.plan.get("budget_scope") == GROUP_SCOPE:
                # Retain each successful read, including a VM observed before a
                # later disk-query failure. Absence after deletion is not proof
                # that no allocation happened.
                self.inventory_sequence += 1
                record = dict(checked_at=now().isoformat(), kind=kind, resources=rows)
                history = self.directory / f"inventory-history-{self.inventory_sequence:04d}.json"
                with history.open("x") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if rows and not (self.directory / "resource-observed.json").exists():
                    with (self.directory / "resource-observed.json").open("x") as stream:
                        json.dump(record, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
        return inventory

    def preflight(self):
        self.validate_plan()
        if self.attempt.exists():
            raise ValueError("The one paid attempt has already been consumed")
        key = Path(self.plan["ssh_key"])
        if not key.is_file() or not key.with_suffix(key.suffix + ".pub").is_file():
            raise ValueError("An existing SSH key is required; key generation is prohibited")
        if any(self.resources().values()):
            raise ValueError("Refusing an existing resource name")
        image = self.query(["images", "describe", self.plan["image"]], "image",
                           project="deeplearning-platform-release")
        if image.get("status") != "READY":
            raise ValueError("Selected official image is not ready")
        if image.get("deprecated", {}).get("state") in ("OBSOLETE", "DELETED"):
            raise ValueError("Selected image is no longer usable")
        region = self.query(["regions", "describe", "us-west1"], "quota")
        l4 = next(q for q in region["quotas"] if q["metric"] == "NVIDIA_L4_GPUS")
        if l4["limit"] - l4["usage"] < 1:
            raise ValueError("No L4 quota available; quota is not a capacity reservation")
        cpus = next(q for q in region["quotas"] if q["metric"] == "CPUS")
        project = self.query(["project-info", "describe"], "global-quota")
        gpu = next(q for q in project["quotas"] if q["metric"] == "GPUS_ALL_REGIONS")
        if cpus["limit"] - cpus["usage"] < 16 or gpu["limit"] - gpu["usage"] < 1:
            raise ValueError("Insufficient regional CPU or global GPU quota")
        import socket
        for port in (18117, 18118):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        return dict(checked_at=now().isoformat(), image=image["name"],
                    image_deprecation=image.get("deprecated"), quota=l4,
                    capacity_reserved=False, paid_resources_started=False)

    def qualify_local(self):
        from uuid import uuid4
        self.local_preparation = local.prepare_models(
            self.directory / ("local-preflight-" + uuid4().hex), self.plan["local_image_id"],
            env=self.local_environment)
        self.local_environment = dict(self.local_preparation.environment)

    def ssh_args(self):
        return ["compute", "ssh", self.plan["instance"], *self.common,
                "--ssh-key-file", self.plan["ssh_key"], "--ssh-key-expire-after", "2h",
                "--ssh-flag=-oUserKnownHostsFile=" + str(self.directory / "known_hosts")]

    def ssh(self, command, name, timeout=60, check=True):
        return self.call([*self.ssh_args(), "--command", command], name, timeout, check)

    def confirm_created(self, deadline):
        inventory = self.resources()
        rows = inventory["instances"]
        if len(rows) != 1:
            raise RuntimeError("Created instance not uniquely found")
        row = rows[0]
        if row.get("labels") != labels(self.plan) or not row["zone"].endswith("/" + self.plan["zone"]):
            raise RuntimeError("Created instance ownership mismatch")
        scheduling = row.get("scheduling", {})
        actual = datetime.fromisoformat(scheduling.get("terminationTime", "").replace("Z", "+00:00"))
        if (actual > deadline or scheduling.get("instanceTerminationAction") != "DELETE"
                or scheduling.get("automaticRestart") is not False
                or len(row.get("disks", [])) != 1
                or not all(d.get("autoDelete") is True for d in row.get("disks", []))):
            raise RuntimeError("Cloud-side deadline/auto-delete was not installed")
        self.instance_id = row["id"]
        self.remember_boot_disks(row, inventory["disks"])
        write(self.directory / "created.json", row)

    def remember_boot_disks(self, instance, disks):
        sources = {d["source"] for d in instance.get("disks", []) if d.get("boot")
                   and d.get("autoDelete") is True}
        for disk in disks:
            if (disk.get("selfLink") in sources and disk.get("users") == [instance.get("selfLink")]
                    and disk["zone"].endswith("/" + self.plan["zone"])):
                self.disk_ids[disk["name"]] = disk["id"]
        write(self.directory / "owned-disk-identities.json", self.disk_ids)

    def cleanup(self):
        write(self.directory / "cleanup.json", dict(
            checked_at=now().isoformat(), cleanup_confirmed=False,
            create_operation_terminal=False, instance_and_boot_disk_absent=False))
        try:
            operation = self.settle_create()
        except Exception as exc:
            write(self.directory / "cleanup.json", dict(
                checked_at=now().isoformat(), instance_and_boot_disk_absent=False,
                create_operation_terminal=False, cleanup_confirmed=False,
                reason=type(exc).__name__ + ": " + str(exc)))
            raise
        inventory = self.resources()
        for row in inventory["instances"]:
            if (row.get("labels") != labels(self.plan)
                    or not row["zone"].endswith("/" + self.plan["zone"])
                    or (self.instance_id is not None and row["id"] != self.instance_id)):
                raise RuntimeError("Refusing deletion: owned instance identity differs")
            # Handles an uncertain create outcome only for the fresh, unique
            # name plus exact attempt labels. Never adopts an existing VM.
            self.remember_boot_disks(row, inventory["disks"])
            self.call(["compute", "instances", "delete", self.plan["instance"], *self.common],
                      "delete-owned-instance", timeout=120)
        remaining = self.resources()
        for disk in remaining["disks"]:
            # Auto-delete is primary. Only an independently recorded numeric
            # boot-disk identity may be removed if it remains unattached.
            if (self.disk_ids.get(disk["name"]) == disk.get("id") and not disk.get("users")
                    and disk["zone"].endswith("/" + self.plan["zone"])):
                self.call(["compute", "disks", "delete", disk["name"], *self.common],
                          "delete-owned-boot-disk", timeout=120, check=False)
        remaining = self.resources()
        receipt = dict(checked_at=now().isoformat(), instance_and_boot_disk_absent=not any(
            remaining.values()), resources=remaining,
            create_operation_terminal=operation is not None and operation.get("status") == "DONE",
            create_was_submitted=self.create_submitted, cleanup_confirmed=not any(remaining.values()))
        write(self.directory / "cleanup.json", receipt)
        if not receipt["instance_and_boot_disk_absent"]:
            raise RuntimeError("Owned cleanup not confirmed; do not assume stopped disk is free")

    @staticmethod
    def stop_child(child, grace):
        if child is None:
            return
        try:
            if child.poll() is None:
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # A reaped/session-leader exit does not imply descendant exit.
            # Every handle passed here was started with start_new_session=True.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                child.wait(timeout=10)

    def collect(self):
        # Only reviewed text/inference evidence, never credentials or weights.
        code = f"""import hashlib,json,tarfile
from pathlib import Path
r=Path.home()/'yokohama-native'
files=[p for p in r.iterdir() if p.is_file() and not p.is_symlink() and
       (p.suffix in {{'.json','.txt','.log','.csv','.stdout','.stderr'}} or p.name=='ready')]
for name in ['vla-evidence','wam-evidence']:
 d=r/name
 if d.exists():files.extend(p for p in d.rglob('*') if p.is_file() and not p.is_symlink())
assert sum(p.stat().st_size for p in files)<{EVIDENCE_LIMIT}, 'Evidence cap exceeded'
with tarfile.open(r/'evidence.tar.gz','w:gz') as t:
 for p in sorted(set(files)):t.add(p,arcname=str(p.relative_to(r)),recursive=False)
assert (r/'evidence.tar.gz').stat().st_size<={EVIDENCE_LIMIT}, 'Compressed evidence cap exceeded'
print(hashlib.sha256((r/'evidence.tar.gz').read_bytes()).hexdigest())
"""
        self.ssh("python3 -c " + shlex.quote(code), "collect", timeout=120)
        # Bound the data at the sender too, so a growing/replaced archive cannot
        # turn a metadata check into an unbounded scp. SSH overhead is not capped.
        transfer = ("from pathlib import Path;import sys;"
                    "p=Path.home()/'yokohama-native/evidence.tar.gz';"
                    f"sys.stdout.buffer.write(p.open('rb').read({EVIDENCE_LIMIT}))")
        with (self.directory / "remote-evidence.tar.gz").open("wb") as out, (
            self.directory / "download.stderr"
        ).open("wb") as err:
            subprocess.run([self.plan["gcloud"], *self.ssh_args(), "--command",
                            "python3 -c " + shlex.quote(transfer)], stdout=out, stderr=err,
                           check=True, timeout=120, stdin=subprocess.DEVNULL)
        if digest_file(self.directory / "remote-evidence.tar.gz") != (
            self.directory / "collect.stdout"
        ).read_text().splitlines()[-1]:
            raise ValueError("Remote evidence digest mismatch")

    def execute(self, approval):
        self.validate_operator_approval(approval)
        self.execution_approval = copy.deepcopy(approval)
        if self.plan.get("budget_scope") == GROUP_SCOPE:
            self.validate_execution_binding(approval)
        self.qualify_local()
        write(self.directory / "preflight.json", self.preflight())
        # Capture the bytes approved by the plan before any billable action.
        # Upload never rereads the caller's mutable payload directory.
        snapshot = self.directory / "payload"
        snapshot.mkdir()
        for name in PAYLOAD:
            raw = (self.payload_directory / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.plan["payload_sha256"][name]:
                raise ValueError("Payload changed during admission: " + name)
            (snapshot / name).write_bytes(raw)
            (snapshot / name).chmod(0o400)
        self.payload_directory = snapshot
        self.create_reserved_at = now()
        # GCP represents terminationTime in milliseconds. Round down before
        # binding the ledger/request so provider rounding cannot extend it.
        deadline = now() + timedelta(seconds=RUNTIME_S)
        deadline = deadline.replace(microsecond=deadline.microsecond // 1000 * 1000)
        with self.attempt.open("x") as stream:
            json.dump(dict(plan_sha256=vehicle.proposal_digest(self.plan),
                           budget_id=self.plan["budget_id"], reserved_usd=self.plan["reserve_usd"],
                           reservation_is_shared=self.plan.get("budget_scope") == GROUP_SCOPE,
                           reserved_at=self.create_reserved_at.isoformat(), deadline=deadline.isoformat(),
                           create_submitted=False), stream)
            stream.flush()
            os.fsync(stream.fileno())
        errors = []
        flight_evidence = None
        prior_term = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt("Controller terminated")

        signal.signal(signal.SIGTERM, terminate)
        try:
            self.validate_execution_binding(approval)
            self.local_preparation.assert_fresh()
            ledger = json.loads(self.attempt.read_text())
            ledger["create_submitted"] = True
            write(self.attempt, ledger)
            try:
                self.local_preparation.assert_age()
            except BaseException:
                # The CLI has not been called. Retain that known state if the
                # reservation flush itself consumed the freshness allowance.
                ledger["create_submitted"] = False
                write(self.attempt, ledger)
                raise
            self.create_submitted = True
            self.call(create_arguments(self.plan, deadline), "create", timeout=180)
            # Async output is an Operation, not an instance. A lost response is
            # reconciled by the target/zone/time-bound read-only operation lookup.
            responses = json.loads((self.directory / "create.stdout").read_text())
            operations = responses if isinstance(responses, list) else [responses]
            if len(operations) != 1 or not self.operation_matches(operations[0]):
                raise RuntimeError("Create returned an unbound operation")
            self.create_operation = operations[0]["name"]
            operation = self.settle_create()
            if operation.get("error"):
                raise RuntimeError("Create operation failed; no paid retry")
            self.confirm_created(deadline)
            self.validate_execution_binding(approval)
            self.bootstrap()
            flight_evidence = self.fly(approval, deadline)
        except BaseException as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
        finally:
            # Failures in diagnostics never skip owned resource cleanup.
            operations = [lambda: self.stop_child(self.flight, 180)]
            if self.remote_accessible:
                operations += [lambda: self.ssh(
                    'python3 "$HOME/yokohama-native/remote_lifecycle.py" stop',
                    "final-model-stop", timeout=60), self.collect]
            operations += [lambda: self.stop_child(self.tunnel, 5), self.cleanup]
            for operation in operations:
                try:
                    operation()
                except BaseException as exc:
                    errors.append(type(exc).__name__ + ": " + str(exc))
            signal.signal(signal.SIGTERM, prior_term)
            # All CPU-only verifiers run after cloud cleanup, not on GPU time.
            if flight_evidence is not None:
                try:
                    self.verify_flight(*flight_evidence)
                except Exception as exc:
                    errors.append(type(exc).__name__ + ": " + str(exc))
            write(self.directory / "controller-result.json", dict(
                status="failed" if errors else "passed", errors=errors,
                finished_at=now().isoformat(), cloud_deadline=deadline.isoformat(),
                budget_id=self.plan["budget_id"], reserved_usd=self.plan["reserve_usd"],
                reservation_is_shared=self.plan.get("budget_scope") == GROUP_SCOPE,
                invoice_confirmed=False, hard_network_byte_cap=False,
                guaranteed_spend_cap=False,
            ))
        return 1 if errors else 0

    def prepare_network_budget(self):
        """Dedicated controllers may constrain their own newly created VM."""

    def bootstrap(self):
        for attempt in range(8):
            if self.ssh('mkdir -p "$HOME/yokohama-native"', f"ssh-ready-{attempt}",
                        timeout=35, check=False) == 0:
                self.remote_accessible = True
                break
            time.sleep(5)
        else:
            raise TimeoutError("SSH readiness failed; no paid retry")
        self.prepare_network_budget()
        self.call(["compute", "scp", *[str(self.payload_directory / p) for p in PAYLOAD],
                   self.plan["instance"] + ":~/yokohama-native/", *self.common,
                   "--ssh-key-file", self.plan["ssh_key"],
                   "--scp-flag=-oUserKnownHostsFile=" + str(self.directory / "known_hosts")],
                  "upload", timeout=120)
        self.ssh('cd "$HOME/yokohama-native" && sha256sum *.py *.sh *.pt', "payload-hashes")
        observed = {line.split()[1]: line.split()[0] for line in (
            self.directory / "payload-hashes.stdout").read_text().splitlines()}
        if observed != self.plan["payload_sha256"]:
            raise ValueError("Uploaded payload differs from approval")
        self.validate_execution_binding(self.execution_approval)
        code = ("import subprocess; from pathlib import Path; r=Path.home()/'yokohama-native'; "
                "subprocess.Popen(['bash','bootstrap.sh'],cwd=r,stdin=subprocess.DEVNULL,"
                "stdout=(r/'bootstrap.stdout').open('w'),stderr=(r/'bootstrap.stderr').open('w'),"
                "start_new_session=True)")
        self.ssh("python3 -c " + shlex.quote(code), "bootstrap-start")
        end = time.monotonic() + 600
        while time.monotonic() < end:
            self.ssh('python3 -c ' + shlex.quote(
                "import json;from pathlib import Path;r=Path.home()/'yokohama-native';"
                "print(json.dumps({'ready':(r/'ready').exists(),'failed':(r/'bootstrap.failed').exists()}))"
            ), "bootstrap-status", timeout=30)
            status = json.loads((self.directory / "bootstrap-status.stdout").read_text().splitlines()[-1])
            if status["failed"]:
                raise RuntimeError("Bootstrap failed; no paid retry")
            if status["ready"]:
                return
            time.sleep(10)
        raise TimeoutError("Bootstrap exceeded 10-minute readiness allowance")

    def fly(self, approval, deadline):
        self.validate_execution_binding(approval)
        self.qualify_local()
        self.local_preparation.assert_fresh()
        if (deadline - now()).total_seconds() < 2400:
            raise TimeoutError("Insufficient time for flight and cleanup reserve")
        resource = dict(schema="missionos.yokohama-cloud-resource.v1", **{
            key: self.plan[key] for key in ("project", "zone", "instance", "gcloud")}, readiness_wait_s=0)
        resource.update(ssh_key_file=self.plan["ssh_key"],
                        known_hosts_file=str(self.directory / "known_hosts"))
        write(self.directory / "resource.json", resource)
        service = dict(vla_port=18117, wam_port=18118)
        for op in ("start", "stop"):
            service[op + "_argv"] = [self.plan["python"], str(REPO / "scripts/yokohama_cloud_lifecycle.py"),
                                      "--resource-json", str(self.directory / "resource.json"), op]
        service_path = self.directory / "native-service.json"
        service_bytes = (json.dumps(service, indent=2, allow_nan=False) + "\n").encode()
        write(service_path, service)
        job = self.directory / "flight"
        job.mkdir()
        proposal = dict(
            schema_version="yokohama_chat_proposal.v1", proposal_id=self.plan["instance"],
            execution_target=vehicle.TARGET, route={"route_id": vehicle.ROUTE_ID},
            destination_id="yokohama_harbour_pad", physical_execution_invoked=False,
            city_models="native", pad_queue={"mission_judge": dict(
                may_only_add_wait=True, max_decisions=2, max_added_wait_s=30)},
            simulator_arguments=vehicle.arguments("native", service_path, "fixture",
                                                 local_image_id=self.plan["local_image_id"]),
            input_sha256={**self.plan["source_sha256"],
                          "native_service_config": hashlib.sha256(service_bytes).hexdigest()},
        )
        execution_approval = dict(approved_proposal_sha256=vehicle.proposal_digest(proposal),
                                 operator_approval_ref=approval["reference"],
                                 actor_session_id=approval["actor"], approved_at=approval["approved_at"])
        write(job / "approved.json", dict(proposal=proposal, approval=execution_approval))
        self.validate_execution_binding(approval)
        with (self.directory / "tunnel.log").open("w") as log:
            self.local_preparation.assert_age()
            self.tunnel = subprocess.Popen([self.plan["gcloud"], *self.ssh_args(), "--", "-N",
                "-L", "127.0.0.1:18117:127.0.0.1:18117", "-L", "127.0.0.1:18118:127.0.0.1:18118",
                "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3"], stdout=log, stderr=log, start_new_session=True)
        env = dict(self.local_environment)
        with (job / "service.log").open("w") as log:
            self.local_preparation.assert_age()
            self.flight = subprocess.Popen([self.plan["python"],
                str(REPO / "scripts/run_yokohama_vehicle_service.py"), "--approve-sitl",
                "--approval-manifest", str(job / "approved.json"), "--output-dir", str(job / "run")],
                cwd=REPO, env=env, stdout=log, stderr=log, start_new_session=True)
        while self.flight.poll() is None:
            if self.tunnel.poll() is not None or (deadline - now()).total_seconds() < 300:
                raise TimeoutError("Tunnel exited or cloud cleanup reserve reached")
            time.sleep(1)
        if self.flight.returncode != 0:
            raise RuntimeError("Vehicle execution failed")
        return job, proposal, execution_approval

    def verify_flight(self, job, proposal, execution_approval):
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL"}}
        for name in VERIFIERS:
            self.validate_plan()
            subprocess.run([self.plan["python"], str(REPO / f"scripts/verify_yokohama_{name}.py"),
                            str(job / "run"), "--output", str(job / "run" / f"verification-{name}.json")],
                           check=True, timeout=180, env=env, stdout=subprocess.DEVNULL)
        self.validate_plan()
        receipt = vehicle.verify_receipt(job / "run", proposal, execution_approval)
        write(job / "run/verification-vehicle_service.json", receipt)
        if receipt["status"] != "passed":
            raise RuntimeError("Vehicle service receipt verification failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--operator-approval", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate_plan(plan)
    trial = Trial(plan, args.plan.parent)
    if args.execute:
        if args.operator_approval is None:
            parser.error("--execute requires --operator-approval")
        return trial.execute(json.loads(args.operator_approval.read_text()))
    if args.preflight:
        trial.qualify_local()
        result = trial.preflight()
        write(args.plan.parent / "preflight.json", result)
    else:
        result = dict(plan_sha256=vehicle.proposal_digest(plan), paid_resources_started=False,
                      maximum_runtime_s=RUNTIME_S, budget_id=plan["budget_id"],
                      reserve_usd=plan["reserve_usd"], hard_network_byte_cap=False,
                      guaranteed_spend_cap=False)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
