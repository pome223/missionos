#!/usr/bin/env python3
"""Run the city-only model lifecycle against one explicit resource manifest.

This helper provisions nothing. Its caller must separately gate city entry and
own the VM. --describe is read-only and makes no cloud request.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


def command(resource, operation):
    if resource.get("schema") != "missionos.yokohama-cloud-resource.v1":
        raise ValueError("Unsupported resource manifest")
    for key in ("instance", "project", "zone"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", resource.get(key, "")):
            raise ValueError("Invalid cloud resource " + key)
    if operation not in ("start", "stop"):
        raise ValueError("Invalid lifecycle operation")
    wait = resource.get("readiness_wait_s", 0)
    if type(wait) is not int or not 0 <= wait <= 120:
        raise ValueError("Readiness wait must be bounded at 120 seconds")
    code = "\n".join(
        [
            "from pathlib import Path",
            "import os,sys,time",
            "root=Path.home()/'yokohama-native'",
            f"deadline=time.monotonic()+{wait}",
            "while not (root/'ready').exists():" if operation == "start" else "while False:",
            " if (root/'bootstrap.failed').exists():raise RuntimeError('Bootstrap failed')",
            " if time.monotonic()>=deadline:raise TimeoutError('Bootstrap not ready')",
            " time.sleep(1)",
            f"os.execv(sys.executable,[sys.executable,str(root/'remote_lifecycle.py'),'{operation}'])",
        ]
    )
    argv = [
        resource.get("gcloud", "gcloud"),
        "compute",
        "ssh",
        resource["instance"],
        "--project",
        resource["project"],
        "--zone",
        resource["zone"],
        "--quiet",
    ]
    # An explicitly approved ephemeral trial may reuse an existing SSH key
    # and keep its host-key cache inside the trial, without changing defaults.
    if resource.get("ssh_key_file") or resource.get("known_hosts_file"):
        for name in ("ssh_key_file", "known_hosts_file"):
            if not isinstance(resource.get(name), str) or not Path(resource[name]).is_absolute():
                raise ValueError("Explicit SSH paths must be absolute")
        if not Path(resource["ssh_key_file"]).is_file():
            raise ValueError("Existing SSH key required; do not generate a key")
        argv += ["--ssh-key-file", resource["ssh_key_file"], "--ssh-key-expire-after", "2h",
                 "--ssh-flag=-oUserKnownHostsFile=" + resource["known_hosts_file"]]
    return [*argv, "--command", "python3 -c " + shlex.quote(code)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-json", type=Path, required=True)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("operation", choices=["start", "stop"])
    args = parser.parse_args()
    data = args.resource_json.read_bytes()
    resource = json.loads(data)
    argv = command(resource, args.operation)
    if args.describe:
        print(json.dumps(dict(argv=argv, resource_sha256=hashlib.sha256(data).hexdigest())))
        return
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=295 if args.operation == "start" else 55
    )
    if result.returncode:
        raise RuntimeError("Remote lifecycle failed: " + result.stderr[-2000:])
    receipt = json.loads(result.stdout.splitlines()[-1])
    receipt["resource_sha256"] = hashlib.sha256(data).hexdigest()
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
