# Offshore splashdown instead of an unqualified tower catch

The `sixdof_managed_splashdown` option keeps launch, orbit and 26 rigid-payload
releases, and gives the booster a separately approved controlled water-entry
goal. Capture is unavailable in this trial; MissionOS selects splashdown and
independent checks bind the offshore reference, speed/pose limits and reserve.

The synthetic goal is a 10 km radius area around the existing 30 km east
reference. Contact speed must be at most 5 m/s, downward speed 2 m/s, tangential
speed 3 m/s, tilt 5 degrees, body rate 0.02 rad/s, and remaining fuel 10 t.
These are declared development-test conditions, not a real maritime clearance.

One Gateway fixture now records launch, orbital conditions, 26 releases and a
booster entry at 1.88 m/s, downward speed 1.43 m/s, tilt 0.30 degrees and 11.42 t
remaining fuel, 8.37 km from the area center. Independent record checks and the
declared entry envelope pass. Both same-start branches use the same local
water-entry controller, so this does not establish an AI advantage.
[Source-bound public summary](../assets/starship-splashdown-20261007/summary.json)
includes the rejected higher-speed and insufficient-reserve trials.
This is one deterministic trajectory used for development. The 1.5 m/s target
was selected after a 1.0 m/s probe failed the reserve condition; the same flight
was used to evaluate that selection. Two unchanged-controller saved-separation
fuel perturbations both failed: −1 t produced a 165 m/s contact despite 81.6 t
unspent propellant; +1 t contacted
at 1.90 m/s but retained only 9.21 t fuel. These are perturbed initialized
separation states, not fresh launches or proof of robust recovery. No subsequent
controller adjustment was made to make those failures pass.
The −1 t case requested terminal braking but its recorded landing-burn samples
show no main-engine thrust above the reporting threshold. The ignition/alignment
failure mechanism remains undiagnosed; this is not explained by fuel scarcity.

The finite controller uses hull-to-surface clearance during terminal descent
and uses the empirically selected 1.5 m/s downward target. The 2 m/s entry limit
remains unchanged, but a robust tracking margin has not been established.
It continues from the exact launch-derived separation state, including engines,
fuel, attitude and rates. No position/velocity assignment creates arrival.

The surface is a zero-elevation WGS84 sea-level proxy. Wave interaction,
buoyancy, structural survival and reuse after splashdown are not simulated.
Passing the entry envelope therefore confirms only the measured model entry.

Try it with the keyless Gateway and operator, or chat:

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/splashdown-state --port 18932
python scripts/smoke_starship_chat_gateway.py --port 18932 \
  --scenario sixdof_managed_splashdown --output-dir output/splashdown-http
```

Use fresh directories. The operator approves the v3 goal before running.
Fixture decisions are scripted; this example does not claim new Jev/DeepSeek
inference or AI superiority. The original return/catch failure records remain
separate from this goal.

SpaceX has used booster soft splashdown as a flight-test objective; see its
[flight updates](https://www.spacex.com/updates/reusability). This example does
not reproduce an identified SpaceX controller or its operational landing area.
