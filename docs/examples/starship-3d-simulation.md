# The older 3DOF reference study and 3D replay

This opt-in model integrates translation with a simplified drag/gravity/thrust
schedule. Its illustrative orientation is not integrated six-DOF attitude.
Public observations, model assumptions, and a display replay remain separate
from measured spacecraft engineering data. For coupled attitude and finite
actuators, use the [6DOF model](starship-sixdof.md).

```sh
python scripts/run_starship_3d.py --approve-simulation \
  --scenario flight14_inspired --output-dir output/starship-3d-new-run
python scripts/run_starship_3d.py --verify output/starship-3d-new-run/study.json
```

Use a new directory for each invocation. The command generates saved records,
source/runtime provenance, a manifest, and an offline report. Verification
checks the declared numerical/evidence contract; it does not turn a failed
return into success. The report's 2D/3D views are evidence displays.

The historical Flight 14-inspired comparison includes explicit synthetic fault
and nominal counterfactual choices. One-point development calibration is not
out-of-sample vehicle identification. Thermal/TPS, attitude control, structural
loads, slosh, realistic aero, and satellite service are not established.

For the normal approval/worker route and public short fixture, use the
[MissionOS showcase](starship-mission-showcase.md). The precise older-model
fields and reference assumptions are in the [3DOF contract](../agents/starship-3d-contract.md).
