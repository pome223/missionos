from __future__ import annotations

import os
from pathlib import Path
import sys

from src.runtime.unitree_go2_real_hardware import (
    UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV,
    build_unitree_go2_real_hardware_readiness,
)


def _prepend_pythonpath(monkeypatch, root: Path) -> None:
    current = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(root) if not current else f"{root}{os.pathsep}{current}",
    )


def _write_fake_unitree_sdk2(
    root: Path,
    *,
    sport_server_api_code: int = 0,
    robot_state_service_list_code: int = 0,
) -> None:
    package_dirs = (
        "unitree_sdk2py",
        "unitree_sdk2py/core",
        "unitree_sdk2py/go2",
        "unitree_sdk2py/go2/robot_state",
        "unitree_sdk2py/go2/sport",
    )
    for relative_path in package_dirs:
        package_dir = root / relative_path
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")

    (root / "unitree_sdk2py/core/channel.py").write_text(
        "def ChannelFactoryInitialize(domain_id, network_interface):\n"
        "    if network_interface in {'lo', 'lo0'}:\n"
        "        raise RuntimeError('loopback is not a real Go2 interface')\n",
        encoding="utf-8",
    )
    (root / "unitree_sdk2py/go2/sport/sport_client.py").write_text(
        "class SportClient:\n"
        "    def __init__(self):\n"
        "        self.timeout = None\n"
        "    def SetTimeout(self, timeout):\n"
        "        self.timeout = timeout\n"
        "    def Init(self):\n"
        "        return None\n"
        "    def GetApiVersion(self):\n"
        "        return '1.0.0.1'\n"
        "    def GetServerApiVersion(self):\n"
        f"        return {sport_server_api_code}, '1.0.0.1'\n"
        "    def Move(self, vx, vy, vyaw):\n"
        "        raise AssertionError('readiness must not call Move')\n",
        encoding="utf-8",
    )
    (root / "unitree_sdk2py/go2/robot_state/robot_state_client.py").write_text(
        "class _Service:\n"
        "    name = 'sport_mode'\n"
        "    status = 1\n"
        "    protect = False\n"
        "class RobotStateClient:\n"
        "    def __init__(self):\n"
        "        self.timeout = None\n"
        "    def SetTimeout(self, timeout):\n"
        "        self.timeout = timeout\n"
        "    def Init(self):\n"
        "        return None\n"
        "    def GetServerApiVersion(self):\n"
        "        return 0, '1.0.0.1'\n"
        "    def ServiceList(self):\n"
        f"        return {robot_state_service_list_code}, [_Service()]\n",
        encoding="utf-8",
    )


def test_unitree_go2_real_readiness_gate_blocks_without_import(monkeypatch) -> None:
    monkeypatch.delenv(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV, raising=False)

    readiness = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface="en0",
        python_executable=sys.executable,
    )

    assert readiness.readiness_status == "blocked"
    assert "unitree_go2_real_hardware_readiness_opt_in_not_enabled" in (
        readiness.blocking_reasons
    )
    assert readiness.channel_initialized is False
    assert readiness.dispatch_request_sent is False
    assert readiness.sport_move_call_attempted is False
    assert readiness.physical_execution_invoked is False
    assert readiness.raw_lowcmd_published is False


def test_unitree_go2_real_readiness_blocks_missing_or_loopback_interface(
    monkeypatch,
) -> None:
    monkeypatch.setenv(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV, "1")

    missing = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface=None,
        python_executable=sys.executable,
    )
    loopback = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface="lo",
        python_executable=sys.executable,
    )

    assert missing.readiness_status == "blocked"
    assert "unitree_go2_network_interface_required" in missing.blocking_reasons
    assert loopback.readiness_status == "blocked"
    assert "unitree_go2_network_interface_must_not_be_loopback" in (
        loopback.blocking_reasons
    )


def test_unitree_go2_real_readiness_reads_services_without_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_fake_unitree_sdk2(tmp_path)
    _prepend_pythonpath(monkeypatch, tmp_path)
    monkeypatch.setenv(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV, "1")

    readiness = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface="en0",
        python_executable=sys.executable,
    )

    assert readiness.readiness_status == "ready"
    assert readiness.domain_id == 0
    assert readiness.network_interface == "en0"
    assert readiness.channel_initialized is True
    assert readiness.sport_client_initialized is True
    assert readiness.sport_server_api_code == 0
    assert readiness.sport_service_available is True
    assert readiness.robot_state_service_list_code == 0
    assert readiness.robot_state_services == (
        {"name": "sport_mode", "protect": False, "status": 1},
    )
    assert readiness.dispatch_request_sent is False
    assert readiness.sport_move_call_attempted is False
    assert readiness.physical_execution_invoked is False
    assert readiness.raw_lowcmd_published is False


def test_unitree_go2_real_readiness_blocks_when_sport_service_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_fake_unitree_sdk2(
        tmp_path,
        sport_server_api_code=3102,
        robot_state_service_list_code=3102,
    )
    _prepend_pythonpath(monkeypatch, tmp_path)
    monkeypatch.setenv(UNITREE_GO2_REAL_HARDWARE_READINESS_SMOKE_ENV, "1")

    readiness = build_unitree_go2_real_hardware_readiness(
        opt_in=True,
        network_interface="en0",
        python_executable=sys.executable,
    )

    assert readiness.readiness_status == "blocked"
    assert readiness.sport_server_api_code == 3102
    assert readiness.robot_state_service_list_code == 3102
    assert "unitree_go2_sport_service_unavailable" in readiness.blocking_reasons
    assert "unitree_go2_robot_state_service_list_unavailable" in (
        readiness.blocking_reasons
    )
    assert readiness.dispatch_request_sent is False
    assert readiness.physical_execution_invoked is False
