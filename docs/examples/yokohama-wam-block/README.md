# Yokohama: bounded final-block post-training

**Final-block adaptation reduced mean RGB error from 18.35 to 16.46 in a more open urban area. One hold image out of eight passed the numeric criteria; no site passed both hold and forward (0/4). The predeclared minimum remains unmet.** This is offline image adaptation; no native VLA/WAM flight ran.

[Interactive comparison](index.html) · [All eight pairs](contact-sheet.png) · [Japanese report](REPORT-ja.md) · [Previous corridor trial](../yokohama-wam-attention/README.md)

The initial model is the saved attention-v2 adapter on the pinned ANWM checkpoint. This continuation additionally trains the last internal transformer block (`blocks.27`): 24 tensors, **47,851,808/1,134,100,256 parameters (4.219%)**. Other parameters and VAE remain frozen. Fixed settings: 2,048 batch-one AdamW updates, lr 0.00005, weight decay zero, clipping 1, seed 42, native 1,000-step diffusion loss. This is supervised weight adaptation, not LoRA, RL or generated-image editing. Additional training duration is a confound; this is not an ablation isolating one block.

## Evaluation envelope and result

The new test sites are **in a more open part of the city, past the authored delivery endpoint**. Even success here does not resolve the prior narrow-corridor failure, expand the flight route or grant flight authority. The earlier attention trial improved numeric passes from 0/8 to 3/8 but qualified 0/4 sites after manual inspection; those failures remain unchanged.

Twelve unchanged training positions were recollected into 24 pairs. Four new test sites supply eight pairs, at least 19.70232 m from all input/endpoint positions inspected in **both** previous studies. The prior catalogs are hash-linked. Nearby sites and overlapping buildings are not independent environments or unseen-city generalization. All test target images stayed local and were absent from the GPU payload. Cross-study aggregate MAEs must not be compared as matched performance results.

With the same eight new inputs/actions, seed 42 and 250-step sampler before/after, mean RGB MAE was **18.35 → 16.46**, numeric passes **0/8 → 1/8**, sites passing both actions plus manual inspection **0/4**.

| Site/action | Prior adapter MAE | Continued MAE | After pixels with error >40 | Numeric | Manual |
|---|---:|---:|---:|---|---|
| test-v3-12-hold | 16.54 | 15.72 | 2.2% | fail | pass |
| test-v3-12-forward | 17.24 | 16.29 | 2.7% | fail | fail |
| test-v3-13-hold | 17.57 | 16.43 | 1.4% | fail | pass |
| test-v3-13-forward | 20.76 | 19.20 | 6.1% | fail | fail |
| test-v3-14-hold | 19.00 | 14.29 | 2.0% | pass | pass |
| test-v3-14-forward | 20.38 | 16.17 | 4.2% | fail | fail |
| test-v3-15-hold | 16.88 | 15.82 | 2.1% | fail | fail |
| test-v3-15-forward | 18.44 | 17.79 | 6.2% | fail | fail |

Frozen minimum: MAE≤15, ≤10% pixels with any channel error >40, inference≤75 s, no invented route-changing buildings/water. At least one new site must pass both hold and forward. Minor blur or texture differences alone need not fail visual inspection. No Rules superiority is required. All eight outcomes are retained without test-based checkpoint selection or relaxed thresholds. [Measurements and observations](comparison.json).

## Next question

Three of four hold images broadly preserved the scene in manual inspection, while all four forward images retained geometry problems. The next useful preparation is GPU-free collection/qualification of measured RGBD and pose histories during short AP movements plus actual endpoint images, to inspect action-to-geometry alignment. Stationary history, field of view and appearance differences remain unseparated hypotheses. No further GPU attempt was launched.

## E2E / Runtime Verification

Runtime source `a9e293c12cc7396b5ba6d914b9fa250d8aa855d1`. `$SCENE_DEPS` contains optional scene dependencies; `$NUMPY_WHEEL` is the pinned NumPy 2.2.6 cp312 aarch64 wheel. Other variables designate separate experiment directories. [Protocol](protocol.json) binds the exact [executed trainer](train_head.py) and helpers.

```sh
PYTHONPATH=.:"$SCENE_DEPS" python scripts/collect_yokohama_adaptation.py \
  --approve-simulation --plan-version block-v3 \
  --numpy-wheel "$NUMPY_WHEEL" --output-dir "$COLLECTION"
python scripts/prepare_yokohama_adaptation.py --collection "$COLLECTION" --output-dir "$PAYLOAD"
python scripts/train_yokohama_anwm_head.py --root "$STAGED_ROOT"
# Actual execution on the separately cost-bounded GPU VM:
wam-venv/bin/python -u train_head.py --execute-training
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:"$SCENE_DEPS" python -m pytest -q
python docs/examples/yokohama-wam-block/verify_bundle.py
```

CPU boundary: isolated networkless/GPU-free Gazebo → timestamped RGBD/pose joins → later actual views → spatial/role/hash qualification → training-target-only export → teardown. Collection 01 qualified all 32 pairs. A gravity-free camera was teleported; no aircraft motion occurred. Each input uses 16 fresh stationary frames at 4 Hz. Time condition 1 denotes one static commanded viewpoint transition, not measured seconds. [Input audit](input-audit.json).

All eight new source/endpoint positions have horizontal building-footprint gaps 22.12–32.29 m and source-terrain height 15.46–15.57 m. These are point geometry checks, not swept-path or executed-flight clearance. [Geometry preflight](scene-position-preflight.json).

GPU boundary: pinned released ANWM + prior attention adapter → bounded parameter updates → save/reset/reload → finite tensor equality/hash checks → 16 real forecasts → local withheld-image comparison → evidence retrieval → owned-resource deletion. Frozen-parameter hashes stayed unchanged. [Receipt](training-receipt.json), [all updates](training.json), [saved adapter check](saved-head-audit.json). A separate CPU process compared all 24 saved tensors with their actual starting values: released EMA for the last block, prior adapter for the eight final-stage tensors; every tensor changed and remained finite ([audit](parameter-change-audit.json), [executed code](audit_changed_parameters.py)).

Full local suite: **3,369 passed**, three dependency warnings, 102.71 s. The public verifier recomputes crops/errors/hashes and verifies receipts, prior-adapter linkage and separation from both earlier catalogs. It does not rerun GPU training or raw sensor acquisition. Full histories, weights and private cloud configuration are retained separately and excluded from publication. The adapter is not installed in native flight.

Additional estimate **$0.5774**, cumulative **$12.5272/$15**, invoice unconfirmed. Owned GPU VM/disks were confirmed absent. [Closed receipt](budget.json). No VLA invocation, aircraft flight, delivery, sea leg, wind, moving-deck recovery, fleet or energy-saving claim. AP remains the sea-leg controller; judgment, human approval, Rules, dispatch and observed outcomes stay separate.

Source: Yokohama / Project PLATEAU, 2024 public catalog, processed, CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md) · [Maintainer contract](../../agents/yokohama-block-adaptation.md).
