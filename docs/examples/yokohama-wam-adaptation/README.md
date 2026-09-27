# Yokohama: bounded ANWM output-head post-training

**Small supervised post-training reduced mean RGB error from 36.78 to 19.66, but building/corridor geometry remains wrong. No evaluation site passed the minimum condition (0/4); flight use remains unqualified.** No native VLA/WAM flight was admitted or performed in this follow-up.

[Interactive before/after comparison](index.html) · [All eight held-out pairs](contact-sheet.png) · [Japanese report](REPORT-ja.md) · [Input audit](input-audit.json)

The released model has 1,134,100,256 parameters. Only 2,692,256 (0.237%) in its final fusion/output layers were trained; the VAE and remaining model parameters stayed frozen. This is supervised post-training, not LoRA or reinforcement learning. Fixed settings: 512 batch-one updates, AdamW lr 0.0001, gradient clipping 1, seed 42, native 1000-step diffusion training loss. The saved head was reset/reloaded before evaluation; no test-based checkpoint selection occurred.

The CPU renderer recorded 16 fresh stationary RGBD frames per location, a later hold view and a view 2.8 m forward. Twelve training locations supply 24 targets; four separate evaluation locations supply eight withheld targets. Evaluation images were never uploaded to the GPU. Buildings visible across areas may overlap: this is within-city adaptation, not unseen-city generalization. Teleporting a gravity-free camera is data rendering, not executed aircraft motion.

## Input findings and result

The pinned upstream notebook specifies 512-square / 90° horizontal-FOV input. Current input is 640×360 / 60°; after the shared crop the horizontal FOV is about 46.83°. RGB, resize, normalization, VAE scale, translation normalization and the released checkpoint's 16 context slots already match. The public YAML's four context frames do not match this released checkpoint. Upstream relative time is a row-index difference divided by 128, not verified seconds. This pilot fixes it to one commanded static viewpoint transition and adapts to the current camera/appearance. These observed differences do not uniquely prove the original failure cause. [Maintainer contract and primary sources](../../agents/yokohama-adaptation.md).

The same eight inputs, commands, random seed and 250-step sampler were evaluated before and after learning. Numeric passes: **0/8 → 0/8**. Sites passing both actions and manual inspection: **0/4**.

| Site/action | Before MAE | After MAE | After pixels with error >40 | Numeric criterion |
|---|---:|---:|---:|---|
| test-12-hold | 36.18 | 19.18 | 10.6% | fail |
| test-12-forward | 35.73 | 21.45 | 17.9% | fail |
| test-13-hold | 41.86 | 19.72 | 15.3% | fail |
| test-13-forward | 34.60 | 21.28 | 16.9% | fail |
| test-14-hold | 38.90 | 17.98 | 10.5% | fail |
| test-14-forward | 35.62 | 16.95 | 9.9% | fail |
| test-15-hold | 37.81 | 18.46 | 13.9% | fail |
| test-15-forward | 33.59 | 22.26 | 19.6% | fail |


MAE is mean RGB absolute error (0–255) against the later actual view. The frozen bounds remain MAE ≤15, at most 10% of pixels with any channel error >40, and inference ≤75 s, plus visual rejection of invented route-changing structure. A limited feasibility result requires at least one evaluation site to pass both actions; this criterion was fixed before GPU execution. All eight outcomes are retained. It never automatically qualifies or dispatches flight, and it does not retroactively change the earlier 12 failed forecasts.

## E2E / Runtime Verification

Training source `f4b160135622e1b9a5f1c26991d0bb8f8c87db85`. `$COLLECTION`, `$PAYLOAD` and `$STAGED_ROOT` are separate new directories. `$NUMPY_WHEEL` is the pinned NumPy 2.2.6 cp312 manylinux aarch64 wheel; `$SCENE_DEPS` contains the optional local scene dependencies. The GPU command runs the exact [archived trainer](train_head.py) and hash-bound protocol, with evaluation targets absent. Cloud credentials and lifecycle configuration are not published.

```sh
PYTHONPATH=.:"$SCENE_DEPS" python scripts/collect_yokohama_adaptation.py \
  --approve-simulation --numpy-wheel "$NUMPY_WHEEL" --output-dir "$COLLECTION"
python scripts/prepare_yokohama_adaptation.py --collection "$COLLECTION" --output-dir "$PAYLOAD"
python scripts/train_yokohama_anwm_head.py --root "$STAGED_ROOT"
# Explicit paid execution on a separately bounded L4 VM:
wam-venv/bin/python -u train_head.py --execute-training
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:"$SCENE_DEPS" python -m pytest -q
python docs/examples/yokohama-wam-adaptation/verify_bundle.py
```

Observed CPU boundary: isolated networkless Gazebo → actual timestamped RGBD/pose joins → spatial split and later-view qualification → hash/role checks → train-target-only export. Collection 04 passed 32 pairs; minimum train/test camera separation was 18.3987 m, with no GPU device and confirmed teardown. Earlier missing-NumPy and zero-pose-envelope timestamp failures are retained in [collection-attempts.json](collection-attempts.json). No PX4 or flight ran.

Observed GPU boundary: pinned released ANWM → frozen-base head updates → saved adapter → reset/reload → identical-seed evaluation → host-side comparison to withheld targets → evidence collection and owned VM/disk deletion. 512 updates, 16 actual forecasts; unchanged frozen-parameter hash and changed head hash. [Training receipt](training-receipt.json), [all training steps](training.json), [protocol](protocol.json).

Full local suite: **3,363 passed, three dependency warnings**, 102.66 s. The public verifier recalculates image crops/errors/hashes and checks training sample membership. It does not rerun training or independently re-open the full raw sensor archive. Weights, raw histories and private cloud configuration are not in the public bundle; the trained head is retained separately with a recorded hash.

## Cost and limits

Additional estimate **$0.5112**, cumulative **$11.3894 / $15**, invoice unconfirmed. Owned GPU VM/disks were confirmed absent. [Closed receipt](budget.json). No VLA call, real-model flight, delivery, sea leg, dynamic obstacle, moving deck, fleet operation or energy-saving claim. The next candidate is bounded adaptation of the final projection-attention layer as well as the output head, assessed on newly collected evaluation sites. These eight inspected pairs would become development data for any follow-up design. This is an untested hypothesis; see the [next learning proposal (Japanese)](NEXT-ja.md). Flight using fresh observations remains a separate milestone after structural qualification.

City source: Yokohama / Project PLATEAU, 2024 public catalog, processed, CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md).
