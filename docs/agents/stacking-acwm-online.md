# Online ACWM prediction contract

The opt-in `StackingACWMPredictor` adapter sends a pre-approach observation to an
explicitly supplied inference backend. The backend used in the accompanying
benchmark ran the fine-tuned ACWM VideoDiT and Wan VAE on NVIDIA L4. Importing the
adapter starts no service, downloads no model and executes no robot action.

## Observation and option

The input schema is `stacking.current_image_macro.v1`. Exactly seven state fields
are accepted. Additional fields, including saved future actions and outcome
labels, are rejected before the backend is invoked.

| Field | Shape | Meaning at the decision gate |
|---|---|---|
| `image` | 240 × 240 × 3 | Current RGB, integer values 0–255 |
| `objects` | 10 × 7 | Current xyz positions and unit quaternions (wxyz) |
| `velocity` | 10 × 6 | Current MuJoCo free-joint velocity values |
| `physics` | 10 × 11 | Full edge lengths, mass, three friction values, center-of-mass offset, box shape code |
| `robot` | 12 | Current end-effector pose (7), gripper joint positions (2), commanded target xyz (3) |
| `count` | scalar | Proposed next placement count, 1–10 |
| `plan` | 12 | Preselected target xyz and registered macro parameters |

The registered macro is `smolvla-physical-tower-placement-284-v1`. Its parameter
tail is the existing stacking adapter's `MACRO`: approach offset, release
clearance, phase lengths and controller rate. It is a staged-grasp simulator
macro, with SmolVLA inference for its learned motion phases.

The supported request has one option: `continue`, horizon **14.2 seconds**,
parameters `macro` and `readout_sha256`. This checkpoint does not provide a
separate bank forecast. A second option or a changed horizon is unavailable.

At the neural backend, the current object/velocity/physics/robot/count arrays are
flattened to 253 pre-action state/context values. Thirty-seven 7-dimensional tokens encode
placement target xyz, normalized nominal time, macro identifier, approach offset
and clearance. These tokens specify the planned macro rather than its eventual
motor commands. The current image is encoded independently. Measured future
frames are supervised training targets only.

## Backend and binding

Construction requires an explicit backend callable plus SHA-256 digests for the
model, readout and frozen SmolVLA policy. The backend receives validated current
arrays. It returns:

- `model_sha256`, `readout_sha256`;
- `macro_id`, `horizon_seconds`;
- `readout_output`, a finite classifier output in [0, 1];
- `invocation_id`, identifying saved generated video and inference evidence.

The adapter rejects mismatched artifact identities, macro, horizon or invalid
scores. `PredictionRegistry` applies the common request/binding/freshness checks.
A successful forecast records `verification_basis=model_inferred` and retains
the generation identifier in `future_state`.

The score is an **uncalibrated visual classifier output**. It is neither a
calibrated physical probability nor an approval. The benchmark's fixed lab policy
banks when the score is at least 0.5. This decision policy is outside Prediction
Core. Registry receipts preserve the existing authority-free fields: no approval,
dispatch authority, physical execution or completion claim is created.

## Runtime evidence requirements

A claim of online use requires more than an input schema:

1. Save the current observation and its identity at the pre-approach gate.
2. Complete real future generation and readout, and save the Prediction receipt.
3. Commit the lab decision before starting the next actual placement.
4. Generate actual SmolVLA chunks after that commitment. Record their invocation
   identifiers and request/response times.
5. Execute and measure the selected branch. After a bank, run any counterfactual
   from its saved prestate only after the actual bank branch has finished.
6. Apply the common terminal hold and derive the score from measured collapse.

The published benchmark follows this sequence. Its simulator loop is an opt-in
research harness. It does not add an automatic forecast-to-approval route or a
new LLM Assurance/Executor ticket integration.

## Verification

Run the contract tests from the repository root:

```sh
PYTHONPATH=packages/missionos-core/src:. python -m pytest tests/contract/test_stacking_acwm.py -q
```

The [benchmark verification script](../assets/acwm-online-stacking-20260921/verify_report.py)
recomputes published game scores, confusion matrices and common-prefix checks
from individual records. The `--statistics` option additionally regenerates the
paired bootstrap and sign-flip results using NumPy. Its scope is verification of
released evidence; executing the physical simulator and neural checkpoints
requires the separately provisioned research runtime.
