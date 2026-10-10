"""Real owned Docker cleanup probe; no flight, network, GPU or model API."""

import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from scripts import yokohama_sitl as runner


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("RUN_MISSIONOS_DOCKER_CLEANUP_PROBE") != "1",
                    reason="Explicit local Docker opt-in required")
def test_diagnostic_failure_removes_owned_container_and_reaps_worker(tmp_path, monkeypatch):
    def docker(*args, check=True):
        return subprocess.run(["docker", *args], check=check, capture_output=True,
                              text=True, timeout=30)

    # Inspect first: this probe never pulls or builds an image.
    image = docker("image", "inspect", "px4io/px4-sitl-gazebo:latest",
                   "--format", "{{.Id}}").stdout.strip()
    identity = uuid4().hex[:12]
    owned, control = ("missionos-cleanup-" + identity + suffix for suffix in ("-owned", "-control"))
    created = []
    worker = None
    try:
        for name in (owned, control):
            docker("run", "-d", "--name", name, "--network", "none", "--cpus", "0.1",
                   "--memory", "64m", "--label", "missionos-cleanup-probe=" + identity,
                   "--entrypoint", "sh", image, "-c", "sleep 300")
            created.append(name)
        worker = subprocess.Popen(["docker", "exec", owned, "sh", "-c", "sleep 300"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        original = runner.command

        def diagnostic_failure(args, **kwargs):
            if args[1] in ("logs", "exec"):
                raise OSError("deliberate diagnostic failure in cleanup probe")
            return original(args, **kwargs)

        monkeypatch.setattr(runner, "command", diagnostic_failure)
        result = {"status": "failed", "reason": "controlled local cleanup probe"}
        try:
            runner.capture_diagnostics(tmp_path, result, owned)
        finally:
            runner.cleanup_owned(tmp_path, result, created=True, container=owned, worker=worker)
        assert result["cleanup"] is True and result["worker_reaped"] is True
        assert worker.poll() is not None
        assert docker("inspect", owned, check=False).returncode != 0
        assert docker("inspect", control, "--format", "{{.State.Running}}").stdout.strip() == "true"
        destination = os.environ.get("MISSIONOS_DOCKER_CLEANUP_PROBE_RECEIPT")
        if destination:
            Path(destination).write_text(json.dumps(dict(
                status="passed", image_id=image, owned_container_absent=True,
                worker_reaped=True, control_container_survived=True,
                physical_execution_invoked=False, gpu_requested=False,
            ), indent=2) + "\n")
    finally:
        for name in created:
            docker("rm", "-f", name, check=False)
        if worker is not None:
            worker.wait(timeout=10)
