# Choosing when to wait or detour

Step 2 starts with two urban conditions: a moving obstruction clears the direct
route quickly, or it blocks the route for much longer. The ship mission pauses
at the urban entrance, observes the obstruction, then follows an approved wait
or detour route. PX4 remains responsible for flight throughout.

The first comparison uses three simple policies: always wait, always detour,
and choose using observed obstacle velocity. This establishes whether the choice
changes the outcome before introducing VLA or WAM.

Inspect the inexpensive analytic screen:

```sh
missionos ship-delivery urban-screen
```

The two constant-motion cases favor different actions, but the velocity rule
already matches the analytic best choice. That gives no evidence that a learned
predictor would improve these cases. The screen reports this explicitly.

Run a local PX4/Gazebo experiment with a new output directory:

```sh
missionos ship-delivery run-sitl --approve-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case short_clear --urban-policy constant_velocity \
  --output-dir /tmp/urban-short-rule
```

The compact scenario uses **100 m of sea and 200 m inland** to reduce repeated
test time. Omit `--scenario` to retain the default 1 km sea leg. The available
cases are `short_clear` and `long_block`; policies are `constant_velocity`,
`always_wait`, and `always_detour`.

The experiment uses Gazebo position observations, collision notifications, and
geometric clearance checks. It has no camera perception or learned model. The
obstruction is a scripted moving solid; the vessel is stationary. Successful
runs must still verify delivery and deck recovery independently.

For each case, compare the velocity rule with the opposite fixed action. After
four successful runs, `urban-compare` rechecks their raw evidence and reports
the measured difference:

```sh
missionos ship-delivery urban-compare \
  --run-dir /tmp/urban-short-rule --run-dir /tmp/urban-short-detour \
  --run-dir /tmp/urban-long-rule --run-dir /tmp/urban-long-wait
```

That initial screen tested whether these idealized cases offered decision
headroom. The later VLA/WAM capability work uses predeclared arrival, safety,
latency and compute bounds, with Rules remaining an independent constraint.
It does not require a learned model to outperform the idealized rule. Calling
a model alone does not establish verified mission completion.

See the [maintainer contract](../agents/ship-urban-decisions.md) for boundaries.

The [measured comparison report](../examples/ship-urban-delivery-report/REPORT-ja.md)
preserves the four verified runs, their scope limits, and an interactive replay
of the recorded urban trajectories.

The next perception checkpoint uses actual RGB images from a fixed entry
camera. A third condition slows the obstacle to a stop. The same image history
feeds fixed actions, a velocity rule and a stopping-aware rule. The experiment
screens analytic decision costs before another flight or model comparison;
it does not yet control the drone from its onboard camera.

See the [RGB screen contract](../agents/ship-urban-camera.md) for the opt-in
capture and archived-image verification commands.
The [RGB comparison report](../examples/ship-urban-camera-report/REPORT-ja.md)
contains the captured images and the observed policy proposals, with analytic
costs explicitly separated from the earlier flight measurements.

The [onboard comparison report](../examples/ship-onboard-report/REPORT-ja.md)
extends this to a camera attached to the aircraft. A stopping-aware rule and
local Gemma4 use observed images to choose wait or detour; PX4 executes the
approved route and delivery and recovery are checked separately. The report
includes inference time and failed development attempts. Gemma4 is a general
vision-language model. That comparison alone does not establish dedicated flight
VLA and action-conditioned WAM integration. The subsequent [native same-flight
report](../examples/ship-native-joint-report/REPORT-ja.md) records real AeroVLA
and ANWM participation, including the 1 km delivery and deck recovery. Its
bounded stationary-ship simulator results do not establish moving-deck or
ten-aircraft operation.
