"""Explicitly supplied cloud transports for a separately approved offline trial.

No credentials are discovered and no resource is created by this module.
The watchdog receives a short-lived token in memory from its authorized caller.
Its REST reads/deletes do not execute the SDK or share the primary observer.
"""

import datetime
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .yokohama_offline_vla_lifecycle import OwnedVM, write_json
else:
    from yokohama_offline_vla_lifecycle import OwnedVM, write_json


PROFILE = {
    "maximum_elapsed_s": 900,
    "preparation_s": 420,
    "comparison_end_s": 465,
    "primary_delete_s": 485,
    "watchdog_s": 510,
    "cloud_DELETE_s": 540,
    "maximum_ingress_bytes": 32 * 1024**3,
    "maximum_egress_bytes": 512 * 1024**2,
    "kernel_RX_threshold": 31 * 1024**3,
    "kernel_TX_threshold": 448 * 1024**2,
}


def quote_900(*, prior_group_usd, prior_cumulative_usd, hourly_usd, egress_usd_per_gib):
    import math

    values = (prior_group_usd, prior_cumulative_usd, hourly_usd, egress_usd_per_gib)
    if any(not math.isfinite(v) or v < 0 for v in values) or prior_group_usd > 1:
        raise ValueError("Preserved finite nonnegative budget and current prices required")
    if hourly_usd <= 0 or prior_cumulative_usd < prior_group_usd:
        raise ValueError("Incomplete price or cumulative ledger")
    subtotal = hourly_usd / 4 + egress_usd_per_gib / 2
    # Explicit contingency, not a claimed tax rate or a fresh budget allocation.
    reserve = subtotal * 1.2
    remaining = 1 - prior_group_usd
    return {
        "resource_estimate_USD": subtotal,
        "tax_fx_contingency_fraction": 0.2,
        "new_estimate_with_contingency_USD": reserve,
        "prior_group_spending_USD": prior_group_usd,
        "remaining_group_USD": remaining,
        "projected_group_with_contingency_USD": prior_group_usd + reserve,
        "prior_cumulative_spending_USD": prior_cumulative_usd,
        "projected_cumulative_with_contingency_USD": prior_cumulative_usd + reserve,
        "fits_existing_budget": reserve <= remaining and prior_cumulative_usd + reserve <= 8,
        "group_budget_USD": 1,
        "aggregate_budget_USD": 8,
        "aggregate_reserved_USD": 7,
        "new_budget_allocation_USD": 0,
        "provider_invoice_confirmed": False,
        "provider_hard_cap": False,
        "strict_deletion_completion_guaranteed": False,
    }


def bind_owner(config, instance, disk):
    """Bind immutable IDs only to the fresh, exact approved nonce and boot disk."""
    if instance is None or disk is None:
        raise ValueError("Both resource identities must be observed before binding")
    created = datetime.datetime.fromisoformat(instance["creationTimestamp"]).timestamp()
    begin = config["submission_unix_s"]
    scheduling = instance.get("scheduling", {})
    termination = datetime.datetime.fromisoformat(scheduling["terminationTime"]).timestamp()
    if not (
        begin - 1 <= created < begin + PROFILE["maximum_elapsed_s"]
        and termination <= begin + PROFILE["cloud_DELETE_s"] + 0.1
        and scheduling.get("instanceTerminationAction") == "DELETE"
        and instance.get("machineType", "").endswith("/machineTypes/g2-standard-16")
        and disk.get("type", "").endswith("/diskTypes/pd-balanced")
        and int(disk["sizeGb"]) == 200
    ):
        raise ValueError("Unreviewed resource configuration or creation/deletion bounds")
    vm = OwnedVM(
        config["project"],
        config["zone"],
        config["instance"],
        str(instance["id"]),
        config["instance"],
        str(disk["id"]),
        config["trial_id"],
    )
    vm.verify(instance, disk)
    return vm


def insertion_terminal(rows, config):
    suffix = f"/zones/{config['zone']}/instances/{config['instance']}"
    matches = [
        row
        for row in rows
        if row.get("operationType") == "insert" and row.get("targetLink", "").endswith(suffix)
    ]
    if len(matches) > 1:
        raise ValueError("Multiple insert operations for one reserved request")
    return bool(matches and matches[0].get("status") == "DONE")


class RestTransport:
    """No SDK/auth discovery; bearer token stays only in this process's memory."""

    def __init__(self, token, *, opener=urllib.request.urlopen, timeout=8):
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Caller-supplied in-memory credential required")
        self._token, self.opener, self.timeout = token, opener, timeout

    def request(self, method, url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "compute.googleapis.com":
            raise ValueError("Unreviewed REST endpoint")
        if method not in ("GET", "DELETE") or not parsed.path.startswith("/compute/v1/projects/"):
            raise ValueError("Watchdog has only approved resource read/delete capability")
        request = urllib.request.Request(
            url, method=method, headers={"Authorization": "Bearer " + self._token}
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # Never include request headers, token, credential source or response body.
            raise RuntimeError(f"Compute REST {method} failed with HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("Compute REST transport unavailable") from None

    def observe(self, config):
        base = f"https://compute.googleapis.com/compute/v1/projects/{config['project']}/zones/{config['zone']}"
        instance = self.request("GET", base + "/instances/" + config["instance"])
        disk = self.request("GET", base + "/disks/" + config["instance"])
        target = (
            base.replace("compute.googleapis.com", "www.googleapis.com")
            + "/instances/"
            + config["instance"]
        )
        query = urllib.parse.urlencode(
            {"filter": '(operationType = insert) AND (targetLink = "' + target + '")'}
        )
        result = self.request("GET", base + "/operations?" + query) or {}
        if result.get("nextPageToken"):
            raise ValueError("Unexpected paginated nonce-scoped operation result")
        return instance, disk, insertion_terminal(result.get("items", []), config)

    def delete(self, vm, request):
        prefix = f"https://compute.googleapis.com/compute/v1/projects/{vm.project}/zones/{vm.zone}"
        if (
            request["method"] != "DELETE"
            or request.get("body") is not None
            or request["url"]
            not in (
                prefix + "/instances/" + vm.name,
                prefix + "/disks/" + vm.disk_name,
            )
        ):
            raise ValueError("Delete outside bound resource")
        return self.request("DELETE", request["url"])


class SDKTransport:
    """Bounded primary reads/deletes with private file receipts; never prints auth."""

    def __init__(self, executable, output, config, *, runner=subprocess.run):
        self.executable, self.output, self.config, self.runner = (
            executable,
            Path(output),
            config,
            runner,
        )
        self.output.mkdir(parents=True, exist_ok=True)
        self.sequence = 0

    def command(self, args, *, timeout=15):
        self.sequence += 1
        tag = f"sdk-{self.sequence:04d}"
        try:
            proc = self.runner(
                [self.executable, *args], capture_output=True, text=True, timeout=timeout
            )
            (self.output / (tag + ".stdout")).write_text(proc.stdout)
            (self.output / (tag + ".stderr")).write_text(proc.stderr)
            write_json(self.output / (tag + ".json"), {"args": args, "returncode": proc.returncode})
            if proc.returncode:
                raise RuntimeError("SDK command failed; private receipt " + tag)
            return json.loads(proc.stdout) if proc.stdout.strip() else None
        except subprocess.TimeoutExpired:
            write_json(self.output / (tag + ".json"), {"args": args, "timeout_s": timeout})
            raise TimeoutError("SDK command timeout; reconcile original request") from None

    def observe(self):
        c = self.config
        shared = ["--project=" + c["project"], "--zones=" + c["zone"], "--quiet", "--format=json"]
        suffix = ["--filter=name=" + c["instance"], *shared]
        instances = self.command(["compute", "instances", "list", *suffix])
        disks = self.command(["compute", "disks", "list", *suffix])
        operations = self.command(
            ["compute", "operations", "list", "--filter=targetLink:" + c["instance"], *shared]
        )
        if len(instances) > 1 or len(disks) > 1:
            raise ValueError("Ambiguous nonce-scoped resource identities")
        return (
            instances[0] if instances else None,
            disks[0] if disks else None,
            insertion_terminal(operations, c),
        )

    def delete(self, vm, arguments):
        if arguments[:3] not in (
            ["compute", "instances", "delete"],
            ["compute", "disks", "delete"],
        ):
            raise ValueError("Not a deletion operation")
        if "--async" in arguments or arguments[3] not in (vm.name, vm.disk_name):
            raise ValueError("Unexpected bound delete arguments")
        return self.command(arguments, timeout=20)
