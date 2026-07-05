# MissionOS Contracts

MissionOS code should preserve these boundaries.

## CLI

The CLI is an operator surface. It may:

- submit operator instructions to the Gateway
- record approval or rejection intent through Gateway routes
- display task status, timeline events, runtime snapshots, and map artifacts
- send explicit operator-approved recovery commands

The CLI must not:

- turn an AI proposal into dispatch authority
- infer delivery completion from ACK alone
- hide missing runtime evidence behind successful command output

## Gateway

The Gateway is the network boundary. It may expose routes for:

- conversation and planning
- operator approval
- execution request creation
- task lookup and timeline lookup
- recovery dispatch
- map and status surfaces

The Gateway must preserve task records and timeline events as auditable
evidence. It should fail closed when required approval, dispatch, or runtime
evidence is missing.

## Core

Core packages own shared schemas and claim semantics. They should not import CLI,
Gateway server internals, simulator runtimes, or hardware adapters.

## SITL And Runtime Adapters

Runtime adapters are opt-in execution boundaries. They may produce runtime
evidence, but they must not rewrite prior proposal or approval facts.

## Hardware Adapter Contract

The Real Hardware Bridge v1 starts with hardware-adapter evidence contracts in
`src/runtime/hardware_adapter_contract.py`, a projection from the existing PX4
bench arm/disarm executor, and a bounded ROS2/Nav2 ground-robot adapter wrapper
and Unitree SDK2/MuJoCo adapter wrapper around injected client boundaries. It is
not a field robot, drone, MAVSDK, or outdoor integration.

`HardwareAdapter` is a structural protocol for future adapters. The current
runtime implementation is the existing PX4 props-removed bench executor plus
adapter artifacts plus Nav2 and Unitree client-wrapper slices, not a standalone
fake adapter route.

Hardware adapters must separate:

- capability declaration
- preflight
- dispatch candidate
- operator approval
- dispatch request
- ACK
- progress observation
- completion claim
- completion scope
- physical execution
- safe stop or abort

The contract tests prove that invalid evidence is rejected and that the existing
PX4 bench executor writes structured adapter artifacts for blocked preflight and
for executed arm/disarm results. Executed loopback results write
`missionos_hardware_adapter_evidence` after command send, ACK, and state
readback. Loopback execution remains `physical_execution_invoked=false`; real
serial bench execution remains opt-in and separate from flight or delivery
completion.

The ROS2/Nav2 ground-robot slice can call a supplied `Nav2DispatchClient` after
preflight and approval, but it does not import ROS2 and does not claim physical
execution. The TurtleBot4 simulator bridge may cross an opt-in external ROS2
command boundary and send a bounded Nav2 `NavigateToPose` action goal, but it
can claim only simulator completion and must reject physical-execution or
MissionOS-generated raw velocity/topic-command claims. Diagnostic simulator
shims must be reported separately and cannot satisfy completion without Nav2
success plus odometry motion. The Unitree SDK2/MuJoCo slice can call a supplied `UnitreeSimClient`
after preflight, approval, and opt-in, but it does not import Unitree SDK2,
start MuJoCo, issue raw motor or velocity commands, invoke special motions, or
claim physical execution. Sim completion uses `completion_scope=sim_action`,
not `adapter_action`.

A real ROS2 client wrapper, real Unitree runner, PX4/MAVSDK, cage, or field
adapter must preserve the same claim boundaries and add its own runtime smoke
before it touches any live controller.

## Runtime Recovery Maneuvers

The runtime recovery agent may propose these bounded actions:

- `continue`
- `hold`
- `return_to_launch`
- `land`
- `adjust_altitude`
- `adjust_speed`
- `reroute`
- `avoid_obstacle`
- `operator_review`

`adjust_altitude`, `adjust_speed`, `reroute`, and `avoid_obstacle` require
bounded numeric parameters in the proposal and in the operator-approved request.
The Gateway validates those parameters before queueing a request to the active
AUTO runner. Without an active runner, only the emergency LAND/RTL path may use
the direct emergency dispatcher.

Operator natural-language recovery requests, such as asking to climb or reroute
from `missionos chat`, are proposal requests only. They may ask the Runtime
Recovery Agent planner for bounded parameters, but they must not approve,
dispatch, execute, verify, or count progress. The resulting command still goes
through the standard operator confirmation and Gateway recovery-dispatch route.

Obstacle and building handling must remain source-backed. `avoid_obstacle` may
pass the recovery guardrail only when telemetry includes obstacle or building
risk evidence, and a Gazebo obstacle model is not claimed unless runtime
evidence explicitly shows that such a model was spawned or observed. The AUTO
runtime probe may materialize source-backed obstacles as static Gazebo box
models through `/world/default/create`, but `gazebo_obstacle_model_spawned=true`
requires pose readback from `/world/default/pose/info`; a service request alone
is not enough to claim the model exists.

## Repair Planning

The Repair Agent is a post-block, post-run, or next-run planning coordinator. It
is not the in-flight recovery controller. When a mission is blocked, fails
verification, or ends with incomplete evidence, it may ask the Gateway-owned
`llm_repair_planning` capability for a bounded repair proposal or an
evidence-collection step.

Repair planning may consider source-bound mission evidence such as weather,
battery, payload, speed, altitude, route progress, verifier findings, and
blocking reasons. It must not approve, dispatch, execute, alter a live vehicle,
claim progress, or claim delivery completion. Any proposed change to payload,
speed, altitude, route, retry conditions, or evidence collection still requires
the normal human approval and execution boundaries.

```mermaid
flowchart TD
    O["Operator asks for repair"] --> C["Chief Agent"]
    C --> R["Repair Agent<br/>coordinator"]
    R --> G["Gateway capability handoff"]
    G --> L["LLM Repair Planner<br/>bounded repair proposal"]
    L --> A["Repair artifact<br/>proposal or evidence request"]
    A --> H["Human approval<br/>before any execution"]
```
