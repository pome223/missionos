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

`probe_libero_displacement_curriculum.py` constructs diagnostic fixtures on a
protected-separating horizontal ray from the successful target position. It
rejects predicate-unstable points, object jumps, protected-pot motion, and
translations above 2 cm. Source dataset metadata is not predicate authority;
the restored official LIBERO environment is.

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
