# M2/M3 review and evidence boundaries

These notes preserve successive M2/M3 review checkpoints. Statements that M3
was incomplete describe the recorded revision at that point, not the current
completion status. The later [bounded M3 completion](starship-m3-completion.md)
and [final campaign report](../assets/starship-m3-final-20261012/report.html)
record all four roles passing, including the in-flight authority-extension path.
Earlier failures and their source identities remain evidence and are not
rewritten as successful runs.

Neither that completion nor these notes claim a SpaceX-qualified vehicle.
Model judgments are assessed by observed control duties, not by superiority over
a scenario-written fallback. [Post-M3 recovery sensitivity](starship-recovery-sensitivity.md)
records the remaining booster and controller-cadence limitations.

## Executed evidence has version identity

The M2 live operations-notice flight used 83 outer source files and 73 return
source files. Its original saved verifier rejected a scalar timestamp differing
by about 9.09e-13 seconds. Keep that original HTTP failure and receipt intact.
A separate replay of the executed-source verifier, with only its timestamp
comparison corrected to absolute 1e-7 seconds and zero relative tolerance,
accepts the saved flight. This is record revalidation, not another model call,
flight, or successful execution of the final integrated source.

The integrated source also adds concurrent physics, scope amendments and stricter
checks. Do not silently evaluate old records against a new authority/qualification
and call them an execution of that source. Keep source manifests and both verdicts.

## Independent Claude review

Claude Code reviewed read-only in the user-selected session, Opus 5.5 with high
effort. Two review passes covered optional search failure/budgets/deadlines,
source and inventory bindings, pre-approved versus newly requested authority,
actual application versus receipt consumption, malformed input, ascent latches,
worker/provider deadlines and the concurrent checkpoint. Findings were checked
against code and tests rather than treated as authority.

Review fixes include immutable approved return candidates, the same cutoff
constraint in host and executor, candidate identity in the HTTP approval body,
verified ascent-withhold evidence, and catching up physical time after optional
search cleanup without discarding still-future opportunities. Overrunning the
entire approved coast horizon remains explicitly unresolved.

## Local process trust is not OS isolation

The M2 host writes a durable provider-attempt ledger before transport. The worker
can read it, and request hashes/actor receipts are independently checked. This
mailbox is nevertheless writable by the same OS user. Its copied ledger is not
cryptographic proof of an isolated host origin. The inherited MissionService
worker also accesses the service signing key; its receipt signature is local
record integrity, not protection against a malicious same-user worker.

The new M3 HTTP fixture creates its operator token and HMAC key in the parent
only after spawning the worker. Requests, grants and trusted executor telemetry
use separate inherited pipes. This is a narrower tested credential boundary,
not a general OS sandbox. Scripted bearer authentication is not SSO, real-person
identity, or an observed human response time.

A future hardened deployment needs an explicit host-finalization handshake,
versioned attempt snapshots, host-only final receipts and OS-separated storage
and credentials. Late or cancelled provider attempts must stay counted. Do not
retroactively label this flight's unsigned host ledger as authenticated evidence.

## Booster stop point

The default concurrent prefix misses its contact-speed criterion. Three
coast-attitude candidate continuations exhausted fuel aloft; a separately fixed
reserve-aware terminal candidate did not find an ignition/near-ground-stop window
and impacted without ignition. No candidate was promoted or retried until success.
The saved previews do contain fuel-feasible stops high above the surface; they
are not evidence of a feasible terminal water entry.

SpaceX's [Flight 14 report](https://www.spacex.com/launches/starship-flight-14?trk=public_post_comment-text)
describes a planned 13-engine high-thrust landing phase, with 11 actually ignited,
then five and three engines. Its documented sequence includes boostback and
landing burns. These public descriptions do not establish this simulator's drag,
energy, fuel distribution or a need for an additional entry burn. Before another
candidate is admitted, inspect the earlier trajectory/energy and staged landing
control; do not add a burn and describe it as SpaceX fidelity without evidence.

## Earlier verification checkpoints

The extracted generator cores preserve all 39 prior default-simulation results:
full sampled control/state/event/payload/booster records match, not only contact
speeds. Certificate domains, tolerances and numerical values are unchanged; only
source and study identities change. Registered-source and scalar feasibility
checks also pass all 39. This establishes default-path compatibility, not the
new concurrent flight or a successful booster recovery.

The integrated checkout passed 793 related contract/Gateway tests and Ruff
checked 58 changed Python files. The fixed full M3 HTTP/6DOF attempt then failed
at host initialization. The original failure records a ModuleNotFoundError; a
separate import-only check identified missing `missionos_core`. No delivered
request, consumed grant, changed reservation, return dispatch or contact was
recorded by that attempt. Do not turn a later startup-only repair check into
full-flight evidence. No new flight was run to replace this failure.

M3 stays open: successful booster water-entry criteria, the full delivered
approval-to-observed-return flight, and live whole-flight ascent/booster AI
control are not established. The current approval endpoint authenticates a local
scripted principal; human identity and human response-time evidence are absent.

Startup repair now checks the production Gateway before starting a physics
worker and loads the repository's core/gateway/CLI packages explicitly. The
actual CLI `--check-runtime` returned healthy Gateway and operator-page HTTP
responses in 1.50 seconds, with worker/flight/forecast/model/approval counts all
zero. Its 85 affected driver/Gateway tests pass. This fixes startup readiness;
it is not a rerun of the failed full M3 flight.


## Follow-up: goal wiring and the second preflight failure

At `a974bca7`, M3 prefix v2 binds the existing splashdown goal through a
body-specific booster grant, while preserving the Ship's M2 authority. It uses
existing hull-clearance control and keeps the eight goal limits unchanged.
The common trajectory/contact record checker precedes the water-entry checker.
The earlier 8.285 m/s prefix had no splashdown goal connected; see the
[saved-record diagnosis](../assets/starship-m23-20261011/booster-standard-diagnosis.json).
Upper-flight attitude instability remains unresolved even if a future terminal
water-entry record passes.

The `a974bca7` Starship contract regression run passed 4,443 tests with five
skips. Both prior CI failures are repaired: the six-DOF CLI now records the two
required concurrent-control source hashes, and the two new smoke entry points
are registered. The real short worker/receipt boundary passes.

A separately authorized fixed HTTP/6DOF attempt then passed Gateway startup in
1.102 s but failed before the first physical step: the nested concurrent-prefix
output directory did not exist. It emitted no telemetry, HTTP approval request,
forecast, deployment result or return. Worker exit zero did not mean a valid
flight; the host verdict correctly remained false. All 96 frozen source hashes
and the 93 saved worker sources match. Preserve this
[second negative record](../assets/starship-m23-20261011/m3-http-startup-repaired-failure.json)
alongside the original bootstrap failure.

The producer now creates its nested evidence directory and refuses any nonempty
existing directory. A test traverses the actual composition/runner/producer
path and stops at the first simulator call, proving that the bound authority,
approval binding and source manifest have already been saved. This is an
initialization check with physics stubbed, not another flight. The worker also
returns nonzero when composition verification fails. No successful full M3
flight or water-entry result follows from these repairs.

The repaired boundary passed 112 affected tests and Ruff. A separate real CLI
`--check-runtime` check passed Gateway health and operator-page HTTP readiness
in 1.197 s, with no worker, physical steps, models, forecasts or approvals.


## Latest fixed fixture flight: partial success, bounded early stop

Source `09db378ddf1b84849834ad4e0d2873db5f55de9b`, run
`m3-http-fixture-directory-repaired`: one explicitly authorized attempt, model
calls zero, forecasts capped at 20, two workers, 2400 s wall maximum, 1.2 GB output
and 1 GiB free-space floor. Eight prediction files were saved. Do not equate that
count with independently verified total attempts: the final return study was not
written before termination.

The shared-clock prefix released 26 payloads, then passed the exact executed
state and inventory into the return constructor. The booster met all eight
registered water-entry constraints: contact-point speed 1.883584 m/s, vertical
-1.433399 m/s, horizontal 1.221990 m/s, tilt 0.295821 degrees, body rate
0.00250563 rad/s, fuel 11419.538 kg, site distance 8370.115 m, no initial overlap.
The prior COM speed 1.846707 m/s is not the contact-point speed. The goal stays
at its previously registered 10 km radius; the separate legacy 1 km contact
criterion remains false. This one model trajectory does not establish wave
response, structural survival, hardware recovery or disturbance robustness.

Real HTTP delivered the candidate-bound request, rejected an invalid bearer,
accepted one scripted approval and rejected replay. Host consumption at
T+1403.95 s was single-use. Separate inherited-pipe telemetry at T+1893.95 s
showed scope revision 1 and the same approved candidate reserved for
T+7731.351389 s. This confirms a later observed reservation change. The worker's
formal confirmation-event record and final return verifier were not saved;
actual burn dispatch and Ship contact are unconfirmed.

The worker was stopped deliberately at 1059.44 wall seconds after diagnosing
four full candidate hashes per 0.25 s tick. The exact approved candidate is
5,271,754 canonical bytes. One digest cost 0.078614 CPU seconds, implying about
7959 CPU seconds for the remaining coast hashes alone. This is a single-measurement
extrapolation, not profiling or a measured completed runtime. The host finalized
its negative result and telemetry; neither a timeout nor successful return is
claimed. All 96 preflight source hashes and 93 copied worker files matched the
frozen execution source. No prior record was replaced or deleted.

The follow-up repair references the booked revision in routine telemetry;
current-candidate full hashing and fresh admission remain at decision and
execution boundaries, including fallback. Mutated nested evidence, same-name
reforecasting and changed burn times cannot inherit the old reservation's
permission. The saved flight is evidence for the old source; repair checks are
separate and do not count as another full flight.

[Portable flight audit](../assets/starship-m23-20261011/m3-http-bounded-flight-audit.json),
[water-entry audit](../assets/starship-m23-20261011/m3-booster-water-entry-audit.json),
[performance diagnosis](../assets/starship-m23-20261011/m3-reservation-hash-performance.json).

M3 still needs a full approval-to-observed-Ship-return flight and actual Jev
ascent/booster supervision. For that live path, keep separate body/phase mailboxes,
short-phase deadlines, host attempts and late-response rejection; do not silently
change the existing fixture authority into model authorization.

## Reservation repair verification

The follow-up passed 326 related contract tests (two dependency warnings), Ruff
and `git diff --check`. The mutation checks cover same-name forecasts, nested
evidence, missing revisions and changed burn times at fallback and dispatch.
The real loopback Gateway smoke passed all 14 checks using fixture snapshots:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python -m pytest -q -p no:cacheprovider \
  tests/contract/test_starship_inflight*.py tests/contract/test_starship_m3*.py

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/smoke_starship_inflight_gateway.py \
  --output-dir output/starship-m23-20261011/inflight-http-reservation-repair
```

The HTTP smoke exercises delivery, authentication, one-use consumption, replay,
expiry and fallback; it does not invoke the physical executor or any AI model.
Its [portable result](../assets/starship-m23-20261011/inflight-http-reservation-repair.json)
is separate from the frozen `09db378d` flight audit. No full-flight retry of the
reservation repair is included here.

A separate saved-input boundary invocation reconstructed the 5,271,754-byte
candidate (matching the approved hash), initialized a fixture executor and sent
its current `_publish_snapshot` through a real anonymous approval pipe. It
received one 633-byte frame. Snapshot hashing touched only the 167-byte booking
and 2,202-byte observation; candidate-full hashes in that method were zero.
This combines saved origin and booking data in a fixture, not a new flight state,
constructor, approval, dispatch, or performance benchmark.
[Result](../assets/starship-m23-20261011/m3-reservation-snapshot-pipe-smoke.json)
and the [saved-input recipe](../assets/starship-m23-20261011/reservation_snapshot_smoke.py)
retain source hashes and the original record binding.

## Completed repaired HTTP fixture flight

One separately authorized attempt used frozen `9806f8a522a165bc5e28ca61b0b981aa14fe7b4c`:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python scripts/smoke_starship_m3_http_flight.py \
  --approve-simulation --run-id m3-http-fixture-reservation-repaired \
  --reference-prefix output/starship-m23-20261011/m3-concurrent-normal-prefix/study.json \
  --output-dir output/starship-m23-20261011/m3-http-fixture-reservation-repaired
```

This is a new launch and a continuation from its actual deployment state, not a
restart from a forecast. Exit 0, `declared_fixture_accepted=true`, all 11 HTTP /
process checks true, no model calls. It completed in about ten wall minutes,
with eight forecasts, within the same two-worker / 2400 s / 1.2 GB bounds.
The previous failed attempts remain saved separately.

- Booster: all eight registered water-entry conditions met, contact-point speed
  1.883584 m/s, remaining fuel 11.4195 t, site distance 8.3701 km. The legacy
  one-kilometre criterion remains false; the previously registered goal is 10 km.
- Ship: scope change applied at T+1402.45 s, distinct later reservation
  confirmation at T+1402.70 s. Checked return dispatch at T+7731.45 s, deorbit
  ignition command at T+7786.95 s, contact at T+10378.626705 s. Contact-point speed
  4.613326 m/s and remaining fuel 62.433932 t, in the western model area.
- The approval verifier passes and observes dispatch, but its own
  `physical_return_verified` stays false: that verifier checks authority and
  reservation. The separate return verifier passes its trajectory/contact/area
  checks with `outcome=contact_and_recovery_area_met`. Neither proves real-world
  physical execution or independent validation of the shared dynamics.
- `m2_normal_case_accepted=false` is retained. This amendment fixture deliberately
  moves a normal plan to the next opportunity; it cannot also satisfy the M2
  normal-case requirement to keep the nominal plan. The independent record and
  return-outcome checks pass, and the separately declared amendment fixture
  accepts the result. Do not relabel it as a nominal no-intervention flight.

[Independent portable audit](../assets/starship-m23-20261011/m3-http-completed-flight-audit.json)
records the source and evidence hashes, elapsed-time basis, distinct boundary
verdicts and registered limits. The replay now includes the complete Ship
execution plus the concurrently integrated booster. It does not extend booster
physics beyond the contact stop.

M3 still needs live ascent/booster judgment in this continuous flight. The
fixture envelope is not implicit authorization for provider calls. A future
live outer grant must retain separate phase mailboxes, count all provider
attempts, enforce short-phase deadlines and reject stale responses. DeepSeek's
longer orbital budget does not apply to ascent or booster terminal control.


## Bounded real-provider M3 flight (`cadfe521`)

The historical fixture and partial-flight sections above describe earlier source.
One new live attempt used frozen `cadfe52183b377d42b1b2ae5cc45505b122eb3dd`,
scenario `sixdof_m3_orbital_comparison`, run
`4aeba42e269f4410af7d22fde9c9afa0`. All 94 recorded source hashes match that commit.
No automatic retry, new model call, or corrective numerical flight followed it.

The production Gateway planned, approved and executed a distinct outer live
grant, then ran a credential-free worker with separate phase mailboxes. Planning
used deterministic scenario selection only; flight decisions used real Jev.
The grant allowed Jev 27 / DeepSeek 1 / forecasts 20, two workers and 2400 wall
seconds. Output/free-space limits were checked every five seconds. DeepSeek was
permitted only for an independently admitted uncertain comparison within a
150-second orbital window (120-second provider timeout), never for short ascent
or terminal control. This grant did not request a new in-flight scope amendment.

| Evidence | Observed result |
| --- | --- |
| Provider calls | Jev 20, all valid and adopted: ascent 2, booster 1, deployment 1, return 16. DeepSeek 0, new rationale citations 0 |
| Inventory | Jev stopped normal deployment at T+481.2 s; observed at T+481.45 s. No injected deployment fault; 0 released / 26 retained |
| Same-flight continuation | Actual deployment state carried into return at T+1000.7 s; forecasts never replace the plant |
| Booster | Contact T+619.6552 s, point speed 1.883584 m/s, tilt 0.29582 degrees, fuel 11.4195 t, site distance 8.3701 km; 8/8 registered water-entry criteria |
| Ship | No return dispatch/burn/contact. `return_unresolved` at T+7671.7 s, altitude 370.3476 km, fuel 110.8338 t |
| Execution budget | Eight ordinary forecasts; 546.167836 wall seconds. No second flight |
| Outcome | Record verification passed, `case_accepted=false`, Gateway `unresolved`, HTTP smoke exit 3. M3 and the requested both-model demonstration remain incomplete |

### Diagnosis from saved records and source

1. Return Jev chose `refresh_observation` 16 times in T+1163.2--1174.45 s before
   the material notice update at T+1350.2 s. Valid/adopted does not establish good
   control judgment. This needs scheduling around changed evidence and remaining
   decision opportunities, not an AI-versus-fallback score.
2. Executor request and provider-agent Jev-budget checks precede the separately
   budgeted prepared DeepSeek route. An unused DeepSeek call cannot be reached
   after Jev exhaustion. This is a control-integration issue.
3. The comparison batch passed cutoff T+1950.2 s for a search starting T+1350.2 s.
   Search requires `start + 600 < cutoff`, which is false at equality. Saved
   `return_time_search` has `work_started=false`, empty trials, and reason
   `search_budget_booking_or_deadline`. Ordinary predictions still ran at the
   unchanged next-orbit time. This did not search or rule out other times.
4. At T+1617.2 s, nominal and next-orbit candidates both failed
   `area_or_availability`. Therefore a provider-budget repair alone cannot prove
   this flight would have yielded a valid DeepSeek comparison or Ship return.

The findings above are not fixed in this execution source. Preserve the failed
flight as the integration result; do not call DeepSeek without an operational
question just to increase invocation counts. A later repair needs separate
provider budgets, observation scheduling, and composable search/batch deadlines,
followed by offline checks before any newly bounded flight.

### E2E / Runtime Verification

Use the qualified Python environment (`spaceflight-qualified`). Start the
production Gateway on loopback with explicitly authorized live providers and
named Secret Manager secrets (never print their values):

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/start_starship_gateway.py \
  --state-dir output/starship-m23-20261011/m3-live-jev-deepseek/gateway \
  --port 18866 --fixture-planner --enable-live-mission-director

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/smoke_starship_chat_gateway.py \
  --port 18866 --scenario sixdof_m3_orbital_comparison \
  --output-dir output/starship-m23-20261011/m3-live-jev-deepseek/http
```

The recorded invocation also supplied the host's configured Secret Manager
project to the Gateway. It is deployment configuration, omitted here. The HTTP
command exercised plan -> approve -> run, outer/inner authority, real phase
providers, shared-clock numerical flight, later observations, signed receipt,
and independent artifact verification. It exited 3 with the unresolved result
above. The trial-owned Gateway was stopped afterward. This command listing is
not an instruction to rerun the paid flight or overwrite its output directory.

Before the flight, 398 related contract tests passed (one dependency warning):

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python -m pytest -q -p no:cacheprovider \
  tests/contract/test_starship_m2_contract.py \
  tests/contract/test_starship_m2_gateway.py \
  tests/contract/test_starship_m2_orbital_attempts.py \
  tests/contract/test_starship_m2_runner.py \
  tests/contract/test_starship_m2_return_continuation.py \
  tests/contract/test_starship_m3_fixture.py \
  tests/contract/test_starship_m3_prefix.py \
  tests/contract/test_starship_m3_splashdown.py \
  tests/contract/test_starship_m3_withheld_evidence.py \
  tests/contract/test_starship_m3_runtime_startup.py \
  tests/contract/test_starship_m3_live_prefix.py \
  tests/contract/test_starship_m3_live.py \
  tests/contract/test_starship_m3_broker.py \
  tests/contract/test_starship_m3_gateway.py \
  tests/contract/test_starship_ascent_supervisor.py \
  tests/contract/test_starship_runtime_budget.py \
  tests/contract/test_starship_mission_planner.py
```

A preflight production-Gateway HTTP check passed 12 plan/approval/rejection
checks with placeholder provider keys and no worker/model invocation. That is
separate from the subsequent real flight. Changed-file Ruff and whitespace
checks passed. Tests and record validity do not override the failed case.

[Portable live audit](../assets/starship-m23-20261011/m3-live-jev-deepseek-audit.json),
[trajectory/report](../assets/starship-m23-20261011/m3-live-report.html).

Portable-delivery checks: six raw evidence hashes and all 94 source hashes
matched; all 769 Ship and 50 booster display points matched executed samples.
The HTML embedded data equals its linked JSON. Thirty-four relative links
resolved, portability scans and whitespace checks passed, and the browser
verified final-state display, playback and restart from the final state.
Displayed speed is ground speed, separately labelled from contact-point speed.

## Repaired real-provider flight (`fe07fa2f`)

The newly bounded trial used source `fe07fa2f267d79db6d0fac1ff17321a28c1cb0bf`,
run `d5b8b106fd7446e98faffd429a55d54a`, and the existing
`sixdof_m3_orbital_comparison` scenario (`orbital_update`). It was executed once,
with no automatic retry. Preserve the earlier `cadfe521` failure separately.
The trial changed provider scheduling, deployment semantics and deadline
composition, not physical models, areas, contact thresholds or model answers.

### Observed main path

| Boundary | Saved observation |
| --- | --- |
| Providers | Jev 7 attempts, 6 valid/adopted: ascent 2/2, booster 1/1, deployment 1/2, return 2/2. DeepSeek 1/1, valid/adopted |
| Deployment | 26 released / 0 retained; actual state and inventory handed over at T+1000.7 s |
| Invalid response | Deployment-monitor Jev response invalid; checked fallback `continue` applied, later observation T+526.7 s. Validation cause remains unknown |
| Initial return | Jev selected nominal at T+1160.7 s; later observation T+1161.2 s |
| Corrected notice | Event T+1350.2 s; comparison prepared using existing nominal and additional next-orbit forecasts |
| DeepSeek | Request T+1614.45 s, `keep_plan`, later observation T+1636.2 s; latency 21.578 s, protocol `ids_orbital_low_16384`, 4368 reasoning tokens |
| Numerical grounding | 12 cited input IDs independently resolved and matched. This validates references, not all free-text reasoning or shared physics |
| Final monitoring | Jev `keep_plan` at T+2220.7 s; later observation T+2221.2 s |
| Execution | Nominal departure T+2280.7 s, no return fallback, zero in-flight human commands |
| Ship | Contact T+4929.8564 s, contact-point speed 4.608259 m/s, fuel 62.3943 t, accepted eastern model area; `low_speed_surface_contact`, not verified hardware landing |
| Booster | Contact T+619.6552 s, point speed 1.883584 m/s, tilt 0.29582 degrees, fuel 11.4195 t, site distance 8.3701 km; 8/8 model water-entry criteria |
| Budget | 8 forecasts, 2-worker ceiling, 560.9867 wall seconds; caps Jev 27, DeepSeek 1, forecasts 20, wall 2400 seconds |
| Verdict | HTTP exit 0, Gateway `verified`, `passed=true`, `case_accepted=true`, `ai_control_demonstrated=true`; `m3_completed=false` and physical execution false |

DeepSeek compared waiting costs and uncertain service-time estimates, then kept
the nominal booking. It cited nominal energy 22 kWh, loss bound 0 kg and 295 K,
and next-orbit energy 12.9156 kWh, loss bound 109.013 kg and 300.4507 K. These are
synthetic resource estimates passed to the model, not observed recovery-service
completion. A plan change is not required for accepted control judgment.

Optional departure-time search did not start: the admitted nominal booking was
protected (`search_budget_booking_or_deadline`). Therefore this flight does not
validate a live refined-time search; the composable search deadline is exercised
by production-actor tests with fake numerical workers. Earlier fixture search
evidence is separate. The full milestone matrix, live scope amendment and
anomaly/disturbance robustness remain incomplete. No new Claude review was run
for this follow-up; the earlier review section has its original scope.

### E2E / Runtime Verification

The actual production boundary was plan -> approve -> run over loopback HTTP,
real Jev/DeepSeek, separate credential-free numerical flight worker, independent
checks and later observations. The planner fixture chooses the scenario only.
Start the Gateway with the qualified environment and the existing authorized
Secret Manager project configured as `GOOGLE_CLOUD_PROJECT`:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/start_starship_gateway.py \
  --state-dir output/starship-m23-20261011/m123-live-repaired/gateway \
  --port 18867 --fixture-planner --enable-live-mission-director \
  --project "$GOOGLE_CLOUD_PROJECT"

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/smoke_starship_chat_gateway.py \
  --port 18867 --scenario sixdof_m3_orbital_comparison \
  --output-dir output/starship-m23-20261011/m123-live-repaired/http
```

The smoke exited 0 with the case above. The trial Gateway was then stopped;
evidence was retained. Do not rerun the paid flight or overwrite its directory
from this command listing. Local approval identity is a test operator, not proof
of real-person authentication.

All 94 source hashes match the frozen protocol, current runtime source and Git
source `fe07fa2f`. The full verifier was rerun on saved JSON with the frozen
authority/source hashes and recorded return-source manifest/run ID; its verdict
exactly matched the saved successful HTTP verification. No additional model call
or numerical flight was used for revalidation.

Before execution, 891 tests passed (two dependency deprecation warnings) across
41 related contract files, including actual loopback provider/Gateway boundaries
and the search production actor with fake numerical workers. Exact command:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python -m pytest -q -p no:cacheprovider \
  tests/contract/test_starship_ascent_supervisor.py \
  tests/contract/test_starship_comparison_audit.py \
  tests/contract/test_starship_comparison_contract.py \
  tests/contract/test_starship_comparison_events.py \
  tests/contract/test_starship_comparison_models.py \
  tests/contract/test_starship_comparison_payload.py \
  tests/contract/test_starship_comparison_runtime.py \
  tests/contract/test_starship_deployment_semantics.py \
  tests/contract/test_starship_m123_provider_scheduling.py \
  tests/contract/test_starship_m2_contract.py \
  tests/contract/test_starship_m2_gateway.py \
  tests/contract/test_starship_m2_orbital_attempts.py \
  tests/contract/test_starship_m2_return_continuation.py \
  tests/contract/test_starship_m2_runner.py \
  tests/contract/test_starship_m3_broker.py \
  tests/contract/test_starship_m3_fixture.py \
  tests/contract/test_starship_m3_gateway.py \
  tests/contract/test_starship_m3_http_driver.py \
  tests/contract/test_starship_m3_live.py \
  tests/contract/test_starship_m3_live_prefix.py \
  tests/contract/test_starship_m3_prefix.py \
  tests/contract/test_starship_m3_runtime_startup.py \
  tests/contract/test_starship_m3_splashdown.py \
  tests/contract/test_starship_m3_withheld_evidence.py \
  tests/contract/test_starship_mission_planner.py \
  tests/contract/test_starship_model_diagnostics.py \
  tests/contract/test_starship_model_loopback_runtime.py \
  tests/contract/test_starship_orbital_model.py \
  tests/contract/test_starship_orbital_protocol_campaign.py \
  tests/contract/test_starship_orbital_window.py \
  tests/contract/test_starship_replanning.py \
  tests/contract/test_starship_replanning_action_notices.py \
  tests/contract/test_starship_replanning_event_audit.py \
  tests/contract/test_starship_replanning_event_margin.py \
  tests/contract/test_starship_replanning_event_runtime.py \
  tests/contract/test_starship_replanning_events.py \
  tests/contract/test_starship_replanning_result_binding.py \
  tests/contract/test_starship_replanning_tradeoff_witness.py \
  tests/contract/test_starship_return_search_regressions.py \
  tests/contract/test_starship_runtime_budget.py \
  tests/contract/test_starship_search_trial_time.py
```

Changed-file Ruff and whitespace checks passed before execution.
[Portable audit](../assets/starship-m23-20261011/m123-live-repaired-audit.json),
[trajectory and report](../assets/starship-m23-20261011/m123-live-repaired-report.html).

Portable-delivery recheck: all 12 DeepSeek ID references re-resolved with exact
candidate/path/value/unit matches. All 494 Ship and 50 booster display points,
including endpoints, match executed samples; HTML embedded data equals linked
trajectory JSON. Raw hashes and all 94 source hashes match. Fifty-five relative
links resolve, portability/credential-pattern and whitespace checks pass, and
the browser confirms the final sample, playback and restart. The comparison
rationale shown in the report is a bounded summary, not verified free prose.
