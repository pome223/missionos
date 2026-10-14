# Inflight approval at the real return executor

This opt-in adapter connects the existing host approval ledger and inherited
pipe protocol to `ReplanningExecutor`. It changes a real return reservation,
then independently gates burn dispatch. It does not implement a new return
loop, guidance law, physical qualification, operator SSO or a complete M3 run.

## Contract and authority

The runner freezes `missionos.inflight_executor_contract.v1` in the outer
approved M3 authority. Its fields are `context` (run/mission/vehicle),
`initial_scope`, and `bounds`. Initial commands are exactly `select_nominal`
and `keep_reserved_return`. The sole resource limit is
`maximum_return_delay_s`, equal to the already validated return envelope.
`select_next_orbit` is present in the technical registry but initially absent
from the active scope. A passing physical prediction never adds authority.

The bounds contain the original simulation deadline, host monotonic deadline,
maximum observation age, and `reservation_cutoff_s`: nominal burn time minus
the existing event decision margin. The approval deadline cannot exceed that
cutoff. Neither operator wait nor a rejected request restarts these bounds.

`scope_revision` is distinct from the ordinary plan revision. One signed,
candidate-bound host consumption can add one command and apply one next-orbit
reservation. Reaffirming that exact current reservation is allowed. After it
is replaced, the same consumed approval cannot create another reservation.
After consumption, both forecast entrypoints exclude that approved next-orbit
candidate from recomputation. Its original generation, origin, burn time and hash
are retained; optional screening cannot move that burn time. Current observations
still pass fresh admission at selection and dispatch, so preserving an approved
prediction never converts stale or infeasible evidence into an allowed return.
Other unapproved candidates may be calculated. A changed prediction hash requires
new authority; this one-request adapter cannot supply it. The model sees technical candidates and the separately bound
`active_authority`. Its out-of-scope choice is an approval request, not dispatch.

## Runtime composition

`InflightReplanningExecutor` takes the ordinary constructor arguments plus:

- `inflight`: the independently approved contract described above.
- `request_writer`, `grant_reader`: only worker-owned inherited anonymous-pipe
  endpoints, never a ledger/key/token.
- `snapshot_sink`: a trusted executor telemetry path to the host. Forward the
  supplied snapshots, retaining a bounded host-observed history. Never create
  history from a worker approval request. A sink failure prevents expansion.
- `clock`: host monotonic time (default). Do not substitute mission time for it.

A runner can install it at the M2 runner's runtime-only executor factory seam:

```python
# In the credential-free worker, after inheriting only these read/write FDs.
def executor_factory(*args, **kwargs):
    return InflightReplanningExecutor(
        *args, **kwargs, inflight=frozen_contract,
        request_writer=ApprovalPipeWriter(request_write_fd),
        grant_reader=ApprovalPipeReader(grant_read_fd),
        snapshot_sink=publish_trusted_executor_snapshot,
    )
```

The host creates the two pipes, keeps their opposite ends, runs
`HostApprovalBridge`, and registers `host.snapshot` with
`InflightApprovalService`. Its separate process owns the ledger signing key
and operator bearer token. The model host uses
`ReplanningAgent(envelope, active, inflight_contract=frozen_contract)`; it
rejects unsolicited or mismatched authority fields. Do not import this host
agent, ledger or credentials into the physics worker. The default executor's
three hooks (`decision_context`, `fixture_response`, `dispatch_check`) retain
its original behavior and request shape.

For a fixture smoke, a scripted operator may use the actual loopback HTTP
`pending` and `approve` routes and echo the displayed request, observation and
candidate hashes. Record that identity as `scripted-test-operator` and report
`human_operator_action=false`. This exercises HTTP and signed consumption; it
is not a real human decision or permission for physical flight. The script
cannot alter the frozen scope/budget or pre-authorize next-orbit return.

## Execution and evidence

At `select_next_orbit`, a physically admitted candidate is held outside scope.
The existing finite `wait`/`step` loop advances the plant during operator wait.
Approval alone does not mutate physics or reserve a burn. After host consumption,
the worker checks request/context/deadlines, unchanged existing reservation,
exact candidate hash, and a fresh physical admission. It calls the existing
base `select`, then updates scope revision. The original model proposal ID is
preserved even if other observations arrived in between. A subsequent measured
state must match the new reservation before next-orbit dispatch is allowed.

Both return-loop dispatch sites call the same new `dispatch_check` hook. It
requires active command authority, exact consumed candidate, later reservation
confirmation, and current candidate/selected/burn-time consistency; the latter
rejects an old cached burn time after a reservation change. Failure changes the
actual gate to rejected. The successful physical `check` dictionary remains
unchanged for the existing scalar verifier; separate
`m3_return_dispatch_authority_checked` events retain authority evidence.

Rejection, expiry, host absence or changed state leaves the existing booking
unchanged and freshly checks it. If that booking is no longer physically
admitted, the result is unresolved; no recovery success is implied. No approved
scope expansion is silently inferred from a fallback. Ordinary final admission
still runs, and numerical/area/contact verification remains separate.

The return study includes `inflight_approval`: original contract, worker record,
active scope/revision, approved candidate hash, and executor snapshot trace.
Persist `host.record()` separately. Call `verify_inflight_return` with the
independently frozen contract, expected host record, and signed ledger records
read through host-owned `ledger.status()` (which checks signatures). The scalar
audit checks original proposal, one-use consumption, actual reservation revision,
original deadlines, strictly later observed effect and dispatch scope binding.
It does not hold the signing key or prove independent flight physics. Compose
it with the existing return verifier and outer M3 source/authority verification.

## Verification boundary

Tests use actual base reservation code, real inherited anonymous pipes and a
host ledger, with stub physics/admission and a scripted test operator. They
cover nominal-only authority, rejection/absence, stale candidate/state,
consumed-but-unused approval, one-time application, later confirmation, changed
candidate rejection at dispatch and scalar evidence forgery. They do not run
hosted models or numerical flights. Separate host/worker HTTP+6DOF runtime
verification and the M3 runner's outer contract/source binding remain required
before reporting integrated M3 operation.

## Fixed fixture entry and preflight

Before starting even the numerical prefix, call
`validate_inflight_executor_contract(contract, envelope, run_id=run_id)`.
When a nominal burn time is already available, also pass `nominal_burn_s` to
check its exact cutoff. The executor repeats that check against the actual
handoff; a valid-looking contract cannot supply a different burn deadline.

The minimal integration fixture adds the exact exported `FIXTURE_POLICY` as
`contract.fixture_policy`; its hash is therefore part of the outer contract
hash. It requests both numerical candidates, requests the admitted next-orbit
candidate once, and preserves the resulting booking. It is accepted only in
fixture mode. Normal M2 fixtures remain unchanged. This is a registered script,
not a live Jev/DeepSeek decision, and its response records the fixture policy
hash and `model_inference_invoked=false`.

For this minimal fixture, the nominal candidate can be technically admitted
without already being reserved. A refusal or timeout then has no existing
booking to preserve and may finish unresolved. The adapter does not guarantee
that a feasible return always exists. If a valid nominal booking already exists,
the original nominal cutoff is protected. The M3 fixture's next-orbit outcome
is evaluated separately from M2 normal-flight completion (which intentionally
requires nominal return); neither a false M2-normal case flag nor passing
scalar records alone substitutes for the M3 scenario result.
