# Native VLA + WAM delivery and the 1 km perception repair

After the successful targeted 1 km flight, the unchanged final implementation completed all three separately predeclared 100 m cases: 3 verified native VLA + WAM integrations, 3 deliveries and 3 stable recoveries. All four conditions are covered on the same implementation across two distinct cohorts; this is not one four-flight cohort.

The final VLA stage is a short urban-entry level inspection with vertical proposals restricted to about ±0.204 m. This is not qualification of unrestricted three-dimensional VLA control.

Location clarification: the waypoint called "urban entry" is 30 m before the coastline. Models ran on a cloud GPU, with VLA warmup before takeoff. These flights do not verify urban-only startup/inference/shutdown or repeated decisions after landfall. The [new urban-loop controller work](../ship-urban-loop-report.md) is separate evidence.

The original cohort completed the three 100 m cases and stopped before delivery in its 1 km extension. That failure is retained. Results from different implementations are not pooled into a success rate.

[Recorded trajectories and images across all seven retained cohorts](replay/replay.html)

## Outcomes by implementation

| Implementation | Planned | Attempted | Integration verified | Stop / scope |
| --- | ---: | ---: | ---: | --- |
| Original | 4 | 4 | 3 | 1 km stopped at ambiguous target reading |
| Association repair | 4 | 3 | 2 | ACK ordering failed; 1 km unattempted |
| ACK repair | 4 | 4 | 3 | Malformed VLA output at 1 km |
| Action syntax | 4 | 1 | 0 | Descent veto; 1 km unattempted |
| Level inspection | 4 | 3 | 2 | Numeric permit reproduction failed; 1 km unattempted |
| Canonical numeric values; 1 km only | 1 | 1 | 1 | One predeclared targeted follow-up |
| Same final implementation; 100 m follow-up | 3 | 3 | 3 | Three separately predeclared conditions |

The first six cohorts are separated by implementation changes. The seventh uses the same source as the sixth for a separately predeclared 100 m follow-up. Successes are not pooled and unattempted conditions are not passes. The third flights of the association-only and level-inspection cohorts verified delivery/recovery but failed the combined integration certificate.

## Measured targeted result

| Case | Offshore | VLA displacement | WAM position error | WAM exchange | Recorded elapsed | Delivery / recovery |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| static_center | 1000 m | 2.905 m | 0.659 m | 50.146 s | 685.140 s | verified |

Position error is the maximum difference between the two decoded predictions and independent simulator obstacle coordinates. Exchange time is measured host wall time; recorded elapsed runs from worker start to the final observation.

## Three 100 m cases on the unchanged final implementation

Source revision is the same as the preceding 1 km flight: `f9c21384916ba0aa0778da5b7527d598303879bb`. Three planned, 3 attempted, 3 integrations verified. Acceptance criteria, models and generation-time action space are unchanged. Unattempted or failed cases are not counted as passes.

| Case | VLA displacement | WAM position error | WAM exchange | Recorded elapsed | Integration | Delivery | Recovery |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| static_center | 2.831 m | 0.630 m | 49.120 s | 456.387 s | verified | verified | verified |
| static_near | 4.827 m | 0.205 m | 45.569 s | 458.044 s | verified | verified | verified |
| static_clear | 4.894 m | 2.333 m | 44.873 s | 411.160 s | verified | verified | verified |

[Follow-up outcomes and raw hashes](compact-followup-evidence.json), [predeclared protocol](compact-followup-protocol.json).

## Cause and repair

The old reader globally ranked warm-colored regions. In the original 1 km right-5 m candidate, the generated left-edge building became orange as well as the central pillar. The pillar occupied columns 104–147 (44 pixels); the extra region occupied 0–28 (29 pixels). The second region exceeded half the largest width, so the reader rejected the image. The 46.86-second model exchange was within the unchanged 75-second limit.

![Original rejected right-5 m prediction](images/right_5m-prediction.png)

The repaired reader projects the last pre-inference RGBD target cross-section into each candidate camera and requires an unambiguous matching region in the generated view. It reads the reported position from the generated image. Missing targets, competing nearby regions, clipping, incomplete depth and large shape/position differences still reject. Future observations and simulator obstacle coordinates do not choose the target.

The 5 m position-error, 75 s exchange and 2 s dispatch-freshness bounds are unchanged. The eight original predictions are development regressions; the subsequent frozen native flights are separate validation. Superiority over Rules is not required or claimed.

The new 1 km right-5 m prediction still contained a central pillar at columns 103–147 and a left-edge region at 0–36. An offline diagnosis on these identical saved bytes reproduced the legacy ambiguity rejection; the repaired association read the central target during the flight and reproduced that interval offline. This is a decoder diagnosis, not a flight rerun with the legacy reader. [Same-image decoder diagnosis](repaired-parser-diagnosis.json).

## Intermediate ACK race

The association-only intermediate cohort attempted three cases. All three verified delivery and recovery, but the third failed combined integration: an ACK arrived 1.779 ms after the timestamp of the sample selected by the executor. This cohort retains 2/3 verified integrations, with its planned 1 km extension unattempted.

The executor now records the ACK receive time and requires a state sample at or after it. The independent verifier was not relaxed. A model-free PX4/Gazebo round trip passed before the subsequent frozen native cohort reported above.

[Intermediate evidence](association-only-evidence.json) and [ACK timing diagnosis](ack-race-diagnosis.json).

## VLA output format

The third cohort, with association and ACK fixes, passed the three 100 m cases. Its 1 km case returned `故4924 49</s>` instead of three numeric bins and stopped before WAM inference. That cohort remains 3/4; its failed response is not reinterpreted.

The fourth cohort opted into `aerovla_action_grammar.v1`: a generation-time next-token constraint for three numeric bins or LAND. Every native bin from 0 through 98 and LAND remains available; the model chooses the values. There is no output rewriting, substituted number or retry. Terminal, motion, freshness and collision constraints remain independent. Syntactic validity does not establish semantic motion quality.

[Third-cohort outcomes](association-ack-evidence.json) and [original malformed response and diagnosis](malformed-action-diagnosis.json).

## Mission-phase action space

The fourth cohort stopped in its first 100 m case: the syntactically valid `55 84 49</s>` proposed a 3.57 m descent and was rejected by the independent altitude envelope. WAM was not invoked. It retains zero completions from one attempted case out of four planned.

The fifth and later cohorts explicitly limit urban-entry inspection to vertical bins 47–51 (about ±0.204 m), while forward/yaw bins 0–98, LAND and terminal bins remain available. No returned value is rewritten. This changes the allowed model action space and is a separate implementation cohort; altitude, clearance, latency, delivery and recovery verification are not relaxed.

[Format-only outcomes](format-only-evidence.json) and [descent proposal and rejection](phase-action-diagnosis.json). The final policy is `aerovla_inspection_grammar.v1`.

## Reproducible execution permits

The fifth cohort verified delivery and recovery in three 100 m flights, but the third failed exact permit reproduction. Linux-produced and macOS-recomputed coordinates differed by at most 1.11×10⁻¹⁶ m; the original Linux environment reproduced the original permit exactly. That cohort remains 2/3 integrations, with 1 km unattempted.

The producer now canonicalizes derived position/time values to nine decimal places and heading to twelve. The exact-equality verifier and safety limits remain unchanged. A newly issued offline permit on the archived input was byte-identical in an actual Linux container and macOS. The historical failed receipt is not reclassified.

The canonical-number repair first passed one predeclared targeted 1 km flight. Following explicit approval of an $11 cumulative limit, three fresh 100 m cases were predeclared on that unchanged source. Model and level-inspection action space remain the same as the fifth cohort.

[Level-inspection outcomes](level-inspection-evidence.json), [protocol](level-inspection-protocol.json), and [numeric diagnosis and cross-platform comparison](precision-diagnosis.json).

## Additional offline check of the three 100 m cases

The final implementation regenerated permits from the fifth cohort’s retained inputs for the center, near and clear 100 m cases. All six results (prestream and execution for each case) were byte-identical on macOS and an actual Linux container with networking disabled. This used no additional GPU, model inference or flight.

This checks numerical reproducibility only. Historical failures remain unchanged. The fresh three-case native-flight results are reported separately above. [Offline results and hashes](compact-offline-readiness.json).

## Evidence and scope

[Original outcomes](evidence.json), [original diagnosis](original-diagnosis.json), [repaired results and raw hashes](repaired-evidence.json), [frozen repair protocol](repaired-protocol.json), [runtime commands](../../agents/ship-native-integration.md), [association contract](../../agents/ship-anwm-static.md).

This is one aircraft, a stationary ship and obstacle, and a 200 m urban segment. Native VLA movement and native WAM candidate views participate sequentially. Model proposal, prior approval, Rules constraints, Executor motion and Verifier outcomes remain separate.

The setup relies on mapped planes, colored objects, calibration markers and Gazebo ego poses. Unassociated colored regions are not certified harmless. General perception, moving-obstacle prediction, real strong winds, moving decks, ten-aircraft operation and physical delivery remain unverified.

The public bundle contains reviewed summaries, synthetic images, observed trajectories and hashes. The raw archive is private; hashes alone do not permit full independent reproduction. The replay uses recorded positions and camera images selected at intervals of at least five seconds.

Estimated cost of the 100 m follow-up: $0.94; cumulative estimate: $8.43 of the authorized $11 limit. The task-owned GPU VM and disk were deleted. These are estimates, not a confirmed invoice.

[日本語レポート](REPORT-ja.md)
