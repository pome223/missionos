# Manipulation VLA Repair progress: GR00T N1.7 and VLA-0

MissionOS is testing a narrow question that ordinary benchmark success does not
answer:

> After a manipulation task has partially succeeded and the world no longer
> looks like a normal episode start, can the policy produce a new trajectory
> that repairs the remaining failed predicate without breaking what is already
> correct?

This is why MissionOS keeps policy inference, human approval, bounded dispatch,
simulator effects, verifier predicates, controller ACK, and physical execution
as separate facts.

## Progress so far

| Policy | Control | Repair observation | Boundary |
| ------ | ------- | ------------------ | -------- |
| GR00T N1.7 | The same saved diagnostic snapshot was recoverable by a deterministic planner through the same 7D interface in 81 actions | GR00T did not recover it in 10 diagnostic clones across two instructions; the native same-world cohort also observed target improvement in 0/5 Repair loops | The saved-state displacement was only 4.1 mm, so this is a useful control but a threshold-adjacent case |
| VLA-0 | The pinned official runner completed the nominal task in 1/1; a privileged oracle then recovered the exact fixture through the same original 7D interface in 517/520 actions | After the second pot was moved approximately 22.7 cm, one governed 520-action Repair observation ended at the same `[true, false, true]` predicate vector | This is one bounded failure on a demonstrated-recoverable scripted fixture, not a general Repair rate |

The GR00T and VLA-0 measurements are not a leaderboard. They use different
policies and different failure states. Together they show why nominal task
execution, verifier-backed recoverability, and model-generated Repair must be
measured separately.

See the [GR00T recoverability control](groot-snapshot-recoverability.md) for the
earlier saved-state result. The rest of this report describes the clearer VLA-0
fixture.

## The VLA-0 test

The task was the LIBERO-10 scene
`KITCHEN_SCENE8_put_both_moka_pots_on_the_stove`, with the original benchmark
instruction `put both moka pots on the stove` and init-state index 15.

The test had three deliberately separate parts:

1. **Nominal positive control.** The pinned upstream VLA-0 runner completed the
   unmodified task in 1/1 run within 520 actions. This weakens the explanations
   that the checkpoint was completely broken or could not solve this task at
   all. It does not establish a nominal success rate.
2. **Recoverability control.** A privileged scripted oracle received the exact
   same fixture through the pinned VLA-0 LIBERO environment's `original` 7D
   action interface and the same 520-action budget. It first reached
   `[true, true, true]` after 497 actions and maintained that vector for 20
   settling actions, for 517 actions total. The already correct pot and stove
   predicates remained satisfied. This proves that the fixture was recoverable
   under the tested interface and budget; it is not a VLA or MissionOS Repair.
3. **Governed Repair.** A test-only fixture moved the second pot approximately
   22.7 cm, leaving it approximately 7.0 cm outside the stove region. The first
   pot and the stove-on predicate remained satisfied. Only after the verifier
   confirmed `[true, false, true]` did MissionOS create one proposal, record one
   human approval, and admit one bounded dispatch.

VLA-0 then produced a fresh 8-by-7D prediction every step. The published
version-1 temporal ensemble selected one 7D action per invocation, with the
latest prediction weighted twice as heavily as each older aligned prediction.
The official checkpoint dataset statistics were loaded and verified.

The adapter constructs a numeric robot state, but the official VLA-0 LIBERO
configuration sets `return_proprio=false`. The Qwen model was therefore
conditioned on the two camera images, task language, and system prompt—not the
numeric robot state.

## What happened

The Repair used the official LIBERO-10 limit of 520 selected actions rather
than the earlier extended 720-action diagnostic budget.

- 520 model forwards produced 520 selected one-action simulator steps.
- The predicate vector remained `[true, false, true]` from start to finish.
- The minimum measured end-effector-to-target-center distance was approximately
  54.6 cm.
- Gripper-target contact was observed on 0 of 520 steps.
- The recorded frames show the arm moving on the right side of the workspace;
  no sustained or meaningful target-directed approach, grasp, or transport was
  observed.
- The already satisfied first-pot and stove predicates were maintained.
- No second attempt was authorized.

[![Annotated VLA-0 LIBERO fixture observation video: target pot displaced 22.7 cm, 520 simulator steps, no sustained target-directed approach observed](../assets/vla0-libero-clear-fixture-repair-poster.png)](../assets/vla0-libero-clear-fixture-repair.mp4)

[Watch the 26-second annotated step-sequence video](../assets/vla0-libero-clear-fixture-repair.mp4).
It is rendered from the 521 recorded agent-view frames at 20 frames per second;
it is not real-time wall-clock playback.

## What this result supports

The strongest bounded statement is:

> A privileged oracle recovered the exact scripted 22.7 cm fixture through the
> same original 7D simulator action interface in 517 of 520 allowed actions,
> including 20 stable success steps, without a preserve violation. VLA-0
> completed one nominal official-runner control, but in the recoverable fixture
> observation it produced no sustained or meaningful target-directed approach;
> the minimum end-effector-to-target-center distance remained approximately
> 54.6 cm, no gripper-target contact occurred, and the predicate vector remained
> `[true, false, true]` through 520 actions.

This behavior was not close to the completion threshold, and the oracle closes
the missing recoverability gate. The result is therefore classified as
`bounded_recoverable_fixture_repair_not_observed`: evidence that this VLA-0
configuration did not repair this exact recoverable fixture under the tested
observation, adapter, and budget. It is not evidence that VLA-0 can never
repair recoverable states.

A mid-episode distribution shift is one hypothesis: VLA-0 completed one nominal
episode but did not produce sustained target-directed behavior after a large
object-only change. Fixture infeasibility is now excluded for this action
interface and budget. Because full adapter parity is not established,
mid-episode distribution shift remains the leading hypothesis rather than a
proven mechanism.

## What is still unresolved

The positive control used the pinned official runner; the Repair used the
MissionOS adapter. The adapter matches the published version-1 ensemble formula
and verified checkpoint statistics. However, the official positive-control
output did not save enough exact numeric prediction/action material to establish
full one-step action parity for the same observation. Adapter difference is
therefore reduced, but not completely excluded.

This report does **not** establish:

- a general VLA-0 nominal success or recovery rate;
- a naturally occurring policy failure—the 22.7 cm displacement was scripted;
- a general conclusion that VLA-0 lacks Repair capability;
- that VLA-0 has no possible recovery trajectory under another observation,
  adapter, prompt, or checkpoint;
- same-world Semantic Repair success;
- controller ACK, physical Panda execution, or real-world safety.

The official positive control is not a MissionOS Repair, approval, or dispatch.
The Repair did apply 520 actions in the simulator, but no independent controller
ACK was observed and no physical execution was invoked.

## Reproducibility record

The public runner in
[`scripts/run_vla0_libero_snapshot_recovery.py`](../../scripts/run_vla0_libero_snapshot_recovery.py)
has the same SHA-256 recorded by the completed measurement. The exact pinned
source revisions, checkpoint contract, dataset-statistics digest, result digest,
raw-action digest, frame-capture digest, media digests, and residual uncertainty
are in the
[publication companion record](../agents/evidence/20260825-vla0-libero-clear-fixture-repair-publication.json).
The companion is cross-checked against a
[normalized public observation manifest](../agents/evidence/20260825-vla0-libero-clear-fixture-repair-normalized-observation.json)
and a
[normalized oracle control](../agents/evidence/20260826-vla0-libero-displaced-fixture-oracle-normalized.json).
They retain only sanitized metrics and source digests, not raw actions or
private frame sequences.

The upstream VLA-0 source and weights are CC BY-NC 4.0 and are not copied into
this repository. Live execution remains opt-in and requires separately obtained,
revision-pinned source and checkpoint files.
