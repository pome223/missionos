from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from src import quickstart_smoke


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_quickstart_smoke_module_preserves_machine_readable_cli(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        quickstart_smoke,
        "run_quickstart_smoke",
        lambda **_: {
            "task_id": "task_fixture_quickstart",
            "task_url": "http://127.0.0.1:18789/tasks/task_fixture_quickstart",
            "timeline_url": (
                "http://127.0.0.1:18789/tasks/task_fixture_quickstart/timeline"
            ),
        },
    )

    assert quickstart_smoke.main(["--json"]) == 0

    assert json.loads(capsys.readouterr().out)["task_id"] == (
        "task_fixture_quickstart"
    )


def test_shell_entrypoints_no_longer_depend_on_legacy_src_main() -> None:
    bridge = (REPO_ROOT / "scripts/bridge_runtime.sh").read_text()
    quickstart = (REPO_ROOT / "scripts/quickstart.sh").read_text()

    assert "-m src.main" not in bridge
    assert "-m src.main" not in quickstart
    assert "-m src.mcp_servers.host_bridge_server --sse" in bridge
    assert "-m src.mcp_servers.desktop_bridge_server --sse" in bridge
    assert "-m src.quickstart_smoke" in quickstart


def test_replacement_process_entrypoints_expose_cli_help() -> None:
    modules = (
        "src.quickstart_smoke",
        "src.mcp_servers.host_bridge_server",
        "src.mcp_servers.desktop_bridge_server",
    )
    for module in modules:
        completed = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "usage:" in completed.stdout.lower()
