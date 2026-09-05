# ADK v2 Runtime Inventory

This inventory defines the completion boundary for the MissionOS ADK v2
migration. A `Runner` is an ADK application/session entrypoint; it is not by
itself evidence of legacy orchestration.

## Classification Rule

- `workflow_root`: the Runner receives an ADK v2 `Workflow`. All multi-stage
  control flow is expressed by Workflow nodes and dynamic `ctx.run_node()`
  children.
- `single_agent_root`: the Runner receives one standalone ADK Agent. There is
  no local multi-stage Python orchestration to convert. Wrapping this in a
  one-node Workflow would not add graph semantics.

`tests/contract/test_adk_v2_runner_inventory.py` AST-scans every Python module
under `src/`. CI fails when a Runner is added, removed, or moved to a different
scope without updating this contract and reviewing its classification.

## Workflow Roots

| Runtime boundary | Workflow purpose |
| --- | --- |
| `ControlLoop.run` | Planner, Executor, Verifier, and bounded Repair loop |
| `GatewayServer._run_gateway_conversation_workflow` | routing, optional specialist, and root synthesis |
| `GatewayServer._select_route_for_message` | route-only child Agent execution |
| `start_missionos_canonical_approval_hitl` | canonical approval RequestInput and guarded continuation |
| `resume_missionos_canonical_approval_hitl` | checkpoint restore, fresh validation, guarded continuation, and bounded recovery routing |
| `_run_missionos_conversation_graph_async` | Chief, deterministic specialist selection, specialist, and Safety Critic proposal graph |
| `_run_mission_incident_graph_async` | Runtime Recovery Agent proposal, source Action Feasibility, MissionAssuranceAgent judgment, and the decision checkpoint before the downstream human/runtime boundary |
| `_run_mission_incident_continuation_async` | frozen incident binding, explicit human approval, dispatch-time Rules revalidation, Executor, Verifier, and next observation without rerunning either Agent |

## Standalone Agent Roots

| Runtime boundary | Single Agent responsibility |
| --- | --- |
| `_run_cli` | interactive root Agent session |
| `_run_channels` | channel root Agent session |
| `_invoke_adk_gemini_async` | dialogue-router JSON judgment |
| `_invoke_adk_gemini_repair_text_async` | one repair-plan judgment |
| `_invoke_adk_gemini_response_text_async` | one Form 2A response-plan judgment |
| `_invoke_adk_agent_text_async` | legacy sequential rollback/shadow comparison Agent invocation |
| `_invoke_runtime_recovery_agent_text_with_tools_async` | one Runtime Recovery Agent with a bounded FunctionTool |
| `_invoke_adk_response` in the Mission Assurance Agent | one backend-neutral mission-response judgment |
| `_invoke_chief_route_function_tool_async` | one Chief route-planning Agent with a bounded FunctionTool |
| `_invoke_adk_gemini_response_text_async` in the arm/disarm planner | one props-removed bench proposal judgment |
| `_invoke_adk_perception_response_async` | one TurtleBot perception judgment |
| `_invoke_adk_response_text_async` | one TurtleBot recovery judgment |
| `SubagentManager._worker_loop` | one explicitly requested background Agent job |

These entries describe individual inference roots, not alternative operational
composition roots: Recovery and Assurance remain leaves of the governed incident
chain whenever their result can reach recovery approval or dispatch.

## Completion Boundary

The runtime migration is complete when all of these remain true:

1. The production dependency is pinned to ADK v2 (`>=2.5.0,<3.0.0`).
2. Every production Runner is classified by the AST contract above.
3. Every multi-stage Agent chain uses a `Workflow`; no production path manually
   appends ADK session events or implements a custom `_run_async_impl` loop.
4. The conversation proposal Workflow is the default. The sequential
   Chief/specialist/Safety Critic code remains only for explicit rollback or
   bounded shadow comparison.
5. Approval, dispatch, execution, receipt, observed effect, verifier result,
   recovery success, and progress remain MissionOS facts outside Agent output.
6. HITL, guarded execution, and recovery remain opt-in because adopting ADK v2
   does not authorize side effects.

The Mission Incident judgment Workflow is the single composition root for live
Recovery and Mission Assurance judgment. Backend adapters may supply
observations and executors, but they must not bypass this graph by creating a
Recovery proposal that is directly eligible for operator approval. The
judgment graph stops at `awaiting_operator_approval`, `no_dispatch`, or
`operator_escalation`; it never turns ADK node completion into approval or
execution truth.

Standalone diagnostic Agent calls may remain outside this Workflow because
they cannot create dispatch authority. At the PX4 Gateway side-effect boundary,
every stored Agent proposal must be a v4 proposal with an accepted Mission
Incident graph whose hash and six-node judgment sequence validate. v1-v3 remain
readable evidence only. The same fail-closed gate runs before both an
active-runner request write and emergency MAVLink dispatch.

The operator natural-language recovery route follows the same rule. It may run
the hosted Recovery Agent first to obtain a bounded candidate, but that result
is then supplied to the Recovery node of the Mission Incident Workflow. A v4
durable proposal is stored only when Rules report `verified_feasible` and
Mission Assurance returns the response kind aligned with that Recovery action.
`continue`, `hold`, or `operator_escalation` outcomes retain the graph judgment
but create no approval candidate and invalidate an older pending candidate for
the task. Graph failure returns a fail-closed response and also invalidates an
older pending candidate; the route never falls back to a v3 proposal.

When Recovery returns `continue` and Mission Assurance instead returns `hold`
or `operator_escalation`, the graph records Mission Assurance as preventing
mission continuation. It does not claim dispatch suppression because no action
candidate existed. Other mismatched no-action responses remain an Agent
disagreement and return to the operator.

The `missionos play` battery/GPS observer and the `missionos play delivery`
route-deviation observer also enter this Workflow. Their result artifacts keep
the Recovery result for compatibility and additionally expose the complete
`missionos_mission_incident_graph`; neither runtime calls Recovery as a standalone
decision path. The PX4 live route-deviation adapter likewise treats a missing
Recovery or MissionSituation graph projection as blocked evidence. It does not
reconstruct the former standalone path outside the graph.

TurtleBot3 supplies its existing Recovery planner result and Nav2/Core
feasibility evidence through `turtlebot3_mission_incident.py` to the same
judgment Workflow and the same configured MissionAssuranceAgent. It does not
define a separate Nav2 Assurance policy. Its existing recovery checkpoint
stores the graph and binds the exact target poses, parameters and resume state.
The Gateway checks that binding before issuing approval or claiming the
checkpoint; the Nav2 continuation checks it again before execution. A missing,
suppressed or changed judgment cannot authorize movement. Legacy checkpoints
remain readable but cannot dispatch without a new judgment.

Recovery movement (`avoid_obstacle`, `return_home`, `reroute`) requires fresh
human approval, including when a prior mission envelope or promotion would
have permitted it automatically. LLM failure cannot turn a deterministic
fallback into an approvable movement. A non-approvable checkpoint retains the
existing pending-human lifecycle with `approval_eligible=false` and
`operator_guidance_required=true`; pending status alone is not authority.
Operator revisions require fresh Recovery and Assurance judgment, not silent
parameter replacement beneath an old graph. Approval does not rerun either LLM.

The TurtleBot3 continuation invokes the existing Nav2 runtime boundary, which
already verifies the bounded recovery before route resume. The shared Verifier
and observation nodes project that checkpoint's actual closed-loop outcome and
observation hash, not an ACK-as-effect inference. A successful recovery does
not alone establish delivery completion or physical execution. Model-fixture
contract tests establish accepted/suppressed behavior and binding; only an
opt-in `missionos chat --robot turtlebot3` run can establish live model and
simulator behavior.

`declared_next_sequence` remains a declaration at the judgment boundary. When
the operator later submits explicit Recovery approval, the Gateway starts the
separate `missionos_mission_incident_continuation_v1` Workflow. It first
validates the frozen judgment graph hash and id, records the human approval,
runs dispatch-time Rules revalidation, invokes the applicable executor
boundary, records Verifier output, and attempts the next observation. The
resulting `missionos_mission_incident_continuation_graph` artifact contains the
actual seven-node sequence.

This is one governed incident chain split into two phase-specific Workflows at
the human wait. It is intentionally not an ADK resume of the judgment Workflow:
Recovery and MissionAssuranceAgent are not rerun after the operator approves,
and the continuation records both `recovery_agent_rerun=false` and
`mission_assurance_agent_rerun=false`. A queue receipt or command ACK is not an
execution effect; the current Gateway verifier leaves `effect_observed=false`
until later runtime evidence establishes that fact. An unchanged telemetry
cursor is recorded as `awaiting_fresh_post_dispatch_observation`, not as a new
MissionSituation.

This does not mean every Agent must be rewritten as a Workflow. In ADK v2, a
standalone Agent is already a valid root node. Graph structure is required when
MissionOS composes multiple decisions or execution stages.
