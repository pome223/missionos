# Orbital comparison preparation

## Goal and authority

This internal follow-up prepares the evidence needed for a return decision during
post-deployment orbital coast. MissionOS computes the missing material; the
model compares it, retains or updates the return plan inside preapproved authority,
and independent admission and later observations constrain the resulting action.
This does not expand ascent, deployment, booster, catch or fleet authority.

The opt-in `comparison_preparation` envelope is approved and hashed with the
existing M1 authority. Existing M1 and orbital-event cases retain their previous
settings. Rules and fallback runs are constraints, contingency and regression
checks, never an AI capability score or an answer key. See the
[flight-control contract](starship-mission-control-contract.md#ai-evaluation-basis).
An unresolved stop does not count as a safe completed recovery.

## Frozen campaign

[The campaign configuration](../../examples/spaceflight/starship-comparison-operations.json)
is fixed before evaluation. Its future input schedule and case identifiers stay
inside the scenario adapter. The model receives only notices already delivered,
current observations, prepared candidate material and remaining authority.

| Condition | Delivered notice | Model condition | Required evidence |
| --- | --- | --- | --- |
| `comparison_normal` | T+1350 s, unchanged operational content | live | preserve the nominal plan without optional alternative forecasting for publication metadata alone |
| `comparison_update` | T+1350 s, corrected service estimate | live | automatically prepare both feasible candidates, expose their costs, obtain and independently admit an executed AI decision; retaining the plan may pass |
| `comparison_late` | T+2250 s, corrected service estimate | fixture | optional comparison cannot consume the already admitted nominal burn deadline |
| `comparison_tool_failure` | T+1350 s, corrected estimate; cancel optional forecast | fixture | record failed preparation and preserve the currently admissible booking |
| `comparison_timeout` | T+1350 s, corrected estimate; persistent missing responses thereafter | fixture | record missing model decisions and execute only an independently admitted preapproved fallback |

The last three conditions may use a finite fake-plant production-loop harness;
reports must distinguish that boundary from a full numerical flight. Live model
calls and physical prediction runs are opt-in. Each campaign report binds its
configuration/source hashes and identifies which boundaries it actually ran.
A failed bounded live run is retained; do not tune the prompt or repeat that same
case until it passes. Implementation defects may be fixed and reported as a new
source scope, with the original failed record preserved and no held-out claim.

Both study areas retain the previous explicit windows: eastern T+4500–5600 s,
western T+10000–11000 s, notice expiry T+12000 s. The initial uncertain post-contact
service delays are east 450–1050 s and west 900–2100 s. A material update changes
the east to 6600–9900 s; the west remains unchanged. These are synthetic estimates,
not clearance or measured servicing outcomes. No preferred answer is embedded in
the case gate: both feasible choices can be accepted.

Waiting uses an initial 22 kWh budget, 6 kW load and 12 kWh floor. At the existing
next-orbit opportunity, approximately 5451 s later, the budget is approximately
12.9 kWh. Thus both options can remain feasible while waiting has a material
energy margin cost. Initial temperature 295 K, 0.001 K/s upper warming, 310 K
ceiling and 0.02 kg/s additional propellant-loss bound remain engineering
assumptions. They are accounting bounds, not validated electrical, thermal or
boil-off physics; physical attitude RCS consumption is a separate simulator cost.

## Preparation and deadlines

Initially prepare only `nominal`. After a material operational update, consider
`nominal` and `next_orbit`. Material notice fields are `areas`, `expires_at_s`,
`text` and `recovery_service_forecast`; `sequence` and `issued_at_s` alone do not
justify a physical recomputation. Such metadata-only updates are still recorded
and can wake the decision loop, but do not cancel an in-progress physical
forecast. A material change or health event can cancel it. Adding another
operational notice field requires updating this explicit schema and its audit.

A physical forecast may be reused if it matches the current observed state.
Current area permission, time, health and resource admission are still repeated;
a changed permission does not itself make the physical trajectory stale.
Compute missing or stale forecast evidence within the approved budget, and record
readiness or why comparison could not be completed before asking for a decision.

Keep the existing ceilings: 20 forecasts, 2 workers, 180 s per forecast,
600 s batch, 16 Jev calls, 2 DeepSeek calls, 35 s per model decision, 4 plan
revisions and 6000 s return delay. Optional computation protects the earliest
currently admissible opportunity, whether or not it has already been booked.
Subtract the 35 s model budget, 60 s deadline margin and one coast step from the
earlier of those opportunities, missing candidates' times and the overall return
deadline. The effective calculation cutoff is the earlier of that limit and the
batch start plus its at-most-600 s budget. Do not start optional computation with
less than 180 s available. Recheck the cutoff while waiting; failed or cancelled
optional work cannot silently discard an admissible booking or spend another
admissible opportunity's decision window.

The recorded calculation duration includes worker construction, exception
handling and shutdown, not only polling. Cleanup time advances the plant clock
within the effective cutoff. If real elapsed work cannot be represented without
crossing that cutoff, record an explicit clock-accounting overrun and reject the
forecast; do not hide the excess as a frozen simulated clock. This is a bounded
admission policy, not an operating-system guarantee that cleanup can never overrun.
The independent comparison/event audits bind preparation to its actual pending
operation; only the observed work interval adds event-response allowance, never
a free 600 s allowance for idle time.

If initial preparation fails, expires or cannot start and no candidate is
admissible, finish with `comparison_preparation_unavailable` and an explicit
unresolved disposition for pending events. Do not silently advance to the next
orbit as if a return decision had been made. When an admissible alternative
remains, the independent action checks and preapproved fallback still apply.
The last-moment commitment fallback is independently checked and cannot execute
revoked clearance or a newly inadmissible return.

Deep reasoning is bounded by the same 35 s decision deadline and 2-call ceiling.
The approved route is `prepared_uncertain_material_update_once_per_generation`:
only a fresh material update with at least two prepared, admissible candidates
and uncertain service estimates can trigger it, once per generation, with enough
remaining time. Its model is explicitly approved as `deepseek-flash`; this does
not change legacy cases or the repository-wide model default. Its declared payload uses `thinking.type=enabled`,
`reasoning_effort=high`, no `temperature`, and caps output at 4096 tokens. These
parameters follow the [official DeepSeek thinking-mode documentation](https://api-docs.deepseek.com/guides/thinking_mode/).
The one-attempt transport has a 30 s response deadline inside the actor's 35 s
window; this route does not make serial Jev-then-DeepSeek calls. Only invocation
metadata and a short operational decision summary are retained, never raw
`reasoning_content`. Neither a configured route nor an attempted request proves
model inference; report actual response evidence and elapsed time separately.

Invalid or truncated DeepSeek responses preserve bounded diagnostic metadata:
normalized finish reason, validation category, available integer token counts,
and whether a reasoning-content field was observed. Output-limit or malformed
JSON responses cannot authorize an action. Their diagnostic records do not contain
raw response text, raw reasoning, credentials or provider error bodies; a token
count or reasoning-field presence is not a valid decision receipt.

## Candidate material and independent quality audit

For every prepared candidate, expose observed/predicted contact and opportunity
times, the service-completion interval available at decision time, waiting
energy, upper temperature, propellant-loss allowance and remaining margins.
Resource projections use the candidate return/burn time, matching admission;
they are not additional post-contact resource estimates.

The audit objectives are estimated completion time, elapsed waiting, remaining
energy, upper temperature and reserve after loss. Minimize completion/waiting/
temperature; maximize energy/reserve. Conservative interval dominance requires
one alternative to be no worse across all declared objectives and strictly better
in at least one, accounting for the available uncertainty intervals. Ambiguous
service areas or missing comparable material make quality indeterminate.

An independently recomputed audit may flag a dominated choice for retrospective
review. It does not veto dispatch, encode a scenario's preferred action, use
unpublished future events, or become a score against Rules/fallback. Record
integrity may fail when evidence is forged; an admitted but weak decision remains
an honestly recorded decision. Completion is assessed as timely awareness,
material preparation/use, justified retention or revision, independent admission,
execution and later observed outcome.

## Response diagnostics and numerical citations

Comparison DeepSeek replies now carry `decision_basis.numeric_evidence` with
schema `missionos.m1_decision_numeric_evidence.v1`, the SHA-256 of the full
original decision request, and 1–16 references. Each reference contains
`candidate_id`, a JSON-pointer `field_path` relative to that candidate's
`comparison_material`, the exact unrounded numeric `value`, and `unit`.
At least one reference for every reported compared candidate is required by the
audit. The prompt supplies a compact field/unit allowlist; the numbers remain in
the original material. The response has a short operational summary, not a saved
reasoning transcript. Numerical assertions belong in the structured references.

The host limits response shape and retained text. The independent record audit
reconstructs candidate costs from prediction/notice records, then checks citations
against the original decision-time request, including its hash, candidate, field,
unit and exact number. Only matching references are emitted as
`verified_references`. Display rounding may happen after verification; a rounded
value is not silently accepted as an exact citation. The result is `valid`,
`invalid` or `not_present`; it appears under each comparison-quality decision.

This citation audit is retrospective and does not authorize or veto an action.
A numerical mismatch remains visible even when the separate action constraints
and recorded execution pass. `valid` means that the cited numbers match supplied
data, not that all prose, internal reasoning or physical predictions are true.
Older live responses have no structured citations and remain `not_present`;
do not retrofit references and claim that the model supplied them. This protocol
change alone is not evidence of a new provider invocation.

Jev response diagnostics retain finite categories for outer JSON/shape, model
identity, answer shape/type, choice, probability keys/values/sum and confidence.
Transport failures remain separate from malformed returned decisions. Raw error
bodies, provider responses, credentials and reasoning are not retained.

`model-diagnostics.json` and the HTML report count attempted and valid responses,
failures by status/category, and missing actor responses. The offline
`scripts/summarize_starship_models.py` accepts explicit repeated `--study` inputs
for cumulative accounting. Identical studies are deduplicated by canonical JSON
hash; the output binds each source-manifest hash. Counts cover only the supplied
studies, not every past MissionOS call or a statistical reliability estimate.
A missing actor response does not prove a provider call occurred. Historical
`invalid_response` without a saved category remains `unknown`. Fixture replies
and injected timeouts are never counted as hosted-model inference.

## Unchanged limits

The two 125 km study areas were calibrated from earlier predicted contacts.
There is no arbitrary-site search or cross-range target guidance. A half-step
forecast previously shifted contact by about 77 km, exceeding the 20 km footprint
padding; four sampled trajectories do not establish numerical convergence or a
bound on real-world uncertainty. Generic payload release, numerical contact,
area inclusion, service forecast and actual recovery completion remain different
facts. This campaign cannot establish SpaceX fidelity, real splashdown safety,
servicing completion or operational generalization from tuned synthetic cases.


## Review follow-up and offline payload preflight

The first review prompted the metadata-update, unbooked-opportunity protection,
full computation/cleanup accounting, explicit initial-failure disposition and
invalid-response diagnostic changes above. These are implementation fixes;
they do not alter the frozen five scenario conditions or replace their outcomes.

An offline encoding check used the two submitted, two-candidate requests from the
saved fixture flight. Only the local envelope mode/hash were changed to exercise
the live payload builders, with transport replaced by a capture stub. DeepSeek's
largest encoded request was 9,441 bytes and Jev's was 9,283 bytes, below the 16,384
byte cap by 6,943 and 7,101 bytes respectively. This was no provider call, model
inference or new flight. A separate synthetic regression fixture retains the
full comparison shape and four contact samples per candidate without importing
private artifacts; another check rejects an oversized payload before transport.
These measurements cover the observed request shapes, not an arbitrary-length
future notice. The existing cap remains enforced for every actual request.


## Observed HTTP results and source boundaries

The [sanitized campaign summary](../assets/starship-comparison/summary.json)
records two live HTTP flights executed at `58cde1f7`. Both passed record and case
verification, released 26 generic payloads, and reached the nominal eastern
contact at 4.608 m/s, 2.375° tilt and 62.394 t remaining propellant. Inflight human
commands were zero. These are numerical contact/area results, not physical
recovery or authenticated-human-identity evidence.

| Live case at `58cde1f7` | Jev attempts / valid responses | DeepSeek attempts / valid responses | Forecasts | Observed decision |
| --- | --- | --- | --- | --- |
| Normal | 4 / 4 | 0 / 0 | 4 | preserve the nominal plan; unchanged notice metadata caused no additional forecasting |
| Corrected service estimate | 2 / 1 | 1 / 1 | 8 | compare both admitted candidates, retain the nominal plan; later checked fallback preserves that booking after an invalid Jev reply |

In the corrected-estimate flight the update was observed at T+1350.2 s. MissionOS
reused the nominal forecast and prepared the missing next-orbit candidate by
T+1597.45 s. DeepSeek returned a valid decision in 17.203 s, inside the 35 s actor
deadline. Its receipt reports 3,526 reasoning tokens; raw reasoning was not saved
or independently verified. The short operational summary explicitly compares
both candidates: nominal retains 22 kWh and incurs no additional waiting loss;
next-orbit waits about 5,451 s, retains about 12.9 kWh and adds about 109 kg of
loss allowance. The estimated service-completion intervals overlap. DeepSeek
chose `keep_plan`, which passed the current independent checks. The retrospective
quality audit labels it `not_demonstrably_dominated`, not optimal or uniquely
correct.

The final Jev monitoring request in that flight returned `invalid_response`.
It did not authorize an action; checked fallback retained the existing admissible
booking. Do not report all model responses as valid or omit this failure because
the numerical contact passed. There was no prompt tuning or repeat-until-success
attempt after either recorded live result.

The three frozen fault conditions (`comparison_late`, `comparison_tool_failure`,
`comparison_timeout`) also pass the finite production-loop harness. That harness
uses fake plant, forecast and clock plus a fixture policy. It is not three full
6DOF flights, a real worker outage or live model evidence. Additional final-source
HTTP late-notice verification at `183ab41e` was documented in a historical private
review record; it is not provided here as publicly reproducible evidence. Full
6DOF execution of the other two faults is recorded below; the two live results
here retain their original source binding.

## Independent review disposition

Two read-only Claude review rounds used the user-designated session with Opus
5.5 and high effort. The second round reviewed the R1 corrections. The following
small changes at `183ab41e` were made after R2; they have not received a third
independent review:

- **R2-A — old failed preparation reused as a stop reason:** stopping now requires
  the unsuccessful preparation to have ended at the current state time. An older
  failure cannot later be reinterpreted as a new terminal failure after an
  otherwise valid candidate simply ages out.
- **R2-B — batch timeout diagnosis:** an effective batch cutoff now records
  `forecast_batch_budget_exceeded`, separately from cleanup/clock-accounting
  overrun. Measured cleanup and any accounting shortfall remain in the record;
  a clearer cause label does not suppress those facts.
- **R2-C — credential echo in the operational summary:** reject a summary
  containing either configured provider credential before retaining decision
  text. Invalid-response metadata remains bounded and does not copy the text.
- **R2-D — initial-failure recovery limit:** no bounded retry of initial forecast
  preparation is implemented. With no admissible candidate, that failure may
  terminate unresolved even if a later feasible opportunity might exist. This
  remains a limitation, not a safe-recovery claim.
- **R2-E — older runtime evidence:** the `07d0` real CLI fixture was validated
  against its own source. It does not establish final-source execution. Likewise,
  the two live HTTP flights above remain `58cde1f7` evidence and are not relabelled
  as `183ab41e` executions.

The frozen campaign configuration is unchanged. The review fixes, older CLI
record, two live HTTP flights and fault harness are separate evidence scopes;
none substitutes for an unperformed final-source live campaign.


## Failure-flight follow-up at `afce045a`

The [source-bound follow-up](../assets/starship-comparison/assurance-followup.json)
records two additional real HTTP Gateway → one-use approval → separate-process
6DOF → independent record/case verification runs. All 68 source-file hashes in
both flights match `afce045a`; the frozen campaign configuration is unchanged.

| Condition | Submitted forecasts / completed trials | Missing decisions | Observed result |
| --- | --- | --- | --- |
| Optional calculation canceled | 8 / 4 | 0 | record and case passed; preserve nominal booking; contact 4.608 m/s |
| Persistent missing responses after update | 8 / 8 | 2 | record and case passed; checked contingency preserves nominal booking; contact 4.608 m/s |

Both release 26 generic payloads, dispatch at T+2280.7 s and reach contact at
T+4929.856 s, tilt 2.375°, with 62.394 t remaining propellant. Human inflight
commands and hosted-model calls are zero. The first case injects cancellation
through the actual forecast pool/cleanup path; four submitted alternative trials
are canceled, not four successful predictions. The second deliberately suppresses
two decision replies; each wait advances the plant by 35.25 s, followed by the
independently checked reserved return. These are synthetic failure injections,
not observations of an actual cloud-provider outage.

```bash
python scripts/start_starship_gateway.py --fixture-planner --port 18847 \
  --state-dir output/starship-assurance-20261009/gateway-state
python scripts/smoke_starship_chat_gateway.py --port 18847 \
  --scenario sixdof_m1_comparison_tool_failure \
  --output-dir output/starship-assurance-20261009/tool-failure
python scripts/smoke_starship_chat_gateway.py --port 18847 \
  --scenario sixdof_m1_comparison_timeout \
  --output-dir output/starship-assurance-20261009/timeout
```

Both smoke commands exited 0. The Gateway was restarted on the frozen source
before the runs and stopped afterward; original evidence is retained. The
existing late-update flight remains bound to `183ab41e`, not this new source.

The cumulative receipt report covers the two earlier live flights plus these two
fixture flights: Jev 6 attempted / 5 valid / 1 invalid with unknown historical
cause; DeepSeek 1 attempted / 1 valid. No fixture response inflates model counts.
The structured citation protocol was checked with real loopback HTTP transport
and a provider stub, then independently audited. At this checkpoint it had **not** been rerun against
a hosted model; the older DeepSeek explanation remains without structured
citations. The later single live attempt is recorded separately below. Free-prose reasoning and operational recovery remain unverified.


## One live numerical-citation attempt at `9d3dfa30`

The [sanitized live-attempt record](../assets/starship-comparison/live-citations.json)
records one newly started HTTP Gateway flight on the unchanged frozen
`comparison_update` scenario. All 68 source hashes match the execution commit.
This is the existing development scenario, not held-out evaluation. No prompt,
output-token budget, authority, physical model or acceptance threshold was changed
for this attempt, and no retry was performed after its result.

**The flight passed; the live citation objective did not.** Jev returned two valid
replies from two attempts: initially `select_nominal`, then `keep_plan` after the
comparison had been prepared. DeepSeek was called once, with both candidate costs
available, but its response reached the approved 4,096-token output limit in
20.081 s. The receipt reports `finish_reason: length`, `validation_error:
output_limit` and 4,096 reasoning tokens. This is an attempted provider response,
not an accepted decision or verified inference receipt. Its decision was rejected;
no partial reasoning or numerical references were adopted. All three decision
audits therefore report `numeric_evidence.status: not_present`, never `valid`.

The checked contingency preserved the already admitted nominal booking at the
failed comparison. The later valid Jev `keep_plan` preserved revision 1, which was
actually dispatched at T+2280.7 s. Record and case verification both passed with
26 payloads released and contact at T+4929.856 s, speed 4.608 m/s, tilt 2.375°,
62.394 t remaining propellant, inside the eastern study area. Human inflight
commands were zero; the simulation worker held no provider credentials. The
case gate can accept the later executed Jev decision; it is separate from the
unsatisfied DeepSeek citation objective.

```bash
python scripts/start_starship_gateway.py --fixture-planner \
  --enable-live-mission-director --jev-secret jev-api-key --port 18849 \
  --state-dir output/starship-citations-live-20261009/gateway-state
python scripts/smoke_starship_chat_gateway.py --port 18849 \
  --scenario sixdof_m1_comparison_update \
  --output-dir output/starship-citations-live-20261009/live-update
```

The Gateway used the configured Secret Manager project; that private locator is
omitted. The smoke exited 0. The freshly started Gateway was stopped afterward,
with all records retained. The cumulative CLI now explicitly includes this study
alongside the earlier two live and two fixture fault studies: **Jev 8 attempts /
7 valid / 1 historical unknown failure; DeepSeek 2 attempts / 1 valid / 1 output
limit failure**. Those five studies are a small receipt-accounting set, not a
statistical reliability estimate. Future live reports must add their study to
this explicit list and preserve its source binding and failure categories.

The next protocol task is to examine the structured-output budget against the
thinking/output contract before another separately bounded live attempt. Do not
weaken citation checks, increase the budget silently, or report this contact as
successful numerical grounding. The earlier M1 control evidence remains valid;
this additional live grounding objective is still open.
