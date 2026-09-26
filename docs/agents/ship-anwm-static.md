# Native WAM use during a stationary urban hold

`run-sitl --urban-policy onboard_anwm_static --anwm-url http://127.0.0.1:18118`
opts into a native ANWM service. Only `static_center`, `static_near` and
`static_clear` are admitted. The aircraft and sensors run in PX4/Gazebo. The
mapped 16 m wide obstacle is fixed at east 0, 8 or 24 m. This is an explicitly
stationary candidate-view task, not dynamic-obstacle forecasting.

## Absolute requirements

The qualification protocol is frozen before prospective runs: prediction centre
error at most 5 m, candidate-position disagreement at most 5 m, model exchange
at most 75 wall seconds and stationary-scene movement at most 1.5 m. The worker
holds for at most 90 seconds while exchanging a proposal. Fresh dispatch
validation is at most two seconds old. All three prespecified flights must
complete delivery and stable deck recovery with no observed or swept-geometry
contact for this bounded demonstration. All attempts and failures remain in the
denominator. No claim about field reliability follows from three flights.

A 5 m prediction envelope is reserved inside the existing 14 m centre-clearance
rule; it is not an observation or controller tolerance change. Both native view
predictions must put the obstacle centre at least 19 m to the right to propose
the direct route. Fresh image clearance remains mandatory. A conservative detour
is acceptable. Superiority over a rule, a changed action, or a faster mission is
not required.

## Prediction and use

The existing collector records twenty synchronized 4 Hz RGBD/downward-image and
pose bundles while the aircraft holds. Only the first sixteen RGBD/pose samples
reach ANWM. The final four samples remain local. Ego pose is explicitly Gazebo
pose, not deployable onboard odometry. Raw inputs, hash bindings and this
limitation remain visible.

Before that sequence, the collector retains and excludes the first eight
complete 4 Hz bundles as a fixed startup interval. New Gazebo subscriptions
can initially render geometry without materials. This exclusion is independent
of pixels, obstacle position and model outputs; no gaps are repaired inside the
twenty-frame sequence. The initial three-run cohort retained a pre-inference
rejection caused by an unrendered first frame. Qualification after this repair
is a separate cohort and must not replace that failed attempt.

The released checkpoint, upstream revision, 250 diffusion steps, seed and the
hold/right-5-metre candidates are unchanged from the native pilot. The service
loads once, binds to IPv4 loopback and receives no aircraft control connection.
Each inference has a new request ID, exact input hashes and a pinned service
identity. Its output never grants dispatch authority.

A substantial vertical red/orange region is extracted from each predicted candidate
view. The native decoder can change saturated red into pale orange. The host
requires R > 80, R - G >= 30 and R - B >= 45 in 8-bit RGB, with at least 55%
support across the central vertical band. This is a restricted color/material
contract, not general object recognition. It was calibrated after a preserved
post-warmup model-response rejection; that response is development evidence,
not prospective qualification of the revised decoder.
Isolated warm-colored texture is ignored; missing, clipped or ambiguous regions
are rejected. Candidate camera pose, crop convention, known intrinsics and the
mapped obstacle plane yield a position estimate. Both candidate views constrain
one direct-versus-detour proposal; the right-5-metre candidate is not itself a
flight instruction and does not certify the whole detour.

Raw input RGB and fresh onboard observations use the same widest-band red-box
decoder, with the selected vertical pixel included in the ray. Generated views
retain the separate vertical-support/color decoder above. The raw-image repair
followed a preserved 2.057 m perception error when a foreground building hid a
lower edge. The existing 2 m perception bound and 5 m prediction bound remain
unchanged. Synthetic calibration and reprocessing old frames are development
checks; new flights must qualify the repaired implementation separately.

The native checkpoint's four-step temporal interpretation remains uncalibrated.
Its generated view is used only under a stationary-scene contract, after checking
observed scene continuity throughout the hold and fresh images before dispatch.
A 75-second response is not called a fresh one-second dynamic prediction.
Moving-obstacle prediction requires a separate temporal qualification.

## Verification and reproduction

The host verifies native outputs against fresh onboard observations. Independent
verification subsequently checks the same prediction against simulator position,
reopens HTTP/input/image artifacts and retains the existing plan, actual motion,
parcel/contact/stability and recovery checks. Rejection holds the aircraft and
is retained as an unsuccessful native path; no rule result replaces it.

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case static_center --urban-policy onboard_anwm_static \
  --anwm-url http://127.0.0.1:18118 --approve-sitl \
  --output-dir /tmp/new-anwm-static-flight
```

The CLI never creates cloud resources. GPU and service startup require separate
existing authorization, an explicit cost cap and deletion after evidence
collection. The [same-flight mode](ship-native-integration.md) combines the
related AeroVLA maneuver with this WAM mode through new post-handoff history.
Separate runs do not establish that combined runtime proof.

The [bounded experiment report](../examples/ship-anwm-static-report.md) records
the final three short-distance cases, a separate one-kilometre extension, and
all unsuccessful development cohorts. Raw evidence stays outside the repository.
