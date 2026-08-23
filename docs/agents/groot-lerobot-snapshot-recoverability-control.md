# GR00T LeRobot snapshot recoverability control

## Purpose

This control asks whether one restored partial-success simulator state was
recoverable through the same 7D LIBERO action interface and within the same
720-action ceiling used by the GR00T diagnostic trials. It separates a policy
failure from the alternative explanation that the saved state, predicate,
action interface, or horizon made recovery impossible.

It is a separate measurement from the native Repair cohort and the language
conditioning diagnostics. It does not revise their registered outcomes.

## Restored state

- snapshot SHA-256:
  `a674f7ed37ffd0511824bdbe5a7146fa4d0fd6bccd0a5a80cdf1e87d8124e111`
- source predicates: `[true, false, true]`
- target: `on(moka_pot_2, flat_stove_1_cook_region)`
- preserve: pot 1 remains on the stove and the stove remains on
- maximum action budget per diagnostic trial: `720`

## GR00T instruction control

Ten diagnostic clones compared five paired sampling seeds under two
instructions:

- target-specific ordinal language: `put the second moka pot on the stove`
- benchmark-standard task language: `put both moka pots on the stove`

Each instruction completed five trials of at most 45 chunks of 16 actions.
Neither instruction produced target improvement (`0/5` and `0/5`). All ten
trials ended at `[true, false, true]`, and no Contract-bound preserve violation
was observed.

These were diagnostic clones. Each trial recorded its own proposal, approval,
dispatch, dispatch receipt, model inference, and simulator execution. No
controller ACK was observed. Because the execution state was a restored
diagnostic clone, these records are not eligible to establish same-world
Semantic Repair.

## Privileged recoverability control

A deterministic privileged waypoint planner then restored the same snapshot
and used the same 7D simulator action interface. The target predicate first
became true after action 61. Twenty zero-action settling steps followed, for 81
actions total. The terminal vector remained `[true, true, true]`.

The failed pot moved about `4.10 mm`; protected pot displacement was effectively
zero; and no preserve violation was observed. The control invoked no model
inference.

This establishes, for this snapshot only, that the restored state, predicate,
7D action interface, and 720-action horizon were sufficient for recovery. It
rejects “the saved state was unrecoverable” as the explanation for these ten
diagnostic outcomes.

## Claim boundary

The bounded conclusion is:

> The state was recoverable. In these diagnostic clones, GR00T N1.7 did not
> generate the corrective trajectory that recovered it.

The control does not establish a general GR00T recovery failure rate. The
planner used privileged simulator state, so it does not establish autonomous
recovery, an observation-grounded fallback, task completion by GR00T, Semantic
Repair, controller ACK, real-world safety, or physical execution. The ten
diagnostic clone dispatch records remain separate from the privileged control,
which created no approval or dispatch.

The publication-safe companion is
[`20260822-groot-n17-lerobot-snapshot-recoverability-publication.json`](evidence/20260822-groot-n17-lerobot-snapshot-recoverability-publication.json).
Raw traces and simulator artifacts remain local.
