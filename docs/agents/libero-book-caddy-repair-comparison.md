# LIBERO book-to-caddy Repair comparison

This document records a second-object diagnostic Repair fixture for the public
Cosmos Policy and VLA-0 comparison. It is a simulator experiment contract, not
evidence of a live MissionOS Repair loop or physical execution.

## Task and fixture

The task is LIBERO-10 task 5, init state 0, environment seed 0:

`STUDY_SCENE1_pick_up_the_book_and_place_it_in_the_back_compartment_of_the_caddy`

The original instruction is `pick up the book and place it in the back
compartment of the caddy`. Its verifier goal is the single predicate
`In(black_book_1, desk_caddy_1_back_contain_region)`.

The fixture starts from the terminal state of successful demonstration 0. It
translates `black_book_1` by 3 cm along simulator x and then applies 60
stationary settle actions. The settled book position is 5.314 cm from the
successful source position because the book also falls and settles. The caddy
and the protected mug do not move.

Admission requires all of the following:

- the actual terminal predicate is `[false]`;
- the failure remains false for the final 20 construction steps;
- target drift over that window is at most 1 mm;
- the settled displacement is between 3 and 12 cm;
- both 256 by 256 policy cameras change by at least 256 pixels;
- the snapshot, simulator state, fixture material, and report are digest-bound.

The admitted snapshot SHA-256 is
`f0c2d1244ac80d28dbe90c5a2714166f29ccb3182bd15bacb63b2a35133d1ac5`.
The fixture-material SHA-256 is
`b85dfd86db1282a920b32c1a54315b60a086f3a7c72906e4cc28fb663db1c9c7`.

## Positive control

A privileged scripted controller restores the exact snapshot and uses the same
raw 7D simulator action interface as the policies. It may inspect simulator
object state, so it is a diagnostic recoverability control only.

The control must be repeated three times. Each run must reach the actual
predicate, retain it for 20 stationary steps, and keep both the caddy and the
protected mug within the preregistered 5 mm preservation limit. The controller
must stop its corrective sequence at the first observed predicate success
before starting the stability hold; actions after success cannot be used to
inflate the policy horizon.

All three fixed-condition runs were identical: first contact after action 9,
first predicate success after action 13, and 20 complete stationary hold steps,
for 33 total actions. Neither protected object moved at reported simulator
precision. The canonical per-run result SHA-256 is
`49177ac15a740800fcacbe3f4710370991a609cb23cb9999a44d884481a0707b`.

## Policy protocol

Each policy uses its pinned public source and checkpoint, the original task
instruction, no additional training, the official LIBERO `original` 7D action
space, and actual predicates as the sole success authority.

The policy protocol is:

1. run the ordinary task from init state 0 with a 520-action ceiling;
2. stop if nominal predicate success is not observed;
3. restore the digest-bound diagnostic snapshot;
4. apply at most 128 policy-selected actions;
5. record initial, minimum, minimum-step, and terminal EEF-to-target distance;
6. record contact, target displacement, gripper changes, and preservation;
7. if the predicate becomes true, apply 20 stationary hold actions and report
   stable completion separately from predicate entry.

Cosmos future predictions and actual observations are stored in separate
trees. A generated future never establishes an observed effect or completion.
VLA-0 uses the upstream evaluator's default fp32 inference condition, one new
prediction per simulator step, and eight-prediction temporal ensembling.

## Observed results

The privileged positive control passed 3/3 fixed-condition replays. It first
contacted the book after action 9, first reached the actual predicate after
action 13, and retained the predicate for all 20 stationary hold steps. Each
run used 33 actions in total.

The pinned Cosmos Policy checkpoint passed the ordinary task from init state 0
after 166 actions. On the admitted displaced snapshot, it applied all 128
actions without book contact or measurable book displacement. EEF-to-book
distance changed from 18.14 cm to a minimum of 14.65 cm after action 52, then
ended at 20.67 cm. The actual predicate remained `[false]`. Thus this bounded
trial observed a modest approach followed by retreat, not predicate recovery.

The pinned VLA-0 runner did not pass the preregistered nominal-control gate in
this condition: after 520 actions, the ordinary-task predicate remained
`[false]`. The displaced-snapshot Repair stage was therefore not run. This is
not evidence of a VLA-0 Repair failure; Repair competence was unmeasured because
the comparison protocol stopped at its nominal gate.

The machine-readable evidence record is
[libero-book-caddy-repair-comparison-20260829.json](evidence/libero-book-caddy-repair-comparison-20260829.json).

## Claim boundary

This comparison can distinguish behavior on one checkpoint, task, init state,
seed, and admitted fixture. It cannot establish a general model-family Repair
rate, a causal WAM-versus-VLA architecture effect, same-world MissionOS Repair,
controller acknowledgement, physical execution, or real-world safety.

The fixture is created by direct simulator-state manipulation and every policy
attempt runs in a fresh diagnostic clone. Proposal, approval, dispatch,
simulator effects, predicate verification, controller ACK, and physical
execution remain separate facts.
