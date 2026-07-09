# Nvblox Perception Evidence

This contract adds an optional Nvblox-derived perception evidence surface for
MissionOS obstacle-aware claims. It does not make Nvblox an approval authority,
dispatch authority, executor, verifier, physical runtime, or delivery-completion
source.

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

## Scope

Nvblox evidence is recorded under:

```text
schema_version=missionos_nvblox_perception_evidence.v1
perception_source=isaac_ros_nvblox
```

The runtime may attach it from either:

```text
MISSIONOS_NVBLOX_PERCEPTION_EVIDENCE_JSON=/path/to/evidence.json
```

or from a ROS2/Nav2 bridge response field named:

```text
nvblox_perception_evidence
```

The optional required gate is:

```text
MISSIONOS_NVBLOX_PERCEPTION_EVIDENCE_REQUIRED=1
```

When required but absent, MissionOS records:

```text
nvblox_perception_evidence_status=not_configured
blocking_reasons includes nvblox_perception_evidence_not_configured
completion_claimed=false
physical_execution_invoked=false
mission_delivery_completion_claimed=false
```

## Evidence Fields

The evidence payload tracks:

```text
perception_evidence_available
perception_source
depth_input_observed
pose_input_observed
scene_reconstruction_observed
nav2_costmap_updated_from_perception
dynamic_obstacle_observed
perception_artifact_refs
limitations
blocking_reasons
```

`perception_evidence_available=true` requires all of:

```text
depth_input_observed=true
pose_input_observed=true
scene_reconstruction_observed=true
nav2_costmap_updated_from_perception=true
```

Missing observations fail closed with explicit `blocking_reasons`.

## Claim Boundary

Nvblox evidence alone cannot claim obstacle avoidance.

Allowed:

```text
MissionOS recorded Nvblox-derived perception evidence, including depth input,
pose input, scene reconstruction, and a Nav2 costmap update.
```

Allowed only when paired with trajectory/verifier evidence:

```text
MissionOS observed an obstacle-relevant perception update and verified that the
observed trajectory cleared the obstacle.
```

Never write:

```text
Nvblox approved the recovery.
Nvblox dispatched the robot.
Nvblox proves obstacle avoidance by itself.
Nvblox proves delivery completion.
Nvblox proves physical execution.
```

Obstacle-aware completion still requires the existing runtime boundary:

```text
bridge_obstacle_avoidance_observed=true
obstacle_trajectory_clearance_observed=true
obstacle_trajectory_intersects_obstacle=false
```

Nvblox may support `costmap_obstacle_observed=true`, but it does not set
`obstacle_avoidance_observed=true`.

## Runtime Status

`nvblox_perception_evidence_status` values:

```text
not_requested   Nvblox evidence was not requested and no payload was present.
not_configured  Nvblox evidence was required but no payload was configured.
unavailable     A payload was present but required observations were missing.
available       Required perception observations and costmap update were present.
```

## Public Fixtures

Public-safe fixture payloads live under:

```text
examples/fixture_missions/nvblox/
```

Use them for contract tests and documentation review when no RTX/GPU host is
available. They are not live Isaac ROS Nvblox output and must not be published
as runtime proof.

- `available-perception-evidence.json` exercises a complete depth/pose ->
  reconstruction -> Nav2 costmap evidence payload.
- `unavailable-missing-pose-evidence.json` intentionally omits pose evidence and
  must fail closed with `nvblox_pose_input_not_observed`.

## References

- NVIDIA Isaac ROS Nvblox describes GPU-accelerated depth/pose scene
  reconstruction and Nav2 costmap output:
  <https://nvidia-isaac-ros.github.io/repositories_and_packages/isaac_ros_nvblox/index.html>
- NVIDIA Nvblox concepts describe voxel scene reconstruction from depth images
  or 3D LiDAR and costmap output for planning:
  <https://nvidia-isaac-ros.github.io/concepts/scene_reconstruction/nvblox/index.html>
- NVIDIA Isaac Sim Nvblox examples describe using simulated sensor data with a
  2D costmap passed to Nav2:
  <https://nvidia-isaac-ros.github.io/concepts/scene_reconstruction/nvblox/tutorials/tutorial_isaac_sim.html>
