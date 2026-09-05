# MissionAssuranceAgent implementation contract

## Purpose

`MissionAssuranceAgent` is the single mission-level LLM judgment path. The
existing Runtime Recovery Agent first proposes a concrete bounded recovery
action. `MissionAssuranceAgent` then receives that proposal inside a
backend-neutral `MissionSituation` and proposes one mission-level disposition:
`MissionResponseProposal`:

```text
continue | hold | replan | return | abort | operator_escalation
```

The Runtime Recovery Agent is enabled as the recovery-action specialist.
Repair remains a separate workflow and is not enabled by this slice.

The authority split is fixed:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

The proposal cannot record approval, create dispatch authority, send a
dispatch request, claim ACK or runtime progress, claim landing, or claim
physical execution or completion.

## Dependency direction

```text
environment observation
  -> missionos_mission_incident_v2 ADK Workflow
  -> Runtime Recovery Agent
  -> concrete RecoveryProposal
  -> source Action Feasibility
  -> MissionSituation
  -> MissionAssuranceAgent.evaluate()
  -> MissionResponseProposal
  -> semantic alignment with the RecoveryProposal
  -> operator approval when eligible
  -> fresh Action Feasibility revalidation
  -> executor
  -> verifier
  -> next MissionSituation
```

`missionos_mission_incident_v2` is the common composition root. PX4 route
deviation and the live obstacle Supervisor both enter this Workflow. A backend
must not construct an approval-eligible Recovery proposal outside the graph.
The task artifact `missionos_mission_incident_graph` records the actual node
sequence, both Agent judgments, source feasibility, and the authority floor.

If the concrete Recovery proposal expires while the aircraft remains in the
same safety HOLD, deterministic recompilation may preserve the prior Recovery
judgment only through a hash-checked `recovery_judgment_binding`. The resulting
parameters and telemetry form a new `MissionSituation`; MissionAssuranceAgent
must run again. The old Assurance judgment is never inherited. The proposal
records both `parameter_recompile_evidence` and
`assurance_recompile_evidence`, including source and fresh telemetry cursors.
If either binding is missing, altered, or no longer aligned, the proposal is
not approval-eligible.

The judgment graph's `declared_next_sequence` field describes the required
downstream order. It is not evidence that approval, revalidation, execution,
verification, or re-observation occurred. Those facts are recorded only after
an explicit operator request starts
`missionos_mission_incident_continuation_v1`.

The continuation graph validates the frozen judgment graph id and hash before
any side effect, then records these actual nodes:

```text
frozen incident judgment
  -> explicit human approval observation
  -> dispatch-time Rules revalidation
  -> Recovery executor boundary
  -> Verifier
  -> next MissionSituation observation attempt
```

The human wait is a boundary between two linked Workflows, not a resume that
reruns the judgment nodes. Recovery and MissionAssuranceAgent remain frozen;
their `*_rerun` fields are false. Revalidation may update whether the already
approved action is still executable, but it does not change the Agent judgment
or manufacture a new proposal. Executor invocation, dispatch request, command
ACK, observed effect, progress, delivery completion, and physical execution
remain separate fields.

PX4 chat recovery review displays v4 proposals with verified compilation,
reachability and feasibility plus an aligned, approval-eligible incident graph.
It shows Assurance's response and rationale alongside Recovery's candidate.
The review sends the proposal id and frozen graph hash using
`expected_recovery_checkpoint_id` / `expected_recovery_checkpoint_hash`.
If either changes while the operator is reviewing, CLI or Gateway blocks the
request before execution and requires a new review. This does not extend the
proposal's freshness deadline or rerun either LLM on approval.

The observation node creates a next MissionSituation only when the runtime
telemetry cursor advances beyond the frozen judgment input. Reading the same
cursor after a queue operation records
`awaiting_fresh_post_dispatch_observation` and leaves
`next_mission_situation_created=false`.

The common Agent does not import PX4, Nav2, or VLA modules and does not branch
on a backend name. Thresholds are included in `constraints` as evidence; they
do not select the final response. It cannot invent, replace, or modify the
Recovery Agent's backend action or parameters.

## Common fields

`MissionSituation` binds:

- mission contract and progress;
- observed facts and deterministic constraints;
- uncertainty and source refs;
- source schema and input digest;
- execution scope;
- allowed semantic responses.

`MissionResponseProposal` records:

- the proposed semantic response;
- parameters, rationale, expected outcome, and uncertainty;
- the question returned to the operator;
- whether model inference was invoked and its evidence;
- `judgment_mode=llm_required`;
- `fallback_mode=operator_escalation_only`.

Unavailable, timed-out, invalid, or guardrail-rejected model output produces an
`operator_escalation` advisory. It never invokes a static action selector.

## PX4 adapter

Two explicit operator operations are not approvals of an Agent proposal:

- Preflight OFFBOARD calibration requests a 5 m leg with at most a 1.5 m
  climb. Its target uses observed terrain clearance plus a 0.5 m reserve, not
  the route monitor's grace as permission to fly below policy. The shorter
  leg leaves observation/dispatch latency room inside the unchanged 15 m
  Rules distance limit; it does not guarantee acceptance after arbitrary drift. The Gateway
  revalidates the exact parameters with Rules; a refusal consumes no execution
  authority and is not automatically retried.
- A direct CLI/chat LAND sets `operator_direct_land=true` in addition to
  explicit execution approval. It is parameter-free and restricted to the
  active, simulation-only PX4 runner. It neither requires nor approves a stale
  Recovery proposal, does not invoke either Agent, and records
  `missionos_operator_direct_land_dispatch_boundary`. Without this explicit
  marker, Agent-origin LAND remains subject to the frozen incident graph.
  The direct operation cannot authorize RTL, maneuvers, hardware, or a caller's
  arbitrary MAVLink endpoint. Queueing it is not landing/effect proof.

The arena TurtleBot3 observation waypoint reserves its stopping tolerance and
one costmap cell beyond Core's unchanged 0.15 m surface-clearance minimum.
Actual recovery candidates still require fresh Nav2 and Core verification.

`src/runtime/px4_gazebo_route/mission_assurance_adapter.py` performs only two
operations:

1. project source-bound PX4 Form 1 evidence into `MissionSituation`;
2. compile an accepted semantic proposal into a PX4 candidate verb.

Current compilation is:

| Semantic response | PX4 candidate |
| --- | --- |
| `continue` | no action |
| `replan` | `reroute` |
| `return` | `return_to_launch` |
| `operator_escalation` | no action |
| `hold` | unverified until an existing action contract is bound |
| `abort` | unverified; it is not silently converted to LAND |

The adapter does not choose the semantic response.

## Form 2 fail-closed rules

The production Gateway applies these rules before issuing an approval token:

- payload climb-delay remains a `level_2_inferred` Form 2b advisory;
- LLM failure becomes `operator_escalation`;
- no-action responses remain advisory results;
- an action must bind a readable Action Feasibility artifact inside the active
  artifact root;
- feasibility hash, action, parameters, mission-situation digest, execution
  scope, hazard source, policy, and false authority flags must match;
- `blocked`, `unverified`, missing, or mismatched feasibility becomes an
  escalation advisory;
- only `verified_feasible` may create an approval request/token and a planned
  dispatch reservation.

An approval token is not `approval_recorded`. A planned dispatch ref is not
`dispatch_authority_created`, `dispatch_request_sent`, ACK, progress, or
effect.

The approval used to start or resume the route is not Recovery approval. When
Recovery proposes an action and MissionAssuranceAgent accepts it, the live
guard must first persist
`missionos_mission_assurance_recovery_approval_request.v1` with
`request_status=awaiting_operator_approval`. At that point selected action,
dispatch authority, dispatch request, ACK, progress, and execution remain
absent. Only a new approval explicitly scoped to the proposed Recovery action
may continue.

After that fresh approval and before token consumption, a Mission Assurance
action must bind the existing
Core `missionos_core_action_revalidation.v1` `RevalidationArtifact`. The
Gateway creates that artifact only from a second Action Feasibility result and
then independently reloads it at action consumption. Both stages require:

- original and current feasibility hashes to be valid and different;
- action, parameters, mission-situation digest, and simulator scope to match;
- current status to remain `verified_feasible`;
- policy and model bindings not to drift;
- the PX4 telemetry cursor to be equal or advance, never regress or become
  incomparable;
- a hash-bound, zero-exit runtime invocation evidence record;
- current evaluation and artifact ages within 30 seconds;
- every authority, execution, ACK, progress, and completion flag to remain
  false.

The production HTTP boundary is
`POST /missionos/form2a-action-revalidation/run`. Action consumption must then
receive the returned artifact path explicitly. Revalidation is still evidence,
not approval or dispatch authority.

The Gateway loopback control remains fixture-backed and does not establish live
executable readiness by itself.

## PX4 same-runtime guard

`src/runtime/px4_gazebo_route/live_mission_assurance.py` connects the common
Agent to the existing route-deviation executor without changing the Agent API:

```text
route deviation and stream stop
  -> same-runtime pose, battery, wind, and terrain projection
  -> existing Runtime Recovery Agent proposes return_to_launch
  -> MissionSituation
  -> shared MissionAssuranceAgent judges mission-level alignment
  -> semantic return must align with the Recovery Agent proposal
  -> original Action Feasibility
  -> fresh operator approval for this Recovery action
  -> new same-runtime telemetry cursor
  -> current Action Feasibility and Core RevalidationArtifact
  -> operator-approved recovery dispatch boundary
```

The compatibility route-deviation adapter remains opt-in with
`--mission-assurance-on-deviation` and bounded to the existing
`--on-deviation-action rtl` executor. It now invokes the same Mission Incident
Workflow used by the live obstacle Supervisor. The CLI value declares the only
executor available to that adapter; it is not sent to the Recovery Agent as an
operator request and does not preselect its answer.

For the obstacle Supervisor, an actionable proposal such as `avoid_obstacle`
can become `awaiting_operator_approval` only when the same graph records:

- Recovery Agent proposal and model evidence;
- matching `verified_feasible` source Action Feasibility;
- a later MissionAssuranceAgent judgment;
- semantic alignment (`avoid_obstacle` is mission-level `replan`);
- false approval, dispatch, execution, ACK, verifier, and completion flags.

MissionAssuranceAgent `hold`, `continue`, or `operator_escalation` against a
feasible action produces `no_dispatch` and no reviewable Recovery proposal.
An accepted action still needs fresh human approval, dispatch-time
revalidation, executor evidence, verification, and the next observation.

The play-lab battery/GPS and delivery-deviation observers use the same graph
entry. Their simulator result retains `recovery_agent_result` as a compatibility
projection of the graph result and stores `missionos_mission_incident_graph` as the
authoritative composition evidence. A missing graph Recovery projection or
MissionSituation is fail-closed; the live adapter records the missing projection
and cannot rerun the former standalone Recovery/Situation construction path.

Either Agent's failure, a Mission Assurance response that does not align with
the Recovery proposal, infeasibility, stale evidence, or cursor regression
leaves the route aborted with no recovery dispatch. The
guard artifact itself keeps approval, dispatch, ACK, execution, progress,
physical-execution, and completion fields false; later runtime artifacts record
those facts independently if they occur.

## Runtime configuration

The production model path is opt-in:

```text
MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED=1
MISSIONOS_MISSION_ASSURANCE_MODEL_ID=<optional model override>
MISSIONOS_MISSION_ASSURANCE_TIMEOUT_SECONDS=60
MISSIONOS_AGENT_RUNTIME_ADK_ENABLED=1
MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_OLLAMA_MODEL=<model>
```

The compatibility route-deviation PX4 route additionally requires:

```text
--mission-assurance-on-deviation
--on-deviation-action rtl
```

A subprocess command exists only for fixture/dev verification and requires
both:

```text
MISSIONOS_MISSION_ASSURANCE_COMMAND=<command>
MISSIONOS_ALLOW_MISSION_ASSURANCE_COMMAND_OVERRIDE=1
```

## Verification

Focused contract tests:

```bash
PYTHONPATH=.:packages/missionos-core/src \
  pytest -q \
  tests/contract/test_mission_assurance_agent_form2a.py \
  tests/contract/test_mission_incident_continuation_graph.py \
  tests/contract/test_px4_live_mission_assurance.py \
  tests/contract/test_missionos_play_live_sitl.py \
  tests/contract/test_missionos_play_delivery.py
```

Loopback production boundary:

```bash
PYTHONPATH=.:packages/missionos-core/src \
  python scripts/smoke_mission_assurance_form2a_gateway.py

PYTHONPATH=.:packages/missionos-core/src \
  python scripts/smoke_operator_chat_mission_incident_graph.py
```

The loopback smokes cover payload advisory, invalid LLM output, blocked Action
Feasibility, and the shared incident judgment paths. The operator-chat smoke
also submits explicit fixture approval, performs fresh production Rules
revalidation, and queues one request at a fixture active-runner boundary through
the continuation graph. It does not run PX4 SITL, invoke MAVLink or hardware,
observe the action's effect, prove safety, prove physical execution, or prove
mission completion.

### E2E / Runtime Verification

The complete operator-facing path starts at the installed MissionOS CLI. It
does not invoke the PX4 runner directly:

```bash
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:packages/missionos-sitl/src \
TASK_STORE_DB_PATH=/tmp/missionos-ma-e2e-tasks.db \
MEMORY_DB_PATH=/tmp/missionos-ma-e2e-memory.db \
AUDIT_LOG_PATH=/tmp/missionos-ma-e2e-audit.jsonl \
MISSION_DESIGNER_REALISM_WIND_MEAN_MPS=4.0 \
MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG=90 \
MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED=1 \
MISSIONOS_AGENT_RUNTIME_ADK_ENABLED=1 \
MISSIONOS_AGENT_RUNTIME_TIMEOUT_SECONDS=90 \
MISSIONOS_LLM_BACKEND=ollama \
MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_OLLAMA_MODEL=llama3:latest \
MISSIONOS_AGENT_MISSION_ASSURANCE_AGENT_OLLAMA_MODEL=llama3:latest \
missionos \
  --gateway-url http://127.0.0.1:18907 \
  --state-path /tmp/missionos-ma-e2e-state.json \
  --json-output \
  mission-assurance-e2e \
  --yes \
  --autostart \
  --enable-live-sitl \
  --poll-interval 1
```

### Feasible-action intervention contract

For an action-producing Recovery proposal, the live order is:

```text
Runtime Recovery Agent proposes a vehicle-level candidate
-> source Action Feasibility is materialized
-> MissionAssuranceAgent judges mission-level alignment
-> accepted actions receive fresh dispatch-time revalidation
-> existing human approval boundary may dispatch
```

Recovery and MissionAssuranceAgent receive the same source mission context but
answer different questions. Recovery proposes the best bounded vehicle-level
recovery candidate; it does not decide final mission alignment.
MissionAssuranceAgent may return `hold`, `continue`, or
`operator_escalation` for a technically feasible candidate. Such a response is
recorded as an Assurance suppression, not as a Rules failure:

- `guard_status=no_dispatch`
- `dispatch_prevented_by_mission_assurance=true`
- `suppression_source=mission_assurance_agent`
- `selected_recovery_action=null`
- `dispatch_request_sent=false`
- no command ACK, runtime effect, or completion claim
- a separate post-suppression re-observation artifact

The deterministic A/B contract freezes the MissionSituation input digest,
Recovery proposal and source-result digest, source Action Feasibility digest,
telemetry cursor, policy digest, executor availability, and route approval
state. Case A changes only the MissionAssuranceAgent response to `return` and
must create a fresh Recovery approval request without dispatch. Case B changes
only that response to `hold` and must produce `no_dispatch` with Assurance as
the sole suppression source. A separate contract supplies a fresh scoped
Recovery approval and proves that revalidation is required before
`dispatch_eligible`.

Continuation intervention is a separate contract. When Recovery judges
`continue` but MissionAssuranceAgent judges `hold` or `operator_escalation`,
there is no Recovery action candidate and therefore no dispatch to suppress.
The graph and PX4 projection must record:

- `guard_status=no_dispatch`
- `mission_continuation_prevented_by_mission_assurance=true`
- `dispatch_prevented_by_mission_assurance=false`
- `suppression_source=mission_assurance_agent`
- `suppressed_recovery_response=continue`
- no Action Feasibility, approval request, selected action, dispatch, ACK, or
  effect claim
- a separate post-suppression re-observation artifact

The reverse no-action disagreement is not suppression. For example,
`Recovery=hold` and `MissionAssuranceAgent=continue` returns
`operator_escalation` with `alignment_status=agent_disagreement`; Mission
Assurance does not silently overrule the more conservative Recovery response.

The reverse disagreement contract covers the case where Recovery produces a
no-action response such as `continue`, while MissionAssuranceAgent requests an
action such as semantic `return`. MissionAssuranceAgent cannot manufacture the
missing backend Recovery candidate. The guard therefore records:

- `guard_status=operator_escalation`
- `agent_disagreement_observed=true`
- `agent_disagreement_kind=assurance_action_without_recovery_action_candidate`
- `agent_disagreement_resolution=operator_escalation`
- no Action Feasibility artifact, Recovery approval request, selected action,
  dispatch, ACK, or runtime-effect claim

This differs from `Recovery=return_to_launch` and `Assurance=hold`: in that
direction a concrete feasible proposal exists and Assurance suppresses it. In
the reverse direction no action candidate exists, so Assurance cannot replace
Recovery and the decision returns to the operator.

An accepted `return` response with source feasibility `verified_feasible`
still ends at `awaiting_operator_approval` when Recovery approval is absent.
Route approval alone must not create Recovery selection, revalidation,
dispatch, ACK, RTL-state, or physical-execution evidence.

### Operator evidence projection

`missionos operate --task-id <task>` keeps the two Agents separate after the
task reaches a terminal status. It displays the Runtime Recovery Agent proposal
first, the MissionAssuranceAgent response second, then feasibility revalidation,
human approval consumption, selected action, observed PX4 state, and command ACK
as distinct facts. The display reads persisted artifacts and creates no approval
or dispatch authority.

When the Mission Assurance scenario has no WGS84 route, `missionos map
--task-id <task> --snapshot` renders the persisted PX4/Gazebo local X/Y flight
profile. Planned `route_target_z_m` is converted from NED to altitude-up only for
display; observed `local_z_m` is shown exactly as persisted. The Recovery marker
is the latest observed point at the evidence boundary. It must not be described
as an observed final RTL/home position because no final X/Y sample establishes
that fact.
