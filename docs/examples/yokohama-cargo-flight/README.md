# Simulated cargo delivery and ship return with native city models

One stationary-ship PX4/Gazebo sortie carried a 50 g dynamic box across a 1 km AP-only sea leg, used two fresh AeroVLA/ANWM city decisions, released the box above the delivery pad, observed pad contact and rest, accepted an independent simulated receiving-station receipt, and returned to deck landing/disarm. All thirteen 30-second holds passed. This is simulated receipt, not a human recipient or physical-world delivery.

[Replay](index.html) · [actual delivery camera, real time](cargo-delivery.mp4) · [actual onboard camera, 8×](onboard-timelapse.mp4) · [Japanese report](REPORT-ja.md) · [result](mission-result.json)

Run `yokohama-6acc29204d62` at source `819b90c13ffc03a5fbd3773aeecb91278e988380`: 3529.33 m, 1687.43 simulator seconds, 2 real-observation VLA calls and 4 fresh adapted ANWM forecasts. Cargo rested for 3.344 seconds, 0.013 m from pad centre. No cargo teleport, respawn or scripted fall. Commands, detachment, contact/rest, receipt and return are separately verified.

Models start only at the approved inland hold and stop before the AP remainder. Both sea legs have zero model requests. The main route and coastal boundary are authored; models choose two preapproved short translations. A 400 m connector lies between the virtual coast and D1. Existing motion-v4 weights are reused without new training. Independent geometry Rules and unchanged image/arrival thresholds remain in force.

## E2E / Runtime Verification

The full CPU fixture qualification passed first. The first native attempt stopped before inference because a copied lifecycle wrapper targeted a deleted old VM. [This failure is retained](retained-failures.json). A single resource manifest and pre-allocation binding check fixed the configuration; the recovery native sortie passed flight, decision and cargo verifiers. The affected boundary includes observed camera input, model proposals, Rules, MAVLink execution, physical simulated cargo separation, pad contact/rest, receipt-gated return, deck contact, landing and disarm. The portable verifier rechecks exported evidence, not inference or all private raw logs.

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --deliver-payload --decision-backend native \
  --native-service-config /secure/native-service-config.json --wam-profile motion-v4 \
  --output-dir /tmp/yokohama-cargo-native --timeout-seconds 2500
python scripts/verify_yokohama_sitl.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/decision-verification.json
python scripts/verify_yokohama_payload.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/payload-verification.json
python docs/examples/yokohama-cargo-flight/verify_bundle.py
```

100 focused runtime tests and 6 lifecycle CLI tests passed. The old resource configuration is rejected before cloud allocation. Previous [cargo-free results and retained failures](../yokohama-sea-city-flight/REPORT-ja.md) remain unchanged. [Maintainer contract](../../agents/yokohama-payload-delivery.md), [cargo verification](verification-payload.json), [receipt](payload-receipt.json).

Estimated cargo-stage total $1.0612, including the retained pre-inference failure ($0.5486); cumulative **$16.7063/$17**; invoice unconfirmed. Owned VM/disks deleted. [Cost](cost.json).

Limits: one drone, zero wind, static buildings/deck, simulated receiver, no packaging-integrity, moving-deck, fleet, hardware or energy-savings validation. Heading conversion uses simulator ground truth. WAM checks visible-shape consistency, not general obstacle recognition or hidden free space. Replay markers are enlarged observed-position interpolations; video is actual camera footage. [PLATEAU attribution](../yokohama-urban-scene/ATTRIBUTION.md).
