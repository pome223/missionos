# MotorLoadBatteryCoupler — motor-load → battery coupling (segment C, full)

This gz-sim System plugin makes a multicopter battery discharge as a function of
**actual rotor effort**. It samples the x500 rotor joint velocities each
`PreUpdate`, computes an aggregate electrical load, integrates state-of-charge
from that load, and publishes the result as a `gz.msgs.BatteryState` topic.

## Why the coupler owns the battery

gz-sim 8 (Harmonic)'s stock `LinearBatteryPlugin` only exposes its discharge as
an **on/off** `<power_draining_topic>` — there is **no proportional power-set
interface** another system can call. So to get a faithful proportional coupling
the coupler integrates the battery itself and publishes `BatteryState` on the
same topic the probe reads (`/model/<model>/battery/<battery_name>/state`). When
coupling is enabled the probe injects **only** this plugin (not the
LinearBatteryPlugin).

## Truth-surface

- This is a **Gazebo SITL simulation model** — a physics-coupled *simulated*
  endurance signal, **not real power-module endurance evidence**.
- The probe reads the `BatteryState` topic as a **separate** observed signal
  (`gz_battery_*`) and never overwrites the PX4 `battery_status` estimate.

## Power + SoC model

```
omega_sum2 = Σ_i (omega_i)^2
load_w  = clamp(idle + (hover-idle) * omega_sum2/(n * hover_rad_s^2), idle, max)
voltage = v_empty + (v_full - v_empty) * soc
current = load_w / voltage
charge_ah -= current * dt_hours
```

Propeller *shaft* power grows ~`omega^3`, but the battery consumes *electrical*
power. The `omega^2` normalization to a measured hover figure (`hover_power_w`
at `hover_rotor_rad_s`) is a deliberately conservative, calibratable first-order
coupling. Recalibrate `hover_power_w` / `hover_rotor_rad_s` from a live hover.

## Live verification (gz-sim 8.11.0, arm64)

Built against the runtime image and loaded into a minimal world; rotors driven
by a JointController:

| state | voltage | current | power (V·I) | percentage |
|-------|---------|---------|-------------|------------|
| idle (ω=0)    | 25.14 V | 0.477 A | ≈ 12 W  | 98.6 % |
| hover (ω≈700) | 24.39 V | 7.379 A | ≈ 180 W | 80.8 % |

Current jumps ~15× from idle to hover and SoC drains faster — i.e. battery
discharge tracks rotor effort. V·I matches `idle_power_w=12` / `hover_power_w=180`.

## SDF usage

```xml
<plugin filename="MotorLoadBatteryCoupler"
        name="boiled_claw::MotorLoadBatteryCoupler">
  <battery_name>linear_battery</battery_name>
  <state_topic>/model/x500_0/battery/linear_battery/state</state_topic>
  <rotor_joint>rotor_0_joint</rotor_joint>
  <rotor_joint>rotor_1_joint</rotor_joint>
  <rotor_joint>rotor_2_joint</rotor_joint>
  <rotor_joint>rotor_3_joint</rotor_joint>
  <idle_power_w>12.0</idle_power_w>
  <hover_power_w>180.0</hover_power_w>
  <hover_rotor_rad_s>700.0</hover_rotor_rad_s>
  <rotor_velocity_slowdown>10.0</rotor_velocity_slowdown>
  <max_power_w>600.0</max_power_w>
  <capacity_ah>5.2</capacity_ah>
  <voltage_full_v>25.2</voltage_full_v>
  <voltage_empty_v>21.0</voltage_empty_v>
  <publish_rate_hz>2.0</publish_rate_hz>
</plugin>
```

> Confirmed against the runtime x500: the rotor joints are
> `rotor_0_joint`..`rotor_3_joint` (in `x500_base`). Names that don't resolve
> are logged and skipped.

### The `rotor_velocity_slowdown` factor (critical for real PX4 flight)

PX4's x500 spins each rotor with `gz-sim-multicopter-motor-model-system`, which
has `<rotorVelocitySlowdownSim>10</rotorVelocitySlowdownSim>`. That means the
**visual joint** turns at `true_motor_speed / 10`, and the `JointVelocity`
component this coupler reads carries that *slowed* value. Without compensation
the coupler sees ~70 rad/s at hover (not ~700), computes `(70/700)² ≈ 0.01`, and
stays pinned near idle — exactly the failure first observed in a live PX4 flight
(0.559 A ≈ idle while hovering at 10 m). Set `<rotor_velocity_slowdown>` to the
model's `rotorVelocitySlowdownSim` (10 for the stock x500) so the coupler
recovers the true motor speed before applying the power model. The isolated
JointController test world drove the joint at the true speed directly, so it did
not need this — which is why the bug only surfaced under the real motor model.

## Build (inside / against the px4-gazebo image)

The image ships gz-sim 8.11.0 dev headers. Install a compiler + the cppzmq dev
package (pulled in transitively by gz-transport13), then:

```bash
apt-get install -y g++ ninja-build cppzmq-dev libzmq3-dev
cmake -S . -B build -G Ninja -DGZ_VERSION=harmonic   # or garden
cmake --build build
# -> libMotorLoadBatteryCoupler.so
```

Then either (a) bake it into the image on `GZ_SIM_SYSTEM_PLUGIN_PATH`, or (b)
mount the prebuilt `.so` at runtime via
`MISSIONOS_AUTO_RUNTIME_GZ_COUPLER_PLUGIN_SO=/abs/path/to/libMotorLoadBatteryCoupler.so`
(the probe mounts it and sets `GZ_SIM_SYSTEM_PLUGIN_PATH`).

## Enabling at runtime

```bash
export MISSIONOS_AUTO_RUNTIME_L1_GAZEBO_CARGO=1           # required: gates the
                                                          # coupler injection path
export MISSIONOS_AUTO_RUNTIME_GZ_PHYSICAL_BATTERY=1        # physical battery
export MISSIONOS_AUTO_RUNTIME_GZ_BATTERY_MOTOR_COUPLING=1  # inject this coupler
export MISSIONOS_AUTO_RUNTIME_GZ_COUPLER_PLUGIN_SO=/path/libMotorLoadBatteryCoupler.so
```

> The coupler injection + `.so` mount currently live inside the L1-cargo branch
> of the probe's `_start_container`, so `MISSIONOS_AUTO_RUNTIME_L1_GAZEBO_CARGO=1`
> must be set for the coupler to reach the spawned model. Without it the topic
> never publishes and every read is `gz_battery_stream_no_data_yet`.

## Real-PX4 SITL flight — verified end-to-end

A full PX4 SITL AUTO.MISSION flight (PX4's multicopter motor model spinning the
rotors — no JointController) confirms the coupling responds to *real* rotor
effort. The coupler loads live (`[MotorLoadBatteryCoupler] ... rotor_slowdown=10`)
and the battery drains proportionally to thrust:

| phase | altitude | current | charge | percentage |
|-------|----------|---------|--------|------------|
| pre-arm idle              | 0 m     | ~0.48 A | —        | 100 %  |
| motor spin-up (t≈1–3 s)   | 0 m     | 2.0 → 8.4 A | 5.198 Ah | 99.97 % |
| climb peak (t≈4 s)        | 0.4 m   | **9.37 A** | 5.193 Ah | 99.86 % |
| cruise (t≈13–146 s)       | 10 m    | **~8.8 A** | 5.171 → 4.840 Ah | 99.4 → 93.1 % |

Over a 150 s monitored flight (136 telemetry samples, 135 with an observed
BatteryState, no guard-abort, 684 m progress) the pack drained ~6.9 % of charge
while sustaining ~8.8 A under cruise thrust — an ~18× current increase over the
~0.48 A idle baseline.

### Two findings this flight surfaced (both fixed)

1. **`rotorVelocitySlowdownSim` compensation** — before `<rotor_velocity_slowdown>`
   the coupler read the 10×-slowed visual joint velocity and stayed pinned near
   idle (0.559 A while hovering). See the section above.
2. **Non-blocking observation** — the probe must read BatteryState via a
   *persistent background subscriber*, not a per-sample `gz topic -e -n 1`: the
   latter redoes transport discovery and blocks the control loop on every sample,
   which starved the loop badly enough that PX4 dropped AUTO.MISSION mode and the
   flight guard-aborted. The streaming read keeps the loop healthy (136 samples
   vs 2).

> Truth-surface: these are Gazebo-SITL-simulated currents/SoC, **not real
> power-module endurance evidence**.
