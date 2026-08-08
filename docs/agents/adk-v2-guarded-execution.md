# ADK v2 Guarded Execution Contract

This contract permits the canonical approval HITL graph to invoke an existing
MissionOS execution boundary once. ADK orders the nodes; MissionOS still owns
approval validation, deterministic rules, dispatch authority, idempotency,
executor invocation, receipts, observations, verification, and progress.

## Opt-in Modes

Guarded execution is disabled unless both canonical HITL and execution are
enabled:

```text
MISSIONOS_ADK_V2_HITL_ENABLED=1
MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENABLED=1
REDIS_URL=redis://127.0.0.1:6379/0
```

Normal production execution also requires the existing simulator opt-ins. The
public-safe Gateway smoke instead sets:

```text
MISSIONOS_ADK_V2_GUARDED_EXECUTION_FIXTURE=1
```

That fixture flag must never be enabled in production. Its adapter records one
fixture executor invocation while fixing `external_sender_invoked=false` and
all ACK, effect, verifier, completion, progress, and physical-execution facts
to false.

## Dispatch Sequence

After the exact canonical `approval_ref` resumes the graph, MissionOS performs:

1. reload the Form 2A selection, approval token, and human-review artifact
2. revalidate approval expiry and the proposal/hash/action/dispatch binding
3. reload the telemetry-derived Form 1 source and verify its SHA-256
4. enforce `MISSIONOS_ADK_V2_DISPATCH_SNAPSHOT_MAX_AGE_SECONDS`
5. reload backend opt-in, source policy, and bounded-action envelope state
6. atomically claim the stable `dispatch_ref` and complete request SHA-256
7. rerun the complete preflight and cancel before send if it changed
8. register and validate a canonical MissionOS dispatch authority
9. durably mark send-start immediately before the execution boundary call
10. record the returned receipt without inferring missing facts

The ADK execution node has no automatic retry. A callback exception after
send-start is persisted as `unknown_send_outcome`; the executor was invoked,
but external send and physical outcome remain unknown and automatic retry is
prohibited.

## Duplicate and Receipt Semantics

A repeated HTTP resume finds no pending `RequestInput` and does not re-enter
the graph. A repeated guarded call with the same canonical request returns the
durable receipt and does not invoke the executor. A changed request under the
same `dispatch_ref` fails closed.

The following remain separate facts:

- canonical human approval validated
- dispatch authority created and validated
- `dispatch_ref` claimed
- send-start persisted
- executor callback invoked
- external sender invocation reported
- ACK observed
- effect observed
- verifier passed
- completion claimed
- physical execution invoked
- mission progress counted

Receipt replay reports the prior receipt separately and fixes current-call
`executor_invoked=false` and `automatic_redispatch_performed=false`.

## Same-task Audit Trace

The canonical Form 2A selection supplies a stable task identity. If no upstream
TaskStore ID is present, MissionOS derives
`missionos_form2a_task:<response_selection_id>`. Every persisted ADK node event
is projected into `missionos_adk_v2_same_task_audit_trace.v1` with:

- ADK event ID, invocation ID, author, node path, and output target
- the same canonical task ID
- `approval_ref`
- `mission_response_candidate_ref`
- proposal SHA-256
- `bounded_action_ref`
- `dispatch_ref`

ADK event IDs remain correlation fields only. Node completion is explicitly
not external execution and does not count progress.

## E2E / Runtime Verification

Run a disposable Redis instance, then:

```text
REDIS_URL=redis://127.0.0.1:6379/0 \
REDIS_SESSION_NAMESPACE=missionos:adk-v2:guarded-smoke \
PYTHONPATH=packages/missionos-cli/src:. \
python scripts/smoke_adk_v2_guarded_execution_gateway.py
```

The smoke starts the real MissionOS Gateway on loopback and calls the canonical
HITL start, human approval, and resume routes. It verifies one dispatch claim,
one fixture executor invocation, duplicate-resume rejection, receipt replay
without reinvocation, a five-node same-task trace, and Redis cleanup.

Limitations: the Form 2A artifacts and executor are fixtures. No external
sender, simulator, hardware, ACK, observed effect, verifier pass, completion,
physical execution, or mission progress is claimed.
