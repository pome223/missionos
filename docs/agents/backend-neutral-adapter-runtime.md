# Backend-neutral adapter runtime contract

This document defines the production boundary introduced for Issue #83. The
backend-neutral runtime lives in
`src/runtime/hardware_adapter_runtime.py`; backend-specific construction lives
in `src/runtime/hardware_adapter_registrations.py`.

## Authority split

```text
LLM or caller proposes a bounded request.
Gateway records preparation and human approval.
Core dispatch authority is single-use.
Registered adapter translates and invokes its backend.
Core verifier evaluates adapter evidence plus runtime invocation evidence.
Repair remains a separate decision epoch.
```

An adapter may:

- publish capabilities and preflight reasons
- compile a backend dispatch candidate
- validate that a supplied approval matches its local action
- dispatch, observe state/progress, and request safe stop
- return adapter-local evidence

An adapter must not:

- mint operator approval
- reuse approval for another adapter, action ref, or action kind
- reuse an expired approval or bind one approval to another preparation
- issue the Core verification verdict
- promote ACK to motion, success, mission completion, or delivery completion
- promote simulator evidence to physical execution

## Registry boundary

`HardwareAdapterRegistry` maps an exact `adapter_id` to an injected factory.
Duplicate and unknown identifiers fail closed. The common prepare, dispatch,
and verification code contains no Unitree or Nav2 identity branches.

The default production Gateway registry currently registers
`unitree_sdk2_mujoco_adapter.v1`. A Nav2 runtime registers
`ros2_nav2_ground_robot_adapter.v1` with an injected Nav2 client. Registration
is configuration, not approval or dispatch authority.

## Artifacts and routes

The production Gateway exposes:

- `POST /missionos/hardware-adapters/prepare`
- `POST /missionos/hardware-adapters/approve-and-dispatch`

Preparation writes `missionos_hardware_adapter_preparations` with
`dispatch_request_sent=false` and no authority. Approved dispatch atomically
claims that exact preparation by ref and SHA-256, writes a distinct operator
approval, consumes single-use dispatch authority, and records validation and
runtime result artifacts.

The generic Gateway approval is valid for at most two minutes and is bound to
the exact adapter id, action ref, action kind, preparation ref, and preparation
SHA-256. Missing expiry, excessive lifetime, expiry, or any binding mismatch
fails before adapter dispatch.

Requests must explicitly supply telemetry freshness, heartbeat, geofence, and
operating-volume observations. There are no implicit healthy defaults. Missing
telemetry is invalid input; explicit unhealthy telemetry produces a blocked
preflight.

Preparing the same request again returns the existing preparation and preserves
its lifecycle state. It must not reset a claimed or terminal preparation to
`prepared`; a caller needs a new action ref for a new decision epoch.

The principal schemas are:

- `missionos_hardware_adapter_runtime_request.v1`
- `missionos_hardware_adapter_preparation.v1`
- `missionos_hardware_adapter_runtime_result.v1`
- `missionos_hardware_adapter_verification.v1`

`dispatch_status=unknown` and `dispatch_request_sent=null` are required when an
adapter raises after dispatch may have escaped. The runtime must not rewrite
that ambiguity as `false`. Factory construction fails before dispatch and is
therefore recorded as `blocked`, not `unknown`.

Every claimed preparation must reach a terminal lifecycle. Route failures move
it to `failed`; successful runtime return moves it to `verified` or
`blocked_or_unverified`. A failed terminal CAS is recorded as a reconciliation
error and returned as HTTP 409 instead of being ignored.

## Verification contract

Core verifies an adapter action only when all of these are true:

- approval matches adapter id, action ref, and action kind
- approval matches the exact preparation ref and SHA-256 and has not expired
- dispatch evidence says the request was sent
- accepted ACK was observed
- backend state was observed
- runtime progress was observed
- adapter-local completion was observed
- every supplied `runtime_invocation_evidence.v1` record validates
- every runtime invocation is source-bound to the adapter, action, preparation
  ref/SHA-256, and operator approval ref for this attempt
- no blocking reason remains

Without valid runtime invocation evidence, adapter-local completion is retained
as adapter evidence but is not promoted to the Core verdict. The verdict scope
is the adapter action only. It never establishes mission completion, delivery
completion, or physical execution.

Abort/safe-stop is recorded separately and never counted as completion.

## Shared conformance suite

`tests/contract/test_hardware_adapter_conformance.py` runs the same assertions
unchanged over Nav2 and Unitree registrations. It covers:

- proposal without dispatch
- missing and mismatched approval
- stale, unbounded, and preparation-mismatched approval
- missing safety telemetry fields
- ACK without progress
- missing runtime invocation evidence
- mixed runtime invocation evidence from another preparation
- stale telemetry
- unknown capability
- adapter failure with unknown dispatch state
- separate abort semantics
- unregistered adapter

## Runtime smoke and limits

Run:

```bash
PYTHONPATH=packages/missionos-cli/src:. \
  python scripts/smoke_backend_neutral_unitree_gateway.py
```

The smoke starts the production Gateway on loopback and uses the real Unitree
subprocess bridge client against a deterministic fixture process. It covers
prepare, rejected missing approval, approved dispatch, ACK, state, progress,
Core verification, replay rejection, stale telemetry, unsupported action, and
unknown adapter handling. It also confirms that omitting required telemetry is
an HTTP 400 input failure.

It does not start the upstream Unitree MuJoCo process or SDK2, touch hardware,
or verify a mission. Those require separate opt-in environment and hardware
evidence.
