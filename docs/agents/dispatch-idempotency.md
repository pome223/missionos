# Dispatch Idempotency Contract

This contract defines the MissionOS `dispatch_ref` baseline that must be used
before an ADK workflow can approach an external execution boundary. It does not
give ADK, an LLM, or a completed graph node approval or dispatch authority.

## Identity and Request Binding

One `dispatch_ref` identifies one bounded external send identity. MissionOS
binds it to the SHA-256 of the canonical request payload before sender
invocation.

- the same `dispatch_ref` and the same request hash may return an existing
  receipt, but may not send again
- the same `dispatch_ref` with a different request hash fails closed as
  `dispatch_ref_payload_mismatch`
- a changed action, target, parameter envelope, or Recovery proposal requires a
  new bounded action, approval, and `dispatch_ref`
- ADK event IDs and node IDs are correlation fields only; they are not the
  idempotency identity

The durable implementation is `DispatchAuthorityTable` in
`src/gateway/missionos_dispatch_runtime.py`. Authority-token consumption and
dispatch idempotency are separate checks. Passing one does not imply passing
the other.

## Required Sender Sequence

An execution boundary must use this order:

1. Reload and validate the canonical MissionOS approval, bounded action,
   deterministic gate, fresh telemetry, backend descriptors, and prior
   receipts.
2. Call `claim_dispatch_ref()` with the stable `dispatch_ref` and complete
   canonical request payload.
3. Stop if `send_permitted=false`.
4. If `idempotency_status=existing_receipt`, return the recorded receipt
   without invoking the sender.
5. If `idempotency_status=unknown_send_outcome`, stop for explicit receipt or
   external-state reconciliation. Do not retry automatically.
6. Immediately before invoking the external sender, call
   `mark_dispatch_send_started()`. Invoke the sender only when this call returns
   `send_permitted=true`.
7. Record the sender return with `record_dispatch_receipt()`.
8. Record ACK, observed effect, verifier verdict, and completion in their own
   canonical evidence stages.

`claim_dispatch_ref()` and `mark_dispatch_send_started()` are both necessary.
A duplicate call to either boundary does not permit another send.

After a restart, an execution wrapper may query a stored receipt before fresh
preflight because the original single-use approval token may already be
consumed. It must first match the stored approval, candidate, proposal hash,
and bounded-action correlation. This is result recovery only; it cannot permit
a sender call. Unknown outcomes still stop before preflight and never retry.

## Crash and Retry Semantics

MissionOS assumes an unknown outcome after any ambiguous interruption between
send claim and durable receipt.

| Durable state | Automatic resend | Required handling |
| --- | --- | --- |
| no claim | prohibited until authority and fresh checks pass | create the first claim |
| claimed, no confirmed cancellation | prohibited | reconcile whether sender invocation occurred |
| send started, no receipt | prohibited | reconcile external state or receipt |
| sender error with unknown outcome | prohibited | operator/recovery reconciliation |
| receipt recorded | prohibited | return existing receipt |
| cancelled before send with proof sender was not invoked | allowed after fresh checks | create a new attempt for the same payload |

`cancel_dispatch_before_send()` is the only safe-retry transition. It rejects a
cancellation after send may have started. A retry after cancellation increments
the attempt ordinal but preserves the same request binding.

## Receipt Claim Boundaries

An idempotency receipt proves only that a sender returned a receipt for the
claimed identity. The ledger copies these fields only when the sender receipt
explicitly contains them:

- `ack_observed`
- `effect_observed`
- `verifier_passed`
- `completion_claimed`
- `physical_execution_invoked`

Missing fields remain false. Sender return, ACK, effect, verification,
completion, and physical execution are separate facts.

## Deployment and Locking Scope

The file-backed table is process-safe only inside one verified file-lock
domain. Its thread lock and POSIX `fcntl.flock` coordinate independent Python
processes that open the same state and lock files on one host, or on a shared
filesystem whose `flock` semantics have been explicitly verified.

This is not a distributed lock or consensus service. Two Gateway replicas with
independent volumes can each grant the first claim for the same `dispatch_ref`.
MissionOS must therefore use a single dispatch-authority writer, a shared
state/lock volume with verified locking semantics, or a future transactional
central ledger before enabling horizontally replicated dispatch.

The process-boundary contracts prove both the atomic claim and the complete
guarded execution path across independent Python interpreters on one host. The
canonical runtime smoke starts eight processes on one barrier with the same
`dispatch_ref` and asserts that exactly one process reaches the fixture sender,
exactly one ledger attempt exists, and one receipt is recorded:

```bash
python scripts/smoke_atomic_dispatch.py
```

This proof does not invoke an external transport or physical executor. It also
does not prove cross-host, independent-container-volume, unverified
network-filesystem, or regional failover exclusion.

## ADK v2 Migration Gate

The public runtime must wire this ledger into its future guarded execution node
before an ADK v2 workflow can approach a sender. That later node must also
require restart-safe resume, canonical human approval, fresh dispatch-time
revalidation, and same-task audit correlation. Adding this ledger alone does
not authorize dispatch and does not mean that ADK v2 migration is complete.

ADK automatic retry remains disabled for dispatch and hardware-affecting
writes. A checkpoint or completed node must not be reported as a send, ACK,
observed effect, verifier pass, completion, or mission progress.
