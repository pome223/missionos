# Yokohama withheld-view diagnostic

The opt-in CPU capture extends the city fixture flight with **evaluation-only**
RGBD streams. It does not enable learned control. Independent geometry Rules,
fresh authorization, PX4 mission ACK, measured arrival, and session revocation
retain their existing meanings.

## Capture boundary

Run `scripts/yokohama_sitl.py --phase flight --decision-backend fixture
--capture-paired-views --approve-sitl --output-dir <new-directory>` with the
optional scene dependencies installed. Native and contact modes reject the
paired-capture flag: the bounded in-memory sensor history cannot guarantee the
first post-input frame after a slow native call.

At D1 and D2, record the existing 24-frame WAM input, then a 24-frame hold outcome
before authorization, then a 24-frame endpoint outcome after measured arrival
and two seconds of settling. All captures use exact RGB/depth timestamps at
4 Hz and measured Gazebo poses within 12 ms. Evaluation capture fails if the
first post-cutoff frame has fallen out of the buffer. The active AP hold remains
checked throughout capture. Output uses `yokohama_rgbd_evaluation.v1` with empty
history indices, `future_frames_included=true`, and `evaluation_only=true`;
`load_capture` rejects that schema as model input.

`scripts/verify_yokohama_paired.py --root <run> --export-dir <new-directory>`
reopens hashed capture assets, binds the pair to the recorded activated permit
and WAM mailbox, verifies input arrays exactly, and separates `inputs/` from
`heldout/`. The hold evaluation frame is the fourth post-input frame (+1 observed
simulator second); the endpoint evaluation frame is the first after settling.
Their poses must be within 0.30 m / 0.05 rad of the **original requested** camera
target. Actual future poses never replace the model's requested target.
The original native and SITL verifiers must also pass.

## Appearance hypothesis

`fill_infinite_appearance` is an offline diagnostic, not an enabled native
inference mode. It uses only the last past RGB image and raw depth. Reverse
rotation maps target rays to source rays at infinite distance. Only positive
infinite depth on upward rays (negative world NED Z) can supply appearance.
NaN, negative infinity, zero depth, horizontal/downward rays and out-of-view
rays remain unfilled. The upstream projection's metric-valid pixels are
unchanged at sensor resolution. Resampling can blend boundary colors.

The separate appearance mask must never become a metric depth, visibility,
collision-clearance, or free-space mask. An infinite-range return is only an
appearance hypothesis; it is not general semantic sky recognition.

## Frozen offline evaluation

Use two locations, hold and the fixed fixture translation at each, and three
conditions per pair: original projection/t4, appearance projection/t4, and
appearance projection/t1. Keep seed 42, 250 diffusion steps, released checkpoint
and 16-frame context fixed. There is no t0 condition, whole-current-image
substitution, future-image upload, model retraining, candidate rewrite, or
post-result threshold adjustment.

Dataset time offsets 1 and 4 are not verified physical seconds. The source
training loader uses a frame-index difference divided by 128; its metric
translation normalization uses waypoint spacing 3.30 m. Report the actual
elapsed observation time separately, especially for the endpoint capture.

For this conservative near-view appearance pilot, predeclare RGB MAE <= 15,
no more than 10% of pixels with any channel error > 40, and inference <= 75 s.
A condition must pass all four pairs and visual inspection for invented water,
skylines or changed route geometry before considering a new native-flight
protocol. These are appearance criteria, not navigation success or generic
safety guarantees. A numeric pass alone never authorizes dispatch. Preserve all
conditions and failures; keep prior runs' looser consistency metrics unchanged.

The GPU payload contains only past history, the original candidate requests,
the last raw depth and frozen source/protocol. Keep evaluation frames local
until inference has finished. Bound instance lifetime, include disk/IP/transfer
reserves in the cumulative estimate, collect evidence, delete owned resources,
and verify absence before closing the estimate.

## Simulator heading qualification

A first otherwise successful fixture flight had 0.0587 m endpoint position
error but 0.1130 rad camera rotation error at D1. It was rejected for paired
forecast evaluation before any GPU call. Later frames were not substituted.
Two subsequent preflight attempts with `EKF2_DECL_TYPE=0` also failed the new
physical-heading check. Zero does not activate `EKF2_MAG_DECL` in this build.
All three attempts remain separate evidence.

This specific simulator has PX4 `381149fb012762f5e38c4a7fdc1b905b28038970`
and Gazebo `8.11.0-1~noble`. Gazebo replaces the SDF field using its geographic
lookup table, emits a NED field through an ENU pose, and this PX4 bridge maps
`x=-field.y`, `y=-field.x`. The resulting geographic declination enters with
the opposite sign from the desired heading. The source world field alone is
therefore not the right calibration input.

Parameter-only trials did not qualify: attempts 2/3/5/6 stopped before arming;
attempt 4 aligned before arming but failed the physical arrival-yaw bound.
These trials are retained. The final bounded configuration instead sets
`EKF2_MAG_TYPE=6` before startup and uses PX4's `commander set_heading 90`
once while disarmed, based on the authored spawn orientation (zero ENU yaw).
The actual spawn orientation and resulting estimated heading must agree within
0.03 rad before arming. This is an explicit initial heading reference, not a
repair to upstream magnetometer physics. No measured future pose, future image,
or continuous ground-truth heading is fed to PX4. Subsequent attitude/navigation
uses PX4's inertial/GNSS estimator.

This workaround is simulator-only; it does not establish a usable real-aircraft
heading source. Runtime versions, initialization command and resulting PX4 state
are recorded. The worker independently checks physical yaw during hold and at
segment arrival. The paired verifier still requires full camera orientation
within 0.05 rad of the original requested view.

`verify_yokohama_paired.py --capture-only` may qualify the two completed pairs
after both arrivals and session revocation while the remaining AP route runs.
It labels its scope `paired_capture_only` and preserves the terminal flight
status as pending or observed. This permits offline inference on already
recorded views; it never proves delivery/return or enables native flight.
Publication still requires the complete SITL and decision verifiers.

Primary source references:

- [Gazebo 8.11 magnetometer geographic field and frame convention](https://github.com/gazebosim/gz-sim/blob/gz-sim8_8.11.0/src/systems/magnetometer/Magnetometer.cc)
- [Pinned PX4 bridge magnetometer conversion](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/simulation/gz_bridge/GZBridge.cpp#L389)
- [Pinned PX4 getMagDeclination bit handling](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/ekf2/EKF/aid_sources/magnetometer/mag_control.cpp#L607)

- [Pinned PX4 one-time heading initialization command](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/commander/Commander.cpp#L487)
