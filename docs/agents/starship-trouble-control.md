# Experimental Starship trouble-control contract

This opt-in MissionOS CLI connects seeded plant faults to current observations,
host-side Jev/DeepSeek decisions, separate-process execution checks, and later
plant observations. It does not register a Gateway/chat scenario or qualify a
faulted return. Existing M3 grants and the no-fault return certificate remain
incompatible with the trouble manifest.

## Authority and lifecycle

`scripts/run_starship_trouble_control.py --prepare` freezes the profile,
manifest hash, source hashes, numerical backend, simulation horizon, actions,
provider model identities, deadlines and resource bounds. It prints a plan SHA256. The manifest
is a separate private harness file; it is never a provider input.

`--approve-plan-sha256 HASH` checks that exact plan and consumes an exclusive
one-use execution claim before spawning the worker. Failures retain the claim
and evidence. This records an explicit CLI test operator, not authenticated
human identity. There are no automatic retries and no physical authority.
The worker also claims an exclusive `worker.started` marker before entering
physics; directly replaying its hidden entrypoint cannot execute a second flight.
The host rejects a changed model configuration before sending provider requests.

The host owns API credentials and model transports. The child receives a small
environment without provider credentials. Ship and Booster share the actual
concurrent physics clock; independent host broker threads avoid blocking one
body's request on the other's orbital reasoning. While a live request is
pending, physical simulation time continues, paced to elapsed wall time.

| Decision | Scope and actual effect |
|---|---|
| `continue_mission` | Retain the explicitly unqualified baseline. Cannot clear a latch. Fresh phase and health must still match the request. |
| `collect_status` | Request another current diagnostic after two simulated seconds. This is the same synthetic channel, not another sensor or an attitude maneuver. No automatic hold. |
| `withhold_deployment` | Irreversibly disable remaining releases before or during deployment. Actual retained payload stays in the plant. |
| `inhibit_return` | Before deorbit, latch a return inhibit for the remaining bounded experiment. Survival and future recovery remain unresolved. |

Engine commands, attitude control, return trajectories, catch and authorization
expansion are not in this grant. Invalid/stale/late/missing responses receive an
independently checked fallback: withhold pending Ship deployment when current
health reports a fault, otherwise keep the baseline. **This fallback is not a
demonstrated safe return policy.** Faulted impacts remain recorded impacts.

## Observation and model boundary

Requests arise on initial monitoring, changed phase/health, a 120-second
periodic interval, or completion of requested diagnostic sampling. Models see
only current time, phase, ideal kinematic measurements, a quantized noisy fuel
proxy, actual released count/sequencer/latches, applicable return deadline and
`synthetic_component_health_v1`. The diagnostic reports currently unavailable
engines, jammed actuators, blocked payload mechanism and current tower health.

Strict key and registered component-name validation rejects extra metadata.
Seed, fault family/target/schedule, true inertia/mass, manifest hashes, source
maps and unobserved failures do not cross the model boundary. The diagnostic is
idealized test instrumentation, not reconstructed SpaceX flight telemetry.

The raw plant diagnostic retains its mission-wide payload signal. Provider
observations and evidence instead label the Booster payload mechanism
`not_applicable`, never falsely `clear`. Booster event detection, staleness and
diagnostic-history comparisons exclude that unrelated signal. Ship still sees
its actual `clear` or `blocked` mechanism. Models also receive the last four
executed actions and whether a completed diagnostic changed relevant health;
the verifier reconstructs this history at each request's time, excluding future
diagnostic results.

Jev chooses among currently eligible actions. DeepSeek is available only if
Jev routes an orbital request to `deep_reasoning` with a full 150-second window;
the host additionally requires 120 seconds plus execution margin remaining.
Short requests allow 12 seconds; the actual transport is deadline-limited.
DeepSeek uses thinking enabled, low effort, and 16,384 combined reasoning/output
tokens. No separate reasoning/output budgets or mission call-count cap exists.
One invocation per provider per request and no transport retry prevent replay.
Diagnostic requests are unavailable during either terminal-burn phase or when
less than two seconds remain in the known approved horizon/deorbit window.
Other monitoring and eligible decisions retain their approved deadlines.

Only a concise returned operational summary and IDs of current evidence are
saved from DeepSeek content. Provider `reasoning_content` is never saved.
Evidence IDs are resolved against the decision-time input; matching them does
not verify every assertion in the free-text explanation. Jev has no invented
free-text rationale. All host attempts are counted, including responses the
actor rejects after the state changes.

## Verification

The worker writes JSON before strict verification. Existing physics and trouble
verifiers remain enabled. The additional verifier crosslinks requests, command
acceptance observations and distinct later observations to raw plant samples,
checks engine/actuator fault consistency, independent action bounds, provider
input binding, executor events and no later payload separation after withholding.
The model-input normalization and evidence-ID table are shared with the
producer; their reuse checks binding, not independent interpretation. Action
eligibility and recorded execution effects are checked separately by the verifier.
Return-inhibit latch observation is distinct from observing the full later
deorbit deadline; a short experiment proves only the portion actually run.
Diagnostic records distinguish pending, completed and interrupted requests.
Actual surface contact before the diagnostic due time can justify an explicit
interruption tied to the recorded terminal outcome; it cannot supply a missing
observation. Such a record may pass consistency checks but has
`all_requested_diagnostics_completed: false` and `case_accepted: false`; the CLI
returns failure for that incomplete control gate. No interrupted action is
reported as a completed diagnostic.

A record pass is not recovery success, explanation correctness, historical
fidelity, unique AI advantage, or M3 completion. The fixed policy and fixture
are regression/transport aids, not an AI-value scoring oracle.

## Reproducible entrypoint

Prepare a seeded, payload-only fixture before any execution:

```sh
python scripts/run_starship_trouble_control.py --prepare --mode fixture \
  --seed 20261013 --family payload_jam --nominal-probability 0 \
  --duration-s 800 --output-dir output/trouble-control-fixture
python scripts/run_starship_trouble_control.py \
  --approve-plan-sha256 PLAN_SHA256 --output-dir output/trouble-control-fixture
```

For an explicitly authorized live attempt, prepare a **fresh** directory with
`--mode live`; host configuration requires
`MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE=live` and existing provider credentials.
Never print or store keys. Preparing a live plan makes no provider request.
Each approved attempt has a 2,400-second wall deadline, output bound 500 MB and
free-space floor 5 GiB. Do not reuse a failed directory or tune/retry silently.

Outputs include the private frozen plan/manifest, one-use approval, worker log,
current public-input copies, provider attempt receipts, responses, actual 6DOF
study, independent verification, outcome summary and host execution accounting.
Keep them under ignored `output/`; publish only a reviewed sanitized summary.

Changes to the plant source invalidate the old no-fault certificate source
binding even when a null trouble-control option preserves the default dynamics.
Do not update certificate hashes without running its existing qualification
procedure. This fault-control experiment does not load that certificate.

## Initial live engineering attempt (2026-10-13)

The fixed seed 20261013, payload-only, 800-second case was run once using Jev
`jev-1.13.0` and conditional DeepSeek `deepseek-v4-flash`. This is the same
engineering scenario as the earlier fixture smoke, not held-out evaluation.
The plan hash was
`a5acf27a8aebb850a1f07c03089459734486a92693c011c85f3c10b2180c9524`.

| Observed fact | Result |
|---|---|
| Real provider attempts | Jev 76, valid responses 75; DeepSeek 0, not routed |
| Actual payload jam | T+489.537 s |
| Jev withholding decision | T+489.787 s; no fallback used for this decision |
| Later physical observation | T+490.037 s: sequencer skipped, deployment withheld |
| Ship outcome | 0 releases; orbital coast at the 800 s horizon, return unresolved |
| Booster outcome | Contact-point speed 8.275 m/s; contact envelope not met |
| Verification | Physics and fault records pass; control record **fails** |

The control failure is `diagnostic_not_completed:65`. Jev requested another
Booster diagnostic at T+607.850 s, due no earlier than T+609.850 s, but Booster
contact ended that body at T+608.216 s. A next-tick sample at T+607.950 s does not
satisfy the two-second diagnostic contract. The original failure is retained;
no missing observation was fabricated and no live retry was performed.

Jev selected `collect_status` 61 times, 53 on Booster. Its current diagnostic
included the shared payload mechanism blockage, leading to repeated requests
without corrective authority on that body. This exposes a monitoring relevance
and termination-handling limitation. One late invalid Jev response caused the
preapproved withholding fallback. The cause of the invalid response is not
inferred from its response hash. These observations do not establish completed
fault control, fault recovery, or a DeepSeek live invocation in this adapter.

### E2E / Runtime Verification

Using the qualified Python backend and host-only Secret Manager credentials:

```sh
python scripts/run_starship_trouble_control.py --prepare --mode live \
  --seed 20261013 --family payload_jam --nominal-probability 0 \
  --duration-s 800 --output-dir output/starship-trouble-control-live-20261013-01
python scripts/run_starship_trouble_control.py \
  --approve-plan-sha256 a5acf27a8aebb850a1f07c03089459734486a92693c011c85f3c10b2180c9524 \
  --output-dir output/starship-trouble-control-live-20261013-01
```

Observed exit 2 after 50.185 seconds wall time, with all frozen source hashes
unchanged. The second command is historical evidence, not permission to replay
the consumed approval. Private output contains every transport attempt and the
failed original verdict. The CLI approval-to-spawn boundary, host model calls,
actual fault, mission-level withholding and later observations were exercised;
the full control completion gate did not pass.

## Revised boundary trial (same day)

After the explicit applicability and interrupted-diagnostic repairs above, one
separately approved trial used the same engineering seed and scenario. The
original failed trial remains unchanged. This is not a held-out evaluation or
an automatic retry, and the two executions should not be pooled as a reliability
estimate. The [scalar summary](../assets/starship-trouble-control-20261013/summary.json)
binds both originals by their saved artifact hashes.

| Observed fact | Revised result |
|---|---|
| Jev transport | 21 attempts, 21 valid responses |
| DeepSeek transport | 1 actual attempt after Jev routing; 0 valid responses/accepted choices |
| Accepted model choices | 20; Jev's separate DeepSeek-routing response is not a final executed choice |
| Diagnostic completion | 5 Ship requests, all completed; 0 Booster diagnostic requests |
| Withholding | Jev at T+489.787 s; skipped/withheld at T+490.037 s |
| Invalid DeepSeek fallback | Withhold at T+621.287 s; observed at T+621.537 s |
| Record and control-completion checks | All pass; process exit 0 |
| Physical mission outcome | Ship unresolved in orbit at 800 s; Booster contact envelope still fails |

DeepSeek received a full 150-second orbital decision window and returned a
complete response after 8.809 seconds. The strict decoder rejected it and its
answer was not executed. Saved metadata has request/response hashes and sizes,
but does not distinguish a missing content field, invalid JSON, schema error,
invalid evidence ID or model mismatch. None of those causes is inferred here.
The next instrumentation change should preserve sanitized decoder failure
categories without retaining provider reasoning or silently retrying the model.
After this live trial, read-only review found that the prompt did not explicitly
state the decoder's 1–12 unique evidence-ID bound. The prompt now states every
returned-object field and size bound, with a serialization assertion in the
provider-boundary test. This is a contract-visibility repair, **not a diagnosed
cause** of the recorded failure. No new inference followed this clarification;
live `02` predates that prompt text, with the same action authority and runtime.

The exact revised command, after preparing the fresh `02` output with the same
arguments as above, was:

```sh
python scripts/run_starship_trouble_control.py \
  --approve-plan-sha256 321b525a7ed69fbaf980f66704e55bb47f07d8fc60f2a3e5827918374f939f0b \
  --output-dir output/starship-trouble-control-live-20261013-02
```

Host wall time was 42.324 seconds and frozen source hashes remained unchanged.
This establishes a real Jev response and a real, rejected DeepSeek attempt in
the experimental CLI. It does not establish a successful DeepSeek decision in
this adapter, an existing M3 Gateway route, or recovery of the faulted flight.

## Default no-fault qualification after control hooks

The [fresh 39-case qualification](../assets/starship-trouble-control-20261013/qualification-refresh.json)
covers retained counts 0–26 and fuel offsets ±1000 kg at counts 0, 14, 15, 16,
25 and 26. All 97,703 samples and complete run fields match this branch's prior
registered baseline exactly except source identity. The certificate's numerical
values and tested domain are unchanged; 39 independent registration/scalar
checks passed. The regenerated certificate SHA256 is
`f5f39701aa9b817e73e95d848d9d2bb3c8f561d106210e3cb12b098199415a18`.

This separate CPU-only cohort took 2229.507 seconds with two workers, no retries
and no model calls. It verifies that null trouble-control hooks preserve the
qualified no-fault path. It neither qualifies injected faults nor certifies the
new CLI's fixed-return baseline. Historical trial authorities retain their old
evidence bindings; current plans bind the regenerated certificate.

## Public-branch randomized integration trial (2026-10-14)

The fresh predeclared seed 20261014 used the default historically motivated
family pool, maximum two events, nominal probability 0.2 and 800 seconds. One
actual CLI host/worker execution applied ascent engine loss at T+71.832 s and
payload blockage at T+490.604 s. Jev returned 18 valid responses in 18 attempts;
one routed request produced a valid DeepSeek `continue_mission` choice. Jev
withheld deployment at T+490.854 s and the separate T+491.104 s observation
confirmed skipped/withheld with no releases. DeepSeek cited ten current-evidence
IDs; their decision-time values were checked. No fallback was used; three
requested diagnostics completed. All three record checks and bounded control
completion passed. This is not mission-success acceptance: Ship remained in
orbital coast at 800 seconds, and Booster contact failed at 154.432 m/s.
The frozen action scope has no Booster return-policy authority.

The [portable report](../assets/starship-trouble-control-20261014/summary.json)
retains every adopted action, fault, provider count, outcome, source map and
private-artifact hash. Original logs stay ignored. The earlier 20261013 failure
and rejected DeepSeek response are not overwritten or pooled into this trial.
The README recovery-choice movie is a separate saved execution without random
faults. This connection uses MissionOS's CLI approval/host/worker boundary, not
a registered existing M3 Gateway fault scenario.
