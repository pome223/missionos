# Agent Documentation

This layer is for AI coding agents and maintainers. It is allowed to be detailed.
Prefer explicit field names, route boundaries, runtime checks, and negative
examples over simplified prose.

## Reading Map by Change Target

Use this table to find the minimum you must read before touching code or public
docs.

| If you are changing... | Read these first (in order) |
| --- | --- |
| CLI commands, help text, or operator UX | `contracts.md`, `claim-semantics.md`, `e2e-verification.md`, `cli-parity-release-gate.md` |
| Gateway routes, sessions, or audit | `contracts.md`, `artifact-taxonomy.md`, `publication-rules.md`, `e2e-verification.md` |
| LLM planning, repair, or recovery logic | `claim-semantics.md`, `agent-architecture.md`, `contracts.md` |
| Recovery delegation (reflex/deliberation split, perception claims, shadow measurement, action promotion) | `recovery-delegation-authority.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| Evidence, claims, completion, or verifier behavior | `claim-semantics.md`, `artifact-taxonomy.md`, `contracts.md` |
| PX4 / Gazebo SITL or PX4 bench-adjacent runtime | `px4-gazebo-route-runtime.md`, `hardware-adapter-contract.md`, `claim-semantics.md`, `e2e-verification.md`, `publication-rules.md` |
| TurtleBot3 / ROS2 Nav2 simulator adapter | `ros2-nav2-turtlebot3-sim.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| TurtleBot4 / other Nav2 simulator work | `ros2-nav2-turtlebot4-sim.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| NVIDIA Isaac Sim / Nova Carter work | `nvidia-isaac-nova-carter.md`, `claim-semantics.md`, `hardware-adapter-contract.md`, `publication-rules.md` |
| Nvblox / perception evidence | `nvblox-perception-evidence.md`, `claim-semantics.md`, `artifact-taxonomy.md`, `publication-rules.md` |
| Real hardware adapter or partner bridge | `hardware-adapter-contract.md`, `hardware-partner-integration-guide.md`, `publication-rules.md` |
| Unitree / MuJoCo or other robot | `unitree-mujoco-environment.md`, `unitree-go2-real-hardware.md`, `hardware-adapter-contract.md` |
| Public docs, README, examples, or screenshots | `publication-rules.md`, `contracts.md`, `claim-semantics.md`, and the human `docs/concepts/boundaries.md` |
| Local LLM backends or model config | `local-llm-backends.md`, `contracts.md` |
| Anything that ships to the public snapshot | `publication-rules.md`, `e2e-verification.md`, `claim-semantics.md` |

## Full Reference

When the map above is not enough, read the complete set:

- `contracts.md`
- `agent-architecture.md`
- `claim-semantics.md`
- `recovery-delegation-authority.md`
- `artifact-taxonomy.md`
- `e2e-verification.md`
- `cli-parity-release-gate.md`
- `local-llm-backends.md`
- `publication-rules.md`
- `legacy-codename-rename-plan.md`
- `hardware-adapter-contract.md`
- `px4-gazebo-route-runtime.md`
- `hardware-partner-integration-guide.md`
- `ros2-nav2-turtlebot3-sim.md`
- `ros2-nav2-turtlebot4-sim.md`
- `nvidia-isaac-nova-carter.md`
- `nvblox-perception-evidence.md`
- `unitree-mujoco-environment.md`
- `unitree-go2-real-hardware.md`
- `recovery-intent-compiler-verifier.md`

## Working Rule

When a change touches CLI, Gateway, runtime adapters, task state, evidence, or
public-facing docs, update the relevant human-facing and agent-facing docs in
the same change.
