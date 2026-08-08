# ADK v2 Proposal Graph Primary Promotion

This contract makes the ADK v2 conversation graph the default primary proposal
generator. It does not move MissionOS approval, dispatch, execution, receipt,
verifier, or recovery authority into ADK.

## Mode Precedence

The production wrapper evaluates the flags in this order:

| Condition | Proposal engine | Graph calls |
| --- | --- | --- |
| rollback is `1`, `true`, `yes`, or `on` | previous sequential pipeline | none |
| primary unset or true | ADK v2 proposal graph | one graph pipeline |
| primary is `0`, `false`, `no`, or `off`; shadow off | sequential compatibility pipeline | none |
| primary is false; shadow is true | sequential compatibility pipeline | one measurement-only shadow pipeline |

Rollback therefore wins if flags are accidentally enabled together. Default
primary mode also suppresses shadow mode so one request does not run two graph
pipelines. Explicit primary `0` exists for bounded comparison and staged
diagnosis; `ROLLBACK=1` is the named operational rollback control.

Boolean spellings are case-insensitive. Any other non-empty value is rejected
during Gateway startup, so an operator typo cannot silently leave the wrong
proposal engine active.

## Primary Result Adapter

The graph remains limited to Chief, deterministic specialist routing,
specialist judgment, Safety Critic judgment, and proposal finalization. The
adapter maps its result to `missionos_agent_runtime_result.v1`, which preserves
the Gateway call boundary.

Every primary graph result fixes these facts:

- `proposal_only=true`
- `approval_created=false`
- `dispatch_authority_created=false`
- `executor_invoked=false`
- `physical_execution_invoked=false`
- `outcome_observed=false`
- `progress_counted=false`

`proposal_guardrail_passed` means only that the bounded proposal passed the
agent-output and Safety Critic guardrails. It is not an approval or a gate-pass
for execution.

## Failure and Rollback

A primary graph exception returns `runtime_status=guardrail_blocked` with an
error-class label. It does not automatically rerun the request through the
sequential engine. This avoids duplicate model calls and avoids changing the
proposal engine invisibly during a request.

For an operator-controlled rollback, set:

```text
MISSIONOS_ADK_V2_GRAPH_ROLLBACK=1
```

Restart the Gateway process so it receives the flag. Runtime evidence then
reports `workflow_execution_mode=sequential_rollback` and
`adk_v2_graph_rollback=true`.

## Remaining Authority Boundary

Primary proposal mode still uses a one-shot in-memory graph. A separate,
opt-in Redis workflow now pauses for an exact canonical Form 2A `approval_ref`
and freshly validates the corresponding MissionOS artifact after resume. It is
defined in `docs/agents/adk-v2-canonical-approval-hitl.md` and creates no
dispatch authority.

Guarded execution is now available as a separate opt-in contract in
`docs/agents/adk-v2-guarded-execution.md`. A resumed checkpoint still cannot
replace fresh dispatch-time telemetry, policy, envelope, idempotency-ledger,
approval, backend, receipt, and verifier validation.

Explicit verifier failure may also create one bounded Recovery proposal under
its own opt-in contract. That proposal requires a fresh approval and cannot
automatically dispatch or re-execute.

The complete production Runner classification is maintained in
`docs/agents/adk-v2-runtime-inventory.md`.

## Runtime Verification

Run the default-primary production boundary through a real loopback Gateway
with an explicitly configured model backend:

```text
RUN_MISSIONOS_ADK_V2_PRIMARY_GATEWAY_SMOKE=1 \
MISSIONOS_LLM_BACKEND=<configured-backend> \
PYTHONPATH=packages/missionos-cli/src:. \
python scripts/smoke_adk_v2_primary_gateway.py
```

The smoke unsets all three graph rollout controls before Gateway startup. It
requires an HTTP 200 proposal response, `workflow_execution_mode` equal to
`adk_v2_graph_primary`, and three dynamic Agent children executed through
`ctx.run_node`. It fails if approval, dispatch, executor, physical execution,
observed effect, or progress authority appears in the response.

This is a hosted/local-model Gateway boundary check. It is not PX4, Nav2,
RoboCasa, hardware, or physical-execution evidence; those environments retain
their own opt-in runtime smokes and claim limits.
