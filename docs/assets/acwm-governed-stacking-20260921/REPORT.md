# Online neural ACWM in the MissionOS governed execution loop

**Date:** 21 September 2026. **Scope:** two known-case simulator integration runs.

## Result

MissionOS used an online neural ACWM forecast as evidence for actual DeepSeek
Assurance judgments, then applied bounded human preapproval and Rules before
issuing execution tickets. SmolVLA generated motor actions only after dispatch,
and the verifier measured the resulting tower before the next decision.

> ACWM predicts. DeepSeek proposes continue or stop. Humans pre-authorize the
> simulator bounds. Rules issue tickets. SmolVLA and the registered controller
> execute. The verifier measures the result.

| Known seed | Width group | Stable banked score | DeepSeek judgments | SmolVLA inference chunks | Motor steps |
| --- | --- | ---: | ---: | ---: | ---: |
| 68000 | Wide | 9 | 10 | 117 | 2,840 |
| 68001 | Narrow | 4 | 5 | 52 | 1,420 |
| **Total** | | **13** | **15** | **169** | **4,260** |

Both games ended by selecting bank and completing the same 14.2-second stability
hold. The observed maximum drops during bank were 6.73 mm and 6.35 mm, below the
30 mm collapse threshold. Both final outcomes were noncollapsed. There were no
technical failures in the completed runs.

This completes the governed connection for the neural ACWM, beyond the fixed
stopping gate in PR #110. It demonstrates the execution path; the two known cases
are integration regressions, not a new performance benchmark.

## Fixed models and available information

The checkpoint, visual readout, noise seed, SmolVLA checkpoint, and placement macro
were retained from the online experiment. No training or threshold adjustment was
performed. The model is the ACWM neural future-video generator; ExtraTrees is not
used in this execution path.

- ACWM digest: `473a14e0b305ad9537f969284ccf6c62fbe12b9fc01501e27f41ce1dd492ff8c`.
- Visual readout digest: `df01a3947a752abd4d7ba7d74aa48ac400ea9fabd2e90a3f6d5c27804dc083c5`.
- SmolVLA digest: `254ec40e3be4a44f62073be9cea999ed165ca533d26204beb7f1238890fd38b4`.
- Actual remote judge: `deepseek-flash`; its weights are inaccessible, so no model
  weight digest is asserted. API response identifiers and payload hashes are recorded.

Before approach, ACWM receives the current 240 × 240 RGB image, exact current
object poses and velocities, physical properties, robot state, count, and the
registered placement plan. It receives no future VLA action tape. Each of the
15 final-run predictions generated 37 frames spanning the next 14.2 seconds.
The frozen readout converted those frames into an uncalibrated collapse score.
GPU generation/readout latency was 1.31 seconds median and 1.42 seconds at p95
for these 15 calls. This excludes remote judgment, network, and simulator work.

The runtime used one NVIDIA L4, Torch 2.7.1+cu128, Transformers 5.5.4, LeRobot
0.6.1, and Python 3.12.14. MuJoCo ran in the existing local simulator container.
Simulation paused while awaiting prediction and judgment.

## What DeepSeek received and decided

DeepSeek received structured, admitted `model_inferred` forecast evidence, rather
than the generated video frames themselves. The saved API payloads contain the
actual forecast score, horizon, model binding, observation context, mission
objective, and policy bounds. Its answer reasons explicitly cite the predictions.

For example, the first wide-tower forecast scored **0.04949** over **14.2 seconds**.
DeepSeek cited that score and chose continuation. After nine successful placements,
the next forecast scored **0.85273**; DeepSeek selected bank and the verifier
confirmed nine stable blocks. The narrow-tower run selected bank before the fifth
placement at a forecast score of **0.83494**, retaining four blocks.

ACWM forecasts only continuation. It supplies no bank forecast and does not cover
the additional terminal hold. Those gaps are explicit in the prompt. Bank risk
and proxy point values are null; ExtraTrees' two-branch proxy calculation is not
used. Thus the observed safe bank outcomes are simulator measurements, not
ACWM bank predictions. Their forecast-comparison receipts correctly say
`incomparable`, while the verifier still records their measured terminal scores.

All 15 actual DeepSeek choices matched the retained threshold-policy reference in
these cases. The LLM was genuinely invoked and selected the dispatched options;
these results do not show an advantage from replacing the fixed stopping policy
with an LLM.

## Authority and causal evidence

For every final-run decision, the records link:

1. Current observation and pinned ACWM/VLA contracts.
2. GPU generation invocation and resulting forecast.
3. Assurance admission, actual DeepSeek request/response, and proposal.
4. Approved bounded policy and decision-bound execution ticket.
5. Subsequent real SmolVLA inference identifiers and applied motor-action digest.
6. Measured object drops, score, and verifier receipt.

Prediction completion preceded the DeepSeek request. Its response preceded the
selected simulator transition and all associated VLA inferences. At dispatch,
the current observation matched the one used for prediction exactly.

At the first step of each game, altered approval binding, altered state revision,
and stale observation time were rejected: **six rejected dispatch probes**, with
no motor calls from those probes. Predictions and LLM proposals themselves did
not create dispatch authority. Human approval was bounded preauthorization of
these simulator games, not a separate human approval at every placement.

The numerical states and executed motor arrays matched the corresponding PR #110
ACWM trajectories exactly at all 15 common decision points. The scores also match
those two earlier cases. This is useful regression evidence for the adapter, not
an independent replication or a performance improvement.

The simulator macro stages the grasp and uses SmolVLA for lowering and release.
This result concerns stacking in that staged environment, not autonomous picking
or physical robot execution. The service is opt-in; it is not installed as a
persistent Gateway service.

## Integration repairs and preserved limitations

Two development attempts preceded the completed runs and remain in the local
audit archive:

- The first attempt aborted on its first placement when the GPU SmolVLA process
  lacked `num2words`. ACWM generation, the API judgment, and ticket issuance had
  succeeded; no terminal game score was claimed. Installing the dependency and
  requiring both inference servers to be ready repaired the environment.
- The next attempt completed one placement in each game, then Rules rejected both
  second-step dispatches for changed state digests. Camera rendering refreshes
  MuJoCo derived kinematics; the new runner had captured numerical state before
  that refresh. Capturing the image first and numerical state afterward restored
  the PR #110 observation order. State-digest checks were retained, not relaxed.

The final runs used that corrected observation order. A regression test protects
it. Model weights, readout, judgment instruction, and selected seeds were not
changed in response to those technical failures.

One inherited reporting field was also corrected after the runs: the top-level
`llm_invoked` flag had retained the earlier prediction-only value `false`. The
preserved raw records are unchanged. Actual invocation is established by
`proposal.model_inference_invoked = true`, the saved DeepSeek API payloads and
responses, and their hashes. Current code synchronizes the legacy flag with the
proposal; this metadata-only change was checked through the HTTP boundary tests,
without repeating GPU games.

## Public evidence and verification

[run.json](run.json) contains individual decisions, measured outcomes, tickets,
GPU generation metadata, real VLA inference records, and verifier receipts.
The `llm/` directory contains the actual API payloads and responses, with no
credentials. [protocol.json](protocol.json) records the bounded plan. The
manifest binds the public files.

```sh
python docs/assets/acwm-governed-stacking-20260921/verify_report.py
python -m pytest docs/assets/acwm-governed-stacking-20260921/test_verify_report.py -q
```

The verifier recomputes totals and checks model/response/digest links, forecast
arrival in the actual API payload, decision mapping, tickets, measured score
consistency, causal timestamps, and rejected-dispatch records. Mutation tests
check that contradictory records fail verification.

Full simulator snapshots, model weights, generated-frame arrays, and raw motor
arrays remain in the local audit archive; the public package includes their
identifiers, hashes, and derived records. The verifier audits this published
chain; it does not rerun neural inference or independently reproduce MuJoCo
trajectories. Input equality and earlier-run prefix comparisons are saved audit
results, not recomputable from the reduced public package alone.

The temporary GPU VM and its boot disk were deleted after hash-verified evidence
transfer. No continuously running ACWM or VLA inference server remains. Operational
use still requires explicitly starting those backends and authorizing a mission.
