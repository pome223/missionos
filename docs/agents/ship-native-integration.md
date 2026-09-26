# Native VLA and WAM in one flight

Both explicit loopback endpoints select a sequential native integration:

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case static_center --urban-policy onboard_anwm_static \
  --aerovla-url http://127.0.0.1:18117 --anwm-url http://127.0.0.1:18118 \
  --approve-sitl --timeout-seconds 900 --output-dir /tmp/new-native-joint-flight
```

The stationary cases and existing absolute bounds remain unchanged. AeroVLA
uses a fresh paired image and completes one guarded maneuver with observed
settling and LOITER handoff. Only then does the collector start a new 4 Hz RGBD
history. ANWM predicts candidate views, MissionOS proposes a route, and fresh
observations constrain dispatch. Delivery and recovery retain their independent
contact, stability and trajectory checks. Neither model grants approval.

The downward camera runs at 20 Hz when capture and the VLA executor coexist,
preserving exact timestamp joins for 10 Hz VLA images and 4 Hz WAM inputs.
It does not interpolate missing frames. Existing standalone modes retain their
original sensor rates. The first eight capture bundles are retained startup
data, followed by twenty uninterrupted bundles, with only sixteen sent to WAM.

The integration verifier requires both independently verified native chains,
one run/world/plan, a strictly ordered handoff/capture/decision/dispatch sequence,
and WAM sensor timestamps after the post-VLA LOITER observation. A fixture or
logged unused forecast cannot satisfy the combined proof. Verification of this
chain is separate from whole-mission completion.

## One-GPU operation

Start ANWM with `--cpu-between-requests` to keep its weights in host memory
between requests. Its transfer to CUDA, actual prediction and return to CPU
all count toward the existing 75-second host exchange limit. Exceptions also
release the model allocation. Model precision, weights, seed and diffusion
steps remain unchanged.

Start AeroVLA with `--exit-after-request`. After its one successful response is
flushed, the process exits and releases CUDA memory. Before each new flight,
start a new service, complete its synthetic warmup and bind its new identity in
the approved mission plan. No service is restarted during a flight to replace
a rejected output. Both options are explicit and default off.

## Qualification and scope

Freeze the source, service configuration, model identities, three stationary
cases and numeric bounds before paid flight trials. Require all three 100 m
offshore cases to complete within 900 wall seconds, followed by a separately
frozen 1,000 m offshore extension with the same 900-second bound. Preserve all
attempts and reject failure-driven model retries. Rules superiority is not a
gate. The VLA age limit remains two seconds, target error 0.25 m and altitude
error 0.15 m; the [WAM absolute bounds](ship-anwm-static.md) remain unchanged.

This qualifies sequential participation in one stationary-ship simulator
mission. It does not establish continuous joint learned control, VLA-generated
whole routes, moving-obstacle forecasts, deployable odometry, real strong-wind
performance, moving decks, ten-aircraft coordination or physical delivery.
Current sensor/material assumptions and simulator-based VLA clearance remain
explicit. Cloud startup requires the existing user budget and opt-in approval;
the CLI never provisions compute.
