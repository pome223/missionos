import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.yokohama_cloud_lifecycle import command


def resource():
    return dict(
        schema="missionos.yokohama-cloud-resource.v1",
        instance="missionos-current",
        project="example-project",
        zone="us-west1-a",
        readiness_wait_s=120,
    )


def test_target_is_shared_manifest_and_stop_does_not_wait():
    value = resource()
    assert command(value, "start")[3] == "missionos-current"
    value["instance"] = "missionos-replacement"
    assert command(value, "start")[3] == command(value, "stop")[3] == "missionos-replacement"
    assert "while False:" in command(value, "stop")[-1]


@pytest.mark.parametrize(
    "key,value",
    [
        ("instance", "old; command"),
        ("schema", "old"),
        ("readiness_wait_s", 121),
        ("readiness_wait_s", True),
    ],
)
def test_rejects_unbound_or_unbounded_resource(key, value):
    spec = resource()
    spec[key] = value
    with pytest.raises(ValueError):
        command(spec, "start")


def test_actual_cli_passes_selected_resource_to_transport(tmp_path):
    transport = tmp_path / "gcloud-stub"
    transport.write_text(
        "#!"
        + sys.executable
        + '\nimport json,sys\nfrom pathlib import Path\nPath(__file__).with_suffix(".argv").write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps({"remote_model_processes_absent":True}))\n'
    )
    transport.chmod(0o755)
    spec = resource()
    spec["gcloud"] = str(transport)
    path = tmp_path / "resource.json"
    path.write_text(json.dumps(spec))
    script = Path(__file__).resolve().parents[1] / "scripts/yokohama_cloud_lifecycle.py"
    for operation in ["start", "stop"]:
        result = subprocess.run(
            [sys.executable, str(script), "--resource-json", str(path), operation],
            capture_output=True,
            text=True,
            check=True,
        )
        receipt = json.loads(result.stdout)
        argv = json.loads(transport.with_suffix(".argv").read_text())
        assert argv[:3] == ["compute", "ssh", "missionos-current"]
        assert argv[argv.index("--project") + 1] == "example-project"
        assert argv[argv.index("--zone") + 1] == "us-west1-a"
        assert operation in argv[-1] and len(receipt["resource_sha256"]) == 64
