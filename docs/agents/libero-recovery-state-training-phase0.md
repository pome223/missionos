# LIBERO recovery-state training Phase 0

## Decision

Paid training is **NO-GO** at this checkpoint. This is a readiness decision,
not evidence that recovery-state training will or will not work.

## Phase 1 capture status (2026-09-05)

There is now bounded positive evidence for data generation, while paid training
remains **NO-GO**. A privileged scripted oracle recovered one non-evaluation
condition (`episode_init_state_index=0`, `environment_seed=101`) in 52 applied
actions, preserved the protected predicates, and held the complete predicate
vector for 20 steps.

The run also captured all 52 aligned pre-action samples required before dataset
conversion:

- 256 x 256 `agentview` RGB;
- 256 x 256 wrist RGB;
- 8-D end-effector axis-angle plus gripper state;
- the actual 7-D action applied at that step;
- 20 Hz timestamps and the existing post-action predicate/preservation trace.

The compact evidence record is
[`evidence/20260905-libero-recovery-transition-capture.json`](evidence/20260905-libero-recovery-transition-capture.json).
The raw archive is digest-bound but intentionally not published in the source
repository. Its status is
`raw_transition_capture_complete_conversion_and_admission_pending`: it has not
yet been converted into GR00T LeRobot v2, admitted as a training example, used
for training, or evaluated by model inference.

The capture layout follows NVIDIA's current
[LIBERO modality contract](https://github.com/NVIDIA/Isaac-GR00T/blob/main/examples/LIBERO/modality.json)
and [GR00T LeRobot v2 preparation requirements](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md).
The next gate is deterministic MP4/Parquet conversion plus validation against
the official LIBERO schema. No GPU should be provisioned before a converted
multi-example manifest passes the leakage audit and receives separate cost
approval.

The first raw capture has now passed that structural conversion gate. The
converter produced two deterministic AV1 videos at 256 x 256 and 20 Hz (52
frames each), one 52-row Parquet episode with fixed-size 8-D state and 7-D
action columns, and the required LIBERO task, episode, modality, and dataset
metadata. A second conversion produced identical video, Parquet, task,
episode, and modality digests. See
[`evidence/20260905-libero-recovery-lerobot-v2-conversion.json`](evidence/20260905-libero-recovery-lerobot-v2-conversion.json).

This closes conversion for one candidate only. It does not close dataset
admission: a multi-example cohort, generated statistics, exact holdout
exclusions, and the Phase 0 leakage audit are still required before any paid
training review.

## Minimal training-candidate cohort (2026-09-05)

The preregistered diagonal cohort `(init, seed) = (0,101), (1,102), (2,103),
(3,104)` was run at the 3 cm negative-x condition. All four privileged-oracle
candidates recovered the predicates, preserved the protected object, and
completed the 20-step hold. Their trajectory lengths were 52, 52, 51, and 51
actions, for 206 aligned frames total. All four captures were converted to the
same LeRobot v2 contract.

The digest-bound candidate manifest is
[`evidence/20260905-libero-recovery-training-candidate-manifest.json`](evidence/20260905-libero-recovery-training-candidate-manifest.json).
All four transition contracts validate and have distinct source-state and
fixture identities. Three preregistered evaluation conditions `(init, seed) =
(4,0), (12,0), (15,0)` were then materialized separately. Their exact source-
state, fixture, and applied-action identities are fixed in
[`evidence/20260905-libero-recovery-evaluation-holdout-manifest.json`](evidence/20260905-libero-recovery-evaluation-holdout-manifest.json).
The holdout manifest explicitly records `used_for_training=false`; holdout
images, states, and actions were not joined to the training candidates.

The exact audit against those three holdouts plus all identifiers available in
the three earlier evaluation references found no collision. Therefore
`leakage_audit.passed=true` and the four candidates are admitted to the bounded
future recovery-training set. This is dataset admission only. It is not an
authorization to provision a GPU, start paid training, or claim learned-model
recovery.

The current set has four admitted recovery-training examples and 367,839 bytes
of serialized LeRobot v2 conversion artifacts. It still has no selected
trainable checkpoint, measured GPU-hour estimate, or reviewed hard cost cap. A
GPU must not be provisioned and training must not start until those inputs,
automatic teardown, and a separate human approval are recorded.

## Training compute preflight (2026-09-05)

The next read-only checkpoint and compute review is recorded in
[`evidence/20260905-libero-recovery-training-preflight.json`](evidence/20260905-libero-recovery-training-preflight.json).
It selects the revision-pinned `nvidia/GR00T-N1.7-3B` only as a research and
evaluation candidate. The checkpoint's current license limits the work and its
derivatives to non-commercial research or evaluation, while the upstream
Isaac-GR00T software repository is Apache-2.0. No derivative model weights are
authorized for publication.

The checkpoint configuration names the auto-gated
`nvidia/Cosmos-Reason2-2B` backbone. Current access to that dependency has not
been verified after deletion of the earlier temporary model cache. This is an
access blocker, not evidence that the model or training procedure fails.

NVIDIA's current hardware guide requires one GPU with at least 40 GB VRAM for
fine-tuning and reports that default projector-plus-diffusion-head tuning stays
below approximately 35 GB peak VRAM. The available L4 quota provides only 24 GB
per GPU and is therefore not an eligible training target. The minimum matching
Google Cloud shape is an A100 40 GB `a2-highgpu-1g`, but the observed
`us-central1` A100 quota is zero.

The cost-control proposal is a 5,000 JPY experiment cap, with a pre-launch
estimate below 3,500 JPY, one GPU maximum, a 60-minute runtime limit, no restart,
termination action `DELETE`, boot-disk auto-delete, no persistent model cache,
and mandatory post-run absence checks. A billing budget alert is monitoring
only; it is not treated as a hard stop. This proposal has not yet been reviewed
or approved.

Accordingly paid training and GPU provisioning remain **NO-GO**. Before any
quota request or billable launch, the gated-backbone access, research-only
license suitability, exact current price, and hard cap require review. If those
pass, the first billable operation would be a resumable 200-step throughput
calibration—not the matched experiment—and it would require separate approval.

Validate this boundary with:

```bash
python scripts/check_libero_recovery_training_preflight.py \
  docs/agents/evidence/20260905-libero-recovery-training-preflight.json
```

The machine-readable decision is
[`evidence/20260905-libero-recovery-training-phase0.json`](evidence/20260905-libero-recovery-training-phase0.json).
Validate it through the production CLI boundary with:

```bash
python scripts/check_libero_recovery_training_phase0.py \
  docs/agents/evidence/20260905-libero-recovery-training-phase0.json
```

Expected result: `status=valid`, `decision=NO_GO`,
`paid_training_authorized=false`, and `gpu_provision_authorized=false`.

## Recovery-transition data contract

Every candidate example uses `missionos.libero_recovery_transition.v1` and
binds the source state, split, actual source predicates, protected-object
reference poses, observation/proprioception schemas, corrective and applied
actions, per-step predicate and preservation traces, stable hold, and generator
provenance by digest.

`recovery_demonstration` requires both an observed corrective transition and a
satisfied stable hold. A failure rollout without a corrective transition is
stored as `failure_rollout`; it cannot be relabeled as a recovery
demonstration. Privileged planners must set `privileged_state_used=true` and
must not be presented as observation-only skills.

## Leakage audit

The published GR00T cohort, VLA-0 supervisory fixture, and registered-skill
same-world fixture are reserved evaluation evidence. Their source state and
fixture identities, when materialized into a future dataset manifest, are
excluded from training. The validator rejects:

- an evaluation source-state or fixture digest in training;
- a record whose `split_id` is `evaluation`;
- duplicate transition or source-state identities;
- malformed provenance or content digests;
- a failure rollout relabeled as a recovery demonstration.

The original Phase 0 decision record remains an immutable **NO-GO** checkpoint.
The subsequent candidate manifest now contains the passed exact audit and four
admitted examples; it does not retroactively authorize paid training.

## Preregistered matched comparison

The minimum comparison has two cells:

1. `nominal_only`
2. `nominal_plus_recovery`

Model, optimizer, compute, seeds, observation representation, and action
representation remain matched. Task, fixture, and seed cohorts remain
separate. Evaluation uses the existing five-axis sequence:

```text
action activity
-> corrective alignment
-> predicate recovery
-> preservation
-> 20-step stable hold
```

All five axes must pass in one held-out scope. First predicate conjunction is
not stable Repair.

The first pilot is fixed to LIBERO-10 task 8. Training-transition generation
may use init-state indices `0, 1, 2, 3`, environment seeds `101-104`,
disturbances of `1, 3, and 5 cm`, and four planar directions. Evaluation
reserves init-state indices `4, 12, 15`, environment seed `0`, and the `3 cm`
condition. Both cells use model sampling seeds `1000, 1001, 1002`, at most 128
policy actions, and the same 20-step verifier hold, for a maximum of 148
simulator actions. Exact evaluation fixtures must be materialized and added to
the exclusion manifest before a future `GO` decision.

These values preregister the comparison but do not claim that all candidate
training transitions will be admitted. Only traces that satisfy the recovery-
demonstration contract enter the recovery cell.

## Requirements before another GO review

- generate a public-safe manifest of admitted training transitions;
- pass the exact state/fixture leakage audit against held-out evaluation data;
- select a legally usable trainable checkpoint and pin all revisions;
- measure serialized dataset size and estimate minimum matched GPU-hours;
- write and review a currency, region, pricing timestamp, and hard cost cap;
- define automatic teardown for VM, boot disk, model cache, and temporary
  artifacts;
- obtain a separate human approval for paid training.

Even a future Phase 0 `GO` means only that the experiment is ready for an
approval decision. It does not itself authorize provisioning or training.

## Claim boundary

This Phase 0 performs no training, model inference, simulator execution,
dispatch, or physical execution. It establishes no Repair capability, general
recovery rate, controller ACK, or real-world safety result.
