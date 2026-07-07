# NVIDIA Isaac Sim / Nova Carter Runtime Profile

This page documents the opt-in Nova Carter simulator profile for MissionOS.
It is an agent-facing runtime contract, not a public proof report.

## Purpose

MissionOS can represent NVIDIA Isaac Sim / Isaac ROS Nova Carter as a distinct
Nav2 runtime profile:

- `robot_profile=nova_carter`
- `robot_model=nova_carter`
- `execution_target=isaac_ros_nav2_nova_carter_sim`
- `runtime_substrate=NVIDIA Isaac Sim + Isaac ROS/Nav2`

The goal is to place MissionOS governance on top of NVIDIA's Physical AI
substrate:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

Nova Carter is not interchangeable with TurtleBot3 or TurtleBot4. They may share
the MissionOS/Nav2 governance contract, but launch, TF, sensors, maps, simulator
setup, and telemetry collection remain profile-specific.

## Current Scope

The first slice is a scaffold and claim-boundary contract:

- `missionos chat --robot nova-carter` records `robot_profile=nova_carter`.
- Task artifacts identify `execution_target=isaac_ros_nav2_nova_carter_sim`.
- Missing Isaac Sim / Nova Carter runtime produces `status=blocked` and
  `runtime_configuration_status=not_configured`.
- Simulator-backed results preserve `completion_scope=sim_action`.
- `physical_execution_invoked=false` and
  `mission_delivery_completion_claimed=false` remain explicit.

This slice is not external-facing NVIDIA runtime proof. The only external-facing
proof is a live Isaac Sim + Nova Carter run with task artifact, map/watch
evidence, and limitations recorded in a PR body.

## Runtime Contract

The adapter still crosses the same bounded Nav2 bridge surface:

1. MissionOS builds a proposal.
2. The human approves the proposal.
3. The executor sends one bounded Nav2 goal through an opt-in bridge command.
4. The bridge returns ACK, Nav2 result, odom or equivalent motion evidence, and
   any profile-specific telemetry references.
5. The verifier decides whether the simulator-backed action can claim
   `completion_scope=sim_action`.

The bridge must not claim:

- physical execution
- raw velocity authority from MissionOS
- raw ROS topic publication by MissionOS
- delivery completion
- whole-environment autonomy

## Opt-In Runtime Expectations

MissionOS does not launch Isaac Sim by default. A runtime-ready Nova Carter
environment must provide an operator-configured bridge command via:

```bash
export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export ROS2_NAV2_BRIDGE_COMMAND="<operator-provided Nova Carter bridge command>"
```

Until that bridge exists, the expected result is:

```text
status=blocked
runtime_configuration_status=not_configured
blocking_reasons includes isaac_sim_nova_carter_bridge_command_missing
blocking_reasons includes isaac_sim_nova_carter_runtime_not_enabled
dispatch_request_sent=false
completion_claimed=false
physical_execution_invoked=false
mission_delivery_completion_claimed=false
```

## Live-Proof Runbook

Run this section only on an RTX Windows/Linux host or a remote cloud
workstation that can run Isaac Sim, Isaac ROS, Nova Carter, ROS2, and Nav2.
Do not run it on a macOS client-only machine and do not claim live runtime proof
from the scaffold path above.

Prepare the Isaac Sim / Nova Carter scene and the ROS2/Nav2 graph first. Then
configure MissionOS to use the operator-provided Nova Carter bridge command:

```bash
export RUN_MISSIONOS_CHAT_NOVA_CARTER_LIVE_PROOF=1
export RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE=1
export ROS2_NAV2_BRIDGE_COMMAND="<operator-provided Nova Carter Nav2 bridge command>"
export MISSIONOS_CHAT_NOVA_CARTER_ARTIFACT_MANIFEST_OUT=output/nova_carter_live_proof/manifest.json
```

The bridge command is the only runtime-specific part of this runbook. It must
send one bounded Nav2 goal to the active Nova Carter graph and return adapter
evidence that includes dispatch, ACK, Nav2 result, odom or equivalent motion,
and profile-specific telemetry/log references. MissionOS must not publish raw
velocity commands or raw ROS topics directly.

Run the smoke:

```bash
PYTHONPATH=.:packages/missionos-cli/src:packages/missionos-gateway/src \
  python3 scripts/smoke_missionos_chat_nova_carter_live_proof.py
```

The smoke starts a production Gateway on loopback, posts the same source-bound
conversation sequence that chat uses, then stops the Gateway:

```text
plan    -> robot_profile=nova_carter
approve -> source-bound human approval context
run     -> execution gate and opt-in bridge dispatch
```

The manifest supports a live runtime smoke only when it says
`runtime_evidence_ready=true` and every runtime field below is true:

```text
robot_profile=nova_carter
execution_target=isaac_ros_nav2_nova_carter_sim
task_id_present=true
dispatch_request_sent=true
ack_observed=true
robot_motion_observed=true
odom_delta_present=true
completion_claimed=true
completion_scope=sim_action
physical_execution_not_invoked=true
mission_delivery_not_claimed=true
```

If ACK is observed but odom motion is not observed, the smoke must fail and the
claim remains blocked. ACK is not success.

## Map And Watch Evidence

After a live run produces a task id, capture read-only watch and map artifacts:

```bash
missionos watch --task-id <task_id> --poll-interval 1
missionos map --task-id <task_id> --snapshot --no-open
```

Record the captured artifact paths before rerunning the manifest smoke or when
preparing the PR body:

```bash
export MISSIONOS_CHAT_NOVA_CARTER_WATCH_ARTIFACT="<watch capture path or URI>"
export MISSIONOS_CHAT_NOVA_CARTER_MAP_ARTIFACT="<map snapshot path or URI>"
```

External-facing NVIDIA runtime proof requires `ready_for_external_claim=true`,
which is `runtime_evidence_ready=true` plus:

```text
map_artifact_recorded=true
watch_artifact_recorded=true
```

The PR or issue update for live proof must include:

- exact Isaac Sim / Nova Carter launch context
- exact MissionOS command and environment
- task id
- dispatch / ACK / motion evidence
- odom delta or equivalent motion evidence
- map/watch artifact paths
- limitations

It must still leave these claims false:

- physical execution
- mission delivery completion
- raw velocity authority from MissionOS
- whole-environment autonomy

## Publication Language

Allowed before live evidence:

```text
MissionOS records a Nova Carter simulator profile and preserves approval,
dispatch, evidence, and claim boundaries when the Isaac runtime is not
configured.
```

Allowed only after `ready_for_external_claim=true`:

```text
MissionOS observed a bounded Nova Carter Nav2 simulator action on NVIDIA Isaac
Sim / Isaac ROS with human approval, dispatch evidence, ACK evidence, motion
evidence, and verifier-bounded sim_action completion.
```

Never write:

```text
MissionOS physically executed Nova Carter.
MissionOS completed delivery with Nova Carter.
The scaffold proves NVIDIA runtime readiness.
Nvblox proves obstacle avoidance by itself.
```

## References

- NVIDIA Isaac Sim documents a ROS 2 Navigation sample under
  `ROS2 > Navigation > Nova Carter`.
- The NVIDIA-ISAAC-ROS `nova_carter` repository describes Nova Carter packages
  for Nav2, Isaac ROS Visual SLAM, and Isaac ROS Nvblox.
- `docs/agents/nvblox-perception-evidence.md` tracks the separate Nvblox
  perception evidence epic. It can support obstacle-aware claims only when
  paired with observed trajectory/verifier evidence.
