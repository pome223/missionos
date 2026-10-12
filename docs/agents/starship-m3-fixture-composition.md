# M3 fixture composition boundary

`starship_m3_fixture.run_fixture(profile, case, output)` composes the actual
shared-clock ascent/deployment prefix with the existing M2 return executor.
Registered prefix cases are `normal`, `operations_notice` and `release_fault`.
The call is opt-in and requires a fresh output directory. It performs one new
launch, then continues from the exact observed state and retained inventory.
No prediction state is installed in the plant. The existing source-qualified
return readiness check must pass before any prefix physics runs.

The outer fixture authority binds both the prefix and canonical M2 subauthority.
Its intersection authorizes this composition; the prefix by itself still grants
no Ship return. Sources, authority and fixture approval binding are saved before
launch. Ascent and booster decisions remain fixtures; model calls are zero.
The fixture approval binding is not an authenticated human preflight approval.

## Shared-clock continuation

The first composition requires the booster to have reached an actual recorded
surface contact no later than the Ship checkpoint. The prefix verifier must
pass and produce a valid handoff. An unfinished booster causes an explicit
negative result before a return actor is created. Supporting such a branch
requires a continued concurrent scheduler; the runner must never silently hold
its state while advancing Ship. A terminated booster remains its recorded
outcome throughout Ship return, including a failed contact envelope.

The added `concurrent_prefix=True` hook selects the registered prefix producer
inside the existing M2 runner. It is fixture-only. All return numerical
calculations, reservations, fresh dispatch checks and observed outcomes still
use the existing executor and independent M2 verifier. Negative prefix evidence
is saved before the continuation guard, without a retry.

## Host approval seam

An optional trusted Python-runtime argument has exactly these keys:

```python
inflight_runtime = {
    "inflight": {"schema": "missionos.inflight_executor_contract.v1",
                 "context": context, "initial_scope": scope, "bounds": bounds,
                 "fixture_policy": FIXTURE_POLICY},
    "request_writer": request_pipe_writer,
    "grant_reader": grant_pipe_reader,
    "snapshot_sink": trusted_host_telemetry_sink,
    "clock": monotonic_clock,
}
```

`context.run_id` must equal the composition run ID plus `-return`. The initial
scope is nominal return and preservation of its reservation; only the host can
approve the registered candidate-bound next-orbit amendment. The constructor
has one fixed import, `InflightReplanningExecutor`; callers cannot provide an
executor class, import path or implementation selector. No CLI or JSON option
exposes this seam. Credentials and signing keys are not accepted. Pipe handles,
callbacks and clock objects are never written to study artifacts. The public
contract and its digest are included in the outer authority and inner study.
The host owns independent live telemetry, operator authentication and signed
single-use consumption; the worker receives only bounded IPC results.

The host must validate its registered contract, nominal reservation cutoff and
fixed simulation/wall deadlines before consuming the flight budget. A pending
request may advance physical time; it does not itself change a reservation.
A consumed amendment changes the reservation only after another physical
admission check. The inherited dispatch gate independently checks the fresh
state at the actual burn. Later reservation confirmation and later burn/outcome
confirmation are distinct records.

The wrapper reports ordinary M2 and prefix checks. With an approval seam it
explicitly marks approval verification as pending: the host must separately
compose `verify_inflight_return` with its ledger and bridge records. Passing the
worker's record alone cannot establish host authentication or signed receipt
verification. Operator scope approvals are accounted separately from direct
human flight commands. No SSO identity, real human response, completed M3,
complete mission or physical execution is claimed by this fixture runner.

The canonical M2 return case is `event_normal`, whose existing fixture policy
normally preserves nominal return. An HTTP amendment test therefore needs an
explicit registered fixture policy to request a valid next-orbit candidate; it
must be recorded as scripted input. Do not globally patch the intelligence
module or relabel fixture decisions as AI inference.

## Bounded integration proposal

After the common-clock source qualification passes, freeze one source manifest,
one `normal` fixture flight, zero provider calls, one optional scope amendment,
no retry, the existing numerical forecast/resource/deadline limits, and a fixed
wall limit before running. Record the loopback HTTP request and approval, host
ledger result, bounded pipe result, executor snapshot progression, reservation
change, later confirmation, fresh burn admission and actual Ship contact.
Retain the booster outcome separately. A failure or missing request remains a
negative or partial result; do not run until success.

This proposal does not itself authorize a new numerical run.

## E2E / Runtime Verification

The staged contract tests invoke the actual composition functions with numerical
actors stubbed. They check exact state/authority handoff, unfinished-booster
rejection, preserved failed booster contact, the registered executor constructor,
pre-launch rejection of extra authority and wrong run identity, and omission of
IPC objects from artifacts. This is a production wiring smoke using fixtures;
it does not establish a full 6DOF flight or HTTP-to-burn operation.

A PR reporting full integration must add the exact frozen-source command,
scenario, fixed budgets, observed HTTP and numerical result, all three
verification results, and the remaining limitations. The independent prefix,
M2 outcome and in-flight authority checks must all be present; none substitutes
for the others.


### One-flight HTTP driver

After a separate fixed-budget authorization and source freeze:

```sh
python scripts/smoke_starship_m3_http_flight.py --approve-simulation --run-id m3-http-fixture --reference-prefix output/prior-concurrent-prefix/study.json --output-dir output/m3-http-fixture
```

This command is not a request to execute automatically. It makes one new fixture
flight, zero provider calls, at most 20 forecasts with two workers, a 2,400-second
wall limit and no retry. The registered `request_checked_next_orbit_v1` fixture
policy requests an already checked next-orbit candidate. A scripted local
operator responds through the real Gateway HTTP route. This is not a live AI or
real human-response measurement.

The prior prefix supplies only the fixed expected orbit-cutoff time. Its
profile must match; its executed state is never used as this flight's origin.
The executor must confirm the exact cutoff again against its new actual prefix.
Host credentials are generated after the worker subprocess is spawned and are
never passed in environment, arguments or pipe frames. The worker receives
request/grant endpoints and a distinct one-way executor telemetry endpoint.
The host authenticates request observations only against that telemetry history.

The driver saves an HTTP transcript without authorization headers, the
host-consumption checkpoint, host runtime and verified ledger records, worker
snapshots and the final independent authority/outcome checks. The consumption
checkpoint makes no claim that the worker has yet observed a changed reservation
or started a burn. Those facts are checked separately at completion. Its private
SQLite files contain local operational state and are not publication assets.

`declared_fixture_accepted` requires same-state continuation, all authority and
HTTP checks, 26 released payloads, an approved next-orbit reservation with later
confirmation, and the existing Ship contact-and-area outcome. The original M2
normal-case acceptance remains separately reported and may be false because
this fixture deliberately chooses a different plan. Booster failure is retained;
`m3_completed` and `mission_completed` remain false.


### Preflight and sequence guard regressions

Candidate-bound HTTP responses echo `request.candidate_sha256` from the
production pending response, together with the displayed request and observation
hashes. Omitting the candidate is rejected by the production API; the driver
must not replace this field with an independently guessed candidate.

The ledger accepts at most 1,200 seconds from issuance in each clock. The driver
therefore fixes its wall deadline at start plus 1,200 seconds and its simulation
deadline at the lesser of checkpoint plus 1,200 seconds and the reserved nominal
cutoff minus 15 seconds. Host consumption does not silently extend or shorten
that contract. Unit tests pass the real worker request through the private pipe
and host bridge into ledger creation at several request times; the former
1,800-second wall limit is retained as a rejected regression case.

M2 accepts the ascent withholding event only after independent prefix validation
and exact binding of the prefix run, observation trace and handoff. Merely
inserting that event in a legacy M2 record does not close a sequence. The latch
runs before the same-tick observation, remains set through ordinary Director
commands and cannot generate a spurious hold expiry or resume. Existing forged
held-to-skipped and missing-resume corruption cases remain rejection tests.
