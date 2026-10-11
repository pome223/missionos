"""Local lifecycle safety helpers; CLI describes a plan and never calls a cloud API.

Cloud transports are supplied explicitly by an independently authorized caller.
No VM creation, credentials, model loading, schedule or retry loop is provided.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tarfile
import time
import traceback
from uuid import uuid4


def write_json(path, value):
    path = Path(path)
    data = json.dumps(value, indent=2, allow_nan=False) + "\n"
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    with temporary.open("w") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class DurableEvents:
    """File evidence is mandatory; stdout and journald are independent best efforts."""

    def __init__(self, output, *, display=None, journal=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.display = display
        self.journal = journal

    def _append(self, filename, value):
        with (self.output / filename).open("a") as stream:
            stream.write(json.dumps(value, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def emit(self, stage, **details):
        value = {"stage": stage, "unix_s": time.time(), **details}
        self._append("events.jsonl", value)
        write_json(self.output / "status.json", value)
        for name, sink in (("display", self.display), ("journal", self.journal)):
            if sink is not None:
                try:
                    sink(json.dumps(value, allow_nan=False))
                except Exception as exc:
                    self._append("log-channel-errors.jsonl", {"channel": name, "error": str(exc)})
        return value

    def failed(self, exc):
        # Persist the failure independently before any optional output channel.
        write_json(self.output / "abort.json", {"type": type(exc).__name__, "reason": str(exc)})
        (self.output / "supervisor-error.log").write_text(traceback.format_exc())
        return self.emit("failed", error_type=type(exc).__name__, reason=str(exc))

    def archive(self, destination):
        """Evidence retrieval uses a file, never an inline serial/base64 dependency."""
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.output.resolve()):
            raise ValueError("Archive must be outside evidence directory")
        members = sorted(p for p in self.output.iterdir() if p.is_file() and not p.is_symlink())
        with tarfile.open(destination, "w:gz") as archive:
            for path in members:
                archive.add(path, arcname=path.name, recursive=False)
        with destination.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        write_json(
            destination.with_suffix(destination.suffix + ".receipt.json"),
            {
                "sha256": sha,
                "bytes": destination.stat().st_size,
                "members": [p.name for p in members],
            },
        )
        return sha


def run_child(
    argv,
    *,
    output,
    deadline_unix_s,
    health=lambda: True,
    preparation_deadline=None,
    ready_path=None,
):
    """Run one child process group; monotonic time prevents a clock jump extending it."""
    start_wall, start_mono = time.time(), time.monotonic()
    remaining = deadline_unix_s - start_wall
    if ready_path is not None and preparation_deadline is None:
        raise ValueError("Model preparation deadline required with readiness receipt")
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Child deadline already reached")
    stop_at = start_mono + remaining
    prepare_stop = (
        start_mono + preparation_deadline - start_wall
        if preparation_deadline is not None
        else stop_at
    )
    if not math.isfinite(prepare_stop) or prepare_stop > stop_at:
        raise ValueError("Finite preparation deadline within child deadline required")
    ready_stop = None

    def read_ready_stop():
        ready = json.loads(Path(ready_path).read_text())["ready_unix_s"]
        if not math.isfinite(ready) or not start_wall <= ready <= preparation_deadline:
            raise TimeoutError("Model readiness exceeded preparation deadline")
        return min(stop_at, start_mono + ready + 45 - start_wall)

    with Path(output).open("wb") as log:
        child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while child.poll() is None:
                active_stop = prepare_stop
                if ready_stop is not None:
                    active_stop = ready_stop
                elif ready_path is not None and Path(ready_path).exists():
                    ready_stop = read_ready_stop()
                    active_stop = ready_stop
                if time.monotonic() >= min(stop_at, active_stop):
                    raise TimeoutError("Child stage deadline reached")
                if not health():
                    raise RuntimeError("Guest health/network limit rejected")
                time.sleep(0.1)
            if child.returncode:
                raise RuntimeError(f"Child failed with status {child.returncode}")
            if ready_path is not None and ready_stop is None:
                if not Path(ready_path).exists():
                    raise RuntimeError("Child exited before verified model readiness")
                ready_stop = read_ready_stop()
            if time.monotonic() >= (ready_stop if ready_stop is not None else stop_at):
                raise TimeoutError("Child completion exceeded stage deadline")
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if child.poll() is None:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=1)


def supervise_guest(
    prepare,
    infer,
    *,
    events,
    prepare_deadline,
    infer_deadline,
    archive_path,
    health=lambda: True,
    runner=run_child,
    model_ready_path=None,
):
    """One preparation and one bounded model-runner invocation; never retry either."""
    successful = False
    try:
        events.emit("preparing")
        runner(
            prepare,
            output=events.output / "bootstrap.log",
            deadline_unix_s=prepare_deadline,
            health=health,
        )
        events.emit("weights_and_dependencies_ready")
        if time.time() >= prepare_deadline:
            raise TimeoutError("Preparation deadline reached")
        events.emit("inference_starting")
        phase = (
            {"preparation_deadline": prepare_deadline, "ready_path": model_ready_path}
            if model_ready_path is not None
            else {}
        )
        runner(
            infer,
            output=events.output / "native-run.log",
            deadline_unix_s=infer_deadline,
            health=health,
            **phase,
        )
        events.emit("completed")
        successful = True
    except Exception as exc:
        events.failed(exc)
    finally:
        events.archive(archive_path)
    return successful


@dataclass(frozen=True)
class OwnedVM:
    project: str
    zone: str
    name: str
    instance_id: str
    disk_name: str
    disk_id: str
    trial_id: str

    def __post_init__(self):
        for value in (self.project, self.name, self.disk_name):
            if not re.fullmatch(r"[a-z][a-z0-9-]{5,62}", value):
                raise ValueError("Invalid resource name")
        if self.zone not in {"us-west1-a", "us-west1-b", "us-west1-c"}:
            raise ValueError("Unreviewed region")
        if not self.instance_id.isdigit() or not self.disk_id.isdigit():
            raise ValueError("Immutable resource IDs required")
        if not re.fullmatch(r"[a-f0-9]{12}", self.trial_id):
            raise ValueError("Trial label required")

    def verify(self, instance, disk):
        prefix = f"/projects/{self.project}/zones/{self.zone}"
        if instance is not None and disk is None:
            raise ValueError("Boot disk identity has not been observed")
        if instance is not None and (
            instance.get("id") != self.instance_id
            or instance.get("name") != self.name
            or not instance.get("selfLink", "").endswith(prefix + "/instances/" + self.name)
            or instance.get("labels", {}).get("trial-id") != self.trial_id
            or instance.get("labels", {}).get("managed-by") != "yokohama-offline-vla"
            or len(instance.get("disks", [])) != 1
            or not instance["disks"][0].get("boot")
            or not instance["disks"][0].get("autoDelete")
            or not instance["disks"][0]
            .get("source", "")
            .endswith(prefix + "/disks/" + self.disk_name)
        ):
            raise ValueError("Instance ownership mismatch")
        if disk is not None and (
            disk.get("id") != self.disk_id
            or disk.get("name") != self.disk_name
            or not disk.get("selfLink", "").endswith(prefix + "/disks/" + self.disk_name)
            or disk.get("users", []) not in ([], [instance["selfLink"]] if instance else [])
        ):
            raise ValueError("Disk ownership mismatch")


def sdk_delete_arguments(vm):
    """The installed stable SDK supports boot/all/data and does not support --async."""
    return [
        "compute",
        "instances",
        "delete",
        vm.name,
        "--project=" + vm.project,
        "--zone=" + vm.zone,
        "--delete-disks=boot",
        "--quiet",
        "--format=json",
    ]


def sdk_delete_disk_arguments(vm):
    return [
        "compute",
        "disks",
        "delete",
        vm.disk_name,
        "--project=" + vm.project,
        "--zone=" + vm.zone,
        "--quiet",
        "--format=json",
    ]


def watchdog_delete_request(vm, *, orphan_disk=False):
    # Independent REST transport: never calls the SDK or its argument builder.
    url = f"https://compute.googleapis.com/compute/v1/projects/{vm.project}/zones/{vm.zone}"
    suffix = "/disks/" + vm.disk_name if orphan_disk else "/instances/" + vm.name
    return {"method": "DELETE", "url": url + suffix, "body": None}


def startup_failed(serial_text):
    return bool(
        re.search(
            r'Script "startup-script" failed with error: exit status [1-9][0-9]*', serial_text
        )
    )


def startup_script(guest_argv):
    """Guest agent owns stdout/journald; never reopen its controlling TTY."""
    if not guest_argv or guest_argv[0] != "/usr/bin/python3":
        raise ValueError("Reviewed guest Python entrypoint required")
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\numask 077\nexec " + shlex.join(guest_argv) + "\n"
    )


class CleanupController:
    """Early SDK cleanup plus separately timed REST watchdog; no creation capability.

    A caller must run watchdog_tick in an independent process with its own reads.
    A cleanup deadline failure remains a failure even when later deletion succeeds.
    """

    def __init__(
        self,
        vm,
        *,
        events,
        observe,
        watchdog_observe,
        sdk_delete,
        rest_delete,
        cleanup_at,
        watchdog_at,
        absence_deadline,
    ):
        if not cleanup_at <= watchdog_at < absence_deadline:
            raise ValueError("Cleanup must precede watchdog and absence deadline")
        self.vm, self.events, self.observe = vm, events, observe
        self.watchdog_observe = watchdog_observe
        self.sdk_delete, self.rest_delete = sdk_delete, rest_delete
        self.cleanup_at, self.watchdog_at = cleanup_at, watchdog_at
        self.absence_deadline = absence_deadline
        self.primary_started = self.watchdog_started = self.deadline_failed = False

    def _read(self):
        instance, disk, insert_terminal = self.observe()
        self.vm.verify(instance, disk)
        return instance, disk, insert_terminal

    def _log(self, stage, **details):
        # A full evidence disk or broken logger must not disable resource deletion.
        try:
            self.events.emit(stage, **details)
        except Exception:
            pass

    def tick(self, now, *, guest_status=None, serial_text="", comparison_finished=False):
        instance, disk, terminal = self._read()
        if now >= self.absence_deadline and (
            instance is not None or disk is not None or not terminal
        ):
            self.deadline_failed = True
            self._log("absence_deadline_failed")
        if instance is None and disk is None and terminal:
            self._log("resources_absent", deadline_failed=self.deadline_failed)
            return "absent"
        if instance is None and disk is None:
            return "pending"  # never issue an unbound delete for a pending insert
        failed = (guest_status or {}).get("stage") == "failed" or startup_failed(serial_text)
        if not self.primary_started and (failed or comparison_finished or now >= self.cleanup_at):
            self.primary_started = True
            self._log("cleanup_starting", startup_failed=failed)
            try:
                arguments = (
                    sdk_delete_arguments(self.vm)
                    if instance is not None
                    else sdk_delete_disk_arguments(self.vm)
                )
                self.sdk_delete(self.vm, arguments)
            except Exception as exc:
                self._log("primary_delete_failed", reason=str(exc))
        return "pending"

    def watchdog_tick(self, now):
        """Call in a separate watchdog process; never inherit primary completion state."""
        if now < self.watchdog_at:
            return
        instance, disk, terminal = self.watchdog_observe()
        self.vm.verify(instance, disk)
        if now >= self.absence_deadline and (
            instance is not None or disk is not None or not terminal
        ):
            self.deadline_failed = True
            self._log("watchdog_absence_deadline_failed")
        if instance is None and disk is None and terminal:
            self._log("watchdog_resources_absent", deadline_failed=self.deadline_failed)
            return "absent"
        if instance is None and disk is None:
            return "pending"
        if not self.watchdog_started:
            self.watchdog_started = True
            self._log("independent_watchdog_delete_starting")
            self.rest_delete(
                self.vm, watchdog_delete_request(self.vm, orphan_disk=instance is None)
            )
        return "pending"


def budget_plan(*, spent_usd, hourly_usd, egress_usd_per_gib):
    values = (spent_usd, hourly_usd, egress_usd_per_gib)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Finite, nonnegative preserved-ledger prices required")
    if hourly_usd <= 0 or spent_usd > 1:
        raise ValueError("Budget exhausted or price unqualified")
    maximum = hourly_usd * 600 / 3600 + egress_usd_per_gib
    remaining = 1 - spent_usd
    return {
        "preparation_s": 330,
        "comparison_s": 45,
        "collection_delete_s": 225,
        "early_cleanup_after_submission_s": 375,
        "independent_watchdog_s": 385,
        "cloud_DELETE_start_after_submission_s": 390,
        "absence_deadline_s": 600,
        "remaining_group_budget_USD": remaining,
        "next_resource_estimate_USD": maximum,
        "cost_fits_remaining_budget": maximum <= remaining,
        "strict_600_s_deletion_guaranteed": False,
        "current_turn_paid_execution_authorized": False,
        "prior_stop_marker_must_not_be_reset": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spent-usd", type=float, required=True)
    parser.add_argument("--hourly-usd", type=float, required=True)
    parser.add_argument("--egress-usd-per-gib", type=float, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            budget_plan(
                spent_usd=args.spent_usd,
                hourly_usd=args.hourly_usd,
                egress_usd_per_gib=args.egress_usd_per_gib,
            ),
            indent=2,
        )
    )
