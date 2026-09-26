# Aerial WAM capability pilot, 2026-09-26

This protocol is fixed before collecting new model outputs. It applies the
[absolute-capability policy](aerial-wam-capability-evaluation.md); no rule,
projection or depth selector is a competing treatment or an acceptance threshold.
Historical runs are not assigned new pass/fail labels.

## Operating envelope and attempts

- Run the existing `gap`, `climb`, and `detour` scenes, in that order, once each
  with `--selector anwm`. These are known engineering scenes, not held-out tests.
- Use the released, pinned ANWM checkpoint, seed 42, 250 diffusion steps, sixteen
  genuine 4 Hz simulator RGB-D history frames, and the existing five-metre
  pose-conditioned route previews. No fine-tuning or scoring changes.
- The aircraft remains in stationary OFFBOARD hover during inference. The scene
  is static. The declared goal image and authored candidate route templates are
  supplied; arbitrary route generation and dynamic avoidance are outside scope.
- Candidate ranking uses predicted-image goal MSE. Full scene geometry is used
  only by the independent constraint gate and verifier. The unfiltered model
  choice is retained before the gate, including any rejected choice.
- Use only isolated PX4/Gazebo simulation. No physical aircraft or VLA calls.
  This uses the existing ANWM experiment runner, not the depth-only Gateway
  stop/reobserve adapter. It does not establish WAM integration into that adapter.
- Permit one infrastructure retry per scene only if no model selection was
  obtained. Preserve every attempt, including capture/setup failure. Never retry
  a rejected, slow, or unfavorable model choice to replace its result.

## Absolute endpoints fixed before execution

These are engineering pilot bounds, not a deployment reliability certificate.
The small static world allows a bounded stop-and-think operation; the 180-second
limit is the existing stale-input ceiling, not an acceptable moving-obstacle
response time. Distance and duration budgets are absolute mission limits for
this small scene, not ratios to an ideal planner.

| Endpoint | Bound and evidence |
| --- | --- |
| Governed arrival | Goal within 0.3 m for 1 simulation second, followed by observed landing and disarm, in all 3 cases |
| Safety | Zero observed building contacts; swept sampled trajectory has at least 0.25 m clearance beyond the 0.6 m enclosing airframe sphere |
| Responsiveness | Last observation to selection **and dispatch** at most 180 wall seconds; hover remains within 0.25 m and 0.1 rad |
| Mission duration | Dispatch to observed goal at most 120 simulation seconds |
| Travel budget | Dispatch to observed goal at most 60 m |
| Model invocation | Fresh, hash-bound ANWM forecasts actually consumed for dispatch in every completed case; no fixture or geometric substitution |
| Requested maneuver | `gap`: forward through the passage; `climb`: climb route; `detour`: either lateral route |

Report governed arrival/bounds, requested maneuver, and unconstrained model
proposal admissibility separately. A detour that reaches the gap-scene goal is
an arrival but does not demonstrate passage through the gap. A Rules veto followed
by the sole admissible climb route does not demonstrate learned climb selection.
Full maneuver/proposal coverage would require 3/3; partial results stay partial.

There is no baseline-superiority endpoint. Do not label this pilot a general
WAM failure, or stop the research line, because a bound is missed. Identify the
specific mismatch: scoring, proposal, sensor/pose alignment, latency, or execution.

The existing previews are pose-conditioned and not verified time forecasts.
Record this unresolved dimension explicitly; image similarity alone cannot certify
collision prediction or future-state accuracy. Setup effort includes the supplied
scene, goal camera, candidate templates, calibration and remote model installation.

## Budget, resources and records

- Retained user authorization: total task ceiling USD 10, GPU use allowed, and
  transfer of the published `aerial_anwm_runtime.py` plus newly captured RGB-D,
  pose and goal inputs to a short-lived Google Cloud VM.
- Prior conservative task estimate: USD 4.61, not invoice-reconciled. Reserve at
  most USD 2.00 for this pilot, leaving at least USD 3.39 of the original ceiling.
- One L4 VM, maximum lifetime 90 minutes with automatic instance deletion and
  auto-deleting boot disk; delete both immediately after evidence retrieval.
- Never reuse, stop or delete another task's running GPU or simulator. Wait for
  the shared GPU quota to become available before provisioning this pilot.
- Upload only the published runtime and fresh `request.json` / `assets.npz`.
  Jev credentials, approval records, HMAC keys and other repository code stay local.
- Retain the frozen protocol digest, code revision, attempt ledger, actual model
  calls, measured timings, route receipt, raw verifier output and cleanup evidence.
  Preserve all failed attempts and distinguish unattempted cases from failures.

## Invocation

```sh
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_wam_trial.py \
  --phase run --scene "$SCENE" --selector anwm \
  --assets-dir "$ASSETS" --gpu-config "$GPU_CONFIG" --output-dir "$RUN" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
python scripts/verify_urban_wam_trial.py --root "$RUN" \
  --output "$RUN/verification.json"
```

The report must lead with the measured absolute outcomes and the model's actual
role. Any stored projection or shortest-route diagnostics in existing raw receipts
are not used for the pilot verdict or displayed as a contest.
