# Ground-Robot 3D Trajectory Clearance

MissionOS evaluates a source-backed observed trajectory against source-backed
obstacle collision volumes using the robot collision envelope. This closes the
point-robot gap in the TurtleBot3 simulator evidence path without turning
perception, geometry, or Nav2 into approval authority.

```text
schema_version=missionos_trajectory_clearance_3d.v2
```

## Required evidence

A `verified_clear` or `collision_observed` result requires all of:

- at least one coherent observed trajectory segment in `frame_id=map`;
- a robot collision envelope with radius and vertical bounds;
- at least one collision candidate AABB with map-frame center, XYZ size,
  evidence source, and stable obstacle reference.

Candidate enumeration is label-independent. `semantic_candidate` may say
`closed door`, `humanoid`, `robot dog`, or `unknown_obstacle`, but the verifier
uses only the source-backed geometry. Each candidate receives its own
`candidate_results[]` entry before MissionOS reduces them to the aggregate
status.

The TurtleBot3 Gazebo profile uses a conservative vertical-cylinder envelope
derived from the collision shapes in the stock `waffle_pi` SDF:

```text
radius_m=0.19
z_min_m=-0.01
z_max_m=0.14
frame_id=base_footprint
```

The opt-in scene candidates use the exact collision boxes spawned by the
headless obstacle smoke. For example, the arena closed-door volume is:

```text
center=(-1.15, -0.50, 0.25)  # arena profile
size=(0.32, 0.32, 0.50)
frame_id=map
```

The house profile changes XY position but keeps the same SDF collision size.

## Evaluation

For every coherent trajectory segment and every candidate, MissionOS sweeps the
robot cylinder against the candidate AABB. A centerline that misses the raw box
can still be a collision when the robot radius overlaps the box. Vertically
separated volumes produce a positive 3D surface clearance.

Statuses:

```text
verified_clear      observed swept volume does not intersect the obstacle
collision_observed  observed swept volume intersects the obstacle
unavailable         trajectory, robot envelope, or obstacle volume is missing
```

If a localized camera/LiDAR candidate has no complete XYZ volume, its stable
reference is recorded in `unresolved_candidate_refs` and aggregate clearance is
`unavailable`. MissionOS does not turn a pixel box, semantic label, or one
LiDAR range into a guessed 3D obstacle. A camera-only candidate without a map
position is retained as visual evidence but is not route-addressable, so the 3D
verifier makes no intersection claim about it.

## Image-recognition compatibility

Image recognition remains useful and independent from collision geometry:

- the VLM or detector proposes `semantic_candidate`, confidence, and image
  region evidence;
- depth, stereo, 3D LiDAR, or another geometric sensor supplies a map-frame
  `collision_volume` with `geometry_source` and `evidence_ref`;
- timestamped TF places that volume in `map`;
- the generic verifier evaluates the resulting volume without branching on
  the semantic label.

`visual_observation_collision_candidates()` is the adapter boundary. Complete
sensor-backed volumes become generic obstacle candidates. A localized visual
candidate with incomplete volume evidence fails closed. This keeps prompts and
future object classes extensible without giving prompt text or VLM output
safety authority.

For TurtleBot3 obstacle missions, obstacle-avoidance completion now requires
both the existing raw map-frame centerline evidence and
`obstacle_trajectory_3d_clearance_observed=true`. A bridge success ACK or a
point-only path is insufficient.

## Claim boundary

The assessment is evidence only:

```text
approval_created=false
dispatch_authority_created=false
physical_execution_invoked=false
completion_claimed=false
```

It can constrain MissionOS completion verification, but it cannot approve or
dispatch an action itself.

Current TurtleBot3 map trajectories are planar, so base Z comes from the
ground-robot runtime profile rather than per-sample 3D pose observations. The
artifact records `planar_base_z_from_robot_runtime_profile` as a limitation.
Arbitrary 3D motion still requires 3D pose samples. RGB-only observations and
unknown obstacle height still return `unavailable` rather than assuming an
extrusion.
