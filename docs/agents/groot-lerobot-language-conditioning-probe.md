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
