# Continuous Starship mission control: M2 and M3 development

MissionOS completed the bounded M3 development matrix: live AI supervised launch,
deployment and return, including a candidate-bound in-flight authority extension.
All four preregistered roles passed their record, outcome and aggregate coverage
checks.

In the authority-extension flight, DeepSeek chose a checked next-orbit return
that the initial scope did not authorize. MissionOS sent a request bound to that
candidate and the observed state. An authenticated scripted operator approved it
through HTTP; the executor consumed that approval once, applied the reservation,
and confirmed it in a later observation. The Ship then followed the approved
return and met the model contact and area criteria at 4.613 m/s.

[Campaign report and decision records](../assets/starship-m3-final-20261012/report.html) and the
[portable summary](../assets/starship-m3-final-20261012/summary.json) separate AI decisions, operator approval,
execution, observed outcomes and campaign coverage.

| Frozen campaign role | Observed result | Real-model evidence |
| --- | --- | --- |
| Normal | 26 released; original return retained; contact 4.608 m/s | Jev 9/9 valid; DeepSeek 0 calls; no fallback |
| Deployment change | 0 released, 26 retained; additional observation and deployment stop; next-orbit return; contact 3.473 m/s | Jev 16/16 valid; DeepSeek 0 calls; no fallback |
| Scope amendment | 26 released; approved next-orbit return; contact 4.613 m/s; complete request-to-return chain | Jev 11/12 valid, DeepSeek 1/1 valid; 9/9 numerical references resolved; one checked fallback |
| Ambiguous comparison | 26 released; original return retained after a later notice prompted a fresh comparison; contact 4.608 m/s | Jev 7/7 valid; DeepSeek 1/2 valid; 16/16 numerical references resolved; one checked fallback |

The current campaign used frozen source `8a55ee69`, a new protocol and one
attempt per role. The independent audit recomputed and matched the passing
aggregate (`m3_completed: true`). Across these flights, Jev returned 43 valid
responses in 44 attempts and DeepSeek returned 2 in 3. The verifier resolved
25/25 cited numerical references against
the inputs available at each decision. It did not verify free-text reasons or
internal reasoning. These development trials are not a reliability estimate.

In the ambiguous-comparison flight, the first DeepSeek response was rejected as
`response_too_large`. A later notice created a new evidence generation, and a
new comparison validly kept the original plan. This was not a retry of the same
input; the original failure and its checked fallback remain recorded.

The in-flight approver was a test script, not a person reviewing the request.
The approval path used an authenticated local principal; this does not establish
human workload reduction or SSO identity. Four integrated flights reproduced one
nominal booster trajectory (1.884 m/s contact); this is reproducibility evidence,
not four independent disturbance cases.
Booster disturbance tolerance, seawater survival and the previously observed
contact-location sensitivity to integration step size remain unresolved.
Recovery areas are synthetic study areas, not validated operational landing zones.

The objective is AI mission control: observe, obtain evidence, decide, pass
independent checks, execute and confirm later observations. Numeric prediction
and fallback are tools and safety nets inside MissionOS. Their agreement with an
AI decision is not a reason to dismiss that decision. Normal flight does not
require an artificial DeepSeek call.

## Historical campaigns and preserved negative results

The earlier four-flight campaign at `a5c92808` remains incomplete because its
scope role did not request an amendment. The `4f881d6d` follow-up remains
interrupted at its unchanged output limit. The `e9ec7219` trial remains a verified
but unresolved flight with no amendment request. The current result does not
change those verdicts; it follows an explicit routing capability change and a
separately frozen protocol.

### Original four-role campaign (`a5c92808`)

At source `a5c92808`, MissionOS completed four preregistered flights using real Jev and DeepSeek
through the Gateway and a separate six-DOF worker. All four met the Ship contact
and model-area criteria, and all four concurrent boosters met the eight model
water-entry criteria. In the release-fault flight, Jev requested more evidence,
stopped deployment, and selected a next-orbit return from the actual flight
state with all 26 payloads retained. In the uncertain-update flight, DeepSeek
compared the return options twice and selected the next orbit.

[Campaign report and decision records](../assets/starship-m3-completion-20261012/report.html)
and the [portable summary](../assets/starship-m3-completion-20261012/summary.json)
separate executed AI decisions, flight outcomes and campaign coverage.

**That historical campaign did not establish full M3 completion.** The scope-amendment flight reached
accepted contact, but its eastern model area's clearance returned before the
deadline and the AI selected the original plan. No authority-extension request,
operator approval or amended reservation was exercised. The independent aggregate
verifier therefore returned `m3_live_scope_amendment_unproven` rather than treating
four accepted flights as a completed campaign. Each role ran once; none was
retried or relabelled to fill the gap.

| Frozen campaign role | Observed Ship result | Real-model evidence |
| --- | --- | --- |
| Normal | 26 released; original plan retained; contact 4.608 m/s | Jev 9/9 valid |
| Deployment change | 0 released, 26 retained; next-orbit return; contact 3.464 m/s | Jev 16/16 valid; additional observation, deployment stop and return reassessment adopted |
| Scope amendment | 26 released; original plan selected; contact 4.608 m/s; required amendment unexercised | Jev 8/9 valid; DeepSeek 1/1 valid, 12 numerical references checked; one fallback |
| Ambiguous comparison | 26 released; next-orbit return; contact 4.613 m/s | Jev 12/12 valid; DeepSeek 2/2 valid, 16 and 12 numerical references checked |

Across these four flights, Jev returned 45 valid responses in 46 attempts and
DeepSeek returned 3 in 3. All 40 selected reference IDs resolved to numerical
fields supplied at decision time. This checks the cited inputs, not the accuracy
of free-text reasons, internal reasoning or forecasts.

Booster contact-point speed was approximately 1.884 m/s in all four flights.
The four fresh integrated runs reproduced one identical nominal booster
trajectory: the separation state and all 243 recorded recovery samples match.
They confirm integration reproducibility, not robustness to four disturbances.
[Earlier fuel +/-1 t probes](starship-splashdown.md) failed on older source;
those saved-separation probes have not been rerun on this campaign's source.
Meeting the model's water-entry criteria does not demonstrate seawater survival
or hardware recovery. Flight-time human commands were zero. The configured
scripted operator was not a human reviewer and received no amendment request.

The objective is AI mission control: observe, obtain evidence, decide, pass
independent checks, execute, and confirm later observations. Numeric prediction
and fallback are tools and safety nets inside MissionOS, not an opponent used to
score AI value. Normal flight does not need an artificial DeepSeek call.

### Separate scope-amendment follow-up

A [separate targeted flight](../assets/starship-m3-scope-20261012/report.html)
withdrew the original area's clearance without publishing its reinstatement.
Jev continued waiting; no expansion request or operator approval occurred. The
Gateway then stopped at the predeclared 1.2 GB output limit, with 18.6 GB of disk
space still free. This is an interrupted, unresolved result, not a completed
return. The original four flights and their coverage verdict remain unchanged.

The saved inputs exposed a concrete interface gap: MissionOS did not tell the
model the approval-request deadline, and still showed the out-of-scope choice
after that deadline. That omission does not prove why the model first chose to
wait. That finding led to deadline-aware scope requests and bounded evidence
storage in the separately frozen follow-up; this flight was not retried.

### Deadline-aware follow-up

The [next frozen trial](../assets/starship-m3-authority-gap-20261012/report.html)
finished with a signed, consistent record within the output cap. The request
window was visible, including a final check before expiry. Jev still did not
request expansion; Ship return remained unresolved and the other three roles
were not started. Later waiting after expiry was the only available action,
not repeated independent refusals.

The later `8a55ee69` implementation added an explicitly approved reasoning route for a
different kind of decision: a physically checked return is available but lies
outside current authority. That case previously could not reach DeepSeek because
there was only one feasible candidate. Routing supplies reasoning time, not
approval or a required choice. Its separately frozen validation is the current
passing campaign above; the unresolved predecessor remains unchanged.

### Earlier main-path flight

Before the frozen campaign, MissionOS completed one real-provider flight through
the main M1--M3 path:
launch, 26 payload releases, concurrent booster water-entry checks, two-candidate
orbital comparison, and observed Ship return. Jev supplied six adopted decisions
from seven attempts; DeepSeek compared both return options, cited 12 input values
that the verifier resolved, and chose to keep the booked plan. Independent checks
and later observations connect those decisions to the same six-DOF flight.

[Earlier live report and actual trajectory](../assets/starship-m23-20261011/m123-live-repaired-report.html)
shows the `fe07fa2f` flight. Ship contact-point speed was 4.608 m/s in the eastern
model area; booster contact-point speed was 1.884 m/s with all eight registered
model water-entry criteria met. Flight-time human commands: zero. One invalid
Jev deployment response used an independently checked continuation fallback.
There was no return fallback. Record verification and this case both passed.

That record completed one flight's main path, not the full M1--M3 qualification
matrix.
In-flight authority extension and anomaly/disturbance coverage remain separate
requirements. The [previous failed live flight](../assets/starship-m23-20261011/m3-live-report.html)
remains intact: 20 valid Jev decisions, no DeepSeek call, 26 retained payloads and
unresolved Ship return. Each bounded flight was attempted once, with its own
frozen source and evidence; the earlier failure was not overwritten.

### Earlier seeded operational updates

The [earlier uncapped development flight](../assets/starship-m23-20261011/op-trial-03-uncapped-report.html)
completed with real Jev and DeepSeek after removing DeepSeek's call-count limit.
Jev made seven adopted decisions. Both corrected notices triggered DeepSeek to
compare the two return options again; both decisions kept the booked plan.
The verifier resolved 13 and 12 cited input values. No fallback was needed.

Notifications were detected 0.245 and 0.140 simulated seconds after delivery.
The flight released all 26 payloads, contacted the eastern model area at
4.608 m/s, and met the booster model water-entry criteria at 1.884 m/s.
Independent saved-record verification also passed; in-flight human commands: zero.

The [previous capped flight](../assets/starship-m23-20261011/op-trial-03-report.html)
keeps its original evidence: the second correction exhausted its one-call limit.
This new flight used the same pre-fixed notification timing, delivery delays and
uncertain recovery estimates, with a newly approved count policy. Deadlines and
independent checks still applied. Each trial ran once; this is not an unseen
scenario or a robustness estimate. Physical actuator faults remain future work.
The [research and trial contract](../agents/starship-operational-trials.md) links
NASA/SpaceX precedents and separates them from assumed trial parameters.

### Earlier recorded results

| Run | What happened | Boundary |
| --- | --- | --- |
| Uncapped operational updates (`627e9647`) | Jev 7/7 and DeepSeek 2/2 valid/adopted; both corrections compared, 25 numerical references resolved; 26 released; Ship 4.608 m/s, booster 1.884 m/s; no fallback | Real HTTP, separate 6DOF worker, same-state handoff; one new development trial, no retry; complete record recheck passed |
| Repaired M1--M3 live (`fe07fa2f`) | 26 released / 0 retained. Jev 7 attempts / 6 valid and adopted; DeepSeek 1 / 1, 12 validated numerical references, `keep_plan`. Ship 4.608 m/s and 62.39 t fuel; booster 1.884 m/s and 8/8 water-entry criteria | Real Gateway HTTP, separate 6DOF worker, same-flight handoff, real providers; record and case accepted; one new trial, no retry; full M3 matrix incomplete |
| Previous M3 live (`cadfe521`) | Jev 20/20 valid and adopted across ascent, booster, deployment and return; DeepSeek 0. Booster contact 1.884 m/s; Ship remains at 370.35 km, 26 payloads retained | Production HTTP, approved live grant, separate 6DOF worker; record verification passed, case failed; one trial, no retry |
| M2 live operations notice | 0 released, 26 retained; next-orbit return contacted at 3.464 m/s, tilt 0.417 degrees, fuel 45.20 t inside the western model area | 11/11 valid Jev responses adopted, no return fallback; original verification and corrected record revalidation are separate below |
| M2 operations-notice fixture | One payload released, 25 retained; a bounded departure-time search admitted a return; contact 3.495 m/s, tilt 0.470 degrees, fuel 45.77 t in the western model area | Actual launch-to-return 6DOF continuation; fixed decisions, zero model calls; source predates review hardening |
| M3 completed fixture (`9806f8a5`) | Launch, 26 releases, booster water-entry criteria, HTTP approval, next-orbit Ship return; Ship contact 4.613 m/s and 62.43 t fuel in the western model area | Real HTTP and separate six-DOF worker; one scripted operator approval, 0 model calls; not a live AI-control completion |
| M3 completed approval chain | Scope applied at T+1402.45 s, later reservation observation at T+1402.70 s, checked return dispatch at T+7731.45 s, contact at T+10378.627 s | The deliberately changed plan passes the scope-amendment fixture; the unchanged-plan normal-case criterion remains false |
| M3 previous partial fixture (`09db378d`) | 26 releases; booster contact-point speed 1.884 m/s, tilt 0.296 degrees, fuel 11.42 t, 8.37 km from the registered site | All eight frozen model water-entry criteria met; one fixture trajectory, not robustness or recovered hardware |
| M3 previous HTTP approval | Approved at T+1403.95 s; a distinct executor telemetry sample at T+1893.95 s still shows the approved next-orbit candidate and scope revision 1 | Scripted operator. Ship burn and contact not reached; controlled early stop, not a successful full M3 flight |
| M3 earlier concurrent prefix | Ascent monitoring and 26 releases; both bodies integrated on one coordinator clock; booster contacted at T+609.109 s before the Ship checkpoint at T+1000.7 s | Fixture decisions; Ship return not included in this run |
| M3 booster result in that prefix | Contact-point speed 8.285 m/s; contact envelope not met | Preserved failed recovery, not a splashdown success |
| Coast-control terminal development | Three saved-separation cases, nominal and fuel +/-1 t, aligned and ignited but exhausted fuel aloft; contact about 587 m/s | Candidate rejected for ordinary operation; not three successful recoveries |
| Reserve-aware terminal candidate | One fixed trial never ignited and contacted at 1049.7 m/s | Failed candidate; not adopted, no retry |
| In-flight approval HTTP check | Authenticated delivery, binding, consumption, expiry and replay checks passed | Scripted operator and fixture snapshots; this check alone is not a flight operation or real-person identity verification |

The live M2 run's original HTTP result remains failed: its verifier compared
search times differing by about 9.09e-13 seconds with exact equality. Revalidating
the saved records with the executed-source verifier and only that comparison
corrected to an absolute tolerance of 1e-7 seconds passed the M2 checks. No new
flight or model call was made for revalidation. This does not demonstrate a
flight using the later integrated source.

Two fixed full M3 fixture attempts stopped before flight. The first failed
Gateway initialization because the bootstrap omitted `missionos_core`. After
that repair, a second attempt passed Gateway HTTP readiness but stopped before
the first physical step because the nested evidence directory did not exist.
Neither attempt delivered an approval request or produced a flight outcome.
Both negative records are preserved. The directory producer now creates its
fresh output directory and refuses to overwrite existing evidence. Runtime-only
HTTP and pre-physics initialization checks pass; these are not a completed
approval-to-return flight.

The model areas are synthetic and unchanged. A search considers three departure
samples around the next opportunity and proposes one time, which still needs the
existing four prediction checks. This is neither arbitrary-site guidance nor
exhaustive reachability. If a refined proposal fails admission, the original next
opportunity is not retained as an extra candidate in this limited search; the
result is unresolved within the tested search, not proof that no return exists.

### What blocked the previous live comparison

The deployment setting was normal, with no injected deployment fault. Stopping
it changed the retained inventory and predicted landing locations. All 16 return
Jev decisions requested another observation before the corrected notice arrived.
Two integration problems then limited replanning:

- Exhausting the Jev budget closes the request route before its separately
  budgeted DeepSeek comparison can be considered.
- The comparison batch supplies a 600-second search deadline, while the search
  requires its full 600-second budget to finish strictly before that deadline.
  Equality rejects the search before any departure-time trial is run.

Eight ordinary forecasts ran, but both candidates failed the area/availability
check. Separating provider budgets alone would not establish a viable comparison
for this flight. The unexecuted search is not evidence that no return is possible.
Those findings remain part of the previous recorded source. The repaired source
separates provider reservations, refreshes an unchanged publication at most once,
clarifies deployment action/qualification semantics, and shares the preparation
deadline across screening and admission. The repaired `fe07fa2f` flight exercised the
provider chain. Its valid nominal booking prevented optional time search, so
search composition is covered by actor/runtime regression tests, not claimed as
an executed live search. No cosmetic DeepSeek call was added.

### Water-entry result and limits

Correcting coast attitude exposed a separate terminal-guidance problem. The
three-engine, constant-mass stopping-height estimate requests ignition around
86 km while only about 37 seconds of full-thrust fuel remains. Fuel is exhausted
around 40 km. Better attitude therefore does not by itself provide a controlled
water entry. A separate reserve-aware candidate found no sampled overlap
between a reserve-preserving stop and the near-surface ignition window; it
therefore never ignited and also failed. Neither candidate was adopted, and
neither actuator strength nor contact criteria were relaxed.

The earlier 8.285 m/s concurrent run used the same offshore reference site as
this campaign, but did not connect the existing splashdown goal. The change is
the water-entry goal and terminal control, not a different geographic destination.
Saved-record analysis found that its terminal vertical-speed target used
center-of-mass height even at hull contact. The M3 adapter now passes the existing
goal through a separate booster authority, enabling the existing hull-clearance
control without changing actuator strength or the goal limits. The repaired concurrent runs
met the registered 10 km water-entry goal; the separate legacy 1 km criterion
remains false. Upper-flight attitude instability and robustness to disturbances
remain unresolved. Water response and structural survival are not modeled.

### Earlier stop and completed repair check

The selected return candidate contains four prediction traces, totaling 5.27 MB.
The approval observer hashed it four times per physical step. One saved-input
measurement, extrapolated over the remaining coast steps, estimated about 133
CPU minutes for these hashes alone. The run was stopped after 17.66 wall minutes
because it could not credibly fit its fixed 40-minute budget. This was an early
stop, not the budget timer firing; both previous preflight failures remain saved.

The repair makes per-step telemetry reference the selected plan revision's
recorded candidate identity. Selection, approval application, fallback and burn
dispatch still check the current full candidate and physical feasibility. One
separately authorized flight with that repair finished in about ten minutes:
8 forecasts, no model calls, a changed reservation, checked return dispatch and
observed contact. The two preflight failures and previous early stop remain
preserved; no failed record was overwritten.

### Evidence and limits

[Earlier uncapped operational-trial audit](../assets/starship-m23-20261011/op-trial-03-uncapped-audit.json),
[earlier capped operational-trial audit](../assets/starship-m23-20261011/op-trial-03-audit.json),
[earlier main-path live audit](../assets/starship-m23-20261011/m123-live-repaired-audit.json),
[previous failed live audit](../assets/starship-m23-20261011/m3-live-jev-deepseek-audit.json),
[Completed M3 fixture audit](../assets/starship-m23-20261011/m3-http-completed-flight-audit.json),
[Previous partial M3 flight audit](../assets/starship-m23-20261011/m3-http-bounded-flight-audit.json),
[booster water-entry scalar checks](../assets/starship-m23-20261011/m3-booster-water-entry-audit.json),
[hashing cost diagnosis](../assets/starship-m23-20261011/m3-reservation-hash-performance.json),
[Live M2 original result](../assets/starship-m23-20261011/m2-live-summary.json),
[saved-record revalidation](../assets/starship-m23-20261011/m2-live-revalidation-frozen-source.json),
[fixture flight](../assets/starship-m23-20261011/m2-fixture-summary.json),
[booster trials](../assets/starship-m23-20261011/booster-terminal-summary.json),
[terminal diagnosis](../assets/starship-m23-20261011/booster-terminal-diagnosis.json),
[reserve-aware trial](../assets/starship-m23-20261011/booster-reserve-terminal-summary.json),
[HTTP boundary](../assets/starship-m23-20261011/inflight-http-summary.json),
[failed full M3 attempt](../assets/starship-m23-20261011/m3-http-flight-failure.json),
[startup repair check](../assets/starship-m23-20261011/m3-runtime-startup.json),
[second preflight failure](../assets/starship-m23-20261011/m3-http-startup-repaired-failure.json),
[saved-record booster diagnosis](../assets/starship-m23-20261011/booster-standard-diagnosis.json),
[39-case numerical regression](../assets/starship-m23-20261011/concurrent-qualification-summary.json).

These are synthetic six-DOF tests, not SpaceX validation, recovered hardware,
water survivability, or physical execution. Payloads are generic rigid bodies;
Starlink deployment mechanisms, communications and orbital raising are not
reproduced. Independent record checks do not constitute independent validation
of the shared physics model. Landing-location numerical sensitivity remains a
limitation before these areas can support multi-flight scheduling guarantees.

See the [maintainer contract](../agents/starship-m23-control-contract.md) for
authority and the fixed trial budgets, and the
[frozen M3 campaign contract](../agents/starship-m3-completion.md) for the latest
aggregate result. The four-flight campaign leaves live scope amendment
unexercised. Broader physical-fault robustness and multi-flight operation remain
separate future work; four accepted numerical flights do not establish either.
