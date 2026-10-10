#!/usr/bin/env python3
"""Owned, bounded local Docker preparation; no GPU, network or image pulls."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

API_TIMEOUT_S = 10
START_TIMEOUT_S = 30
FRESH_S = 30
ENV_KEYS = ("PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST",
            "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
MODEL_FILES = ("models/worlds/default.sdf", "models/x500/model.config", "models/x500/model.sdf",
               "models/x500_base/model.config", "models/x500_base/model.sdf")


def environment():
    return {key: os.environ[key] for key in ENV_KEYS if key in os.environ}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return dict(path=str(path), device=stat.st_dev, inode=stat.st_ino)


def image_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("An immutable local simulator image ID is required")
    return value


class Preparation:
    def __init__(self, root, image, *, env=None):
        self.root = Path(root).resolve()
        self.image = image_id(image)
        self.environment = dict(environment() if env is None else env)
        self.nonce = uuid4().hex
        self.name = "missionos-local-preflight-" + self.nonce
        self.container_id = None
        self.create_resolved = False
        self.completed_monotonic = None
        self.binding = None
        self.endpoint = None
        self.receipt = dict(status="not_started", owner_nonce=self.nonce, container_name=self.name,
                            image_id=self.image, gpu_requested=False, network="none",
                            create_submitted=False, cleanup_confirmed=False)

    def command(self, args, timeout=API_TIMEOUT_S):
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, check=True, env=self.environment,
                              stdin=subprocess.DEVNULL)

    def runtime_binding(self):
        if self.endpoint is None:
            if self.environment.get("DOCKER_HOST") and not self.environment.get("DOCKER_CONTEXT"):
                self.endpoint = self.environment["DOCKER_HOST"]
            else:
                context = self.command(["context", "show"]).stdout.strip()
                self.endpoint = json.loads(self.command([
                    "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"
                ]).stdout)
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("unix://"):
            raise ValueError("Local preflight requires a local Unix Docker endpoint")
        # Capturing a context name is insufficient: its definition/default can
        # change. Pin the resolved endpoint before every operational CLI call.
        self.environment.pop("DOCKER_CONTEXT", None)
        self.environment["DOCKER_HOST"] = self.endpoint
        daemon = json.loads(self.command(["info", "--format",
            '{"id":{{json .ID}},"cpus":{{.NCPU}},"memory":{{.MemTotal}}}']).stdout)
        if not daemon.get("id") or daemon["cpus"] < 6 or daemon["memory"] < 6 * 1024**3:
            raise ValueError("Local simulator needs a known daemon, 6 CPU and 6 GiB")
        observed = self.command(["image", "inspect", self.image, "--format", "{{.Id}}"]).stdout.strip()
        if observed != self.image:
            raise ValueError("Approved local simulator image is unavailable")
        return dict(daemon=daemon, endpoint_sha256=hashlib.sha256(self.endpoint.encode()).hexdigest(),
                    environment_sha256=hashlib.sha256(json.dumps(self.environment, sort_keys=True).encode()).hexdigest(),
                    parent=identity(self.root.parent), root=identity(self.root), image_id=observed)

    def save(self):
        temporary = self.root / "local-preparation.tmp"
        with temporary.open("w") as stream:
            json.dump(self.receipt, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.root / "local-preparation.json")

    def candidates(self):
        rows = self.command(["ps", "-aq", "--no-trunc", "--filter", "name=^/" + self.name + "$"]).stdout.split()
        if len(rows) > 1 or any(not re.fullmatch(r"[a-f0-9]{64}", row) for row in rows):
            raise RuntimeError("Ambiguous local preparation ownership")
        return rows

    def cleanup(self):
        if self.command(["info", "--format", "{{.ID}}"]).stdout.strip() != self.binding["daemon"]["id"]:
            raise RuntimeError("Docker daemon changed; local cleanup cannot be confirmed")
        rows = self.candidates()
        for container in rows:
            fields = json.loads(self.command(["inspect", "--format",
                '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
                '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}', container]).stdout)
            mounts = fields["mounts"]
            if (fields["name"] != "/" + self.name or fields["image"] != self.image
                    or fields["labels"].get("missionos-local-preflight") != self.nonce
                    or (self.container_id is not None and container != self.container_id)
                    or len(mounts) != 1 or mounts[0].get("Source") != str(self.root)
                    or mounts[0].get("Destination") != "/mission"):
                raise RuntimeError("Refusing cleanup of unbound local container")
            self.create_resolved = True
            self.container_id = container
            self.command(["rm", "--force", container], timeout=20)
        remaining = self.candidates()
        # An empty list after a lost create response cannot prove that the
        # daemon will not materialize that request later.
        self.receipt.update(create_resolved=self.create_resolved,
                            cleanup_confirmed=self.create_resolved and not remaining,
                            remaining_container_ids=remaining)
        if not self.receipt["cleanup_confirmed"]:
            raise RuntimeError("Local create/cleanup remains unknown; cloud admission forbidden")

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / "local-preparation.json").exists():
            raise ValueError("Local preparation receipt cannot be replayed")
        with (self.root / "local-preparation-owner.json").open("x") as stream:
            json.dump(self.receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.receipt.update(status="running")
        self.save()
        error = None
        try:
            self.binding = self.runtime_binding()
            if self.candidates():
                raise ValueError("Local preparation name already exists")
            (self.root / "local-preparation-input.txt").write_text(self.nonce)
            self.receipt["binding"] = self.binding
            shell = (
                "set -eu; test \"$(cat /mission/local-preparation-input.txt)\" = " + self.nonce + "; "
                "command -v gz >/dev/null; command -v python3 >/dev/null; "
                "mkdir -p /mission/models/worlds; "
                "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /mission/models/worlds/; "
                "for model in x500 x500_base; do mkdir -p /mission/models/$model; "
                "cp /opt/px4-gazebo/share/gz/models/$model/model.* /mission/models/$model/; "
                "if [ -d /opt/px4-gazebo/share/gz/models/$model/meshes ]; then "
                "ln -s /opt/px4-gazebo/share/gz/models/$model/meshes /mission/models/$model/meshes; fi; done; "
                "cd /mission; sha256sum " + " ".join(MODEL_FILES) + " > local-preparation-sha256.txt; "
                "printf " + self.nonce + " > local-preparation-output.txt"
            )
            self.receipt["create_submitted"] = True
            self.save()  # retain ownership before the potentially ambiguous call
            created = self.command([
                "create", "--name", self.name, "--label", "missionos-local-preflight=" + self.nonce,
                "--network", "none", "--pull", "never", "--cpus", "1", "--memory", "512m",
                "--entrypoint", "sh", "--mount", "type=bind,source=" + str(self.root) + ",target=/mission",
                self.image, "-c", shell])
            self.container_id = created.stdout.strip()
            if not re.fullmatch(r"[a-f0-9]{64}", self.container_id):
                self.container_id = None
                raise RuntimeError("Unbound local create response")
            self.create_resolved = True
            self.receipt["container_id"] = self.container_id
            self.save()
            self.command(["start", "--attach", self.container_id], timeout=START_TIMEOUT_S)
            if (self.root / "local-preparation-output.txt").read_text() != self.nonce:
                raise ValueError("Local container/host read-write handshake failed")
            observed = dict(line.split(maxsplit=1)[::-1] for line in (
                self.root / "local-preparation-sha256.txt").read_text().splitlines())
            observed = {key.strip(): value for key, value in observed.items()}
            expected = {name: sha(self.root / name) for name in MODEL_FILES}
            if observed != expected or any(not (self.root / name).stat().st_size for name in MODEL_FILES):
                raise ValueError("Local model/world copy missing or mismatched")
            self.receipt["model_sha256"] = expected
        except BaseException as exc:
            error = exc
            self.receipt["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            try:
                if self.receipt["create_submitted"]:
                    self.cleanup()
                else:
                    self.receipt.update(create_resolved=True, cleanup_confirmed=True)
            except BaseException as exc:
                self.receipt["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
                error = error or exc
            self.receipt["status"] = "failed" if error else "passed"
            self.save()
        if error:
            raise error
        self.completed_monotonic = time.monotonic()
        return self

    def assert_fresh(self):
        if self.completed_monotonic is None or self.receipt["status"] != "passed":
            raise ValueError("No live successful local prerequisite check")
        current = self.runtime_binding()
        if (current != self.binding
                or self.receipt.get("cleanup_confirmed") is not True
                or any(sha(self.root / name) != digest for name, digest in self.receipt["model_sha256"].items())):
            raise ValueError("Local prerequisite check is stale or its binding changed")
        self.assert_age()  # include time spent on the binding and file reads

    def assert_age(self):
        if self.completed_monotonic is None or self.receipt["status"] != "passed":
            raise ValueError("No live successful local prerequisite check")
        age = time.monotonic() - self.completed_monotonic
        if not math.isfinite(age) or not 0 <= age <= FRESH_S:
            raise ValueError("Local prerequisite check is stale")


def prepare_models(root, image, *, env=None):
    return Preparation(root, image, env=env).run()
