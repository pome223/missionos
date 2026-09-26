# Aerial WAM observation-support audit and transport pilot

This refinement follows the [absolute-capability pilot](../assets/aerial-wam-capability-20260926/README.md).
It does not require superiority over Rules, depth or an ideal planner.

## Development diagnosis

Reconstruct the pose-conditioned projections from the retained RGB-D and camera
poses, without using declared building geometry. Match them to the saved model
projection PNGs before interpreting the audit. Track actual projected validity,
not nonblack color: observed black surfaces remain valid. Apply the pinned model's
4:3 crop and bilinear resize. Report native coverage, model-grid observation
weight, fully supported pixels, and forecast MSE on those fully supported pixels.
A missing supported region yields `null`, not a perfect zero error.

Coverage is evidence availability, not traversability or forecast accuracy.
ANWM may predict an unseen region; this diagnostic does not declare such a
prediction false, authorize a route, or replace WAM with a geometry selector.
The preceding cohort is development evidence. Do not choose score weights to
turn its expected route names into a claimed navigation result. The existing
model ranking is retained in this transport pilot; selection improvement remains
unestablished until a separate scoring change is validated.

## Freeze before new inference

- Run `gap`, `climb`, `detour` in order, one fresh simulator each. Known scenes,
  not held out. Preserve every attempt; at most one infrastructure retry per
  scene before prepared model input exists. Never replace model outputs.
- Change only the host transport to explicit `single_ssh_tar_v1`. One SSH session
  uploads exactly `request.json` and `assets.npz`, checks the published runtime
  hash, runs the same model CLI, and retrieves exactly the candidate PNGs and
  result JSON. Reject unexpected members, links, paths, duplicates or missing
  files before writing returned assets. No credentials or approval data transfer.
- The GPU script, checkpoint, seed 42, 250 diffusion steps, 16 genuine history
  frames, 5 m pose previews and existing goal-image ranking remain unchanged.
  Do not trade prediction quality for fewer denoising steps in this pilot.
- Retain independent constraints and the 180 wall-second execution freshness
  ceiling. Require goal within 0.3 m with 1 sim-second dwell, observed landing /
  disarm, zero building-contact notifications plus at least 0.25 m observed
  enclosing-envelope clearance, and route limits of 60 m / 120 sim seconds.
- Additional engineering target: last observation to selection and dispatch at
  most **120 wall seconds in all three cases**. This is a latency target, separate
  from the unchanged 180-second dispatch safety ceiling. A miss remains a miss.
- Record total observation age, host preparation, transport round trip, selection
  validation, model load and model forecast times. The round trip includes upload,
  process startup, runtime identity checks, inference and result download; do not
  label its residual as network time without measuring those components.
- Reconstruct observation support on the new forecasts. Report requested maneuvers
  and raw model proposals separately from constrained arrival. No new scoring,
  training, VLA, Jev, Gateway integration or physical flight claim.

## Budget and cleanup

Retained task cap: USD 10. Prior conservative cumulative estimate: USD 5.6697101,
not invoice-reconciled. Reserve at most USD 1.50 for this pilot: one L4 with a
45-minute automatic-delete ceiling and auto-deleting boot disk. Delete owned
resources after retrieving evidence and independently confirm their absence.
Wait at most ten minutes for shared quota; do not touch another task's resources.
Retain all infrastructure failures separately from model/flight attempts.

## Commands

```sh
python scripts/audit_urban_wam_candidates.py --input-dir "$RUN/input" \
  --result "$RUN/forecast/result.json" --output "$AUDIT"
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_wam_trial.py \
  --phase run --scene "$SCENE" --selector anwm --assets-dir "$ASSETS" \
  --gpu-config "$GPU_CONFIG" --output-dir "$RUN" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
python scripts/verify_urban_wam_trial.py --root "$RUN" --output "$VERIFICATION"
```

The GPU configuration opts into `"transport": "single_ssh_tar_v1"`. Existing
configurations retain `scp_v1`. Historic raw receipts remain unchanged. New
`remote_call_wall_seconds` covers the complete transport boundary; its explicit
`transport_timing` breakdown is authoritative for the new scope. Older receipts
started that clock after upload and cannot be directly compared as identical
intervals. Observation-to-dispatch age retains its original definition.
