# Agent Documentation

This layer is for AI coding agents and maintainers. It is allowed to be detailed.
Prefer explicit field names, route boundaries, runtime checks, and negative
examples over simplified prose.

## Required Reading

1. `docs/agents/contracts.md`
2. `docs/agents/agent-architecture.md`
3. `docs/agents/claim-semantics.md`
4. `docs/agents/artifact-taxonomy.md`
5. `docs/agents/e2e-verification.md`
6. `docs/agents/cli-parity-release-gate.md`
7. `docs/agents/local-llm-backends.md`
8. `docs/agents/publication-rules.md`
9. `docs/agents/legacy-codename-rename-plan.md`
10. `docs/agents/hardware-adapter-contract.md`
11. `docs/agents/hardware-partner-integration-guide.md`
12. `docs/agents/ros2-nav2-turtlebot3-sim.md`
13. `docs/agents/ros2-nav2-turtlebot4-sim.md`
14. `docs/agents/unitree-mujoco-environment.md`
15. `docs/agents/unitree-go2-real-hardware.md`

## Working Rule

When a change touches CLI, Gateway, runtime adapters, task state, evidence, or
public-facing docs, update the relevant human-facing and agent-facing docs in
the same change.
