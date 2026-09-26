# Ship uncertainty gate: CPU experiment contract

`ship-delivery uncertainty-screen` compares six predeclared controls in the
[two-route CPU fixture](ship-dynamic-routes.md). It does not call a learned model,
dispatcher, PX4 or Gazebo. `recommend_detour` is a synthetic proposal, not human
approval, Rules authorization, physical dispatch, delivery, or deck recovery.
No production mission configuration is changed by running this command.

## Reproduction and immutable protocol

```sh
missionos ship-delivery uncertainty-screen --freeze protocol.json
missionos ship-delivery uncertainty-screen --protocol protocol.json --output evidence.json.gz
```

Run the freeze separately before scoring. Both commands reject overwrites;
scoring requires the exact supported hash-bound protocol and a separate `.gz`
output. Stdout contains a compact summary; full cases are compressed. The
receipt binds the three runtime source files by SHA-256. A local freeze time
establishes local provenance, not independent external preregistration.

The frozen comparison includes all 4,320 prior window states as regressions and
7,560 fresh states: 36 profile pairs × 10 deadlines × 3 noise levels × 7 futures.
Fresh motion parameters are disjoint from the earlier profiles. Futures either
continue or change acceleration at 2.5/5.5/8 seconds with magnitude 0.3/0.6 m/s².
The changed acceleration opposes current velocity and continues through zero,
so it can reverse motion. All failures remain in the reported denominator.

The rules use lessons from the prior comparison. They were fixed before scoring
the new panel; no threshold was tuned after the present results. Fresh states
share the same designed world structure. They are not independent flight trials
or a held-out learned-model evaluation.

## Online data, clocks, and uncertainty

Only `history`, `deadline_s`, and `observed_through_s` enter admission. History
has sixteen 4 Hz scalar position samples for each route, expressed relative to
the newest observation. Unknown fields, future frames, bad clocks, NaN, and a
sensor callback changing the deadline are rejected. Scenario IDs, true motion
profiles and latent change times are evaluator-only.

Every decision charges 0.5 seconds after its last observation. This is assigned
mission-clock time, not a measured CPU latency. Re-observation decisions occur
at 0.5, 1.5, 2.5, 3.5, 4.5 and 5.5 seconds while the existing six-second direct
approach continues. A switch at time `t` pays retreat `t - 0.5`, so departure on
the detour is `2*t - 0.5`. With no switch, arrival is unchanged. The hold control
instead departs at 2.5 seconds, paying a two-second delay even on fallback.

Receipt `switched` belongs to the inherited blocked-gate fallback procedure;
it does not describe the outer controller's approach-time change. That change
is recorded by `decisions[].recommend_detour`, `retreat_s`, `candidate` and
`departure_s`. A successful proposal still requires the shared crossing guard.

Planning noise is the same deterministic sinusoid on initial and subsequent
observations, with amplitudes 0/0.1/0.25 m on the fresh panel. Common crossing
guards still use exact current and previous synthetic range samples. Do not
present this as noisy perception safety or calibrated real sensor evaluation.

The gate fits constant acceleration (CA) to all frames. It retains direct/wait
if nominal prediction says that route fits the deadline. Otherwise it requires
both routes' CA residual RMS ≤ 0.1 m and a detour feasible with one second of
reserve under every admitted hypothesis:

- CA continues with the inherited ±4 m/s speed cap.
- Ten brake-to-rest alternatives begin braking after 0/2/4/6/8 seconds, with
  deceleration 0.3/0.6 m/s²; after stopping they remain stationary.
- Full-history constant velocity is additionally tested if its own RMS passes.

RMS and enumerated hypotheses are heuristic filters, not calibrated confidence
or coverage of all futures. In particular brake-to-rest does not cover reversal.
Small errors near discrete crossing windows can still change the route choice.

## Controls and acceptance

`baseline` is direct/wait. `raw_ca` preserves the previous six-candidate CA
selector, including blocked-gate fallback switches. `patient_ca` intervenes only
when nominal direct fails and nominal detour succeeds; it omits RMS, alternative
hypotheses and reserve. This separates removal of early fallback behavior from
the added uncertainty filter. `initial_gate`, `shadow_gate` and `hold_gate` apply
the full filter at departure, during approach, or after holding respectively.

`fixed_detour` is a separate comparator. The reported oracle knows the true
future and picks the better immediate direct/wait or detour/wait outcome. It is
not an online selector or evidence that hidden intent is learnable.

Frozen acceptance requires zero lost baseline successes in every regression and
fresh panel plus at least one fresh gain. `cpu_acceptance_passed` specifically
evaluates `shadow_gate`. It is **false** in this run: fresh gains 8, losses 3.
The separately predeclared `initial_gate` ablation satisfies the empirical rule:
fresh gains 8/losses 0; regression gains 8/losses 0. This is not a universal
preservation guarantee, model admission, or Step 2 completion. No policy is
installed in a live mission. See [the bounded report](../examples/ship-uncertainty-report.md).

## Verification

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_ship_uncertainty_gate.py \
  tests/contract/test_ship_dynamic_routes.py tests/contract/test_ship_wam_headroom.py \
  tests/contract/test_ship_urban_decision.py
```

Observed: 73 passed. The actual CLI freeze/scoring smoke exercises frozen input,
all six controls, causal sensor callbacks, charged retreat, guarded execution
and gzip receipts. It scored 11,880 conditions without model/cloud calls.
Tests include complete retreat costs, unchanged fallback time, harm from waiting,
future-input rejection, and equality of all 4,320 prior initial observations.

Private evidence retains source snapshots, all cases, protocol and file hashes,
positive witnesses and all three late-switch regressions. An independent audit
uses a clipped-velocity integral and safe tick sets to recompute 83,160 route
outcomes, 106,532 decision records and all paired summaries. It matches prior
baseline/raw-CA outcomes in all 4,320 regression states. Sensor-hash reconstruction
preserves producer floating-point order and is cross-checked against the integral.
Forecast selection is source-bound and contract-tested, not independently
reimplemented. Neither the audit nor the tests verify physical flight.
