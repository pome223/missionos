from __future__ import annotations

from pathlib import Path
import sys

from src.runtime.unitree_mujoco_environment import (
    UNITREE_MUJOCO_PROCESS_SMOKE_ENV,
    UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV,
    UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV,
    UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV,
    UNITREE_SDK2_IMPORT_SMOKE_ENV,
    build_unitree_mujoco_environment_readiness,
    build_unitree_sdk2_import_readiness,
    launch_unitree_mujoco_python_simulator,
    observe_unitree_mujoco_sdk2_bridge_state,
    observe_unitree_mujoco_scene_motion,
    probe_unitree_mujoco_sport_client_bounded_move,
)


def _write_unitree_mujoco_checkout(
    root: Path,
    *,
    robot: str = "go2",
    domain_id: int = 1,
    interface: str = "lo",
    use_joystick: int = 0,
) -> None:
    files = (
        "readme.md",
        "simulate_python/unitree_mujoco.py",
        "simulate_python/unitree_sdk2py_bridge.py",
        "unitree_robots/go2/scene.xml",
        "example/python/stand_go2.py",
    )
    for relative_path in files:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")

    config = f'''
ROBOT = "{robot}"
ROBOT_SCENE = "../unitree_robots/" + ROBOT + "/scene.xml"
DOMAIN_ID = {domain_id}
INTERFACE = "{interface}"
PRINT_SCENE_INFORMATION = True
USE_JOYSTICK = {use_joystick}
'''
    (root / "simulate_python").mkdir(parents=True, exist_ok=True)
    (root / "simulate_python/config.py").write_text(config.strip(), encoding="utf-8")


def _write_fake_mujoco_module(simulator_dir: Path) -> None:
    (simulator_dir / "mujoco.py").write_text(
        "import numpy as np\n"
        "class _Opt:\n"
        "    timestep = 0.0\n"
        "class MjModel:\n"
        "    nq = 2\n"
        "    nv = 2\n"
        "    nu = 1\n"
        "    opt = _Opt()\n"
        "    @classmethod\n"
        "    def from_xml_path(cls, path):\n"
        "        return cls()\n"
        "class MjData:\n"
        "    def __init__(self, model):\n"
        "        self.qpos = np.array([0.0, 0.0])\n"
        "def mj_step(model, data):\n"
        "    data.qpos = data.qpos + np.array([0.1, 0.0])\n",
        encoding="utf-8",
    )


def _write_fake_sdk2_bridge_modules(
    simulator_dir: Path,
    *,
    sport_move_return_code: int = 3102,
    sport_server_api_code: int = 3102,
    robot_state_service_list_code: int = 3102,
) -> None:
    package_dirs = (
        "unitree_sdk2py",
        "unitree_sdk2py/core",
        "unitree_sdk2py/go2",
        "unitree_sdk2py/go2/robot_state",
        "unitree_sdk2py/go2/sport",
        "unitree_sdk2py/idl",
        "unitree_sdk2py/idl/unitree_go",
        "unitree_sdk2py/idl/unitree_go/msg",
        "unitree_sdk2py/idl/unitree_go/msg/dds_",
    )
    for relative_path in package_dirs:
        package_dir = simulator_dir / relative_path
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")

    (simulator_dir / "unitree_sdk2py/core/channel.py").write_text(
        "def ChannelFactoryInitialize(*args, **kwargs):\n"
        "    return None\n"
        "class ChannelSubscriber:\n"
        "    def __init__(self, name, type_):\n"
        "        self.name = name\n"
        "    def Init(self, handler=None, queueLen=0):\n"
        "        if handler is not None:\n"
        "            from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_\n"
        "            handler(LowState_())\n",
        encoding="utf-8",
    )
    (simulator_dir / "unitree_sdk2py/idl/unitree_go/msg/dds_/__init__.py").write_text(
        "class _MotorState:\n"
        "    q = 0.25\n"
        "class LowState_:\n"
        "    def __init__(self):\n"
        "        self.motor_state = [_MotorState() for _ in range(20)]\n"
        "        self.power_v = 12.0\n",
        encoding="utf-8",
    )
    (simulator_dir / "unitree_sdk2py/go2/sport/sport_client.py").write_text(
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
        f"        return {sport_server_api_code}, None\n"
        "    def Move(self, vx, vy, vyaw):\n"
        f"        return {sport_move_return_code}\n"
        "    def StopMove(self):\n"
        "        return 0\n",
        encoding="utf-8",
    )
    (
        simulator_dir / "unitree_sdk2py/go2/robot_state/robot_state_client.py"
    ).write_text(
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
        f"        return {robot_state_service_list_code}, None\n",
        encoding="utf-8",
    )
    (simulator_dir / "unitree_sdk2py_bridge.py").write_text(
        "class UnitreeSdk2Bridge:\n"
        "    def __init__(self, model, data):\n"
        "        self.model = model\n"
        "        self.data = data\n",
        encoding="utf-8",
    )


def test_unitree_mujoco_readiness_accepts_go2_loopback_checkout(tmp_path: Path) -> None:
    _write_unitree_mujoco_checkout(tmp_path)

    readiness = build_unitree_mujoco_environment_readiness(checkout_root=tmp_path)

    assert readiness.readiness_status == "ready"
    assert readiness.config_robot == "go2"
    assert readiness.config_robot_scene == "../unitree_robots/go2/scene.xml"
    assert readiness.config_domain_id == 1
    assert readiness.config_interface == "lo"
    assert readiness.unitree_sdk2_imported is False
    assert readiness.mujoco_started is False
    assert readiness.dispatch_request_sent is False
    assert readiness.physical_execution_invoked is False
    assert readiness.blocking_reasons == ()


def test_unitree_mujoco_readiness_accepts_macos_loopback_checkout(
    tmp_path: Path,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path, interface="lo0")

    readiness = build_unitree_mujoco_environment_readiness(checkout_root=tmp_path)

    assert readiness.readiness_status == "ready"
    assert readiness.config_interface == "lo0"
    assert readiness.blocking_reasons == ()


def test_unitree_mujoco_readiness_blocks_when_root_missing() -> None:
    readiness = build_unitree_mujoco_environment_readiness(checkout_root=None)

    assert readiness.readiness_status == "blocked"
    assert "unitree_mujoco_root_not_provided" in readiness.blocking_reasons
    assert readiness.unitree_sdk2_imported is False
    assert readiness.mujoco_started is False
    assert readiness.physical_execution_invoked is False


def test_unitree_mujoco_readiness_blocks_missing_required_files(
    tmp_path: Path,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    (tmp_path / "unitree_robots/go2/scene.xml").unlink()

    readiness = build_unitree_mujoco_environment_readiness(checkout_root=tmp_path)

    assert readiness.readiness_status == "blocked"
    assert "unitree_robots/go2/scene.xml" in readiness.missing_relative_files
    assert "unitree_mujoco_required_files_missing" in readiness.blocking_reasons


def test_unitree_mujoco_readiness_blocks_non_loopback_or_real_domain(
    tmp_path: Path,
) -> None:
    _write_unitree_mujoco_checkout(
        tmp_path,
        robot="b2",
        domain_id=0,
        interface="en0",
        use_joystick=1,
    )

    readiness = build_unitree_mujoco_environment_readiness(checkout_root=tmp_path)

    assert readiness.readiness_status == "blocked"
    assert "unitree_mujoco_robot_not_go2" in readiness.blocking_reasons
    assert "unitree_mujoco_domain_id_not_sim_safe_1" in readiness.blocking_reasons
    assert "unitree_mujoco_interface_not_loopback" in readiness.blocking_reasons
    assert "unitree_mujoco_joystick_enabled" in readiness.warning_reasons


def test_unitree_sdk2_import_readiness_is_gate_controlled(monkeypatch) -> None:
    monkeypatch.delenv(UNITREE_SDK2_IMPORT_SMOKE_ENV, raising=False)

    readiness = build_unitree_sdk2_import_readiness(
        opt_in=True,
        module_names=("json",),
    )

    assert readiness.readiness_status == "blocked"
    assert readiness.import_attempted is False
    assert "unitree_sdk2_import_opt_in_not_enabled" in readiness.blocking_reasons


def test_unitree_sdk2_import_readiness_reports_missing_modules(monkeypatch) -> None:
    monkeypatch.setenv(UNITREE_SDK2_IMPORT_SMOKE_ENV, "1")

    readiness = build_unitree_sdk2_import_readiness(
        opt_in=True,
        module_names=("json", "unitree_sdk2py_missing_for_contract_test"),
    )

    assert readiness.import_attempted is True
    assert "json" in readiness.imported_modules
    assert "unitree_sdk2py_missing_for_contract_test" in readiness.missing_modules
    assert (
        "ModuleNotFoundError"
        in readiness.module_errors["unitree_sdk2py_missing_for_contract_test"]
    )
    assert readiness.mujoco_started is False
    assert readiness.dispatch_request_sent is False
    assert readiness.physical_execution_invoked is False


def test_unitree_sdk2_import_readiness_can_use_external_python(
    monkeypatch,
) -> None:
    monkeypatch.setenv(UNITREE_SDK2_IMPORT_SMOKE_ENV, "1")

    readiness = build_unitree_sdk2_import_readiness(
        opt_in=True,
        module_names=("json",),
        python_executable=sys.executable,
    )

    assert readiness.readiness_status == "ready"
    assert readiness.import_attempted is True
    assert readiness.import_subprocess_invoked is True
    assert readiness.import_subprocess_returncode == 0
    assert readiness.python_executable == sys.executable
    assert readiness.imported_modules == ("json",)
    assert readiness.missing_modules == ()
    assert readiness.module_errors == {}
    assert readiness.mujoco_started is False
    assert readiness.dispatch_request_sent is False
    assert readiness.physical_execution_invoked is False


def test_unitree_mujoco_process_launch_is_gate_controlled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    monkeypatch.delenv(UNITREE_MUJOCO_PROCESS_SMOKE_ENV, raising=False)

    result = launch_unitree_mujoco_python_simulator(
        checkout_root=tmp_path,
        opt_in=True,
        wait_seconds=0.01,
        python_executable=sys.executable,
    )

    assert result.launch_status == "blocked"
    assert result.process_started is False
    assert "unitree_mujoco_process_opt_in_not_enabled" in result.blocking_reasons
    assert result.scene_loaded_observed is False
    assert result.robot_motion_observed is False
    assert result.dispatch_request_sent is False
    assert result.physical_execution_invoked is False


def test_unitree_mujoco_process_launch_starts_and_terminates_fixture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    simulator = tmp_path / "simulate_python/unitree_mujoco.py"
    simulator.write_text(
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(UNITREE_MUJOCO_PROCESS_SMOKE_ENV, "1")

    result = launch_unitree_mujoco_python_simulator(
        checkout_root=tmp_path,
        opt_in=True,
        wait_seconds=0.05,
        python_executable=sys.executable,
    )

    assert result.launch_status == "started"
    assert result.process_started is True
    assert result.mujoco_started is True
    assert result.terminated_by_smoke is True
    assert result.scene_loaded_observed is False
    assert result.robot_motion_observed is False
    assert result.unitree_sdk2_imported is False
    assert result.dispatch_request_sent is False
    assert result.physical_execution_invoked is False


def test_unitree_mujoco_scene_observation_is_gate_controlled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    monkeypatch.delenv(UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV, raising=False)

    result = observe_unitree_mujoco_scene_motion(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
    )

    assert result.observation_status == "blocked"
    assert "unitree_mujoco_scene_observation_opt_in_not_enabled" in (
        result.blocking_reasons
    )
    assert result.scene_loaded_observed is False
    assert result.robot_motion_observed is False
    assert result.dispatch_request_sent is False
    assert result.physical_execution_invoked is False


def test_unitree_mujoco_scene_observation_loads_fixture_scene_and_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    simulator_dir = tmp_path / "simulate_python"
    _write_fake_mujoco_module(simulator_dir)
    monkeypatch.setenv(UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE_ENV, "1")

    result = observe_unitree_mujoco_scene_motion(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
        step_count=3,
    )

    assert result.observation_status == "observed"
    assert result.scene_loaded_observed is True
    assert result.robot_motion_observed is True
    assert result.motion_source == "uncommanded_physics_step"
    assert result.qpos_delta_norm is not None
    assert result.qpos_delta_norm > 0.0
    assert result.step_count == 3
    assert result.model_nq == 2
    assert result.model_nv == 2
    assert result.model_nu == 1
    assert result.config_robot == "go2"
    assert result.config_domain_id == 1
    assert result.config_interface == "lo"
    assert result.mujoco_process_started is False
    assert result.dispatch_request_sent is False
    assert result.physical_execution_invoked is False


def test_unitree_mujoco_sdk2_bridge_observation_is_gate_controlled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    monkeypatch.delenv(
        UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV,
        raising=False,
    )

    result = observe_unitree_mujoco_sdk2_bridge_state(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
    )

    assert result.bridge_observation_status == "blocked"
    assert "unitree_mujoco_sdk2_bridge_observation_opt_in_not_enabled" in (
        result.blocking_reasons
    )
    assert result.sdk2_bridge_started is False
    assert result.lowstate_observed is False
    assert result.dispatch_request_sent is False
    assert result.command_ack_observed is False
    assert result.physical_execution_invoked is False


def test_unitree_mujoco_sport_client_probe_is_gate_controlled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    monkeypatch.delenv(
        UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV,
        raising=False,
    )

    result = probe_unitree_mujoco_sport_client_bounded_move(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
    )

    assert result.probe_status == "blocked"
    assert "unitree_mujoco_sport_client_probe_opt_in_not_enabled" in (
        result.blocking_reasons
    )
    assert result.sport_client_initialized is False
    assert result.sport_move_call_attempted is False
    assert result.dispatch_request_sent is False
    assert result.command_ack_observed is False
    assert result.physical_execution_invoked is False
    assert result.raw_lowcmd_published is False


def test_unitree_mujoco_sport_client_probe_records_unavailable_move_without_raw_lowcmd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    simulator_dir = tmp_path / "simulate_python"
    _write_fake_mujoco_module(simulator_dir)
    _write_fake_sdk2_bridge_modules(simulator_dir, sport_move_return_code=3102)
    monkeypatch.setenv(UNITREE_MUJOCO_SPORT_CLIENT_PROBE_SMOKE_ENV, "1")

    result = probe_unitree_mujoco_sport_client_bounded_move(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
        step_count=3,
        warmup_steps=1,
    )

    assert result.probe_status == "blocked"
    assert result.scene_loaded_observed is True
    assert result.robot_motion_observed is True
    assert result.sdk2_bridge_started is True
    assert result.lowstate_observed is True
    assert result.sport_client_initialized is True
    assert result.sport_move_call_attempted is True
    assert result.sport_move_return_code == 3102
    assert result.sport_server_api_code == 3102
    assert result.sport_service_available is False
    assert result.robot_state_service_list_code == 3102
    assert result.stop_move_return_code is None
    assert result.dispatch_request_sent is False
    assert result.command_ack_observed is False
    assert result.completion_claimed is False
    assert result.physical_execution_invoked is False
    assert result.raw_lowcmd_published is False
    assert result.raw_motor_invoked is False
    assert result.raw_velocity_invoked is False
    assert result.special_motion_invoked is False
    assert "unitree_sport_client_move_not_accepted" in result.blocking_reasons
    assert "unitree_mujoco_sport_service_unavailable" in result.blocking_reasons
    assert "unitree_sport_service_version_unavailable" in result.blocking_reasons
    assert "unitree_robot_state_service_list_unavailable" in result.blocking_reasons


def test_unitree_mujoco_sdk2_bridge_observation_reads_fixture_lowstate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_unitree_mujoco_checkout(tmp_path)
    simulator_dir = tmp_path / "simulate_python"
    _write_fake_mujoco_module(simulator_dir)
    _write_fake_sdk2_bridge_modules(simulator_dir)
    monkeypatch.setenv(UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE_ENV, "1")

    result = observe_unitree_mujoco_sdk2_bridge_state(
        checkout_root=tmp_path,
        opt_in=True,
        python_executable=sys.executable,
        step_count=3,
    )

    assert result.bridge_observation_status == "observed"
    assert result.scene_loaded_observed is True
    assert result.robot_motion_observed is True
    assert result.sdk2_bridge_started is True
    assert result.lowstate_observed is True
    assert result.lowstate_sample == {
        "first_motor_q": 0.25,
        "motor_count": 20,
        "power_v": 12.0,
    }
    assert result.step_count == 3
    assert result.model_nq == 2
    assert result.model_nv == 2
    assert result.model_nu == 1
    assert result.config_robot == "go2"
    assert result.config_domain_id == 1
    assert result.config_interface == "lo"
    assert result.channel_interface_mode == "config"
    assert result.channel_interface_used == "lo"
    assert result.dispatch_request_sent is False
    assert result.command_ack_observed is False
    assert result.physical_execution_invoked is False
