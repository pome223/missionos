# Booster return guidance diagnostics

The operational default remains `fixed_v1`. The three `site_return_*` variants
are failed development candidates, available only through the standalone
diagnostic API/CLI. None is promoted into a MissionOS approved mission catalog.
They do not hand off to the mechanical catch harness.

## Evidence boundary

All four runs start from the identical recorded six-DOF booster separation
state. Position, velocity, quaternion, angular rate, propellant and actuator
states are retained. No vehicle mass, aerodynamic coefficient, inertia,
propellant amount or engine specification was fitted or changed. The initialized
terminal catch campaign is a separate experiment and does not repair these
failed arrival trajectories.

The full-launch return failed before development: its contact-point speed was
8.275 m/s and its miss distance was 17.081 km. The later trials also failed:

| Policy | Contact-point speed | Site distance | Observation |
| --- | ---: | ---: | --- |
| `fixed_v1` | 8.275 m/s | 17.081 km | Low altitude descent slows, but misses site |
| `site_return_v1` | 579.301 m/s | 20.920 km | Early aligned three-engine burn exhausts fuel |
| `site_return_v2` | 1027.572 m/s | 9.631 km | Fuel-feasibility gate never requests the final burn |
| `site_return_v3` | 289.610 m/s | 9.457 km | Finite impact-mitigation burn reduces impact speed; still a failed return |

Values describe this unvalidated engineering profile, not SpaceX hardware or
flight performance. No variant reached the terminal capture corridor. The
search stopped after this bounded sequence; no parameter sweep was performed.

## Diagnosis and policy differences

The original terminal horizontal controller damps horizontal velocity without
feeding back site displacement. It can stop far from the tower. Its vertical
schedule uses center-of-mass altitude, so even at first hull contact it still
requests several metres per second of descent.

`site_return_v1` adds position feedback and hull clearance, tightens boostback
velocity tolerance to the site radius, and selects fewer boostback engines when
the requested acceleration is small. It also supplies finite thrust for terminal
attitude control. The resulting boostback lasts 70 seconds; landing is requested
near 84 km with downward speed about 1.47 km/s. The aligned three-engine burn
consumes the return fuel high above the ground. In the baseline, a large attitude
error had delayed actual ignition while later aerodynamic deceleration removed
much of the descent speed; that accidental delay was not a valid ignition law.

`site_return_v2` retains the original boostback behavior. During coast it requests
local-up attitude and derives attitude bandwidth from the configured jet-pair
moment and current inertia. Its landing preview integrates one-dimensional
descent with current measured drag area, atmospheric density, gravity, finite
engine spool and propellant consumption. Up to the configured 13 gimballed main
engines can be requested. The preview assumes aligned thrust and frozen drag
area; it is an estimate, not a certified reachable envelope. A numerical preview
loop approaching zero fuel was fixed before the completed trial; this changed no
physical parameter. The completed trial exposes a policy failure: requiring a
fully fuel-feasible stop prevents any burn even when impact is imminent.

`site_return_v3` retains actual spooled gimbal authority in the coast bandwidth
estimate. It also requests a finite burn at the estimated remaining deceleration
distance when complete arrest is fuel-infeasible, recording
`impact_mitigation_insufficient_predicted_fuel`. This mitigates the v2 impact but
does not make the return safe. Its burn begins at 509.85 seconds; ground contact
occurs at 518.785 seconds.

`_landing_engine_demand` is shared by the catch harness and is unchanged by these
variants. They change requested guidance only; the common six-DOF engine,
actuator, aerodynamic and contact solvers continue to determine actual motion.

## Reproduction

The standalone command binds the exact input study SHA256, requires its recorded
profile to equal the supplied profile and recorded profile hash, continues the
specified run's exact separation state, and records current source hashes before
and after execution. Source changes fail the run. Output directories must be
fresh; failed records are not overwritten. The flag authorizes local simulation
only and does not represent operational policy or authenticated human identity.
Both the producer and diagnostic entrypoint reject nonadvancing clocks, invalid
time steps, and requests exceeding 100,000 nominal integration steps. The
diagnostic provenance preserves both the requested and resolved horizon.

Generate a new compatible six-DOF study first. The historical private study is
not part of this public package. Replace the two uppercase input placeholders
with that study's path and actual SHA-256; an invalid binding is refused.

```sh
PYTHONPATH=. python scripts/study_starship_booster_return.py \
  --study YOUR_GENERATED_STUDY.json \
  --study-sha256 YOUR_64_CHARACTER_SHA256 \
  --scenario launch --policy site_return_v3 \
  --output output/booster-return-diagnostic-new --approve-simulation
```

This is an opt-in development diagnostic, not the public short fixture or a
request to repeat the historical campaign. The table above is a historical
negative summary; it is not a new public-checkout execution or a catch claim.

The regression tests cover input-state preservation for every policy, finite
fuel preview termination, engine availability, drag/speed response, jet versus
spooled-TVC authority, and the distinction between fuel-feasible braking and
impact mitigation. Passing these tests is not a successful return or catch.
