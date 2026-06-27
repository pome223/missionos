# MissionOS

MissionOS is a control CLI for LLM-assisted drone missions.

The LLM proposes routes and recovery actions. A human approves them. MissionOS
records what was proposed, what was approved, what was sent to the execution
boundary, and what was actually observed.

The point is not to hand the LLM a joystick. The point is to let the LLM judge,
plan, and propose while MissionOS keeps approval, dispatch, ACK, observed
progress, and completion as separate facts. An ACK is not success. Observed
progress is not mission completion.

## Disclaimer

MissionOS is reference software for AI-assisted mission-control research and
simulation. It is not a certified autopilot, flight controller, safety system,
legal compliance system, or substitute for a qualified human operator.

Do not use MissionOS to operate drones, robots, vehicles, or other physical
systems unless you have independent safety controls, appropriate supervision,
applicable permissions, and a test environment designed for that use. Simulation
and hardware-related paths are opt-in surfaces and must be treated as
experimental.

MissionOS separates AI proposals, human approval, constrained dispatch,
runtime observations, verifier evidence, and completion claims. A proposal,
approval, ACK, simulator observation, or map display is not proof of safe
physical execution or delivery completion.

## Current Status

MissionOS is an early public snapshot. The public path documented here is:

```bash
missionos chat --autostart
```

Use it with an explicit LLM backend. Other runtime and simulator paths exist in
the repository, but they are not presented as public quickstarts in this
snapshot unless separately verified.

MissionOS does not claim:

- unchecked LLM control
- physical execution
- real hardware flight
- delivery completion
- observed progress without evidence
- general-purpose destination planning

## What MissionOS Does

1. You ask MissionOS for a drone mission or recovery decision.
2. The LLM proposes a route or bounded recovery action.
3. A human operator approves or rejects the proposal.
4. MissionOS sends only approved actions through the execution boundary.
5. MissionOS records proposal, approval, command send, ACK, observed progress,
   and completion separately.
6. If evidence shows a problem, the LLM can propose the next repair or recovery
   action. It still cannot approve or execute by itself.

**Core principle:**

> LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks.
> Repair loops.

## Chat Quickstart

Install the repository and CLI packages (requires Python 3.11+):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . -e packages/missionos-gateway -e packages/missionos-cli
missionos --help
```

Configure an LLM backend before running the intended MissionOS experience.
Gemini is the fastest hosted path; Ollama/Gemma is a local no-spend path that
can be slower and may need longer timeouts.

```bash
cp .env.example .env
# For the intended chat path, edit .env:
# MISSIONOS_LLM_BACKEND=gemini
# GOOGLE_API_KEY=...
#
# Or use local Ollama/Gemma:
# MISSIONOS_LLM_BACKEND=ollama
# MISSIONOS_OLLAMA_MODEL=gemma4:26b
```

Then start chat:

```bash
missionos chat --autostart
```

`MISSIONOS_LLM_BACKEND=off` exists for development fallbacks and boundary tests.
It is not the main MissionOS product experience.

## Why MissionOS Exists

Most current AI agent work stops at "the agent did something."

In the real world, that is not enough.

Physical agents need mission control, not a chatbot with a joystick.

When a mission stalls, an LLM can propose a recovery. MissionOS should not treat
that proposal as approval, execution, or success.

MissionOS exists because those situations require a system that stays honest.
Planning, recovery, rules, execution, and verification must remain separate
roles.

Without that separation, mission control is unlikely to scale beyond demonstrations.

The gap between "the agent did something" and "we can explain what actually happened" is where physical AI either matures or collapses under ambiguity. MissionOS is being built to close that gap without turning weak evidence into confident success claims.

## Repository Layout

```text
packages/
  missionos-cli/       Operator CLI
  missionos-gateway/   HTTP and WebSocket Gateway
  missionos-core/      Shared schemas and claim semantics
  missionos-sitl/      Simulator adapters
src/                   Copied MissionOS backend and runtime modules
scripts/               Runtime smoke scripts and maintenance utilities
simulators/            Simulator helper code and plugins
config/                Runtime configuration templates
docs/
  concepts/            Human-readable explanations
  examples/            Scenario writeups
  agents/              Detailed contracts for AI agents and maintainers
tests/
  contract/            Schema and contract tests
  e2e/                 Runtime boundary tests
```

## Documentation Guide

Start with the concepts if you want the reasoning. Jump to the packages if you want to run something.

**Concepts**

- [docs/concepts/README.md](docs/concepts/README.md)
- [docs/concepts/boundaries.md](docs/concepts/boundaries.md)

**Packages**

- [packages/missionos-cli/README.md](packages/missionos-cli/README.md)
- [packages/missionos-gateway/README.md](packages/missionos-gateway/README.md)
- [packages/missionos-core/README.md](packages/missionos-core/README.md)
- [packages/missionos-sitl/README.md](packages/missionos-sitl/README.md)

**For agents and maintainers**

- [docs/agents/README.md](docs/agents/README.md)
- [docs/agents/contracts.md](docs/agents/contracts.md)
- [docs/agents/claim-semantics.md](docs/agents/claim-semantics.md)
- [docs/agents/artifact-taxonomy.md](docs/agents/artifact-taxonomy.md)
- [docs/agents/e2e-verification.md](docs/agents/e2e-verification.md)
- [docs/agents/cli-parity-release-gate.md](docs/agents/cli-parity-release-gate.md)
- [docs/agents/publication-rules.md](docs/agents/publication-rules.md)
- [docs/agents/legacy-codename-rename-plan.md](docs/agents/legacy-codename-rename-plan.md)

## Documentation Layers

Human-facing docs (`docs/concepts/`, `docs/examples/`) should be short and readable.
Agent-facing docs (`docs/agents/`) should be precise enough for automated agents to modify the code without breaking core boundaries.

## Initial Development

The repository also carries simulator/runtime modules and maintenance scripts.
Anything that starts simulation or hardware-adjacent execution must remain
opt-in and evidence-bounded.

## License

MissionOS is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE).
