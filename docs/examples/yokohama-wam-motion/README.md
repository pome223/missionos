# Yokohama WAM: continuation using observed AP motion histories

The CPU AP acquisition completed seven measured holds, return, landing and disarm. A bounded 2,048-update continuation using real simulated motion histories produced mean RGB MAE **10.29 → 9.36**, numeric passes **4/5 → 4/5**, and combined numeric/visual qualification **4/5**. The fixed minimum requires all five cases. **Native VLA/WAM-controlled flight remains unqualified.**

[Japanese report](REPORT-ja.md) · [interactive comparison, actual camera clips and measured AP route](index.html) · [all five image comparisons](contact-sheet.png)

## Scope

The 1,639-point AP route measured 688.11 m over 551.224 simulator seconds. Seven 30-second holds, the geometry checks, return/landing/disarm and teardown passed. The first development collection failed at its 200 MiB storage bound and is retained. Lossless byte-plane depth compression and fixed spatial capture windows allowed the second flight to finish; flight controls and pose thresholds were unchanged. This was city-only collection, without sea flight or payload transfer.

327 RGBD/pose frames at 4 Hz yielded 36 qualified forward and seven hold windows. A target must match the past-only requested view at exactly +1 observed simulator second within 0.30 m / 0.05 rad. The fixed grid uses 16 past frames and the fourth subsequent frame as outcome. Offline prediction candidates were not separately dispatched AP commands. Histories overlap and are correlated.

Before model work, pose screening fixed 12 training cases (10 forward, two nearby holds) and five test cases (four forward, one hold). The first delivery loiter frames were still settling, so training holds come from D2. Histories and requested endpoints are separated by 19.78 m, including a 0.6 m reserve for actual endpoint errors. No test future frame occurs in any uploaded input history. Test target pixels and actual future poses remained local. This is previously inspected city geography, not an unseen-scene generalization claim; all historical adapter provenance and failed studies remain linked in the protocol.

The prior block adapter supplies the initial 24 tensors. Another 2,048 AdamW updates at lr 0.00005, seed 42, update 47,851,808 parameters (4.219%). The remaining parameters and VAE are frozen. Both evaluations use the same inputs, actions, seeds and sampler; fixed final checkpoint selection avoids choosing by test results. Saved/reset/reloaded tensors were checked, and a separate CPU process confirmed all 24 changed. Runtime training took 230.76 seconds; all model work took 574.95 seconds. These outcomes do not isolate the causal contribution of changing histories, nor compare means across different earlier cohorts.

All five must meet MAE≤15, ≤10% pixels with maximum-channel error>40, inference≤75 s and visual preservation of route-relevant occupied geometry. Minor blur/texture differences alone need not fail, and superiority to ideal Rules is not required. Hold and forward cases are distinct recorded trajectories, not same-anchor counterfactual pairs. Time index 1 is tested against +1 observed second only within this dataset; general physical-time calibration is unproven.

Numeric passes were already 4/5 before this continuation. The approximately 9.0% mean-error reduction did not increase that count. The unresolved second forward case has an ambiguous near-left-wall boundary and 10.19% large-error pixels. It is retained without excluding it or relaxing the 10% limit. The next engineering question is how to handle uncertain near-field occupied boundaries and stop rather than dispatch from an ambiguous prediction. No further GPU attempt was made.

## E2E / Runtime Verification

CPU capture source `13f4b785`; frozen training source `d2b36e72509ed5f0725df85e2ecc411758f95351`. Variables denote task-owned output directories and optional scene dependencies.

```sh
PYTHONPATH=.:"$SCENE_DEPS" python scripts/yokohama_sitl.py \
  --phase flight --capture-motion-views --approve-sitl \
  --output-dir "$FLIGHT" --timeout-seconds 1200
PYTHONPATH=.:"$SCENE_DEPS" python scripts/verify_yokohama_sitl.py "$FLIGHT" --output "$SITL_CHECK"
PYTHONPATH=. python scripts/verify_yokohama_motion.py --root "$FLIGHT" --output "$MOTION_CHECK"
PYTHONPATH=. python scripts/prepare_yokohama_motion.py --root "$FLIGHT" \
  --output-dir "$PAYLOAD" --heldout-dir "$LOCAL_TEST_TARGETS" --sitl-verification "$SITL_CHECK"
python scripts/train_yokohama_anwm_head.py --root "$STAGED_ROOT"
# Only on the separately cost-bounded, opt-in L4 VM:
wam-venv/bin/python -u train_head.py --execute-training
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src:"$SCENE_DEPS" python -m pytest -q
python docs/examples/yokohama-wam-motion/verify_bundle.py
```

The exercised boundary is actual PX4/Gazebo AP motion → recorded camera/pose synchronization → full flight verification → past-only payload export → real ANWM continuation and 10 forecasts → withheld local target comparison → evidence retrieval → owned VM/disk deletion. CPU acquisition has no model inference; GPU training runs after the flight and has no flight authority. Local full suite: **3,389 passed**, three dependency warnings, 106.21 s.

The public verifier recomputes image metrics, crops, hashes and train/test history separation and checks training-membership, parameter and CPU receipts. It does not rerun GPU training or independently inspect all private raw sensor bytes. Model weights and the complete raw RGBD archive remain private. Actual clips are simulator camera recordings; the route is a measured position replay. They are not model-generated videos or learned-control evidence.

Additional estimate **$0.5443**, cumulative **$13.0714/$15**, invoice unconfirmed. Owned GPU VM and disks were deleted and absence confirmed. No VLA invocation, learned-model mission, payload delivery, sea leg, strong wind, moving deck, fleet, physical execution or energy-saving claim. The trained adapter is not installed in flight. [Budget receipt](budget.json) · [maintainer contract](../../agents/yokohama-motion-capture.md)

Source: Yokohama City / Project PLATEAU (2024 release catalog), processed, CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md).
