# GR00T LeRobot three-chunk semantic-direction probe

## Purpose

This is a separately preregistered diagnostic follow-up to the completed
one-chunk semantic-direction observation. It does not revise or reinterpret
that result. It asks whether a longer, three-chunk closed-loop trajectory shows
local motion aligned with the failed target named by the instruction.

## Frozen design

- restore the same asymmetric snapshot with source predicates
  `[true, false, true]` before every trial
- run `A, A, B` with sampling seed `1000`
- A: `put the second moka pot on the stove`
- B: `put the first moka pot on the stove`
- run exactly three model forwards and three 16-action chunks per trial
- begin each trial with the same frozen first policy observation and an empty
  action queue
- apply actions only inside the diagnostic simulator clone
- create no approval, governed dispatch, or controller ACK

The first A/A policy request, prediction, and 16-action chunk must match
exactly. This is the causal control. After the first chunk, each simulator clone
is an independent closed-loop trajectory. Later observations, predictions,
actions, and terminal simulator states are recorded but are not required to be
bit-identical.

## Frozen positive classifier

The status may be
`local_three_chunk_failed_target_direction_alignment_observed` only when all
of the following are true:

1. the first A/A request, prediction, and action chunk reproduce exactly;
2. each A trial ends closer to the failed second pot than it began;
3. in each A trial, progress toward the failed second pot is greater than
   progress toward the protected first pot;
4. each A trial's progress toward the failed second pot is greater than the B
   trial's progress toward that same second pot; and
5. no Contract-bound preserve predicate is violated during any of the 144
   simulator actions.

If the exact first-chunk A/A control fails, the status is
`aa_initial_chunk_control_not_reproduced`. Otherwise, failure of any positive
criterion yields `local_three_chunk_direction_alignment_not_observed`.

No numerical terminal-state tolerance is selected or used.

## Claim boundary

A positive result supports only local semantic-direction alignment at one
restored observation and seed. It does not establish general instruction
comprehension, Repair capability, task completion, Semantic Repair, controller
ACK, governed dispatch, real-world safety, or physical execution. This probe is
not a Repair attempt and does not change the published native cohort result.

## Verified measurement (2026-08-22)

The publication-safe companion is
[`20260822-groot-n17-lerobot-semantic-direction-horizon-probe-publication.json`](evidence/20260822-groot-n17-lerobot-semantic-direction-horizon-probe-publication.json).
The live result was
`local_three_chunk_direction_alignment_not_observed`:

- the first A/A policy request, full prediction, and 16-action chunk reproduced;
- both A trials completed all three closed-loop chunks;
- failed-second-pot progress was `-0.07404997105318845 m` and
  `-0.0763733205535726 m` under A;
- the B contrast's progress toward that same failed second pot was
  `-0.07342729718890362 m`;
- neither A trial had positive failed-target progress or exceeded the B
  contrast's progress toward that target;
- no preserve predicate was violated across 144 simulator actions; and
- exactly nine model forwards and 144 simulator actions were observed.

Therefore the frozen positive classifier was not met. This result narrows the
local observation from instruction sensitivity to a negative three-chunk
semantic-direction diagnostic. It does not establish general semantic
misunderstanding, Repair incapability, task completion, Semantic Repair,
controller ACK, governed dispatch, real-world safety, or physical execution.
It does not alter the native Repair cohort `0/5`.

The raw simulator logs remain local. The checked-in companion contains only
reviewed digests, bounded metrics, revisions, cleanup state, and claim
limitations.
