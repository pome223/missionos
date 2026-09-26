# Ship-based delivery

The first development step is one drone carrying one payload from a stationary
ship, crossing about 1 km of sea, delivering in a small urban area, and returning
to the ship. It has two execution paths: a fast deterministic fixture and an
opt-in PX4/Gazebo simulation with flight dynamics and a detachable payload.

```text
Ship launch → sea transit → urban delivery → urban exit → sea return → recovery
```

Both paths check delivery and recovery separately. Reaching the destination
does not establish payload delivery, and returning near the ship does not
establish recovery. Each path also checks that the same vehicle and payload
remain associated with the run.

With local Docker running and the `px4io/px4-sitl-gazebo:latest` image available,
run the PX4/Gazebo path from the configured repository environment:

```sh
missionos ship-delivery run-sitl --approve-sitl --output-dir /tmp/ship-sitl-run
```

Use a new output directory for each run. The default flight takes roughly six
minutes. The drone takes off from a stationary deck, crosses 1 km of sea, flies
200 m inland, releases a simulated 50 g parcel from a low hover, and returns.
MissionOS waits for observed parcel separation, pad contact, and stability before
authorizing the return leg. Recovery requires deck contact, PX4 landed/disarmed
state, and stability. The result and raw observations are saved in the output
directory. A blocked run exits with status 1.

This first world has static buildings beside a clear flight corridor. Wind is
applied after takeoff with an uncalibrated force model. It does not validate
strong-wind endurance, moving-deck recovery, visual obstacle avoidance, or
physical delivery. VLA and WAM remain off.

In the fixture, an urban blockage gives MissionOS a bounded decision: wait,
take a detour, or stop when the approved time and energy limits cannot support
the mission.
The decision changes the simulated execution. The selector is a deterministic
policy; VLA and WAM are not invoked in this step.

From a configured repository environment, inspect and run the default scenario:

```sh
missionos ship-delivery plan
missionos ship-delivery run --approve-fixture --output /tmp/ship-delivery.json
```

The explicit approval applies only to the fixture scenario. Running without
`--approve-fixture` reports a blocked result and exits with status 1. Both commands
print JSON; `--output` also saves it.

To try an urban blockage, save this as `ship-scenario.json`:

```json
{"urban_blockage_s": 20, "max_wait_s": 30}
```

```sh
missionos ship-delivery plan --scenario ship-scenario.json
missionos ship-delivery run --scenario ship-scenario.json --approve-fixture
```

You can also inspect a proposed PX4 round-trip mission tape:

```sh
missionos ship-delivery px4-plan --output /tmp/ship-delivery-px4-plan.json
```

This legacy export produces waypoints only. Its timed delivery stop has no cargo
release or delivery verification gate, so the exported tape remains blocked for
execution. `run-sitl` instead builds two missions separated by an indefinite
hold and a verified-delivery gate. Only the scenario's distances and cruise
altitude map into this plan. Wind, battery, and
urban decisions are not connected to PX4 by this export.

The next steps are to measure whether learned prediction improves decisions
over a matched simple policy, add a moving ship
and deck motion, and then coordinate multiple drones up to a fleet of ten.
Those capabilities remain separate development milestones.

See the [maintainer contract](../agents/ship-delivery.md) for evidence and authority
requirements.

The [urban decision experiment](ship-urban-decisions.md) adds moving-obstruction
cases and model-free wait/detour comparisons as the next development slice.
