# MissionOS Chat PR Merge E2E Checklist

This is the merge gate for pull requests that change `missionos chat`, the
Gateway, planner or recovery agents, task state, runtime adapters, approval or
dispatch, `operate`, `watch`, `map`, or their evidence contracts.

A focused unit test or one successful HTTP request is not a `missionos chat`
E2E. The required E2E starts at the installed CLI entrypoint, crosses the real
affected production boundary, and compares the resulting task through every
applicable operator surface.

## 1. Merge Rule

Choose the highest applicable level. Every required item must be either:

- `PASS`, with an exact command and evidence reference; or
- `N/A`, with a concrete reason showing that the boundary is unaffected.

`PARTIAL`, `NOT RUN`, an unexplained `N/A`, or a required failure blocks merge.

| Level | When required | Minimum scope |
| --- | --- | --- |
| A: affected boundary | Any runtime-affecting PR | Real `missionos chat` entrypoint, real loopback Gateway, affected backend or adapter, positive path, and relevant fail-closed path |
| B: shared Core or contract | Core, shared schema, authority, feasibility, task-state, or cross-backend change | Level A plus PX4/Gazebo and Nav2/TurtleBot3 conformance and live paths |
| C: release or public acceptance | RC, stable, public-sync, packaging, installation, or release-documentation PR | Level B from a fresh public clone and clean install, plus publication-safety scan and anonymized replay |

Documentation-only changes may use link, formatting, and example-command checks
instead of a simulator run only when no runtime behavior, safety boundary,
command contract, or release claim changes.

## 2. Non-Negotiable Authority Boundary

Every run must preserve:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

Record these as separate facts:

| Stage | What it proves | What it does not prove |
| --- | --- | --- |
| LLM judgment | A model proposed or explained an action | Approval, feasibility, dispatch, or execution |
| Human approval | The operator approved one exact proposal/checkpoint | Dispatch or observed movement |
| Rule/verifier result | The candidate is `verified_feasible`, `blocked`, or `unverified` | Approval or execution |
| Dispatch authority | A bounded request may be sent | Executor ACK or physical effect |
| Executor ACK | The executor accepted or queued the request | Movement or target reached |
| Runtime observation | Telemetry shows an observed effect | Mission or delivery completion |
| Completion verification | A stated completion predicate passed | Physical execution when the backend is simulated |

Before human approval, verify that no dispatch authority exists. For simulation,
`physical_execution_invoked` remains false. Do not claim delivery completion
unless a separate delivery verifier establishes it.

## 3. Preflight Record

Before running, record:

- [ ] repository, branch, and commit SHA
- [ ] dirty worktree status and any unrelated local changes
- [ ] Python version and installation command
- [ ] Gateway URL, isolated port, and isolated state/database path
- [ ] selected robot/backend and simulator image or version
- [ ] requested LLM provider and model
- [ ] whether the run uses fixture, replay, simulator, HITL, or hardware
- [ ] test start time and operator

For hosted-model proof:

- [ ] confirm that the required key exists without printing it
- [ ] unset or disable competing provider keys when provider selection itself is
      under test
- [ ] record the provider/model reported by runtime evidence
- [ ] prove that the hosted response was used and deterministic fallback was
      not substituted

Never copy `.env`, credentials, private task databases, cookies, private prompts
or responses, internal task IDs, or workstation-specific absolute paths into a
public evidence bundle.

## 4. Automated Baseline

Run these before live E2E and report them separately from E2E:

- [ ] focused unit and contract tests for every changed boundary
- [ ] negative/fail-closed contract tests for every new safety condition
- [ ] full repository test suite when shared state, schemas, Core, Gateway, or
      release behavior changes
- [ ] lint and compile/type checks used by the repository
- [ ] Python 3.11 and 3.13 CI
- [ ] fixture/replay schema validation where applicable
- [ ] backend conformance corpus where applicable

Passing these checks does not waive the runtime checks below.

## 5. Required `missionos chat` Path

Use the installed CLI, not a helper that bypasses the CLI/Gateway boundary.
A typical live PX4 entrypoint is:

```bash
missionos chat --autostart --enable-live-sitl
```

For TurtleBot3:

```bash
missionos chat --robot turtlebot3 --autostart
```

Once a task exists, inspect the exact same task ID:

```bash
missionos job-status --task-id "$TASK_ID"
missionos operate --task-id "$TASK_ID"
missionos watch --task-id "$TASK_ID"
missionos map --task-id "$TASK_ID" --snapshot --no-open
```

Do not use “latest task” resolution as evidence when multiple runs exist.

### 5.1 Startup

- [ ] Gateway autostart or explicit start succeeds
- [ ] health/status is obtained from the intended loopback Gateway
- [ ] no stale Gateway, database, task, or simulator process is reused
- [ ] failure to start displays an actionable error and does not fabricate a
      ready state

### 5.2 Natural-Language Planning

Use a prompt that contains the conditions affected by the PR. For example:

```text
Plan a PX4 SITL mission from Tokyo Station to Akihabara.
Place an obstacle at 50% of the route, use wind 3 m/s, temperature 5 C,
and payload 0.5 kg.
```

- [ ] route endpoints and robot/backend are correct
- [ ] percentages, units, signs, payload, weather, terrain, and obstacles are
      extracted without borrowing unrelated defaults
- [ ] English and Japanese inputs are tested when language parsing changes
- [ ] ambiguous or contradictory text requests clarification or remains
      unverified; it is not silently guessed
- [ ] provider/model evidence matches the requested hosted model
- [ ] the first response is a proposal only
- [ ] no preparation, simulator start, approval, dispatch, or progress is
      implied before its explicit stage

### 5.3 Approval and Preparation

- [ ] plan approval is an explicit, separate human action
- [ ] approval binds the exact proposal/checkpoint/hash shown to the operator
- [ ] revision invalidates the superseded approval
- [ ] preparation does not claim simulator start or dispatch
- [ ] starting the simulator remains a separate, explicit action
- [ ] live execution remains a separate, explicit action
- [ ] the task ID is recorded immediately after creation

### 5.4 Runtime Materialization

For every requested condition, complete this evidence chain:

| Condition | Requested | Apply attempt | Applied/readback | Observed behavior delta | Verifier verdict | Mission response |
| --- | --- | --- | --- | --- | --- | --- |
| obstacle |  |  |  |  |  |  |
| wind |  |  |  |  |  |  |
| temperature |  |  |  |  |  |  |
| payload |  |  |  |  |  |  |
| terrain/clearance |  |  |  |  |  |  |

An artifact field is not runtime capability. A successful SET command is not a
readback. A readback is not behavior. A verifier result is not causal proof
unless the requested condition was materialized and an observed behavior delta
was recorded.

## 6. Cross-Surface Consistency

At each applicable checkpoint, compare `chat`, `job-status`, `operate`, `watch`,
and `map` using one task ID.

| Checkpoint | chat | job-status | operate | watch | map |
| --- | --- | --- | --- | --- | --- |
| proposal created, not approved | [ ] | [ ] | [ ] | N/A if no task | N/A if no task |
| backend started | [ ] | [ ] | [ ] | [ ] | [ ] |
| hazard detected / Safety HOLD | [ ] | [ ] | [ ] | [ ] | [ ] |
| recovery proposal awaiting approval | [ ] | [ ] | [ ] | [ ] | [ ] |
| approved and dispatch revalidated | [ ] | [ ] | [ ] | [ ] | [ ] |
| maneuver in progress | [ ] | [ ] | [ ] | [ ] | [ ] |
| target reached / route rejoined | [ ] | [ ] | [ ] | [ ] | [ ] |
| terminal state | [ ] | [ ] | [ ] | [ ] | [ ] |

Verify:

- [ ] task ID, robot/backend, status, proposal ID, and checkpoint agree
- [ ] PX4 views do not display Nav2/TurtleBot wording, and ground-robot views do
      not display flight-only wording
- [ ] telemetry sample index/time is fresh and does not regress
- [ ] `operate` shows the proposal source, model/fallback status, feasibility,
      approval state, and dispatch state
- [ ] `watch` shows the actual hazard/HOLD/maneuver, not only the planned route
- [ ] `map` distinguishes planned, observed, recovery, collision/obstacle, and
      return paths
- [ ] gaps in observations are not drawn as movement
- [ ] no surface says `completed` while another says `blocked` or `running`
- [ ] terminal wording distinguishes task termination, mission success,
      delivery completion, and physical execution

## 7. Positive-Path Scenario

At least one affected backend must demonstrate that the system does not merely
reject every action:

- [ ] requested conditions are applied and source-backed
- [ ] a real hazard is observed and Safety HOLD is visible
- [ ] telemetry continues updating while hosted LLM inference is pending
- [ ] the model sees all candidates and selects a candidate
- [ ] the selected candidate is `verified_feasible`
- [ ] the model does not approve or dispatch it
- [ ] a durable proposal/checkpoint is shown in `operate`
- [ ] a human explicitly approves that proposal
- [ ] dispatch-time policy, telemetry, obstacle source, safe window, and
      feasibility are revalidated
- [ ] dispatch authority is created only after successful revalidation
- [ ] executor request and ACK are recorded separately
- [ ] enough runtime samples establish observed movement
- [ ] target/recovery goal is reached according to the verifier
- [ ] the planned route is rejoined when the scenario requires it
- [ ] PX4 returns to AUTO, continues mission progress, RTLs, lands, and disarms;
      or Nav2 resumes and reaches its bounded route goal
- [ ] final claim flags match what was actually proven

## 8. Mandatory Fail-Closed Scenarios

Select every row affected by the PR. Core, authority, Gateway, recovery, and
release PRs must run all applicable rows.

- [ ] missing or invalid hosted-model key: no silent claim of hosted-model proof
- [ ] malformed model output or unavailable model: safe fallback is labeled, or
      the operation remains blocked
- [ ] ambiguous natural language: no invented route, condition, or recovery
- [ ] natural-language recovery: creates a proposal/checkpoint and never
      dispatches directly
- [ ] missing, stale, regressed, or incomparable telemetry: dispatch denied
- [ ] telemetry continues and Safety HOLD persists during slow LLM inference
- [ ] situation changes during inference: old result becomes superseded or
      retryable
- [ ] policy changes after approval: old proposal cannot dispatch
- [ ] wind exceeds policy or safe-window evidence no longer matches latest
      telemetry: dispatch denied
- [ ] current obstacle differs from candidate/compiled/proposal source:
      dispatch denied
- [ ] obstacle geometry, frame, or required clearance is missing: candidate is
      `unverified`
- [ ] collision, boundary-clearance, descending-terrain, and unreachable path:
      candidate is blocked
- [ ] payload or thermal requested/applied/readback values are missing or
      mismatched: candidate is not `verified_feasible`
- [ ] LLM selects a blocked or unverified candidate: no proposal capable of
      dispatch is created
- [ ] stale/superseded proposal, checkpoint/hash mismatch, or approval replay:
      dispatch denied
- [ ] executor rejects or never ACKs: no movement or completion claim
- [ ] verifier has too few samples: no `target_reached`
- [ ] goal is obstructed: obstacle placement and recovery do not silently move
      or redefine the goal
- [ ] recovery proposal generation fails: retryable state and bounded backoff
      remain possible
- [ ] backend or companion view is unavailable/stale: the failure is explicit
      and other surfaces do not fabricate its evidence
- [ ] terminal failure remains `blocked` or the contract-specific failure state,
      not successful `completed`

When compound-hazard, asynchronous inference, or multi-obstacle code changes,
also run:

- [ ] at least two simultaneous hazard factors
- [ ] at least two distinct obstacle/recovery epochs
- [ ] a hazard change while approval is pending
- [ ] inference completing after landing or terminal state is safely discarded

## 9. Backend-Specific Minimums

### PX4 / Gazebo

- [ ] obstacle is not accidentally placed on the destination unless that is the
      explicit negative scenario
- [ ] requested wind, temperature, payload, and obstacle values appear in
      application and readback evidence
- [ ] map shows the obstacle at the requested route fraction
- [ ] Recovery HOLD, proposal, approval, OFFBOARD maneuver, target reached,
      AUTO resume, RTL, landing, and disarm are individually evidenced
- [ ] SITL remains described as simulation; physical execution remains false

### Nav2 / TurtleBot3

- [ ] house/indoor world and expected floor plan are used, not a pillar-only
      fallback world
- [ ] obstacle geometry and frame match the costmap/map frame
- [ ] Safety stop, proposal, explicit approval, Nav2 goal, odometry movement,
      minimum clearance, route rejoin, and goal result are individually
      evidenced
- [ ] `map` uses source-backed indoor XY state and does not claim payload
      delivery from navigation alone

## 10. Screenshots and Evidence Bundle

Capture screenshots or terminal recordings at minimum:

1. `chat`: parsed plan and “not yet approved/dispatched” boundary
2. `chat`: task ID and backend start
3. `operate`: recovery proposal before approval
4. `operate`: explicit approval and dispatch-time revalidation result
5. `watch`: hazard/HOLD and maneuver in progress
6. `map`: planned route, obstacle, observed path, recovery path, and current
   position
7. `job-status`: final status and verifier facts
8. one relevant fail-closed scenario

Each image must identify the task/checkpoint it supports. Screenshots do not
replace JSON, timeline, telemetry, or verifier artifacts; cross-check them.

The evidence report should include:

- commit SHA and exact commands
- scenario and requested values
- model/provider and fallback count
- task ID or anonymized fixture ID
- checklist result by section
- artifact and screenshot references
- explicit limitations and skipped boundaries
- separate values for approval, dispatch, ACK, observed effect, landing,
  delivery completion, and physical execution

Public evidence must be anonymized and free of credentials, private databases,
private task IDs, internal hostnames, and local absolute paths.

## 11. Release / Fresh-Clone Acceptance

Level C additionally requires:

- [ ] clone the public repository into a new temporary directory
- [ ] install only from public files using the README procedure
- [ ] start the fixture Gateway and run the CLI workflow
- [ ] run the house map and anonymized replay
- [ ] run live PX4/Gazebo and Nav2/TurtleBot3 paths when claimed by the release
- [ ] compare one task ID across `chat`, `job-status`, `operate`, `watch`, and
      `map`
- [ ] scan for internal dependencies, secrets, private IDs, and local paths
- [ ] confirm a third party can reproduce the documented commands
- [ ] attach the acceptance report before creating or moving an RC/stable tag

## 12. PR Evidence Template

Every runtime PR body must contain:

```markdown
## E2E / Runtime Verification

- Commit:
- Level: A / B / C
- Backend(s):
- LLM provider/model:
- Install/start commands:
- Scenario and requested conditions:
- Task ID or anonymized fixture ID:
- Positive path:
- Fail-closed scenarios:
- Cross-surface result:
  - chat:
  - job-status:
  - operate:
  - watch:
  - map:
- Observed behavior delta:
- Screenshots/artifacts:
- Automated tests and CI:
- Claim flags:
  - approval_created:
  - dispatch_authority_created:
  - executor_acknowledged:
  - target_reached:
  - delivery_completion_claimed:
  - physical_execution_invoked:
- Limitations / not run:
- Verdict: PASS / BLOCKED
```

## 13. Hard Merge Blockers

Do not merge when any of the following is true:

- a required item is `PARTIAL` or `NOT RUN`
- different task IDs are compared across operator surfaces
- requested provider/model use is not proven
- only configuration or artifact presence is shown, with no observed behavior
  delta
- proposal, approval, dispatch, ACK, effect, or completion is collapsed
- a fail-closed case permits dispatch
- a live production boundary changed but only unit/fixture tests ran
- screenshots disagree with task/timeline/verifier artifacts
- the public tree or evidence contains secrets, private state, or local paths
- CI is not green for supported Python versions

The correct result in these cases is `Request changes`, not a qualified merge.
