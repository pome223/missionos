# Yokohama wind stress and battery video

Live simulation remains opt-in through `scripts/yokohama_sitl.py --approve-sitl`.
Use `--wind-east-mps 2` to activate Gazebo 8 WindEffects; non-finite values,
negative speeds and speeds above 8 m/s are rejected before launch. Default 0
preserves previous worlds and behavior. This is a uniform force approximation,
not an operational wind rating or calibrated aerodynamic model.

`--wind-after-takeoff` additionally requires `--sea-round-trip` and nonzero wind.
It starts from a zero vector, waits for the existing 30-second SEA-TAKEOFF hold,
publishes the requested vector, and verifies Gazebo's wind-info service response.
It does not prove takeoff in wind. Keep immediate-onset failures in reports.
No hold, city arrival, contact or cargo receipt tolerances are relaxed.

WindEffects applies `mass * (wind - link velocity) * 1/s` at each opted-in
link's center of mass. Enable it on x500 base_link and the dynamic cargo link;
stock rotor drag remains active. Gusts are separately opt-in; building wakes,
sea waves and moving ships are not modeled. The world/model hashes bind these settings. A gravity-free,
unpowered witness at `[0, 3000, 100]` and a wind-disabled control at
`[0, 3010, 100]` are outside the mission geometry. The verifier checks their
observed displacement against the fixed force law and rejects missing force,
stale poses, disabled cargo wind and missing plugins. A configured wind vector
alone is insufficient evidence of applied force.

The delayed activation receipt bounds the start time between the pre-publish
and post-service snapshots. The witness equation tolerance includes that time
interval. Service response confirms the seed vector, not an independently
measured local flow. The witness provides the separate force observation.

## Battery display

`scripts/build_yokohama_battery_video.py --run RUN --output NEW_OUTPUT` is an
offline exporter; it starts no simulation, GPU or model. It checks camera PNG
hashes, run/world bindings and PX4/Gazebo clock alignment. It adds a separate
88-pixel header (112 with wind receipts) without obscuring or modifying the retained original images.
An image receives only the latest preceding battery sample within 2 simulator
seconds. Missing, invalid, disconnected or stale values show UNAVAILABLE;
future samples and fabricated zeros are prohibited. Output includes an MP4,
preview, per-frame binding table and hashes of the source logs and exporter.

PX4 revision `381149fb012762f5e38c4a7fdc1b905b28038970` uses
[`battery_simulator`](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/simulation/battery_simulator/BatterySimulator.cpp):
time-based voltage while armed, minimum floor, and voltage reset after disarm.
The worker records `SIM_BAT_DRAIN=3600` and `SIM_BAT_MIN_PCT=0` at runtime.
Current `-1` is unavailable, not negative generation. Do not infer Wh, endurance,
wind cost, onboard accelerator cost or energy savings from these percentages.
Native cloud inference also does not simulate an onboard battery load.

For wind CPU trials, label `fixture` decisions explicitly. Successful wind
fixtures do not extend the zero-wind native-model qualification. Preserve
failed trials and distinguish generator/launch failures from flight outcomes.

## Authored harbour profiles

`--wind-profile harbor-nominal` selects offshore/harbour/coast/city seeds of
6/5/4/3 m/s; `harbor-upper` selects 8/7/6/4 m/s. These are user-supplied scenario
assumptions, not Yokohama observations, forecasts, surface-to-altitude conversions
or operational limits. Both require `--decision-backend fixture --sea-round-trip
--wind-after-takeoff` and exclude `--wind-east-mps`. Default runs are unchanged.

The city box is the authored waypoint XY bounds plus 15 m. Outside this box,
project the observed vehicle position onto the entry-to-ship axis. The coast
boundary is 400 m, offshore starts at 900 m, and the ship is at 1400 m. These
boundaries are synthetic, not surveyed geography. A 5 m hysteresis avoids
chattering. All seed vectors remain ENU +X; no southwest-weather claim.

The world seed follows one vehicle's region globally. This is not a simultaneous
spatial wind field and must not be reused for multiple independently positioned
aircraft. Height dependence and building shielding/wakes remain unimplemented.
The already released cargo
still receives the globally selected vector. Landings retain wind; launches
start calm and are not qualified as windy deck launches.

`wind-transitions.json` binds every confirmed transport change to a fresh observed
Gazebo pose, simulator times, phase, sequence, run and world. The verifier checks
the fixed profile and the entire witness response to the sequence of steps, not
only the initial seed. `all_zones_exercised` is separate from valid partial force
evidence; the flight verifier requires all four zones for a full-profile pass.
`wind-diagnostics.jsonl` records PX4 trajectory and controller setpoints plus
motor outputs at approximately 5 simulator-second intervals. Reads are serial;
use embedded PX4 timestamps rather than treating each record as simultaneous.
Do not infer saturation or root cause from a single output sample.

The existing `airspeed_mps` stage field sets `MPC_XY_CRUISE`, a ground-speed
command for these multicopter missions; it is not measured airspeed.

`scripts/smoke_yokohama_wind_profile.py --approve-sitl --flight-config
RUN/config.json --output-dir NEW_OUTPUT` exercises seven confirmed seed changes
and their witness responses using a teleported marker in a separate minimal
Gazebo world. It invokes no PX4, vehicle flight, model or GPU. Its explicit
`wind_validation_scope=transport-marker-only` cannot pass the flight verifier.

## Seeded gusts

Add `--gust-seed 20260928` to a `harbor-nominal` fixture flight or the transport
smoke. Other seeds in [0, 2**32-1] are accepted; upper-profile gusts are rejected
because the fixed peaks would not increase all upper-profile steady speeds.
The v2 profile stores the entire seeded schedule before launch; v1 profiles
without gusts remain verifiable. No GPU, model provider or hardware is enabled.

The clock starts at the first post-takeoff wind observation. First onset is
20–35 simulator seconds later, pulse duration 6–10 seconds, and the steady-wind gap
between pulses 45–75 seconds, over a frozen 3600-second window. Randomized
peaks are 7–8 offshore, 6–7 harbour, 5–6 coast and 8–12 m/s city. They are total
seed speeds, not increments. All are eastward with no vertical component.
WindEffects filters the rectangular seed with its 1-second time constant.
These are synthetic stresses, not local weather or calibrated urban turbulence.

The same schedule continues during travel, holds, delivery and return; there is
no redraw on failure or wall-clock pause. Each receipt includes `gust_id`,
`gust_epoch_sim_s`, `steady_speed_mps` and the requested seed. Fresh poses and
confirmed transport are still required. More than 2 simulator seconds late at
an onset/end, or a blocked worker skipping a whole pulse, fails the trial.
The verifier regenerates the canonical schedule, requires scheduled onset/end
receipts within that delay, and checks the entire physical witness transient.
For short pulses it requires at least three fresh force samples 0.5–10 s after
each transition, including the combined response if a zone boundary follows
immediately. It does not isolate each near-simultaneous impulse or require
steady state. Incomplete pulses cannot
establish recovery. Unvisited city peaks remain untested even if sea gusts pass.

Video wind labels use only receipts confirmed before the frame timestamp,
with a separate unconfirmed label on failed transport. They identify the
accepted global seed, not measured local flow. Raw RGB remains unchanged.

## Straight sea endpoints

Sea stages now send one endpoint waypoint followed by a loiter at that endpoint.
The geometric line, 1 km offshore distance, ground-speed command, deadlines,
0.5 m waypoint acceptance radius, and independent arrival/hold bounds remain
unchanged. City paths retain their existing waypoint and model constraints.
This removes unnecessary 20 m fly-through acceptance dependencies on the
obstacle-free authored sea lines; it does not patch PX4 or relax verification.
Validate with trajectory, mission index and endpoint hold evidence, not mode
transitions alone. This fixes a route construction failure mode, not a hardware
wind-tolerance qualification.
