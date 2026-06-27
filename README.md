# MissionOS

One LLM-assisted mission conversation today. Simulator and runtime paths are
present, but opt-in and evidence-bounded. The control loop has to keep its
authority boundaries clear as the system grows.

That is the bet behind MissionOS.

MissionOS is an LLM mission-control boundary system for AI-assisted physical
missions. The LLM reads the situation, plans the route, adapts when conditions
change, and proposes the safest path to mission success. Humans approve. Rules
constrain. Evidence verifies what actually happened.

You are not the pilot. You are the controller, working *with* MissionOS to bring a mission home safely. A destination is given. MissionOS reads wind, distance, battery, terrain, and route, then proposes a plan — and when conditions shift mid-mission, it re-judges toward success: raise altitude here, detour around that ridge, land at a safe point instead of pushing on, or return home while there is still reserve. You approve. It never self-approves or dispatches itself.

The point is safe success, not honest failure. Honesty and evidence are the instruments that make safe success trustworthy, not the goal. Precisely because an LLM is doing the judging, MissionOS stays exact about what that judgment has and has not earned: what was proposed, what was approved, what was sent, what was observed, and what remains unproven. An acknowledgement is not observed progress, and observed progress is not mission completion.

Most agent demos stop once something moves. Real missions do not. A command gets acknowledged, yet nothing happens. Wind shifts the plan. Recovery is proposed and approved, but observed progress never arrives. MissionOS is built for that world.

## Disclaimer

MissionOS is reference software for AI-assisted mission-control research and
simulation. It is not a certified autopilot, flight controller, safety system,
legal compliance system, or substitute for a qualified human operator.

Do not use MissionOS to operate drones, robots, vehicles, or other physical
systems unless you have independent safety controls, appropriate supervision,
applicable permissions, and a test environment designed for that use. Live SITL,
PX4/Gazebo, MAVLink, and hardware-related paths are opt-in surfaces and must be
treated as experimental.

MissionOS separates AI proposals, human approval, constrained dispatch,
runtime observations, verifier evidence, and completion claims. A proposal,
approval, ACK, simulator observation, or map display is not proof of safe
physical execution or delivery completion.

## Current Status

MissionOS is an early public snapshot. The primary public path documented here
is the LLM-in-the-loop chat surface:

```bash
missionos chat --autostart
```

MissionOS is valuable when an LLM is part of the loop: the LLM judges, plans,
diagnoses, and proposes bounded mission actions; humans approve; Gateway and
rules keep approval, dispatch, runtime evidence, and completion claims separate.

| Surface | Public status | Notes |
|---------|---------------|-------|
| `missionos chat --autostart` | Primary tested path | Use with an explicit LLM backend. |
| `missionos play` | Present, experimental | Deterministic sandbox; not the main LLM loop. |
| `missionos tutorial` | Present, unverified as a public entrypoint | Do not treat as quickstart. |
| local mock/fixture Gateway paths | Maintainer boundary tests | Useful for contract checks, not product demos. |
| live SITL / PX4 / Gazebo | Opt-in experimental runtime | Requires local Docker/PX4/Gazebo preparation and operator approval. |

Not claimed:

- physical execution
- real hardware flight
- delivery completion
- general-purpose destination planning
- simulator observation as proof of real-world success

The repository also includes copied backend/runtime modules, simulator helpers,
and curated fixture/golden assets. Those are useful for development and
boundary verification, but they are not the public first-run story.

The copied production backend is opt-in:

```bash
missionos gateway start --enable-live-sitl
missionos chat --autostart --enable-live-sitl
```

Live SITL still requires the local Docker/PX4/Gazebo environment, an explicit
runtime opt-in, and explicit operator approval. It does not prove physical
execution or delivery completion.

## What MissionOS Does

An operator gives an instruction. MissionOS turns it into a bounded path with evidence at every step — not a single success banner at the end.

1. Planning agents read the facts and context, judge the situation, choose a response, compute its parameters and route, and propose it as a bounded mission action.
2. A human operator approves or rejects the proposed action.
3. Rules and gates constrain what can be dispatched.
4. Executors run only approved bounded actions.
5. Verifiers record evidence from task records, timelines, runtime snapshots, acknowledgements, maps, or simulator/hardware readback.
6. When evidence shows a problem, recovery agents diagnose the cause, design the next response and its parameters, and propose it as the next bounded action. Repair loops continue until the mission is resolved or stopped.

**Core principle:**

> The LLM owns mission intelligence — situation judgment, response selection, parameter computation, planning, diagnosis, and repair. Humans own approval. Rules own guardrails only. So the LLM may judge and propose, but it never self-approves, dispatches, or turns evidence into a success claim. An ACK is not observed progress, and observed progress is not mission completion.

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

## Experimental Surfaces

These commands are present in the repository, but they are not the primary
tested public path.

### `missionos play`

`missionos play` is an experimental deterministic lab. It does not currently
represent the main LLM mission-control loop, and it should not be treated as
the tested public quickstart.

### `missionos tutorial`

`missionos tutorial` is present, but it is not documented as a verified public
entrypoint in this snapshot.

### Local mock/fixture Gateway paths

The local fixture-backed Gateway routes are maintainer-facing boundary tests.
They can be useful for contract checks because they avoid private state and
live runtime effects, but they should not be presented as the product demo.

### Live SITL / PX4 / Gazebo

Live SITL paths are experimental and opt-in. They may use source-backed route
planning, PX4/Gazebo, MAVLink upload, live-weather inputs, runtime recovery,
and companion views, but each boundary must be verified in the local
environment before making claims from it.

Historical SITL runs have shown useful evidence such as route progress,
recovery proposals, and simulated payload events. They remain simulator
evidence only: no live SITL run proves physical execution or delivery
completion.

For the current agent map, see `docs/concepts/agent-roles.md`. In short:
`missionos chat` is the Chief Agent surface for planning and `/repair`,
`missionos operate` is the active Runtime Recovery surface, and Gateway remains
the approval, dispatch, execution, and evidence boundary.

## Why MissionOS Exists

Most current AI agent work stops at "the agent did something."

In the real world, that is not enough.

Physical agents need mission control, not a chatbot with a joystick.

When delivery drones, inspection robots, and field agents operate together, some missions stall, some commands are accepted but produce no movement, and some recoveries succeed while others do not. A control layer that treats every ACK as success will not survive this environment.

MissionOS exists because those situations require a system that stays honest. The current work exercises that boundary discipline in single-mission PX4 SITL loops, and uses the same separation as the design constraint for future multi-vehicle operations. Planning, recovery, rules, execution, and verification must remain separate roles.

Without that separation, mission control is unlikely to scale beyond demonstrations.

The gap between "the agent did something" and "we can explain what actually happened" is where physical AI either matures or collapses under ambiguity. MissionOS is being built to close that gap without turning weak evidence into confident success claims.

## Historical Experiment Notes

These notes come from development mission loops and record where boundaries
held or broke. They are not public quickstarts, and each claim remains limited
to the evidence named in the row.

| Experiment              | Environment          | Observed evidence                        | Not claimed                       |
|-------------------------|----------------------|------------------------------------------|-----------------------------------|
| Strong-wind recovery    | PX4 SITL, 9 m/s wind | Recovery proposal → approval → dispatch path | Return progress was not observed |
| AUTO delivery probe     | PX4/Gazebo SITL      | Route completion, dropoff, ACK, RTL      | Physical delivery completion     |
| Cargo release           | Gazebo L1 cargo      | Payload separation observed              | Real hardware release            |
| Play live-weather recovery | PX4/Gazebo SITL, real Open-Meteo surface + altitude-profile wind, real-gust turbulence, relative-airflow drag, rotor-coupled battery | LLM read wind + drift → proposed return_to_launch, human approval required; battery drains with rotor effort | Drag uses the vehicle's own velocity (relative airflow) but a modelled coefficient; turbulence dynamics modelled (amplitude is real gust); battery is a sim signal not hardware endurance; no delivery/physical execution |
| Play delivery in wind | PX4/Gazebo SITL, MAVLink waypoint mission, gust-driven turbulence | simulated takeoff → dropoff → detachable cargo-joint separation → return, weather-driven wind, cross-track tracked | Sim cargo not real hardware; delivery completion not claimed |
| Play GPS-denied | PX4/Gazebo SITL, EKF GPS fusion disabled | xy position estimate goes invalid (position_trustworthy=false) and surfaces to the recovery agent | SDF sensor-noise scaling and PX4 failure-injection do NOT degrade the GPS-dominated estimate here; only EKF GPS denial does; sim EKF signal, not real GNSS |

More experiments will be added as the public extraction progresses.

## Repository Layout

```text
packages/
  missionos-cli/       Operator CLI
  missionos-gateway/   HTTP and WebSocket Gateway
  missionos-core/      Shared schemas and claim semantics
  missionos-sitl/      Simulator adapters and fixture runtime
src/                   Copied MissionOS backend and runtime modules
scripts/               Runtime smoke scripts and maintenance utilities
simulators/            Gazebo/PX4 helper code and simulator plugins
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

The repository now carries local mock/fixture boundary-test paths and copied
production runtime code. Mock/fixture paths are for maintainers and contract
checks, not the public product demo. Anything that starts Docker, PX4/Gazebo,
MAVLink upload, or live flight must remain opt-in and evidence-bounded.

## License

MissionOS is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE).
