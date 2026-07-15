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

## Current cleanup branch result

As of `codex/codebase-inventory` after the thirteenth PX4/Gazebo monolith
extraction:

- tracked Python: 276,588 lines, down 20,221 lines from the baseline (6.81%)
- `smoke_*.py`: 69 files / 35,931 lines, down from 135 files / 52,004 lines
- automated verification: 700 passed, 5 warnings
- runtime verification: `python -m src.quickstart_smoke --json` created and
  completed fresh task `task_e5e900c70d95` in an isolated SQLite store without
  a model or bridge

The line reduction is not a capability claim. The deleted Compose entrypoints
were not runnable from the public checkout, and fixture tests explicitly do not
claim external Gazebo or PX4 execution.

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
| `scripts/smoke_px4_gazebo_horizontal_route_delivery.py` | 10,344 | a production dispatch backend is exposed through a 2,106-line `main` function |
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

After removing the launchers, static reachability was recalculated before any
implementation deletion. Four modules had no remaining importer, script,
documentation entrypoint, test, or Gateway/CLI binding: the Gazebo Classic log
collector, the fake PX4/Gazebo SITL telemetry spike, the Docker mock-simulator
service client, and its private HTTP server. Those 844 lines were removed as
transitive dead code. The fixture-backed mock simulator adapter and current
Gazebo/PX4 collectors remain; this deletion does not remove those contracts or
claim that static reachability alone is sufficient for broader runtime cleanup.

A second fixed-point scan found five pre-existing runtime modules with no
inbound edge even before considering launcher reachability: an empty superseded
approval placeholder, an unpublished mission-template registry, an unattached
toy-grid persistence helper, the retired Compose command-driven delivery
artifact path, and a historical tenth-stage checklist. None was exported by the
supported package, invoked by the CLI/Gateway, documented, or covered as a
public entrypoint. They were removed rather than retained as speculative APIs.

Removing the historical tenth-stage checklist exposed its sole supporting
module, `limited_live_action_rehearsal.py`, as another zero-inbound artifact
builder. A final fixed-point pass removed it as well. This does not change the
current approval or live-SITL opt-in paths; neither imported the rehearsal
module.

That removal in turn exposed `limited_live_action_gate.py`, an explicitly
unintegrated design/schema slice, as the final orphan in this dependency chain.
It was removed and the fixed-point scan then found no zero-inbound Python module
under `src/runtime` or `src/simulators`.

## Smoke classification pass

The second cleanup phase classifies a script from repository evidence rather
than its `smoke_` prefix:

1. `production_boundary`: invoked by `src/`, CI, or a maintained public
   reproduction document
2. `regression_candidate`: no maintained inbound entrypoint, but exercises a
   contract owned by a current `src/runtime` module; preserve the invariant in
   pytest before removing or replacing the script
3. `historical_one_off`: its target or repository-local dependency is gone or
   superseded, so the script is not a reproducible current check

The first machine pass found ten zero-inbound scripts that imported test modules
which no longer exist. These 1,777 lines were removed as
`historical_one_off`. A contract test now statically resolves all `src.*`,
`scripts.*`, and `tests.*` imports in public Python scripts so this broken state
cannot silently recur.

The resulting fixed-point scan exposed three advisory epic-exit/invariance
modules, totaling 590 lines, whose only consumers were in that broken script
cohort. They were removed after confirming the current TurtleBot3 recovery,
Gateway, and mission-play paths do not import them.

One final dependency, `advisory_lesson_invariance.py` (166 lines), then became
orphaned and was removed in the same fixed-point cleanup.

The next zero-inbound candidate,
`smoke_px4_gazebo_sensor_visible_actor.py`, contained its artifact schemas and
Docker experiment entirely inside the script and imported no MissionOS module.
Because no current runtime contract, verifier, documentation, or caller owned
its result, the 503-line standalone prototype was classified as
`historical_one_off` and removed rather than converted into a misleading
product regression test.

Four small zero-inbound contract smokes were then promoted into the maintained
pytest suite. Existing ROS2/Nav2 and Unitree adapter tests already covered their
fail-closed cases. The Gazebo delivery-world fixture and PX4/Gazebo profile
invariants were added to `test_gazebo_delivery_profiles.py`, including task
preservation and no command/live/physical authority. The four manual scripts
were removed after the automated replacements passed.

The first full artifact-chain consolidation replaced six more manual scripts
with one reusable `DeliveryArtifactChain` fixture and three contract tests. The
fixture builds contract, sanitized telemetry, HIL review, policy review, gate,
episode, Gazebo scenario, and progress review once. Tests then verify schema and
safety invariants plus every TaskStore attach boundary, including that recovery
produces recommendations but no approval, promotion, reuse, command, live, or
physical authority. New delivery contracts can extend this fixture without
adding another standalone smoke program.

The same fixture was then extended with a completed bounded-delivery branch:
scenario proposal, approval-scoped simulation request, fixture-backed Gazebo
run evidence, verified dropoff, episode/replay, scorecard, and review. One
contract test now proves both completion scoring and the resulting
`completed_no_recovery_needed` recommendation while keeping every authority
surface false. This replaced the two separate completed-delivery smoke scripts.

Seven failure-handling scripts were then promoted into parameterized contract
tests. A shared fixture now supplies invalid PX4 SIH, PX4/Gazebo log, classic
Gazebo log, Gazebo Sim empty-world, and Gazebo Sim delivery-world inputs. The
tests verify fail-closed rejection, unchanged task status and existing
artifacts, absence of approval or execution authority, debug-only diagnostics,
and command-like payload redaction. A loopback-only sidecar test preserves the
HTTP client boundary, while delivery-observation tests preserve diagnostics and
geofence-blocked runner behavior. These tests do not start PX4, Gazebo, Docker,
or hardware and therefore do not replace the protected opt-in runtime smokes.

Two additional opt-in scripts were found to be deterministic fixture checks,
not external PX4/Gazebo executions. Coupled-command failure cases are now seven
parameterized tests that require every timeout, rejection, target, endpoint,
approval, and allowlist failure to terminate blocked without retry or stronger
authority. Route recovery now uses a reusable loopback UDP fixture and asserts
that bounded dispatch serialization sends datagrams before validating the
16-case golden corpus, recovery approval/allowlist separation, hold behavior,
and physical-execution boundaries. No simulator or hardware endpoint is used.

Three more fixture-backed smokes were converted to contracts for operational
envelope parameter transfer and Digital Twin vehicle energy. The tests keep
Form 0b parameter knowledge separate from causal verification, require a new
physical Form 1, reject consumption after a backend image change, and verify
that no dispatch or physical authority is created. Vehicle, DEM, and weather
inputs are injected fixtures with no network access. The separate smokes that
use default external DEM or weather fetchers remain protected as source-bound
runtime checks.

Five delivery shared-observation and recovery scripts were consolidated around
one reusable two-vehicle mission fixture. Contract tests now cover citable pose
and hazard observations, future and stale rejection, decision-context and epic
exit artifacts, logic-only recovery-loop persistence, promoted advisory lesson
references, and hidden-suppression rejection. Shared observations and advisory
memory remain judgement inputs only: they grant no command, dispatch, outcome,
scorecard, success-proof, hardware, or physical-execution authority. The former
smoke output for `proposal_uses_advisory_authority_for_judgement` was not an
assertion; the maintained test now explicitly requires it to remain false.

Six simulated-delivery scripts were then replaced by tests built on the shared
completed-delivery chain. The fixture now reaches recovery decision and
operator status once, while tests separately cover bounded episode evidence,
operator-minimal status, simulator command proposal/approval/receipt,
rehearsal/preflight, top-level runner timelines, PX4 artifact-stub dispatches,
and phase-derived SITL observations. The simulator command approval is asserted
to be scoped only to a dry-run receipt; no external dispatch, Gazebo mutation,
MAVLink/ROS/actuator action, mission upload, hardware target, or physical
execution is permitted. No external simulator process is started by this suite.

Five Gazebo delivery scenario, sidecar, and simulation-control scripts were
then promoted into three contract modules. Tests now require deterministic and
distinct scenario variants, task identity preservation, and no command, live,
or physical authority. Sidecar coverage verifies the five-phase sequence,
final `completed` phase, task-timeline status change, and attached evidence
chain; the retired v0 script previously printed output without asserting these
facts. Simulation-control cases separately cover a successful fixture run and
fail-closed approval-missing, low-battery gate, contract-identity, and mission-
identity failures. Blocked cases must skip the sidecar and runner. These tests
do not start Gazebo, Docker, PX4, or hardware and make no external-runtime
claim.

## Monolith decomposition

The first behavior-preserving extraction moved the three embedded MAVLink
programs out of `smoke_px4_gazebo_horizontal_route_delivery.py` and into
`src/runtime/px4_gazebo_route/embedded_mavlink.py`. The legacy executable still
imports and sends the exact same source strings, so its public opt-in command
and container behavior remain compatible. Its local size fell from 12,634 to
12,066 lines; total Python grew by 139 lines because the extraction added a
package boundary and five maintained contract tests. This step targets
reviewability rather than deletion.

The contract suite compiles all three embedded programs, verifies that the
legacy entrypoint uses the packaged sources, and executes the route helper in a
separate process against a loopback UDP peer. The peer returns an accepted ARM
`COMMAND_ACK`; the helper must report the exact observed ACK rather than infer
success from sending a frame. A runtime smoke also executed the packaged
read-only heartbeat observer for 0.1 seconds and observed
`observer_status=completed`, `observer_sent_packets=false`, and
`packet_drop_performed=false`. Running the legacy route entrypoint without
`RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE=1` still terminates fail-closed before
starting PX4 or Gazebo. No external simulator, Docker container, or hardware
was invoked by this extraction verification.

The second extraction moved 13 pure Gazebo world builders and inspectors into
`src/runtime/px4_gazebo_route/world.py`. Payload, landing marker, no-fly-zone,
traffic-conflict, alternate-landing, moving-actor, fog, and wind fragments now
have one owner outside the execution entrypoint. The module accepts values and
returns text, XML elements, deterministic motion metadata, or a hash; it does
not read environment authority, mutate files, start Docker/Gazebo, or dispatch
commands. Environment-dependent collision-obstacle construction and all file
mutation deliberately remain in the orchestrator until their inputs can be
made explicit.

Five new contract tests parse the generated XML, preserve model and plugin
identities, verify payload mass and wind-vector transfer, require idempotent
fog insertion, and pin the moving-actor trajectory hash. The legacy entrypoint
uses the same packaged function objects. A fixture runtime command generated
and parsed a fogged world plus wind plugins with `dispatch_requested=false` and
`physical_execution_invoked=false`; the opt-in route entrypoint still stopped
before external execution when its explicit environment gate was absent. The
entrypoint fell from 12,066 to 11,807 lines. Total Python grew by 161 lines
because this reviewability step added the module boundary and maintained tests.

The third extraction separated read-only observation normalization into
`src/runtime/px4_gazebo_route/observation.py`. PX4 listener fields and battery
status, Gazebo contact-topic selection and evidence, pose phase rows, terminal
pose summaries, XY matching, and point-to-route distance now transform inputs
without invoking Docker, Gazebo, PX4, or a dispatch function. The orchestrator
still owns every external read and passes the resulting text or mappings into
the package. Contact-topic fallback precedence is preserved before the
orchestrator runs its read-only sample command.

Five contract tests cover successful, failed, and incomplete battery listener
output; contact-topic priority and read-only claim fields; ordered pose phases;
terminal Gazebo and PX4-local-NED sources; and shared distance geometry. A
fixture runtime command observed battery, contact, and route pose facts while
retaining `task_status_mutated=false` and `delivery_completion_claimed=false`.
The entrypoint still stopped without its explicit opt-in. It fell from 11,807
to 11,622 lines, while total Python grew by 240 lines for the package boundary
and regression coverage.

The fourth extraction introduced
`src/runtime/px4_gazebo_route/execution.py` for low-level helper invocation,
read-only heartbeat observation, and the bounded MAVLink endpoint-loss
mechanic. Every function receives its process runner, container identity,
helper source, and endpoint ports explicitly. The module does not create an
approval, select a recovery action, widen an allowlist, or claim mission
completion; those authority and verification responsibilities remain with the
caller. Observer and link-loss outputs forcibly retain their read-only and
unverified claim fields even if an untrusted helper payload says otherwise.

Eight contract cases cover exact command construction, successful observer
output, nonzero and malformed-output failure paths, explicit endpoint restart
selection, false failsafe claims, and legacy-entrypoint delegation. A fixture
runtime runner executed the packaged heartbeat observer in a separate host
process and bound its UDP listener without Docker. It completed with
`observer_sent_packets=false` and `packet_drop_performed=false`; the legacy
entrypoint remained fail-closed without opt-in. The entrypoint fell from 11,622
to 11,526 lines. Total Python grew by 344 lines because execution mechanics now
have an explicit interface and maintained failure-path coverage.

The fifth extraction moved route-process construction and pose-deviation
monitoring into the same execution module. Process creation, clock, sleep,
pose sampling, trace append, distance calculation, and the recovery callback
are explicit dependencies. This makes the ordering reviewable: the route
stream is terminated and reaped before a deviation callback can request a new
bounded recovery. A timeout terminates the process before another pose sample,
and normal completion only reports the helper payload after its process exits
successfully.

Three additional tests cover normal monitored completion, deviation abort and
stream-stop-before-recovery ordering, and timeout without post-timeout sampling.
A fixture runtime used a real child process and observed five pose samples
before `sent=true` with no deviation. The opt-in entrypoint remained
fail-closed. It fell from 11,526 to 11,425 lines; total Python grew by 213 lines
for the injected execution interface and its deterministic process tests.

The sixth extraction added `src/runtime/px4_gazebo_route/verification.py` as the
fifth package responsibility. Materialized-status allowlisting, Gazebo wind
vector readback, PX4 parameter application/value checks, and route-corridor
obstacle source verification now consume existing observations without
dispatching, mutating task state, or inferring mission completion. Missing
obstacle evidence returns every failed source fact instead of collapsing the
result into a generic false value.

Five contract tests preserve legacy delegation, the explicit status allowlist,
wind vector/hash matching, PX4 parameter failure behavior, and all eight
obstacle-evidence rejection reasons. A fixture runtime accepted matching wind
readback and rejected an empty obstacle artifact with eight reasons while
keeping dispatch and completion false. The opt-in entrypoint remained
fail-closed. It fell from 11,425 to 11,327 lines; total Python grew by 163 lines
for the verifier boundary and regression coverage.

The seventh extraction added `src/runtime/px4_gazebo_route/scenario.py` for
explicit-value scenario decisions. Wind-compensation request construction,
wind geometry, feed-forward ramping, telemetry and MAVLink degradation mode
normalization, terrain-relative thresholds, wind vectors, and thermal factors
now run without reading environment variables or touching runtime state. The
entrypoint still owns every environment read and passes the resulting values
into the pure helpers; this prevents scenario configuration from becoming an
implicit approval or dispatch source.

Fourteen contract cases cover legacy delegation, authority-free compensation
artifacts, bounded ramp fractions, vector conventions, supported and unknown
mode aliases, terrain materialization requirements, and thermal clamps. An
entrypoint-level runtime command read real process environment values and
created an enabled velocity-feed-forward request while retaining
`automatic_dispatch_executed=false`, `physical_execution_invoked=false`,
`hardware_target_allowed=false`, and `delivery_completion_claimed=false`. The
external PX4/Gazebo entrypoint still exited before execution without explicit
opt-in. It fell from 11,327 to 11,220 lines; total Python grew by 259 lines for
the new boundary and its regression coverage.

The eighth extraction moved wind, thermal-weather, battery, and sensor-failure
requested-profile construction into the scenario module. Environment parsing
remains in the entrypoint; profile builders now receive only explicit values.
They preserve out-of-range rejection and clamps, derive supported battery
warning scenarios, report unsupported sensor values, and retain false
execution and completion fields. In particular, a requested battery percentage
remains scenario input and cannot masquerade as observed PX4 battery status.

Seven additional contract cases cover thermal and pressure bounds, battery
scenario derivation, invalid remaining percentages, sensor defaults and
unsupported values, and the environment-to-explicit-value wrapper boundary. A
runtime invocation supplied wind, temperature, invalid pressure, low battery,
and sensor-failure environment values through the entrypoint. It derived a
critical-battery scenario, rejected the invalid pressure, and retained
`physical_execution_invoked=false` and `delivery_completion_claimed=false`.
The entrypoint still rejected external execution without opt-in. It fell from
11,220 to 11,139 lines; total Python grew by 155 lines for the profile boundary
and regression coverage.

The ninth extraction moved the accepted vocabulary for landing-zone blocking,
visibility, world markers, RTH behavior, moving obstacles, contact topics, and
multi-drone probes into explicit scenario selectors. Each selector normalizes
an explicit value and accepts only its documented aliases; unknown values stay
false rather than becoming an execution request. The entrypoint wrappers now
do only the environment read and delegate the decision.

Eleven additional contract cases exercise every selector's accepted alias,
generic true value, unknown value, empty value, visibility normalization, and
the environment wrapper. A runtime invocation accepted blocked-landing, fog,
and RTH inputs, rejected an unknown obstacle value, and retained false dispatch
and physical-execution fields. The legacy entrypoint still stopped without
explicit opt-in. The entrypoint grew by one line (11,139 to 11,140) because the
explicit wrappers are formatted across lines; 97 lines of selector ownership
nevertheless moved into the package. Total Python grew by 142 lines for that
boundary and its regression coverage. This step improves reviewability rather
than reducing total LOC.

The tenth extraction separated the moving collision-obstacle definition across
the existing scenario and world boundaries. The scenario builder receives four
explicit coordinates and creates deterministic motion metadata. The world
builder receives that metadata plus an explicit contact topic and creates the
Gazebo SDF. Only the entrypoint reads bounded coordinate environment values;
neither package function reads environment state, mutates files, starts Gazebo,
or dispatches a command.

Three additional contract cases preserve exact motion metadata, environment
wrapper behavior, SDF pose and waypoint transfer, contact-topic identity, and
the contact-disabled variant. A runtime invocation passed four coordinate
environment values through the entrypoint, parsed the generated XML, and
observed matching pose, waypoints, and contact topic with dispatch and physical
execution false. The external entrypoint remained fail-closed without opt-in.
It fell from 11,140 to 11,071 lines. Total Python grew by 83 lines because the
new package boundary has focused regression coverage.

The eleventh extraction introduced
`src/runtime/px4_gazebo_route/supervision.py`. Wind assessment, recovery-cycle
audit artifacts, and two-cycle loop aggregation now consume explicit profiles,
observations, references, and outcome flags. The entrypoint only collects the
current global summaries and forwards them. The package does not read process
environment, mint approval or dispatch authority, execute an action, mutate a
task, or claim delivery or physical execution.

The extraction also corrected an existing truthful-status defect: a cycle with
`approval_ref=None` previously emitted `operator_approved=true` and
`operator_approved_dispatch_allowed=true`. Both fields now require an actual
approval reference. Six contract cases cover nominal and four-risk compound
assessment, unapproved and approved cycles, two-outcome loop support,
conflict-driven fail-closed behavior, and entrypoint summary collection. A
runtime fixture produced an unapproved cycle plus a separately approved
two-cycle loop; the first stayed unapproved, while the loop retained false
dispatch-authority, delivery-completion, and physical-execution fields. The
external entrypoint remained fail-closed without opt-in. It fell from 11,071
to 10,765 lines. Total Python grew by 324 lines because the 409-line package
boundary and maintained tests replace implicit monolith behavior.

The twelfth extraction moved obstacle assessment, alternate-route and landing
cycle artifacts, and obstacle-loop aggregation into the supervision package.
The entrypoint retains only current-summary collection, final-pose parsing, and
the explicit calls that bind the two cycles. Battery and telemetry conflicts
now remain visible inputs to the loop decision rather than implicit globals in
artifact construction.

The same truthful-approval correction was applied to obstacle cycles:
`approval_ref=None` now produces `operator_approved=false` and
`operator_approved_dispatch_allowed=false`. Four additional contract cases
cover compound risks, unapproved cycles, successful and fail-closed loops, and
entrypoint summary collection. A runtime fixture built an approved
alternate-route plus landing loop and separately checked an unapproved cycle;
dispatch authority, delivery completion, and physical execution remained
false. The external entrypoint still rejected execution without opt-in. It
fell from 10,765 to 10,540 lines. Total Python grew by 227 lines for the
explicit package boundary and regression coverage.

The thirteenth extraction moved payload-advisory assessment, RTL and landing
cycle artifacts, and payload-loop aggregation into the supervision package.
The payload profile, battery observation, telemetry freshness, advisory ref,
approval refs, and outcome observations are now explicit inputs. A payload
advisory remains a risk input and never becomes evidence of dropoff or delivery
completion.

Payload cycles now use the same truthful approval rule as wind and obstacle
cycles: without an approval reference, neither the decision nor action request
claims operator approval. Four additional contract cases cover payload mass
and advisory identity, compound risks, unapproved cycles, loop success and
fail-closed behavior, and entrypoint summary collection. A runtime fixture
produced a separately approved RTL and landing loop and an unapproved control
cycle; authority, completion, and physical-execution fields remained false.
The opt-in gate still stopped external execution. The entrypoint fell from
10,540 to 10,344 lines. Total Python grew by 219 lines for the explicit package
boundary and regression coverage.

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
