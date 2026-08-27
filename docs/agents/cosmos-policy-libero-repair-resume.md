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
