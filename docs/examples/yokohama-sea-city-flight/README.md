# Stationary ship, AP sea legs, native city decisions, and return

One PX4/Gazebo sortie completed ship takeoff, 1 km AP-only sea transit, a 400 m authored city connector, two fresh native AeroVLA/ANWM movements, delivery-waypoint hold, AP return, ship landing and disarm. Eleven 30-second holds passed. This is simulation; payload release/receipt is absent.

[Replay and model images](index.html) · [actual camera video, 8×](onboard-timelapse.mp4) · [Japanese report](REPORT-ja.md) · [result](mission-result.json)

Run `yokohama-f17245dce007` at source `51d6de5962fb1f70cc53e85bfb06fd8a85449042`: 6,989 trajectory points, 3501.39 m, 1577.46 simulator seconds. Two real-observation VLA calls and four native adapted ANWM forecasts were generated in this sortie. Predictions from the earlier city trial were not replayed.

Models start/warm up only after the first inland hold; phase and measured position both gate calls. Models stop after the second short segment, before the AP remainder. Both sea legs contain zero model requests. The lifecycle, requests, permits, arrivals and final deck contact are separately checked. The main route is authored; VLA selects parameters for two preapproved short translations. Independent mapped-geometry Rules remain in control of admission.

The coastal gateway is an authored boundary 400 m from D1, beyond the original crop, not a surveyed shoreline. The static deck lies another 1,000 m offshore. Wind is zero. No moving ship, payload release/receipt, strong-wind qualification, fleet scheduling, hardware or energy-savings claim. Image gates remain unchanged and test visible-shape consistency, not general obstacle recognition or hidden free space. Heading conversion uses measured simulator ground truth.

## E2E / Runtime Verification

The first CPU attempt failed at its second model segment because the city world height was used as offshore-home-relative altitude, retaining about 0.19 m error. It is preserved in [retained failures](retained-failures.json). Observed PX4 global-minus-home altitude is now mapped to the unchanged world goal; the 0.15 m vertical bound is unchanged. The revised full CPU fixture sortie passed before the native trial. Both verifiers cover the same real simulator trajectory; the portable bundle verifier reopens exported facts and image gates without running models.

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --decision-backend native --native-service-config /secure/native-service-config.json \
  --wam-profile motion-v4 --output-dir /tmp/yokohama-sea-city-native --timeout-seconds 2500
python scripts/verify_yokohama_sitl.py /tmp/yokohama-sea-city-native \
  --output /tmp/yokohama-sea-city-native/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-sea-city-native \
  --output /tmp/yokohama-sea-city-native/decision-verification.json
python docs/examples/yokohama-sea-city-flight/verify_bundle.py
```

This attempt: $0.9349; cumulative: **$15.6450/$17**, estimated, invoice unconfirmed. Owned GPU VM and disks deleted. [Cost](cost.json), [protocol](protocol.json), [maintainer contract](../../agents/yokohama-sitl.md). Earlier city successes/failures remain in [their original report](../yokohama-integrated-flight/REPORT-ja.md).

Next: connect the delivery waypoint to observed cargo separation, landing and receipt. Geometry: Yokohama / Project PLATEAU; [attribution](../yokohama-urban-scene/ATTRIBUTION.md).
