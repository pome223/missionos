# MissionOS

One drone in a simulator today. More vehicles and more complex missions later. The control loop has to keep its authority boundaries clear as the system grows.

That is the bet behind MissionOS.

MissionOS is a mission control game and runtime for AI-assisted physical missions. The AI reads the situation, plans the route, adapts when conditions change, and proposes the safest path to mission success. Humans approve. Rules constrain. Evidence verifies what actually happened.

You are not the pilot. You are the controller, working *with* MissionOS to bring a mission home safely. A destination is given. MissionOS reads wind, distance, battery, terrain, and route, then proposes a plan — and when conditions shift mid-mission, it re-judges toward success: raise altitude here, detour around that ridge, land at a safe point instead of pushing on, or return home while there is still reserve. You approve. It never self-approves or dispatches itself.

The point is safe success, not honest failure. Honesty and evidence are the instruments that make safe success trustworthy, not the goal. Precisely because an LLM is doing the judging, MissionOS stays exact about what that judgment has and has not earned: what was proposed, what was approved, what was sent, what was observed, and what remains unproven. An acknowledgement is not observed progress, and observed progress is not mission completion.

Most agent demos stop once something moves. Real missions do not. A command gets acknowledged, yet nothing happens. Wind shifts the plan. Recovery is proposed and approved, but observed progress never arrives. MissionOS is built for that world.

## Current Status

MissionOS is currently a publication candidate extracted from a private
experiment history. It now includes the operator CLI, fixture
Gateway, copied MissionOS backend, PX4/Gazebo SITL runtime modules, scripts,
simulator helpers, and curated fixture/golden assets.

The default Gateway remains fixture-backed for safe demos. The copied production
backend is opt-in:

```bash
missionos gateway start --enable-live-sitl
missionos chat --autostart --enable-live-sitl
```

Live SITL still requires the local Docker/PX4/Gazebo environment and explicit
operator approval. Fixture runs do not prove delivery completion or physical
execution.

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

## CLI Quickstart

Install the local CLI packages (requires Python 3.11+):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e packages/missionos-gateway -e packages/missionos-cli
missionos --help
```

Configure the environment. The fixture Gateway and the smoke test below run
without any key. The default `.env.example` keeps LLM-backed ADK calls off so
first-run demos use deterministic fallbacks unless you explicitly choose a
backend. Use Gemini for the fastest hosted API path, or Ollama/Gemma for a
local no-spend path that can be slower.

```bash
cp .env.example .env
# Default: MISSIONOS_LLM_BACKEND=off
# Gemini/API opt-in: MISSIONOS_LLM_BACKEND=gemini plus GOOGLE_API_KEY
# Local Gemma opt-in: MISSIONOS_LLM_BACKEND=ollama plus MISSIONOS_OLLAMA_MODEL=gemma4:26b
```

Then play a mission. `missionos play` is a deterministic mission-control lab —
no Gateway, Docker, network, or API key required. You turn the knobs; MissionOS
shows how the situation changes and proposes the safest path to success:

```bash
missionos play
```

```text
play> altitude 2960     # too low: clearance falls under the safety rule
play> altitude 3150     # clears the ridge, but return reserve drops
play> compare           # +clearance vs −battery: going higher is never free
play> route west        # detour skirts a lower ridge, at a distance cost
play> approve           # you are the controller; MissionOS never self-approves
```

MissionOS reads each plan and recommends — raise altitude, reroute, return to
home while reserve remains — and rules block anything past the vehicle envelope.

### Live SITL: real weather, real flight, real recovery

`missionos play --real-weather` goes further. It pulls live local weather for
the scenario — the hourly surface forecast *and* a real multi-height wind
profile (Open-Meteo 10/80/120/180 m) — and `fly` launches a real headless
PX4/Gazebo SITL flight, injecting that weather as wind on the vehicle *during*
flight: recomputed each step from the forecast (time-varying) and read from the
real profile at the drone's current altitude (altitude-varying). When the
profile is unavailable it falls back to a modelled power-law lift, and the
capability matrix records which was used (`real_forecast_profile` vs
`modelled_power_law`) — so the sim run is honest evidence about what weather it
actually reproduced.

```bash
# needs Docker + the px4io/px4-sitl-gazebo image
# opt-in: --battery-coupling (rotor-load battery), --gps-denied (EKF GPS off)
missionos play --real-weather --flight-duration 24 --wind-step 3 --battery-coupling
play> fly
```

When wind pushes the drone off track, the runtime recovery agent reads
the telemetry and proposes a bounded action — you approve it. From a real
strong-wind run:

```text
wind 14.7 m/s @ ~39 m AGL · route deviation above limit
recovery agent → return_to_launch
  triggers: wind_speed_exceeds_limit, route_deviation_exceeds_limit
  requires_human_approval: true
  physical_execution_invoked: false   delivery_completion_claimed: false
```

The agent advises; the human approves; rules constrain; nothing self-dispatches
or claims delivery. The live recovery agent uses ADK when
`MISSIONOS_AGENT_RUNTIME_ADK_ENABLED=1`. By default,
`MISSIONOS_LLM_BACKEND=off` keeps LLM-backed ADK calls disabled and uses
deterministic fallbacks where available. `MISSIONOS_LLM_BACKEND=gemini` is the
hosted API opt-in. `MISSIONOS_LLM_BACKEND=ollama` routes ADK model calls to a
local Ollama/LiteLLM backend and does not propagate `GOOGLE_API_KEY` to the
Gateway child process. Previous Gemma 4 26B MoE verification showed that the
Chief/planning function-tool path can work locally with longer timeouts, while
the live Runtime Recovery loop was too slow and JSON-mode fragile for repeated
in-flight use. Use per-agent env overrides only when that tradeoff is
intentional. Without an enabled LLM backend, play falls back to bundled weather
and a deterministic recommendation rather than fabricating one.
The LLM Repair Planner is proposal-only and is enabled for CLI-managed Gateway
sessions with `MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED=1`; it can draft a
next-run repair proposal from blocked evidence, but it never approves,
dispatches, executes, or counts progress.

For the current agent map, see `docs/concepts/agent-roles.md`. In short:
`missionos chat` is the Chief Agent surface for planning and `/repair`,
`missionos operate` is the active Runtime Recovery surface, and Gateway remains
the approval, dispatch, execution, and evidence boundary.

Opt-in **battery coupling** makes the energy real too: the
`motor_load_battery_coupler` gz-sim plugin discharges the simulated battery in
proportion to actual rotor effort, so fighting wind draws more current and
drains faster (verified idle ~0.48 A → hover ~8.6 A). It is a *simulated*
endurance signal — physics-coupled to rotor load, but recorded as
`real_hardware_endurance_evidence: false`, not real power-module evidence.

Run a safe fixture smoke test:

```bash
missionos gateway stop
missionos gateway start
missionos status
missionos say "Check the fixture Gateway"
missionos job-status --task-id task_fixture_delivery
missionos map --task-id task_fixture_delivery --snapshot --no-open
missionos gateway stop
```

Fixture mode is deterministic. It checks the CLI/Gateway/status/map path, but it
does not geocode real destinations, start Docker, upload a mission, claim
delivery completion, or invoke physical execution.

Try source-backed route planning with the copied production backend:

```bash
missionos gateway restart --enable-live-sitl
missionos say "New York Public Library -> Brooklyn Bridge"
```

The production backend resolves the two place names through source-backed route
tools and returns a bounded proposal. It still does not approve, prepare SITL,
dispatch, or count progress from the planning command alone.

When an interactive `missionos chat` session reaches `fly` / `/execute-sitl`,
MissionOS opens companion terminals for `missionos operate`, `missionos watch`,
and `missionos map` for the active task. Those companion views are read-only or
operator-gated surfaces, and the chat session closes the companion terminals it
started when chat exits. Use `--no-companion-terminals` or
`MISSIONOS_CHAT_COMPANION_TERMINALS=0` to disable that desktop UX.

Or let chat start an interactive session:

```bash
missionos chat --autostart
missionos chat --autostart --enable-live-sitl
```

When the production backend is running, `missionos gateway status` shows:

```text
Live SITL env  enabled
Backend        production
```

## Why MissionOS Exists

Most current AI agent work stops at "the agent did something."

In the real world, that is not enough.

Physical agents need mission control, not a chatbot with a joystick.

When delivery drones, inspection robots, and field agents operate together, some missions stall, some commands are accepted but produce no movement, and some recoveries succeed while others do not. A control layer that treats every ACK as success will not survive this environment.

MissionOS exists because those situations require a system that stays honest. The current work exercises that boundary discipline in single-mission PX4 SITL loops, and uses the same separation as the design constraint for future multi-vehicle operations. Planning, recovery, rules, execution, and verification must remain separate roles.

Without that separation, mission control is unlikely to scale beyond demonstrations.

The gap between "the agent did something" and "we can explain what actually happened" is where physical AI either matures or collapses under ambiguity. MissionOS is being built to close that gap without turning weak evidence into confident success claims.

## Experiments

These ideas come from running actual mission loops and recording where the boundaries broke.

| Experiment              | Environment          | What was proven                          | What was not claimed              |
|-------------------------|----------------------|------------------------------------------|-----------------------------------|
| Strong-wind recovery    | PX4 SITL, 9 m/s wind | Recovery proposal → approval → dispatch path | Return progress was not observed |
| AUTO delivery probe     | PX4/Gazebo SITL      | Route completion, dropoff, ACK, RTL      | Physical delivery completion     |
| Cargo release           | Gazebo L1 cargo      | Payload separation observed              | Real hardware release            |
| Play live-weather recovery | PX4/Gazebo SITL, real Open-Meteo surface + altitude-profile wind, real-gust turbulence, relative-airflow drag, rotor-coupled battery | LLM read wind + drift → proposed return_to_launch, human approval required; battery drains with rotor effort | Drag uses the vehicle's own velocity (relative airflow) but a modelled coefficient; turbulence dynamics modelled (amplitude is real gust); battery is a sim signal not hardware endurance; no delivery/physical execution |
| Play delivery in wind | PX4/Gazebo SITL, real MAVLink waypoint mission, gust-driven turbulence | takeoff → dropoff → real cargo separation (detachable joint, verified drop) → return, real wind, cross-track tracked | Sim cargo not real hardware; delivery completion not claimed |
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

## Documentation Layers

Human-facing docs (`docs/concepts/`, `docs/examples/`) should be short and readable.
Agent-facing docs (`docs/agents/`) should be precise enough for automated agents to modify the code without breaking core boundaries.

## Initial Development

The repository now carries both safe fixtures and copied production runtime code.
The safe default remains fixture mode; anything that starts Docker, PX4/Gazebo,
MAVLink upload, or live flight must remain opt-in and evidence-bounded.

## License

Apache-2.0
