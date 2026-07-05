from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV,
    UnitreeBoundedLocalMove,
    UnitreeHardwareAdapter,
    UnitreeHardwareAdapterConfig,
)
from src.runtime.unitree_mujoco_dispatch_bridge import (
    UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV,
    UNITREE_MUJOCO_BRIDGE_COMMAND_ENV,
    UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV,
    UnitreeMujocoBridgeCommandClient,
    UnitreeMujocoBridgeError,
)


def _write_bridge(
    path: Path,
    *,
    physical_claim: bool = False,
    raw_lowcmd_claim: bool = False,
) -> None:
    physical = "True" if physical_claim else "False"
    raw_lowcmd = "True" if raw_lowcmd_claim else "False"
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('physical_execution_invoked') is True:\n"
        "    raise SystemExit(3)\n"
        "if request.get('raw_motor_allowed') is not False:\n"
        "    raise SystemExit(4)\n"
        "if request.get('raw_velocity_allowed') is not False:\n"
        "    raise SystemExit(5)\n"
        "if request.get('special_motion_allowed') is not False:\n"
        "    raise SystemExit(6)\n"
        "action = request.get('action')\n"
        "response = {'physical_execution_invoked': "
        + physical
        + ", 'raw_lowcmd_published': "
        + raw_lowcmd
        + "}\n"
        "if action == 'bounded_local_move':\n"
        "    response.update({'ack_status': 'accepted', 'ack_source': 'fixture_bridge'})\n"
        "elif action == 'read_state':\n"
        "    response.update({'pose_observed': True, 'base_stable': True})\n"
        "elif action == 'read_progress':\n"
        "    response.update({'runtime_progress_observed': True, 'completion_observed': True, 'unitree_status': 'succeeded'})\n"
        "else:\n"
        "    response.update({'ack_status': 'accepted'})\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )


def _command(path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_unitree_bridge_dispatch_crosses_command_boundary_without_physical_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "unitree_bridge.py"
    _write_bridge(bridge)
    monkeypatch.setenv(UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    monkeypatch.setenv(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, _command(bridge))

    client = UnitreeMujocoBridgeCommandClient()
    adapter = UnitreeHardwareAdapter(
        config=UnitreeHardwareAdapterConfig(
            missionos_action_ref="missionos_plan_unitree_bounded_local_move",
            local_move=UnitreeBoundedLocalMove(forward_m=0.25),
            operator_approval_ref="approval_unitree_bridge_001",
            approval_actor="operator-001",
            approval_timestamp=datetime.now(timezone.utc),
            opt_in=True,
        ),
        client=client,
    )

    evidence = adapter.dispatch_approved_action()
    bridge_responses = client.collect_responses()

    assert evidence.dispatch_request_sent is True
    assert evidence.command_ack_observed is True
    assert evidence.runtime_progress_observed is True
    assert evidence.completion_claimed is True
    assert evidence.completion_scope == "sim_action"
    assert evidence.physical_execution_invoked is False
    assert "sim_action_completion_not_physical" in evidence.unproven_claims
    assert "raw_motor_not_invoked" in evidence.unproven_claims
    assert "raw_velocity_not_invoked" in evidence.unproven_claims
    assert "special_motion_not_invoked" in evidence.unproven_claims
    assert bridge_responses[0]["action"] == "bounded_local_move"
    assert bridge_responses[0]["ack_source"] == "fixture_bridge"


def test_unitree_bridge_rejects_physical_execution_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "unitree_bridge_physical.py"
    _write_bridge(bridge, physical_claim=True)
    monkeypatch.setenv(UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, _command(bridge))

    client = UnitreeMujocoBridgeCommandClient()

    with pytest.raises(UnitreeMujocoBridgeError):
        client.send_bounded_local_move(UnitreeBoundedLocalMove(forward_m=0.25))


def test_unitree_bridge_rejects_raw_lowcmd_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bridge = tmp_path / "unitree_bridge_raw.py"
    _write_bridge(bridge, raw_lowcmd_claim=True)
    monkeypatch.setenv(UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV, "1")
    monkeypatch.setenv(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, _command(bridge))

    client = UnitreeMujocoBridgeCommandClient()

    with pytest.raises(UnitreeMujocoBridgeError):
        client.send_bounded_local_move(UnitreeBoundedLocalMove(forward_m=0.25))


def test_unitree_sport_client_bridge_is_gate_controlled(
    monkeypatch,
) -> None:
    monkeypatch.delenv(UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV, raising=False)
    request = {
        "schema_version": "missionos_unitree_mujoco_bridge_request.v1",
        "action": "bounded_local_move",
        "payload": UnitreeBoundedLocalMove(forward_m=0.25).model_dump(mode="json"),
        "physical_execution_invoked": False,
        "raw_motor_allowed": False,
        "raw_velocity_allowed": False,
        "special_motion_allowed": False,
    }

    completed = subprocess.run(
        (sys.executable, "scripts/unitree_mujoco_sport_client_bridge.py"),
        input=json.dumps(request, ensure_ascii=True, sort_keys=True),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response["ack_status"] == "rejected"
    assert "unitree_mujoco_sport_client_bridge_opt_in_not_enabled" in (
        response["blocking_reasons"]
    )
    assert response["physical_execution_invoked"] is False
    assert response["raw_lowcmd_published"] is False
