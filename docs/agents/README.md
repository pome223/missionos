# Agent Documentation

This layer is for AI coding agents and maintainers. It is allowed to be detailed.
Prefer explicit field names, route boundaries, runtime checks, and negative
examples over simplified prose.

## Reading Map by Change Target

Use this table to find the minimum you must read before touching code or public
docs.

| If you are changing... | Read these first (in order) |
| --- | --- |
| CLI commands, help text, or operator UX | `contracts.md`, `claim-semantics.md`, `e2e-verification.md`, `missionos-chat-pr-merge-e2e-checklist.md`, `cli-parity-release-gate.md` |
| Gateway routes, sessions, or audit | `gateway-profiles.md`, `contracts.md`, `artifact-taxonomy.md`, `publication-rules.md`, `e2e-verification.md`, `missionos-chat-pr-merge-e2e-checklist.md` |
| LLM planning, repair, or recovery logic | `claim-semantics.md`, `agent-architecture.md`, `contracts.md` |
| Recovery delegation (reflex/deliberation split, perception claims, shadow measurement, action promotion) | `recovery-delegation-authority.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| Nav2 Core Recovery candidate divergence | `nav2-core-recovery-divergence.md` |
| Evidence, claims, completion, or verifier behavior | `claim-semantics.md`, `artifact-taxonomy.md`, `contracts.md` |
| GR00T / LeRobot native Repair cohort accounting or publication | `groot-lerobot-native-single-attempt-cohort.md`, `groot-lerobot-semantic-repair-checkpoint-gate.md`, `publication-rules.md`, `claim-semantics.md` |
| GR00T / LeRobot instruction delivery or local language sensitivity | `groot-lerobot-language-conditioning-probe.md`, `groot-lerobot-native-single-attempt-cohort.md`, `claim-semantics.md`, `publication-rules.md` |
| Anonymized recovery replay publication or verification | `replay-bundle-contract.md`, `publication-rules.md`, `claim-semantics.md` |
| PX4 / Gazebo SITL or PX4 bench-adjacent runtime | `px4-gazebo-route-runtime.md`, `px4-sim-to-hardware-portability.md`, `hardware-adapter-contract.md`, `claim-semantics.md`, `e2e-verification.md`, `publication-rules.md` |
| PX4 SITL 成果を実機（bench/HITL/field）に載せる・作り直し境界 | `px4-sim-to-hardware-portability.md`, `hardware-adapter-contract.md`, `recovery-intent-compiler-verifier.md`, `claim-semantics.md`, `hardware-partner-integration-guide.md` |
| PX4 bench を第三の Core conformance backend にする作業 | `bench-conformance-corpus-design.md`, `px4-sim-to-hardware-portability.md`, `hardware-adapter-contract.md`, `action-feasibility-conformance-corpus.md`, `claim-semantics.md`, `publication-rules.md` |
| TurtleBot3 / ROS2 Nav2 simulator adapter or CLI companions | `turtlebot3-recovery-contracts.md`, `turtlebot3-nav2-execution.md`, `turtlebot3-cli-companions.md`, `ros2-nav2-turtlebot3-sim.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| TurtleBot camera/LiDAR perception or 3D obstacle clearance | `perception-corroboration-binding.md`, `trajectory-clearance-3d.md`, `claim-semantics.md`, `publication-rules.md` |
| TurtleBot4 / other Nav2 simulator work | `ros2-nav2-turtlebot4-sim.md`, `claim-semantics.md`, `hardware-adapter-contract.md` |
| NVIDIA Isaac Sim / Nova Carter work | `nvidia-isaac-nova-carter.md`, `claim-semantics.md`, `hardware-adapter-contract.md`, `publication-rules.md` |
| Nvblox / perception evidence | `nvblox-perception-evidence.md`, `claim-semantics.md`, `artifact-taxonomy.md`, `publication-rules.md` |
| Real hardware adapter or partner bridge | `backend-neutral-adapter-runtime.md`, `hardware-adapter-contract.md`, `hardware-partner-integration-guide.md`, `px4-sim-to-hardware-portability.md`, `publication-rules.md` |
| Unitree / MuJoCo or other robot | `backend-neutral-adapter-runtime.md`, `unitree-mujoco-environment.md`, `unitree-go2-real-hardware.md`, `hardware-adapter-contract.md` |
| Public docs, README, examples, or screenshots | `publication-rules.md`, `contracts.md`, `claim-semantics.md`, and the human `docs/concepts/boundaries.md` |
| Local LLM backends or model config | `local-llm-backends.md`, `contracts.md` |
| Anything that ships to the public snapshot | `publication-rules.md`, `e2e-verification.md`, `missionos-chat-pr-merge-e2e-checklist.md`, `claim-semantics.md` |

## Full Reference

When the map above is not enough, read the complete set:

- `contracts.md`
- `agent-architecture.md`
- `claim-semantics.md`
- `recovery-delegation-authority.md`
- `artifact-taxonomy.md`
- `e2e-verification.md`
- `missionos-chat-pr-merge-e2e-checklist.md`
- `gateway-profiles.md`
- `cli-parity-release-gate.md`
- `local-llm-backends.md`
- `publication-rules.md`
- `legacy-codename-rename-plan.md`
- `hardware-adapter-contract.md`
- `px4-gazebo-route-runtime.md`
- `px4-sim-to-hardware-portability.md` — SITL→実機の KEEP/REWIRE/REWRITE 棚卸し
- `bench-conformance-corpus-design.md` — PX4 bench を 3 つ目の conformance backend にする設計案（未実装）
- `hardware-partner-integration-guide.md`
- `ros2-nav2-turtlebot3-sim.md`
- `perception-corroboration-binding.md`
- `trajectory-clearance-3d.md`
- `turtlebot3-recovery-contracts.md`
- `turtlebot3-nav2-execution.md`
- `turtlebot3-cli-companions.md`
- `ros2-nav2-turtlebot4-sim.md`
- `nvidia-isaac-nova-carter.md`
- `nvblox-perception-evidence.md`
- `unitree-mujoco-environment.md`
- `unitree-go2-real-hardware.md`
- `recovery-intent-compiler-verifier.md`
- `backend-neutral-adapter-runtime.md`
- `replay-bundle-contract.md`
- `groot-lerobot-native-single-attempt-cohort.md` — bounded `0/5` Repair result and Contract-bound `16/16` preservation accounting
- `groot-lerobot-language-conditioning-probe.md` — inference-only A/A/B evidence and its claim boundary
- `repository-status.md` — whole-repo status snapshot (maturity, risks, next work)

## Working Rule

When a change touches CLI, Gateway, runtime adapters, task state, evidence, or
public-facing docs, update the relevant human-facing and agent-facing docs in
the same change.

## ADK v2 Migration Contracts

- `docs/agents/adk-v2-primary-promotion.md`
- `docs/agents/adk-v2-session-persistence.md`
- `docs/agents/adk-v2-canonical-approval-hitl.md`
- `docs/agents/adk-v2-guarded-execution.md`
- `docs/agents/adk-v2-bounded-recovery.md`
- `docs/agents/adk-v2-control-loop.md`
- `docs/agents/adk-v2-legacy-gateway.md`
- `docs/agents/adk-v2-runtime-inventory.md`
