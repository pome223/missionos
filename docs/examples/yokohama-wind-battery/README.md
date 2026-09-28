# Yokohama wind stress and simulated battery overlay

This bundle retains every CPU wind trial, including the immediate-onset preflight failure. Select a trial in [the replay](index.html); inspect [all results](summary.json) or the [Japanese report](REPORT-ja.md). Wind trials use fixture decisions, not native VLA/WAM. Earlier zero-wind native flight now has [timestamp-bound battery overlays](../yokohama-cargo-flight/index.html#battery-video).

Gazebo WindEffects applies a uniform, uncalibrated force approximation to the vehicle and cargo. An unpowered wind-enabled witness and wind-disabled control independently establish applied force. Delayed wind begins only after the measured takeoff hold; it does not qualify takeoff in wind. Retain world/source hashes and every failed trial. Success requires the unchanged flight, decision and payload verifiers.

PX4 battery percentages and voltage are time-based synthetic telemetry. Current and energy are unavailable; no Wh, endurance or inference/wind energy claim is made. Video frames use only preceding telemetry within 2 simulator seconds, otherwise UNAVAILABLE. Recorded camera PNGs remain unchanged; header, source hashes, per-frame bindings and the exporter are retained. This work uses no paid GPU.

City source: Yokohama City / Project PLATEAU (2024 catalog), adapted under CC BY 4.0. [Attribution](../yokohama-urban-scene/ATTRIBUTION.md).
