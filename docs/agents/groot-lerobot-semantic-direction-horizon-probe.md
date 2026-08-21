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
