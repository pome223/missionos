# Public rotational reference

`nesc-case02-sim01-angular-rates.csv` contains all 301 sample times and the
three inertial-relative body angular-rate columns from NASA NESC's published
[Atmospheric Case 02, Sim 01 results](https://nescacademy.nasa.gov/workshop/FlightSim/2015/atmos_scn_02/Atmos_02_sim_01.csv).
The original numerical text is preserved. `nesc-source.json` records the full
114,000-byte source hash and the hash of this 18,518-byte column subset.

The [case definition and errata](https://nescacademy.nasa.gov/flightsim/2015/atmospheric/acc02)
specify a dragless, undamped tumbling brick. The
[body specification](https://nescacademy.nasa.gov/flightsim/2015/bodies)
provides its mass and principal moments. The case's initial *local-frame*
rates differ from the CSV's *inertial-relative* rates. The comparison initializes
from the latter and compares those same three columns at every recorded time.

This is a decoupled torque-free **rotational subsystem** comparison. It omits
the full case's translation, WGS84 geodesy, J2 gravity, atmosphere and local
attitude outputs. It is not a full NASA check-case reproduction, NASA
certification, flight telemetry or evidence of Starship vehicle fidelity.

The independent native comparison uses upstream
[Basilisk spacecraft](https://avslab.github.io/basilisk/Documentation/simulation/dynamics/spacecraft/spacecraft.html)
and [extForceTorque](https://avslab.github.io/basilisk/Documentation/simulation/dynamics/extForceTorque/extForceTorque.html)
at pinned package version `bsk==2.12.0`. It runs a fixed-mass rigid hub with
non-diagonal inertia, nonzero three-axis angular velocity and prescribed
body force and torque. Both position and attitude are integrated. The comparison
uses every native output sample and treats quaternion sign reversal as the same
orientation. It does not validate variable-mass transport or Starship airloads.

Run with an environment containing the optional `spaceflight` dependency:

```sh
python scripts/check_starship_sixdof.py --approve-simulation --native-basilisk --output-dir output/starship-sixdof-validation
```

Choose a new output directory for each run. The JSON contains complete compared
traces, numerical limits, source hashes and whether native Basilisk was invoked.
Omitting `--native-basilisk` explicitly leaves that comparison unexecuted.
