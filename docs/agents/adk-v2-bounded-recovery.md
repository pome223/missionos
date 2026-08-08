# ADK v2 Bounded Recovery Contract

This contract routes an explicit failed verifier verdict to one new
approval-pending Recovery proposal. It does not retry the failed dispatch,
reuse the prior approval, or execute the proposed recovery.

## Activation and Trigger

Recovery routing is a separate opt-in on top of canonical HITL and guarded
execution:

```text
MISSIONOS_ADK_V2_HITL_ENABLED=1
MISSIONOS_ADK_V2_GUARDED_EXECUTION_ENABLED=1
MISSIONOS_ADK_V2_RECOVERY_ENABLED=1
```

The recovery node runs only when the guarded execution receipt contains the
explicit verdict `verifier_status=failed`. `false`, missing, `not_run`, or
`unverified` evidence does not become a verifier failure and creates no
Recovery proposal.

## Proposal Boundary

The failure router persists `missionos_adk_v2_recovery_proposal.v1` with:

- the same root MissionOS task ID and a new recovery task ID
- a hash of the prior verifier and receipt evidence
- one bounded `hold_and_reconcile_failed_verifier` proposal
- a new recovery proposal ref
- a new approval-request ref
- a new bounded-action ref
- a new pending dispatch ref
- `maximum_recovery_attempts=1`

The prior approval is fixed as non-reusable. Both the bounded-action and
dispatch refs must differ from the failed attempt before the graph reports
`recovery_approval_pending`.

The recovery result fixes these facts:

- `proposal_only=true`
- `approval_request_created=true`
- `new_human_approval_required=true`
- `approval_created=false`
- `dispatch_authority_created=false`
- `executor_invoked=false`
- `external_sender_invoked=false`
- `automatic_recovery_executed=false`
- `automatic_redispatch_performed=false`
- `physical_execution_invoked=false`
- `progress_counted=false`

The first dispatch facts remain available as prior history. They must not be
copied into the Recovery proposal as new authority or execution.

## Resume and Loop Limit

The recovery node is `rerun_on_resume=true`, but the consumed `RequestInput`
prevents duplicate HTTP resume from re-entering it. The node emits one proposal
and stops. A later recovery execution must start a new canonical approval flow,
reload fresh telemetry, run fresh dispatch-time checks, and claim the new
`dispatch_ref`.

This is a bounded one-attempt recovery transition, not an automatic repair
loop. Another verifier failure requires a separately created proposal epoch and
fresh human approval.

## E2E / Runtime Verification

Run a disposable Redis instance, then:

```text
REDIS_URL=redis://127.0.0.1:6379/0 \
REDIS_SESSION_NAMESPACE=missionos:adk-v2:recovery-smoke \
PYTHONPATH=packages/missionos-cli/src:. \
python scripts/smoke_adk_v2_recovery_gateway.py
```

The real loopback Gateway smoke uses an explicitly marked fixture executor and
fixture failed-verifier verdict. It proves the six-node route, one prior
dispatch attempt, new refs, fresh approval requirement, duplicate-resume
suppression, same-task trace, artifact persistence, and Redis cleanup.

Limitations: no external sender, simulator, hardware, observed effect, positive
verifier result, Recovery approval, Recovery execution, completion, physical
execution, or mission progress is claimed.
