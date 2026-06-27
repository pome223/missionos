"""Real payload separation for the play delivery mission (gz detachable joint).

The stock PX4 world has no cargo, so play's dropoff was a *commanded* release
with no physical separation. This wires gz's DetachableJoint in: a
``delivery_payload`` body is spawned in the world and attached under the x500
``base_link``; the drone carries it, and publishing the detach topic physically
releases it so it falls. Verified live: payload z ~2.5 m (carried) -> ~0.02 m
(dropped) after detach.

Mechanism mirrors the battery coupler: read the image's x500 ``model.sdf`` and
``default.sdf``, inject the DetachableJoint plugin (into the model) and the
payload model (into the world), and mount the patched files at ``docker run``
time so they are in place before the world loads.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

PX4_IMAGE = "px4io/px4-sitl-gazebo:latest"
X500_MODEL_SDF_IN_IMAGE = "/opt/px4-gazebo/share/gz/models/x500/model.sdf"
DEFAULT_WORLD_SDF_IN_IMAGE = "/opt/px4-gazebo/share/gz/worlds/default.sdf"
DETACH_TOPIC = "/model/x500_0/delivery_payload/detach"
PAYLOAD_MODEL_NAME = "delivery_payload"

_DETACHABLE_JOINT_BLOCK = """
    <plugin filename="gz-sim-detachable-joint-system" name="gz::sim::systems::DetachableJoint">
      <parent_link>base_link</parent_link>
      <child_model>delivery_payload</child_model>
      <child_link>payload_link</child_link>
      <detach_topic>/model/x500_0/delivery_payload/detach</detach_topic>
    </plugin>
"""


def _payload_model_block(mass_kg: float) -> str:
    return f"""
    <model name="delivery_payload">
      <pose>0 0 0.18 0 0 0</pose>
      <static>false</static>
      <link name="payload_link">
        <inertial><mass>{mass_kg:.6f}</mass>
          <inertia><ixx>0.0001</ixx><ixy>0</ixy><ixz>0</ixz><iyy>0.0001</iyy><iyz>0</iyz><izz>0.0001</izz></inertia>
        </inertial>
        <collision name="payload_collision"><geometry><box><size>0.12 0.12 0.08</size></box></geometry></collision>
        <visual name="payload_visual"><geometry><box><size>0.12 0.12 0.08</size></box></geometry>
          <material><diffuse>0.1 0.5 1.0 1</diffuse></material></visual>
      </link>
    </model>
"""


def inject_detachable_joint(model_sdf: str) -> str:
    index = model_sdf.rfind("</model>")
    if index < 0:
        raise ValueError("x500 model.sdf has no </model> close tag")
    return model_sdf[:index] + _DETACHABLE_JOINT_BLOCK + model_sdf[index:]


def inject_payload_model(world_sdf: str, *, mass_kg: float) -> str:
    index = world_sdf.rfind("</world>")
    if index < 0:
        raise ValueError("world sdf has no </world> close tag")
    return world_sdf[:index] + _payload_model_block(mass_kg) + world_sdf[index:]


def _cat_from_image(image: str, path: str) -> str:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", image, path],
        check=False, capture_output=True, text=True, timeout=60,
    ).stdout


def prepare_payload_mounts(
    *,
    image: str = PX4_IMAGE,
    payload_mass_kg: float = 0.05,
    workdir: Path | None = None,
) -> list[str] | None:
    """Return ``docker run`` -v mounts that spawn + attach the payload, or None."""
    model_sdf = _cat_from_image(image, X500_MODEL_SDF_IN_IMAGE)
    world_sdf = _cat_from_image(image, DEFAULT_WORLD_SDF_IN_IMAGE)
    if "</model>" not in model_sdf or "</world>" not in world_sdf:
        return None
    out_dir = Path(workdir or tempfile.mkdtemp(prefix="missionos_play_payload_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    patched_model = out_dir / "x500_model_payload.sdf"
    patched_world = out_dir / "default_payload.sdf"
    patched_model.write_text(inject_detachable_joint(model_sdf), encoding="utf-8")
    patched_world.write_text(
        inject_payload_model(world_sdf, mass_kg=payload_mass_kg), encoding="utf-8"
    )
    return [
        "-v", f"{patched_model.resolve()}:{X500_MODEL_SDF_IN_IMAGE}",
        "-v", f"{patched_world.resolve()}:{DEFAULT_WORLD_SDF_IN_IMAGE}",
    ]


@dataclass(frozen=True)
class PayloadSeparation:
    z_before_m: float | None
    z_after_m: float | None
    physically_separated: bool


def read_payload_z(container: str) -> float | None:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "gz", "topic", "-e", "-n", "1",
             "-t", "/world/default/pose/info"],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    text = result.stdout or ""
    index = text.find(PAYLOAD_MODEL_NAME)
    if index < 0:
        return None
    match = re.search(r"z:\s*(-?[0-9.eE+]+)", text[index:])
    return float(match.group(1)) if match else None


def command_detach(container: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "gz", "topic", "-t", DETACH_TOPIC,
             "-m", "gz.msgs.Empty", "-p", ""],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def verify_separation(z_before: float | None, z_after: float | None, *, drop_m: float = 0.5) -> bool:
    """A real separation: the payload fell at least ``drop_m`` after the detach."""
    if z_before is None or z_after is None:
        return False
    return (z_before - z_after) >= drop_m
