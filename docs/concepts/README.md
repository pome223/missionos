# MissionOS Concepts

MissionOS is a mission control plane. Its job is to keep the operator, AI
proposal, safety constraints, execution boundary, verifier evidence, and repair
loop separate enough that the system can stay honest.

## The Simple Model

```text
AI proposes.
Human approves.
Rules constrain.
Executor acts.
Verifier records evidence.
Repair loop proposes the next response.
```

The important point is not that every mission succeeds. The important point is
that MissionOS should be able to say what was proposed, what was approved, what
was sent, what was observed, and what remains unproven.

## Core Surfaces

- CLI: an operator-facing command line for planning, approval, execution, status,
  recovery, and map viewing.
- Gateway: the network boundary used by CLI, UI, agents, and runtime workers.
- Core contracts: shared schemas and semantics for tasks, evidence, approval,
  dispatch, and verifier output.
- Simulator adapters: opt-in runtime paths for mock/fixture boundary checks and
  SITL validation.

## Where To Go Next

- Read `docs/concepts/what-happens-in-a-run.md` for the single clearest picture of
  proposal -> approval -> dispatch -> evidence -> claim.
- Read `docs/concepts/boundaries.md` for the claim boundary in plain language.
- Read `docs/concepts/agent-roles.md` for the plain-language map of Chief,
  Runtime Recovery, Repair, and Gateway responsibilities.
- Read `docs/concepts/simulator-baseline.md` for why the current indoor ROS2/Nav2
  simulator baseline uses TurtleBot3 while TurtleBot4 remains opt-in.
- Read `docs/concepts/recovery-intent-compilation.md` for how Recovery judgment,
  compilation, reachability, approval, execution, and outcome verification stay
  separate.
- Read `docs/concepts/turtlebot3-recovery-boundaries.md` for the same separation
  in the TurtleBot3 Recovery loop.
- Read `docs/concepts/replay-evidence.md` for publication-safe,
  machine-readable Recovery evidence and its limits.
- Read `docs/concepts/real-hardware-bridge.md` for the first contract-first
  hardware bridge slices and their limits.
- Read `docs/concepts/recovery-delegation.md` for the opt-in two-phase
  reflex/deliberation recovery model, perception claims, shadow
  measurement, and action promotion.
- Read `docs/concepts/backend-neutral-adapters.md` for the shared approval,
  dispatch, observation, and verification flow across robot backends.
- Read `docs/concepts/action-feasibility.md` for how common evidence and
  robot-specific calculations produce a tri-state result without authority.
- Read `docs/concepts/groot-language-conditioning.md` for the inference-only
  A/A/B check that separates instruction delivery from local model sensitivity.
- Read `docs/concepts/groot-snapshot-recoverability.md` for the bounded control
  that separates a policy's negative result from an unrecoverable saved state.
- Read `docs/examples/README.md` for planned scenario writeups and their
  verification requirements.
- Read `docs/agents/README.md` if you are an AI agent or maintainer changing the
  code.
