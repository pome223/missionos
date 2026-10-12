# Starship operational trials

This contract extends the [flight-control contract](starship-mission-control-contract.md).
The objective is to test MissionOS doing mission-control work with Jev and,
where the decision deadline permits, DeepSeek: detect an event, obtain evidence,
compare feasible plans, decide within approved authority, execute and verify.
Agreement with scenario Rules or fallbacks is not an AI-value score. Those paths
remain constraint enforcement, contingency coverage and regression checks.

## Primary-source precedents

These sources establish that a kind of problem occurred. They do not establish
Starship incident rates, sensor accuracy, recovery duration or operating limits.

| Source ID / event or report date | Recorded event and operational response | Transfer boundary |
| --- | --- | --- |
| `nasa_crew8_weather_20241016` / 2024-10-16 | [NASA: Crew-8 return weather](https://www.nasa.gov/blogs/commercialcrew/2024/10/16/nasa-spacex-monitor-weather-for-crew-8-return/): multiple splashdown zones remained unsuitable; managers continued monitoring and scheduled another forecast briefing. | Dragon return operations; supports changing recovery availability, not Starship weather thresholds. |
| `nasa_cygnus_array_20221108` / 2022-11-08 | [NASA: Cygnus NG-18 rendezvous](https://www.nasa.gov/blogs/commercialresupply/2022/11/08/cygnus-prepares-for-rendezvous-with-space-station-2/): one of two solar arrays deployed. Teams assessed sufficient power, stopped further deployment attempts and planned additional inspections. | Cygnus hardware; supports degraded-resource replanning, not a claim that this Starship model has those arrays. |
| `spacex_flight3_roll_20240314` / 2024-03-14; report 2024-05-24 | [SpaceX: On the Path to Rapid Reusability](https://www.spacex.com/updates/reusability): attitude control degraded during coast; a preplanned automatic command skipped the Raptor relight. Roll-control valve clogging was the most likely cause. | Direct Starship precedent; automatic protection is not evidence of AI control. Hardware characteristics remain vehicle-specific. |
| `nasa_orion_comms_20221123` / 2022-11-23 | [NASA: Orion communications restored](https://www.nasa.gov/blogs/artemis/2022/11/23/artemis-i-flight-day-eight-update-unexpected-loss-of-communication-with-orion-is-restored/): communications were unexpectedly lost for 47 minutes during an Orion–DSN link reconfiguration; the cause was still under investigation. Ground-side reconfiguration restored the link and onboard records were to be retrieved. Orion remained healthy. | Orion/DSN, not Starship. The first implementation borrows only delayed operations-message delivery, not spacecraft telemetry failure. |
| `nasa_crew6_hook_20230303` / 2023-03-03 | [NASA: Crew-6 docking](https://www.nasa.gov/blogs/spacestation/2023/03/03/spacex-crew-6-mission-docks-to-stations-harmony-module/): a faulty hook sensor delayed docking. NASA and SpaceX verified the hook configuration, and SpaceX developed a software override that allowed docking to continue. | Dragon docking; supports independent evidence for a suspected sensor fault, not unrestricted permission to bypass interlocks. |
| `nasa_crs1_engine_20121008` / 2012-10-08 UTC | [NASA/TM-2017-219497](https://ntrs.nasa.gov/api/citations/20170009844/downloads/20170009844.pdf?attachment=true): a Falcon 9 first-stage engine stopped around T+80 s. Longer burns preserved Dragon's primary mission, while the secondary payload did not reach its required orbit. | Falcon 9/Merlin, not Starship/Raptor. Supports mission reprioritization after changed propulsion capability, not transfer of engine parameters. |

[NASA's Demo-2 departure and splashdown criteria (July 2020)](https://www.nasa.gov/sites/default/files/atoms/files/ccp_splashdown.pdf)
also document primary/alternate sites, repeated weather decisions and wave-off.
These are Demo-2-specific practices and limits; they are not present-day Starship
requirements. Real events above were handled by existing automation and people.
Assigning analogous supervisory work to MissionOS is this project's experiment.

## First implementation: frozen operational-message catalog

The catalog contains eight preregistered, opaque case identifiers:

| Harness split | IDs | Profiles in order |
| --- | --- | --- |
| Development | `op_trial_01` through `op_trial_04` | nominal, clearance interruption, service forecast correction, delivery outage |
| Evaluation | `op_trial_05` through `op_trial_08` | nominal, clearance interruption, service forecast correction, delivery outage |

The allocation is stratified: one profile per split. It does not estimate an
incident probability. Named seeded draws freeze onset, message delay, reported
wind/waves and service-delay intervals before any model is called. All numeric
distributions are synthetic engineering assumptions, not NASA measurements or
Starship failure frequencies. `sha256_named_draw_v1` makes draws reproducible
without dependence on process-global random state or Python's random version.

Only two precedent IDs support this first implementation:
`nasa_crew8_weather_20241016` for recovery-operation updates and
`nasa_orion_comms_20221123` for the message-delivery analogy.

- **Nominal:** a routine publication preserves clearance and service estimates.
  Successful monitoring may leave the approved plan unchanged.
- **Clearance interruption:** an explicit delivered bulletin withdraws eastern
  clearance; a later delivered bulletin can reinstate it. A reopening estimate
  alone is never authority to use the area.
- **Service forecast correction:** delivered revisions change uncertain
  post-contact service-delay intervals while clearance stays unchanged.
  Predictions are not observations of completed recovery.
- **Delivery outage:** the preceding bulletin can expire before the next one
  arrives. Issuance, delivery and expiry are distinct. This does not inject a
  spacecraft telemetry outage or alter vehicle-link health.

Reported weather is informational, not an extra aerodynamic forcing model.
The operational effect must come from delivered notices changing the existing
candidate-clearance or service-estimate inputs and triggering reconsideration.
Adding display metrics without affecting those production boundaries is not
completion. No weather number silently creates or restores operating clearance.

## Information and authority boundaries

The harness retains the complete seed, split, profile, random draws and future
publication schedule for preregistration and audit, bound by a manifest hash.
These latent fields are not policy inputs. A policy sees only publications
already delivered at the current simulation time, their reported measurements,
issuance/expiry times, current observations and permitted tools/actions.

Do not disclose a fault label, future recovery time, later bulletin, seed or
evaluation split through scenario names, prompts, tool results or observations.
Audit retention is not permission to copy the manifest into a model request.
Issuing a bulletin does not make it observable before its delivery time.
Simulation time, resource consumption and approved deadlines continue while a
model or calculation is pending. Critical deadlines retain the approved fallback;
an operations message cannot enlarge authority or override independent checks.

## Verification and execution discipline

Freeze generator, manifests/hashes, development/evaluation split, attempt counts
and model/forecast/time budgets before model evaluation. Preserve every attempt,
including no-action, invalid response, timeout, unresolved return and failure.
Do not replace a failed attempt by retrying until success. Any later campaign
gets a new frozen protocol and retains the previous evidence.

Verify deterministic regeneration and the absence of future metadata in policy
input. Exercise delivered-notice timing, expiry, revision handling, event wakeup,
candidate feasibility and subsequent observation at the production boundary.
Record real provider calls separately from fixtures and fixture planners; record
record-verification, mission outcome and physical execution as separate facts.
Assess detection delay, evidence freshness/use, approved action, deadline margin,
resource effect and observed outcome. Compare candidates using decision-time
information, never the harness's future schedule as an answer key.

## Current live DeepSeek count policy

At the user's explicit request after the recorded trial, newly issued live
`comparison_orbital_update` and `op_trial_*` return grants have
`maximum_llm_calls: null`: no per-flight DeepSeek call-count cap. This propagates
through the current M1 orbital path and the nested M2/M3 grants; their combined
`maximum_model_calls` is also null so it cannot recreate a hidden DeepSeek cap.
`null` is explicit JSON, not a missing field, infinity or a large finite number.
The approval message displays `DeepSeek回数上限なし（判断期限は適用）`.

Provider reservations, host attempt ledgers and independent verifiers count every
attempt without using the DeepSeek count to reject another eligible comparison.
Jev/ascent/booster limits, new-evidence eligibility, no duplicate request or
comparison generation, current-state checks, decision deadlines, provider timeout,
forecast limits, output/disk limits and the M3 2400-second wall limit remain.
This does not force a model call when there is no new evidence or time to act.
Fixture grants and legacy non-orbital scenarios retain their previous semantics.

Historical finite grants are not rewritten or expanded. Record validation accepts
only an exact current or historical canonical grant, including nested prefix
bindings; new execution requires a newly issued/current approved grant. The earlier
`803e0ad3` trial below still used one DeepSeek call and its second correction still
used fallback. It is not evidence of a post-change unlimited-count flight.

### E2E / Runtime Verification — count policy

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python -m pytest -q -p no:cacheprovider \
  tests/contract/test_starship_deepseek_call_policy_host.py::test_three_reasoning_decisions_cross_real_loopback_and_durable_host_ledger
```

Observed three distinct eligible DeepSeek decisions through actual loopback HTTP,
production deadline transport/decoder and the durable host-attempt ledger, even
with Jev slots exhausted. All three test responses were adopted. Existing
120-second provider timeouts and 16,384-token response limits were retained.
The responses were synthetic; no hosted API or numerical flight ran. Related
regression suite: 1,080 passed, two dependency deprecation warnings. Separate
checks retain finite old caps, reject fabricated `budget_exhausted` under null,
and preserve identity, deadline, exact-count and Jev enforcement.

## Uncapped repeated reconsideration — `627e9647`

One new `sixdof_m3_op_trial_03` flight used a newly approved grant with explicit
null DeepSeek and aggregate call-count limits. Run
`1cc8a9c309544565bd37ff4dd4c711c6` used source
`627e9647f306e1ee8961c9ba1e16200a4b231849`. The 96 captured runtime source hashes
match that commit. The notification manifest is byte-identical to the earlier
capped trial. No runtime code, prompt or scenario was changed during the flight;
this was one attempt, with no retry. It is the same development condition, not a
held-out robustness evaluation.

| Evidence | Observed result |
| --- | --- |
| First correction | Delivered T+1387.955 s, detected T+1388.200 s; comparison requested T+1653.200 s after numerical preparation |
| First DeepSeek decision | `keep_plan`, adopted; 13 input references resolved; later observation T+1681.950 s, before T+1803.200 s deadline |
| Second correction | Delivered T+1895.310 s, detected T+1895.450 s; immediate new DeepSeek request with `used_slots: 1`, `maximum_slots: null` |
| Second DeepSeek decision | `keep_plan`, adopted; 12 input references resolved; later observation T+1919.450 s, before T+2045.450 s deadline |
| Provider accounting | Jev 7/7 valid and adopted; DeepSeek 2/2 valid and adopted. Neither invalid responses nor fallbacks occurred |
| Context | Both comparisons used the latest delivered notice and two admissible candidates; no stale action adopted |
| Execution | 26 released / 0 retained; same-state handoff; nominal return dispatched T+2280.700 s; eastern model-area contact at 4.608259 m/s and 62.3943 t fuel |
| Booster | 1.883584 m/s contact; all eight registered model water-entry checks passed |
| Verification | Gateway `verified`; record/case accepted; independent full saved-JSON verification reproduced the complete verdict |
| Limits | Zero in-flight human commands, eight forecasts; M2 runtime wall time 593.835 s; full M3 remains incomplete |

The second correction now received actual model reconsideration instead of
count-budget fallback. Keeping an already booked plan after comparing current
evidence is an adopted decision. Reference resolution validates cited values,
not every explanatory sentence, internal reasoning or the shared physical model.
This trial does not establish unbounded API throughput or reliability from n=1.
The previous capped trial and its exhausted-budget record remain unchanged.

[New report](../assets/starship-m23-20261011/op-trial-03-uncapped-report.html),
[flight/provider audit](../assets/starship-m23-20261011/op-trial-03-uncapped-audit.json),
[event audit](../assets/starship-m23-20261011/op-trial-03-uncapped-event-audit.json),
[full recheck](../assets/starship-m23-20261011/op-trial-03-uncapped-recheck.json).

### E2E / Runtime Verification — uncapped live flight

The completed one-attempt invocation in the qualified environment was:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/start_starship_gateway.py \
  --state-dir output/starship-operational-20261011/op-trial-03-uncapped-single/gateway \
  --port 18869 --fixture-planner --enable-live-mission-director \
  --project "$GOOGLE_CLOUD_PROJECT"
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/smoke_starship_chat_gateway.py \
  --port 18869 --scenario sixdof_m3_op_trial_03 \
  --output-dir output/starship-operational-20261011/op-trial-03-uncapped-single/http
```

The project was resolved from the authorized local gcloud configuration without
printing it. Boundary: real HTTP plan/approval/run, credential-free separate
6DOF worker, real providers, repeated notification events, independent action
checks, later observations and signed artifact verification. Smoke exit 0.
Scenario selection alone used a fixture planner. The trial-owned Gateway was
stopped; raw evidence remains local. Reviewed portable numeric summaries are
linked above. No second flight or model call was used for post-flight auditing.
Operator identity was not authenticated; these are model contact/water-entry
criteria, not recovered hardware or real SpaceX certification.

## Recorded development trial — `803e0ad3`

One frozen `op_trial_03` flight ran through real Gateway HTTP, preflight approval,
a separate credential-free 6DOF worker and real Jev/DeepSeek providers. No retry
was performed. Source hashes for all 95 files match the frozen commit; independent
revalidation of saved JSON reproduced the complete verdict. The final related
suite passed 1,023 tests, including real loopback boundaries (two dependency
warnings). Catalog and boundary tests do not count as additional flights.

| Evidence | Observed result |
| --- | --- |
| First correction delivery / detection | T+1387.955 / 1388.200 s; 0.245 s latency |
| Comparison preparation | Detection to decision request: 261.750 s, including the missing candidate's numerical predictions |
| DeepSeek comparison | T+1649.950 s: `keep_plan`; both candidates were admissible, 13 numerical references resolved, observed later at T+1670.200 s |
| Second correction delivery / detection | T+1895.310 / 1895.450 s; 0.140 s latency |
| Second immediate reconsideration | DeepSeek's one-call cap was exhausted; no provider invocation, independently checked booking fallback |
| Later Jev confirmation | T+2220.700 s: `keep_plan`; later observation T+2221.200 s |
| Actual provider counts | Jev 7/7 valid and adopted; DeepSeek 1/1 valid and adopted |
| Execution | 26 released, same-state return handoff; Ship 4.608259 m/s contact in the eastern model area; booster 1.883584 m/s, 8/8 model water-entry checks |
| Scope | Zero in-flight human commands; one development flight; full M3 incomplete |

The second update exposes a genuine coverage limit: detection works, but an
immediate AI comparison does not recur once the approved deep-reasoning budget
is spent. The later Jev check is separate evidence, not a retroactive replacement.
No budget or prompt was changed during or after this trial to obtain another run.

The existing M3 `case_accepted` combines contact and requested provider evidence.
It still requires an adopted DeepSeek response even for nominal trials; this
legacy demonstration flag must not be used to call appropriate non-intervention
a normal-flight failure. The newly registered IDs do not inherit the older
`comparison_orbital_update` scenario-specific completion assertion. Accordingly,
the post-update comparison above is established from its actual request, two
admissible candidates, adopted response, reference audit and dispatched booking,
not from the case flag alone. Normal/control and evaluation flights are unrun.

[Flight report](../assets/starship-m23-20261011/op-trial-03-report.html),
[provider/flight audit](../assets/starship-m23-20261011/op-trial-03-audit.json),
[event audit](../assets/starship-m23-20261011/op-trial-03-event-audit.json) and
[independent recheck](../assets/starship-m23-20261011/op-trial-03-recheck.json).

### E2E / Runtime Verification

In the qualified Python environment, the already completed one-attempt commands
were (the authorized Secret Manager project is supplied locally):

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/start_starship_gateway.py \
  --state-dir output/starship-operational-20261011/op-trial-03-single/gateway \
  --port 18868 --fixture-planner --enable-live-mission-director \
  --project "$GOOGLE_CLOUD_PROJECT"
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src \
python scripts/smoke_starship_chat_gateway.py \
  --port 18868 --scenario sixdof_m3_op_trial_03 \
  --output-dir output/starship-operational-20261011/op-trial-03-single/http
```

Observed HTTP smoke exit 0, Gateway `verified`, record/case accepted, eight
predictions and 559.306 s worker wall time. Before execution, manifest, authority,
source and caps were frozen: Jev 27, DeepSeek 1, predictions 20, wall 2400 s,
output 1.2 GB, minimum free reserve 1 GiB. The trial-owned Gateway was stopped;
raw evidence stays local, portable reviewed summaries are retained above. These
commands describe this trial and must not overwrite it or imply repeat approval.
The simulator's contact and water-entry criteria are not real-vehicle recovery,
seawater survivability or certification. No new Claude review is claimed.

## Physical-fault backlog

Partial deployment, false mechanism indication, degraded RCS and engine loss
are research-backed backlog items, not faults implemented by this catalog.
They require causal changes to actuator/sensor/resource physics, observer truth
separation, applicable authority and return qualification, plus independent
verification. Do not implement them as an answer-shaped boolean or claim that
randomizing a notice reproduced the corresponding hardware failure.
