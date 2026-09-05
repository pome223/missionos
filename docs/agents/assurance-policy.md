# Mission Assurance policy v1 contract

## Authority and scope

`LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.`

A YAML file is an unapproved contract. The local operator command records explicit
approval of its canonical SHA-256. It never records an action approval on behalf
of that operator. An Assurance judgment cannot issue or extend authority.

The shared continuation graph accepts an optional trusted Python
`policy_authorization_handler`. A JSON request cannot inject this handler.
With no handler the existing individual approval requirement is unchanged.
An executable CLI fixture composition is available. The normal TurtleBot3
Gateway mission execution entrypoint also installs policy continuation when
`MISSIONOS_ASSURANCE_POLICY_DB` names a trusted local store containing exactly
one active policy for the proposal's mission ID. PX4 remains individual approval.
Real-simulator evidence must be reported separately from fixture tests.

## Strict schema

The source schema is `AssurancePolicy` in `src/intelligence/mission_assurance_policy.py`.
Unknown keys, duplicate YAML keys, aliases, non-finite numeric ranges, inverted
ranges and non-string keys are rejected. Use a quoted, timezone-aware expiry.

- `mission_id` binds authority and the lifetime budget to one mission.
- `mission_contract_sha256` fixes the exact original mission contract, including
  objective and destination. A changed contract requires explicit new approval
  and a fresh Assurance judgment.
- `execution_scope` is `fixture` or `simulator`. It grants no hardware authority.
  The composition layer must supply scope from trusted runtime state.
- `actions` is an exact allowlist of executor action names. `parameters` defines
  the exact set of permitted numeric parameters with inclusive finite ranges.
  Unknown or omitted parameters are rejected. `equals` supports exact boolean/string
  values, `properties` supports exact object fields, and `items` plus bounded
  `min_items`/`max_items` supports waypoint arrays. `parameter_variants` permits
  a list of exact alternative parameter shapes instead of `parameters`.
  No free-form conditions, arbitrary expressions, geographic polygons or generic
  retry verbs are evaluated.
- `preserve` names predicates with independent observation/verification bindings.
  All must be exactly boolean true in fresh, source-referenced runtime facts.
  Missing, false or unknown is insufficient. True current predicates do not prove
  that a candidate will preserve them: the existing action feasibility boundary
  and the adapter must also evaluate the proposed action against these conditions.
- `max_total_actions` and per-action `max_uses` bound attempts, not success claims.
  Each bounded dispatch reservation counts once, including initial and failed
  attempts. Replanning, restarts and replacement policy versions do not reset counts.
- `max_observation_age_seconds` rejects stale and future observations.
- `on_unresolved=request_human` returns a blocked result for the operator surface.
  It is not an actuator-level safe-stop instruction.

## Composition and trusted evidence

Before judging, load the operator-approved policy with `PolicyStore.context(sha)`
and include it in `mission_context.constraints.assurance_policy` of the incident
graph. The existing situation digest and frozen graph hash bind that content.
The model receives the policy through the actual MissionSituation prompt.

At continuation, supply `PolicyStore.handler(sha, observe)` as the optional
`policy_authorization_handler`. `observe()` reads trusted runtime facts:
`mission_id`, `execution_scope`, `mission_contract_sha256`, `observed_at`,
`source_ref`, and `predicates`. Do not derive these from model output or a client
request body. Validate source/session provenance in the adapter. Current-predicate
checks are not trajectory feasibility or independent safety certification.

The continuation validates the frozen accepted graph and action, observes the
individual-approval or pending-policy boundary, runs existing dispatch-time
feasibility checks, then checks/reserves policy authority immediately before the
executor callback. The policy check compares the exact judged parameters with
the continuation request; in-range substitutions still require a fresh judgment.
Executor, Verifier and re-observation remain distinct callbacks and evidence facts.
An adapter must dispatch exactly these bound parameters and independently verify
both the intended effect and all preserve conditions after execution.

`human` and `shadow` only record eligibility and require individual approval;
they do not create delegated reservations. Their eligibility projection does not
simulate a counterfactual sequence's cumulative budget. `bounded` requires a
successful reservation even when the request also contains individual approval.

## Persistence and cancellation

SQLite `BEGIN IMMEDIATE` serializes budget checks and reservation inserts.
The primary key `(mission, proposal)` prevents repeated use. Committing a
reservation consumes authority before invocation; crashes or executor failures
never refund it. Revocation and expiry are checked again under the reservation
lock. Reservation is the authorization linearization point: cancellation after
reservation does not cancel that in-flight action. The runtime emergency-stop
boundary remains separate.

Keep the database under operator/runtime ownership. The local CLI relies on OS
account trust, not cryptographic identity: `--operator` is an audit label, not
identity authentication. DB files are mode 0600; neither LLM tool access nor
untrusted uploads may write the store. Replacing/deleting the store or choosing
a new mission identity is an operator administrative action, not an agent retry.
Do not expose approve/revoke directly through an unauthenticated remote API.

## Evidence

`human_approval_observed` means individual action approval only.
`authorization_source` distinguishes `individual_human_approval`,
`human_approved_policy`, and `none`. `policy_authorization` records the policy
hash, original operator/time, eligibility, reservation and rejection reasons.
A denied policy records no delegated authority. Dispatch, ACK, effect,
physical execution and completion remain independent fields.

The fixture CLI uses real production ADK incident/continuation graphs with
synthetic Recovery, Assurance, feasibility, executor and observation callbacks.
It proves policy plumbing and authority enforcement, not LLM judgment quality,
robot motion, task completion or physical safety.

## E2E / Runtime Verification

Run `PYTHONPATH=packages/missionos-cli/src:. .venv/bin/python scripts/smoke_assurance_policy.py`.
The script starts real CLI subprocesses and checks approval, in-scope execution,
replay rejection, parameter rejection, exhausted budget, revocation, and shadow
individual approval. It uses a temporary operator store and synthetic model and
executor IO. No Gateway HTTP, PX4, Nav2 or physical boundary is covered.


## Normal TurtleBot3 execution

`run_turtlebot3_home_mission_dispatch` loads the locally approved policy, places
it in the real Assurance prompt, and runs the ordinary mission executor. At an
awaiting checkpoint it calls the existing common continuation graph. On policy
authorization it resumes the ordinary Nav2 executor, Verifier and re-observation.
Fresh follow-up checkpoints undergo fresh Recovery/Assurance judgment and new
reservations. There is no recursive HTTP dispatch and no operator-approval flag
fabricated to satisfy an older gate.

The `PolicyGrant` is a trusted in-process object bound to the entire checkpoint;
request JSON cannot supply it. Its scope is checked again by runtime resume
validation and independently by outcome verification. Lower Nav2 evidence carries
`authorization_source=human_approved_policy`. Physical-mode delegation is rejected.

The mission contract also binds a digest of initial approval ref, approval time,
and approved route hash. Approval refs alone are insufficient because current
TurtleBot3 proposal/approval IDs are deterministic. Reapproval of the same route
at a different time requires a new bound policy. Policy budgets still accumulate
for the stable mission identity; revoke old versions before replacing them.

The adapter currently supports `nav2_path_feasible` and
`mission_contract_unchanged` preserve predicates. Path feasibility uses a fresh
real Nav2 evaluation with both global and local costmap hashes. This proves a
bounded path check, not collision-free physical operation or payload integrity.
Other predicate names are rejected at binding until an observation adapter exists.
The configured autonomy envelope must require fresh recovery checkpoints, so the
old preapproved-envelope path cannot bypass policy checks.

`assurance_policy_execution` preserves every continuation and distinguishes
executor invocation count from actual dispatch count. Failed/unknown effects are
not successes. A policy rejection keeps the prior runtime result and pending
checkpoint available to the human operator. Reservations are never refunded.

For segment failures, Recovery receives the actual bridge `nav2_status` and
`goal_cancel_result_observed`, rather than an unconditional return-home
recommendation. A fault-injection request alone does not supply either fact.
Recovery may judge that one retry is appropriate. A selected `reroute` then
compiles the exact failed route segment into `target_x_m`, `target_y_m`,
`retry_failed_segment_required=true`, and `retry_count=1`. The failure cursor must
match the stored failed segment; there is no arbitrary model-supplied target.
The new candidate still needs dual-costmap feasibility, Assurance judgment,
authorization, fresh pre-dispatch validation and outcome verification. Policies
must explicitly allow that parameter shape. No local-map coverage or target-cost
check is relaxed to make a distant return-home candidate executable.

## Simulator comparison

`scripts/run_assurance_policy_turtlebot3_e2e.py` uses the actual Gateway HTTP
conversation and recovery-dispatch routes, real model configuration, and Nav2.
It explicitly submits operator-authorized test approvals in `human` mode;
`bounded` submits the initial mission and policy approvals only. Count these as
required approval boundaries, not measured human UI effort. Restart the dedicated
simulator between runs, use the same instruction/fault configuration, and include
the initial policy approval when comparing counts. Do not infer success-rate
improvement from one paired run.

Count episode approvals from `approval-events.json` and the authority in each
`recovery_closed_loop_cycles` entry. The runtime's
`fresh_recovery_operator_approval_count` describes the current invocation and
must not be treated as the cumulative count after several continuation calls.

### Completed comparison after retry integration (2026-09-05)

These simulator results were collected on the implementation before its public
port. They are not a new SITL run of the public checkout. The port is checked
separately with public contract tests, CLI subprocesses and a loopback Gateway
smoke with fixture model/executor IO. Raw experiment logs and task databases are
not part of this publication.

The public port passed 2,517 local tests, a 15-subprocess policy CLI fixture
smoke, and `smoke_operator_chat_mission_incident_graph.py` through real loopback
HTTP. That HTTP smoke uses fixture judgments and an executor queue; its effect
remains pending and it is not a new policy-driven Nav2/SITL completion result.

Using the same instruction and both fault-injection flags, both modes completed
all seven route segments after two verified recovery cycles: `avoid_obstacle`
and `reroute`. Each cycle recorded an ACK, observed executor effect, successful
outcome verification and authorization to resume the route.

| Episode outcome | Individual approval | Bounded policy |
| --- | --- | --- |
| Initial mission approval | 1 | 1 |
| Initial policy approval | 0 | 1 |
| Individual recovery approvals | 2 | 0 |
| Total approval boundaries | **3** | **2** |
| Verified recovery cycles | 2 | 2 |
| Route segments completed | 7 / 7 | 7 / 7 |

The policy continuations both recorded `human_approval_observed=false`,
`authorization_source=human_approved_policy` and `verifier_status=verified`.
Each outcome independently recorded `policy_authority_bound=true` and
`individual_human_approval_bound=false`. The prior reservation from the initial
experiment was retained; the new policy consumed two further reservations, with
no budget reset. The experiment policy was revoked and the dedicated simulator
stopped after evidence capture.

Three implementation gaps were corrected: an unconditional return-home
recommendation hid cancellation details from Recovery; generic recovery goal
evaluation omitted the planned `max_speed_mps` required by Core; and the HTTP
comparison client did not carry the Gateway task identity across a second
recovery approval. The human episode paused before sending its second request
while that client was repaired, then resumed the same pending task. Consequently
this comparison demonstrates one fewer approval boundary while completing the
route, but does not compare elapsed time. It is one completed episode per mode,
not statistical evidence of a higher success rate or a human usability study.

The final policy artifact preserves both frozen incident graph hashes and both
verified continuation records, but only the latest complete raw Assurance
proposal. Both continuations enforce frozen judgment validation; a future study
requiring analysis of every raw model judgment should archive each incident
graph at creation as well as its hash.

Validation passed 238 relevant policy/runtime/Core/planner contract tests and
11 recovery-client contract tests. Physical execution and delivery completion
remain unclaimed. The two-cycle runtime results here supersede the limited
initial comparison below; the earlier negative outcomes remain useful diagnostics.

### Initial simulator result (2026-09-05, before retry integration)

The Docker TurtleBot3/Nav2 runtime and actual DeepSeek `deepseek-v4-flash`
Assurance judgment were exercised through the HTTP entrypoint above, with both
post-recovery failure injection flags enabled. In each mode the first
`avoid_obstacle` recovery was verified: dispatch, ACK, observed movement,
obstacle clearance, and route resumption. The policy run recorded
`authorization_source=human_approved_policy` and
`individual_human_approval_bound=false`; the ordinary run recorded an individual
human approval. The policy run did not manufacture that approval artifact.

| Observed boundary | Individual approval | Bounded policy |
| --- | --- | --- |
| Initial mission approval | 1 | 1 |
| Initial policy approval | 0 | 1 |
| Individual recovery approval | 1 | 0 |
| Total approval boundaries | 2 | 2 |
| Verified recovery cycles | 1 | 1 |
| Route segments completed | 1 / 7 | 1 / 7 |

After a real injected cancellation, both runs proposed `return_home` but lacked
local target cost evidence (`recovery_local_target_cost_unavailable`). The
follow-up graph was not accepted and the policy run sent no second recovery.
Thus one live recovery without individual approval is demonstrated. Reduction
in total approval count, two successful consecutive live recoveries, and mission
completion under the repeated-fault scenario remain unproven. The two-cycle
contract test uses synthetic executor IO and does not close this live gap.

Earlier diagnostics are separate: a run without the bridge fault flag completed
all seven segments after one individual approval, but did not inject the second
failure. Another run exposed an Assurance `continue` response whose rationale
endorsed recovery; the graph rejected that inconsistency. The prompt now explains
that endorsement of a bounded recovery uses `replan`; Rules do not reinterpret
an inconsistent model response. Both paired runs above used that same prompt.

Validation also passed 207 focused contract tests, the 15-subprocess CLI fixture
smoke, and a real CLI binding to a simulator initial approval (including output
overwrite rejection). These are not a human usability study, statistical success
rate evaluation, physical execution, or delivery-completion evidence.
