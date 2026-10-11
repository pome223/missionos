"""Explicit opt-in guest entrypoint; default only describes an approved local payload.

No cloud API, credentials, WAM or vehicle access. The existing pinned bootstrap
and native runner must be provided locally by a separately authorized controller.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

if __package__:
    from .yokohama_offline_vla_lifecycle import DurableEvents, supervise_guest, write_json
else:
    from yokohama_offline_vla_lifecycle import DurableEvents, supervise_guest, write_json


RX_LIMIT = 31 * 1024**3
TX_LIMIT = 896 * 1024**2
REQUIRED_SOURCES = {
    "bootstrap-vla.sh",
    *(
        "sources/" + name
        for name in (
            "ship_aerovla.py",
            "ship_aerovla_server.py",
            "ship_anwm.py",
            "yokohama_offline_vla_pair.py",
            "yokohama_offline_vla_remote.py",
            "yokohama_offline_vla_lifecycle.py",
            "yokohama_offline_vla_guest.py",
        )
    ),
}


def counters():
    totals = {"rx_bytes": 0, "tx_bytes": 0}
    for interface in Path("/sys/class/net").iterdir():
        if interface.name == "lo":
            continue
        for key in totals:
            totals[key] += int((interface / "statistics" / key).read_text())
    return totals


def packet_quota_arguments(initial):
    if any(type(initial.get(k)) is not int or initial[k] < 0 for k in ("rx_bytes", "tx_bytes")):
        raise ValueError("Unqualified network counters")
    if initial["rx_bytes"] >= RX_LIMIT or initial["tx_bytes"] >= TX_LIMIT:
        raise ValueError("Initial network traffic exceeds conservative threshold")
    commands = []
    for family in ("iptables", "ip6tables"):
        for direction, key, limit in (("RX", "rx_bytes", RX_LIMIT), ("TX", "tx_bytes", TX_LIMIT)):
            chain = "YOKOHAMA_" + direction
            commands.append([family, "-w", "-N", chain])
            if family == "iptables":
                commands.append(
                    [
                        family,
                        "-w",
                        "-A",
                        chain,
                        "-m",
                        "quota",
                        "--quota",
                        str(limit - initial[key]),
                        "-j",
                        "RETURN",
                    ]
                )
            commands.append([family, "-w", "-A", chain, "-j", "DROP"])
        commands.append([family, "-w", "-I", "INPUT", "1", "!", "-i", "lo", "-j", "YOKOHAMA_RX"])
        commands.append([family, "-w", "-I", "OUTPUT", "1", "!", "-o", "lo", "-j", "YOKOHAMA_TX"])
    return commands


def run_guest(workdir, control, *, execute=False):
    workdir = Path(workdir).resolve()
    if not execute:
        return {
            "execution": False,
            "cloud_API_calls": 0,
            "model_calls": 0,
            "logging": "durable_files_and_guest_agent_journal",
            "inline_serial_archive_required": False,
        }
    if not control.get("approved_execution_reference"):
        raise ValueError("Separate explicit guest execution approval required")
    begin = control["submission_unix_s"]
    if not math.isfinite(begin) or (
        control["preparation_deadline_unix_s"] != begin + 330
        or control["comparison_deadline_unix_s"] != begin + 375
    ):
        raise ValueError("Unexpected reviewed stage limits")
    if set(control["source_sha256"]) != REQUIRED_SOURCES:
        raise ValueError("Complete reviewed native source closure required")
    expected_prepare = ["bash", str(workdir / "bootstrap-vla.sh")]
    expected_infer = [
        str(workdir / "vla-venv/bin/python"),
        str(workdir / "sources/yokohama_offline_vla_remote.py"),
        "--base",
        str(workdir / "openvla-7b"),
        "--adapter",
        str(workdir / "aerovla/aero_vla"),
        "--inputs",
        str(workdir / "inputs"),
        "--output",
        str(workdir / "evidence"),
        "--preparation-deadline",
        str(control["preparation_deadline_unix_s"]),
        "--comparison-deadline",
        str(control["comparison_deadline_unix_s"]),
    ]
    if control["prepare_argv"] != expected_prepare or control["infer_argv"] != expected_infer:
        raise ValueError("Unexpected preparation or inference invocation")
    if time.time() >= control["preparation_deadline_unix_s"]:
        raise TimeoutError("Preparation deadline already expired")
    for name, sha in control["source_sha256"].items():
        path = workdir / name
        if not path.resolve().is_relative_to(workdir) or path.is_symlink():
            raise ValueError("Unexpected guest source path")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise ValueError("Guest source differs from approved payload")
    events = DurableEvents(workdir / "evidence", display=lambda text: print(text, flush=True))
    # Exclusive creation survives repeated metadata startup invocations and clock changes.
    with (events.output / "attempt-reserved.json").open("x") as receipt:
        receipt.write(json.dumps({"submission_unix_s": begin, "one_guest_attempt": True}))
        receipt.flush()
        os.fsync(receipt.fileno())
    try:
        initial = counters()
        for argv in packet_quota_arguments(initial):
            subprocess.run(argv, check=True, capture_output=True)
        events.emit("packet_quotas_installed", initial_counters=initial)
    except Exception as exc:
        events.failed(exc)
        events.archive(workdir / "remote-evidence.tar.gz")
        return False

    def healthy():
        current = counters()
        write_json(events.output / "network.json", current)
        return current["rx_bytes"] < RX_LIMIT and current["tx_bytes"] < TX_LIMIT

    try:
        return supervise_guest(
            control["prepare_argv"],
            control["infer_argv"],
            events=events,
            prepare_deadline=control["preparation_deadline_unix_s"],
            infer_deadline=control["comparison_deadline_unix_s"],
            model_ready_path=events.output / "ready.json",
            archive_path=workdir / "remote-evidence.tar.gz",
            health=healthy,
        )
    finally:
        # Archive already exists even if progress sinks have failed; preserve final counters too.
        write_json(events.output / "network-at-guest-exit.json", counters())
        events.archive(workdir / "remote-evidence.tar.gz")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--control", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    value = json.loads(args.control.read_text()) if args.control else {}
    result = run_guest(args.workdir, value, execute=args.execute)
    print(json.dumps(result, indent=2))
    if args.execute and result is False:
        raise SystemExit(1)
