# Yokohama wind stress and battery video

Live simulation remains opt-in through `scripts/yokohama_sitl.py --approve-sitl`.
Use `--wind-east-mps 2` to activate Gazebo 8 WindEffects; non-finite values,
negative speeds and speeds above 5 m/s are rejected before launch. Default 0
preserves previous worlds and behavior. This is a uniform force approximation,
not an operational wind rating or calibrated aerodynamic model.

`--wind-after-takeoff` additionally requires `--sea-round-trip` and nonzero wind.
It starts from a zero vector, waits for the existing 30-second SEA-TAKEOFF hold,
publishes the requested vector, and verifies Gazebo's wind-info service response.
It does not prove takeoff in wind. Keep immediate-onset failures in reports.
No hold, city arrival, contact or cargo receipt tolerances are relaxed.

WindEffects applies `mass * (wind - link velocity) * 1/s` at each opted-in
link's center of mass. Enable it on x500 base_link and the dynamic cargo link;
stock rotor drag remains active. No gusts, building wakes, sea waves or moving
ship are modeled. The world/model hashes bind these settings. A gravity-free,
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
88-pixel header without obscuring or modifying the retained original images.
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
