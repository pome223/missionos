# Cosmos Policy LIBERO Repair resume contract

This procedure resumes the pinned Cosmos Policy LIBERO experiment only after a
human has accepted the NVIDIA model terms and the approved Hugging Face token
has been stored in Google Cloud Secret Manager. Never put an access token in a
command, local credential store, log, issue, checkpoint manifest, VM metadata,
or chat message.

## 1. Stage the gated tokenizer before creating a GPU VM

The public Policy checkpoint depends on one file from the gated base-model
repository. Resolve the Secret Manager locator through environment variables;
the script reads the payload only into process memory:

```bash
export RUN_MISSIONOS_COSMOS_POLICY_TOKENIZER_DOWNLOAD=1
export MISSIONOS_COSMOS_POLICY_HF_SECRET_PROJECT="<gcp-project>"
export MISSIONOS_COSMOS_POLICY_HF_SECRET_NAME="<secret-name>"
python scripts/prepare_cosmos_policy_libero_tokenizer.py \
  --output-dir "$HOME/.cache/missionos/cosmos-policy-predict2-tokenizer"
```

The preflight fixes and verifies all of the following before paid GPU use:

- repository: `nvidia/Cosmos-Predict2-2B-Video2World`;
- revision: `f50c09f5d8ab133a90cac3f4886a6471e9ba3f18`;
- file: `tokenizer/tokenizer.pth`;
- size: `507609880` bytes;
- SHA-256: `38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981`.

The script fails before downloading when the explicit opt-in, Secret Manager
locator, secret access, or gated-model entitlement is absent. It records no
credential material. An already-present file is accepted only when its size
and digest match.

## 2. Establish fixture recoverability before creating a GPU VM

The frozen Repair snapshot must have a digest-bound scripted recoverability
report before any paid policy run. The report is diagnostic-only and must show:

- the report digest is valid and its snapshot SHA-256 matches the exact input;
- success through the same raw 7D LIBERO simulator action interface;
- all actual goal predicates true for at least 20 settle steps;
- no declared protected-object preservation violation;
- no model inference and no physical execution.

The 22.7 cm fixture with SHA-256
`370c9436d63c0eafcc76deeb139cd4e2cafe40b04ab3189d6892c8dbf2fc8386`
passes this gate through report digest
`6ac0f20779787a2fa7a7d57df604a59b460c7219097557caf0565416547024`.
That diagnostic first observed success after action 497 and retained all three
predicates for 20 settle steps, with 517 total raw 7D actions and a maximum
protected-object displacement of about 4.177 mm under the 5 mm bound. Other
scripted controllers failed on the same fixture; those negative attempts do
not negate this successful digest-bound run.

## 3. Create the capped L4 runtime only after both local gates pass

Use one `g2-standard-4` L4 VM with a maximum runtime and `DELETE` termination
action. Transfer the verified tokenizer file, the four digest-bound public
Policy checkpoint files, the pinned public source, the publication-reviewed
MissionOS runner, and the exact fixture. Do not transfer the Hugging Face token
or mount the Secret Manager credential into the GPU VM.

Before model loading, verify that the runtime can create a real LIBERO
offscreen context. The CUDA image may contain the compute driver without the
matching NVIDIA EGL userspace library. Confirm that `libEGL_nvidia.so.0` is
available and install the `libnvidia-gl` package from the same driver branch
already present on the image. Do not mix `-server` and non-server driver
branches. The runtime binds `MUJOCO_GL=egl`, `PYOPENGL_PLATFORM=egl`, and
`MUJOCO_EGL_DEVICE_ID=0`; a successful Python import alone is insufficient.

The live runner must receive the verified local tokenizer explicitly:

```bash
export RUN_MISSIONOS_COSMOS_POLICY_LIBERO_EXPERIMENT=1
python scripts/run_cosmos_policy_libero_experiment.py \
  --source-root /path/to/pinned/cosmos-policy \
  --checkpoint-path /path/to/Cosmos-Policy-LIBERO-Predict2-2B \
  --tokenizer-path /path/to/tokenizer/tokenizer.pth \
  --restore-snapshot /path/to/displaced-from-stove.npz \
  --oracle-recoverability-report /path/to/oracle-recoverability.json \
  --output-dir /path/to/new/output \
  --dispatch-state /path/to/new/dispatch.json \
  --operator-approval-ref operator:explicit-approval \
  --maximum-actions 520
```

The runner validates the oracle report before creating its output directory or
loading the model. A missing, negative, mismatched, or claim-invalid report
therefore fails before paid inference.

## 4. Preserve the experiment gates

The runner loads one model and executes these phases in order:

1. nominal LIBERO-10 task 8, init state 15;
2. stop without starting Repair if the actual nominal predicate conjunction is
   not observed;
3. restore the digest-bound `displaced_from_stove` fixture only after nominal
   success;
4. apply at most 520 simulator actions as 32 full 16-action chunks plus one
   final 8-action prefix;
5. save model future predictions and actual simulator observations in disjoint
   artifact trees;
6. allow only actual LIBERO predicates to establish success.

Model output is a proposal. Human approval, bounded dispatch, simulator action
application, observed predicate effects, completion, controller ACK, and
physical execution remain separate facts.

## 5. Collect evidence and delete resources

Copy the complete output and runtime logs locally, validate their hashes, and
then delete the exact VM and all attached billable disks. A model load, future
prediction, or generated action does not by itself establish nominal success,
Repair success, controller ACK, or physical execution.

## 6. Diagnose candidate generation before building a selector

The exact-fixture 520-action run produced no contact. Before implementing a
best-of-N selector, run the cache-only seed probe with the successful nominal
report and exact-fixture oracle report as admission evidence:

```bash
export RUN_MISSIONOS_COSMOS_POLICY_LIBERO_SEED_PROBE=1
python scripts/run_cosmos_policy_libero_seed_probe.py \
  --source-root /path/to/pinned/cosmos-policy \
  --checkpoint-path /path/to/Cosmos-Policy-LIBERO-Predict2-2B \
  --tokenizer-path /path/to/tokenizer/tokenizer.pth \
  --restore-snapshot /path/to/displaced-from-stove.npz \
  --oracle-recoverability-report /path/to/oracle-result.json \
  --nominal-report /path/to/nominal/report.json \
  --output-dir /path/to/new/seed-probe-output \
  --operator-approval-ref operator:explicit-seed-probe-approval \
  --seeds 17,71,195,231
```

This loads the model once and applies at most 128 actions for each of four
unique seeds. It uses only the released embedding for the original task string
and does not perform candidate selection. The 2026-08-27 run found zero
contacts for all four seeds, minimum end-effector distances of about 52.7 cm,
and target translations of about 0.67 micrometres. A selector is therefore not
the next implementation step for this exact state unless candidate generation
changes first.

Do not silently turn this into an instruction ablation. Upstream computes an
unseen string with `google-t5/t5-11b`, while the released cache contains the
known LIBERO task strings. Before adding a new instruction, regenerate one
known cached sentence through the exact upstream encoder and require embedding
equality. A smaller encoder or different pooling path is a different
experiment.

## 7. Measure the small-displacement boundary before blaming fixture severity

`probe_libero_displacement_curriculum.py` constructs one diagnostic point per
process on a protected-separating horizontal ray from the successful target
position. It rejects predicate-unstable points, object jumps, protected-pot
motion, and translations above the preregistered 5 cm sweep range. Source
dataset metadata is not predicate authority; the restored official LIBERO
environment is. Fixture construction, oracle execution, and policy execution
must use the same environment seed.

The 2026-08-28 boundary run found stable `[true, false, true]` fixtures at 0.5,
1.0, 1.5, and 2.0 cm. The 0.5 cm snapshot has SHA-256
`458e2d60eef71e4b9a6ebf3528c944ebad725f1eb5c6abfeda56a0c63d200a80`.
A same-hash raw-7D oracle recovered it after action 52 and held all predicates
for 20 settle steps, with 73 total actions and effectively zero protected-pot
motion.

The bounded Cosmos Policy trial on that 0.5 cm fixture still produced zero
contacts in 128 actions, moved the target by less than 0.5 micrometres, and
never changed `[true, false, true]`. This rejects displacement severity as a
sufficient explanation. It does not establish general model incapability.

That result used a seed-7 fixture in a seed-0 policy environment and is retained
only as historical diagnostic evidence. It must not be used as the primary
competence result. The aligned seed-0 rerun below supersedes it.

## 8. Keep robot-pose and instruction probes diagnostic-only

`generate_libero_robot_pose_normalized_fixture.py` creates a diagnostic clone
that preserves the fixture objects and stove state while directly replacing
the robot joint and gripper state with the task-initial pose. Because simulator
state is directly changed, the clone is never eligible for a Repair claim even
if actual predicates later improve.

For the 0.5 cm clone, object motion during construction was at most 5.7
micrometres and `[true, false, true]` held for all 10 settle steps. Its snapshot
SHA-256 is
`1d10b9544e867cf415bb0ea2e4dcac287c5db7cafcaf151c5ceafed5a2173c26`.
The same-hash oracle recovered it in 53 actions without a preservation
violation.

With the original cached instruction, Cosmos Policy then moved the end
effector to 26.9 cm from the target and changed the gripper sign once, but
still produced zero contacts and no target motion in 128 actions. The exact
cached sentence `turn on the stove and put the moka pot on it` reduced the
minimum distance to 23.1 cm but also produced zero contacts and no predicate
improvement. This is an embedding-cache-controlled diagnostic, not a
target-specific instruction test: the sentence does not identify the second
pot.

Run `analyze_cosmos_policy_future_actual_motion.py` only as a visual-motion
diagnostic. After correcting LIBERO's stored image orientation, the original
instruction run had mean agent-view pixel differences of 8.96 for actual
motion and 17.59 for predicted motion; the cached singular run measured 8.19
and 16.95. Thus the future rollout is not visually static, but pixel difference
does not establish object motion, physical correctness, or task success.

These results prioritize action/world alignment and target-specific language
over a best-of-N selector. A selector remains unjustified until at least one
candidate contacts or moves the target. Unknown target-specific text still
requires exact T5-11B cache-parity verification before use.

## 9. Separate object sensitivity from corrective targeting

`run_cosmos_policy_libero_paired_pose_sensitivity.py` replays the exact
successful nominal trace and tests five robot poses against one successful
object state plus 0.5, 5, and 22.5 cm requested displacements. It performs 80
forward queries without applying any queried action. The requested 5 cm state
settled to 14.31 cm, so reports keep requested and observed displacement
separate.

The strongest action-signal-to-seed-noise ratios occurred at nominal action
184: 14.51 for the observed 14.31 cm displacement and 13.03 for the observed
22.71 cm displacement. The model is therefore not object-blind in this fixed
diagnostic. Sensitivity is phase-dependent, however; poses 92 and 276 were
mostly below seed noise.

`run_cosmos_policy_libero_pose_rollout_diagnostic.py` then applied 128 actions
for poses 0, 184, and 369 with four seeds at the observed 22.71 cm state. All
12 trials had zero target contacts, zero target motion, and no actual predicate
success. Pose 184 produced large actions but moved the end effector away from
the target. One pose-369 trial also changed the protected-pot predicate. Thus
action sensitivity is not evidence of corrective targeting, and neither an
MPC wrapper nor a candidate selector is justified by these candidates.

Both runners directly transplant simulator state and robot pose. Their results
are diagnostic-only and cannot establish MissionOS Repair.

## 10. Do not describe the public training mixture as success-only

The public `nvidia/LIBERO-Cosmos-Policy` task-8 subset contains 31 successful
and 19 failed episodes. The released training configuration samples
demonstrations and rollouts and includes both successful and failed rollouts.
An offline action-only probe found six of 50 episodes with more than four
gripper sign flips, which is only a regrasp proxy.

The HDF5 files do not contain per-step LIBERO predicates or complete simulator
state. They therefore cannot establish that any extra gripper cycle is a
semantic recovery segment. The bounded conclusion is that the earlier
"success demonstrations only" premise is false for this public training
mixture; the presence or absence of labeled repair trajectories remains
unestablished.

## 11. Close the target-specific instruction hypothesis before MPC

The exact upstream `google-t5/t5-11b` encoder generated a replacement baseline,
three target-specific instructions, and one wrong-target control. The generated
baseline differed from the released bf16 cache in 5,665 of 524,288 elements;
the maximum absolute difference was one tested bf16 rounding step
(`0.00390625`). Its 128-action behavior closely reproduced the released-cache
baseline but was not action-bitwise identical.

At pose 184 and the observed 22.71 cm displacement, six 128-action arms were
run with the same seed: released baseline, regenerated baseline, three
target-specific forms, and the wrong-target control. Every arm produced zero
contacts, zero target motion, no predicate improvement, and a minimum target
distance equal to the initial 24.05 cm distance. The target-specific strings
changed action values, but the wrong-target string changed them by a similar
order and produced the same qualitative outcome. This supports instruction as
a perturbation in this diagnostic, not semantic corrective grounding.

Stop prompt, seed-selector, and WAM-MPC work for this exact checkpoint, task,
and fixture. The next comparison should change the executor capability: use a
known skill or scripted controller, or add explicitly labeled recovery data.
That is a design recommendation, not proof that every Cosmos Policy task lacks
recovery capability.

## 12. Use the aligned seed-0 3 cm fixture as the bounded primary result

The policy runner, source-state replay, fixture constructor, visibility probe,
and oracle now all use LIBERO environment seed 0. Replaying the digest-bound
nominal trace produced a successful object state with the robot normalized to
its nominal initial pose before displacement construction.

The aligned sweep changed the fixture choice:

- 2 cm remained `[true, true, true]` and is not a Repair fixture;
- 5 cm settled 16.20 cm from the source and was rejected for instability;
- 3 cm remained `[true, false, true]` for 60 construction steps and another 60
  zero-action steps after fresh restore, with target drift below 1 nanometre;
- both policy cameras observed robust processed-image differences.

The exact 3 cm snapshot SHA-256 is
`8064d6faeeb02a67a08649be0ca39529b4a79da459cf8d11493c0412bbc7b651`.
The same controller recovered it identically in three runs: first explicit
contact after action 32, predicate success after action 33, and 54 actions
including 20 stable-success steps. Protected-object motion was effectively
zero under the fixed 5 mm limit.

Cosmos Policy then applied 128 actions to the same snapshot with its released
task embedding and no additional training. It produced no target contact,
moved the target by about 1 nanometre, never came closer than 27.0 cm, and
finished at `[true, false, true]`. The commands were not near-null and included
one gripper-sign transition. The bounded conclusion is therefore not that the
model was inactive, but that this action sequence did not engage the target or
recover the missing predicate despite an oracle horizon of 33 actions.

`bind_libero_known_skill_repair_diagnostic.py` separately maps the exact
residual predicate to the proven privileged push controller. This establishes
a diagnostic known-skill decomposition on this fixture. It does not establish
learned-policy Repair, human approval, governed dispatch, controller ACK, or
physical execution. The publication-safe numeric record is
`docs/agents/evidence/cosmos-policy-libero-seed0-3cm-20260828.json`.

## 13. Compare VLA-0 on the exact admitted 3 cm snapshot

The pinned VLA-0 LIBERO checkpoint was run without additional training on the
same seed-0 snapshot, original instruction, and 128-action ceiling. A dedicated
wrapper admits only the exact 3 cm snapshot digest and the matching v2 oracle
report before loading the model. It leaves the historical publication runner
unchanged.

The preregistered first run admitted one repeat only if the policy contacted
the target or moved it by at least 1 mm. The first run passed both conditions,
so one same-condition repeat was executed. VLA-0 produced an actual terminal
`[true, true, true]` conjunction in both runs:

- run 1: first contact after action 66, conjunction after action 84, and 25.41
  mm maximum target translation;
- run 2: first contact after action 70, conjunction after action 83, and 22.34
  mm maximum target translation.

Neither run breached the declared preservation invariant. The result was not
bitwise deterministic, but the target engagement and terminal predicate
conjunction repeated 2/2. The privileged oracle remained faster, with first
contact after action 32 and predicate success after action 33.

This changes the executor comparison. Cosmos Policy failed to contact or move
the target in its 128-action trial on this snapshot, while VLA-0 contacted the
target and restored the missing predicate twice. Repair behavior is therefore
executor-dependent in this bounded fixture; it is not correct to infer that
all learned executors lack a corrective path.

The authority boundary remains strict. The snapshot restore is a diagnostic
MuJoCo clone, each VLA-0 run stopped on its first observed terminal conjunction,
and stable success within either VLA-0 run was not measured. These runs do not
establish same-world semantic Repair, a general VLA-0 recovery rate, controller
ACK, physical execution, or real-world safety. The publication-safe numeric
record is
`docs/agents/evidence/vla0-libero-seed0-3cm-20260828.json`.

## 14. Require stable success, not a one-step conjunction

A third fixed-condition VLA-0 trial was run to extend the first conjunction by
20 simulator steps. It used the same snapshot, original instruction, pinned
source and checkpoint, process seed, and 128-action ceiling. Unlike the first
two trials, it did not reach a conjunction: it contacted the target after
action 67 and moved it by as much as 14.06 cm, but finished after 128 actions at
`[true, false, true]`. The post-success hold was therefore not admitted. This
is target engagement followed by an incorrect outcome, not policy inactivity.

To test the physical stability of the two earlier terminal conjunctions
without paying for more policy inference, their digest-bound VLA-0 action
traces were replayed from the exact snapshot. Both replays reproduced their
first conjunction at actions 84 and 83. Each then received the same stationary
7D hold used by the scripted oracle: zero arm motion with gripper command `-1`.
Both retained `[true, true, true]` for four complete hold steps, then regressed
to `[true, false, true]` on the fifth. The target moved another 4.88 mm and
5.21 mm during those hold intervals. Protected-pot motion remained below
0.002 mm, far inside the preregistered 5 mm limit.

The evidence must therefore be described in three layers:

- VLA-0 engaged the target in all three policy trials on this fixture;
- a terminal conjunction was observed in two of three policy trials;
- 20-step stable predicate recovery was established in zero of the two
  digest-bound successful-trace replays.

The replay is diagnostic physical-outcome evidence. It invokes no new policy
inference and must not be counted as another VLA-0 policy trial. Conversely,
the fresh third trial did not enter the stability gate and must not be called a
stability failure. Together these results weaken the earlier implication that
VLA-0 had completed Repair on this fixture: the demonstrated capability is
target-directed corrective motion and transient predicate entry, not stable
task completion.

The same bounded executor comparison remains useful. Cosmos Policy produced no
contact and approximately 1 nm of target motion in its trial; VLA-0 repeatedly
engaged and moved the target. But neither executor has established 20-step
stable recovery on this fixture. The publication-safe record is
`docs/agents/evidence/vla0-libero-seed0-3cm-stability-20260829.json`.
