"""Physics-coupled battery for play live SITL via the MotorLoadBatteryCoupler.

The stock PX4 gz world has no rotor-effort battery coupling, so play's battery
was a heuristic. This wires the repo's ``motor_load_battery_coupler`` gz-sim
plugin in: it samples the x500 rotor joint velocities and discharges a battery
state-of-charge proportional to actual motor load, publishing ``BatteryState``.
So fighting wind (motors work harder) genuinely drains the battery faster.

Mechanism (mount-based, no entrypoint hacking): read the image's x500
``model.sdf``, inject the coupler plugin block, and mount both the patched SDF
and the compiled ``.so`` over the container paths at ``docker run`` time so they
are in place before PX4 spawns the model.

Truth-surface: this is a *Gazebo simulation* endurance signal physics-coupled to
rotor effort — verified idle ~0.48 A -> hover ~8.6 A — **not** real power-module
endurance evidence. Play reads it as a separate observed signal and never claims
real-hardware endurance.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

PX4_IMAGE = "px4io/px4-sitl-gazebo:latest"
COUPLER_SO_SOURCE = Path(
    "simulators/gazebo/plugins/motor_load_battery_coupler/build/"
    "libMotorLoadBatteryCoupler.so"
)
X500_MODEL_SDF_IN_IMAGE = "/opt/px4-gazebo/share/gz/models/x500/model.sdf"
COUPLER_SO_IN_IMAGE = "/opt/px4-gazebo/lib/gz/plugins/libMotorLoadBatteryCoupler.so"
BATTERY_STATE_TOPIC = "/model/x500_0/battery/linear_battery/state"

_COUPLER_PLUGIN_BLOCK = """
    <plugin filename="MotorLoadBatteryCoupler" name="boiled_claw::MotorLoadBatteryCoupler">
      <battery_name>linear_battery</battery_name>
      <state_topic>/model/x500_0/battery/linear_battery/state</state_topic>
      <rotor_joint>rotor_0_joint</rotor_joint>
      <rotor_joint>rotor_1_joint</rotor_joint>
      <rotor_joint>rotor_2_joint</rotor_joint>
      <rotor_joint>rotor_3_joint</rotor_joint>
      <idle_power_w>12.0</idle_power_w>
      <hover_power_w>180.0</hover_power_w>
      <hover_rotor_rad_s>700.0</hover_rotor_rad_s>
      <rotor_velocity_slowdown>10.0</rotor_velocity_slowdown>
      <max_power_w>600.0</max_power_w>
      <capacity_ah>5.2</capacity_ah>
      <voltage_full_v>25.2</voltage_full_v>
      <voltage_empty_v>21.0</voltage_empty_v>
      <publish_rate_hz>2.0</publish_rate_hz>
    </plugin>
"""


@dataclass(frozen=True)
class BatteryState:
    voltage_v: float | None
    current_a: float | None
    percentage: float | None  # 0..1


def inject_coupler_plugin(model_sdf: str) -> str:
    """Inject the coupler plugin block before the model's closing tag."""
    index = model_sdf.rfind("</model>")
    if index < 0:
        raise ValueError("x500 model.sdf has no </model> close tag")
    return model_sdf[:index] + _COUPLER_PLUGIN_BLOCK + model_sdf[index:]


def coupler_so_available(source: Path | None = None) -> bool:
    return Path(source or COUPLER_SO_SOURCE).exists()


def prepare_battery_mounts(
    *,
    image: str = PX4_IMAGE,
    so_source: Path | None = None,
    workdir: Path | None = None,
) -> list[str] | None:
    """Return ``docker run`` -v mount args enabling the coupler, or None if the
    compiled .so is absent (caller then leaves battery coupling off)."""
    so_path = Path(so_source or COUPLER_SO_SOURCE)
    if not so_path.exists():
        return None
    out_dir = Path(workdir or tempfile.mkdtemp(prefix="missionos_play_batt_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    original = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", image, X500_MODEL_SDF_IN_IMAGE],
        check=False, capture_output=True, text=True, timeout=60,
    ).stdout
    if "</model>" not in original:
        return None
    patched_sdf = out_dir / "x500_model_patched.sdf"
    patched_sdf.write_text(inject_coupler_plugin(original), encoding="utf-8")
    so_copy = out_dir / "libMotorLoadBatteryCoupler.so"
    so_copy.write_bytes(so_path.read_bytes())

    return [
        "-v", f"{patched_sdf.resolve()}:{X500_MODEL_SDF_IN_IMAGE}",
        "-v", f"{so_copy.resolve()}:{COUPLER_SO_IN_IMAGE}",
    ]


def _scan(text: str, field_name: str) -> float | None:
    match = re.search(rf"(?:^|\s){field_name}:\s*(-?[0-9.eE+]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_battery_state(container: str) -> BatteryState | None:
    """Read one BatteryState message from the coupler. None if unavailable."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "gz", "topic", "-e", "-n", "1",
             "-t", BATTERY_STATE_TOPIC],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    text = result.stdout or ""
    if "percentage" not in text and "voltage" not in text:
        return None
    return BatteryState(
        voltage_v=_scan(text, "voltage"),
        current_a=_scan(text, "current"),
        percentage=_scan(text, "percentage"),
    )
