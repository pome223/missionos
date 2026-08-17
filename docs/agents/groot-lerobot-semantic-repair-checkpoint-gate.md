# GR00T LeRobot Semantic Repair Checkpoint Gate

## Purpose

Before another L4 session, MissionOS must establish that both the task-specific
LeRobot checkpoint and its gated Cosmos processor dependency are accessible.
This is an admission check only. It creates no approval, dispatch authority,
execution evidence, or Semantic Repair claim.

The pinned candidate is:

- LeRobot source: `6adf51511b7625090eade8d82d9f61a1846ebe56`
- policy: `nvidia/gr00t17-lerobot-libero_10-640`
- policy revision: `5ee08ab09fac5c5ef2388a14c882ea825ac861db`
- processor dependency: `nvidia/Cosmos-Reason2-2B`
- processor revision: `9ce19a195e423419c349abfc86fd07178b230561`
- candidate task: LIBERO-10 task 8, the two-moka-pot Scene8 task

The policy checkpoint is purpose-built for LIBERO-10 and uses two visual
inputs, an eight-dimensional state, and seven-dimensional action chunks of 16
steps. That makes it a stronger Repair candidate than a checkpoint with no
success evidence on the selected suite. It does not itself establish that a
partially completed task can be repaired.

## GPU-free preflight

Run before provisioning a GPU:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SEMANTIC_PREFLIGHT=1 \
  HF_TOKEN=<access-token-not-written-to-evidence> \
  python scripts/preflight_groot_lerobot_semantic_repair.py
```

The gate passes only when both repositories answer for their exact pinned
revision. The public checkpoint is checked without credentials. The gated
Cosmos request presents the token only to `huggingface.co`, never follows a
redirect, and never includes the token value in the report. A missing token,
revision mismatch, gated-repository rejection, or transport error fails closed.
A passing result means only that the L4 baseline preconditions are met. It does
not create human approval or authorize provisioning, Repair, dispatch, or
physical execution.

## 2026-08-16 earlier unauthenticated L4 result

A Standard `g2-standard-4` L4 VM reached the official LeRobot rollout boundary:

1. the pinned LeRobot source and `groot` plus `libero` extras installed;
2. CUDA recognized the NVIDIA L4;
3. the task-specific checkpoint loaded;
4. LIBERO-10 task 8 was created;
5. the environment camera key was explicitly mapped from
   `observation.images.image2` to `observation.images.wrist_image`;
6. the official runner began the first rollout batch.

The episode did not execute. Processor construction attempted to access the
gated `nvidia/Cosmos-Reason2-2B` repository and received HTTP 401 because the VM
had no Hugging Face credential. Therefore:

```text
model_runtime_invoked = false
simulator_steps_observed = 0
official_predicate_observed = false
semantic_repair_established = false
physical_execution_invoked = false
```

The key mapping is a compatibility adaptation, not an unmodified official
command. Pinned LeRobot source maps `robot0_eye_in_hand_image` to `image2` in
`src/lerobot/envs/libero.py:151-159`, while the pinned policy config expects
`observation.images.wrist_image`. The adaptation changes the observation key,
not the recorded pixel input, but any future positive result from this path
must be described as a pinned LeRobot run with an explicit key mapping rather
than an unmodified official-runner result.

An earlier attempt was killed by host-memory exhaustion while the default FP32
parameter path loaded. A 24 GiB ephemeral swap file allowed loading to proceed;
this changed no model, task, instruction, or verifier condition. The final
failure stage was repository authorization, not GPU capacity or model behavior.

The three raw logs are retained outside the repository. No local path is
recorded here. Their SHA-256 digests are:

- first host-memory failure: `c60383015c2e40522da207dd9e87a0c4d0ce124405ed5a6fa87bff6e9da4c027`
- camera-key fail-closed run: `409c5b9f7fda61749d0fdf9c6ce93d0dbecfde05e647e8b415404d330787e1a6`
- final gated-repository failure: `28f75bb588ee14056a0c58e66185016f1dc055a60a8b7db154ad5077bc366a3b`

The VM and its boot disk were deleted after evidence collection. Existing
protected images and model-cache disks were not modified.

## 2026-08-16 authenticated baseline screen

The GPU-free preflight later passed using an existing Hugging Face credential.
The credential value was neither printed nor written to evidence. A fresh
Standard `g2-standard-4` VM in `us-east1-b` then downloaded and verified the
three pinned snapshots listed above plus `nvidia/GR00T-N1.7-3B` at
`2fc962b973bccdd5d8ce4f67cc63b264d6886495`.

The live path required four explicit compatibility adaptations:

- the camera-key mapping documented above;
- rewriting the checkpoint's stale training-time base-model and processor
  locators to the exact local pinned snapshots;
- resolving LIBERO scene assets omitted from the installed LeRobot package
  from the pinned Isaac-GR00T checkout; and
- 24 GiB of ephemeral host swap for the FP32 state-dict load.

These adaptations did not change the task, language instruction, model weights,
action stream, simulator success predicate, or episode budget. They do mean the
result is not an unmodified official-runner claim.

The first authenticated episode, with process seed 14, completed 520 simulator
steps and returned official failure. A five-episode screen with process seed 0
then returned:

```text
episode_successes = [true, false, true, true, false]
success_count = 3
episode_count = 5
```

This establishes that the pinned task-specific checkpoint can both complete and
naturally fail the same frozen task. It is candidate-screen evidence, not a
Semantic Repair run.

### Predicate-vector reproduction

The first two seed-0 episodes were rerun with a small observation-only patch to
the pinned LeRobot `LiberoEnv`. The patch read the same LIBERO `goal_state` and
`_eval_predicate` API used by the official task success function and emitted a
record only when the predicate vector changed. It did not change model input,
action, termination, reward, or success logic.

The rerun reproduced `[true, false]`. Both output videos were bit-identical to
episodes 0 and 1 of the uninstrumented five-episode screen. In the successful
episode, all three predicates became true at step 397 and official success was
true. In the natural failure episode, the last change occurred at step 172:

```text
on(moka_pot_1, flat_stove_1_cook_region) = false
on(moka_pot_2, flat_stove_1_cook_region) = true
turnon(flat_stove_1) = true
```

No later predicate change was emitted through the completed 520-step budget,
and official success remained false. Therefore this checkpoint/task pair now
has a live, non-model, achieved-plus-unmet candidate vector: preserve predicate
indices 1 and 2, target predicate index 0.

The source patch and sanitized evidence summary are retained at:

- `docs/agents/evidence/20260816-groot-n17-lerobot-libero10-baseline-screen-instrumentation.patch`
- `docs/agents/evidence/20260816-groot-n17-lerobot-libero10-baseline-screen.json`

Raw logs and videos remain outside the repository. Their digests are recorded
in the sanitized summary. The VM and auto-delete boot disk were explicitly
deleted after SHA-256 verification; no protected image or model-cache disk was
modified.

## CPU authority and adapter gate

The LeRobot execution profile is now connected to the existing same-world
Repair authority core without adding a second authority implementation.

The proposal schema binds both:

- `execution_adapter = lerobot_groot_n17_select_action_v1`; and
- `n_action_steps = 16`.

Both fields contribute to the Repair Contract digest and are repeated in the
approval and dispatch records. An approval created for the older 8-step
Isaac-GR00T/ZMQ adapter therefore cannot authorize the LeRobot path.

The fixed LeRobot proposal uses the short task-distribution instruction:

```text
put the first moka pot on the stove
```

The instruction targets predicate index 0. It does not ask the model to judge
or guarantee preservation. Predicate indices 1 and 2 remain deterministic
preserve conditions checked after every admitted 16-step chunk.

CPU runtime verification:

```bash
python \
  scripts/smoke_groot_libero_same_world_repair.py \
  --execution-profile lerobot-n17
```

The fixture smoke established the following runtime boundary:

- one new approval and one dispatch authorize at most the Contract-bound chunk
  count;
- the second model invocation receives the observation returned by the first
  chunk;
- the verifier can terminate with `satisfied`;
- a true-to-false preserve transition terminates with
  `stopped_on_preservation_violation` before another model invocation;
- the dispatch receipt is single-use and no second Repair is authorized; and
- fixture model and simulator behavior create no live inference, simulator, or
  physical-execution claim.

This is a production-boundary fixture, not live LeRobot evidence. The actual
LeRobot environment lifecycle, policy processor, CUDA model invocation, and
LIBERO `env.step()` path remain the next live gate.

## Next live gate

The baseline and candidate-selection gates are now satisfied. The next live unit
is not another success-rate screen. It is a new Mission Contract bound to the
observed failure vector, a new human approval, and one bounded same-world Repair
attempt that targets predicate index 0 while preserving indices 1 and 2.

That future run must continue to keep proposal, approval, dispatch, model
runtime, simulator effect, predicate preservation, and completion as separate
facts. This baseline created no approval or dispatch authority and did not
execute or establish Semantic Repair.

The live environment horizon must cover both the frozen 520-step source budget
and the separately bounded Repair budget while the MissionOS reset counter
remains exactly one. Reaching the source budget must not cause Gymnasium to
autoreset the environment. The live run passes only if all of the following are
observed in one environment session:

1. the source phase ends with `[false, true, true]` after its bound budget;
2. a new Contract, human approval, and dispatch bind that exact vector;
3. GR00T is invoked at least twice under the short Repair instruction;
4. each later invocation consumes the observation returned by the prior chunk;
5. the non-model predicate verifier evaluates after every chunk;
6. preserve indices 1 and 2 never become false; and
7. index 0 becomes true, yielding `[true, true, true]`.

Budget exhaustion, a preservation violation, an adapter or instruction digest
mismatch, an implicit reset, or missing simulator-step evidence is a bounded
negative result. None may be rewritten as successful Semantic Repair.

## LeRobot live-session harness

`scripts/run_groot_lerobot_same_world_repair.py` now composes the pinned
LeRobot environment, policy, processor pipelines, Mission Contract, approval,
single-use dispatch, bounded-chunk authority, and per-step predicate verifier.
It remains opt-in and has not yet produced live evidence.

The pinned policy implements `select_action` as a stateful queue. A model
forward fills the queue with 16 actions; the next 15 calls consume that queue.
The frozen 520-step source budget is not divisible by 16, so a failed source
episode can end with eight actions generated under the source instruction still
queued. Those actions are not authorized by the later Repair Contract.

The live-session adapter therefore enforces this boundary:

1. create and reset the LIBERO environment exactly once;
2. run the frozen source instruction for at most 520 simulator steps;
3. require the exact observed source vector `[false, true, true]`;
4. record and discard any queued source actions by resetting policy state only;
5. verify that the environment reset count remains one;
6. create the new proposal, human approval, and dispatch;
7. require each admitted Repair chunk to start with an empty policy queue;
8. require the first `select_action` call to invoke the model and leave exactly
   15 queued actions;
9. observe the language payload after `GrootN17PackInputsStep` and require an
   exact readback match before retaining the pending chunk;
10. apply exactly 16 actions while observing predicates and physical-simulation
   witnesses after every step; and
11. require the queue to be empty before another model invocation.

Policy reset at the Repair boundary is not an environment reset and does not
restore simulator state. It only prevents commands generated under the source
instruction from crossing into the newly approved Repair dispatch.

CPU production-boundary smoke:

```bash
python scripts/smoke_groot_lerobot_live_session.py
```

The fixture establishes queue ownership, one-world continuity, updated
observation flow, bounded dispatch, and predicate-driven termination. It does
not invoke CUDA, the real checkpoint, or LIBERO.

The future L4 command is:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
  python scripts/run_groot_lerobot_same_world_repair.py \
  --checkpoint-path <pinned-local-checkpoint> \
  --operator-approval-ref <new-human-approval-ref> \
  --dispatch-state-path <new-ledger-path> \
  --output <new-evidence-path> \
  --screen-progress-output <new-progress-path> \
  --maximum-repair-chunks 45
```

The command requires the pinned LeRobot Git revision and a locally resolved
checkpoint whose model and processor locators already point to the pinned
snapshots. Local paths, credentials, raw simulator state, and model weights are
not written to the result. Failures record a sanitized cause type and preserve
`semantic_repair_established=false`. If a chunk fails closed after one or more
simulator actions were applied, the failure artifact also retains a sanitized
failure code and a digest-bound `executed_step_trace`. That partial trace is
execution evidence only: it sets neither verifier pass nor completion.

## 2026-08-17 live attempt: not established

A cost-capped Standard `g2-standard-4` L4 session exercised the committed live
entrypoint. The pinned policy, base-model, and Cosmos snapshots were admitted
from local revision metadata and required-file digests. The real LeRobot
environment downloaded its public LIBERO assets, reset, and entered the first
policy forward.

The run did **not** produce an action chunk. The frozen `LiberoEnvConfig` used
`pixels_agent_pos`, but the concrete runtime environment was created without
passing that field and therefore returned pixels only. GR00T N1.7 rejected the
missing state at its action input. Commit `cf8e536` passes the frozen observation
mode into the runtime environment, but the one-hour cloud session ended before
that fix could be rerun.

Two earlier preflight failures in the same bounded session are retained rather
than upgraded: the first encountered LIBERO's interactive first-run config in a
non-interactive shell, and the second lacked a local traceback. No attempt
reached the source partial vector, Repair dispatch, simulator action, or
completion verifier. Therefore:

```text
pinned policy weights loaded on L4       = true
real LIBERO environment reset observed   = true
first policy forward entered             = true
action chunk produced                    = false
Repair executed                          = false
Semantic Repair established              = false
physical execution invoked               = false
post-fix live rerun completed             = false
```

The sanitized record is
`docs/agents/evidence/20260817-groot-n17-lerobot-same-world-live-not-established.json`.
Raw operator logs remain outside the repository. The VM and its temporary boot
disk were deleted after evidence retrieval.

## 2026-08-17 committed-head rerun: source satisfied, Repair not needed

A second cost-capped Standard `g2-standard-4` L4 session reran the committed
entrypoint. The first attempt exposed a second harness boundary: unlike the
official one-element `SyncVectorEnv`, the directly owned same-world environment
returned nested robot-state arrays without a leading batch dimension. Commit
`59c36c3` adds only that one-environment vector dimension before LeRobot's
official observation preprocessor. A production-boundary probe then observed
`observation.state` with shape `(1, 8)` before the live rerun.

The committed rerun loaded the pinned policy, base model, and Cosmos processor,
reset the real LIBERO environment exactly once, invoked the real model 29 times,
and executed 453 simulator steps. Its predicate timeline changed at steps 168,
176, 179, and 452. At step 452 all three official predicates were true, so the
source task terminated as `source_satisfied_no_repair_needed`.

This is valid source-execution evidence, but it is not Semantic Repair. The
frozen expected partial vector `[false, true, true]` was not reproduced; no
Repair proposal, approval-bound Repair dispatch, or Repair action was created.
The stronger claim therefore remains false:

```text
committed head exercised                 = true
real GR00T model forwards                = 29
real LIBERO simulator steps              = 453
same-world reset count                   = 1
source predicate timeline observed       = true
source task satisfied                    = true
expected partial vector reproduced       = false
Repair executed                          = false
Semantic Repair established              = false
physical execution invoked               = false
```

The sanitized record is
`docs/agents/evidence/20260817-groot-n17-lerobot-live-rerun-source-satisfied.json`.
Raw operator logs remain outside the repository. Their archive and every
contained file were verified by SHA-256 before the VM and temporary boot disk
were deleted.

The raw archive's operator-written `session-meta/lerobot-commit.txt` is not a
trustworthy revision record: the collection command wrote an incorrect fixed
string instead of reading `git rev-parse`. The runtime itself remained
fail-closed on the committed pin
`6adf51511b7625090eade8d82d9f61a1846ebe56`; otherwise it would have stopped
before policy construction. The sanitized evidence records this metadata error
rather than silently replacing the archived file.

The rerun also exposed why the natural partial failure from the authenticated
baseline screen did not recur. The screen used process seed `0`, environment
seed `0`, and init-state index `1`; the live harness used environment seed `1`.
The source success therefore did not retest all frozen candidate conditions.
The candidate profile now binds all three values to the screened baseline.
The live entrypoint also observes the exact material passed to LIBERO's
`set_init_state`, verifies its digest and selected index after the sole reset,
and records that evidence in the report. This is stronger than trusting the
requested index or seed alone.

## 2026-08-17 budget-truncation attempts: control path only

Two later attempts deliberately stopped the source task before the previously
observed success step. They exercised the production proposal, approval,
single-use dispatch, GR00T invocation, simulator-step, and verifier path, but
they do not establish Semantic Repair because the source failure was created
post hoc rather than observed under the frozen 520-step task budget.

At a 480-step source cutoff, the first Repair chunk moved protected
`moka_pot_2` by `0.005054431863653664` metres against a Contract-bound `0.005`
metre limit. The verifier stopped before a second chunk while the predicate
vector remained `[false, true, true]`. The threshold was not relaxed to fit the
result.

At a 496-step source cutoff, six Repair actions were applied before the
simulator rejected the next action because its episode horizon had already
expired. No predicate improved. The harness had allocated source plus Repair
steps but omitted LIBERO's ten reset-stabilization steps from the simulator
horizon. The live entrypoint now binds those ten steps separately; this fixes
the runtime budget but does not upgrade the old attempt.

The sanitized record is
`docs/agents/evidence/20260817-groot-n17-lerobot-budget-truncation-attempts.json`.
The raw archive remains outside the repository. Its transfer SHA-256 matched;
all contained evidence files matched their hashes except the manifest's
self-referential entry, which was generated incorrectly and is reported as
invalid. The VM and boot disk were deleted after collection.

## Natural-failure screen before Repair

The live CLI can now accept repeated `--screen-init-state-index` arguments. It
loads the pinned GR00T policy once, then evaluates the requested init states in
order under the full frozen 520-step source budget. Successful episodes and
non-asymmetric failures create no approval or dispatch. The first observed
`[false, true, true]` or `[true, false, true]` full-budget failure remains in its
same simulator world and is the only state eligible for a new Repair proposal,
human approval, and dispatch.

The natural-failure basis is derived by the harness. It is not a CLI choice,
and screen mode rejects any source budget other than 520. Therefore a
post-hoc-truncated run cannot reach `semantic_repair_established=true`.
The screen accepts at most twenty distinct init states per invocation so an
operator can amortize the observed model-initialization cost without turning
one paid session into an unbounded search. The first natural asymmetric
failure still terminates screening and is the only state eligible for Repair;
the larger diagnostic budget creates no approval or dispatch authority.
The report derives policy reuse from the observed model-load counter rather
than asserting it. The live entrypoint also reads LeRobot's `num_steps_wait`
from the constructed environment and fails closed unless the observed value is
the Contract-bound ten reset-stabilization steps.

Example bounded screen:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
  python scripts/run_groot_lerobot_same_world_repair.py \
  --checkpoint-path <pinned-local-checkpoint> \
  --operator-approval-ref <new-human-approval-ref> \
  --dispatch-state-path <new-ledger-path> \
  --output <new-evidence-path> \
  --maximum-repair-chunks 45 \
  --screen-init-state-index 0 \
  --screen-init-state-index 1 \
  --screen-init-state-index 2 \
  --screen-init-state-index 3 \
  --screen-init-state-index 4
```

The CPU runtime smoke now invokes this production CLI's fixture backend rather
than a separate orchestration script. It traverses source candidate selection,
proposal, approval, generated non-empty `dispatch_ref`, single-use ledger, two
bounded chunks, and predicate termination. Fixture success remains incapable
of creating live inference, simulator execution, physical execution, or
Semantic Repair claims.

### 2026-08-17 first one-load screen: throughput negative

The first cost-bounded one-load screen did not write a final result before the
operator stopped it. A live Python stack sample showed the active path inside
`json.dumps` through `canonical_sha256` while `select_action` was consuming a
queued Repair action. The old entrypoint canonicalized the full processed
observation, including image tensors, before every policy selection even when
LeRobot's queue meant no model forward would occur. The run therefore does not
establish a natural candidate, Repair execution, or Semantic Repair.

The runtime now creates policy request and response digests only when an
observed model forward occurs. A queued action must carry neither digest, and
the live session rejects evidence that claims otherwise. The screen also
atomically writes a diagnostic progress artifact after each completed init
state. That artifact cannot create approval, dispatch, task-completion, or
Semantic Repair authority. This preserves completed screen results if a later
episode is interrupted without turning partial progress into a receipt.

The sanitized negative record is
`docs/agents/evidence/20260817-groot-n17-lerobot-natural-screen-throughput-negative.json`.
The raw profiler output remains outside the repository under an archive with
SHA-256 `13c290a56124f21fb0c3cf1a40939f6ed6f4a036c583f4b517a3fbd753fbe66a`.

## 2026-08-17 natural partial failure: governed loop closed, Repair did not

A later one-load screen found a natural full-budget asymmetric failure at init
state index 9. This candidate was not created by truncating an otherwise
successful episode:

```text
source steps                    = 520 / 520
source predicate vector         = [true, false, true]
natural task failure            = true
new Contract                    = created
new human approval              = observed
single dispatch                 = observed
same-world reset count          = 1
real GR00T Repair chunks        = 45
real LIBERO Repair steps        = 720
final predicate vector          = [true, false, true]
preserve predicates maintained  = true
target predicate improved       = false
result                          = budget_exhausted_without_improvement
Semantic Repair established     = false
physical execution invoked      = false
```

This closes the governed control loop on a natural partial failure: diagnosis,
Contract, approval, dispatch, repeated real model inference, same-world
simulator execution, and non-model verification all ran. It does **not** close
Semantic Repair because the unmet predicate never became true. The original
failure is retained and the failed Repair is not upgraded to success.

The same init-state index did not reproduce the candidate. Two later executions
of index 9 satisfied the source task at step 384. Across the retained campaign,
14 episode executions covered 12 distinct init-state indices. Eleven executions
succeeded, one produced the asymmetric candidate, and two ended with
`[false, false, true]`. Ten of the 12 distinct indices were observed successful
at least once. These counts are descriptive only: the executions are not
established to be independent and do not estimate a success probability.

The important negative result is that an init-state index is insufficient to
revisit a candidate outcome. More random screening can find more outcomes, but
it cannot provide a controlled instruction comparison if the candidate world
cannot be restored. Additional GPU screening is therefore not the next gate.

The sanitized summary is
`docs/agents/evidence/20260817-groot-n17-lerobot-natural-repair-and-screen-summary.json`.
Raw logs and per-step traces remain outside the repository under archives whose
SHA-256 digests are recorded in that summary.

## Simulator-state snapshot gate

The pinned LIBERO wrapper exposes MuJoCo state through
`get_sim_state()` and restores it through `regenerate_obs_from_state()`, which
sets the flattened simulator state, calls `forward`, runs post-processing, and
refreshes observables. The opt-in probe is:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_STATE_SNAPSHOT_PROBE=1 \
  LIBERO_CONFIG_PATH=<local-libero-config> \
  MUJOCO_GL=egl \
  python scripts/probe_groot_lerobot_state_snapshot.py \
  --episode-init-state-index 9 \
  --snapshot-out <external-snapshot.npy> \
  --output <external-result.json>
```

The committed-head CPU runtime probe loaded no GR00T model. It captured a
47-value simulator state, wrote it atomically to disk, changed the live
simulator state, loaded the file, and restored from it. The restored state was
exactly equal with maximum absolute error `0.0`; all three official
goal-predicate observations also matched their pre-mutation values. The raw
snapshot remains outside the repository and is bound by SHA-256. The exercised
MissionOS commit was `4fa53890ab832f216a1e1aa6cdb76456a39692a9`.

This establishes disk-backed restoration of the MuJoCo physics state. It does
not yet establish a bit-for-bit trajectory replay. Before a saved candidate can
support instruction ablation, the replay harness must additionally bind or
reset:

- the GR00T policy action queue;
- Python, NumPy, and Torch random-generator states;
- environment episode counters and horizon bookkeeping; and
- any controller state not represented in the flattened MuJoCo state.

Restored clones are diagnostic comparison worlds. They must not be described as
the original live world or as proof that production Repair continued without a
reset. A future production Repair claim still requires one authorized branch
to continue from the naturally observed failure without cloning.

The sanitized probe summary is
`docs/agents/evidence/20260817-groot-lerobot-simulator-state-snapshot-probe.json`.

## Reusable runtime image

The successful setup was sanitized and saved as Google Cloud image
`missionos-groot-n17-lerobot-runtime-v2-20260817` in `us-central1`. Before image
creation, the model cache, temporary MissionOS checkout, raw evidence, logs,
credential file, shell histories, and swap file were removed. The image keeps
only the public pinned runtime, Python dependencies, LIBERO assets, and the
OSMesa libraries needed for CPU-only simulator probes. Model weights remain a
separate cache concern and must not be baked into the image.

The source L4 VM was stopped before image creation and then deleted; its
auto-delete boot disk was confirmed absent. The image reached `READY`. This
reduces future paid setup time but creates no execution, Repair, or completion
claim.

## Final bounded candidate-and-replay experiment

Full RNG and GPU-kernel replay determinism is not required for the remaining
question. The experiment instead separates the one production-relevant branch
from a small statistical diagnostic comparison:

1. screen at most the explicitly listed init-state indices with one model load;
2. atomically save every completed failed world, including non-asymmetric
   `[false, false, true]` failures, as a diagnostic-only `.npz` artifact;
3. when the first natural asymmetric failure appears, run one approved Repair
   in that uninterrupted original world;
4. restore that saved physics state into fresh diagnostic environments;
5. before every clone trial, require the exact saved predicate vector or reject
   the trial before proposal, approval, or dispatch;
6. run `short_target` and `original_task` five times each in alternating pair order,
   with the same recorded sampling seed within each pair; and
7. atomically write each full trial record and the cumulative progress after
   every trial.

The original-world Repair and the diagnostic clones have different authority.
Only success in the uninterrupted original world can establish Semantic
Repair. A successful clone can show repair capability or instruction
sensitivity for a saved physics state, but it cannot be promoted to same-world
continuity or task completion. The snapshot digest is Contract-bound and the
shared Repair core returns `satisfied_diagnostic_observation`, never
`satisfied`, for clone completion.

The result language is fixed before execution:

- original-world success: Semantic Repair established for that live run;
- clone-only success: diagnostic repair capability or instruction sensitivity
  observed; Semantic Repair remains false;
- both variants at `0/5`: Repair was not shown in ten trials for that saved
  world, checkpoint, instruction set, and budget;
- unequal X/Y counts: exploratory instruction-sensitivity evidence only, with
  no general superiority claim; and
- no natural candidate: report the cumulative screened-state count and
  asymmetric-failure count, then stop.

The source Contract records the SHA-256 of this result-language registry, the
live runner, the LeRobot Repair binding, and the shared Repair authority core.
Changing the interpretation or execution code therefore changes the frozen
contract material rather than silently reinterpreting a completed run.

The live one-session invocation is:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
  PYTHONPATH="$PWD" \
  python scripts/run_groot_lerobot_same_world_repair.py \
  --runtime live \
  --checkpoint-path <pinned-checkpoint> \
  --operator-approval-ref <operator-approval-ref> \
  --dispatch-state-path <external-dispatch-ledger.json> \
  --output <external-final-result.json> \
  --screen-progress-output <external-screen-progress.json> \
  --failure-snapshot-dir <external-failure-snapshots> \
  --replay-trials-per-variant 5 \
  --replay-seed-base 1000 \
  --replay-progress-output <external-replay-progress.json> \
  --replay-trial-output-dir <external-replay-trials> \
  --maximum-repair-chunks 45 \
  --screen-init-state-index 0 \
  --screen-init-state-index 1 \
  --screen-init-state-index 2 \
  --screen-init-state-index 3 \
  --screen-init-state-index 4 \
  --screen-init-state-index 5 \
  --screen-init-state-index 6 \
  --screen-init-state-index 7 \
  --screen-init-state-index 8 \
  --screen-init-state-index 9 \
  --screen-init-state-index 10 \
  --screen-init-state-index 11
```

The cost-capped VM must still be deleted after collecting and hash-verifying
the external evidence, regardless of whether the candidate or Repair succeeds.

### Final bounded live result (2026-08-17)

The committed harness at `740c76c` completed the pre-registered experiment on
one standard, cost-capped NVIDIA L4 VM. The first two screened worlds satisfied
all three task predicates. The third world, init-state index `1`, reached its
full 520-step source budget with the natural asymmetric vector
`[false, true, true]`. The failed state was saved outside the repository before
Repair and bound by snapshot SHA-256
`6f2fa4e6c89cf6b7935355a683ca67fe9865cd2b8eb4960f1b63a0635d17d9b9`.
This is the pre-registered candidate shape: `moka_pot_1` was the sole target,
while `moka_pot_2` and `flat_stove_1` were the two preserve conditions.

MissionOS then continued the uninterrupted original world with a new Repair
Contract, the supplied human approval reference, one dispatch, and a 45-chunk
budget. GR00T inference and simulator execution were observed. The source and
terminal vectors were both `[false, true, true]`; the two preserve predicates
remained true, but the target predicate did not improve. The result was
`budget_exhausted_without_improvement`. A dispatch receipt was present, but a
separate controller ACK was not observed. This run therefore establishes the
governed original-world Repair attempt, not successful Semantic Repair.

Ten diagnostic clone trials then restored the saved predicate vector exactly
before any proposal, approval, or dispatch. Pair order alternated, but five
pairs cannot be fully order-balanced: `short_target` ran first in three pairs
and `original_task` ran first in two. The results were:

- `short_target`: `1/5` diagnostic completions;
- `original_task`: `1/5` diagnostic completions;
- no preservation violation in the ten clone trials; and
- no instruction-variant superiority established.

The strongest independent positive result is preservation, not Repair success:
the two preserve predicates remained true in the original-world attempt and in
all ten diagnostic clones (`11/11`, zero observed violations). This is bounded
to the saved world, checkpoint, instruction set, and budget; it is not a claim
of general VLA safety.

The two completions occurred under different sampling seeds: `original_task`
at seed `1001` after 28 chunks, and `short_target` at seed `1003` after 26
chunks. Because these were fresh diagnostic policy sessions restored from a
physics snapshot, neither completion is same-world continuation. The
pre-registered conclusion applies without reinterpretation: Repair capability
was observed in diagnostic clones, while Semantic Repair remains false.

The clone completion rate is descriptive only: `2/10 = 0.2`, with a rough
interval of about `0.03–0.56` for this small sample. The original-world Repair
was one attempt and did not improve; that single miss does not show that
continuation is harder than clone re-entry. The continuation-versus-clone
difference remains unmeasured.

This also exposes a product question rather than a measured requirement. If a
future model really had an independent per-attempt repair probability near
`0.2`, a single-dispatch policy would be a roughly one-in-five rescue path;
about eleven independent attempts would be needed to reach roughly 90% chance
of at least one success. This is a planning calculation, not an estimate of
this checkpoint, and it does not authorize repeated dispatches. The choices
between one approval per attempt, one approval for a bounded attempt budget,
or a better checkpoint remain product decisions.

The raw simulator snapshot, per-step traces, dispatch ledger, and trial reports
remain outside the repository. The sanitized, hash-bound summary is
`docs/agents/evidence/20260817-groot-n17-lerobot-natural-repair-diagnostic-replay.json`.
All copied artifact hashes matched between the VM and local evidence archive.
The GPU VM and its auto-delete boot disk were confirmed absent after collection;
the protected model-cache disk was left untouched.
