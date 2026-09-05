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

The current record has no admitted recovery-training examples, measured
dataset size, selected trainable checkpoint, measured GPU-hour estimate, or
reviewed cost cap. A GPU must not be provisioned and training must not start
until all of those inputs exist and a separate human approval is recorded.

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

The current audit status is `blocked_no_training_manifest`, because zero
training examples exist. This is not a passed leakage audit.

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
