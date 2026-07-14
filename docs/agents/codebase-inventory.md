# MissionOS codebase inventory

This document records a maintenance inventory, not a capability claim. Its
purpose is to identify safe code-reduction work without weakening MissionOS
authority, causal-evidence, or runtime-verification boundaries.

## Snapshot and method

- Repository: public `pome223/missionos`
- Baseline: `public/main` at `cbfe5f7`
- Inventory date: 2026-07-14
- Scope: Git-tracked files only
- Baseline test command: `python -m pytest -q`
- Baseline result: `554 passed, 5 warnings`

Line counts are physical lines, including comments and blank lines. Static
reachability is advisory only: subprocess entrypoints, dynamic imports, public
imports, and operator-run scripts can make an apparently unreferenced file
live. No file is safe to delete solely because it has no static importer.

## Size profile

| Area | Files | Physical lines |
|---|---:|---:|
| Python | 554 | 296,809 |
| `src/` | 309 | 186,977 |
| `scripts/` | 194 | 76,270 |
| `tests/` | 83 | 23,457 |
| `docs/` | 34 | 16,684 |
| `packages/` | 15 | 13,449 |

The maintained Python surface is concentrated in a small number of very large
files:

| File | Lines | Main maintenance risk |
|---|---:|---|
| `packages/missionos-cli/src/missionos_cli/cli.py` | 12,866 | CLI, chat, companion processes, maps, and HTML rendering share one module |
| `scripts/smoke_px4_gazebo_horizontal_route_delivery.py` | 12,634 | a production dispatch backend is exposed through a 2,106-line `main` function |
| `src/gateway/server.py` | 11,932 | route registration, orchestration, robot-specific handling, and recovery are coupled |
| `src/runtime/digital_twin_mission_environment.py` | 9,961 | environment construction and PX4/Gazebo mechanics are coupled |
| `src/runtime/turtlebot3_home_mission.py` | 9,808 | proposal, approval, resolver, execution, verification, UI evidence, and repair share one module |

The largest individual functions are also boundary-spanning:

- `GatewayServer._setup_routes`: 4,161 lines
- `run_turtlebot3_home_mission_dispatch`: 2,285 lines
- PX4 horizontal-route smoke `main`: 2,106 lines
- `build_digital_twin_stage1_environment`: 1,494 lines
- CLI `_mission_map_html`: 957 lines

These are decomposition targets, not immediate deletion targets.

## Findings

### 1. Historical smoke scripts dominate the most plausible deletion pool

There are 189 tracked Python scripts totaling 75,266 lines. Of these, 135 are
named `smoke_*` and total 52,004 lines. Only 34 Python scripts, totaling 10,105
lines, are referenced by public documentation, CI configuration, package
documentation, or the root project configuration. The other 155 scripts total
65,161 lines.

This does **not** prove that 65,161 lines are dead. Many scripts are operator-run
evidence producers or historical causal experiments. Before deletion, each
script needs a small manifest entry with one of these statuses:

```text
production_boundary
current_reproduction
golden_fixture_producer
historical_one_off
superseded
unknown
```

`production_boundary`, `current_reproduction`, and required fixture producers
stay. `historical_one_off` evidence belongs in release notes or a provenance
record rather than indefinitely in the executable source tree. `superseded`
scripts may be removed only after their replacement command and covered boundary
are recorded.

The largest script is an important counterexample to name-based deletion:
`smoke_px4_gazebo_horizontal_route_delivery.py` is called by production runtime
bindings and multiple audit runners. It is a production boundary whose current
filename is misleading. It must be extracted or renamed before its executable
surface can be reduced; it is not an unreferenced historical smoke.

### 2. Two CLI generations coexist

`src/cli/missionos.py` is 5,862 lines. All 187 function and class names defined
there also exist in the current 12,866-line packaged CLI. The old module remains
reachable through `src/main.py`, which is 1,111 lines, and two shell entrypoints:

- `scripts/bridge_runtime.sh`
- `scripts/quickstart.sh`

This was a high-confidence consolidation target. The remaining bridge commands
now call their dedicated MCP server modules, and quickstart calls the existing
no-model smoke module directly. No supported public command requires
`python -m src.main`, so the old CLI, REPL, and wrapper were removed together.

### 3. `src.runtime` is an oversized eager compatibility barrel

`src/runtime/__init__.py` is 1,889 lines and eagerly imports 64 runtime modules
while re-exporting 876 names. Internal code does not import those names directly
from `src.runtime`; three tests import runtime submodules through the package.

This file increased import coupling and made unrelated runtime modules look
like one public surface. No internal implementation or public documentation
used its symbol re-exports, so the eager barrel was removed. The supported
contract is now explicit import from the owning runtime module; Python's normal
`from src.runtime import <submodule>` compatibility remains covered by a
contract test.

### 4. The Gateway currently knows backend identities

The canonical architecture requires a backend-neutral Core. The production
Gateway directly imports PX4, real-hardware, and TurtleBot3 runtime modules, and
`src/gateway/server.py` contains hundreds of backend-specific references. Some
of this may be composition-root behavior, but a 4,161-line route-registration
method makes that boundary impossible to review reliably.

Do not solve this by deleting backend code. Extract backend-specific route
registration and execution bindings behind an interface such as:

```text
Gateway core
  -> mission target registry
     -> TurtleBot3/Nav2 target
     -> PX4/Gazebo target
     -> future targets
```

The Core should consume target capabilities, authority requirements, evidence,
and safety descriptors without branching on backend identity.

### 5. Active modules combine all authority stages

The current TurtleBot3 implementation is proven by live E2E, but one module
contains proposal generation, checkpoint construction, approval validation,
candidate resolution, dispatch, verification, map evidence, and repair loops.
Splitting it is valuable only if the authority chain remains explicit:

```text
proposal -> approval -> rules -> executor -> verifier -> repair
```

Module extraction must not turn a proposal into authority, reuse approval across
checkpoints, or let UI state become verifier evidence. This work should target
reviewability first; it may initially be line-neutral.

## Pilot consolidation

The first bounded pilot consolidated these two nearly identical programs:

- `smoke_ros2_nav2_turtlebot3_bounded_dispatch.py`
- `smoke_ros2_nav2_turtlebot4_bounded_dispatch.py`

Their 198 lines became one shared runner selected with `--robot-profile`. The
robot fixture contains only the differing default goal X coordinate; action,
approval, label, and smoke identifiers are derived from the profile name. The
two duplicated entrypoints were removed and the documented commands now call
the shared runner directly.

The executable smoke surface fell from 198 lines to one bounded shared runner.
Five contract cases were added for profile preservation and no-opt-in
fail-closed behavior. This is intentionally a small proof of the consolidation
pattern, not evidence that every similarly named smoke has equivalent semantics.

The first historical Gazebo cohort also retired two telemetry-only entrypoints.
Both called `docker compose` without a compose file in the public repository, so
neither was a reproducible public runtime boundary. Their reusable log-to-
evidence contract was moved into fixture-backed contract tests before deletion:

| Retired entrypoint | Preserved contract |
|---|---|
| `smoke_gz_sim_telemetry.py` | Gazebo Harmonic startup logs become read-only sanitized telemetry and HIL/gate artifacts without approval or execution authority |
| `smoke_gz_sim_delivery_world.py` | delivery-world identity is preserved while the same no-authority evidence boundary is enforced |

The fixtures do not claim that Gazebo was started. They preserve parser,
sanitizer, task-store, and authority-boundary behavior only. A future public
Gazebo runtime smoke must include its complete, publication-safe container
configuration and must be verified as an external runtime boundary before being
listed as maintained.

A follow-up scan found nine more standalone Python entrypoints that directly
called `docker compose`, plus a command-driven script that imported Compose
helpers from one of them. Together with the first two files, all 13 depended on
profiles absent from the public repository and failed before reaching their
claimed runtime boundary. The remaining 11 entrypoints were therefore retired
as one cohort. Their underlying `src/runtime` implementations remain available
for a separate reachability and contract audit; removing an unusable launcher
does not establish that those implementation modules are dead.

A repository contract now rejects Python scripts that invoke Docker Compose
unless a root Compose configuration is tracked. This is a publication-safety
and reproducibility rule, not a claim that container-backed Gazebo or PX4 was
executed by the fixture tests.

## Protected regression baseline

Every reduction PR must keep these facts separate and test them independently:

- proposal created
- approval artifact created for the exact checkpoint
- bounded dispatch authority created
- dispatch request sent
- executor ACK observed
- runtime behavior delta observed
- verifier verdict emitted
- completion scope claimed
- repair created a new observation and a fresh checkpoint

The current public contract suite (`554 passed`) is the minimum automated gate.
Runtime-boundary changes also require the exact smoke or live E2E required by
`AGENTS.md`. For TurtleBot3 recovery, preserve the public PR #15 scenario: two
separately approved recovery cycles, route completion, common task identity
across chat/operate/watch/map, and no delivery or physical-execution claim.

## Recommended reduction sequence

### PR 1: inventory enforcement, no deletion

- add a machine-readable script manifest
- add checks for undocumented scripts and duplicate supported entrypoints
- add import-time contracts for `src.runtime`
- record the supported public CLI and Gateway entrypoints

This PR is Form 0b maintenance work and does not claim capability progress.

### PR 2: retire the legacy CLI path (completed on this branch)

- migrated `bridge_runtime.sh` and `quickstart.sh` from `python -m src.main`
- retained dedicated Host Bridge, Desktop Bridge, and no-model quickstart modules
- removed `src/cli/missionos.py`, `src/cli/repl.py`, and `src/main.py`

Reduction: about 7,000 lines.

### PR 3: slim the runtime package barrel (completed on this branch)

- defined direct owning-module imports as the supported contract
- removed eager bulk re-exports
- retained and tested runtime submodule compatibility imports

Reduction: approximately 1,880 lines plus lower import coupling.

### PR 4 onward: consolidate smoke and audit programs by cohort

Review scripts in separate cohorts rather than one destructive PR:

1. TurtleBot3/Nav2
2. current PX4/Gazebo
3. digital-twin experiments
4. Unitree/other adapter experiments
5. historical Form 1/Form 2/Form 3 audits

For each cohort, keep one canonical runner with scenario data in fixtures where
possible. Remove superseded one-off programs only after preserving the scenario,
expected invariant, and reproduction command. The 65,161 unreferenced-script
lines are the upper candidate pool, not a promised deletion count.

### Later: split active monoliths

Split CLI, Gateway, TurtleBot3, and digital-twin modules along authority and
backend boundaries. Do this after deletion work has reduced the historical
surface, and judge success by dependency direction and reviewability rather than
raw line count.

## Decision

Do not begin with a repository-wide rewrite. The first safe target is the legacy
CLI path, followed by the eager runtime barrel. In parallel, classify scripts so
that historical experiments stop masquerading as maintained product surface.
The realistic first reduction is roughly 8,000 to 9,000 lines. Larger reductions
should come only from cohort-by-cohort script evidence, not from static-reference
counts alone.
