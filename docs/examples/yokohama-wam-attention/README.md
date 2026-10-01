# Yokohama: bounded projection-attention post-training

**Projection-attention adaptation reduced mean RGB error from 18.29 to 14.98. Numeric passes increased to 3/8, but ambiguous corridor appearance failed manual qualification: 0/4 sites met the full minimum.** This is offline image adaptation; no native VLA/WAM flight ran.

[Interactive comparison](index.html) · [All eight pairs](contact-sheet.png) · [Japanese report](REPORT-ja.md) · [Previous head pilot](../yokohama-wam-adaptation/README.md)

The initial model is the prior 512-update head on the pinned released checkpoint. This follow-up adds the final projection-attention layer to the trainable fusion/output layers: 8,005,280 / 1,134,100,256 parameters (0.706%). Everything else, including the VAE, stays frozen. Fixed settings: 2,048 batch-one AdamW updates, lr 0.00005, weight decay zero, gradient clipping 1, seed 42, native 1,000-step diffusion training loss. This is supervised post-training, not LoRA, RL or image post-processing. Because training duration also changes, this is not a causal ablation isolating attention.

Twelve unchanged training positions were recollected into 24 fresh target pairs. Four new evaluation sites supply eight pairs, with at least 16.17681 m separation from **all** previously inspected source/endpoint positions, including the old evaluation sites. The old site catalog is hash-linked. The new sites are close together on one corridor and buildings may overlap across views: no independent-scene or unseen-city generalization claim. All evaluation targets stayed local and were absent from the GPU payload.

Each site has 16 fresh stationary RGBD frames at 4 Hz, a later hold target and a settled camera view 2.8 m forward. Camera teleportation is rendering, not executed aircraft motion. The time condition remains one static commanded viewpoint transition, not measured seconds. The input convention is unchanged from the [previous audit](input-audit.json).

## Result

The same eight new inputs/actions, seed 42 and 250-step sampler were used before and after this continuation. The before model already includes the previous head adaptation. Mean RGB MAE: **18.29 → 14.98**. Numeric passes **0/8 → 3/8**; sites passing both actions plus manual inspection **0/4**.

| Site/action | Previous head MAE | Continued MAE | After pixels with error >40 | Numeric criterion |
|---|---:|---:|---:|---|
| test-v2-12-hold | 16.90 | 13.09 | 8.6% | pass |
| test-v2-12-forward | 16.50 | 13.87 | 8.4% | pass |
| test-v2-13-hold | 17.40 | 14.80 | 9.8% | pass |
| test-v2-13-forward | 17.13 | 15.01 | 10.0% | fail |
| test-v2-14-hold | 18.50 | 15.19 | 10.4% | fail |
| test-v2-14-forward | 19.61 | 16.50 | 11.6% | fail |
| test-v2-15-hold | 20.44 | 15.52 | 11.0% | fail |
| test-v2-15-forward | 19.84 | 15.84 | 11.2% | fail |

Frozen minimum: MAE≤15, no more than 10% of pixels with any channel error >40, inference≤75 s, and visual rejection of invented route-changing buildings/water. At least one site must pass both hold and forward. No Rules superiority is required. All eight outcomes are retained; no test-based checkpoint selection or threshold adjustment occurred. This result never automatically qualifies flight. [Measurements and manual observations](comparison.json).

## E2E / Runtime Verification

Source `6db04f3b0520b4a9b86c9e8676340d49298fdf13`. `$SCENE_DEPS` contains optional local scene dependencies; `$NUMPY_WHEEL` is the pinned NumPy 2.2.6 cp312 aarch64 wheel. Other variables refer to separate new experiment directories. The staged [executed trainer](train_head.py) and helpers are hash-bound by [protocol](protocol.json).

```sh
PYTHONPATH=.:"$SCENE_DEPS" python scripts/collect_yokohama_adaptation.py \
  --approve-simulation --plan-version attention-v2 \
  --numpy-wheel "$NUMPY_WHEEL" --output-dir "$COLLECTION"
python scripts/prepare_yokohama_adaptation.py --collection "$COLLECTION" --output-dir "$PAYLOAD"
python scripts/train_yokohama_anwm_head.py --root "$STAGED_ROOT"
# On the bounded GPU VM, with the hash-bound staged files:
wam-venv/bin/python -u train_head.py --execute-training
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:"$SCENE_DEPS" python -m pytest -q
python docs/examples/yokohama-wam-attention/verify_bundle.py
```

CPU boundary: isolated networkless, GPU-free Gazebo → fresh timestamped RGBD/pose joins → actual later images → spatial/role/hash qualification → train-target-only export. Collection 01 passed all 32 pairs and cleanup. No PX4/aircraft movement occurred.

GPU boundary: pinned released ANWM + previous saved head → bounded trainable tensors → 2,048 real updates → save/reset/reload → finite tensor equality and hashes → 16 actual forecasts → local comparison against withheld targets → evidence retrieval and owned-resource deletion. [Receipt](training-receipt.json), [all updates](training.json), [reopened adapter](saved-head-audit.json). A separate CPU process also reopened the saved adapter and compared all eight tensors with their actual starting values ([audit](parameter-change-audit.json), [executed code](audit_changed_parameters.py)); each changed and remained finite. The public verifier checks the records and recalculates pixels and hashes; it does not independently rerun GPU training or raw sensor verification.

Full local suite: **3,367 passed**, three dependency warnings, 103.34 s. Weights, full raw sensor archives and private cloud configuration are retained separately and excluded from publication. The changed adapter is bound to the original checkpoint, prior adapter and current protocol hashes. It is not installed in native flight.

## Budget and limits

Additional estimate **$0.5604**, cumulative **$11.9498 / $15**, invoice unconfirmed. Owned GPU VM and disks were confirmed absent. [Closed receipt](budget.json). No VLA invocation, flight, delivery, sea leg, wind, moving-deck recovery, fleet or energy-saving claim. AP remains the sea-leg controller; model proposals, human approval, Rules, dispatch and observed outcomes stay separate.

City source: Yokohama / Project PLATEAU, 2024 public catalog, processed, CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md). [Maintainer contract](../../agents/yokohama-attention-adaptation.md).
