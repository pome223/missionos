#!/usr/bin/env python3
"""Explicitly approved, single-use capacity retry group; offline by default."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import yokohama_native_trial as single  # noqa: E402

SCHEMA = "missionos.yokohama-capacity-retry.v1"
MAX_ATTEMPTS = 6
MIN_INTERVAL_S = 600
CAPACITY_ERRORS = {"ZONE_RESOURCE_POOL_EXHAUSTED", "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"}
digest = single.vehicle.proposal_digest


def read(path):
    return json.loads(Path(path).read_text())


def durable_write(path, value, *, exclusive=False):
    path = Path(path)
    temporary = path if exclusive else path.with_suffix(".tmp")
    with temporary.open("x" if exclusive else "w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_plan(plan):
    root = Path(plan.get("group_directory", ""))
    if (plan.get("schema") != SCHEMA or not root.is_absolute()
            or plan.get("max_attempts") != MAX_ATTEMPTS
            or plan.get("min_interval_s") != MIN_INTERVAL_S
            or plan.get("aggregate_budget_usd") != 5
            or plan.get("max_flights") != 1
            or plan.get("stop_after_resource_success") is not True
            or plan.get("abort_on_unknown") is not True
            or plan.get("guaranteed_spend_cap") is not False
            or plan.get("hard_network_byte_cap") is not False
            or not re.fullmatch(r"[a-f0-9]{32}", plan.get("group_id", ""))
            or not re.fullmatch(r"[a-f0-9]{32}", plan.get("budget_id", ""))):
        raise ValueError("Unsupported capacity group envelope")
    children = plan.get("children", [])
    if len(children) != MAX_ATTEMPTS:
        raise ValueError("Exactly six bound child plans are required")
    names = set()
    shared = None
    for index, child in enumerate(children, 1):
        single.validate_plan(child)
        if (child.get("budget_scope") != single.GROUP_SCOPE
                or child.get("budget_id") != plan["budget_id"]
                or child.get("capacity_group_id") != plan["group_id"]
                or Path(child["trial_directory"]) != root / f"attempt-{index:02d}"
                or child["instance"] in names):
            raise ValueError("Child plan identity or shared budget mismatch")
        names.add(child["instance"])
        common = {k: v for k, v in child.items() if k not in {"instance", "zone", "trial_directory"}}
        if shared is not None and common != shared:
            raise ValueError("Children must use the same reviewed configuration")
        shared = common


def retry_eligible(trial, returncode):
    """Read-only confirmation; ANY ambiguity ends the entire group."""
    directory = trial.directory
    cleanup = read(directory / "cleanup.json")
    result = read(directory / "controller-result.json")
    ledger = read(trial.attempt)
    if (returncode != 1 or result.get("status") != "failed"
            or result.get("errors") != ["RuntimeError: Create operation failed; no paid retry"]
            or ledger.get("create_submitted") is not True
            or ledger.get("plan_sha256") != digest(trial.plan)
            or ledger.get("budget_id") != trial.plan["budget_id"]
            or result.get("budget_id") != trial.plan["budget_id"]
            or any(cleanup.get(k) is not True for k in (
                "cleanup_confirmed", "create_operation_terminal", "instance_and_boot_disk_absent",
                "create_was_submitted"))
            or cleanup.get("resources") != {"instances": [], "disks": []}
            or trial.instance_id is not None or trial.disk_ids or trial.remote_accessible):
        return False
    markers = ("resource-observed.json", "created.json", "owned-disk-identities.json",
               "bootstrap-start.stdout", "native-service.json", "flight")
    if any((directory / name).exists() for name in markers):
        return False
    retained = read(directory / "create-operation.json")
    operation = trial.settle_create(timeout=single.CREATE_SETTLE_S)
    if (operation != retained or not trial.operation_matches(operation)
            or operation.get("status") != "DONE"):
        return False
    errors = operation.get("error", {}).get("errors", [])
    if not errors or any(row.get("code") not in CAPACITY_ERRORS for row in errors):
        return False
    # Query failures propagate, never become empty inventory. The Trial retains
    # a sticky observation marker before any cleanup can erase an allocation.
    inventory = trial.resources()
    receipt = dict(checked_at=single.now().isoformat(), operation=operation,
                   resources=inventory, retry_eligible=inventory == {"instances": [], "disks": []})
    durable_write(directory / "capacity-retry-confirmation.json", receipt, exclusive=True)
    return receipt["retry_eligible"] and not (directory / "resource-observed.json").exists()


class Group:
    def __init__(self, plan_path, approval_path):
        self.plan_path, self.approval_path = Path(plan_path), Path(approval_path)
        self.plan, self.approval = read(plan_path), read(approval_path)
        self.directory = Path(self.plan["group_directory"])
        self.ledger_path = self.directory / "group-attempt.json"
        self.state = None
        self.active_child = None

    def validate_binding(self):
        if read(self.plan_path) != self.plan or read(self.approval_path) != self.approval:
            raise ValueError("Original group plan or approval changed")
        if self.state is not None and read(self.ledger_path) != self.state:
            raise ValueError("Exclusive group reservation changed")
        single.validate_approval(self.plan, self.approval)
        validate_plan(self.plan)

    def gate(self, child):
        self.validate_binding()
        if (self.state is None or read(self.ledger_path) != self.state
                or self.state["status"] != "running"
                or self.active_child is None
                or child != self.plan["children"][self.active_child]
                or self.state["current_attempt"] != self.active_child + 1):
            raise ValueError("Child lacks an active exclusive group reservation")

    def wait_interval(self, completed_at):
        end = completed_at + MIN_INTERVAL_S
        while time.monotonic() < end:
            time.sleep(min(30, max(0, end - time.monotonic())))

    def execute(self):
        self.validate_binding()
        self.state = dict(schema=SCHEMA, plan_sha256=digest(self.plan), budget_id=self.plan["budget_id"],
                          reserved_usd=5, started_at=single.now().isoformat(), status="running",
                          current_attempt=None, completed=[], reservation_is_shared=True,
                          invoice_confirmed=False, guaranteed_spend_cap=False)
        # A consumed group is never resumed, copied to another directory or reset.
        durable_write(self.ledger_path, self.state, exclusive=True)
        prior_term = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt("Capacity group terminated")

        signal.signal(signal.SIGTERM, terminate)
        last_completion = None
        try:
            for index, child in enumerate(self.plan["children"]):
                if last_completion is not None:
                    self.wait_interval(last_completion)
                self.validate_binding()
                self.active_child = index
                self.state["current_attempt"] = index + 1
                durable_write(self.ledger_path, self.state)
                directory = Path(child["trial_directory"])
                directory.mkdir()  # refuses any preexisting/consumed attempt
                child_approval = copy.deepcopy(self.approval)
                child_approval.update(approved_plan_sha256=digest(child),
                                      derived_from_group_plan_sha256=digest(self.plan),
                                      group_attempt=index + 1)
                for name, value in (("plan.json", child), ("operator-approval.json", child_approval)):
                    durable_write(directory / name, value, exclusive=True)
                    (directory / name).chmod(0o400)
                trial = single.Trial(child, directory, group_gate=self.gate)
                print(json.dumps(dict(event="attempt_started", attempt=index + 1,
                                      zone=child["zone"], at=single.now().isoformat())), flush=True)
                result = trial.execute(child_approval)
                eligible = retry_eligible(trial, result)
                self.state["completed"].append(dict(attempt=index + 1, returncode=result,
                    retry_eligible=eligible, finished_at=single.now().isoformat(),
                    child_plan_sha256=digest(child)))
                last_completion = time.monotonic()
                durable_write(self.ledger_path, self.state)
                print(json.dumps(dict(event="attempt_finished", **self.state["completed"][-1])), flush=True)
                if not eligible:
                    self.state["status"] = "passed" if result == 0 else "stopped_nonretryable"
                    break
            else:
                self.state["status"] = "capacity_exhausted"
        except BaseException as exc:
            self.state.update(status="aborted", error=type(exc).__name__ + ": " + str(exc))
        finally:
            self.active_child = None
            self.state["finished_at"] = single.now().isoformat()
            durable_write(self.ledger_path, self.state)
            signal.signal(signal.SIGTERM, prior_term)
        return 0 if self.state["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--operator-approval", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute and args.operator_approval is None:
        parser.error("--execute requires --operator-approval")
    plan = read(args.plan)
    validate_plan(plan)
    if not args.execute:
        print(json.dumps(dict(plan_sha256=digest(plan), paid_resources_started=False,
                              aggregate_budget_usd=5, max_attempts=6, min_interval_s=600)))
        return 0
    return Group(args.plan, args.operator_approval).execute()


if __name__ == "__main__":
    raise SystemExit(main())
