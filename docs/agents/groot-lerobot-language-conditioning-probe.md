# GR00T / LeRobot Language-Conditioning Probe

## Purpose

Use this diagnostic after policy-input delivery and native model-forward
evidence exist, but before interpreting a negative Repair cohort as a model
capability result. The probe asks whether changing only the task instruction
changes the native GR00T prediction at one frozen observation.

This is not a Repair attempt and must not be added to a Repair cohort.

## Fixed Design

The live route requires a verified diagnostic-only failure snapshot. It
regenerates one observation from the snapshot and chooses the failed predicate
as instruction A. The other moka-pot predicate is the contrast instruction B.

The sequence is exactly `A, A, B`. Every trial must have:

- the same `observation_sha256`
- the same non-negative `sampling_seed`
- an empty policy action queue before forwarding
- an exact packed-language match
- one observed native `predict_action_chunk` call
- a policy-request digest
- a full prediction digest
- a selected-action digest
- `simulator_action_applied: false`

The classifier fails closed when any field is absent, has the wrong type, or
does not match the fixed observation, seed, or sequence.

## Classification

`aa_control_not_reproduced`
: The two A calls differ in request, prediction, or selected action. Do not
  interpret the A/B comparison.

`ab_language_request_not_distinguished`
: The A/A control reproduces, but changing the instruction does not change the
  policy request.

`no_local_prediction_difference_observed`
: The A/A control reproduces and the policy request changes, but the full model
  prediction does not change.

`local_instruction_conditioning_observed`
: The A/A control reproduces, the A/B request differs, and the full model
  prediction differs.

The last status establishes only local instruction sensitivity at the frozen
observation and seed. It does not establish semantic comprehension, general
language following, successful Repair, controller ACK, simulator effect, or
physical execution.

## Authority Boundary

The CLI requires `--diagnostic-authorization-ref`. This is authorization to run
an inference-only diagnostic and incur its bounded compute cost. It is not a
Repair approval. Probe mode rejects `--operator-approval-ref` and
`--dispatch-state-path`, creates no approval or dispatch artifact, and never
passes the selected action to the environment.

## Fixture Runtime Verification

```bash
tmp_dir="$(mktemp -d /tmp/missionos-language-probe-fixture.XXXXXX)"
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR_FIXTURE=1 \
python scripts/run_groot_lerobot_same_world_repair.py \
  --runtime fixture \
  --checkpoint-path /tmp \
  --diagnostic-authorization-ref diagnostic:fixture \
  --output "$tmp_dir/probe.json" \
  --language-conditioning-probe \
  --repair-sampling-seed 1000
```

Expected fixture result: a reproduced A/A control and a differing A/B
prediction, with approval, dispatch, simulator action application, Semantic
Repair, and physical execution all false.

## Live Invocation

Live execution remains opt-in and requires the pinned GR00T N1.7 checkpoint,
base model, Cosmos snapshot, LeRobot revision, GPU/EGL environment, and a
verified `.npz` failure snapshot:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
python scripts/run_groot_lerobot_same_world_repair.py \
  --runtime live \
  --checkpoint-path /opt/model-cache/checkpoint \
  --restore-snapshot /path/to/failure-snapshot.npz \
  --diagnostic-authorization-ref diagnostic:operator-authorized \
  --output /path/to/probe.json \
  --language-conditioning-probe \
  --repair-sampling-seed 1000
```

Do not publish a live result until its `result_sha256`, file SHA-256, source
commit, bundle digest, snapshot digest, model revisions, and resource cleanup
have been verified. A capacity failure is environment status, not model
evidence.

## Verified Live Measurement (2026-08-21)

The publication-safe companion is
[`20260821-groot-n17-lerobot-language-conditioning-probe-publication.json`](evidence/20260821-groot-n17-lerobot-language-conditioning-probe-publication.json).
It records one `g2-standard-8` plus NVIDIA L4 run from commit `615dff3`:

- the A/A request, full prediction, and selected action reproduced
- the A/B request, full prediction, and selected action differed
- packed language matched exactly in all three forwards
- the model-forward count was exactly three
- no approval, dispatch, controller ACK, simulator action, predicate
  improvement, Semantic Repair, or physical execution was claimed
- the transient VM and boot disk were deleted and the persistent cache was
  left detached

The exact supported claim is local instruction sensitivity at one frozen
observation and seed. The measurement does not show that the change points in
the semantically correct object-relative direction.

## Semantic-Direction Follow-up

`--semantic-direction-probe` is a separate diagnostic clone. It restores the
same asymmetric `[true, false, true]` snapshot before each `A, A, B` trial and
applies exactly one 16-action chunk. A names the failed second-pot predicate;
B names the already-satisfied first-pot contrast. No Repair approval or
governed dispatch is created.

The preregistered local-direction status requires:

- exact A/A request, prediction, action-chunk, and terminal-state reproduction
- positive end-effector progress toward the failed second pot under A
- more A progress toward the failed pot than toward the protected first pot
- more progress toward the failed pot under A than under the B contrast
- no Contract-bound preservation violation during the 16 simulator steps

Because B names an already-satisfied predicate, the classifier does not require
B to approach the first pot. A positive result supports only local failed-target
direction alignment. It is not instruction comprehension, Repair capability,
task completion, Semantic Repair, controller ACK, governed dispatch, or
physical execution.
