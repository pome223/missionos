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

- Read `docs/concepts/boundaries.md` for the claim boundary in plain language.
- Read `docs/concepts/agent-roles.md` for the plain-language map of Chief,
  Runtime Recovery, Repair, and Gateway responsibilities.
- Read `docs/examples/README.md` for planned scenario writeups and their
  verification requirements.
- Read `docs/agents/README.md` if you are an AI agent or maintainer changing the
  code.
