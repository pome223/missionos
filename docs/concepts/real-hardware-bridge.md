# Real Hardware Bridge

MissionOS is not trying to let an LLM directly pilot a drone or robot.

The hardware bridge idea is narrower: MissionOS can sit above an existing
autopilot, Nav2 stack, or safety controller. The LLM may propose a bounded
action. A human approves or rejects it. Rules and adapter capabilities constrain
what can be sent. The controller executes. MissionOS records what happened and
what is still unproven.

The first implementation slices add adapter evidence to the existing PX4
props-removed bench executor and add a bounded ROS2/Nav2 ground-robot adapter
wrapper plus a Unitree SDK2/MuJoCo adapter wrapper. In loopback or simulation
tests, MissionOS sends a bounded action through the adapter boundary and records
command send, ACK, state readback or progress, and what remains unproven.
Loopback and simulator evidence are not physical execution; the real serial
bench path remains opt-in.

If the bridge is blocked before command send, MissionOS records that as
structured preflight evidence instead of hiding it behind a generic error.

This v1 slice completes the first evidence paths, not the hardware roadmap. The
Nav2 and Unitree wrappers can call injected client boundaries after approval,
but they do not start ROS2, import Unitree SDK2, start MuJoCo, or claim physical
robot movement. They do not add MAVSDK flight, takeoff, mission upload, payload
release, outdoor operation, or partner hardware onboarding.

The Unitree MuJoCo path now has a readiness check for an operator-provided Go2
simulator checkout. That check reads the local simulator layout and config; it
does not launch the simulator or move a robot.

The next Unitree step adds opt-in checks for SDK2 import, simulator process
launch, and a bounded dispatch bridge. The dispatch bridge still treats MuJoCo
as simulation evidence, not physical execution.

This is not a release promise for outdoor flight or autonomous hardware
operation. Real adapters must be added one boundary at a time, with their own
operator controls, safety case, runtime smoke, and evidence limits.

For PX4, “props removed” is necessary but not sufficient. The bench path also
requires the vehicle to be physically secured, a tested physical stop control
and an immediate power disconnect to be within reach, and a fresh named human
attestation. Those conditions authorize only the bounded bench action. They do
not authorize takeoff, flight, payload release, or mission completion.
