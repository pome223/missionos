# M3 completion campaign

Current runtime source: `8a55ee699e79a331a3953b5f175e6ed9378ba02a`.
Frozen matrix: `scope_closure_v2`; protocol SHA-256:
`9516203a27b2c4c214750659e4ba631b5676339884832ab727a93eb810ac9237`.

**Current aggregate: `passed: true`, `m3_completed: true`, `issues: []`.**
The independent audit recomputed the aggregate from all four raw records and
matched the saved verdict. Source, authority, Gateway signatures, artifact
bindings and registered runtime boundaries passed. Runtime-source CI passed on
Python 3.11 and 3.13. Internal development only; no public synchronization
or merge is included in this update. Each role is a new bounded attempt on the
same frozen source. The earlier campaigns retain their original verdicts.

## Current frozen matrix

| Role | Registered scenario | Observed result and coverage |
| --- | --- | --- |
| scope_amendment | `sixdof_m3_inflight_op_scope_01` | Verified record and accepted case; DeepSeek selected next orbit; candidate-bound HTTP approval, one consumption, later reservation observation, checked dispatch and accepted contact |
| normal | `sixdof_m3_op_trial_05` | Satisfied: 26 released; nominal return; Ship 4.608 m/s; Jev 9/9 valid, no DeepSeek or fallback |
| deployment_change | `sixdof_m3_release_fault` | Satisfied: actual 26-payload retention handed into next-orbit return; Ship 3.473 m/s; Jev 16/16 valid, no fallback |
| ambiguous_comparison | `sixdof_m3_op_trial_07` | Satisfied: first DeepSeek response rejected; later new-notice comparison adopted keep_plan; Ship 4.608 m/s; Jev 7/7, DeepSeek 1/2, 16 references resolved |

[Current report](../assets/starship-m3-final-20261012/report.html) and [portable summary](../assets/starship-m3-final-20261012/summary.json).
The aggregate verdict comes from the frozen protocol, raw saved studies,
signed Gateway evidence and runtime ledgers. A metrics-only report does not
replace those checks. Retain a negative or unexercised role as such.

## Executed authority-extension chain

The independent scope audit checks source and authority equality, Gateway
signatures, raw-study artifact binding and production record/case verification.
The model selected a candidate; it did not grant itself authority.

| Event | Simulation time (s) | Actor or boundary |
| --- | ---: | --- |
| Selected next-orbit candidate | 1567.700 | DeepSeek; `authority_gap` route |
| Candidate-bound extension requested | 1567.950 | Executor, bound candidate and observed state |
| Authenticated approval received | 1569.450 | Scripted test operator via HTTP |
| Single-use consumption and reservation applied | 1569.450 | Host ledger and executor |
| Reserved candidate observed afterward | 1569.700 | Distinct later observation |
| Independently checked return dispatched | 7731.450 | Executor |
| Ship contact | 10378.627 | Simulator; 4.613326 m/s, 2.396269 degrees, 62,433.932 kg reserve |

There is one request, one receipt and one consumption. The authenticated host
attestation is verified; the ephemeral internal ledger signing key was not
recovered for an independent cryptographic replay of each ledger signature.
Request, receipt, consumption and reservation content bindings were checked.
Operator source is `scripted_test_driver`; actual human review and SSO identity
remain false. Keep those facts distinct from the successful HTTP chain.

Scope-role counts: Jev 12 attempts / 11 valid and adopted; DeepSeek 1 / 1;
9 numerical references submitted / 9 resolved; one fallback.
Final campaign totals: Jev 44 attempts / 43 valid and adopted; DeepSeek 3 attempts
/ 2 valid and adopted; 25/25 numerical references resolved; two checked fallbacks.
These are current-cohort observations, not a statistical reliability claim.
Reference checks validate cited input fields, not free prose, internal reasoning
or forecast truth.

In the ambiguous-comparison role, DeepSeek first returned `response_too_large`
for the request at T+1573.200 s. The response was not adopted and a checked
fallback preserved the booking. A later notice produced a different evidence
generation and request at T+1890.200 s; its valid `keep_plan` decision cited
16 resolved fields. This was a new event-driven decision, not a same-input retry.
The scope role separately had one invalid Jev response and checked fallback.

## Reasoning admission and preserved authority

The versioned opt-in `authority_gap_reasoning` policy gives DeepSeek a route
when no physically admissible authorized return remains but a fresh, physically
checked candidate requires candidate-bound scope approval. Original simulation
and wall deadlines must both leave the full reasoning window, operator scheduling
reserve and coast guard. The route neither authorizes the candidate nor forces
an action or operator response. It is limited to one reasoning call per prepared
evidence generation; DeepSeek has no call-count cap. The existing uncertain
comparison route remains separate.

The 20-second operator reserve is a source-bound scheduling assumption, not a
human response guarantee. Numeric feasibility, approved scope, operator response,
execution admission and post-action observations remain separate checks.

## Evidence limits

Four integrated flights reproduced one nominal booster trajectory (1.884 m/s
contact); this is reproducibility evidence, not four independent disturbance cases.
Do not count repeated nominal trajectories as disturbance cases. Earlier fuel
+/-1 t probes failed on historical source and have not qualified current source.
Model water-entry criteria do not prove seawater survival or hardware recovery.
Physical execution remains false.

Contact-location numerical sensitivity remains unresolved; the earlier half-step
probe moved the prediction by about 77 km. This campaign does not qualify
arbitrary-area targeting or multi-flight shared-area reservations. These are
targeted, in-sample capability regressions with synthetic notices and areas,
not unseen-fault or fleet-operation qualification.

## Historical evidence

The `a5c92808` four-role result below remains incomplete because its scope role
did not exercise amendment. The [interrupted `4f881d6d` follow-up](../assets/starship-m3-scope-20261012/report.html)
still ends at its predeclared output cap. The [deadline-aware `e9ec7219` trial](../assets/starship-m3-authority-gap-20261012/report.html)
still ends unresolved, with valid signed records but no amendment request; the
rest of that matrix was not run. Their original source, authorities, counts and
verdicts are unchanged. Current coverage is not substituted into an old protocol.

## Fixed scope

M3 means one governed launch, concurrent booster and Ship supervision, actual
payload inventory handed into return, and candidate-bound in-flight authority
extension. Completion does not establish SpaceX fidelity, seawater survival,
physical flight, all-fault robustness, arbitrary-site guidance, or multi-flight
operations. It does not require tower catch. Rules and fallback parity are
safety/regression evidence, not a score against which to dismiss AI judgment.

The aggregate verdict is separate from each flight's record and outcome verdict.
Normal flight does not need an artificial DeepSeek call. A provider outage can
leave a physically accepted flight without demonstrating an adopted AI decision.
These facts must not be collapsed into one flag.

## Historical frozen flight matrix (`a5c92808`)

Before the first provider invocation, save the four authorities, exact source
hashes, role-to-scenario mapping, one-attempt policy, and protocol hash. The
Gateway assigns run IDs at launch; signed consumed approvals and worker receipts
subsequently bind each identity and raw study hash to the frozen role. Do not
invent run IDs retroactively or retry a failed row until it passes.

The campaign CLI preserves every verification result. After a preflight check,
use `verify --verification-output completion-final.json` for the later result;
an existing filename is rejected rather than overwritten.

| Role | Registered scenario | Required observation |
| --- | --- | --- |
| normal | `sixdof_m3_op_trial_05` | 26 released, original return retained, accepted Ship contact and booster model water entry |
| deployment_change | `sixdof_m3_release_fault` | Real Jev deployment decision, changed retained inventory, fresh return assessment from the actual same-flight state, accepted observed return |
| scope_amendment | `sixdof_m3_inflight_op_trial_02` | AI requests a particular next-orbit candidate beyond initial nominal-only scope; authenticated HTTP approval; independent single-use consumption; later reservation observation and checked dispatch |
| ambiguous_comparison | `sixdof_m3_op_trial_07` | Actual DeepSeek comparison of delivered uncertain updates, adopted decision and independently resolved numerical references; physically checked outcome |

The operational trial seeds and synthetic notice series already exist; new
seeds from the same generator are not evidence of unknown-fault generalization.
The fault is an existing failed payload-separation condition, not a new physical
engine or weather injection. Scope non-use is an unexercised row, not success.

Each flight retains its approved wall-time, prediction, disk and output bounds.
All four live M3 grants have an explicit null DeepSeek call-count cap; deadlines,
eligibility, accounting and independent action checks remain. No action or
provider invocation is forced merely to satisfy a coverage counter. Actual adopted
Jev decisions must cover ascent, booster, deployment and return across the four
flights; DeepSeek must supply the ambiguous-comparison decision. Per-flight
physical acceptance alone does not satisfy this aggregate coverage requirement.

## Historical campaign result (`a5c92808`)

The [report](../assets/starship-m3-completion-20261012/report.html) and
[portable summary](../assets/starship-m3-completion-20261012/summary.json) preserve
the observed duties and outcomes. The aggregate verdict comes from raw saved
studies, the frozen protocol and locally verified signed Gateway evidence, not
from the metrics-only report. The report includes results from a separate audit of all four Gateway signatures
and bindings; it does not replace the aggregate verdict. The aggregate is
fail-fast and stopped at scope non-use before checking the fourth role and
boundary records. The separate audit checked those omitted records with the
same production functions, without changing the unmet aggregate result.
The final `completion-final.json` preserves the earlier preflight verdicts.

| Frozen role | Flight and provider observations | Role coverage |
| --- | --- | --- |
| normal | 26 released; original plan retained; Ship 4.608 m/s; Jev 9/9 valid | Satisfied |
| deployment_change | 0 released, 26 retained; additional mechanism observation, deployment stop, actual-state return reassessment and next-orbit selection; Ship 3.464 m/s; Jev 16/16 valid | Satisfied |
| scope_amendment | 26 released; nominal return; Ship 4.608 m/s; Jev 8/9 valid; DeepSeek 1/1 valid, 12 references checked | Unexercised: no request, approval, consumption or amended reservation |
| ambiguous_comparison | 26 released; next-orbit selection; Ship 4.613 m/s; Jev 12/12 valid; DeepSeek 2/2 valid, 16 + 12 references checked | Satisfied |

Totals apply only to these four new flights: Jev 46 attempts / 45 valid,
DeepSeek 3 / 3, and 40 / 40 reference IDs independently resolved to numerical
fields in their original decision-time requests. Free-text reasons, internal
reasoning and forecast truth are not verified by this reference audit. There
was one fallback in the scope-amendment flight. All four concurrent boosters
met 8/8 model water-entry checks, with contact-point speed about 1.884 m/s.

The reviewed `booster_reproducibility` audit in the portable summary compares
the four raw concurrent-prefix studies. It records four fresh integration runs
but one unique complete `booster_run`, including the same separation state,
243 samples, 7 events, actuator commands/states, contact and goal. The canonical
JSON hashes are reproduced from the saved records; they do not add a new flight
or disturbance case. Nominal reproducibility therefore does not qualify fuel,
wind, attitude or other perturbations. The [older fuel +/-1 t probes](../assets/starship-splashdown-20261007/summary.json)
failed on their recorded older source and have not been rerun on the current
campaign source. Both the earlier 8.285 m/s prefix and the present splashdown
use the same geographic reference site; only the latter connects the separate
water-entry goal and its hull-clearance terminal controller.

Normal and uncertain-update flights retained zero payloads; the release fault's
26 retained payloads remained bound to its same-flight return assessment.
Human flight commands were zero. Physical execution, seawater survival, SSO
identity and physical-fault robustness were not established.

In the scope role, delivered clearance allowed the eastern model area again
before the nominal dispatch deadline. The AI chose that permitted plan instead
of requesting the next-orbit candidate beyond its scope. The flight verifier
passed, but `scope_amendment_applied` and `reservation_observed` stayed false,
the scripted operator stayed unresolved, and its driver returned 3. A valid
comparison with references and accepted contact do not satisfy the registered
amendment chain. This is missing campaign coverage; choosing the permitted
nominal plan is not by itself evidence of poor AI judgment.

The aggregate confirmed adopted live Jev decisions in ascent, booster,
deployment and return, and live DeepSeek return comparison. Its only reported
issue was `m3_live_scope_amendment_unproven`. Keep that result in its frozen role.
Do not borrow fixture coverage, relabel another flight, change notice timing
after observing the result, force an AI action, or retry until approval is requested.
Any follow-up needs its own bounded protocol and preserved evidence.

## Historical boundary matrix and operator identity (`a5c92808`)

At the tested source, real loopback HTTP passed 14 checks covering authentication,
approval without dispatch, rejection, expiry and duplicate response or consumption
using raw request/receipt/consumption records. An actor trace
covers delayed/missing model replies and an observed ascent health change while
a previous request is pending. The aggregate replays these registered checks;
boolean smoke summaries alone cannot establish completion.

Those negative rows are bounded fixture/runtime evidence, not full engine-out
flights or real network outages. Every live flight supplies actual concurrent
booster contact/site/resource evidence. No new booster disturbance qualification
is implied.

The automated approval driver uses a configured bearer principal named as a
scripted test operator and the same HTTP path as the local console. The live
scope role produced no request for it to resolve. The separate fixture HTTP
checks exercise approval, rejection and expiry. Neither is evidence that a person
reviewed a flight request, nor can it measure human workload or SSO identity.
The human console remains available for actual operator use. The operator bearer, ledger key and Gateway signing key stay off
model inputs and the physics process.

## Completion checks

`starship_m3_completion.verify_completion` consumes raw saved studies, frozen
protocol, signed Gateway evidence and registered runtime ledgers. It re-runs
flight verification and rejects missing roles, substituted authorities, duplicate
run identities, changed source, wrong artifact hashes, unconsumed approval,
unobserved amendments, and missing real-provider coverage. Keys are provided in
memory and never copied into a report. An unresolved or unexercised row leaves
`m3_completed` false; historical single-flight flags are not rewritten.

Numerical touchdown sensitivity remains an open limitation, and areas are model
study areas. A finite set of accepted flights is not a proven global safety
bound. All failures and original provider outputs remain private evidence;
portable reports contain reviewed metrics and claim boundaries only.
